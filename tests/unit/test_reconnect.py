"""Unit tests for the MCP ``reconnect`` tool.

Importing ``fle.env.protocols._mcp`` builds a process-wide ``FactorioMCPState``,
which creates a gym environment and connects to a running Factorio container.
The default suite has to stay offline, so these tests import the real
``fle.env.protocols._mcp.tools`` module with ``fle.env.protocols._mcp.init``
replaced by a stub and drive it with fake RCON clients instead of a container.
"""

import asyncio
import importlib
import sys
import types
from typing import Dict, List, Optional

import pytest

MCP_PACKAGE = "fle.env.protocols._mcp"
PARENT_PACKAGE = "fle.env.protocols"
_MISSING = object()
ADDRESS = "127.0.0.1"
PORT_A = 27100  # host port the session is attached to
PORT_B = 27200  # host port the container publishes after it was recreated
PORT_C = 27300  # a second container of the same cluster


async def _stub_initialize_session(ctx=None) -> str:
    return "stub session initialisation"


async def _stub_shutdown_session() -> None:
    return None


class FakeRconClient:
    """Records commands and whether the socket was released."""

    def __init__(self, tcp_port: int) -> None:
        self.tcp_port = tcp_port
        self.commands: List[str] = []
        self.closed = False

    def send_command(self, command: str) -> str:
        if self.closed:
            raise ConnectionError(f"RCON client for port {self.tcp_port} is closed")
        self.commands.append(command)
        return "1"

    def close(self) -> None:
        self.closed = True


class FakeInstance:
    """Stand-in for ``FactorioInstance``, covering what the tools touch."""

    def __init__(
        self,
        address: str,
        tcp_port: int,
        eval_error: Optional[BaseException] = None,
    ) -> None:
        self.address = address
        self.tcp_port = tcp_port
        self.rcon_client = FakeRconClient(tcp_port)
        self.eval_errors: List[BaseException] = [eval_error] if eval_error else []
        self.eval_calls: List[str] = []

    def is_rcon_connected(self) -> bool:
        return not self.rcon_client.closed

    def eval(self, code: str, timeout: int = 60):
        self.eval_calls.append(code)
        if self.eval_errors:
            error = self.eval_errors.pop(0)
            if isinstance(error, (ConnectionError, OSError)):
                # factorio_rcon closes the socket when a command fails
                self.rcon_client.close()
            raise error
        return 0, 0, f"ran {code}"


class FakeRepository:
    """Stand-in for ``FactorioMCPRepository`` that never touches the filesystem."""

    def __init__(self, instance) -> None:
        self.instance = instance
        self.undo_stack: List[str] = []

    def commit(self, game_state, message: str, code: str = "") -> str:
        self.undo_stack.append(f"commit-{len(self.undo_stack)}")
        return self.undo_stack[-1]


class FakeGameState:
    @staticmethod
    def from_instance(instance):
        return object()


class FakeCluster:
    """Stands in for Docker: reports the containers running at this moment."""

    def __init__(self, tcp_ports: List[int]) -> None:
        self.tcp_ports = list(tcp_ports)
        self.discoveries = 0
        self.opened: List[FakeInstance] = []
        self.initial_eval_error: Optional[BaseException] = None

    def get_local_container_ips(self):
        self.discoveries += 1
        return (
            [ADDRESS] * len(self.tcp_ports),
            [port - 1000 for port in self.tcp_ports],
            list(self.tcp_ports),
        )

    def create_instance(self, instance_id, address=None, tcp_port=None) -> FakeInstance:
        # Mirrors the real factory: without an explicit endpoint it resolves one
        # through discovery.
        if address is None or tcp_port is None:
            address, tcp_port = ADDRESS, self.tcp_ports[instance_id]

        error, self.initial_eval_error = self.initial_eval_error, None
        instance = FakeInstance(address, tcp_port, error)
        self.opened.append(instance)
        return instance


class Harness:
    """Real ``tools`` module wired to a fake container cluster."""

    def __init__(
        self, tools, state, models, cluster: FakeCluster, real_factory, monkeypatch
    ) -> None:
        self.tools = tools
        self.state = state
        self.models = models
        self.cluster = cluster
        # The shipped method, so the endpoint parameters are covered as well.
        self.real_factory = real_factory

        monkeypatch.setattr(
            tools,
            "get_local_container_ips",
            cluster.get_local_container_ips,
            raising=False,
        )
        monkeypatch.setattr(tools, "GameState", FakeGameState)
        state.create_factorio_instance = cluster.create_instance

    def reconnect(self) -> str:
        return asyncio.run(self.tools.reconnect())

    def execute(self, code: str) -> str:
        return asyncio.run(self.tools.execute(code))


def _mcp_module_names():
    return {
        name
        for name in sys.modules
        if name == MCP_PACKAGE or name.startswith(MCP_PACKAGE + ".")
    }


def _parent_mcp_attribute():
    """The ``_mcp`` attribute the import machinery sets on ``fle.env.protocols``."""
    parent = sys.modules.get(PARENT_PACKAGE)
    return _MISSING if parent is None else getattr(parent, "_mcp", _MISSING)


@pytest.fixture(scope="module", autouse=True)
def mcp_import_state_is_restored():
    """The stubbed import must leave the MCP import state exactly as it found it."""
    names_before = _mcp_module_names()
    attribute_before = _parent_mcp_attribute()
    yield
    assert _mcp_module_names() == names_before
    assert _parent_mcp_attribute() is attribute_before, (
        "fle.env.protocols._mcp must point at the original package module again"
    )


@pytest.fixture()
def harness(monkeypatch):
    """Build a ``Harness`` against the real tools module, with no Docker and no RCON."""
    saved: Dict[str, types.ModuleType] = {}
    saved_attribute = None

    def _purge() -> None:
        for name in [
            name
            for name in sys.modules
            if name == MCP_PACKAGE or name.startswith(MCP_PACKAGE + ".")
        ]:
            del sys.modules[name]

    def _build(cluster: FakeCluster) -> Harness:
        nonlocal saved_attribute
        if not saved:
            saved_attribute = _parent_mcp_attribute()
            saved.update(
                {
                    name: module
                    for name, module in sys.modules.items()
                    if name == MCP_PACKAGE or name.startswith(MCP_PACKAGE + ".")
                }
            )
        _purge()

        stub_init = types.ModuleType(MCP_PACKAGE + ".init")
        stub_init.state = None
        stub_init.log_info = lambda message: None
        stub_init.initialize_session = _stub_initialize_session
        stub_init.shutdown_session = _stub_shutdown_session
        # Deliberately not monkeypatch.setitem: monkeypatch records the key as
        # absent (it was purged above) and would delete the restored original
        # package module when it undoes itself. sys.modules is restored by hand
        # in this fixture's teardown instead.
        sys.modules[MCP_PACKAGE + ".init"] = stub_init

        state_module = importlib.import_module(MCP_PACKAGE + ".state")
        monkeypatch.setattr(state_module, "FactorioMCPRepository", FakeRepository)

        # The real state class, without its __init__: that would build a gym
        # environment and connect to a live container.
        state = state_module.FactorioMCPState.__new__(state_module.FactorioMCPState)
        state.available_servers = {}
        state.active_server = None
        state.vcs_repos = {}

        stub_init.state = state
        tools = importlib.import_module(MCP_PACKAGE + ".tools")
        monkeypatch.setattr(tools, "log_info", lambda message: None, raising=False)

        models = importlib.import_module(MCP_PACKAGE + ".models")
        real_factory = types.MethodType(
            state_module.FactorioMCPState.create_factorio_instance, state
        )
        return Harness(tools, state, models, cluster, real_factory, monkeypatch)

    yield _build

    _purge()
    sys.modules.update(saved)
    # Re-importing the package also rebinds `fle.env.protocols._mcp`, so the
    # parent attribute has to be restored as well.
    parent = sys.modules.get(PARENT_PACKAGE)
    if parent is not None:
        if saved_attribute is _MISSING:
            if getattr(parent, "_mcp", _MISSING) is not _MISSING:
                del parent._mcp
        elif saved_attribute is not None:
            parent._mcp = saved_attribute


def test_reconnect_follows_a_recreated_container_to_its_new_port(harness):
    catalogue = harness(FakeCluster([PORT_A]))

    # A long-lived session holding a connection to the old endpoint, exactly as
    # left behind when the container is recreated on different host ports.
    cluster = catalogue.cluster
    stale_instance = cluster.create_instance(0, ADDRESS, PORT_A)
    catalogue.state.active_server = stale_instance
    catalogue.state.available_servers = {
        0: catalogue.models.FactorioServer(
            address=ADDRESS,
            tcp_port=PORT_A,
            instance_id=0,
            name="Factorio Server 1",
            connected=True,
            is_active=True,
        )
    }
    cluster.tcp_ports = [PORT_B]

    report = catalogue.reconnect()

    active = catalogue.state.active_server
    assert active is not stale_instance, "reconnect must open a fresh connection"
    assert (active.address, active.tcp_port) == (ADDRESS, PORT_B)
    assert f"({ADDRESS}:{PORT_B})" in report
    assert f":{PORT_A}" not in report, "the dead endpoint must not be reported"
    assert "Commit history:" in report

    assert stale_instance.rcon_client.closed, "the stale RCON socket must be released"
    assert not active.rcon_client.closed, "the new RCON socket must stay open"

    assert [
        server.tcp_port for server in catalogue.state.available_servers.values()
    ] == [PORT_B]
    assert cluster.discoveries == 1, "reconnect must run a fresh container discovery"


def test_reconnect_reports_a_missing_container_instead_of_pretending(harness):
    catalogue = harness(FakeCluster([PORT_A]))
    catalogue.reconnect()
    catalogue.cluster.tcp_ports = []
    catalogue.cluster.opened.clear()

    message = catalogue.reconnect()

    assert "no running factorio container" in message.lower()
    assert "cluster start" in message, "the error must say what to do next"
    assert "Connected to Factorio server" not in message
    assert catalogue.cluster.opened == [], "nothing may be connected"


def test_execute_retries_once_when_the_same_endpoint_is_reachable_again(harness):
    catalogue = harness(FakeCluster([PORT_A]))
    catalogue.cluster.initial_eval_error = ConnectionError("stale RCON socket")
    catalogue.reconnect()
    dead_instance = catalogue.state.active_server

    # The same endpoint answers again (e.g. RCON restarted), so the same world
    # is still reachable and the code may run once more.
    output = catalogue.execute("1 + 1")

    active = catalogue.state.active_server
    assert active is not dead_instance
    assert active.tcp_port == PORT_A
    assert len(dead_instance.eval_calls) == 1
    assert active.eval_calls == ["1 + 1"], "the code must be retried exactly once"
    assert output.startswith("[commit ")


def test_execute_does_not_replay_code_in_a_recreated_container(harness):
    catalogue = harness(FakeCluster([PORT_A]))
    catalogue.cluster.initial_eval_error = ConnectionError("stale RCON socket")
    catalogue.reconnect()
    dead_instance = catalogue.state.active_server

    # The container is recreated on another host port: another endpoint, and so
    # another world that the caller did not choose.
    catalogue.cluster.tcp_ports = [PORT_B]

    with pytest.raises(Exception, match="not retried in that world"):
        catalogue.execute("1 + 1")

    active = catalogue.state.active_server
    assert active is not dead_instance
    assert active.tcp_port == PORT_B, "the session must still reconnect"
    assert active.eval_calls == [], "the code must not be replayed in the new world"
    assert len(dead_instance.eval_calls) == 1


def test_execute_does_not_retry_code_errors(harness):
    catalogue = harness(FakeCluster([PORT_A]))
    catalogue.reconnect()
    discoveries = catalogue.cluster.discoveries

    catalogue.state.active_server.eval_errors = [
        ValueError("Lua error in submitted code")
    ]

    with pytest.raises(ValueError, match="Lua error in submitted code"):
        catalogue.execute("1 + 1")

    assert catalogue.cluster.discoveries == discoveries, (
        "a code error must not reconnect"
    )


def test_timeouts_are_not_treated_as_a_lost_connection(harness):
    catalogue = harness(FakeCluster([PORT_A]))

    # A code timeout must not trigger a second, equally slow execution attempt.
    assert catalogue.tools.is_lost_rcon_connection(object(), TimeoutError()) is False
    assert catalogue.tools.is_lost_rcon_connection(object(), ConnectionError()) is True


def test_create_factorio_instance_takes_an_explicit_or_discovered_endpoint(
    harness, monkeypatch
):
    catalogue = harness(FakeCluster([PORT_A]))
    state_module = sys.modules[MCP_PACKAGE + ".state"]

    built = []

    class FakeFactorioInstance:
        def __init__(self, address, tcp_port, **kwargs):
            built.append((address, tcp_port, kwargs))
            self.address = address
            self.tcp_port = tcp_port
            self.rcon_client = FakeRconClient(tcp_port)

        def set_speed(self, speed):
            self.speed = speed

    discoveries = []

    def discover():
        discoveries.append(True)
        return [ADDRESS], [PORT_A - 1000], [PORT_A]

    monkeypatch.setattr(state_module, "FactorioInstance", FakeFactorioInstance)
    monkeypatch.setattr(state_module, "get_local_container_ips", discover)

    explicit = catalogue.real_factory(0, address=ADDRESS, tcp_port=PORT_B)
    assert (explicit.address, explicit.tcp_port) == (ADDRESS, PORT_B)
    assert discoveries == [], "an explicit endpoint must skip discovery"

    discovered = catalogue.real_factory(0)
    assert (discovered.address, discovered.tcp_port) == (ADDRESS, PORT_A)
    assert discoveries == [True], "the one-argument form must still discover"

    with pytest.raises(IndexError):
        catalogue.real_factory(3)


def test_reconnect_keeps_the_session_on_its_endpoint_when_ids_shift(harness):
    catalogue = harness(FakeCluster([PORT_A, PORT_B, PORT_C]))

    # Session attached to the second of three containers (instance id 1).
    stale_instance = catalogue.cluster.create_instance(1, ADDRESS, PORT_B)
    catalogue.state.active_server = stale_instance
    catalogue.state.available_servers = {
        server_id: catalogue.models.FactorioServer(
            address=ADDRESS,
            tcp_port=port,
            instance_id=server_id,
            name=f"Factorio Server {server_id + 1}",
            connected=True,
            is_active=True,
        )
        for server_id, port in enumerate([PORT_A, PORT_B, PORT_C])
    }

    # The first container disappears, so discovery re-enumerates the survivors:
    # instance id 1 is now the third container.
    catalogue.cluster.tcp_ports = [PORT_B, PORT_C]

    report = catalogue.reconnect()

    active = catalogue.state.active_server
    assert (active.address, active.tcp_port) == (ADDRESS, PORT_B), (
        "the session must stay on the endpoint it was attached to"
    )
    assert [
        server.tcp_port for server in catalogue.state.available_servers.values()
    ] == [
        PORT_B,
        PORT_C,
    ]
    assert f"({ADDRESS}:{PORT_B})" in report
    assert f":{PORT_C}" not in report
