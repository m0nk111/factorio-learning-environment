import os
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from fle.env.a2a_instance import A2AFactorioInstance
import gymnasium as gym

from fle.commons.cluster_ips import (
    get_local_container_ips,
    list_factorio_containers,
    select_container,
)
from fle.commons.asyncio_utils import run_async_safely
from fle.env import FactorioInstance
from fle.env.gym_env.environment import FactorioGymEnv
from fle.eval.tasks import TaskFactory

PORT_OFFSET = int(os.environ.get("PORT_OFFSET", 0))


@dataclass
class GymEnvironmentSpec:
    """Specification for a registered gym environment"""

    task_key: str
    task_config_path: str
    description: str
    num_agents: int
    enable_vision: bool = False


class FactorioGymRegistry:
    """Registry for Factorio gym environments"""

    def __init__(self):
        self._environments: Dict[str, GymEnvironmentSpec] = {}
        self._discovered = False

    def discover_tasks(self) -> None:
        """Automatically discover all task definitions and register them as gym environments"""
        if self._discovered:
            return

        # Register all Python-based tasks from the unified registry
        from fle.eval.tasks.task_definitions.task_registry import (
            list_all_tasks,
            get_task_info,
        )

        for task_key in list_all_tasks():
            task_info = get_task_info(task_key)
            self.register_environment(
                task_key=task_key,
                task_config_path=task_key,  # Task key is used directly now
                description=task_info["goal_description"],
                num_agents=task_info["num_agents"],
            )

        self._discovered = True

    def register_environment(
        self,
        task_key: str,
        task_config_path: str,
        description: str,
        num_agents: int,
        enable_vision: bool = False,
    ) -> None:
        """Register a new gym environment"""
        spec = GymEnvironmentSpec(
            task_key=task_key,
            task_config_path=task_config_path,
            description=description,
            num_agents=num_agents,
            enable_vision=enable_vision,
        )

        self._environments[task_key] = spec

        # Register with gym
        gym.register(
            id=task_key,
            entry_point="fle.env.gym_env.registry:make_factorio_env",
            kwargs={"spec": spec},
            order_enforce=False,
        )

    def list_environments(self) -> List[str]:
        """List all registered environment IDs"""
        return list(self._environments.keys())

    def get_environment_spec(self, env_id: str) -> Optional[GymEnvironmentSpec]:
        """Get environment specification by ID"""
        return self._environments.get(env_id)


# Global registry instance
_registry = FactorioGymRegistry()


def make_factorio_env(
    spec: GymEnvironmentSpec,
    run_idx: int,
    reset_on_init: bool = True,
    container_name: Optional[str] = None,
    rcon_port: Optional[int] = None,
) -> FactorioGymEnv:
    """Factory function to create a Factorio gym environment.

    The instance-creation block (FactorioInstance build + task.setup +
    FactorioGymEnv wrap) can be retried with backoff by setting
    ``FLE_INIT_RETRIES`` > 1 (default 1, i.e. retries disabled; enable
    via ``fle inspect-eval --init-retries N``). Transient failures like
    ``"Could not save research state"`` happen when a Factorio container
    is recycled across samples and its RCON server returns malformed
    data on the first reset; a short wait + fresh attempt clears it.

    Configuration errors (no containers available, invalid run_idx) are
    raised without retry — they won't fix themselves.

    Args:
        reset_on_init: provision the world (reset game state, task setup). The
            evaluation harness needs this; a caller attaching to a world that
            already holds a factory passes False.
        container_name / rcon_port: explicit target container. When set, the
            environment attaches to exactly that container instead of the
            ``PORT_OFFSET + run_idx`` position, and an unresolvable pin raises
            with the candidates listed.
    """
    # Create task from the task definition
    task = TaskFactory.create_task(spec.task_config_path)

    # Resolve server address (no retry — pure config)
    address = os.getenv("FACTORIO_SERVER_ADDRESS")
    tcp_port = os.getenv("FACTORIO_SERVER_PORT")

    if container_name or rcon_port:
        # An explicit pin is authoritative: it must not be silently overridden by
        # FACTORIO_SERVER_ADDRESS/PORT, or a provisioning run would reset a world
        # the caller did not select.
        pinned = select_container(
            list_factorio_containers(), name=container_name, rcon_port=rcon_port
        )
        if (address and address != pinned.address) or (
            tcp_port and int(tcp_port) != pinned.tcp_port
        ):
            raise RuntimeError(
                f"Conflicting Factorio target: FACTORIO_SERVER_ADDRESS={address!r} "
                f"FACTORIO_SERVER_PORT={tcp_port!r} but the target pin resolves to "
                f"{pinned.describe()}. Unset one of the two."
            )
        address = pinned.address
        tcp_port = pinned.tcp_port
        print(f"Using pinned Factorio container {pinned.describe()}", file=sys.stderr)

    if not address and not tcp_port:
        try:
            ips, udp_ports, tcp_ports = get_local_container_ips()
        except ValueError:
            raise RuntimeError("No Factorio containers available")

        if len(tcp_ports) == 0:
            raise RuntimeError("No Factorio containers available")

        # Apply port offset for multiple terminal sessions
        container_idx = PORT_OFFSET + run_idx
        if container_idx >= len(tcp_ports):
            raise RuntimeError(
                f"Container index {container_idx} (PORT_OFFSET={PORT_OFFSET} + run_idx={run_idx}) exceeds available containers ({len(tcp_ports)})"
            )

        address = ips[container_idx]
        tcp_port = tcp_ports[container_idx]

    common_kwargs = {
        "address": address,
        "tcp_port": int(tcp_port),
        "num_agents": spec.num_agents,
        "fast": True,
        "cache_scripts": True,
        "inventory": {},
        "all_technologies_researched": False,
    }

    print(f"Using local Factorio container at {address}:{tcp_port}")

    # Retry the FactorioInstance build + task.setup block. The "Could
    # not save research state" failure mode happens when a Factorio
    # container is recycled across samples and its Lua script registry
    # holds stale state from the previous instance. ``FactorioInstance``
    # with ``cache_scripts=True`` (the default) compares checksums and
    # skips re-uploading scripts that already match — but the Lua VM
    # state behind those scripts can be corrupt. ``cleanup()`` only
    # closes the RCON connection, it does not reset the server.
    #
    # Fix: on every retry attempt after the first, force
    # ``cache_scripts=False`` so all scripts are freshly re-uploaded
    # (re-initializing their global Lua state). Plus longer backoff
    # so containers have time to settle.
    # Retries are opt-in: default is a single attempt (previous behavior).
    # Enable with `fle inspect-eval --init-retries N` or FLE_INIT_RETRIES=N.
    max_attempts = max(1, int(os.getenv("FLE_INIT_RETRIES", "1")))
    backoff = [int(x) for x in os.getenv("FLE_INIT_BACKOFF", "2,5,10,20,30").split(",")]
    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        instance = None
        try:
            attempt_kwargs = dict(common_kwargs)
            attempt_kwargs["reset_on_init"] = reset_on_init
            if attempt > 1:
                # Force fresh Lua script upload on retry — clears stale
                # server-side state that survived ``cleanup()``.
                attempt_kwargs["cache_scripts"] = False

            if spec.num_agents > 1:
                instance = run_async_safely(
                    A2AFactorioInstance.create(**attempt_kwargs)
                )
            else:
                instance = FactorioInstance(**attempt_kwargs)

            # Set initial speed and unpause
            instance.set_speed_and_unpause(10)

            # Setup the task. Task setup resets the game state and snapshots a
            # starting GameState, so it belongs to provisioning only: an
            # attaching session must not re-provision the world it attaches to.
            if reset_on_init:
                task.setup(instance)

            # Create and return the gym environment
            return FactorioGymEnv(
                instance=instance, task=task, enable_vision=spec.enable_vision
            )

        except Exception as e:
            last_err = e
            print(
                f"[make_factorio_env] attempt {attempt}/{max_attempts} failed "
                f"on container {address}:{tcp_port} "
                f"(cache_scripts={attempt_kwargs.get('cache_scripts')}): {e!r}"
            )
            # Best-effort cleanup of partially-built instance.
            if instance is not None:
                try:
                    instance.cleanup()
                except Exception:  # noqa: BLE001
                    pass
            if attempt < max_attempts:
                wait = backoff[min(attempt - 1, len(backoff) - 1)]
                print(f"[make_factorio_env] sleeping {wait}s before retry")
                time.sleep(wait)

    raise RuntimeError(f"Failed to create Factorio environment: {last_err}")


def register_all_environments() -> None:
    """Register all discovered environments with gym"""
    _registry.discover_tasks()


def list_available_environments() -> List[str]:
    """List all available gym environment IDs"""
    return _registry.list_environments()


def get_environment_info(task_key: str) -> Optional[Dict[str, Any]]:
    """Get detailed information about a specific environment"""
    spec = _registry.get_environment_spec(task_key)
    if spec is None:
        return None
    return asdict(spec)


# Auto-register environments when module is imported
register_all_environments()

# Example usage and documentation
if __name__ == "__main__":
    # List all available environments
    print("Available Factorio Gym Environments:")
    for env_id in list_available_environments():
        info = get_environment_info(env_id)
        print(f"  {env_id}: {info['description']}")
