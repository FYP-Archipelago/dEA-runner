"""The master: spins up islands, brokers migration, writes its own records, reports."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archipelago_logging import EventLogger, TerminationReason, iso_now, new_run_id
from archipelago_logging.version import HARNESS_VERSION, SCHEMA_VERSION

from .backends import DockerBackend, IslandSpec
from .config import RunConfig
from .control_server import ControlServer
from .redis_stream import BrokerError, RedisStreamWriter, ping_broker, stream_key, stream_length
from .topology import build_topology, describe_topology

__all__ = ["Master", "RunSummary", "run"]


@dataclass
class RunSummary:
    """Everything a caller needs to know about a finished run."""

    run_id: str
    output_dir: Path
    stream_key: str
    termination_reason: str
    wallclock_seconds: float
    islands_completed: int
    islands_failed: int
    total_evaluations: int
    total_migration_events: int
    total_migrants: int
    global_best_fitness: float | None
    global_best_island: int | None
    per_island: dict[str, Any] = field(default_factory=dict)
    island_results: list[dict[str, Any]] = field(default_factory=list)


def _host_facts() -> dict[str, Any]:
    """Machine facts recorded once in ``run_start.host``."""
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "hostname": platform.node(),
        "cpu_count": os.cpu_count(),
    }


def _dataset_provenance(config: RunConfig) -> dict[str, Any]:
    """Identify the benchmark instance this run actually used."""
    from benchmarks.registry import create_problem

    provenance: dict[str, Any] = {"benchmark": config.benchmark}
    try:
        problem = create_problem(config.benchmark, config.benchmark_params)
    except Exception as exc:
        provenance["error"] = repr(exc)
        return provenance

    described = problem.describe()
    provenance.update(
        {
            "name": described.get("instance_name", described.get("name")),
            "domain": described.get("domain"),
            "source": described.get("instance_source", "definition"),
            "maximising": described.get("maximising"),
            "known_optimum": described.get("known_optimum"),
        }
    )

    instance_file = config.benchmark_params.get("instance_file") or config.benchmark_params.get(
        "csv_path"
    )
    if instance_file:
        path = Path(instance_file)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        if path.exists():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            provenance["instance_file"] = str(path)
            provenance["instance_sha256"] = digest
            provenance["instance_bytes"] = path.stat().st_size
    return provenance


class Master:
    """Orchestrates one dEA run."""

    def __init__(self, config: RunConfig, *, repository_root: Path | None = None) -> None:
        self.config = config
        self.repository_root = Path(repository_root or Path(__file__).resolve().parents[1])
        self.run_id = config.run_id or new_run_id()
        self.output_dir = config.output_path(self.run_id)
        self.topology = build_topology(
            config.migration.topology, config.num_islands, **config.migration.topology_params
        )

        self.writer: RedisStreamWriter | None = None
        self.logger: EventLogger | None = None
        self.server: ControlServer | None = None
        self.backend: DockerBackend | None = None
        self._node_ids: dict[int, str] = {}


    def _make_backend(self) -> DockerBackend:
        return DockerBackend(
            image=self.config.docker.image,
            run_id=self.run_id,
            dockerfile=self.config.docker.dockerfile,
            build_context=self.config.docker.build_context,
            build=self.config.docker.build,
            build_network=self.config.docker.build_network,
            cpus=self.config.docker.cpus,
            memory=self.config.docker.memory,
            repository_root=self.repository_root,
            logging_output_root=Path(self.config.logging.output_dir),
        )

    def _bind_address(self) -> tuple[str, int]:
        """Where the master listens."""
        host = self.config.execution.control_host
        if host in ("127.0.0.1", "localhost"):
            host = "0.0.0.0"
        return host, self.config.execution.control_port

    def _advertise_host(self) -> str:
        """The address islands use to reach the master."""
        if self.config.execution.advertise_host:
            return self.config.execution.advertise_host
        alias = getattr(self.backend, "master_alias", None)
        if alias:
            return alias
        if getattr(self.backend, "island_network", None) == "host":
            return self.config.execution.control_host
        return "host.docker.internal"


    def run(self) -> RunSummary:
        """Execute the run and return its summary."""
        started = time.monotonic()
        if self.output_dir.exists() and any(self.output_dir.iterdir()):
            raise FileExistsError(
                f"run_id {self.run_id!r} has already been used: {self.output_dir} holds its "
                "output, and this run would be appended to it. Choose a new run_id."
            )
        broker = self.config.broker
        stream = stream_key(self.run_id)
        ping_broker(broker.url, broker.connect_timeout_seconds)
        existing = stream_length(broker.url, stream)
        if existing:
            raise FileExistsError(
                f"run_id {self.run_id!r} has already been used: {stream} holds {existing} entries, "
                "and run_start must be the first. Choose a new run_id."
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "islands").mkdir(parents=True, exist_ok=True)
        self.output_dir.chmod(0o777)
        (self.output_dir / "islands").chmod(0o777)
        config_path = self.config.write_resolved(self.output_dir / "resolved_config.yaml")

        self.writer = RedisStreamWriter(
            broker.url,
            stream,
            buffer_records=broker.buffer_records,
            stall_seconds=broker.stall_seconds,
        )
        self.logger = EventLogger(self.run_id, "master", self.writer)

        termination = TerminationReason.COMPLETED.value
        island_results: list[dict[str, Any]] = []

        try:
            bind_host, bind_port = self._bind_address()
            self.server = ControlServer(
                run_id=self.run_id,
                topology=self.topology,
                logger=self.logger,
                host=bind_host,
                port=bind_port,
                max_pending_per_island=self.config.migration.max_pending_per_island,
                fanout=self.config.migration.fanout,
                evaluation_budget=self.config.evaluation_budget,
                seed=self.config.seed,
            )
            actual_host, actual_port = self.server.start()

            migration_description = {
                **describe_topology(
                    self.config.migration.topology,
                    self.config.num_islands,
                    self.topology,
                    **self.config.migration.topology_params,
                ),
                "enabled": self.config.migration.enabled,
                "interval": self.config.migration.interval,
                "num_migrants": self.config.migration.num_migrants,
                "selection": self.config.migration.selection,
                "replacement": self.config.migration.replacement,
                "fanout": self.config.migration.fanout,
                "copy_migrants": self.config.migration.copy_migrants,
                "phase_jitter": self.config.migration.phase_jitter,
            }

            self.backend = self._make_backend()
            self.backend.prepare()
            build_warning = getattr(self.backend, "build_warning", None)

            self.logger.run_start(
                started_at=iso_now(),
                harness_version=HARNESS_VERSION,
                algorithm=self.config.algorithm,
                benchmark=self.config.benchmark,
                num_islands=self.config.num_islands,
                population_size=self.config.resolved_population_size(),
                evaluation_budget=self.config.evaluation_budget,
                evaluation_budget_per_island=self.config.budget_per_island(),
                target_generations=self.config.target_generations,
                max_generations=self.config.termination.max_generations,
                backend=self.backend.name,
                datasets=_dataset_provenance(self.config),
                migration=migration_description,
                config={
                    **self.config.resolved(),
                    "_runtime": {
                        "control_host": actual_host,
                        "control_port": actual_port,
                        "advertise_host": self._advertise_host(),
                        "output_dir": str(self.output_dir),
                        "resolved_config_path": str(config_path),
                        "budget_plan": self.config.budget_plan(),
                        "backend": self.backend.describe(),
                    },
                },
                host=_host_facts(),
            )

            if build_warning:
                self.logger.diagnostic(build_warning, level="warning")

            advertise = self._advertise_host()
            for island_id in range(self.config.num_islands):
                spec = IslandSpec(
                    island_id=island_id,
                    run_id=self.run_id,
                    config_path=config_path.resolve(),
                    output_dir=self.output_dir.resolve(),
                    control_host=advertise,
                    control_port=actual_port,
                    broker_url=broker.url,
                )
                node_id = self.backend.launch(spec)
                self._node_ids[island_id] = node_id
                self.logger.diagnostic(
                    "island container launched",
                    island_id=island_id,
                    node_id=node_id,
                    backend=self.backend.name,
                )

            termination = self._supervise(started)
            island_results = self._collect_results()

        except Exception as exc:
            termination = TerminationReason.ERROR.value
            if self.logger is not None and not isinstance(exc, BrokerError):
                self.logger.diagnostic(f"run failed: {type(exc).__name__}: {exc}", level="error")
            raise
        finally:
            summary = self._shutdown(started, termination, island_results, stream)

        return summary

    def _supervise(self, started: float) -> str:
        """Wait for the islands to finish, or for the wall-clock limit to expire."""
        assert self.backend is not None and self.server is not None and self.logger is not None

        limit = self.config.termination.time_limit_seconds
        deadline = started + limit if limit is not None else None
        poll_interval = 0.5

        while True:
            time.sleep(poll_interval)
            statuses = self.backend.statuses()

            if not any(status.running for status in statuses.values()):
                time.sleep(1.0)
                finished = sum(
                    1 for status in self.server.islands.values() if status.finished
                )
                return (
                    TerminationReason.COMPLETED.value
                    if finished == self.config.num_islands
                    else TerminationReason.ERROR.value
                )

            if deadline is not None and time.monotonic() >= deadline:
                self.logger.diagnostic(
                    "wall-clock limit reached; asking islands to wind down",
                    level="warning",
                    time_limit_seconds=self.config.termination.time_limit_seconds,
                )
                self.server.stop_requested.set()
                self.backend.stop_all(self.config.execution.shutdown_grace_seconds)
                return TerminationReason.TIME_LIMIT.value

    def _collect_results(self) -> list[dict[str, Any]]:
        """Read each island's result file. Every island writes one, on every path."""
        results: list[dict[str, Any]] = []
        for island_id in range(self.config.num_islands):
            path = self.output_dir / "islands" / f"island-{island_id:02d}.result.json"
            try:
                results.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as exc:
                results.append(
                    {
                        "island_id": island_id,
                        "termination_reason": TerminationReason.ERROR.value,
                        "error": f"no readable result file at {path}: {exc}",
                    }
                )
        return results

    def _shutdown(
        self,
        started: float,
        termination: str,
        island_results: list[dict[str, Any]],
        stream: str,
    ) -> RunSummary:
        """Emit ``run_end`` and flush it, then stop the server and remove the containers."""
        elapsed = time.monotonic() - started

        completed = sum(
            1
            for result in island_results
            if result.get("termination_reason")
            in (
                TerminationReason.COMPLETED.value,
                TerminationReason.MAX_GENERATIONS.value,
                TerminationReason.MAX_EVALUATIONS.value,
                TerminationReason.TARGET_FITNESS_REACHED.value,
                TerminationReason.STAGNATION.value,
                TerminationReason.TIME_LIMIT.value,
                TerminationReason.ABORTED.value,
            )
        )
        failed = len(island_results) - completed
        total_evaluations = sum(int(result.get("evaluations_total") or 0) for result in island_results)

        best_fitness: float | None = None
        best_island: int | None = None
        best_hash: str | None = None
        best_genome: Any | None = None
        maximising = self._maximising()
        for result in island_results:
            value = result.get("best_fitness")
            if value is None:
                continue
            value = float(value)
            better = (
                best_fitness is None
                or (maximising and value > best_fitness)
                or (not maximising and value < best_fitness)
            )
            if better:
                best_fitness = value
                best_island = int(result.get("island_id", -1))
                best_hash = result.get("best_genome_hash")
                best_genome = result.get("best_genome")

        per_island = self.server.summary() if self.server is not None else {}
        for result in island_results:
            key = str(result.get("island_id"))
            if key in per_island:
                per_island[key] = {
                    **per_island[key],
                    "termination_reason": result.get("termination_reason"),
                    "best_fitness": result.get("best_fitness"),
                    "best_genome_hash": result.get("best_genome_hash"),
                    "migrations_sent": result.get("migrations_sent"),
                    "migrations_received": result.get("migrations_received"),
                    "generations_since_improvement": result.get("generations_since_improvement"),
                    "error": result.get("error"),
                    "algorithm_summary": result.get("algorithm_summary"),
                }

        try:
            if self.logger is not None and self.server is not None:
                self._warn_if_no_island_ever_connected()
                self.logger.run_end(
                    ended_at=iso_now(),
                    termination_reason=termination,
                    wallclock_seconds=elapsed,
                    islands_completed=completed,
                    islands_failed=failed,
                    total_evaluations=total_evaluations,
                    total_migration_events=self.server.total_migration_events,
                    total_migrants=self.server.total_migrants,
                    global_best_fitness=best_fitness,
                    global_best_island=best_island,
                    global_best_genome_hash=best_hash,
                    global_best_genome=best_genome,
                    per_island=per_island,
                )
            if self.writer is not None:
                self.writer.close()
        finally:
            if self.backend is not None:
                self.backend.stop_all(self.config.execution.shutdown_grace_seconds)
            if self.server is not None:
                self.server.stop()
            if self.backend is not None:
                self.backend.cleanup()

        summary = RunSummary(
            run_id=self.run_id,
            output_dir=self.output_dir,
            stream_key=stream,
            termination_reason=termination,
            wallclock_seconds=elapsed,
            islands_completed=completed,
            islands_failed=failed,
            total_evaluations=total_evaluations,
            total_migration_events=self.server.total_migration_events if self.server else 0,
            total_migrants=self.server.total_migrants if self.server else 0,
            global_best_fitness=best_fitness,
            global_best_island=best_island,
            per_island=per_island,
            island_results=island_results,
        )
        payload = {
            **{
                key: (str(value) if isinstance(value, Path) else value)
                for key, value in summary.__dict__.items()
            },
            "schema_version": SCHEMA_VERSION,
            "harness_version": HARNESS_VERSION,
        }
        (self.output_dir / "summary.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        return summary

    def _warn_if_no_island_ever_connected(self) -> None:
        """Say so when the islands ran but never reached the master."""
        assert self.logger is not None and self.server is not None
        if any(status.ready for status in self.server.islands.values()):
            return
        hint = (
            "islands reach the master over the run's private Docker network"
            if getattr(self.backend, "master_alias", None)
            else "islands reach a host master through host.docker.internal, which a host "
            "firewall may reject -- on Ubuntu, `sudo ufw allow in on docker0`, or run the "
            "master in a container so the traffic never leaves Docker"
        )
        self.logger.diagnostic(
            "no island ever connected to the master, so no migration took place; "
            f"the islands ran independently. Check the control address: {hint}.",
            level="error",
            advertise_host=self._advertise_host(),
            backend=self.backend.name if self.backend else None,
        )

    def _maximising(self) -> bool:
        """Objective orientation, needed to pick the global best across islands."""
        from benchmarks.registry import create_problem

        try:
            return bool(create_problem(self.config.benchmark, self.config.benchmark_params).maximising)
        except Exception:
            return False


def run(config: RunConfig, *, repository_root: Path | None = None) -> RunSummary:
    """Convenience wrapper: build a :class:`Master` and run it."""
    return Master(config, repository_root=repository_root).run()
