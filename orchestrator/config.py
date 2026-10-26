"""Run configuration: one YAML file, validated at startup, overridable everywhere."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from archipelago_logging.genome import DEFAULT_PRECISION_DECIMALS, GenomeLogMode
from archipelago_logging.ids import check_run_id
from archipelago_logging.version import HARNESS_VERSION, SCHEMA_VERSION

__all__ = [
    "MigrationConfig",
    "LoggingConfig",
    "ExecutionConfig",
    "DockerConfig",
    "RunConfig",
    "load_config",
    "apply_overrides",
    "ENV_PREFIX",
]

ENV_PREFIX = "ARCHIPELAGO_"


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
    """What gets logged, how much of it, and where it goes."""

    output_dir: str = Field(default="runs", description="Parent directory; each run gets a subdirectory.")
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
    flush_interval_seconds: float = Field(default=2.0, ge=0.0)
    network_queue_size: int = Field(
        default=100_000,
        ge=1000,
        description="Per-island outbound log queue. On overflow, records are dropped and counted rather than back-pressuring the evolutionary loop.",
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
    time_limit_seconds: float | None = Field(
        default=None, gt=0.0, description="Wall-clock limit for the whole run."
    )


class ExecutionConfig(_Base):
    """How the master and its island containers talk to each other."""

    control_host: str = Field(
        default="127.0.0.1", description="Address the master binds for control and log collection."
    )
    control_port: int = Field(
        default=0, ge=0, le=65535, description="0 selects an ephemeral port, reported in the log."
    )
    advertise_host: str | None = Field(
        default=None,
        description="Address workers use to reach the master. Defaults to the master's alias on the run's network when the master is itself a container, to control_host when the islands share the host network, and to the Docker host gateway otherwise.",
    )
    connect_timeout_seconds: float = Field(
        default=60.0,
        gt=0.0,
        description="How long a worker retries the master's control socket at startup before giving up and running standalone (no migration). Containers can take several seconds to start, so this needs headroom.",
    )
    heartbeat_interval_seconds: float = Field(
        default=2.0,
        gt=0.0,
        description="How often an island reports its progress to the master. Carries the archipelago-wide evaluation total, and brings back the master's 'wind down' flag; it is not a liveness probe.",
    )
    shutdown_grace_seconds: float = Field(
        default=20.0,
        gt=0.0,
        description="How long a worker gets to finish its generation and flush its logs after being asked to stop, before it is killed.",
    )


class DockerConfig(_Base):
    """Worker container image and resource limits."""

    image: str = Field(default="archipelago-worker:latest")
    build: bool = Field(
        default=True, description="Build the image from `dockerfile` if it is missing or out of date."
    )
    dockerfile: str = Field(default="docker/Dockerfile.worker")
    build_context: str = Field(default=".")
    build_network: str | None = Field(
        default=None,
        description="Network mode for the image build. Leave unset for the daemon default.",
    )
    cpus: float = Field(default=1.0, gt=0.0, description="CPU quota.")
    memory: str = Field(default="2g", description="Memory limit per worker container, e.g. '2g'.")


class RunConfig(_Base):
    """The complete, validated description of one dEA run."""

    run_id: str | None = Field(
        default=None,
        description="Supplied by the caller, or minted when absent. Must look like run-YYYYMMDDTHHMMSSZ-xxxxxxxx; container and network names are built from its last 8 hex digits.",
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
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    docker: DockerConfig = Field(default_factory=DockerConfig)


    @field_validator("run_id")
    @classmethod
    def _run_id_shape(cls, run_id: str | None) -> str | None:
        """A supplied run id must have the shape the harness mints."""
        return run_id if run_id is None else check_run_id(run_id)

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
        """How the evaluation budget resolves. Logged in ``run_start`` and shown by the CLI."""
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

    def output_path(self, run_id: str) -> Path:
        """Directory this run's artefacts are written to."""
        return Path(self.logging.output_dir) / run_id

    def resolved(self) -> dict[str, Any]:
        """JSON-safe dict of the fully resolved config, for ``run_start.config``."""
        payload = self.model_dump(mode="json")
        payload["_meta"] = {
            "schema_version": SCHEMA_VERSION,
            "harness_version": HARNESS_VERSION,
        }
        return payload

    def write_resolved(self, path: Path) -> Path:
        """Write ``resolved_config.yaml`` beside the logs."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(self.resolved(), sort_keys=False), encoding="utf-8")
        return path


def _coerce(text: str) -> Any:
    """Parse an override value as JSON, falling back to the raw string."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _set_path(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cursor = target
    for part in parts[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[parts[-1]] = value


def _env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Collect ``ARCHIPELAGO_*`` variables into a nested override dict."""
    environ = os.environ if environ is None else environ
    known = set(RunConfig.model_fields)
    overrides: dict[str, Any] = {}
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        dotted = key[len(ENV_PREFIX) :].lower().replace("__", ".")
        head = dotted.split(".", 1)[0]
        if head not in known:
            raise ValueError(
                f"environment variable {key} does not name a run-config field "
                f"({head!r} is not one of {sorted(known)}). The {ENV_PREFIX} prefix is "
                "reserved for configuration overrides."
            )
        _set_path(overrides, dotted, _coerce(raw))
    return overrides


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def apply_overrides(base: dict[str, Any], overrides: list[str] | None) -> dict[str, Any]:
    """Apply ``key.path=value`` strings from ``--set`` flags."""
    result = dict(base)
    for item in overrides or []:
        if "=" not in item:
            raise ValueError(f"malformed override {item!r}; expected key.path=value")
        dotted, _, raw = item.partition("=")
        _set_path(result, dotted.strip(), _coerce(raw.strip()))
    return result


def load_config(
    path: str | Path | None = None,
    *,
    overrides: list[str] | None = None,
    environ: dict[str, str] | None = None,
) -> RunConfig:
    """Load, merge and validate a run configuration."""
    document: dict[str, Any] = {}
    if path is not None:
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"config file not found: {config_path}")
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_path}: top level of a run config must be a mapping")
        loaded.pop("_meta", None)
        document = loaded

    document = _deep_merge(document, _env_overrides(environ))
    document = apply_overrides(document, overrides)
    return RunConfig(**document)
