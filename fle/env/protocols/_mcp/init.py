"""
Initialization module for the Factorio MCP server with automatic connection
"""

import asyncio
import sys
from fle.commons.cluster_ips import (
    ContainerSelectionError,
    format_candidates,
    select_container,
)
from fle.env.protocols._mcp import config
from fle.env.protocols._mcp.state import FactorioMCPState, release_level_handlers

# Initialize server state
state = FactorioMCPState()
_initialization_lock = asyncio.Lock()
_initialized = False


# Safe logging function that never writes to stdout
def log_info(message):
    """Log a message to stderr to avoid MCP protocol corruption"""
    print(message, file=sys.stderr)


# async def initialize_servers_if_needed():
#     """Make sure we've initialized servers at least once"""
#     global _initialized
#     async with _initialization_lock:
#         if not _initialized:
#             await state.scan_for_servers()
#             _initialized = True
#


# Handle initialization at session start
async def initialize_session(ctx=None):
    """Attach the session to one Factorio server.

    The target is explicit when ``FLE_SERVER``/``FLE_RCON_PORT`` is set: the
    session connects to exactly that container, or raises with the candidates
    listed. Without a pin the historical choice is kept — the first active
    server — but every candidate is logged together with the one that was
    picked, so an attachment to the wrong world is visible instead of silent.
    """
    log_info("Initializing Factorio session...")

    try:
        pin_name = config.server_pin_name()
        pin_rcon_port = config.server_pin_rcon_port()

        # Scan for available servers. A pinned target is resolved and validated
        # before anything is probed (see State.scan_for_servers), so a
        # contradictory FACTORIO_SERVER_* cannot provision a world first.
        servers = await state.scan_for_servers(ctx)
        log_info(f"Found {len(servers)} servers")

        if not servers:
            return "No Factorio servers found. Please ensure Factorio containers are running."

        pinned = pin_name is not None or pin_rcon_port is not None

        log_info("Server candidates:\n" + format_candidates(servers))

        if pinned:
            server = select_container(servers, name=pin_name, rcon_port=pin_rcon_port)
            if not server.is_active:
                raise ContainerSelectionError(
                    f"Pinned server {server.name} ({server.address}:{server.tcp_port}) "
                    f"is not reachable: {server.system_response or 'unknown error'}. "
                    f"Candidates:\n{format_candidates(servers)}"
                )
        else:
            active_servers = [s for s in servers if s.is_active]
            log_info(f"Active servers: {len(active_servers)}")
            if not active_servers:
                return "No active Factorio servers available for connection"
            server = select_container(active_servers)

        log_info(
            f"Connecting to server {server.instance_id}: {server.name} "
            f"({server.address}:{server.tcp_port})"
            + (" [pinned]" if pinned else " [first active server]")
        )
        success = await state.connect_to_server(server.instance_id)

        if not success:
            return f"Failed to connect to server {server.name}"

        if state.active_server is None:
            return "Connection succeeded but active_server not set"

        return f"Connected to Factorio server {server.name} ({server.address}:{server.tcp_port})"
    except Exception as e:
        log_info(f"Error during initialization: {e}")
        return f"Initialization failed: {str(e)}"


async def shutdown_session():
    """Close the RCON connection and drop in-process state.

    This does not reset the game. Resetting here wiped the world the session
    was pointed at: one initialize-and-exit cycle turned a seeded save into a
    pristine regenerated map (belts and assemblers gone, trees re-rolled). The
    lab behaviour is opt-in through ``FLE_RESET_ON_SHUTDOWN=1``.
    """
    # Close any active connections
    server = state.active_server
    state.active_server = None
    try:
        if server is None:
            return
        log_info("Disconnecting from Factorio server...")

        # Unregister the level-script handlers before the socket goes away: on a
        # world we do not own they are what makes the save refuse every client.
        release_level_handlers(server)

        try:
            if config.reset_on_shutdown():
                log_info(
                    "FLE_RESET_ON_SHUTDOWN is set: resetting the game state on shutdown"
                )
                server.reset()
        except Exception as e:
            # Includes an invalid FLE_RESET_ON_SHUTDOWN value: report it, but the
            # connection must still be closed.
            log_info(f"Reset on shutdown failed: {e}")
        finally:
            try:
                server.cleanup()
            except Exception as e:
                log_info(f"Closing the RCON connection failed: {e}")
    finally:
        # Clear any temporary state
        state.close_gym_env()
        state.vcs_repos.clear()
