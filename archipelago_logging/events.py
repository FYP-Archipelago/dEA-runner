"""Typed event models for the Archipelago standardized log format (JSON Lines)."""

from __future__ import annotations

import csv
import io
import json
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .version import SCHEMA_VERSION

__all__ = [
    "EventType",
    "Operator",
    "TerminationReason",
    "DiagnosticLevel",
    "BaseEvent",
    "RunStartEvent",
    "IslandStartEvent",
    "EvaluationEvent",
    "GenerationEndEvent",
    "MigrationSendEvent",
    "MigrationArriveEvent",
    "IslandEndEvent",
    "RunEndEvent",
    "DiagnosticEvent",
    "AnyEvent",
    "EVENT_MODELS",
    "parse_event",
    "EVALUATION_CSV_COLUMNS",
    "EVALUATION_CSV_HEADER",
    "PARENT_ID_SEPARATOR",
    "evaluation_csv_schema",
    "format_evaluation_csv_row",
    "evaluation_from_csv_row",
]

class EventType(str, Enum):
    """The ``type`` discriminator. Adding a member is a MINOR schema change."""

    RUN_START = "run_start"
    ISLAND_START = "island_start"
    EVALUATION = "evaluation"
    GENERATION_END = "generation_end"
    MIGRATION_SEND = "migration_send"
    MIGRATION_ARRIVE = "migration_arrive"
    ISLAND_END = "island_end"
    RUN_END = "run_end"
    DIAGNOSTIC = "diagnostic"


class Operator(str, Enum):
    """What produced an individual -- the label on the incoming STN edge."""

    INIT = "init"
    MUTATION = "mutation"
    CROSSOVER = "crossover"
    CROSSOVER_MUTATION = "crossover_mutation"
    DE_TRIAL = "de_trial"
    PSO_MOVE = "pso_move"
    ES_MUTATION = "es_mutation"
    MIGRATION_ARRIVAL = "migration_arrival"
    ELITE_COPY = "elite_copy"
    CLONE = "clone"
    REEVALUATION = "reevaluation"


class TerminationReason(str, Enum):
    """Why a run or an island stopped. Field is typed ``str``."""

    COMPLETED = "completed"
    MAX_GENERATIONS = "max_generations"
    MAX_EVALUATIONS = "max_evaluations"
    TARGET_FITNESS_REACHED = "target_fitness_reached"
    STAGNATION = "stagnation"
    TIME_LIMIT = "time_limit"
    ERROR = "error"
    ABORTED = "aborted"
    INSUFFICIENT_ISLANDS = "insufficient_islands"


class DiagnosticLevel(str, Enum):
    """Severity of a diagnostic record."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class BaseEvent(BaseModel):
    """Fields present on every record in the stream."""

    model_config = ConfigDict(extra="allow", use_enum_values=True, validate_assignment=False)

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Schema version this record was written against. See archipelago_logging.version.",
    )
    type: str = Field(description="Event discriminator; one of EventType.")
    run_id: str = Field(description="Identifies the run. Every record in a run shares this value.")
    emitter: str = Field(
        description="Who wrote the record: 'master', or 'island:<id>'. Distinguishes a master-observed fact (run_start, run_end) from an island-observed one, and scopes `seq`, which counts per emitter.",
    )
    seq: int = Field(
        description="Per-emitter monotonic counter starting at 0. The authoritative ordering of one emitter's records; unaffected by clock skew or NTP steps.",
    )
    t_wall: float = Field(
        description="Unix epoch seconds (float) on the emitting node. Comparable across nodes only after applying island_start.clock_offset_ns.",
    )
    t_mono_ns: int = Field(
        description="time.monotonic_ns() on the emitting node. Origin is node-local; differences are meaningful only within one emitter.",
    )
    island_id: int | None = Field(
        default=None,
        description="Island this record concerns. Null for whole-run master records.",
    )
    generation: int | None = Field(
        default=None,
        description="Logical generation on island_id at emit time. Null where not applicable. Islands are asynchronous: equal generation numbers on two islands do NOT imply the same wall-clock moment.",
    )
    node_id: str | None = Field(
        default=None,
        description="Container id or hostname of the physical node, for joining a record against the container it ran in (`docker ps`) and against per-node device metrics.",
    )

    def to_json_line(self) -> str:
        """Serialise to a single JSON Lines record (no trailing newline)."""
        return self.model_dump_json(exclude_none=True)


class RunStartEvent(BaseEvent):
    """First record of a run. Carries the fully resolved configuration."""

    type: Literal["run_start"] = "run_start"
    started_at: str = Field(description="Human-facing UTC ISO 8601 timestamp.")
    harness_version: str = Field(description="Version of the dEA harness that produced this log.")
    algorithm: str = Field(description="Registry name of the EA, e.g. 'de'.")
    benchmark: str = Field(description="Registry name of the problem, e.g. 'sphere'.")
    num_islands: int
    population_size: int = Field(
        description="Per-island population size, as resolved. Usually derived from evaluation_budget rather than set directly.",
    )
    evaluation_budget: int = Field(
        description="Total objective evaluations allowed across the WHOLE archipelago -- every island, every generation, until termination. This, not a generation count, is what sizes a run.",
    )
    evaluation_budget_per_island: int = Field(
        description="evaluation_budget divided among the islands; each island's hard ceiling.",
    )
    target_generations: int = Field(
        description="The generation count the population size was derived to land near. A target, not a limit: a run normally ends on stagnation before reaching it.",
    )
    max_generations: int = Field(description="Safety cap on generations.")
    backend: str = Field(description="Execution backend: 'local' or 'docker'.")
    datasets: dict[str, Any] = Field(
        default_factory=dict,
        description="Provenance of the benchmark instance actually used: name, source (file path or generator), and the SHA-256 of the instance file where there is one.",
    )
    migration: dict[str, Any] = Field(
        description="Resolved migration policy: topology, interval, selection, replacement, number of migrants, and the resolved neighbour lists.",
    )
    config: dict[str, Any] = Field(description="Complete resolved run configuration.")
    host: dict[str, Any] = Field(
        default_factory=dict,
        description="Master host facts: platform, python version, cpu count, total memory.",
    )


class IslandStartEvent(BaseEvent):
    """One island has come up and is about to initialise its population."""

    type: Literal["island_start"] = "island_start"
    algorithm: str
    algorithm_params: dict[str, Any] = Field(
        description="Resolved operator parameters actually in force on this island. Logged per-island because heterogeneous-island configurations are a planned experiment, and a run-level copy would then be wrong.",
    )
    benchmark: str
    benchmark_params: dict[str, Any] = Field(default_factory=dict)
    genome_encoding: str = Field(description="GenomeEncoding family this island's individuals use.")
    population_size: int
    evaluation_budget: int | None = Field(
        default=None,
        description="This island's share of the run's evaluation budget; its hard ceiling.",
    )
    island_seed: int = Field(
        description="The random seed this island actually drew, from OS entropy. Recorded as an observation, not an input: runs are deliberately not reproducible, and there is no configuration field that sets this.",
    )
    hostname: str
    pid: int
    container_id: str | None = None
    neighbours_out: list[int] = Field(
        default_factory=list, description="Islands this island may send migrants to."
    )
    neighbours_in: list[int] = Field(
        default_factory=list, description="Islands this island may receive migrants from."
    )
    clock_offset_ns: int | None = Field(
        default=None,
        description="Estimated master_clock - local_clock in nanoseconds. Add to this island's t_wall (x1e9) to place its records on the master timeline.",
    )
    clock_offset_rtt_ns: int | None = Field(
        default=None, description="Round-trip time of the offset probe; uncertainty is ~rtt/2."
    )


class IslandEndEvent(BaseEvent):
    """One island finished (or gave up). Absent for an island that crashed."""

    type: Literal["island_end"] = "island_end"
    termination_reason: str
    generations_completed: int
    evaluations_total: int
    best_fitness: float | None = None
    best_genome_hash: str | None = None
    best_individual_id: str | None = None
    best_genome: Any | None = Field(
        default=None, description="Best genome in FULL representation, regardless of genome mode."
    )
    migrations_sent: int = 0
    migrations_received: int = 0
    migrants_sent: int = 0
    migrants_received: int = 0
    wallclock_seconds: float
    generations_since_improvement: int | None = Field(
        default=None,
        description="Generations the island's best-so-far went without improving before it stopped. Read together with termination_reason it says whether the island converged or simply ran out of budget.",
    )
    algorithm_summary: dict[str, Any] = Field(
        default_factory=dict,
        description="Algorithm-specific end-of-run facts that have no cross-algorithm analogue -- GP tree size and depth, a held-out test error, ES step-size statistics.",
    )
    control_channel: dict[str, Any] = Field(
        default_factory=dict,
        description="Control-plane health for this island: failed calls, log records dropped by the network sink, last transport error.",
    )


class RunEndEvent(BaseEvent):
    """Last record of a run, written by the master."""

    type: Literal["run_end"] = "run_end"
    ended_at: str
    termination_reason: str
    wallclock_seconds: float
    islands_completed: int
    islands_failed: int
    total_evaluations: int
    total_migration_events: int
    total_migrants: int
    global_best_fitness: float | None = None
    global_best_island: int | None = None
    global_best_genome_hash: str | None = None
    global_best_genome: Any | None = None
    per_island: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-island summary keyed by island id as a string (JSON object keys are strings; do not expect integer keys after a round trip).",
    )


class EvaluationEvent(BaseEvent):
    """One individual was evaluated. The atomic unit of the whole format."""

    type: Literal["evaluation"] = "evaluation"
    individual_id: str = Field(description="Globally unique within the run. See archipelago_logging.ids.")
    parent_ids: list[str] = Field(
        default_factory=list,
        description="Always a list: empty for init, one element for mutation, two or more for recombination. A uniform type means downstream never branches on the operator just to read the parents.",
    )
    operator: str = Field(description="What produced this individual; see Operator.")
    fitness: float = Field(
        description="The value the search optimises. For a constrained problem this is the PENALISED objective; see `objective` for the raw one. Orientation is given by generation_end.maximising.",
    )
    objective: float | None = Field(
        default=None,
        description="The raw, unpenalised objective value. Equal to `fitness` for an unconstrained problem, and omitted there.",
    )
    objectives: list[float] | None = Field(
        default=None,
        description="Raw objective vector for multi-objective problems. None for single objective. Reserved: no shipped algorithm is multi-objective in this phase.",
    )
    feasible: bool | None = Field(
        default=None,
        description="Constraint satisfaction, where the problem defines constraints (e.g. the knapsack capacity). None where the problem is unconstrained.",
    )
    constraint_violation: float | None = Field(
        default=None, description="Total violation magnitude; 0.0 when feasible."
    )
    eval_index: int = Field(
        description="Per-island monotonic evaluation counter, gap-free. This is the index of the point in the island's search trajectory.",
    )
    genome_encoding: str
    genome_hash: str = Field(description="128-bit blake2b of the location signature. Always present.")
    genome_repr: Any | None = Field(
        default=None, description="Genome body; interpretation depends on genome_repr_mode."
    )
    genome_repr_mode: str = Field(
        default="hashed", description="'hashed' | 'compact' | 'full'. See archipelago_logging.genome."
    )
    genome_precision_decimals: int | None = Field(
        default=None,
        description="Rounding used to form the signature for real encodings. Hashes from runs with different precision are NOT comparable.",
    )
    genome_dim: int | None = None
    origin_island: int | None = Field(
        default=None,
        description="For an individual that arrived by migration, the island it was born on. Denormalised on purpose: it lets a streaming consumer recognise a migrant without having buffered the migration_arrive record.",
    )
    origin_individual_id: str | None = Field(
        default=None, description="This individual's id on its island of birth."
    )
    is_island_best: bool | None = Field(
        default=None, description="True when this evaluation improved the island's best-so-far."
    )


class GenerationEndEvent(BaseEvent):
    """Per-generation summary so downstream need not replay every evaluation."""

    type: Literal["generation_end"] = "generation_end"
    best_fitness: float
    mean_fitness: float
    worst_fitness: float
    median_fitness: float
    std_fitness: float
    maximising: bool = Field(
        description="Orientation of `fitness`. Recorded."
    )
    best_individual_id: str | None = None
    best_genome_hash: str | None = None
    best_so_far_fitness: float | None = Field(
        default=None, description="Island best across all generations to date, not just this one."
    )
    generations_since_improvement: int | None = Field(
        default=None,
        description="Generations since best_so_far_fitness last improved.",
    )
    diversity: float | None = Field(
        default=None, description="Population diversity; see diversity_metric for its definition."
    )
    diversity_metric: str | None = Field(
        default=None,
        description="Name of the diversity measure, e.g. 'mean_pairwise_euclidean', 'mean_pairwise_hamming', 'fitness_std'. Representation-dependent, so the name must be logged with the value.",
    )
    diversity_sample_size: int | None = Field(
        default=None,
        description="Number of individuals sampled for the O(n^2) pairwise measures. Null means the whole population was used.",
    )
    unique_genome_hashes: int | None = Field(
        default=None, description="Distinct location signatures in the population; a cheap premature-convergence indicator."
    )
    population_size: int
    evaluations_this_generation: int
    evaluations_total: int
    wallclock_seconds: float = Field(description="Wall-clock duration of this generation on this island.")
    migrants_received_this_generation: int = 0
    migrants_sent_this_generation: int = 0


class MigrationSendEvent(BaseEvent):
    """Migrants left the source island. Emitted by the source island."""

    type: Literal["migration_send"] = "migration_send"
    migration_id: str = Field(description="Join key linking this to its migration_arrive.")
    source_island: int
    dest_island: int
    source_generation: int
    migrant_individual_ids: list[str]
    migrant_genome_hashes: list[str]
    migrant_fitnesses: list[float]
    num_migrants: int
    topology: str
    selection_policy: str = Field(description="How migrants were chosen, e.g. 'best', 'random', 'tournament'.")
    migration_interval: int = Field(description="Generations between migration attempts on this island.")
    payload_bytes: int | None = Field(
        default=None,
        description="Serialised size of the transferred payload. Makes the cost of a migration policy measurable -- sending 5 tree genomes is not the same transfer as sending 5 bitstrings.",
    )


class MigrationArriveEvent(BaseEvent):
    """Migrants were integrated into the destination island's population."""

    type: Literal["migration_arrive"] = "migration_arrive"
    migration_id: str
    source_island: int
    dest_island: int
    source_generation: int
    dest_generation: int
    origin_individual_ids: list[str] = Field(description="Ids the migrants carried on the source island.")
    arrived_individual_ids: list[str] = Field(
        description="New ids minted on arrival. A migrant is a distinct individual at its destination -- it has its own evaluation record there -- so it gets its own id, and origin_individual_ids carries the cross-island...",
    )
    replaced_individual_ids: list[str] = Field(
        default_factory=list,
        description="Individuals evicted from the destination population to make room. Empty if the replacement policy grew the population instead.",
    )
    migrant_fitnesses: list[float] = Field(default_factory=list)
    replacement_policy: str
    accepted: bool = Field(description="False when the destination rejected the transfer entirely.")
    rejected_reason: str | None = None
    latency_seconds: float | None = Field(
        default=None,
        description="Wall-clock in-flight time, corrected with the two islands' clock offsets. Null if either offset is unknown.",
    )
    generational_drift: int | None = Field(
        default=None, description="dest_generation - source_generation."
    )


class DiagnosticEvent(BaseEvent):
    """Free-form harness diagnostics kept in-band with the run."""

    type: Literal["diagnostic"] = "diagnostic"
    level: str = DiagnosticLevel.INFO.value
    message: str
    context: dict[str, Any] = Field(default_factory=dict)

AnyEvent = Annotated[
    Union[
        RunStartEvent,
        IslandStartEvent,
        EvaluationEvent,
        GenerationEndEvent,
        MigrationSendEvent,
        MigrationArriveEvent,
        IslandEndEvent,
        RunEndEvent,
        DiagnosticEvent,
    ],
    Field(discriminator="type"),
]

EVENT_MODELS: dict[str, type[BaseEvent]] = {
    EventType.RUN_START.value: RunStartEvent,
    EventType.ISLAND_START.value: IslandStartEvent,
    EventType.EVALUATION.value: EvaluationEvent,
    EventType.GENERATION_END.value: GenerationEndEvent,
    EventType.MIGRATION_SEND.value: MigrationSendEvent,
    EventType.MIGRATION_ARRIVE.value: MigrationArriveEvent,
    EventType.ISLAND_END.value: IslandEndEvent,
    EventType.RUN_END.value: RunEndEvent,
    EventType.DIAGNOSTIC.value: DiagnosticEvent,
}

_ADAPTER: TypeAdapter[Any] = TypeAdapter(AnyEvent)


def parse_event(record: dict[str, Any]) -> BaseEvent:
    """Validate one decoded JSON object into its typed event model."""
    if record.get("type") not in EVENT_MODELS:
        return BaseEvent.model_validate(record)
    return _ADAPTER.validate_python(record)


PARENT_ID_SEPARATOR = ";"

EVALUATION_CSV_COLUMNS: tuple[str, ...] = (
    "run_id",
    "island_id",
    "seq",
    "eval_index",
    "generation",
    "t_wall",
    "t_mono_ns",
    "individual_id",
    "parent_ids",
    "operator",
    "fitness",
    "objective",
    "feasible",
    "constraint_violation",
    "is_island_best",
    "genome_encoding",
    "genome_hash",
    "genome_repr",
    "genome_repr_mode",
    "genome_precision_decimals",
    "genome_dim",
    "origin_island",
    "origin_individual_id",
)

EVALUATION_CSV_HEADER = ",".join(EVALUATION_CSV_COLUMNS)


def evaluation_csv_schema() -> dict[str, Any]:
    """Sidecar describing ``evaluations.csv``, written once."""
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": EventType.EVALUATION.value,
        "columns": list(EVALUATION_CSV_COLUMNS),
        "conventions": {
            "null": "empty field",
            "boolean": "1 for true, 0 for false, empty for unknown",
            "parent_ids": f"{PARENT_ID_SEPARATOR!r}-separated list; empty for an initial individual",
            "genome_repr": (
                "interpretation depends on genome_repr_mode: absent for 'hashed', a base64 "
                "string for 'compact' (see archipelago_logging.decode_compact), JSON for "
                "'full'. Composite encodings (real_vector_velocity, real_vector_sigma) are "
                "JSON objects in both non-hashed modes."
            ),
            "quoting": "RFC 4180 (csv.QUOTE_MINIMAL); read with Python's csv module or pandas",
        },
        "note": (
            "Every other event type for this run is in run.jsonl. Join on run_id, "
            "island_id and generation."
        ),
    }


def _csv_scalar(value: Any) -> str:
    """Render one field. ``None`` becomes an empty string; booleans become 1/0."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _csv_genome_repr(value: Any) -> str:
    """Render ``genome_repr``, which is a string, a JSON structure, or absent."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def format_evaluation_csv_row(event: "EvaluationEvent") -> str:
    """Serialise one evaluation to a single RFC 4180 CSV line (no trailing newline)."""
    values = {
        "run_id": event.run_id,
        "island_id": event.island_id,
        "seq": event.seq,
        "eval_index": event.eval_index,
        "generation": event.generation,
        "t_wall": f"{event.t_wall:.6f}",
        "t_mono_ns": event.t_mono_ns,
        "individual_id": event.individual_id,
        "parent_ids": PARENT_ID_SEPARATOR.join(event.parent_ids or ()),
        "operator": event.operator,
        "fitness": repr(float(event.fitness)),
        "objective": "" if event.objective is None else repr(float(event.objective)),
        "feasible": event.feasible,
        "constraint_violation": event.constraint_violation,
        "is_island_best": event.is_island_best,
        "genome_encoding": event.genome_encoding,
        "genome_hash": event.genome_hash,
        "genome_repr": _csv_genome_repr(event.genome_repr),
        "genome_repr_mode": event.genome_repr_mode,
        "genome_precision_decimals": event.genome_precision_decimals,
        "genome_dim": event.genome_dim,
        "origin_island": event.origin_island,
        "origin_individual_id": event.origin_individual_id,
    }
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="")
    writer.writerow(
        [
            values[column] if column == "genome_repr" else _csv_scalar(values[column])
            for column in EVALUATION_CSV_COLUMNS
        ]
    )
    return buffer.getvalue()


def _csv_int(text: str) -> int | None:
    return int(text) if text != "" else None


def _csv_float(text: str) -> float | None:
    return float(text) if text != "" else None


def _csv_bool(text: str) -> bool | None:
    return None if text == "" else text == "1"


def evaluation_from_csv_row(row: dict[str, str]) -> EvaluationEvent:
    """Rebuild an :class:`EvaluationEvent` from one parsed CSV row."""
    representation: Any = row.get("genome_repr") or None
    mode = row.get("genome_repr_mode") or "hashed"
    if representation is not None and mode != "compact":
        try:
            representation = json.loads(representation)
        except (json.JSONDecodeError, TypeError):
            pass
    elif representation is not None and representation.startswith("{"):
        try:
            representation = json.loads(representation)
        except json.JSONDecodeError:
            pass

    parents = row.get("parent_ids") or ""
    return EvaluationEvent(
        schema_version=SCHEMA_VERSION,
        run_id=row["run_id"],
        emitter=f"island:{row['island_id']}",
        seq=int(row["seq"]),
        t_wall=float(row["t_wall"]),
        t_mono_ns=int(row["t_mono_ns"]),
        island_id=_csv_int(row.get("island_id", "")),
        generation=_csv_int(row.get("generation", "")),
        individual_id=row["individual_id"],
        parent_ids=[part for part in parents.split(PARENT_ID_SEPARATOR) if part],
        operator=row["operator"],
        fitness=float(row["fitness"]),
        objective=_csv_float(row.get("objective", "")),
        feasible=_csv_bool(row.get("feasible", "")),
        constraint_violation=_csv_float(row.get("constraint_violation", "")),
        is_island_best=_csv_bool(row.get("is_island_best", "")),
        eval_index=int(row["eval_index"]),
        genome_encoding=row["genome_encoding"],
        genome_hash=row["genome_hash"],
        genome_repr=representation,
        genome_repr_mode=mode,
        genome_precision_decimals=_csv_int(row.get("genome_precision_decimals", "")),
        genome_dim=_csv_int(row.get("genome_dim", "")),
        origin_island=_csv_int(row.get("origin_island", "")),
        origin_individual_id=row.get("origin_individual_id") or None,
    )
