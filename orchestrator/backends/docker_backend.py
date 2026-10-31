"""Docker backend: each island is its own container, created per run."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ..config import ISLAND_CONFIG_ENV
from ..settings import ServiceSettings

__all__ = [
    "DockerBackend",
    "DockerUnavailable",
    "IslandSpec",
    "IslandContainerStatus",
    "MASTER_CONTAINER_NAME_ENV",
]

MASTER_CONTAINER_NAME_ENV = "MASTER_CONTAINER_NAME"
"""Names the run's master container, when the Runner service started the master in one."""


@dataclass
class IslandSpec:
    """Everything one island container needs to start."""

    island_id: int
    run_id: str
    config_json: str
    control_host: str
    control_port: int


@dataclass
class IslandContainerStatus:
    """Whether one island's container is still running."""

    island_id: int
    node_id: str
    running: bool


class DockerUnavailable(RuntimeError):
    """Raised when the Docker SDK or daemon is not usable."""


class DockerBackend:
    """Runs each island in its own container."""

    name = "docker"

    def __init__(self, *, run_id: str, settings: ServiceSettings) -> None:
        try:
            import docker
        except ImportError as exc:
            raise DockerUnavailable(
                "the `docker` Python SDK is not installed; `pip install docker`"
            ) from exc

        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as exc:
            raise DockerUnavailable(
                f"cannot reach the Docker daemon ({exc}). Check that it is running and that "
                "this user is in the `docker` group"
            ) from exc

        self.settings = settings
        self.image = settings.island_image
        self.run_id = run_id
        self.cpus = settings.island_cpus
        self.memory = settings.island_memory
        self.labels = {
            "app": "archipelago",
            "component": "island",
            "archipelago.run_id": run_id,
        }

        self._containers: dict[int, Any] = {}
        self._node_ids: dict[int, str] = {}
        self.network_name: str = f"archipelago-{run_id[-8:]}"
        self.island_network: str = self.network_name
        self._created_network: Any | None = None
        self.master_alias: str | None = None
        self._master_container: Any | None = None
        self._master_networks: list[str] = []


    def prepare(self) -> None:
        """Check the island image exists, then make the run's network ready."""
        import docker.errors

        try:
            self.client.images.get(self.image)
        except docker.errors.ImageNotFound:
            raise DockerUnavailable(
                f"island image {self.image!r} not found. Build it with:  "
                f"docker build --network=host -f docker/Dockerfile.island -t {self.image} ."
            ) from None
        self._ensure_network()

    MASTER_ALIAS = "archipelago-master"

    def _ensure_network(self) -> None:
        """Decide where the islands live, so that they can reach the master and Redis."""
        master = self._own_container()

        if master is not None and master.attrs["HostConfig"]["NetworkMode"] != "host":
            # Islands also join every network the master is on, so they reach Redis at the
            # address the master uses.
            self._master_networks = sorted(master.attrs["NetworkSettings"]["Networks"])
            self._created_network = self.client.networks.create(
                self.network_name, driver="bridge", labels=self.labels
            )
            self.island_network = self.network_name
            self._attach_master(master)
        else:
            self.island_network = "host"
            self.network_name = "host"

    def _attach_master(self, container: Any) -> None:
        """Join the islands' network, so they can reach us by name."""
        try:
            self.client.networks.get(self.island_network).connect(
                container.id, aliases=[self.MASTER_ALIAS]
            )
        except Exception as exc:
            raise DockerUnavailable(
                f"the master is running in container {container.id[:12]} but could not join "
                f"the island network {self.island_network!r}: {exc}"
            ) from exc
        self._master_container = container
        self.master_alias = self.MASTER_ALIAS

    def _own_container(self) -> Any | None:
        """The master's own container, or None when the master runs on the host.

        The Runner service names the container when it starts a master in one. A master in a
        container on the host's network is treated like one on the host: its islands share that
        network too.
        """
        name = os.environ.get(MASTER_CONTAINER_NAME_ENV)
        if name is None:
            return None
        try:
            return self.client.containers.get(name)
        except Exception as exc:
            raise DockerUnavailable(f"cannot find the master's own container {name!r}: {exc}") from exc

    @staticmethod
    def container_name(run_id: str, island_id: int) -> str:
        """This island's container name, and its ``node_id`` in the log."""
        return f"archipelago-{run_id[-8:]}-island-{island_id:02d}"

    def launch(self, spec: IslandSpec) -> str:
        """Create and start one island's container."""
        command = [
            "--run-id",
            spec.run_id,
            "--island-id",
            str(spec.island_id),
            "--control-host",
            spec.control_host,
            "--control-port",
            str(spec.control_port),
        ]
        container_name = self.container_name(spec.run_id, spec.island_id)
        environment = {
            **self.settings.to_env(),
            "PYTHONUNBUFFERED": "1",
            "IN_ARCHIPELAGO_CONTAINER": "1",
            "ISLAND_CONTAINER_NAME": container_name,
            ISLAND_CONFIG_ENV: spec.config_json,
        }
        container = self.client.containers.create(
            self.image,
            command=command,
            name=container_name,
            labels={**self.labels, "archipelago.island_id": str(spec.island_id)},
            environment=environment,
            network=self.island_network,
            nano_cpus=int(self.cpus * 1e9),
            mem_limit=self.memory,
            auto_remove=False,
            working_dir="/archipelago",
        )
        self._containers[spec.island_id] = container
        for network in self._master_networks:
            self.client.networks.get(network).connect(container)
        container.start()
        self._node_ids[spec.island_id] = container_name
        return container_name

    def exit_report(self, island_id: int, tail: int = 40) -> dict[str, Any]:
        """An island container's exit code and the last lines of its output.

        Read before :meth:`cleanup` removes the container, so a failure that never reached the
        master (a crash before the island could say goodbye) is still explained.
        """
        container = self._containers.get(island_id)
        if container is None:
            return {"exit_code": None, "log_tail": "no container was created for this island"}
        try:
            container.reload()
            exit_code = container.attrs.get("State", {}).get("ExitCode")
            log_tail = container.logs(tail=tail).decode("utf-8", errors="replace")
        except Exception as exc:
            return {"exit_code": None, "log_tail": f"could not read the container: {exc}"}
        return {"exit_code": exit_code, "log_tail": log_tail.strip()}

    def statuses(self) -> dict[int, IslandContainerStatus]:
        """Poll container states."""
        result: dict[int, IslandContainerStatus] = {}
        for island_id, container in self._containers.items():
            try:
                container.reload()
                running = bool(container.attrs.get("State", {}).get("Running"))
            except Exception:
                running = False
            result[island_id] = IslandContainerStatus(
                island_id=island_id,
                node_id=self._node_ids.get(island_id, ""),
                running=running,
            )
        return result

    def signal_all(self, signal: str = "SIGTERM") -> None:
        """Send a signal to every island container still running, without waiting."""
        for container in self._containers.values():
            try:
                container.reload()
                if container.attrs.get("State", {}).get("Running"):
                    container.kill(signal=signal)
            except Exception:
                pass

    def kill(self, island_ids: list[int]) -> None:
        """Kill these islands' containers now."""
        for island_id in island_ids:
            container = self._containers.get(island_id)
            if container is None:
                continue
            try:
                container.kill()
            except Exception:
                pass

    def cleanup(self) -> None:
        """Remove the run's containers and the network created for it."""
        for container in self._containers.values():
            try:
                container.remove(force=True)
            except Exception:
                pass
        self._containers.clear()

        if self._master_container is not None and self._created_network is not None:
            try:
                self._created_network.disconnect(self._master_container.id, force=True)
            except Exception:
                pass
        self._master_container = None
        self.master_alias = None

        if self._created_network is not None:
            try:
                self._created_network.remove()
            except Exception:
                pass
            self._created_network = None

    def describe(self) -> dict[str, Any]:
        """Backend facts recorded in run_start."""
        return {
            "backend": self.name,
            "image": self.image,
            "cpus": self.cpus,
            "memory": self.memory,
            "network": self.island_network,
            "network_created_for_this_run": self._created_network is not None,
            "master_networks": self._master_networks,
            "master_in_container": self.master_alias is not None,
            "labels": self.labels,
        }
