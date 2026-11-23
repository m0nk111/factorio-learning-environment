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
            state, "create_factorio_instance", lambda instance_id: object()
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
