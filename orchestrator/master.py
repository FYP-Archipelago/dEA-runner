"""The master: spins up islands, brokers migration, writes its own records, reports."""

from __future__ import annotations

import hashlib
import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archipelago_logging import EventLogger, TerminationReason, iso_now, new_run_id
from archipelago_logging.version import HARNESS_VERSION

from .backends import DockerBackend, IslandSpec
from .config import RunConfig
from .control_server import ControlServer
from .redis_stream import RedisStreamWriter, ping_broker, stream_key, stream_length
from .topology import build_topology, describe_topology

__all__ = ["Master", "RunSummary", "run"]

_POLL_SECONDS = 0.5


@dataclass
class RunSummary:
    """Everything a caller needs to know about a finished run."""

    run_id: str
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
    error: str | None = None
    """Why the run failed, readable by a person; None unless ``termination_reason`` is error."""


def _failure_text(failure: Exception | None, failed_islands: list[dict[str, Any]]) -> str:
    """The run's error for a person: what the master hit, then why each failed island failed."""
    parts = []
    if failure is not None:
        parts.append(f"run failed: {type(failure).__name__}: {failure}")
    for result in failed_islands:
        reason = result.get("error") or result.get("termination_reason")
        parts.append(f"island {result.get('island_id')} failed: {reason}")
    return "; ".join(parts)


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
        self.topology = build_topology(
            config.migration.topology, config.num_islands, **config.migration.topology_params
        )

        self.writer: RedisStreamWriter | None = None
        self.logger: EventLogger | None = None
        self.server: ControlServer | None = None
        self.backend: DockerBackend | None = None
        self._node_ids: dict[int, str] = {}
        self._run_started = False


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
        # The master is on the host, and so, on host networking, are its islands.
        return self.config.execution.control_host


    def run(self) -> RunSummary:
        """Execute the run and return its summary.

        Whatever happens after ``run_start``, the stream ends the same way: every island has
        said goodbye, died, or been stopped (the barrier), then the closing diagnostics, then
        ``run_end``, and nothing after it.
        """
        started = time.monotonic()
        stream = self._preflight()
        termination = TerminationReason.COMPLETED.value
        failure: Exception | None = None
        try:
            self._start()
            termination = self._supervise(started)
        except Exception as exc:
            failure = exc
            termination = TerminationReason.ERROR.value
        try:
            stopping = failure is not None or termination == TerminationReason.TIME_LIMIT.value
            if stopping and self.backend is not None and self.server is not None:
                self._wind_down()
            island_results = self._collect_results() if self.backend is not None else []
            summary = self._finish(started, termination, island_results, stream, failure)
        finally:
            self._release()
        if failure is not None:
            raise failure
        return summary

    def _preflight(self) -> str:
        """Check Redis and the run's stream before anything starts; open the master's writer."""
        broker = self.config.broker
        stream = stream_key(self.run_id)
        ping_broker(broker.url, broker.connect_timeout_seconds)
        existing = stream_length(broker.url, stream)
        if existing:
            raise FileExistsError(
                f"run_id {self.run_id!r} has already been used: {stream} holds {existing} entries, "
                "and run_start must be the first. Choose a new run_id."
            )
        self.writer = RedisStreamWriter(
            broker.url,
            stream,
            buffer_records=broker.buffer_records,
            stall_seconds=broker.stall_seconds,
        )
        self.logger = EventLogger(self.run_id, "master", self.writer)
        return stream

    def _start(self) -> None:
        """Start the control server, prepare Docker, write ``run_start`` and launch the islands."""
        assert self.logger is not None
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
                    "budget_plan": self.config.budget_plan(),
                    "backend": self.backend.describe(),
                },
            },
            host=_host_facts(),
        )
        self._run_started = True
        # Wait until Redis holds run_start: a broken or full Redis fails the run here, before
        # any island container starts.
        self.logger.flush()

        if build_warning:
            self.logger.diagnostic(build_warning, level="warning")

        advertise = self._advertise_host()
        for island_id in range(self.config.num_islands):
            spec = IslandSpec(
                island_id=island_id,
                run_id=self.run_id,
                config_json=self.config.model_dump_json(),
                control_host=advertise,
                control_port=actual_port,
            )
            node_id = self.backend.launch(spec)
            self._node_ids[island_id] = node_id
            self.logger.diagnostic(
                "island container launched",
                island_id=island_id,
                node_id=node_id,
                backend=self.backend.name,
            )


    def _supervise(self, started: float) -> str:
        """Wait until every island is done, or the wall-clock limit expires."""
        assert self.backend is not None and self.writer is not None and self.logger is not None
        limit = self.config.termination.time_limit_seconds
        deadline = started + limit if limit is not None else None
        while True:
            self.writer.raise_if_failed()
            if not self._islands_still_running():
                return TerminationReason.COMPLETED.value
            if deadline is not None and time.monotonic() >= deadline:
                self.logger.diagnostic(
                    "wall-clock limit reached; asking islands to wind down",
                    level="warning",
                    time_limit_seconds=limit,
                )
                return TerminationReason.TIME_LIMIT.value
            time.sleep(_POLL_SECONDS)

    def _islands_still_running(self) -> list[int]:
        """Islands that have neither said goodbye nor exited."""
        assert self.backend is not None and self.server is not None
        statuses = self.backend.statuses()
        return [
            island_id
            for island_id, status in statuses.items()
            if status.running and not self.server.islands[island_id].finished
        ]

    def _wind_down(self) -> None:
        """Stop the islands, and wait for each to say goodbye or die: the barrier before ``run_end``.

        Islands are asked to finish their generation (the heartbeat flag, and SIGTERM for any that
        cannot hear it) and get ``shutdown_grace_seconds`` to flush their records and say goodbye.
        An island still silent after that is killed. On the normal path the barrier is already
        met when :meth:`_supervise` returns: every island has said goodbye or exited.
        """
        assert self.backend is not None and self.server is not None
        self.server.stop_requested.set()
        self.backend.signal_all("SIGTERM")
        grace = self.config.execution.shutdown_grace_seconds
        gives_up_at = time.monotonic() + grace
        while running := self._islands_still_running():
            if time.monotonic() >= gives_up_at:
                self._note(
                    f"islands {running} did not say goodbye within {grace:g} s of being asked "
                    "to stop; killing them",
                    level="error",
                    islands=running,
                )
                self.backend.kill(running)
                return
            time.sleep(_POLL_SECONDS)

    def _collect_results(self) -> list[dict[str, Any]]:
        """Each island's result, as it arrived in its goodbye.

        An island that never said goodbye crashed before it could; its container's exit code
        and last output, read before the container is removed, say why.
        """
        assert self.server is not None and self.backend is not None
        results: list[dict[str, Any]] = []
        for island_id in range(self.config.num_islands):
            reported = self.server.islands[island_id].result
            if reported is not None:
                results.append(reported)
                continue
            report = self.backend.exit_report(island_id)
            error = (
                f"island {island_id} exited without reporting a result "
                f"(container exit code {report['exit_code']})"
            )
            self._note(error, level="error", island_id=island_id, log_tail=report["log_tail"])
            results.append(
                {
                    "island_id": island_id,
                    "termination_reason": TerminationReason.ERROR.value,
                    "error": f"{error}; last output: {report['log_tail'][-500:]}",
                }
            )
        return results

    def _finish(
        self,
        started: float,
        termination: str,
        island_results: list[dict[str, Any]],
        stream: str,
        failure: Exception | None,
    ) -> RunSummary:
        """Write the closing diagnostics and ``run_end``, then close the writer."""
        elapsed = time.monotonic() - started
        failed_islands = [
            result
            for result in island_results
            if result.get("error") is not None
            or result.get("termination_reason") == TerminationReason.ERROR.value
        ]
        failed = len(failed_islands)
        completed = len(island_results) - failed
        if failed and termination != TerminationReason.ERROR.value:
            termination = TerminationReason.ERROR.value
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

        if self.server is not None:
            self.server.stop()  # no island is left to talk to, and nothing may follow run_end
        # Closing records only follow a run_start: a stream must never begin with anything else.
        if self._run_started and self.server is not None and not self.writer.failed:
            self._warn_if_no_island_ever_connected()
            if termination == TerminationReason.ERROR.value:
                self.logger.diagnostic(
                    _failure_text(failure, failed_islands), level="error", islands_failed=failed
                )
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
        if not self.writer.failed:
            self.writer.close()

        return RunSummary(
            run_id=self.run_id,
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
            error=_failure_text(failure, failed_islands) if failed or failure else None,
        )

    def _release(self) -> None:
        """Stop the control server and remove the run's containers and network."""
        if self.server is not None:
            self.server.stop()
        if self.backend is not None:
            self.backend.cleanup()

    def _note(self, message: str, **context: Any) -> None:
        """A diagnostic, unless the master's writer has already failed."""
        if self.logger is not None and self.writer is not None and not self.writer.failed:
            self.logger.diagnostic(message, **context)

    def _warn_if_no_island_ever_connected(self) -> None:
        """Say so when the islands ran but never reached the master."""
        assert self.logger is not None and self.server is not None
        if any(status.ready for status in self.server.islands.values()):
            return
        hint = (
            "islands reach the master over the run's private Docker network, as "
            f"{getattr(self.backend, 'master_alias', None)!r}"
            if getattr(self.backend, "master_alias", None)
            else "islands share the host's network and reach a host master at "
            "execution.control_host; check advertise_host and any firewall on that address"
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
