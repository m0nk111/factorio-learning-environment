import time
from typing import Dict, List, Optional

from mcp.server.fastmcp import Image
from mcp.types import ImageContent

from fle.commons.cluster_ips import get_local_container_ips
from fle.env.entities import Position
from fle.commons.models.game_state import GameState

from fle.env.protocols._mcp.models import FactorioServer
from fle.env.protocols._mcp.init import log_info, state, initialize_session
from fle.env.protocols._mcp import mcp


# ============== CONNECTION HELPERS ==============


def is_lost_rcon_connection(instance, error: BaseException) -> bool:
    """Whether `error` means the RCON socket died instead of the code being wrong."""
    if isinstance(error, TimeoutError):
        # Submitted code ran out of time; the socket is usually still fine and a
        # retry would only repeat the same slow failure.
        return False
    if isinstance(error, ConnectionError):
        return True
    is_connected = getattr(instance, "is_rcon_connected", None)
    return callable(is_connected) and not is_connected()


def discover_servers() -> Dict[int, FactorioServer]:
    """Rebuild the server table from the Factorio containers running right now."""
    ips, _udp_ports, tcp_ports = get_local_container_ips()
    return {
        server_id: FactorioServer(
            address=address,
            tcp_port=int(tcp_port),
            instance_id=server_id,
            name=f"Factorio Server {server_id + 1}",
            last_checked=time.time(),
        )
        for server_id, (address, tcp_port) in enumerate(zip(ips, tcp_ports))
    }


def close_rcon_client(instance) -> None:
    """Release the RCON socket held by `instance`, ignoring already-dead sockets."""
    close = getattr(getattr(instance, "rcon_client", None), "close", None)
    if not callable(close):
        return

    try:
        close()
    except Exception as error:
        log_info(f"Ignoring error while closing the stale RCON client: {error}")


def previous_server_id() -> Optional[int]:
    """Instance id of the server this session is attached to, when it is known."""
    active_server = state.active_server
    if active_server is None:
        return None

    for server_id, server in state.available_servers.items():
        if server.tcp_port == active_server.tcp_port:
            return server_id
    return None


#
@mcp.tool()
async def render(center_x: float = 0, center_y: float = 0) -> ImageContent:
    """
    Render the current factory state to an image

    Args:
        center_x: X coordinate to center on (defaults to factory center)
        center_y: Y coordinate to center on (defaults to factory center)
    """
    if not state.active_server:
        raise Exception("No active Factorio server connection. Use status first.")

    instance = state.active_server

    try:
        img = instance.namespace._render(position=Position(center_x, center_y))
        if img is None:
            raise Exception(
                "Failed to render: Game state not properly initialized or player entity invalid"
            )
        return Image(data=img._repr_png_(), format="png").to_image_content()
    except Exception as e:
        raise Exception(f"Error rendering: {str(e)}")


@mcp.tool()
async def execute(code: str) -> str:
    """
    Run Python code and automatically commit the result.

    All API methods are already imported into the namespace, so no need to import any Factorio methods or types (e.g Direction, Prototype etc)

    If you are confused about what methods are available, use `ls` followed by `man` to read the manual about a method.

    If you need to debug an error message, use the introspection tools (e.g `cat`) to analyse the *.lua and *.py implementations.

    Args:
        code: Python code to execute
    """
    if not state.active_server:
        await initialize_session(
            None
        )  # "No active Factorio server connection. Use connect first."

    vcs = state.get_vcs()
    if not vcs:
        return "VCS not initialized. Please connect to a server first."

    instance = state.active_server

    # Execute the code
    try:
        result, score, response = instance.eval(code, timeout=60)
    except Exception as error:
        if not is_lost_rcon_connection(instance, error):
            raise
        # The socket behind this session is gone (typically because the container
        # was recreated on new host ports): re-discover once and retry this code.
        log_info(
            f"RCON connection lost ({type(error).__name__}: {error}); "
            "re-discovering Factorio containers and retrying once"
        )
        reconnect_report = await reconnect()
        if state.active_server is None or state.active_server is instance:
            raise Exception(
                f"Execution failed: {error}\nReconnect failed: {reconnect_report}"
            ) from error
        instance = state.active_server
        vcs = state.get_vcs()
        result, score, response = instance.eval(code, timeout=60)

    # Automatically commit the successful state
    current_state = GameState.from_instance(instance)
    commit_id = vcs.commit(current_state, "Auto-commit after code execution", code)

    # Update game state file after execution
    try:
        game_state_data = {
            "inventory": instance.namespace.inspect_inventory(),
            "entities": instance.namespace.get_entities(radius=100),
            "score": instance.namespace.score(),
            "game_tick": instance.namespace.game_info.tick,
            "position": instance.namespace.player_location,
            "production_stats": instance.namespace.get_production_stats(),
        }

        import json
        from pathlib import Path

        state_file = Path("/tmp/factorio_game_state.json")
        with open(state_file, "w") as f:
            json.dump(game_state_data, f, default=str)
    except Exception:
        # Don't fail the whole execution just because state file update failed
        pass

    '/c local surface = game.player.surface; for key, entity in pairs(surface.find_entities_filtered({force="enemy"})) do; entity.destroy(); end'

    try:
        player_pos = instance.namespace.player_location
        # RCON command to move camera to player position
        rcon_cmd = f"/c game.players[1].teleport({{x={player_pos.x}, y={player_pos.y}}})"  # ; game.player.zoom_to_world(game.player.position, 1)'
        meta = instance.rcon_client.send_command(rcon_cmd)

        kill_biters_cmd = '/c game.forces["enemy"].kill_all_units()'
        instance.rcon_client.send_command(kill_biters_cmd)

        clear_rendering = "/c rendering.clear()"
        instance.rcon_client.send_command(clear_rendering)

    except Exception as e:
        # Don't fail execution if viewport move fails
        meta = str(e)

    return f"[commit {commit_id[:8]}] - stdio:\n{response}\n{meta}"


@mcp.tool()
async def reconnect() -> str:
    """
    Reconnect with the Factorio server

    Re-discovers the running Factorio containers, closes the RCON client this
    session holds and opens a fresh one at the endpoint the containers publish
    right now, so a session survives its container being recreated on different
    host ports.
    """
    attached_server_id = previous_server_id()

    try:
        servers = discover_servers()
    except Exception as error:
        return (
            "Reconnect failed: could not list the running Factorio containers "
            f"({type(error).__name__}: {error}). Check that Docker is reachable, "
            "start a container with `python fle/run.py cluster start -n 1` and call "
            "`reconnect` again."
        )

    if not servers:
        return (
            "Reconnect failed: no running Factorio container found. Start one with "
            "`python fle/run.py cluster start -n 1`, then call `reconnect` again."
        )

    close_rcon_client(state.active_server)
    state.active_server = None
    state.available_servers = servers

    # Retry the server the session was attached to first, then any other container
    # that discovery reported.
    server_ids: List[int] = sorted(servers)
    if attached_server_id in servers:
        server_ids.remove(attached_server_id)
        server_ids.insert(0, attached_server_id)

    failures = []
    for server_id in server_ids:
        server = servers[server_id]
        try:
            instance = state.create_factorio_instance(
                server_id, address=server.address, tcp_port=server.tcp_port
            )
        except Exception as error:
            failures.append(f"{server.address}:{server.tcp_port} ({error})")
            continue

        server.connected = True
        server.is_active = True
        state.active_server = instance

        vcs = state.get_vcs()
        commits = len(vcs.undo_stack) if vcs else 0

        return (
            f"Connected to Factorio server: {server.name} "
            f"({instance.address}:{instance.tcp_port})\n"
            f"Commit history: {commits} commits"
        )

    discovered = ", ".join(
        f"{server.address}:{server.tcp_port}" for server in servers.values()
    )
    return (
        f"Reconnect failed: discovered {discovered} but none answered RCON "
        f"({'; '.join(failures)}). Check the container (`docker logs <container>`) "
        "and call `reconnect` again."
    )
