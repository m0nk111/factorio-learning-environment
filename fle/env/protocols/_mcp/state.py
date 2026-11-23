import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

from fle.env import FactorioInstance
from fle.commons.cluster_ips import (
    ContainerSelectionError,
    get_local_container_ips,
    list_factorio_containers,
    select_container,
)
from fle.env.gym_env.registry import list_available_environments
from fle.env.protocols._mcp import config

from fle.env.protocols._mcp.models import FactorioServer, Recipe, ResourcePatch
from fle.env.protocols._mcp.repository import FactorioMCPRepository
import gymnasium as gym


def _diag(message: str) -> None:
    """Write a diagnostic to stderr: stdout carries the MCP protocol stream."""
    print(message, file=sys.stderr)


# Generated, gitignored recipe dump. Kept as a module constant so tests can point
# the loader at a known path instead of depending on local generated data.
RECIPES_PATH = Path(__file__).parent.parent / "data" / "recipes" / "recipes.jsonl"


class FactorioMCPState:
    """Manages the state of the Factorio MCP server"""

    def __init__(self):
        self.available_servers: Dict[int, FactorioServer] = {}  # instance_id -> server
        self.active_server: Optional[FactorioInstance] = None
        self.server_entities: Dict[
            int, Dict[str, Any]
        ] = {}  # instance_id -> {entity_id -> entity}
        self.server_resources: Dict[
            int, Dict[str, ResourcePatch]
        ] = {}  # instance_id -> {resource_id -> resource}
        self.recipes: Dict[str, Recipe] = {}  # Global recipes
        self.recipes_loaded = (
            False  # Flag to track if recipes have been loaded from file
        )
        self.checkpoints: Dict[
            int, Dict[str, str]
        ] = {}  # instance_id -> {checkpoint_name -> save file path}
        self.current_task: Optional[str] = None
        self.last_entity_update = 0
        self.vcs_repos: Dict[
            int, "FactorioMCPRepository"
        ] = {}  # instance_id -> VCS repo

        # The observation environment is created on first use. Building it opens
        # an RCON connection (and in lab mode provisions the world), so importing
        # this module must not attach to any container — that import used to be
        # enough to reset the world the session would later be pointed at.
        self._gym_env = None
        self._gym_env_target = None
        # Whether the cached environment has already been provisioned; an
        # environment built while provisioning was off must not silently skip a
        # later opted-in attach.
        self._gym_env_provisioned = False
        # Provisioning is an attach-time decision: an environment rebuilt later
        # (for instance after a failed re-attach rolled the session back) must
        # not reset the world it observes.
        self._provision_gym_env = False
        self.gym_env_id = self._resolve_gym_env_id()

    @property
    def gym_env(self):
        """Observation environment for this session, created on first use.

        A cached environment is only reused for the world it was built for. The
        key is the attached server, not the configuration: the pin can change
        while a session keeps its previous attachment (a failed re-attach rolls
        back to the old server), and observations must follow the world the
        session is actually attached to. Before the first attach the
        configuration is the only available target.
        """
        target = self.observation_target()
        if self._gym_env is not None and self._gym_env_target != target:
            self.close_gym_env()
        if self._gym_env is None:
            self._gym_env = self._make_gym_env(self.gym_env_id)
            self._gym_env_target = target
            self._gym_env_provisioned = False
        if not self._gym_env_provisioned:
            try:
                self._reset_gym_env()
            except Exception:
                # A half-initialised environment must not stay cached: the next
                # access has to retry rather than treat it as ready.
                self.close_gym_env()
                raise
        return self._gym_env

    def observation_container(self):
        """Container the observation environment must attach to.

        Same reasoning as :meth:`observation_target`: the configured pin can
        change while the session stays attached to its previous server, so an
        environment built for the pin would observe a different world.
        """
        server = self.active_server
        if server is not None:
            return (
                getattr(server, "container_name", None),
                getattr(server, "tcp_port", None),
            )
        return (config.server_pin_name(), config.server_pin_rcon_port())

    def observation_address(self):
        """Address of the attached container, for the observation environment.

        The container name is not an identity: a renamed container frees its
        name and port for a replacement elsewhere. The session keeps the address
        it actually attached to, so the environment reads the same world that
        actions go to.
        """
        server = self.active_server
        if server is not None:
            return getattr(server, "address", None)
        return None

    def observation_target(self):
        """World the observation environment belongs to.

        Bound to the active attachment so a failed re-attach that restored the
        previous server cannot leave observations pointed at a different world.
        """
        server = self.active_server
        if server is not None:
            return (
                getattr(server, "address", None),
                getattr(server, "tcp_port", None),
            )
        return (config.server_pin_name(), config.server_pin_rcon_port())

    def close_gym_env(self):
        """Drop the cached observation environment (its own RCON connection)."""
        env, self._gym_env = self._gym_env, None
        self._gym_env_target = None
        self._gym_env_provisioned = False
        if env is None:
            return
        try:
            env.close()
        except Exception as e:  # noqa: BLE001 - closing must not mask shutdown
            _diag(f"Closing the observation environment failed: {e}")

    @staticmethod
    def _resolve_gym_env_id() -> str:
        """Pick the environment that supplies observations for the session."""
        try:
            env_ids = list_available_environments()
            if not env_ids:
                raise Exception("No environments found")
            for env_id in env_ids:
                if "open" in env_id:
                    _diag(f"DEBUG: Using open environment: {env_id}")
                    return env_id
            return env_ids[0]
        except Exception as e:
            _diag(f"Error resolving gym environment: {e}")
            _diag("Falling back to steel_plate_throughput environment")
            return "steel_plate_throughput"

    def _make_gym_env(self, env_id: str):
        """Create the observation environment for this session.

        The target pin and the provisioning switch are forwarded, so this
        attachment obeys the same rules as the session: it never resets the
        game unless ``FLE_RESET_ON_CONNECT`` is set, and it never attaches to a
        different container than the session is pinned to.
        """
        container_name, rcon_port = self.observation_container()
        return gym.make(
            env_id,
            disable_env_checker=True,
            run_idx=0,
            reset_on_init=self._provision_gym_env,
            container_name=container_name,
            rcon_port=rcon_port,
            address=self.observation_address(),
        )

    def _reset_gym_env(self):
        """Provision the observation environment — only for an attach.

        ``_provision_gym_env`` is what actually guarantees that a rebuild after
        a rolled-back attach cannot reset the world: gating construction alone
        is not enough, because the environment is reset right after it is built.
        """
        if self._provision_gym_env and config.reset_on_connect():
            self._gym_env.reset()
            self._gym_env_provisioned = True
        return self._gym_env_provisioned

    def _build_instance(self, address: str, tcp_port: int) -> FactorioInstance:
        """Attach to one server by explicit address and RCON port."""
        instance = FactorioInstance(
            address=address,
            tcp_port=tcp_port,
            bounding_box=200,
            fast=True,
            cache_scripts=True,
            inventory={
                "stone-furnace": 1,
                "burner-mining-drill": 1,
                "wood": 5,
                "iron-plate": 8,
            },
            all_technologies_researched=False,
            reset_on_init=config.reset_on_connect(),
        )
        # Ensure agent characters exist (removed one-time associate command)
        # Check if agent characters exist, if not create them
        char_check = instance.rcon_client.send_command(
            "/c rcon.print(storage.agent_characters and #storage.agent_characters or 0)"
        )

        if int(char_check) == 0:
            instance.first_namespace._create_agent_characters(1)

        instance.set_speed(10)
        return instance

    def create_factorio_instance(
        self,
        instance_id: int,
        address: Optional[str] = None,
        tcp_port: Optional[int] = None,
    ) -> FactorioInstance:
        """Create a single Factorio instance

        The endpoint is looked up with a fresh container discovery unless an
        explicit `address`/`tcp_port` is passed, so a caller that already
        discovered the live endpoint (e.g. a reconnect) can reuse it.
        """
        if address is None or tcp_port is None:
            ips, udp_ports, tcp_ports = get_local_container_ips()

            if instance_id >= len(ips):
                raise IndexError(
                    f"instance_id {instance_id} out of range for ips list of length {len(ips)}"
                )
            if instance_id >= len(tcp_ports):
                raise IndexError(
                    f"instance_id {instance_id} out of range for tcp_ports list of length {len(tcp_ports)}"
                )
            address, tcp_port = ips[instance_id], tcp_ports[instance_id]

        # The attachment carries the container identity. available_servers is
        # keyed by enumeration index, so the record is matched on the resolved
        # endpoint — address *and* port, since two containers can publish the
        # same port on different hosts.
        server = next(
            (
                s
                for s in self.available_servers.values()
                if s.tcp_port == tcp_port and s.address == address
            ),
            None,
        )

        instance = self._build_instance(address, tcp_port)
        instance.container_name = getattr(server, "name", None)
        return instance

    async def scan_for_servers(self, ctx=None) -> List[FactorioServer]:
        """Scan for running Factorio servers.

        Servers are identified by their container name and published ports, not
        by their position in the docker listing. When the target is pinned
        (``FLE_SERVER``/``FLE_RCON_PORT``) only that container is probed, so a
        session never opens a connection to another container merely to find out
        whether it is alive; an unresolvable pin raises with the candidates.
        """
        containers = list_factorio_containers()

        target = None
        if (
            config.server_pin_name() is not None
            or config.server_pin_rcon_port() is not None
        ):
            target = select_container(
                containers,
                name=config.server_pin_name(),
                rcon_port=config.server_pin_rcon_port(),
            )
            # Fail before probing (and, in lab mode, before provisioning): a
            # contradictory FACTORIO_SERVER_ADDRESS/PORT means the target is
            # ambiguous, so nothing may attach.
            conflict = config.explicit_target_conflict(target.address, target.tcp_port)
            if conflict:
                raise ContainerSelectionError(conflict)

        scanned = 0
        new_servers = {}
        for instance_id, container in enumerate(containers):
            if target is not None and container != target:
                continue

            if ctx:
                await ctx.report_progress(scanned, len(containers))
                scanned += 1

            server = self.available_servers.get(instance_id)
            if server is None:
                server = FactorioServer(
                    address=container.address,
                    tcp_port=int(container.tcp_port),
                    udp_port=container.udp_port,
                    instance_id=instance_id,
                    name=container.name,
                    last_checked=time.time(),
                )
            else:
                server.last_checked = time.time()
                server.address = container.address
                server.tcp_port = int(container.tcp_port)
                server.udp_port = container.udp_port
                server.name = container.name

            # Try to verify if it's active
            if not server.is_active:
                probe = None
                try:
                    probe = self._build_instance(server.address, server.tcp_port)
                    server.is_active = True
                except Exception as e:
                    server.is_active = False
                    server.system_response = str(e)
                    # stderr: stdout carries the MCP protocol stream, and an
                    # unreachable candidate must not corrupt the handshake.
                    _diag(f"{server.name}: {e}")
                finally:
                    if probe is not None:
                        probe.cleanup()

            new_servers[instance_id] = server
            self.checkpoints.setdefault(instance_id, {})

        self.available_servers = new_servers
        return list(self.available_servers.values())

    async def connect_to_server(self, instance_id: int) -> bool:
        """Connect to a Factorio server by instance ID"""
        # Find the server with the given instance ID
        if instance_id not in self.available_servers:
            return False

        server = self.available_servers[instance_id]

        if not server.is_active:
            return False

        # An attach can fail after the RCON connection is already up (for example
        # when the observation environment raises in lab mode). Keep the previous
        # attachment so a failed attempt leaves the session exactly as it was.
        previous_active_server = self.active_server
        was_connected = getattr(server, "connected", False)
        previous_gym_env = self._gym_env
        instance = None

        try:
            # Create an instance to the server, passing the endpoint that the
            # scan resolved: a fresh discovery by index could pick a container
            # that appeared since the scan (FLE_SERVER would be bypassed).
            instance = self.create_factorio_instance(
                instance_id, address=server.address, tcp_port=server.tcp_port
            )

            # If we get here, the connection was successful
            server.connected = True

            self.active_server = instance

            # Initial data fetch
            await self.refresh_game_data(instance_id)

            # In lab mode the observation environment provisions the world, so
            # build it now — during the attach, before any action can run. In
            # world-safe mode it is built on first use and resets nothing.
            if config.reset_on_connect():
                self._provision_gym_env = True
                try:
                    self.gym_env
                finally:
                    self._provision_gym_env = False

            # Initialize recipes (global)
            if not self.recipes:
                self.recipes = self.load_recipes_from_file()

            # Initialize VCS repository for this instance if it doesn't exist
            if instance_id not in self.vcs_repos:
                _diag("Initializing repo")
                self.vcs_repos[instance_id] = FactorioMCPRepository(instance)

            return True
        except Exception as e:
            _diag(f"Error connecting to Factorio server: {e}")
            # Roll the attach back: a caller that sees False must not be left with
            # a live RCON connection or a server marked as connected.
            self.active_server = previous_active_server
            server.connected = was_connected
            if self._gym_env is not previous_gym_env and self._gym_env is not None:
                # Lab mode built (or replaced) an observation environment during
                # this attempt; it holds its own RCON connection and must not stay
                # cached. Comparing identity also covers a cached environment that
                # was closed and replaced because the pin changed.
                self.close_gym_env()
            if instance is not None:
                try:
                    instance.cleanup()
                except Exception as cleanup_error:
                    _diag(f"Cleaning up the failed attach failed: {cleanup_error}")
            return False

    def get_vcs(self):
        """Get the VCS repository for the active server"""
        if not self.active_server:
            return None

        instance_id = self.active_server.tcp_port
        if instance_id not in self.vcs_repos:
            self.vcs_repos[instance_id] = FactorioMCPRepository(self.active_server)

        return self.vcs_repos[instance_id]

    async def refresh_game_data(self, instance_id: int):
        """Refresh game data for a specific server instance"""
        if instance_id not in self.available_servers:
            return False

        self.last_entity_update = time.time()
        return True

    def load_recipes_from_file(self) -> Dict[str, Recipe]:
        """Load recipes from the jsonl file"""
        if self.recipes_loaded:
            return self.recipes

        recipes_path = RECIPES_PATH

        if not recipes_path.exists():
            # The recipe dump is generated data (gitignored), so it can legitimately
            # be absent. It powers the recipe resources only; a missing file must not
            # fail the attach, and the upstream author's local macOS path is not a
            # fallback that can ever resolve here.
            _diag(
                f"No recipe dump at {recipes_path}; recipe resources stay empty "
                "until one is generated"
            )
            return {}

        try:
            recipes = {}
            with open(recipes_path, "r") as f:
                for line in f:
                    if line.strip():
                        try:
                            recipe_data = json.loads(line)
                            # Extract top-level ingredients and results
                            ingredients = recipe_data.get("ingredients", [])
                            # For simplicity, we'll use just the name and amount from ingredients
                            simplified_ingredients = []
                            for ingredient in ingredients:
                                simplified_ingredients.append(
                                    {
                                        "name": ingredient.get("name", ""),
                                        "amount": ingredient.get("amount", 1),
                                    }
                                )

                            # Most recipes don't have a results field in the JSONL, so we'll create one
                            results = [
                                {"name": recipe_data.get("name", ""), "amount": 1}
                            ]

                            recipes[recipe_data["name"]] = Recipe(
                                name=recipe_data["name"],
                                ingredients=simplified_ingredients,
                                results=results,
                                energy_required=1.0,  # Default value as it's not in the JSONL
                            )
                        except json.JSONDecodeError:
                            _diag(f"Warning: Could not parse recipe line: {line}")
                        except KeyError as e:
                            _diag(f"Warning: Missing key in recipe: {e}")
                        except Exception as e:
                            _diag(f"Warning: Error processing recipe: {e}")

            self.recipes_loaded = True
            return recipes
        except Exception as e:
            _diag(f"Error loading recipes from file: {e}")
            raise e
