"""Regression tests: an MCP session must not wipe or mis-target the world.

Two production defects are covered here, both observed against a real sandbox
container:

1. Closing a session reset the game. ``shutdown_session`` called
   ``state.active_server.reset()``, which runs ``game.reset_game_state()`` in
   Lua and restores a pristine generated map. One initialize-and-exit cycle
   turned a seeded save (162 belts, 4 assemblers, 43,758 trees) into a bare map
   (0 belts, 0 assemblers, 57,272 trees). Closing a session may only close RCON
   and drop in-process state; the lab behaviour is opt-in through
   ``FLE_RESET_ON_SHUTDOWN=1``.

2. The session attached to whichever ``factorio_`` container sorted first. With
   two containers running, ``active_servers[0]`` was the wrong world. The target
   can be pinned with ``FLE_SERVER``/``FLE_RCON_PORT``; without a pin every
   candidate and the chosen one are logged, so a wrong attachment is visible.

Neither the module import nor these tests touch Docker: no state is built, no
RCON connection is opened, and the container scan is replaced with a fake.
"""

import asyncio
import importlib
from dataclasses import dataclass, field
from typing import List

import pytest

# The package re-exports an attribute named `state` (the state instance), so
# `import ..._mcp.state as state_module` would bind that instance instead of the
# module; go through the import machinery to get the module itself.
state_module = importlib.import_module("fle.env.protocols._mcp.state")

from fle.commons.cluster_ips import (  # noqa: E402
    ContainerSelectionError,
    FactorioContainer,
    _published_port,
    select_container,
)
from fle.env.protocols._mcp import config  # noqa: E402
from fle.env.protocols._mcp import init as session_module  # noqa: E402
from fle.env.protocols._mcp.state import FactorioMCPState  # noqa: E402


@dataclass
class _RecordingInstance:
    """Stand-in for FactorioInstance as ``active_server``."""

    name: str = "factorio_1"
    calls: List[str] = field(default_factory=list)

    def reset(self, *args, **kwargs):
        self.calls.append("reset")

    def cleanup(self):
        self.calls.append("cleanup")


@dataclass
class _Candidate:
    """A container the fake scan reports as running."""

    name: str
    tcp_port: int
    udp_port: int
    is_active: bool = True


CANDIDATES = [
    _Candidate("factorio_1", 27100, 35197),
    _Candidate("factorio_2", 27101, 35198),
]

PINS = ("FLE_SERVER", "FLE_RCON_PORT", "FLE_RESET_ON_CONNECT", "FLE_RESET_ON_SHUTDOWN")


class _FakeCluster:
    """Fake docker listing plus the calls the session makes against it."""

    def __init__(self):
        self.probes: List[tuple] = []
        self.connections: List[int] = []

    def build_instance(self, address, tcp_port):
        self.probes.append((address, tcp_port))

        class _Probe:
            def cleanup(self_inner):
                pass

        return _Probe()

    async def connect_to_server(self, state, instance_id):
        self.connections.append(instance_id)
        state.active_server = _RecordingInstance(name=f"server-{instance_id}")
        return True


@pytest.fixture(autouse=True)
def clear_pins(monkeypatch):
    for name in PINS:
        monkeypatch.delenv(name, raising=False)
    yield


def _install_fake_cluster(monkeypatch, candidates=CANDIDATES, build=None):
    """Point the session module at a fake multi-container scan."""
    cluster = _FakeCluster()

    monkeypatch.setattr(
        state_module,
        "list_factorio_containers",
        lambda: [
            FactorioContainer(
                name=c.name,
                container_id=f"id-{c.name}",
                address="127.0.0.1",
                udp_port=c.udp_port,
                tcp_port=c.tcp_port,
            )
            for c in candidates
        ],
    )
    monkeypatch.setattr(
        FactorioMCPState,
        "_build_instance",
        build
        or (lambda self, address, tcp_port: cluster.build_instance(address, tcp_port)),
    )
    monkeypatch.setattr(
        FactorioMCPState,
        "connect_to_server",
        lambda self, instance_id: cluster.connect_to_server(self, instance_id),
    )

    fake_state = FactorioMCPState()
    monkeypatch.setattr(session_module, "state", fake_state)
    return cluster, fake_state


# --------------------------------------------------------------------------
# Defect 1 — closing a session must not reset the world
# --------------------------------------------------------------------------


def test_shutdown_closes_rcon_without_resetting_the_game():
    server = _RecordingInstance()
    session_module.state.active_server = server
    session_module.state.vcs_repos = {0: object()}

    asyncio.run(session_module.shutdown_session())

    assert server.calls == ["cleanup"], (
        "shutting a session down must close RCON and nothing else; "
        f"calls were {server.calls}"
    )
    assert session_module.state.active_server is None
    assert session_module.state.vcs_repos == {}


def test_shutdown_resets_when_explicitly_opted_in(monkeypatch):
    monkeypatch.setenv("FLE_RESET_ON_SHUTDOWN", "1")
    server = _RecordingInstance()
    session_module.state.active_server = server
    session_module.state.vcs_repos = {}

    asyncio.run(session_module.shutdown_session())

    assert server.calls == ["reset", "cleanup"]


def test_a_failing_close_still_drops_session_state():
    class Exploding(_RecordingInstance):
        def cleanup(self):
            raise RuntimeError("socket already gone")

    session_module.state.active_server = Exploding()
    session_module.state.vcs_repos = {}

    asyncio.run(session_module.shutdown_session())

    assert session_module.state.active_server is None


def test_provisioning_defaults_to_off_and_rejects_garbage(monkeypatch):
    assert config.reset_on_connect() is False
    assert config.reset_on_shutdown() is False

    monkeypatch.setenv("FLE_RESET_ON_SHUTDOWN", "sometimes")
    with pytest.raises(ValueError, match="FLE_RESET_ON_SHUTDOWN"):
        config.reset_on_shutdown()

    monkeypatch.setenv("FLE_RCON_PORT", "not-a-port")
    with pytest.raises(ValueError, match="FLE_RCON_PORT"):
        config.server_pin_rcon_port()

    monkeypatch.delenv("FLE_RCON_PORT")
    assert config.server_pin_name() is None


def test_importing_the_session_module_does_not_build_a_live_env():
    """The import used to construct a gym env, which attached and reset a world."""
    assert session_module.state._gym_env is None


# --------------------------------------------------------------------------
# Defect 2 — the session must attach to the pinned world
# --------------------------------------------------------------------------


def test_pin_by_container_name_selects_that_server(monkeypatch):
    monkeypatch.setenv("FLE_SERVER", "factorio_2")
    cluster, state = _install_fake_cluster(monkeypatch)

    message = asyncio.run(session_module.initialize_session())

    assert cluster.connections == [1], f"connected to {cluster.connections}"
    # Only the pinned container is probed; a session does not touch the rest.
    assert cluster.probes == [("127.0.0.1", 27101)]
    assert list(state.available_servers) == [1]
    assert "factorio_2" in message and "27101" in message


def test_pin_by_rcon_port_selects_that_server(monkeypatch):
    monkeypatch.setenv("FLE_RCON_PORT", "27100")
    cluster, state = _install_fake_cluster(monkeypatch)

    asyncio.run(session_module.initialize_session())

    assert cluster.connections == [0]
    assert state.available_servers[0].name == "factorio_1"


def test_unknown_pin_fails_loudly_with_candidates(monkeypatch):
    monkeypatch.setenv("FLE_SERVER", "factorio_9")
    cluster, state = _install_fake_cluster(monkeypatch)

    message = asyncio.run(session_module.initialize_session())

    assert cluster.connections == [], "an unknown pin must not fall back"
    assert "Initialization failed" in message
    assert "factorio_1" in message and "factorio_2" in message


def test_pin_naming_and_port_disagreeing_is_an_error(monkeypatch):
    monkeypatch.setenv("FLE_SERVER", "factorio_1")
    monkeypatch.setenv("FLE_RCON_PORT", "27101")
    cluster, state = _install_fake_cluster(monkeypatch)

    message = asyncio.run(session_module.initialize_session())

    assert cluster.connections == []
    assert "Initialization failed" in message


def test_pinned_server_that_is_down_fails_with_candidates(monkeypatch):
    monkeypatch.setenv("FLE_SERVER", "factorio_2")

    def exploding_build(self, address, tcp_port):
        raise ConnectionError("RCON refused")

    cluster, state = _install_fake_cluster(monkeypatch, build=exploding_build)

    message = asyncio.run(session_module.initialize_session())

    assert cluster.connections == []
    assert "is not reachable" in message and "factorio_2" in message


def test_unpinned_selection_logs_every_candidate(monkeypatch, capsys):
    cluster, state = _install_fake_cluster(monkeypatch)

    message = asyncio.run(session_module.initialize_session())
    log = capsys.readouterr().err

    assert cluster.connections == [0], "historical behaviour: first active server"
    assert "Server candidates:" in log
    for candidate in CANDIDATES:
        assert candidate.name in log
        assert f"rcon_port={candidate.tcp_port}" in log
        assert f"game_port={candidate.udp_port}" in log
    assert "Connecting to server 0: factorio_1" in log
    assert "first active server" in log
    assert "Connected to Factorio server factorio_1" in message


# --------------------------------------------------------------------------
# Selection and discovery helpers
# --------------------------------------------------------------------------


def test_select_container_prefers_no_pin_first_candidate():
    containers = [
        FactorioContainer("factorio_1", "id1", "127.0.0.1", 35197, 27100),
        FactorioContainer("factorio_2", "id2", "127.0.0.1", 35198, 27101),
    ]
    assert select_container(containers).name == "factorio_1"
    assert select_container(containers, name="factorio_2").tcp_port == 27101
    assert select_container(containers, rcon_port=27100).name == "factorio_1"


def test_select_container_reports_candidates_on_a_bad_pin():
    containers = [
        FactorioContainer("factorio_1", "id1", "127.0.0.1", 35197, 27100),
    ]
    with pytest.raises(ContainerSelectionError) as excinfo:
        select_container(containers, rcon_port=27999)
    assert "factorio_1" in str(excinfo.value)
    assert "27100" in str(excinfo.value)


def test_published_port_prefers_the_rcon_binding():
    ports = {
        "27015/tcp": [{"HostPort": "27150"}],
        "34197/udp": [{"HostPort": "35250"}],
    }
    assert _published_port(ports, "tcp", 27015) == 27150
    assert _published_port(ports, "udp", 34197) == 35250
    # A second TCP binding must not be able to hijack the RCON port.
    ports["9999/tcp"] = [{"HostPort": "1"}]
    assert _published_port(ports, "tcp", 27150) == 27150
    assert _published_port({}, "tcp", 27015) is None
