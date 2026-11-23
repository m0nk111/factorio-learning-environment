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
import json
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import pytest

# The package re-exports an attribute named `state` (the state instance), so
# `import ..._mcp.state as state_module` would bind that instance instead of the
# module; go through the import machinery to get the module itself.
state_module = importlib.import_module("fle.env.protocols._mcp.state")
registry_module = importlib.import_module("fle.env.gym_env.registry")

from fle.commons.cluster_ips import (  # noqa: E402
    ContainerSelectionError,
    FactorioContainer,
    _published_port,
    list_factorio_containers,
    select_container,
)
from fle.env.protocols._mcp import config  # noqa: E402
from fle.env.protocols._mcp import config as config_module  # noqa: E402
from fle.env.protocols._mcp import init as session_module  # noqa: E402
from fle.env.protocols._mcp.state import FactorioMCPState  # noqa: E402
from fle.env.protocols._mcp import version_control as version_control_module  # noqa: E402

# Captured at import time: the autouse guard below replaces the class attribute,
# so a test that needs the real builder has to keep a reference to it.
REAL_MAKE_GYM_ENV = FactorioMCPState._make_gym_env


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

PINS = (
    "FLE_SERVER",
    "FLE_RCON_PORT",
    "FLE_RESET_ON_CONNECT",
    "FLE_RESET_ON_SHUTDOWN",
    # The session also honours these; a value inherited from the shell would
    # make these tests depend on the invoking environment.
    "FACTORIO_SERVER_ADDRESS",
    "FACTORIO_SERVER_PORT",
)


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
def no_live_gym_env(monkeypatch):
    """Fail loudly instead of attaching to a real container from a unit test."""

    def explode(self, env_id):
        raise AssertionError(
            f"a unit test must not build a live gym env (env_id={env_id!r})"
        )

    monkeypatch.setattr(FactorioMCPState, "_make_gym_env", explode)
    yield


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
    """Importing the MCP module must not attach to (or reset) any world.

    Checked in a fresh interpreter: in-process assertions are order-dependent,
    because other tests in this module legitimately clear the shared state.
    """
    repo_root = Path(__file__).resolve().parents[2]
    probe = """
import gymnasium

def explode(*args, **kwargs):
    raise AssertionError("import built a live gym environment")

gymnasium.make = explode

import fle.env.protocols._mcp  # noqa: F401

print("IMPORT_OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        "importing fle.env.protocols._mcp failed or built a live environment:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "IMPORT_OK" in result.stdout


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
    containers = _fake_containers()
    assert select_container(containers).name == "factorio_1"
    assert select_container(containers, name="factorio_2").tcp_port == 27101
    assert select_container(containers, rcon_port=27100).name == "factorio_1"


def test_select_container_reports_candidates_on_a_bad_pin():
    containers = [
        FactorioContainer(
            name="factorio_1",
            container_id="id1",
            address="127.0.0.1",
            tcp_port=27100,
            udp_port=35197,
        )
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

    # An unrelated TCP binding listed first must not hijack the RCON port, and
    # the preferred port is the container-internal one, not the host port.
    decoy_first = {
        "9999/tcp": [{"HostPort": "1"}],
        "27015/tcp": [{"HostPort": "27150"}],
    }
    assert list(decoy_first)[0] == "9999/tcp"
    assert _published_port(decoy_first, "tcp", 27015) == 27150
    assert _published_port(decoy_first, "tcp", 27016) == 1  # fallback, first binding
    assert _published_port({}, "tcp", 27015) is None


# --------------------------------------------------------------------------
# Review findings: cleanup survives a bad config, and the attach path itself is
# covered (not just the selection around it)
# --------------------------------------------------------------------------


def test_shutdown_cleans_up_when_the_reset_flag_is_invalid(monkeypatch):
    monkeypatch.setenv("FLE_RESET_ON_SHUTDOWN", "sometimes")
    server = _RecordingInstance()
    session_module.state.active_server = server
    session_module.state.vcs_repos = {0: object()}

    asyncio.run(session_module.shutdown_session())

    assert server.calls == ["cleanup"], "the connection must still be closed"
    assert session_module.state.active_server is None
    assert session_module.state.vcs_repos == {}


def test_shutdown_closes_rcon_when_the_reset_raises(monkeypatch):
    monkeypatch.setenv("FLE_RESET_ON_SHUTDOWN", "1")

    class ResetFails(_RecordingInstance):
        def reset(self, *args, **kwargs):
            self.calls.append("reset")
            raise ConnectionError("RCON died mid-reset")

    server = ResetFails()
    session_module.state.active_server = server
    session_module.state.vcs_repos = {}

    asyncio.run(session_module.shutdown_session())

    assert server.calls == ["reset", "cleanup"]


def test_attach_uses_config_for_reset_on_init(monkeypatch):
    """The real attach helper must not provision the world by default."""
    captured = {}

    class _FakeInstance:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.rcon_client = types.SimpleNamespace(
                send_command=lambda command: "1"  # agents already exist
            )
            self.first_namespace = types.SimpleNamespace(
                _create_agent_characters=lambda n: captured.setdefault("chars", n)
            )

        def set_speed(self, speed):
            captured["speed"] = speed

    monkeypatch.setattr(state_module, "FactorioInstance", _FakeInstance)
    state = FactorioMCPState()

    state._build_instance("127.0.0.1", 27100)
    assert captured["reset_on_init"] is False
    assert captured["tcp_port"] == 27100

    monkeypatch.setenv("FLE_RESET_ON_CONNECT", "1")
    state._build_instance("127.0.0.1", 27100)
    assert captured["reset_on_init"] is True


def test_initialise_skips_every_provisioning_step(monkeypatch):
    """FactorioInstance.initialise(reset_on_init=False) must only load Lua."""
    from fle.env.instance import FactorioInstance

    calls = []

    class _Namespace:
        def _create_agent_characters(self, num):
            calls.append("characters")

        def _reset(self, *args, **kwargs):
            calls.append("reset")

        def _clear_collision_boxes(self):
            calls.append("collision_boxes")

    class _Rcon:
        def send_command(self, command):
            if "remove_enemies" in command:
                calls.append("remove_enemies")
            return ""

    class _Lua:
        def load_init_into_game(self, name):
            calls.append(f"lua:{name}")

    def build(reset_on_init):
        instance = object.__new__(FactorioInstance)
        instance.reset_on_init = reset_on_init
        instance.peaceful = True
        instance.num_agents = 1
        instance.initial_inventory = {}
        instance.namespaces = [_Namespace()]
        instance.rcon_client = _Rcon()
        instance.lua_script_manager = _Lua()
        instance._generate_chunks = lambda **kwargs: calls.append("chunks")
        return instance

    calls.clear()
    build(reset_on_init=False).initialise()
    assert "reset" not in calls
    assert "chunks" not in calls
    assert "remove_enemies" not in calls
    assert "characters" not in calls
    assert any(call.startswith("lua:") for call in calls), "the Lua layer still loads"

    calls.clear()
    build(reset_on_init=True).initialise()
    assert {"reset", "chunks", "remove_enemies", "characters"} <= set(calls)


def _gym_spec():
    from fle.env.gym_env import registry as registry_module

    return registry_module, registry_module.GymEnvironmentSpec(
        task_key="open_play",
        task_config_path="open_play",
        description="unit-test spec",
        num_agents=1,
    )


def test_make_factorio_env_skips_task_setup_unless_provisioning(monkeypatch):
    registry_module, spec = _gym_spec()
    setup_calls = []
    built = {}

    class _Task:
        def setup(self, instance):
            setup_calls.append(instance)

    class _Env:
        def __init__(self, instance, task, enable_vision):
            built["wrapped"] = True

    class _Instance:
        def __init__(self, **kwargs):
            built["kwargs"] = kwargs

        def set_speed_and_unpause(self, speed):
            built["speed"] = speed

    monkeypatch.setattr(
        registry_module,
        "TaskFactory",
        types.SimpleNamespace(create_task=lambda path: _Task()),
    )
    monkeypatch.setattr(registry_module, "FactorioInstance", _Instance)
    monkeypatch.setattr(registry_module, "FactorioGymEnv", _Env)
    monkeypatch.setenv("FACTORIO_SERVER_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("FACTORIO_SERVER_PORT", "27100")

    registry_module.make_factorio_env(spec, 0, reset_on_init=False)
    assert setup_calls == [], "task.setup resets the world; it must be skipped"
    assert built["kwargs"]["reset_on_init"] is False

    registry_module.make_factorio_env(spec, 0)
    assert len(setup_calls) == 1
    assert built["kwargs"]["reset_on_init"] is True


def _fake_containers():
    return [
        FactorioContainer(
            name="factorio_1",
            container_id="id1",
            address="127.0.0.1",
            tcp_port=27100,
            udp_port=35197,
        ),
        FactorioContainer(
            name="factorio_2",
            container_id="id2",
            address="127.0.0.1",
            tcp_port=27101,
            udp_port=35198,
        ),
    ]


def test_pin_wins_over_an_explicit_target_and_conflicts_loudly(monkeypatch):
    registry_module, spec = _gym_spec()
    built = {}

    class _Instance:
        def __init__(self, **kwargs):
            built["kwargs"] = kwargs

        def set_speed_and_unpause(self, speed):
            pass

    class _Task:
        def setup(self, instance):
            pass

    monkeypatch.setattr(registry_module, "list_factorio_containers", _fake_containers)
    monkeypatch.setattr(registry_module, "FactorioInstance", _Instance)
    monkeypatch.setattr(registry_module, "FactorioGymEnv", lambda **kwargs: object())
    monkeypatch.setattr(
        registry_module,
        "TaskFactory",
        types.SimpleNamespace(create_task=lambda path: _Task()),
    )

    # A pin resolving to factorio_2 must be used, not the explicit target.
    monkeypatch.setenv("FACTORIO_SERVER_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("FACTORIO_SERVER_PORT", "27101")
    registry_module.make_factorio_env(spec, 0, container_name="factorio_2")
    assert built["kwargs"]["tcp_port"] == 27101

    # ... and a pin that contradicts the explicit target must be rejected.
    monkeypatch.setenv("FACTORIO_SERVER_PORT", "27100")
    with pytest.raises(RuntimeError, match="Conflicting Factorio target"):
        registry_module.make_factorio_env(spec, 0, container_name="factorio_2")


def test_discovery_diagnostics_stay_off_stdout(monkeypatch, capsys):
    """stdout carries the MCP protocol stream; diagnostics must go to stderr."""
    from fle.commons import cluster_ips

    monkeypatch.setattr(
        cluster_ips.subprocess,
        "run",
        lambda *args, **kwargs: types.SimpleNamespace(
            stdout="", stderr="", returncode=0
        ),
    )

    assert cluster_ips.list_factorio_containers() == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No running Factorio containers found" in captured.err


def test_unreachable_candidate_writes_nothing_to_stdout(monkeypatch, capsys):
    """A failing probe is a diagnostic; stdout carries the MCP protocol stream."""
    cluster, state = _install_fake_cluster(
        monkeypatch,
        build=lambda self, address, tcp_port: (_ for _ in ()).throw(
            ConnectionError("RCON refused")
        ),
    )

    servers = asyncio.run(state.scan_for_servers())

    captured = capsys.readouterr()
    assert captured.out == "", f"probe wrote to stdout: {captured.out!r}"
    assert "RCON refused" in captured.err
    assert len(servers) == len(CANDIDATES)
    assert all(not server.is_active for server in servers)
    assert all(server.system_response for server in servers)


def test_direct_factory_rejects_an_invalid_pin(monkeypatch, tmp_path):
    """rcon_port=0 is an invalid pin, not an absent one."""
    registry_module, spec = _gym_spec()

    class _Instance:
        def __init__(self, **kwargs):
            raise AssertionError("an invalid pin must not attach to anything")

    monkeypatch.setattr(registry_module, "FactorioInstance", _Instance)
    monkeypatch.setattr(
        registry_module, "list_factorio_containers", lambda: _fake_containers()
    )
    monkeypatch.setenv("FACTORIO_SERVER_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("FACTORIO_SERVER_PORT", "27100")

    with pytest.raises(ValueError, match="rcon_port"):
        registry_module.make_factorio_env(spec, 0, rcon_port=0)
    with pytest.raises(ValueError, match="rcon_port"):
        registry_module.make_factorio_env(spec, 0, rcon_port=70000)


def test_rcon_only_container_is_still_discoverable(monkeypatch):
    """A headless container publishing only RCON must not disappear."""
    from fle.commons import cluster_ips

    def fake_run(cmd, capture_output=True, text=True):
        if "ps" in cmd:
            return types.SimpleNamespace(stdout='"abc123"\n', stderr="", returncode=0)
        return types.SimpleNamespace(
            stdout=json.dumps(
                [
                    {
                        "Name": "/factorio_headless",
                        "NetworkSettings": {
                            "Ports": {"27015/tcp": [{"HostPort": "27100"}]}
                        },
                    }
                ]
            ),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(cluster_ips.subprocess, "run", fake_run)

    containers = cluster_ips.list_factorio_containers()
    assert len(containers) == 1
    assert containers[0].name == "factorio_headless"
    assert containers[0].tcp_port == 27100
    assert containers[0].udp_port is None

    # The historical contract keeps a placeholder in the game-port list.
    ips, udp_ports, tcp_ports = cluster_ips.get_local_container_ips()
    assert (ips, udp_ports, tcp_ports) == (["127.0.0.1"], [-1], [27100])


def test_scan_handles_a_container_without_a_game_port(monkeypatch):
    """The scan and its logging must tolerate udp_port=None."""
    from fle.commons.cluster_ips import FactorioContainer

    cluster, state = _install_fake_cluster(monkeypatch)
    monkeypatch.setattr(
        state_module,
        "list_factorio_containers",
        lambda: [
            FactorioContainer(
                name="factorio_headless",
                container_id="id1",
                address="127.0.0.1",
                tcp_port=27100,
            )
        ],
    )

    servers = asyncio.run(state.scan_for_servers())

    assert len(servers) == 1
    assert servers[0].tcp_port == 27100
    assert servers[0].udp_port is None
    assert servers[0].name == "factorio_headless"


def test_observation_env_does_not_outlive_its_target(monkeypatch):
    """A reused state must not keep observing the container of a past session."""
    built = []

    class _Env:
        def __init__(self, label):
            self.label = label
            self.closed = False

        def close(self):
            self.closed = True

    state = FactorioMCPState()
    state._make_gym_env = lambda env_id: built.append(_Env(env_id)) or built[-1]
    state._reset_gym_env = lambda: None

    monkeypatch.setenv("FLE_SERVER", "factorio_1")
    first = state.gym_env
    assert state.gym_env is first, "the same target reuses the cached env"

    monkeypatch.setenv("FLE_SERVER", "factorio_2")
    second = state.gym_env
    assert second is not first
    assert first.closed, "the stale environment must be closed"
    assert state._gym_env_target == ("factorio_2", None)

    state.close_gym_env()
    assert state._gym_env is None
    state.close_gym_env()  # idempotent


def test_shutdown_closes_the_observation_environment():
    closed = []

    class _Env:
        def close(self):
            closed.append(True)

    session_module.state._gym_env = _Env()
    session_module.state.active_server = None

    asyncio.run(session_module.shutdown_session())

    assert session_module.state._gym_env is None
    # Not merely dropped: close() must actually be called on the cached env.
    assert closed == [True]


def test_lab_mode_builds_the_observation_env_at_connect_time(monkeypatch):
    """With provisioning on, the world is prepared during attach, not later."""
    touched = []

    def prepare(mode_env):
        state = FactorioMCPState()
        state.available_servers[0] = types.SimpleNamespace(
            address="127.0.0.1", tcp_port=27100, is_active=True, instance_id=0
        )
        monkeypatch.setattr(
            state, "create_factorio_instance", lambda instance_id, **kw: object()
        )

        async def refresh(instance_id):
            return True

        monkeypatch.setattr(state, "refresh_game_data", refresh)
        monkeypatch.setattr(state, "load_recipes_from_file", lambda: {"x": 1})
        monkeypatch.setattr(
            FactorioMCPState,
            "gym_env",
            property(lambda self: touched.append("built") or types.SimpleNamespace()),
        )
        monkeypatch.setattr(
            state_module, "FactorioMCPRepository", lambda instance: object()
        )
        return state

    monkeypatch.setenv("FLE_RESET_ON_CONNECT", "1")
    assert asyncio.run(prepare("lab").connect_to_server(0)) is True
    assert touched == ["built"], "lab mode must provision during the attach"

    touched.clear()
    monkeypatch.delenv("FLE_RESET_ON_CONNECT")
    assert asyncio.run(prepare("safe").connect_to_server(0)) is True
    assert touched == [], "world-safe mode must not build it during the attach"


async def _async_noop(*args, **kwargs):
    return True


def test_conflicting_explicit_target_fails_before_any_probe(monkeypatch):
    """A contradictory FACTORIO_SERVER_* must not survive as far as a probe."""
    monkeypatch.setenv("FLE_SERVER", "factorio_2")
    monkeypatch.setenv("FACTORIO_SERVER_PORT", "27100")  # factorio_1
    monkeypatch.setenv("FLE_RESET_ON_CONNECT", "1")
    cluster, state = _install_fake_cluster(monkeypatch)

    message = asyncio.run(session_module.initialize_session())

    assert cluster.probes == [], "nothing may be probed after a target conflict"
    assert cluster.connections == []
    assert "Initialization failed" in message
    assert "Conflicting Factorio target" in message

    # A matching explicit target is not a conflict.
    monkeypatch.setenv("FACTORIO_SERVER_PORT", "27101")
    cluster, state = _install_fake_cluster(monkeypatch)
    message = asyncio.run(session_module.initialize_session())
    assert cluster.connections == [1]
    assert "Connected to Factorio server factorio_2" in message


def test_a_failed_provisioning_is_not_cached(monkeypatch):
    """A reset that raises must leave no ready-looking environment behind."""
    state = FactorioMCPState()
    built = []

    class _Env:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    state._make_gym_env = lambda env_id: built.append(_Env()) or built[-1]

    attempts = []

    def reset_that_fails_once():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("provisioning failed")

    monkeypatch.setattr(state, "_reset_gym_env", reset_that_fails_once)
    monkeypatch.setenv("FLE_RESET_ON_CONNECT", "1")

    with pytest.raises(RuntimeError, match="provisioning failed"):
        state.gym_env
    assert state._gym_env is None, "the failed environment must not stay cached"
    assert built[0].closed

    env = state.gym_env  # retries and succeeds
    assert env is built[-1]
    assert len(built) == 2


def test_a_failed_attach_rolls_back_the_connection(monkeypatch):
    """A failed attach must not leave a live connection or a connected flag.

    Regression for the lab-mode path: building the observation environment can
    raise after the RCON connection is already up, and the caller used to get
    False while ``active_server`` and ``server.connected`` stayed set.
    """

    cleaned = []

    class _FakeInstance:
        def cleanup(self):
            cleaned.append("cleanup")

    server = state_module.FactorioServer(
        address="127.0.0.1", tcp_port=27200, instance_id=1, name="srv", is_active=True
    )
    state = FactorioMCPState()
    state.available_servers = {1: server}

    monkeypatch.setattr(
        state, "create_factorio_instance", lambda instance_id, **kw: _FakeInstance()
    )

    async def failing_provisioning(instance_id):
        raise RuntimeError("observation environment failed")

    monkeypatch.setattr(state, "refresh_game_data", failing_provisioning)

    assert asyncio.run(state.connect_to_server(1)) is False
    assert state.active_server is None
    assert server.connected is False
    assert cleaned == ["cleanup"]


def test_a_failed_attach_keeps_the_previous_attachment(monkeypatch):
    """A failed re-attach must leave an existing attachment untouched."""

    cleaned = []
    previous = _RecordingInstance(name="previous")

    class _FakeInstance:
        def cleanup(self):
            cleaned.append("cleanup")

    server = state_module.FactorioServer(
        address="127.0.0.1", tcp_port=27300, instance_id=2, name="srv2", is_active=True
    )
    state = FactorioMCPState()
    state.available_servers = {2: server}
    state.active_server = previous

    monkeypatch.setattr(
        state, "create_factorio_instance", lambda instance_id, **kw: _FakeInstance()
    )

    async def failing_provisioning(instance_id):
        raise RuntimeError("observation environment failed")

    monkeypatch.setattr(state, "refresh_game_data", failing_provisioning)

    assert asyncio.run(state.connect_to_server(2)) is False
    assert state.active_server is previous
    assert server.connected is False
    assert cleaned == ["cleanup"]


def test_a_missing_recipe_dump_does_not_fail_the_attach(tmp_path, monkeypatch):
    """The recipe dump is generated data; its absence must not break a session.

    It used to raise FileNotFoundError through ``connect_to_server`` (after the
    connection was already attached, where the leaked state hid the failure), and
    its fallback pointed at a macOS path belonging to the upstream author.
    """

    monkeypatch.setattr(state_module, "RECIPES_PATH", tmp_path / "recipes.jsonl")
    state = FactorioMCPState()

    assert state.load_recipes_from_file() == {}
    assert state.recipes == {}


def test_the_attach_survives_a_missing_recipe_dump(tmp_path, monkeypatch):
    """An otherwise healthy attach must succeed without a recipe dump."""

    instance = _RecordingInstance(name="srv")

    server = state_module.FactorioServer(
        address="127.0.0.1", tcp_port=27200, instance_id=1, name="srv", is_active=True
    )
    state = FactorioMCPState()
    state.available_servers = {1: server}

    monkeypatch.setattr(state_module, "RECIPES_PATH", tmp_path / "recipes.jsonl")
    monkeypatch.setattr(
        state, "create_factorio_instance", lambda instance_id, **kw: instance
    )

    async def refreshes(instance_id):
        return None

    monkeypatch.setattr(state, "refresh_game_data", refreshes)
    monkeypatch.setattr(
        state_module, "FactorioMCPRepository", lambda active: types.SimpleNamespace()
    )

    assert asyncio.run(state.connect_to_server(1)) is True
    assert state.active_server is instance
    assert server.connected is True
    assert state.recipes == {}


def test_a_failed_attach_discards_its_observation_env(tmp_path, monkeypatch):
    """An observation environment built during a failed attach must not stay cached."""

    events = []

    class _FakeEnv:
        def reset(self, *args, **kwargs):
            events.append("reset")

        def close(self):
            events.append("close")

    instance = _RecordingInstance(name="srv")
    server = state_module.FactorioServer(
        address="127.0.0.1", tcp_port=27200, instance_id=1, name="srv", is_active=True
    )
    state = FactorioMCPState()
    state.available_servers = {1: server}

    monkeypatch.setenv("FLE_RESET_ON_CONNECT", "1")
    monkeypatch.setattr(state_module, "RECIPES_PATH", tmp_path / "recipes.jsonl")
    monkeypatch.setattr(
        state, "create_factorio_instance", lambda instance_id, **kw: instance
    )
    monkeypatch.setattr(state, "_make_gym_env", lambda env_id: _FakeEnv())

    async def refreshes(instance_id):
        return None

    monkeypatch.setattr(state, "refresh_game_data", refreshes)
    monkeypatch.setattr(
        state,
        "load_recipes_from_file",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert asyncio.run(state.connect_to_server(1)) is False
    assert "reset" in events and "close" in events
    assert state._gym_env is None


def test_a_failed_reattach_keeps_the_previous_connected_flag(monkeypatch):
    """The rollback must not clear a connection that was already established."""

    server = state_module.FactorioServer(
        address="127.0.0.1", tcp_port=27200, instance_id=1, name="srv", is_active=True
    )
    server.connected = True
    previous = _RecordingInstance(name="previous")

    state = FactorioMCPState()
    state.available_servers = {1: server}
    state.active_server = previous

    def failing_factory(instance_id, **kwargs):
        raise RuntimeError("cannot build the instance")

    monkeypatch.setattr(state, "create_factorio_instance", failing_factory)

    assert asyncio.run(state.connect_to_server(1)) is False
    assert server.connected is True
    assert state.active_server is previous


def test_a_failed_attach_discards_a_replacement_observation_env(tmp_path, monkeypatch):
    """A pin change replaces the cached env; the replacement must not survive either."""

    built = []

    class _FakeEnv:
        def __init__(self, tag):
            self.tag = tag
            self.closed = False

        def reset(self, *args, **kwargs):
            return None

        def close(self):
            self.closed = True

    def make_env(env_id):
        env = _FakeEnv(f"env-{len(built)}")
        built.append(env)
        return env

    instance = _RecordingInstance(name="srv")
    server = state_module.FactorioServer(
        address="127.0.0.1", tcp_port=27200, instance_id=1, name="srv", is_active=True
    )
    state = FactorioMCPState()
    state.available_servers = {1: server}
    # A cached environment built for a previous attachment.
    state._gym_env = make_env("preexisting")
    state._gym_env_target = ("127.0.0.1", 27100)

    monkeypatch.setenv("FLE_RESET_ON_CONNECT", "1")
    monkeypatch.setenv("FLE_SERVER", "factorio_other")
    monkeypatch.setattr(state_module, "RECIPES_PATH", tmp_path / "recipes.jsonl")
    monkeypatch.setattr(
        state, "create_factorio_instance", lambda instance_id, **kw: instance
    )
    monkeypatch.setattr(state, "_make_gym_env", make_env)

    async def refreshes(instance_id):
        return None

    monkeypatch.setattr(state, "refresh_game_data", refreshes)
    monkeypatch.setattr(
        state,
        "load_recipes_from_file",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert asyncio.run(state.connect_to_server(1)) is False
    assert len(built) == 2, "the pin change should have built a replacement"
    assert built[0].closed is True
    assert built[1].closed is True, (
        "the replacement created by the failed attach must be closed"
    )
    assert state._gym_env is None


def test_a_failed_reattach_does_not_repoint_observations(tmp_path, monkeypatch):
    """A failed re-attach keeps observing the world the session is attached to.

    Changing the pin and failing to attach elsewhere used to leave the
    observation environment keyed on the new pin, so the session read one world
    and acted on another.
    """

    built = []

    class _FakeEnv:
        def __init__(self):
            self.window = None

        def reset(self, *args, **kwargs):
            return None

        def close(self):
            return None

    def make_env(env_id):
        env = _FakeEnv()
        built.append(env)
        return env

    attached = _RecordingInstance(name="factorio_1")
    attached.address = "127.0.0.1"
    attached.tcp_port = 27200
    server = state_module.FactorioServer(
        address="127.0.0.1", tcp_port=27200, instance_id=1, name="srv", is_active=True
    )
    state = FactorioMCPState()
    state.available_servers = {1: server}
    state.active_server = attached

    # An environment already observing the attached world.
    monkeypatch.setattr(state, "_make_gym_env", make_env)
    observed_first = state.gym_env
    observed_target = state._gym_env_target

    monkeypatch.setenv("FLE_SERVER", "factorio_2")
    monkeypatch.setattr(state_module, "RECIPES_PATH", tmp_path / "recipes.jsonl")
    monkeypatch.setenv("FLE_RESET_ON_CONNECT", "1")

    def failing_factory(instance_id, **kwargs):
        raise RuntimeError("cannot attach to the pinned server")

    monkeypatch.setattr(state, "create_factorio_instance", failing_factory)

    assert asyncio.run(state.connect_to_server(1)) is False
    assert state.active_server is attached
    assert state.observation_target() == ("127.0.0.1", 27200)
    assert state.gym_env is observed_first, "observations must not move to the new pin"
    assert state._gym_env_target == observed_target


def test_observations_are_built_for_the_attached_container(monkeypatch):
    """The observation env must be constructed for the attachment, not the pin."""

    calls = []
    resets = []

    class _FakeEnv:
        def reset(self, *args, **kwargs):
            resets.append(kwargs)

        def close(self):
            return None

    fake_gym = types.SimpleNamespace(
        make=lambda env_id, **kwargs: calls.append(kwargs) or _FakeEnv()
    )
    monkeypatch.setattr(state_module, "gym", fake_gym)
    # Restore the real builder for this test; `gym` is stubbed above, so nothing
    # attaches to a live container.
    monkeypatch.setattr(
        state_module.FactorioMCPState, "_make_gym_env", REAL_MAKE_GYM_ENV
    )

    attached = _RecordingInstance(name="factorio_1")
    attached.address = "127.0.0.1"
    attached.tcp_port = 27200
    record = state_module.FactorioServer(
        address="127.0.0.1",
        tcp_port=27200,
        instance_id=1,
        name="factorio_1",
        is_active=True,
    )
    state = FactorioMCPState()
    # Keyed by enumeration index, exactly like scan_for_servers.
    state.available_servers = {1: record}
    state.active_server = attached
    attached.container_name = "factorio_1"

    # The pin points elsewhere; the attachment wins.
    monkeypatch.setenv("FLE_SERVER", "factorio_2")
    monkeypatch.setattr(
        state_module, "RECIPES_PATH", Path("/nonexistent/recipes.jsonl")
    )

    state.gym_env

    assert calls, "the environment should have been built"
    assert calls[0]["container_name"] == "factorio_1"
    assert calls[0]["rcon_port"] == 27200


def test_a_rolled_back_reattach_does_not_provision_the_previous_world(monkeypatch):
    """Rebuilding observations after a failed attach elsewhere must not reset the
    world the session fell back to."""

    calls = []
    resets = []

    class _FakeEnv:
        def reset(self, *args, **kwargs):
            resets.append(kwargs)

        def close(self):
            return None

    fake_gym = types.SimpleNamespace(
        make=lambda env_id, **kwargs: calls.append(kwargs) or _FakeEnv()
    )
    monkeypatch.setattr(state_module, "gym", fake_gym)
    monkeypatch.setattr(
        state_module.FactorioMCPState, "_make_gym_env", REAL_MAKE_GYM_ENV
    )

    attached = _RecordingInstance(name="factorio_1")
    attached.address = "127.0.0.1"
    attached.tcp_port = 27200
    attached.container_name = "factorio_1"

    other = _RecordingInstance(name="factorio_2")
    other.address = "127.0.0.1"
    other.tcp_port = 27300
    other.container_name = "factorio_2"

    records = {
        1: state_module.FactorioServer(
            address="127.0.0.1",
            tcp_port=27200,
            instance_id=1,
            name="factorio_1",
            is_active=True,
        ),
        2: state_module.FactorioServer(
            address="127.0.0.1",
            tcp_port=27300,
            instance_id=2,
            name="factorio_2",
            is_active=True,
        ),
    }
    state = FactorioMCPState()
    state.available_servers = records
    state.active_server = attached

    # An environment provisioned for the attached world already exists.
    state.gym_env

    # Lab mode: attaching elsewhere provisions that world, then fails.
    monkeypatch.setenv("FLE_RESET_ON_CONNECT", "1")
    monkeypatch.setattr(
        state, "create_factorio_instance", lambda instance_id, **kw: other
    )
    monkeypatch.setattr(
        state,
        "load_recipes_from_file",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(state, "refresh_game_data", lambda instance_id: _noop())

    assert asyncio.run(state.connect_to_server(2)) is False
    assert state.active_server is attached

    calls.clear()
    resets.clear()
    rebuilt = state.gym_env

    assert resets == [], "the rebuild must not reset the world it observes"
    assert calls, "observations should be rebuilt for the restored attachment"
    assert calls[0]["reset_on_init"] is False, "the rebuild must not provision again"
    assert calls[0]["container_name"] == "factorio_1"
    assert rebuilt is not None


async def _noop():
    return None


def test_two_containers_publishing_one_port_keep_their_own_address(monkeypatch):
    """Two containers can publish the same RCON port on different host addresses.

    Discovery used to assign 127.0.0.1 to everything, so probing and connecting
    reached whichever container owned the loopback binding even when FLE_SERVER
    named the other one.
    """

    containers = {
        "id-a": ("factorio_1", "127.0.0.1"),
        "id-b": ("factorio_2", "127.0.0.2"),
    }
    listing = "".join(f'"{cid}"\n' for cid in containers).strip()

    def fake_run(cmd, capture_output, text):
        if cmd[:3] == ["docker", "ps", "--filter"]:
            return subprocess.CompletedProcess(cmd, 0, listing, "")
        cid = cmd[-1]
        name, host_ip = containers[cid]
        payload = [
            {
                "Name": f"/{name}",
                "NetworkSettings": {
                    "Ports": {
                        "27015/tcp": [{"HostIp": host_ip, "HostPort": "27100"}],
                        "34197/udp": [{"HostIp": host_ip, "HostPort": "35197"}],
                    }
                },
            }
        ]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    found = {c.name: c.address for c in list_factorio_containers()}

    assert found == {"factorio_1": "127.0.0.1", "factorio_2": "127.0.0.2"}


def test_wildcard_publish_still_resolves_to_loopback(monkeypatch):
    """A wildcard binding is reachable on loopback; an empty HostIp too."""

    payload = [
        {
            "Name": "/factorio_1",
            "NetworkSettings": {
                "Ports": {
                    "27015/tcp": [{"HostIp": "", "HostPort": "27100"}],
                    "34197/udp": [{"HostIp": "0.0.0.0", "HostPort": "35197"}],
                }
            },
        }
    ]

    def fake_run(cmd, capture_output, text):
        if cmd[:3] == ["docker", "ps", "--filter"]:
            return subprocess.CompletedProcess(cmd, 0, '"cid"\n', "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    (container,) = list_factorio_containers()
    assert container.address == "127.0.0.1"


def test_the_attach_forwards_the_scanned_endpoint(monkeypatch):
    """A container appearing between scan and attach must not steal the attach.

    The factory re-discovers by index, so connect_to_server has to pass the
    endpoint the scan resolved; otherwise a lower-sorted container that starts
    later takes over the index and the session attaches to the wrong world.
    """

    seen = {}

    def fake_factory(instance_id, address=None, tcp_port=None):
        seen.update(instance_id=instance_id, address=address, tcp_port=tcp_port)
        return _RecordingInstance(name="srv")

    server = state_module.FactorioServer(
        address="127.0.0.5",
        tcp_port=27300,
        instance_id=0,
        name="pinned",
        is_active=True,
    )
    state = FactorioMCPState()
    state.available_servers = {0: server}
    monkeypatch.setattr(state, "create_factorio_instance", fake_factory)
    monkeypatch.setattr(state, "refresh_game_data", lambda instance_id: _noop())
    monkeypatch.setattr(state, "load_recipes_from_file", lambda: {"x": 1})
    monkeypatch.setattr(
        state_module, "FactorioMCPRepository", lambda instance: object()
    )

    assert asyncio.run(state.connect_to_server(0)) is True
    assert seen == {"instance_id": 0, "address": "127.0.0.5", "tcp_port": 27300}


def test_the_container_identity_matches_address_and_port(monkeypatch):
    """Two containers publishing the same port on different hosts must not be
    confused: the identity has to match the resolved endpoint exactly."""

    built = []

    class _FakeInstance:
        def __init__(self, address, tcp_port, **kwargs):
            built.append((address, tcp_port))
            self.rcon_client = types.SimpleNamespace(send_command=lambda command: "1")
            self.first_namespace = types.SimpleNamespace(
                _create_agent_characters=lambda n: None
            )

        def set_speed(self, speed):
            return None

    monkeypatch.setattr(state_module, "FactorioInstance", _FakeInstance)
    monkeypatch.setattr(
        state_module,
        "get_local_container_ips",
        lambda: (["127.0.0.1", "127.0.0.2"], [35197, 35197], [27100, 27100]),
    )

    first = state_module.FactorioServer(
        address="127.0.0.1", tcp_port=27100, instance_id=0, name="factorio_1"
    )
    second = state_module.FactorioServer(
        address="127.0.0.2", tcp_port=27100, instance_id=1, name="factorio_2"
    )
    state = FactorioMCPState()
    state.available_servers = {0: first, 1: second}

    # Index 1 is the one that matters: a port-only lookup matches the first
    # record and would still pass on index 0.
    instance = state.create_factorio_instance(1)

    assert built == [("127.0.0.2", 27100)]
    assert instance.container_name == "factorio_2"


def test_the_observation_env_receives_the_attached_address(monkeypatch):
    """The environment must read the world the session is attached to.

    A container that is renamed frees its name and port for a replacement on
    another host, so rebuilding the target from the name alone can point the
    observation environment at a different world than actions.
    """
    state = FactorioMCPState()
    state.available_servers[0] = types.SimpleNamespace(
        address="127.0.0.7", tcp_port=27100, container_name="factorio_7"
    )
    state.active_server = state.available_servers[0]

    seen = {}
    monkeypatch.setattr(
        state_module,
        "gym",
        types.SimpleNamespace(
            make=lambda env_id, **kwargs: seen.update(kwargs) or object()
        ),
    )
    # The autouse guard replaces the method; this test needs the real one.
    monkeypatch.setattr(FactorioMCPState, "_make_gym_env", REAL_MAKE_GYM_ENV)

    state._make_gym_env("Factorio-v0")

    assert seen["address"] == "127.0.0.7"
    assert seen["container_name"] == "factorio_7"
    assert seen["rcon_port"] == 27100


def test_the_registry_prefers_the_attached_address(monkeypatch):
    """A pin that resolves to a replacement container must not win."""
    selected = FactorioContainer(
        name="factorio_7",
        container_id="cid",
        address="127.0.0.9",  # what a fresh lookup would say
        tcp_port=27100,
        udp_port=35197,
    )
    monkeypatch.setattr(registry_module, "list_factorio_containers", lambda: [selected])
    monkeypatch.delenv("FACTORIO_SERVER_ADDRESS", raising=False)
    monkeypatch.delenv("FACTORIO_SERVER_PORT", raising=False)

    captured = {}

    class _Stop(Exception):
        pass

    def fake_instance(**kwargs):
        captured.update(kwargs)
        raise _Stop

    monkeypatch.setattr(registry_module, "FactorioInstance", fake_instance)
    monkeypatch.setattr(
        registry_module,
        "TaskFactory",
        types.SimpleNamespace(create_task=lambda p: None),
    )
    monkeypatch.setattr(
        registry_module, "list_available_environments", lambda: [], raising=False
    )

    # The factory wraps the build in its retry/error handler, so the sentinel
    # surfaces as a RuntimeError; the captured kwargs are what this test checks.
    with pytest.raises(RuntimeError):
        registry_module.make_factorio_env(
            types.SimpleNamespace(task_config_path=None, num_agents=1),
            run_idx=0,
            container_name="factorio_7",
            rcon_port=27100,
            address="127.0.0.7",  # the attachment's endpoint
        )

    assert captured["address"] == "127.0.0.7"
    assert int(captured["tcp_port"]) == 27100


def test_a_complete_attachment_endpoint_needs_no_name_lookup(monkeypatch):
    """A renamed container must not break observation on a live attachment.

    The name is gone, so name resolution fails or matches a replacement; the
    endpoint the session is attached to is what has to be used.
    """
    monkeypatch.setattr(registry_module, "list_factorio_containers", lambda: [])
    monkeypatch.delenv("FACTORIO_SERVER_ADDRESS", raising=False)
    monkeypatch.delenv("FACTORIO_SERVER_PORT", raising=False)

    captured = {}

    class _Stop(Exception):
        pass

    def fake_instance(**kwargs):
        captured.update(kwargs)
        raise _Stop

    monkeypatch.setattr(registry_module, "FactorioInstance", fake_instance)
    monkeypatch.setattr(
        registry_module,
        "TaskFactory",
        types.SimpleNamespace(create_task=lambda p: None),
    )

    with pytest.raises(RuntimeError):
        registry_module.make_factorio_env(
            types.SimpleNamespace(task_config_path=None, num_agents=1),
            run_idx=0,
            container_name="renamed-away",
            rcon_port=27100,
            address="127.0.0.7",
        )

    assert captured["address"] == "127.0.0.7"
    assert int(captured["tcp_port"]) == 27100


def test_a_cached_environment_is_provisioned_by_a_later_opt_in(monkeypatch):
    """Attaching twice to the same endpoint with reset turned on must provision.

    The cached environment is reused, so the opt-in has to reach it instead of
    being skipped because construction happened earlier with reset off.
    """
    state = FactorioMCPState()
    state.available_servers[0] = types.SimpleNamespace(
        address="127.0.0.7", tcp_port=27100, container_name="factorio_7"
    )
    state.active_server = state.available_servers[0]

    resets = []

    class _Env:
        def reset(self, *args, **kwargs):
            resets.append(True)
            return None

    monkeypatch.setattr(
        state_module, "gym", types.SimpleNamespace(make=lambda env_id, **kw: _Env())
    )
    monkeypatch.setattr(FactorioMCPState, "_make_gym_env", REAL_MAKE_GYM_ENV)

    monkeypatch.delenv("FLE_RESET_ON_CONNECT", raising=False)
    state.gym_env  # first attach: safe mode, nothing provisioned
    assert resets == []

    monkeypatch.setenv("FLE_RESET_ON_CONNECT", "1")
    state._provision_gym_env = True
    state.gym_env  # same endpoint, now opted in
    assert resets == [True], "the cached environment must be provisioned"

    state.gym_env  # and not provisioned again on every access
    assert resets == [True]


def test_a_pin_can_name_a_secondary_rcon_binding(monkeypatch):
    """A container published on two RCON host ports must be pinnable by either."""

    payload = [
        {
            "Name": "/factorio_1",
            "NetworkSettings": {
                "Ports": {
                    "27015/tcp": [
                        {"HostIp": "127.0.0.1", "HostPort": "27100"},
                        {"HostIp": "127.0.0.1", "HostPort": "27101"},
                    ],
                    "34197/udp": [{"HostIp": "127.0.0.1", "HostPort": "35197"}],
                }
            },
        }
    ]

    def fake_run(cmd, capture_output, text):
        if cmd[:3] == ["docker", "ps", "--filter"]:
            return subprocess.CompletedProcess(cmd, 0, '"cid"\n', "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    (container,) = list_factorio_containers()
    assert container.tcp_port == 27100
    assert container.rcon_bindings == (("127.0.0.1", 27100), ("127.0.0.1", 27101))

    pinned = select_container([container], rcon_port=27101)
    assert pinned.container_id == container.container_id
    assert pinned.tcp_port == 27101


def test_a_secondary_port_pin_still_scans_the_pinned_container(monkeypatch):
    """A pin resolved to a secondary binding must survive the scan loop.

    The pin returns a re-addressed copy, so an equality check against the
    discovery record skips it and the session finds no servers at all.
    """
    cluster, state = _install_fake_cluster(monkeypatch)
    monkeypatch.setattr(
        state_module,
        "list_factorio_containers",
        lambda: [
            FactorioContainer(
                name="factorio_1",
                container_id="id-factorio_1",
                address="127.0.0.1",
                udp_port=35197,
                tcp_port=27100,
                rcon_bindings=(("127.0.0.1", 27100), ("127.0.0.1", 27101)),
            )
        ],
    )
    monkeypatch.setenv("FLE_RCON_PORT", "27101")
    monkeypatch.delenv("FLE_SERVER", raising=False)
    monkeypatch.delenv("FACTORIO_SERVER_PORT", raising=False)
    monkeypatch.delenv("FACTORIO_SERVER_ADDRESS", raising=False)

    servers = asyncio.run(state.scan_for_servers())

    assert len(servers) == 1, "the pinned container must not be skipped"
    assert servers[0].tcp_port == 27101
    assert servers[0].name == "factorio_1"


def test_a_failing_probe_cleanup_does_not_abort_the_scan(monkeypatch):
    """A reachable candidate must survive its own cleanup failing."""
    monkeypatch.setattr(
        state_module,
        "list_factorio_containers",
        lambda: [
            FactorioContainer(
                name="factorio_1",
                container_id="id-factorio_1",
                address="127.0.0.1",
                udp_port=35197,
                tcp_port=27100,
            )
        ],
    )
    monkeypatch.delenv("FLE_SERVER", raising=False)
    monkeypatch.delenv("FLE_RCON_PORT", raising=False)
    monkeypatch.delenv("FACTORIO_SERVER_PORT", raising=False)
    monkeypatch.delenv("FACTORIO_SERVER_ADDRESS", raising=False)

    class _ProbeThatFailsOnClose:
        def cleanup(self):
            raise RuntimeError("cleanup lost the socket")

    state = FactorioMCPState()
    monkeypatch.setattr(
        state,
        "_build_instance",
        lambda address, tcp_port: _ProbeThatFailsOnClose(),
    )

    servers = asyncio.run(state.scan_for_servers())

    assert len(servers) == 1
    assert servers[0].is_active is True, "the probe succeeded; cleanup must not undo it"


def test_direct_endpoint_attaches_without_container_discovery(monkeypatch):
    """A natively run server is addressable when host and port are both pinned.

    Container discovery cannot find it, so a session that only knows docker
    would report "no Factorio containers are running" for a world that is
    perfectly reachable — and another toolset may already be playing it.
    """
    cluster, state = _install_fake_cluster(monkeypatch)
    monkeypatch.setattr(
        state_module,
        "list_factorio_containers",
        lambda: pytest.fail("a direct endpoint must not touch docker discovery"),
    )
    monkeypatch.setenv("FLE_RCON_HOST", "127.0.0.1")
    monkeypatch.setenv("FLE_RCON_PORT", "34199")
    monkeypatch.delenv("FLE_SERVER", raising=False)

    message = asyncio.run(session_module.initialize_session())

    assert cluster.probes == [("127.0.0.1", 34199)], cluster.probes
    assert cluster.connections == [0], cluster.connections
    server = state.available_servers[0]
    assert (server.address, server.tcp_port) == ("127.0.0.1", 34199)
    assert "34199" in message


def test_a_host_without_a_port_still_uses_container_discovery(monkeypatch):
    """Half an endpoint is not an endpoint: the port may still name a container."""
    cluster, state = _install_fake_cluster(monkeypatch)
    monkeypatch.setenv("FLE_RCON_HOST", "127.0.0.1")
    monkeypatch.delenv("FLE_RCON_PORT", raising=False)

    asyncio.run(session_module.initialize_session())

    assert state.available_servers[0].name == "factorio_1"
    assert config_module.direct_rcon_endpoint() is None


def test_a_host_pin_and_a_name_pin_cannot_be_combined(monkeypatch):
    cluster, state = _install_fake_cluster(monkeypatch)
    monkeypatch.setenv("FLE_RCON_HOST", "127.0.0.1")
    monkeypatch.setenv("FLE_RCON_PORT", "34199")
    monkeypatch.setenv("FLE_SERVER", "factorio_1")

    message = asyncio.run(session_module.initialize_session())

    assert "Initialization failed" in message
    assert "cannot name a container as well" in message
    assert cluster.connections == [], "nothing may attach on a contradictory pin"


def test_local_settings_name_the_endpoint_without_environment(monkeypatch, tmp_path):
    """A machine names its endpoint once; sessions need no exported variables."""
    settings = tmp_path / "mcp.json"
    settings.write_text('{"rcon_host": "10.0.0.5", "rcon_port": 34199}')
    monkeypatch.setenv("FLE_LOCAL_SETTINGS", str(settings))
    monkeypatch.delenv("FLE_RCON_HOST", raising=False)
    monkeypatch.delenv("FLE_RCON_PORT", raising=False)

    assert config_module.direct_rcon_endpoint() == ("10.0.0.5", 34199)


def test_the_environment_still_wins_over_the_settings_file(monkeypatch, tmp_path):
    settings = tmp_path / "mcp.json"
    settings.write_text('{"rcon_host": "10.0.0.5", "rcon_port": 34199}')
    monkeypatch.setenv("FLE_LOCAL_SETTINGS", str(settings))
    monkeypatch.setenv("FLE_RCON_HOST", "127.0.0.1")
    monkeypatch.setenv("FLE_RCON_PORT", "27200")

    assert config_module.direct_rcon_endpoint() == ("127.0.0.1", 27200)


def test_a_malformed_settings_file_says_so(monkeypatch, tmp_path):
    settings = tmp_path / "mcp.json"
    settings.write_text("{not json")
    monkeypatch.setenv("FLE_LOCAL_SETTINGS", str(settings))
    monkeypatch.delenv("FLE_RCON_HOST", raising=False)
    monkeypatch.delenv("FLE_RCON_PORT", raising=False)

    with pytest.raises(ValueError, match="not valid JSON"):
        config_module.direct_rcon_endpoint()


def test_undo_and_restore_refuse_to_reset_a_foreign_world(monkeypatch):
    """undo/restore call instance.reset(); on a world this session did not
    provision that would wipe someone else's factory."""
    monkeypatch.setenv("FLE_RCON_HOST", "127.0.0.1")
    monkeypatch.setenv("FLE_RCON_PORT", "34199")

    undo_refusal = version_control_module._foreign_world_refusal("undo")
    restore_refusal = version_control_module._foreign_world_refusal("restore")

    assert "does not own" in undo_refusal
    assert "34199" in undo_refusal
    assert "destroy" in restore_refusal
    assert "Refusing to run restore" in restore_refusal


def test_undo_is_allowed_on_a_world_this_session_owns(monkeypatch):
    """A sandbox reached through container discovery stays fully undoable."""
    monkeypatch.delenv("FLE_RCON_HOST", raising=False)
    monkeypatch.delenv("FLE_RCON_PORT", raising=False)

    assert version_control_module._foreign_world_refusal("undo") == ""
