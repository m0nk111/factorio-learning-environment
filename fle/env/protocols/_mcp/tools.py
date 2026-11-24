import time
from typing import Dict, List, Optional

from mcp.server.fastmcp import Image
from mcp.types import ImageContent

from fle.commons.cluster_ips import get_local_container_ips
from fle.env.entities import Position
from fle.commons.models.game_state import GameState

from fle.env.protocols._mcp.models import FactorioServer
from fle.env.protocols._mcp.init import log_info, state, initialize_session
from fle.env.protocols._mcp import config, mcp
from fle.env.protocols._mcp.state import release_level_handlers


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
    """Rebuild the server table from the Factorio containers running right now.

    A pinned direct endpoint is not a container: the server it names belongs to
    another process (a campaign, say), so the docker listing can never find it.
    Without this branch `reconnect` answered "no running Factorio container
    found" while the pinned server was running and answering on its RCON port.
    """
    direct = config.direct_rcon_endpoint()
    if direct is not None:
        host, port = direct
        if config.server_pin_name() is not None:
            raise ValueError(
                f"Conflicting Factorio target: {config.RCON_HOST_PIN_ENV}={host!r} "
                f"attaches to an endpoint directly, so {config.SERVER_PIN_ENV}="
                f"{config.server_pin_name()!r} cannot name a container as well"
            )
        return {
            0: FactorioServer(
                address=host,
                tcp_port=port,
                instance_id=0,
                name=f"{host}:{port}",
                last_checked=time.time(),
            )
        }
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


def server_preference_order(
    servers: Dict[int, FactorioServer],
    endpoint: Optional[tuple],
    attached_server_id: Optional[int],
) -> List[int]:
    """Order discovery results: same endpoint first, then the same instance id.

    Instance ids are positional, so a container that disappears shifts every id
    after it; matching the endpoint first keeps the session on the world it was
    working in.
    """

    def rank(server_id: int) -> int:
        server = servers[server_id]
        if endpoint is not None and (server.address, server.tcp_port) == endpoint:
            return 0
        return 1 if server_id == attached_server_id else 2

    return sorted(servers, key=rank)


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

    When the RCON connection is lost, the tool re-discovers the containers and
    reconnects once, then reports where the session landed together with the
    error. The submitted code is never re-executed automatically: whether the
    endpoint hosts the same world is not observable from here, so re-running a
    script that may already have mutated a world is left to the caller.

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
        # The socket behind this session is gone: heal the connection so the
        # caller can continue, but never replay code that may already have run.
        log_info(
            f"RCON connection lost ({type(error).__name__}: {error}); "
            "re-discovering Factorio containers"
        )
        previous_endpoint = f"{instance.address}:{instance.tcp_port}"
        reconnect_report = await reconnect()
        reconnected = state.active_server
        if reconnected is None or reconnected is instance:
            raise Exception(
                f"Execution failed: {error}\nReconnect failed: {reconnect_report}"
            ) from error

        raise Exception(
            f"Execution failed: {error}\n"
            f"The RCON connection was lost. The session reconnected to "
            f"{reconnected.address}:{reconnected.tcp_port} (was "
            f"{previous_endpoint}); the code was not executed there. Call "
            "`execute` again if that is the world you want."
        ) from error
    finally:
        # A world we do not own must stay joinable between calls.
        release_level_handlers(instance)

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

    meta = ""

    # Showing the agent by moving the player's camera, and clearing the biters so
    # a benchmark is not disturbed by them, are lab conveniences: they assume the
    # world is ours. On a world reached through a pinned endpoint, teleporting
    # players[1] yanks a human's character across the map and killing every enemy
    # unit deletes the player's biters. Neither is ours to do there.
    if config.direct_rcon_endpoint() is None:
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
    attached_endpoint = (
        None
        if state.active_server is None
        else (state.active_server.address, state.active_server.tcp_port)
    )

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

    server_ids = server_preference_order(servers, attached_endpoint, attached_server_id)

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
