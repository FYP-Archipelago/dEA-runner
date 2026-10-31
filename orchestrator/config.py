"""A run's configuration: what its preset and the backend's overrides decide.

How the Runner is deployed (Redis, images, limits, timeouts) is not part of it; that is
:class:`~orchestrator.settings.ServiceSettings`, read from the environment.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from archipelago_logging.genome import DEFAULT_PRECISION_DECIMALS, GenomeLogMode
from archipelago_logging.ids import check_run_id
from archipelago_logging.version import HARNESS_VERSION, SCHEMA_VERSION

__all__ = [
    "MigrationConfig",
    "LoggingConfig",
    "TerminationConfig",
    "RunConfig",
    "RUN_CONFIG_ENV",
    "ISLAND_CONFIG_ENV",
]

RUN_CONFIG_ENV = "RUN_CONFIG"
"""Carries the run's config, as JSON, into the run's master container."""
ISLAND_CONFIG_ENV = "ISLAND_CONFIG"
"""Carries the run's config, as JSON, into each island container."""


class _Base(BaseModel):
    """Shared model settings: reject unknown keys, validate on assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class MigrationConfig(_Base):
    """The migration policy -- the mechanism the whole platform exists to explain."""

    enabled: bool = Field(default=True, description="Set false for a no-migration control run.")
    topology: str = Field(
        default="ring",
        description="Neighbourhood structure: ring, bidirectional_ring, fully_connected, star, random, torus, or lattice. See orchestrator/topology.py.",
    )
    topology_params: dict[str, Any] = Field(
        default_factory=dict, description="Topology-specific options, e.g. {'degree': 2} for random."
    )
    interval: int = Field(
        default=10,
        ge=1,
        description="Generations between migration attempts on an island. The single most consequential migration parameter: too short and the islands become one population, too long and they never share anything.",
    )
    num_migrants: int = Field(default=5, ge=1, description="Individuals sent.")
    selection: str = Field(
        default="best",
        description="How emigrants are chosen: best, worst, random, tournament, roulette, or diverse.",
    )
    replacement: str = Field(
        default="worst",
        description="Who the immigrants displace: worst, random, similar, or none (append).",
    )
    selection_params: dict[str, Any] = Field(default_factory=dict)
    replacement_params: dict[str, Any] = Field(default_factory=dict)
    fanout: Literal["all", "one_random", "round_robin"] = Field(
        default="all",
        description="How many out-neighbours receive each migration event. 'all' (default) sends to every neighbour, which on a ring is exactly the single successor. 'one_random' and 'round_robin' send to one neighbour.",
    )
    copy_migrants: bool = Field(
        default=True,
        description="True: emigrants are copied and the source keeps them (the usual island model). False: they are removed from the source, which shrinks the source population unless replacement adds them back.",
    )
    phase_jitter: int = Field(
        default=0,
        ge=0,
        description="Per-island random offset, in generations, applied to the migration schedule.",
    )
    poll_every_generation: bool = Field(
        default=True,
        description="Check the inbox every generation, while still *sending* only every `interval` generations.",
    )
    max_pending_per_island: int = Field(
        default=1000,
        ge=1,
        description="Cap on undelivered migrants queued for one destination. Bounds master memory when a destination island is slower than the islands sending to it; overflow drops the oldest and is reported as a diagnostic rather than silently.",
    )


class LoggingConfig(_Base):
    """How genomes are logged."""

    genome_mode: GenomeLogMode = Field(
        default=GenomeLogMode.COMPACT,
        description="compact | full. See archipelago_logging.genome. The genome hash is always written regardless of this setting.",
    )
    genome_precision_decimals: int = Field(
        default=DEFAULT_PRECISION_DECIMALS,
        ge=0,
        le=15,
        description="Rounding used to form the location signature of real-valued genomes. Hashes from runs with different precision are not comparable.",
    )


class TerminationConfig(_Base):
    """When an island stops."""

    stagnation_generations: int = Field(
        default=30,
        ge=0,
        description="Stop an island after this many consecutive generations with no improvement in its best-so-far fitness. 0 disables the rule.",
    )
    stagnation_tolerance: float = Field(
        default=1e-10,
        ge=0.0,
        description="Improvement smaller than this does not reset the stagnation counter. Guards against an island creeping forward by floating-point dust forever and never triggering the rule.",
    )
    max_generations: int = Field(
        default=5000,
        ge=1,
        description="Safety cap. Not the intended stopping condition -- the budget and the stagnation rule are -- but a run must terminate even if both are misset.",
    )
    target_fitness: float | None = Field(
        default=None,
        description="Stop an island once its best objective value passes this.",
    )


class RunConfig(_Base):
    """The complete, validated description of one dEA run."""

    run_id: str = Field(
        description="Minted by the Archipelago Backend and used verbatim everywhere. Must look like run-YYYYMMDDTHHMMSSZ-xxxxxxxx; container and network names are built from its last 8 hex digits.",
    )
    description: str = Field(default="", description="Free-text note carried into run_start.")
    seed: int | None = Field(
        default=None,
        ge=0,
        le=4_294_967_295,
        description="Makes the run repeatable: every random stream in it is derived from this one value, and nothing draws OS entropy. With migration off, two runs with the same seed produce identical evaluation records. With migration on they share every island's random stream but not its trajectory, because a migrant joins its destination on whatever generation that island has reached. Null draws fresh entropy.",
    )
    algorithm: str = Field(default="de", description="Registry name: ga, de, pso, gp or es.")
    algorithm_params: dict[str, Any] = Field(default_factory=dict)
    benchmark: str = Field(default="rastrigin", description="Registry name; see benchmarks.registry.")
    benchmark_params: dict[str, Any] = Field(default_factory=dict)

    evaluation_budget: int = Field(
        default=100_000,
        ge=10,
        description="Total objective evaluations across the WHOLE archipelago -- every island, every generation, until termination.",
    )
    population_size: int | None = Field(
        default=None,
        ge=2,
        description="Per-island population. Leave null to derive it from evaluation_budget and target_generations, which is the normal case -- setting both a population and a budget over-determines the run.",
    )
    target_generations: int = Field(
        default=100,
        ge=2,
        description="The generation count the derived population size aims for. A target, not a limit: an island normally stops on stagnation well before it, and the budget stops it if not.",
    )
    num_islands: int = Field(
        default=5,
        ge=4,
        le=6,
        description="Islands, one container each. The project scope fixes this at 4-6.",
    )

    termination: "TerminationConfig" = Field(default_factory=lambda: TerminationConfig())

    migration: MigrationConfig = Field(default_factory=MigrationConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


    @field_validator("run_id")
    @classmethod
    def _run_id_shape(cls, run_id: str) -> str:
        """The run id must have the shape `new_run_id` mints."""
        return check_run_id(run_id)

    @model_validator(mode="after")
    def _validate(self) -> "RunConfig":
        from algorithms.registry import accepted_parameters, algorithm_class, check_compatible
        from benchmarks.registry import (
            _accepted_parameters as accepted_benchmark_parameters,
        )
        from benchmarks.registry import _REGISTRY as BENCHMARK_REGISTRY
        from benchmarks.registry import available_benchmarks, benchmark_domain
        from orchestrator.migration import REPLACEMENT_POLICIES, SELECTION_POLICIES
        from orchestrator.topology import TOPOLOGIES, build_topology

        algorithm_class(self.algorithm)
        if self.benchmark.lower() not in available_benchmarks():
            raise ValueError(
                f"unknown benchmark {self.benchmark!r}; available: {sorted(available_benchmarks())}"
            )
        check_compatible(self.algorithm, benchmark_domain(self.benchmark))

        unknown_algorithm = sorted(set(self.algorithm_params) - accepted_parameters(self.algorithm))
        if unknown_algorithm:
            raise ValueError(
                f"unknown algorithm_params for {self.algorithm!r}: {unknown_algorithm}; "
                f"accepted: {sorted(accepted_parameters(self.algorithm))}"
            )
        accepted_benchmark = accepted_benchmark_parameters(BENCHMARK_REGISTRY[self.benchmark.lower()])
        if accepted_benchmark is not None:
            unknown_benchmark = sorted(set(self.benchmark_params) - accepted_benchmark)
            if unknown_benchmark:
                raise ValueError(
                    f"unknown benchmark_params for {self.benchmark!r}: {unknown_benchmark}; "
                    f"accepted: {sorted(accepted_benchmark)}"
                )

        if self.migration.enabled:
            if self.migration.topology.lower() not in TOPOLOGIES:
                raise ValueError(
                    f"unknown migration topology {self.migration.topology!r}; "
                    f"available: {sorted(TOPOLOGIES)}"
                )
            if self.migration.selection.lower() not in SELECTION_POLICIES:
                raise ValueError(
                    f"unknown migrant selection policy {self.migration.selection!r}; "
                    f"available: {sorted(SELECTION_POLICIES)}"
                )
            if self.migration.replacement.lower() not in REPLACEMENT_POLICIES:
                raise ValueError(
                    f"unknown migrant replacement policy {self.migration.replacement!r}; "
                    f"available: {sorted(REPLACEMENT_POLICIES)}"
                )
            topology = build_topology(
                self.migration.topology, self.num_islands, **self.migration.topology_params
            )
            if self.migration.topology.lower() == "torus" and all(
                len(neighbours) <= 2 for neighbours in topology.values()
            ):
                rows = self.migration.topology_params.get("rows")
                grid = f"{self.num_islands} islands" + (
                    f" in {rows} row{'s' if rows != 1 else ''}" if rows else ""
                )
                raise ValueError(
                    f"a torus of {grid} gives every island only two neighbours, which makes it "
                    "identical to bidirectional_ring, so the log would record a torus that "
                    "never ran. A torus needs at least a 2x3 grid: num_islands: 6, in 2 or 3 "
                    "rows. For 4 or 5 islands, use bidirectional_ring."
                )
            resolved_population = self.resolved_population_size()
            if self.migration.num_migrants >= resolved_population:
                raise ValueError(
                    f"migration.num_migrants={self.migration.num_migrants} must be smaller than "
                    f"the per-island population ({resolved_population}). That population is "
                    f"derived from evaluation_budget={self.evaluation_budget} over "
                    f"{self.num_islands} islands and target_generations="
                    f"{self.target_generations}; raise the budget, lower "
                    f"target_generations, or send fewer migrants."
                )
            if self.migration.interval > self.target_generations:
                raise ValueError(
                    f"migration.interval={self.migration.interval} exceeds "
                    f"target_generations={self.target_generations}, so a run is unlikely to "
                    "migrate even once. Set migration.enabled: false if that is intended."
                )

        return self


    def offspring_multiplier(self) -> float:
        """Evaluations."""
        if self.algorithm.lower() != "es":
            return 1.0
        return float(self.algorithm_params.get("offspring_per_parent", 7))

    def budget_per_island(self) -> int:
        """This island's hard evaluation ceiling: the budget split evenly."""
        return max(1, self.evaluation_budget // self.num_islands)

    def minimum_population_size(self) -> int:
        """Smallest population the chosen algorithm can actually run with."""
        minimum = 4
        if self.algorithm.lower() == "de":
            minimum = 6 if "/2" in str(self.algorithm_params.get("strategy", "rand/1")) else 4
        tournament = self.algorithm_params.get("tournament_size")
        if tournament:
            minimum = max(minimum, int(tournament))
        return max(2, minimum)

    def resolved_population_size(self) -> int:
        """The per-island population actually used."""
        if self.population_size is not None:
            return int(self.population_size)

        denominator = (self.target_generations + 1) * self.offspring_multiplier()
        derived = int(round(self.budget_per_island() / denominator))
        return max(self.minimum_population_size(), derived)

    def evaluations_per_generation(self) -> int:
        """Evaluations one island performs."""
        return int(round(self.resolved_population_size() * self.offspring_multiplier()))

    def projected_generations(self) -> int:
        """Generations an island can afford, if it does not stop early."""
        per_generation = max(1, self.evaluations_per_generation())
        return max(1, (self.budget_per_island() - self.resolved_population_size()) // per_generation)

    def budget_plan(self) -> dict[str, Any]:
        """How the evaluation budget resolves. Logged in ``run_start``."""
        population = self.resolved_population_size()
        per_generation = self.evaluations_per_generation()
        generations = self.projected_generations()
        projected_total = (population + per_generation * generations) * self.num_islands
        return {
            "evaluation_budget": self.evaluation_budget,
            "budget_per_island": self.budget_per_island(),
            "num_islands": self.num_islands,
            "population_size": population,
            "population_size_source": "explicit" if self.population_size else "derived",
            "offspring_multiplier": self.offspring_multiplier(),
            "evaluations_per_generation_per_island": per_generation,
            "target_generations": self.target_generations,
            "projected_generations_if_no_early_stop": generations,
            "projected_total_evaluations": projected_total,
            "max_generations": self.termination.max_generations,
            "stagnation_generations": self.termination.stagnation_generations,
            "note": (
                "Stagnation normally ends a run before the budget does, so the actual total "
                "is typically below the projection."
            ),
        }

    def resolved(self) -> dict[str, Any]:
        """JSON-safe dict of the fully resolved config, for ``run_start.config``."""
        payload = self.model_dump(mode="json")
        payload["_meta"] = {
            "schema_version": SCHEMA_VERSION,
            "harness_version": HARNESS_VERSION,
        }
        return payload
