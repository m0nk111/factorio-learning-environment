"""Discover the running Factorio containers and resolve an explicit target.

Two callers attach to a container by different routes: an MCP session
(`fle.env.protocols._mcp`) and the gym factory used by the evaluation harness
(`fle.env.gym_env.registry`). Both used to address a container purely by
position in this list, which silently attaches to whichever container happens
to sort first — a session then mutates a world it was never pointed at.

Everything here therefore reports identity (docker name, published host ports)
rather than a bare index, and :func:`select_container` resolves an explicit pin
or fails loudly with the candidates listed.
"""

import json
import subprocess
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

# Container-internal ports published by the image; used to pick the right
# binding when a container publishes more than one port of a protocol.
INTERNAL_RCON_PORT = 27015
INTERNAL_GAME_PORT = 34197


@dataclass(frozen=True)
class FactorioContainer:
    """One running Factorio container, addressed by its published host ports."""

    name: str
    container_id: str
    address: str
    udp_port: int
    tcp_port: int

    def describe(self) -> str:
        return (
            f"{self.name} (address={self.address} "
            f"rcon_port={self.tcp_port} game_port={self.udp_port})"
        )


class ContainerSelectionError(LookupError):
    """A pinned target does not resolve to exactly one container."""


def _published_port(ports: dict, protocol: str, preferred: int) -> Optional[int]:
    """Return the host port for `protocol`, preferring the image's internal port."""
    candidates = []
    for container_port, bindings in (ports or {}).items():
        if not container_port.endswith(f"/{protocol}") or not bindings:
            continue
        try:
            internal = int(container_port.split("/")[0])
        except ValueError:
            internal = -1
        candidates.append((internal, int(bindings[0]["HostPort"])))
    if not candidates:
        return None
    for internal, host_port in candidates:
        if internal == preferred:
            return host_port
    return candidates[0][1]


def list_factorio_containers() -> List[FactorioContainer]:
    """List running containers whose name contains ``factorio_``.

    Sorted by published game port then RCON port, so an index into the result
    is stable across calls (the historical contract of
    :func:`get_local_container_ips`).
    """
    cmd = ["docker", "ps", "--filter", "name=factorio_", "--format", '"{{.ID}}"']
    result = subprocess.run(cmd, capture_output=True, text=True)
    container_ids = [c.strip('"') for c in result.stdout.strip().split("\n")]
    container_ids = [c for c in container_ids if c]

    if not container_ids:
        print("No running Factorio containers found")
        return []

    containers = []
    for container_id in container_ids:
        inspect = subprocess.run(
            ["docker", "inspect", container_id], capture_output=True, text=True
        )
        container_info = json.loads(inspect.stdout)[0]
        ports = container_info["NetworkSettings"]["Ports"]
        udp_port = _published_port(ports, "udp", INTERNAL_GAME_PORT)
        tcp_port = _published_port(ports, "tcp", INTERNAL_RCON_PORT)
        if udp_port is None or tcp_port is None:
            # Not a game server we can talk to (no published game or RCON port).
            print(f"Skipping container {container_id}: no published game/RCON port")
            continue
        containers.append(
            FactorioContainer(
                name=container_info.get("Name", container_id).lstrip("/"),
                container_id=container_id,
                # The published host port is what we connect to; containers are
                # published on loopback by the cluster launcher.
                address="127.0.0.1",
                udp_port=udp_port,
                tcp_port=tcp_port,
            )
        )

    containers.sort(key=lambda c: (c.udp_port, c.tcp_port))
    return containers


def get_local_container_ips() -> tuple[List[str], List[int], List[int]]:
    """Get IP addresses of running Factorio containers in the local Docker setup.

    Returns parallel lists of address, game (UDP) port and RCON (TCP) port. Use
    :func:`list_factorio_containers` when you need to know *which* container you
    are about to address.
    """
    containers = list_factorio_containers()
    if not containers:
        return [], [], []
    return (
        [c.address for c in containers],
        [c.udp_port for c in containers],
        [c.tcp_port for c in containers],
    )


def format_candidates(containers: Iterable) -> str:
    """One line per candidate: name, address and published ports."""
    lines = []
    for container in containers:
        name = getattr(container, "name", None) or "<unnamed>"
        extra = ""
        if hasattr(container, "is_active") and not container.is_active:
            extra = " active=False"
        lines.append(
            f"  - {name} (address={container.address} "
            f"rcon_port={container.tcp_port} "
            f"game_port={getattr(container, 'udp_port', None)}{extra})"
        )
    return "\n".join(lines) if lines else "  (none)"


def select_container(
    candidates: Sequence,
    name: Optional[str] = None,
    rcon_port: Optional[int] = None,
):
    """Return the single candidate matching the pin.

    ``candidates`` may hold any objects exposing ``name``, ``address`` and
    ``tcp_port`` (:class:`FactorioContainer` and the MCP ``FactorioServer`` both
    do). No pin returns the first candidate. A pin that matches nothing, or a
    name and a port that point at different candidates, raises
    :class:`ContainerSelectionError` with every candidate listed — an ambiguous
    target must never fall back to "whatever sorts first".
    """
    candidates = list(candidates)
    if not candidates:
        raise ContainerSelectionError("no Factorio containers are running")

    if name is None and rcon_port is None:
        return candidates[0]

    matches = candidates
    if name is not None:
        matches = [c for c in matches if getattr(c, "name", None) == name]
    if rcon_port is not None:
        matches = [c for c in matches if int(c.tcp_port) == int(rcon_port)]

    if len(matches) == 1:
        return matches[0]

    pin = ", ".join(
        part
        for part in (
            f"FLE_SERVER={name!r}" if name is not None else None,
            f"FLE_RCON_PORT={rcon_port}" if rcon_port is not None else None,
        )
        if part
    )
    reason = "no running container matches" if not matches else "pin is ambiguous"
    raise ContainerSelectionError(
        f"{reason} {pin}. Candidates:\n"
        f"{format_candidates(candidates) if candidates else '  (none)'}"
    )


if __name__ == "__main__":
    found = list_factorio_containers()
    if found:
        print("Local Factorio containers:")
        for container in found:
            print(f"  {container.describe()}")
    else:
        print("No local Factorio containers found.")
