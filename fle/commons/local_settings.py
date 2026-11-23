"""Local deployment settings for the MCP surface.

A machine that runs an MCP session against a *natively* run Factorio server — a
campaign, not a docker container — needs an endpoint that container discovery
can never find, plus that server's RCON password. Neither belongs in a
repository, an environment file that is committed, or a systemd unit: the
password is a secret and the endpoint is machine-local.

The file is read at use time, from ``$FLE_LOCAL_SETTINGS`` when set and
``~/.config/fle/mcp.json`` otherwise:

    {
      "rcon_host": "127.0.0.1",
      "rcon_port": 34199,
      "rcon_password_file": "/home/me/.config/fle/campaign-rcon.txt"
    }

``rcon_password`` may hold the secret inline instead of a file path. Environment
variables win over the file, so a caller can always override one setting without
editing it. A missing file is not an error; a malformed one is.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

SETTINGS_PATH_ENV = "FLE_LOCAL_SETTINGS"
DEFAULT_SETTINGS_PATH = "~/.config/fle/mcp.json"

_cached: Dict[str, Any] = {}
_cache_key: Optional[str] = None
_cache_stamp: Optional[float] = None


def settings_path() -> Path:
    return Path(
        os.path.expanduser(os.environ.get(SETTINGS_PATH_ENV) or DEFAULT_SETTINGS_PATH)
    )


def load() -> Dict[str, Any]:
    """Parsed settings, or an empty dict when the file does not exist."""
    global _cached, _cache_key, _cache_stamp
    path = settings_path()
    try:
        stamp = path.stat().st_mtime
    except FileNotFoundError:
        _cached, _cache_key, _cache_stamp = {}, str(path), None
        return {}

    # Re-read when the file changed, so a deployment can fix a value without
    # restarting every session.
    if _cache_key == str(path) and _cache_stamp == stamp:
        return _cached
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    _cached, _cache_key, _cache_stamp = data, str(path), stamp
    return data


def value(key: str) -> Any:
    """A single setting, ignoring a blank string as if it were absent."""
    raw = load().get(key)
    if isinstance(raw, str) and raw.strip() == "":
        return None
    return raw


def rcon_password() -> Optional[str]:
    """Password from ``rcon_password`` or ``rcon_password_file``, if configured."""
    inline = value("rcon_password")
    if inline:
        return str(inline)
    path = value("rcon_password_file")
    if not path:
        return None
    resolved = Path(os.path.expanduser(str(path)))
    try:
        return resolved.read_text().strip() or None
    except FileNotFoundError:
        raise ValueError(f"rcon_password_file does not exist: {resolved}") from None
