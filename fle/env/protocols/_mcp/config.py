"""Settings for the MCP surface, read from the environment at use time.

An MCP session attaches to a world that may already contain someone's factory,
so the defaults below never mutate it: closing a session only closes RCON, and
attaching does not re-provision the world. The lab behaviour — regenerate the
map on attach and wipe it on shutdown — stays available, but only when asked
for explicitly.

``FLE_SERVER``
    Pin the target container by docker name (e.g. ``factorio_1``). With a pin
    the session attaches to exactly that container or fails with the candidates
    listed; without one it keeps the historical "first active server" choice,
    logging every candidate and the one it picked.

``FLE_RCON_PORT``
    Pin the target container by published host RCON port instead of (or
    together with) the name.

``FLE_RESET_ON_CONNECT``
    Provision the world when a session attaches: reset game state, clear
    entities around the agents and apply the starting inventory. Default ``0``.

``FLE_RESET_ON_SHUTDOWN``
    Reset the world when a session ends. Default ``0``.
"""

import os
from typing import Optional

SERVER_PIN_ENV = "FLE_SERVER"
RCON_PORT_PIN_ENV = "FLE_RCON_PORT"
RESET_ON_CONNECT_ENV = "FLE_RESET_ON_CONNECT"
RESET_ON_SHUTDOWN_ENV = "FLE_RESET_ON_SHUTDOWN"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean flag (1/0, true/false), got {raw!r}")


def _env_port(name: str) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer port number, got {raw!r}")
    if not 0 < port < 65536:
        raise ValueError(f"{name} must be a valid port number, got {raw!r}")
    return port


def server_pin_name() -> Optional[str]:
    """Docker name the session must attach to, or None."""
    raw = os.environ.get(SERVER_PIN_ENV)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip()


def server_pin_rcon_port() -> Optional[int]:
    """Published host RCON port the session must attach to, or None."""
    return _env_port(RCON_PORT_PIN_ENV)


def reset_on_connect() -> bool:
    """Whether attaching a session is allowed to provision the world."""
    return _env_flag(RESET_ON_CONNECT_ENV, default=False)


def reset_on_shutdown() -> bool:
    """Whether closing a session is allowed to reset the world."""
    return _env_flag(RESET_ON_SHUTDOWN_ENV, default=False)
