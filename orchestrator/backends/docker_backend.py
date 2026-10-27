"""Docker backend: each island is its own container, created per run."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import BROKER_URL_ENV

__all__ = [
    "DockerBackend",
    "DockerUnavailable",
    "WorkerSpec",
    "WorkerStatus",
    "HOST_RUNS_DIR_ENV",
]

HOST_RUNS_DIR_ENV = "HOST_RUNS_DIR"


@dataclass
class WorkerSpec:
    """Everything one island worker needs to start."""

    island_id: int
    run_id: str
    config_path: Path
    output_dir: Path
    control_host: str
    control_port: int
    broker_url: str


@dataclass
class WorkerStatus:
    """Whether one island's container is still running."""

    island_id: int
    node_id: str
    running: bool


class DockerUnavailable(RuntimeError):
    """Raised when the Docker SDK or daemon is not usable."""


class DockerBackend:
    """Runs each island in its own container."""

    name = "docker"

    def __init__(
        self,
        *,
        image: str,
        run_id: str,
        dockerfile: str = "docker/Dockerfile.worker",
        build_context: str = ".",
        build: bool = True,
        build_network: str | None = None,
        cpus: float = 1.0,
        memory: str = "2g",
        repository_root: Path | None = None,
        logging_output_root: Path | None = None,
    ) -> None:
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

        self.image = image
        self.run_id = run_id
        self.dockerfile = dockerfile
        self.build_context = build_context
        self.build = build
        self.build_network = build_network
        self.cpus = cpus
        self.memory = memory
        self.labels = {
            "app": "archipelago",
            "component": "island-worker",
            "archipelago.run_id": run_id,
        }
        self.repository_root = Path(repository_root or Path(__file__).resolve().parents[2])
        self.logging_output_root = (
            Path(logging_output_root).resolve() if logging_output_root is not None else None
        )

        self._containers: dict[int, Any] = {}
        self._node_ids: dict[int, str] = {}
        self._build_log: list[str] = []
        self.network_name: str = f"archipelago-{run_id[-8:]}"
        self.worker_network: str = self.network_name
        self._created_network: Any | None = None
        self.master_alias: str | None = None
        self._master_container: Any | None = None
        self._master_networks: list[str] = []
        self.build_warning: str | None = None


    def prepare(self) -> None:
        """Make the worker image and the run's network ready, in that order."""
        self._ensure_image()
        self._ensure_network()

    def _ensure_image(self) -> None:
        """Ensure a usable worker image exists, building it when asked to."""
        import docker.errors

        image_exists = True
        try:
            self.client.images.get(self.image)
        except docker.errors.ImageNotFound:
            image_exists = False

        if not self.build:
            if not image_exists:
                raise DockerUnavailable(
                    f"worker image {self.image!r} not found and docker.build is false. "
                    f"Build it with:  docker build --network=host -f {self.dockerfile} "
                    f"-t {self.image} ."
                )
            self._check_image_freshness()
            return

        dockerfile_path = self.repository_root / self.dockerfile
        if not dockerfile_path.exists():
            if image_exists:
                self.build_warning = f"dockerfile not found at {dockerfile_path}; using the existing image"
                return
            raise DockerUnavailable(f"dockerfile not found: {dockerfile_path}")

        try:
            _image, logs = self.client.images.build(
                path=str(self.repository_root / self.build_context),
                dockerfile=str(dockerfile_path.relative_to(self.repository_root)),
                tag=self.image,
                rm=True,
                pull=False,
                network_mode=self.build_network,
            )
            for entry in logs:
                line = entry.get("stream") or entry.get("status")
                if line:
                    self._build_log.append(str(line).rstrip())
        except Exception as exc:
            hint = ""
            message = str(exc)
            if "credential" in message.lower() or "credsStore" in message:
                hint = (
                    " This looks like a Docker credential-helper problem in ~/.docker/config.json "
                    "rather than anything about the image itself."
                )
            elif "name resolution" in message or "Temporary failure" in message:
                hint = " The builder cannot resolve DNS; try docker.build_network: host."
            if image_exists:
                self.build_warning = (
                    f"image rebuild failed ({type(exc).__name__}: {exc}); "
                    f"continuing with the existing {self.image!r}.{hint}"
                )
                self._check_image_freshness(prefix=self.build_warning)
                return
            raise DockerUnavailable(
                f"could not build worker image {self.image!r}: {exc}.{hint} "
                f"Build it by hand with:  docker build --network=host "
                f"-f {self.dockerfile} -t {self.image} ."
            ) from exc


    MASTER_ALIAS = "archipelago-master"

    def _ensure_network(self) -> None:
        """Decide where the islands live, so that they can reach the master and Redis."""
        master = self._own_container()

        if master is not None:
            # Islands also join every network the master is on, so they reach Redis at the
            # address the master uses.
            self._master_networks = sorted(master.attrs["NetworkSettings"]["Networks"])
            self._created_network = self.client.networks.create(
                self.network_name, driver="bridge", labels=self.labels
            )
            self.worker_network = self.network_name
            self._attach_master(master)
        else:
            self.worker_network = "host"
            self.network_name = "host"

    def _attach_master(self, container: Any) -> None:
        """Join the islands' network, so they can reach us by name."""
        try:
            self.client.networks.get(self.worker_network).connect(
                container.id, aliases=[self.MASTER_ALIAS]
            )
        except Exception as exc:
            raise DockerUnavailable(
                f"the master is running in container {container.id[:12]} but could not join "
                f"the island network {self.worker_network!r}: {exc}. A master container "
                "started with --network host cannot be attached to another network; start it "
                "without that flag, or run the master on the host instead."
            ) from exc
        self._master_container = container
        self.master_alias = self.MASTER_ALIAS

    def _own_container(self) -> Any | None:
        """The container this process is running in, or None when on the host."""
        if not Path("/.dockerenv").exists():
            return None
        try:
            return self.client.containers.get(socket.gethostname())
        except Exception:
            return None

    _IMAGE_SOURCES = (
        "archipelago_logging",
        "algorithms",
        "benchmarks",
        "orchestrator",
        "requirements-worker.txt",
    )

    def _check_image_freshness(self, prefix: str | None = None) -> None:
        """Warn when the worker image predates the source it was built from."""
        import datetime
        import re

        try:
            image = self.client.images.get(self.image)
            created = re.sub(r"\.\d+", "", image.attrs.get("Created", ""))
            image_time = datetime.datetime.fromisoformat(
                created.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            return

        newest = 0.0
        newest_path = ""
        for relative in self._IMAGE_SOURCES:
            root = self.repository_root / relative
            if not root.exists():
                continue
            candidates = root.rglob("*.py") if root.is_dir() else [root]
            for candidate in candidates:
                try:
                    mtime = candidate.stat().st_mtime
                except OSError:
                    continue
                if mtime > newest:
                    newest, newest_path = mtime, str(candidate.relative_to(self.repository_root))

        if newest > image_time:
            age = newest - image_time
            message = (
                f"worker image {self.image!r} was built {age / 60:.1f} minutes before the newest "
                f"source file ({newest_path}); the containers will run stale code. Rebuild with: "
                f"docker build --network=host -f {self.dockerfile} -t {self.image} ."
            )
            self.build_warning = f"{prefix} {message}" if prefix else message

    @staticmethod
    def container_name(run_id: str, island_id: int) -> str:
        """This island's container name, and its ``node_id`` in the log."""
        return f"archipelago-{run_id[-8:]}-island-{island_id:02d}"

    def _host_mount_source(self, path: Path) -> str:
        """Translate one of *our* paths into the host path the daemon must mount."""
        host_root = os.environ.get(HOST_RUNS_DIR_ENV)
        if not host_root or self.logging_output_root is None:
            return str(path)
        try:
            relative = path.resolve().relative_to(self.logging_output_root)
        except ValueError:
            return str(path)
        return str(Path(host_root) / relative)

    def launch(self, spec: WorkerSpec) -> str:
        """Create and start one island's container."""
        output_dir = spec.output_dir.resolve()
        command = [
            "--config",
            str(spec.config_path),
            "--run-id",
            spec.run_id,
            "--island-id",
            str(spec.island_id),
            "--output-dir",
            str(output_dir),
            "--control-host",
            spec.control_host,
            "--control-port",
            str(spec.control_port),
        ]

        container_name = self.container_name(spec.run_id, spec.island_id)
        environment = {
            "PYTHONUNBUFFERED": "1",
            "IN_ARCHIPELAGO_CONTAINER": "1",
            "ISLAND_CONTAINER_NAME": container_name,
            # The resolved config on disk carries a redacted URL; the real one travels here.
            BROKER_URL_ENV: spec.broker_url,
        }

        output_dir.mkdir(parents=True, exist_ok=True)
        volumes = {self._host_mount_source(output_dir): {"bind": str(output_dir), "mode": "rw"}}

        container = self.client.containers.create(
            self.image,
            command=command,
            name=container_name,
            labels={**self.labels, "archipelago.island_id": str(spec.island_id)},
            environment=environment,
            volumes=volumes,
            network=self.worker_network,
            extra_hosts=(
                {} if self.worker_network == "host"
                else {"host.docker.internal": "host-gateway"}
            ),
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

    def statuses(self) -> dict[int, WorkerStatus]:
        """Poll container states."""
        result: dict[int, WorkerStatus] = {}
        for island_id, container in self._containers.items():
            try:
                container.reload()
                running = bool(container.attrs.get("State", {}).get("Running"))
            except Exception:
                running = False
            result[island_id] = WorkerStatus(
                island_id=island_id,
                node_id=self._node_ids.get(island_id, ""),
                running=running,
            )
        return result

    def stop_all(self, grace_seconds: float = 15.0) -> None:
        """Ask every running container to stop, within the grace period."""
        for container in self._containers.values():
            try:
                container.reload()
                if container.attrs.get("State", {}).get("Running"):
                    container.stop(timeout=int(grace_seconds))
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
            "network": self.worker_network,
            "network_created_for_this_run": self._created_network is not None,
            "master_networks": self._master_networks,
            "master_in_container": self.master_alias is not None,
            "labels": self.labels,
            "build_warning": self.build_warning,
        }
