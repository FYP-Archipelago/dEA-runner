"""The island loop: one subpopulation, one algorithm, one log stream."""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from algorithms.base import Algorithm, Individual, IslandContext
from algorithms.registry import create_algorithm
from archipelago_logging import (
    EVALUATION_CSV_HEADER,
    CsvFileSink,
    EventLogger,
    GenomePolicy,
    IndividualIdMinter,
    JsonlFileSink,
    NullSink,
    MultiSink,
    NetworkSink,
    Sink,
    TerminationReason,
    wall_now,
)
from benchmarks.registry import create_problem

from .config import RunConfig
from .control_client import ControlClient
from .migration import choose_replacements, select_migrants
from .protocol import Role
from .topology import build_topology

__all__ = ["IslandRunner", "IslandResult", "build_island_sinks", "resolve_node_id"]


@dataclass
class IslandResult:
    """What the island loop returns to whoever launched it."""

    island_id: int
    termination_reason: str
    generations_completed: int
    evaluations_total: int
    best_fitness: float | None
    best_genome_hash: str | None
    best_individual_id: str | None
    best_genome: Any | None
    migrations_sent: int
    migrations_received: int
    migrants_sent: int
    migrants_received: int
    wallclock_seconds: float
    generations_since_improvement: int = 0
    algorithm_summary: dict[str, Any] = field(default_factory=dict)
    control_channel: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def resolve_node_id(island_id: int) -> str:
    """Identify the physical node this island runs on."""
    name = os.environ.get("ISLAND_CONTAINER_NAME")
    if name:
        return name
    hostname = socket.gethostname()
    if os.path.exists("/.dockerenv") or os.environ.get("IN_ARCHIPELAGO_CONTAINER"):
        return hostname
    return f"{hostname}:{os.getpid()}"


def build_island_sinks(
    config: RunConfig,
    run_id: str,
    island_id: int,
    output_dir: Path,
    control_address: tuple[str, int] | None,
) -> tuple[Sink, Sink]:
    """Build this island's two sink fan-outs: its own shard files, and the stream to the master."""
    import json

    event_components: list[Sink] = [
        JsonlFileSink(
            output_dir / "islands" / f"island-{island_id:02d}.jsonl",
            flush_interval_seconds=config.logging.flush_interval_seconds,
        )
    ]
    evaluation_components: list[Sink] = []
    if config.logging.log_evaluations:
        evaluation_components.append(
            CsvFileSink(
                output_dir / "islands" / f"island-{island_id:02d}.evaluations.csv",
                header=EVALUATION_CSV_HEADER,
                flush_interval_seconds=config.logging.flush_interval_seconds,
            )
        )

    if control_address is not None:
        for role, targets in (
            (Role.LOG, event_components),
            (Role.EVALUATIONS, evaluation_components if config.logging.log_evaluations else None),
        ):
            if targets is None:
                continue
            handshake = (
                json.dumps({"role": role, "run_id": run_id, "island_id": island_id}) + "\n"
            )
            targets.append(
                NetworkSink(
                    control_address[0],
                    control_address[1],
                    queue_size=config.logging.network_queue_size,
                    handshake=handshake,
                )
            )

    evaluation_sink: Sink = (
        MultiSink(evaluation_components) if evaluation_components else NullSink()
    )
    return MultiSink(event_components), evaluation_sink


class IslandRunner:
    """Runs one island to completion."""

    def __init__(
        self,
        config: RunConfig,
        island_id: int,
        run_id: str,
        *,
        output_dir: Path,
        control_address: tuple[str, int] | None = None,
        sink: Sink | None = None,
        evaluation_sink: Sink | None = None,
        container_id: str | None = None,
    ) -> None:
        self.config = config
        self.island_id = island_id
        self.run_id = run_id
        self.output_dir = Path(output_dir)
        self.control_address = control_address
        self.node_id = resolve_node_id(island_id)
        if container_id is None and (
            os.path.exists("/.dockerenv") or os.environ.get("IN_ARCHIPELAGO_CONTAINER")
        ):
            container_id = os.environ.get("ISLAND_CONTAINER_NAME") or socket.gethostname()
        self.container_id = container_id

        if sink is not None:
            self.sink = sink
            self.evaluation_sink: Sink | None = evaluation_sink
            self._owns_sink = False
        else:
            self.sink, self.evaluation_sink = build_island_sinks(
                config, run_id, island_id, self.output_dir, control_address
            )
            self._owns_sink = True

        self.logger = EventLogger(
            run_id,
            f"island:{island_id}",
            self.sink,
            evaluation_sink=self.evaluation_sink,
            island_id=island_id,
            node_id=self.node_id,
            validate=config.logging.validate_events,
        )

        self.control: ControlClient | None = None
        if control_address is not None:
            self.control = ControlClient(
                control_address[0],
                control_address[1],
                run_id=run_id,
                island_id=island_id,
                node_id=self.node_id,
                connect_timeout=config.execution.connect_timeout_seconds,
            )

        self.island_seed = int.from_bytes(os.urandom(8), "little")
        self.rng = np.random.default_rng(self.island_seed)

        self.problem = create_problem(config.benchmark, config.benchmark_params)
        self.genome_policy = GenomePolicy(
            mode=config.logging.genome_mode,
            precision_decimals=config.logging.genome_precision_decimals,
            sample_every=config.logging.genome_sample_every,
        )
        self.population_size = config.resolved_population_size()
        self.budget = config.budget_per_island()
        self._evaluations_per_generation = config.evaluations_per_generation()

        self.ctx = IslandContext(
            island_id=island_id,
            problem=self.problem,
            rng=self.rng,
            logger=self.logger,
            genome_policy=self.genome_policy,
            genome_encoding=None,
            id_minter=IndividualIdMinter(island_id),
            log_evaluations=config.logging.log_evaluations,
            max_evaluations=self.budget,
        )

        algorithm_params = dict(config.algorithm_params)
        if config.algorithm.lower() == "pso":
            algorithm_params.setdefault("total_generations", config.projected_generations())
        self.algorithm: Algorithm = create_algorithm(config.algorithm, self.ctx, algorithm_params)

        topology = build_topology(
            config.migration.topology, config.num_islands, **config.migration.topology_params
        )
        self.neighbours_out = topology.get(island_id, [])
        self.neighbours_in = [
            source for source, targets in topology.items() if island_id in targets
        ]

        self.phase_offset = (
            int(self.rng.integers(0, config.migration.phase_jitter + 1))
            if config.migration.phase_jitter > 0
            else 0
        )

        self.clock_offset_ns = 0
        self.migrations_sent = 0
        self.migrations_received = 0
        self.migrants_sent = 0
        self.migrants_received = 0

        self._generations_since_improvement = 0
        self._last_improvement_fitness = self.problem.worst_possible()


    def _start_island(self) -> None:
        if self.control is not None and self.control.connect():
            offset = self.control.measure_offset()
            self.clock_offset_ns = offset.offset_ns
            self.control.ready(self.clock_offset_ns)
            rtt_ns: int | None = offset.rtt_ns
        else:
            rtt_ns = None

        self.logger.island_start(
            algorithm=self.config.algorithm,
            algorithm_params=self.algorithm.describe(),
            benchmark=self.config.benchmark,
            benchmark_params=self.problem.describe(),
            genome_encoding=self.algorithm.genome_encoding.value,
            population_size=self.population_size,
            island_seed=self.island_seed,
            hostname=socket.gethostname(),
            pid=os.getpid(),
            container_id=self.container_id,
            neighbours_out=self.neighbours_out,
            neighbours_in=self.neighbours_in,
            clock_offset_ns=self.clock_offset_ns if self.control is not None else None,
            clock_offset_rtt_ns=rtt_ns,
            generation=0,
            evaluation_budget=self.budget,
        )

    def _finish(self) -> None:
        if self.control is not None:
            self.control.bye(generation=self.ctx.generation, evaluations=self.ctx.evaluations)
            self.control.close()
        self.logger.flush()
        if self._owns_sink:
            self.logger.close()


    def _migration_active(self) -> bool:
        """Whether this island participates in migration at all."""
        if not self.config.migration.enabled or self.control is None:
            return False
        return bool(self.neighbours_out or self.neighbours_in)

    def _should_send(self, generation: int) -> bool:
        """Send on the configured interval, offset by this island's phase jitter."""
        if not self._migration_active() or generation < 1 or not self.neighbours_out:
            return False
        return ((generation + self.phase_offset) % self.config.migration.interval) == 0

    def _should_poll(self, generation: int) -> bool:
        """Collect arrivals every generation by default; see MigrationConfig."""
        if not self._migration_active() or generation < 1:
            return False
        if not self.neighbours_in:
            return False
        if self.config.migration.poll_every_generation:
            return True
        return ((generation + self.phase_offset) % self.config.migration.interval) == 0

    def _send_migrants(self, population: list[Individual], generation: int) -> int:
        """Choose emigrants, dispatch them, and emit one ``migration_send``."""
        assert self.control is not None
        policy = self.config.migration

        indices = select_migrants(
            policy.selection,
            population,
            policy.num_migrants,
            self.rng,
            self.problem,
            **policy.selection_params,
        )
        if not indices:
            return 0

        emigrants = [population[index] for index in indices]
        payloads = [self.algorithm.serialize_migrant(individual) for individual in emigrants]
        individual_ids = [individual.individual_id for individual in emigrants]
        fitnesses = [float(individual.fitness) for individual in emigrants]
        genome_hashes = [
            self.genome_policy.encode(
                self.algorithm.genome_encoding, self.algorithm.genome_for_log(individual)
            ).genome_hash
            for individual in emigrants
        ]

        source_wall = wall_now()
        dispatched = self.control.migrate(
            migrants=payloads,
            individual_ids=individual_ids,
            fitnesses=fitnesses,
            genome_hashes=genome_hashes,
            source_generation=generation,
            source_wall=source_wall,
        )

        import json

        payload_bytes = len(json.dumps(payloads).encode("utf-8"))
        for record in dispatched:
            self.logger.migration_send(
                generation=generation,
                migration_id=record["migration_id"],
                source_island=self.island_id,
                dest_island=int(record["dest_island"]),
                source_generation=generation,
                migrant_individual_ids=individual_ids,
                migrant_genome_hashes=genome_hashes,
                migrant_fitnesses=fitnesses,
                num_migrants=len(emigrants),
                topology=policy.topology,
                selection_policy=policy.selection,
                migration_interval=policy.interval,
                payload_bytes=payload_bytes,
            )
            self.migrations_sent += 1
            self.migrants_sent += len(emigrants)

        if not policy.copy_migrants:
            for index in sorted(indices, reverse=True):
                population.pop(index)

        return len(dispatched)

    def _collect_migrants(self, population: list[Individual], generation: int) -> int:
        """Collect waiting transfers, integrate them, and emit ``migration_arrive``."""
        assert self.control is not None
        policy = self.config.migration
        deliveries = self.control.poll(generation)
        received = 0

        for delivery in deliveries:
            incoming: list[Individual] = []
            for payload, origin_id in zip(
                delivery.get("migrants") or [], delivery.get("individual_ids") or []
            ):
                individual = self.algorithm.deserialize_migrant(payload)
                individual.origin_island = int(delivery["source_island"])
                individual.origin_individual_id = origin_id
                incoming.append(individual)

            if not incoming:
                continue

            incoming = self.ctx.evaluate_all(incoming)

            victims = choose_replacements(
                policy.replacement,
                population,
                incoming,
                self.rng,
                self.problem,
                **policy.replacement_params,
            )
            replaced_ids = [population[index].individual_id for index in victims]
            for position, index in enumerate(victims):
                population[index] = incoming[position]
            for individual in incoming[len(victims) :]:
                population.append(individual)

            latency = None
            dispatched_at = delivery.get("dispatched_master_wall")
            if dispatched_at is not None:
                latency = (wall_now() + self.clock_offset_ns / 1e9) - float(dispatched_at)

            self.logger.migration_arrive(
                generation=generation,
                migration_id=delivery["migration_id"],
                source_island=int(delivery["source_island"]),
                dest_island=self.island_id,
                source_generation=int(delivery["source_generation"]),
                dest_generation=generation,
                origin_individual_ids=list(delivery.get("individual_ids") or []),
                arrived_individual_ids=[individual.individual_id for individual in incoming],
                replaced_individual_ids=replaced_ids,
                migrant_fitnesses=[float(individual.fitness) for individual in incoming],
                replacement_policy=policy.replacement,
                accepted=True,
                latency_seconds=latency,
                generational_drift=generation - int(delivery["source_generation"]),
            )
            self.migrations_received += 1
            self.migrants_received += len(incoming)
            received += len(incoming)

        return received


    def _emit_generation_end(
        self,
        population: list[Individual],
        generation: int,
        elapsed: float,
        migrants_in: int,
        migrants_out: int,
    ) -> None:
        fitnesses = np.fromiter(
            (individual.fitness for individual in population), dtype=float, count=len(population)
        )
        finite = fitnesses[np.isfinite(fitnesses)]
        if finite.size == 0:
            finite = np.array([float("nan")])

        picker = np.argmax if self.problem.maximising else np.argmin
        best_index = int(picker(fitnesses))
        best = population[best_index]

        diversity, metric, sample_size = self.algorithm.diversity(population)
        hashes = {
            self.genome_policy.encode(
                self.algorithm.genome_encoding, self.algorithm.genome_for_log(individual)
            ).genome_hash
            for individual in population
        }

        self.logger.generation_end(
            generation=generation,
            best_fitness=float(np.max(finite) if self.problem.maximising else np.min(finite)),
            mean_fitness=float(np.mean(finite)),
            worst_fitness=float(np.min(finite) if self.problem.maximising else np.max(finite)),
            median_fitness=float(np.median(finite)),
            std_fitness=float(np.std(finite)),
            maximising=self.problem.maximising,
            best_individual_id=best.individual_id,
            best_genome_hash=self.genome_policy.encode(
                self.algorithm.genome_encoding, self.algorithm.genome_for_log(best)
            ).genome_hash,
            best_so_far_fitness=float(self.ctx.best_fitness),
            generations_since_improvement=self._generations_since_improvement,
            diversity=diversity,
            diversity_metric=metric,
            diversity_sample_size=sample_size,
            unique_genome_hashes=len(hashes),
            population_size=len(population),
            evaluations_this_generation=self.ctx.evaluations_this_generation,
            evaluations_total=self.ctx.evaluations,
            wallclock_seconds=elapsed,
            migrants_received_this_generation=migrants_in,
            migrants_sent_this_generation=migrants_out,
        )

    def _update_stagnation(self) -> None:
        """Advance or reset the no-improvement counter for this generation."""
        tolerance = self.config.termination.stagnation_tolerance
        current = self.ctx.best_fitness
        baseline = self._last_improvement_fitness

        if self.problem.maximising:
            improved = current > baseline + tolerance
        else:
            improved = current < baseline - tolerance

        if improved:
            self._last_improvement_fitness = current
            self._generations_since_improvement = 0
        else:
            self._generations_since_improvement += 1

    def _can_afford_another_generation(self) -> bool:
        """Whether the island's remaining budget covers a whole further generation."""
        reserve = self._evaluations_per_generation
        if self.config.migration.enabled and self.neighbours_in:
            reserve += self.config.migration.num_migrants * len(self.neighbours_in)
        return self.ctx.evaluations + reserve <= self.budget

    def _stagnated(self) -> bool:
        limit = self.config.termination.stagnation_generations
        return limit > 0 and self._generations_since_improvement >= limit

    def _reached_target(self) -> bool:
        target = self.config.termination.target_fitness
        if target is None:
            return False
        best = self.ctx.best_individual
        value = self.ctx.best_fitness if best is None or best.objective is None else best.objective
        return self.problem.is_better(value, target) or value == target


    def run(self) -> IslandResult:
        """Run this island to completion and return its summary."""
        started = time.monotonic()
        termination = TerminationReason.MAX_GENERATIONS.value
        error: str | None = None
        population: list[Individual] = []
        limit = self.config.termination.time_limit_seconds
        deadline = started + limit if limit is not None else None

        try:
            self._start_island()
            self.ctx.generation = 0
            population = self.algorithm.initialize(self.population_size)
            self._update_stagnation()
            self._emit_generation_end(population, 0, time.monotonic() - started, 0, 0)

            last_heartbeat = time.monotonic()
            for generation in range(1, self.config.termination.max_generations + 1):
                if not self._can_afford_another_generation():
                    termination = TerminationReason.MAX_EVALUATIONS.value
                    break

                generation_started = time.monotonic()
                self.ctx.generation = generation
                self.ctx.evaluations_this_generation = 0

                population = self.algorithm.step(population)
                self._update_stagnation()

                migrants_out = migrants_in = 0
                if self._should_send(generation):
                    migrants_out = self._send_migrants(population, generation)
                if self._should_poll(generation):
                    migrants_in = self._collect_migrants(population, generation)

                self._emit_generation_end(
                    population,
                    generation,
                    time.monotonic() - generation_started,
                    migrants_in,
                    migrants_out,
                )

                now = time.monotonic()
                if (
                    self.control is not None
                    and now - last_heartbeat >= self.config.execution.heartbeat_interval_seconds
                ):
                    last_heartbeat = now
                    if self.control.heartbeat(
                        generation=generation,
                        evaluations=self.ctx.evaluations,
                        best_fitness=(
                            None if not np.isfinite(self.ctx.best_fitness) else float(self.ctx.best_fitness)
                        ),
                    ):
                        termination = TerminationReason.ABORTED.value
                        self.logger.diagnostic("master requested shutdown", level="warning")
                        break

                if self.ctx.budget_exhausted():
                    termination = TerminationReason.MAX_EVALUATIONS.value
                    break
                if self._stagnated():
                    termination = TerminationReason.STAGNATION.value
                    self.logger.diagnostic(
                        "island stopped: best-so-far has not improved",
                        generations_since_improvement=self._generations_since_improvement,
                        stagnation_generations=self.config.termination.stagnation_generations,
                        best_fitness=float(self.ctx.best_fitness),
                    )
                    break
                if self._reached_target():
                    termination = TerminationReason.TARGET_FITNESS_REACHED.value
                    break
                if deadline is not None and now >= deadline:
                    termination = TerminationReason.TIME_LIMIT.value
                    break
            else:
                termination = TerminationReason.MAX_GENERATIONS.value

        except Exception as exc:
            termination = TerminationReason.ERROR.value
            error = f"{type(exc).__name__}: {exc}"
            self.logger.diagnostic(f"island failed: {error}", level="error")

        summary = self._algorithm_summary(population)
        best = self.ctx.best_individual
        best_hash = (
            self.genome_policy.encode(
                self.algorithm.genome_encoding, self.algorithm.genome_for_log(best)
            ).genome_hash
            if best is not None
            else None
        )
        control_channel = {
            "control_available": bool(self.control and self.control.available),
            "failed_control_calls": self.control.failed_calls if self.control else 0,
            "last_control_error": self.control.last_error if self.control else None,
            "sink_stats": (
                self.sink.describe() if isinstance(self.sink, MultiSink) else {}
            ),
        }

        elapsed = time.monotonic() - started
        self.logger.island_end(
            generation=self.ctx.generation,
            termination_reason=termination,
            generations_completed=self.ctx.generation,
            evaluations_total=self.ctx.evaluations,
            best_fitness=None if best is None else float(best.fitness),
            best_genome_hash=best_hash,
            best_individual_id=None if best is None else best.individual_id,
            best_genome=None if best is None else self.algorithm.best_genome_full(best),
            migrations_sent=self.migrations_sent,
            migrations_received=self.migrations_received,
            migrants_sent=self.migrants_sent,
            migrants_received=self.migrants_received,
            wallclock_seconds=elapsed,
            generations_since_improvement=self._generations_since_improvement,
            algorithm_summary=summary,
            control_channel=control_channel,
        )
        self._finish()

        return IslandResult(
            island_id=self.island_id,
            termination_reason=termination,
            generations_completed=self.ctx.generation,
            evaluations_total=self.ctx.evaluations,
            best_fitness=None if best is None else float(best.fitness),
            best_genome_hash=best_hash,
            best_individual_id=None if best is None else best.individual_id,
            best_genome=None if best is None else self.algorithm.best_genome_full(best),
            migrations_sent=self.migrations_sent,
            migrations_received=self.migrations_received,
            migrants_sent=self.migrants_sent,
            migrants_received=self.migrants_received,
            wallclock_seconds=elapsed,
            generations_since_improvement=self._generations_since_improvement,
            algorithm_summary=summary,
            control_channel=control_channel,
            error=error,
        )

    def _algorithm_summary(self, population: list[Individual]) -> dict[str, Any]:
        """Per-algorithm end-of-run facts, where the algorithm offers any."""
        summary: dict[str, Any] = {}
        statistics = getattr(self.algorithm, "population_statistics", None)
        if callable(statistics) and population:
            try:
                summary.update(statistics(population))
            except Exception:
                pass

        evaluate_test = getattr(self.problem, "evaluate_test", None)
        best = self.ctx.best_individual
        if callable(evaluate_test) and best is not None:
            try:
                value = evaluate_test(best.genome)
                if value is not None:
                    summary["test_error"] = float(value)
            except Exception:
                pass
        return summary
