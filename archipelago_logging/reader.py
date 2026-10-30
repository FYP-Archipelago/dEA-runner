"""Read side of the log contract: parse, validate, and reconcile."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from .events import (
    BaseEvent,
    EventType,
    MigrationArriveEvent,
    MigrationSendEvent,
    parse_event,
)
from .version import SCHEMA_VERSION

__all__ = [
    "parse_records",
    "LogValidationError",
    "validate_log",
    "MigrationEdge",
    "join_migration_edges",
]


class LogValidationError(Exception):
    """Raised when a log violates the schema contract."""


def parse_records(records: Iterable[dict[str, Any]]) -> Iterator[BaseEvent]:
    """Validate decoded records into event models, in the order given."""
    for number, record in enumerate(records, start=1):
        version = record.get("schema_version", "")
        if version != SCHEMA_VERSION:
            raise LogValidationError(
                f"record {number}: schema_version {version!r}; this reader reads "
                f"{SCHEMA_VERSION} only"
            )
        yield parse_event(record)


@dataclass(frozen=True)
class MigrationEdge:
    """One logical migration, reconstructed from its send/arrive pair."""

    migration_id: str
    send: MigrationSendEvent
    arrive: MigrationArriveEvent | None

    @property
    def delivered(self) -> bool:
        """True once the destination accepted the transfer."""
        return self.arrive is not None and bool(self.arrive.accepted)

    @property
    def source_island(self) -> int:
        """Island the migrants left."""
        return self.send.source_island

    @property
    def dest_island(self) -> int:
        """Island the migrants were sent to."""
        return self.send.dest_island

    @property
    def generational_drift(self) -> int | None:
        """Generations the destination was ahead of the source."""
        if self.arrive is None:
            return None
        return self.arrive.dest_generation - self.send.source_generation

    @property
    def latency_seconds(self) -> float | None:
        """Clock-corrected in-flight time, or None if unknown."""
        if self.arrive is None:
            return None
        if self.arrive.latency_seconds is not None:
            return self.arrive.latency_seconds
        return self.arrive.t_wall - self.send.t_wall


def join_migration_edges(events: Iterable[BaseEvent]) -> list[MigrationEdge]:
    """Pair ``migration_send`` with ``migration_arrive`` on ``migration_id``."""
    sends: dict[str, MigrationSendEvent] = {}
    arrivals: dict[str, MigrationArriveEvent] = {}

    for event in events:
        if event.type == EventType.MIGRATION_SEND.value and isinstance(event, MigrationSendEvent):
            sends[event.migration_id] = event
        elif event.type == EventType.MIGRATION_ARRIVE.value and isinstance(event, MigrationArriveEvent):
            arrivals[event.migration_id] = event

    return [
        MigrationEdge(migration_id=migration_id, send=send, arrive=arrivals.get(migration_id))
        for migration_id, send in sends.items()
    ]


def validate_log(
    records: Iterable[dict[str, Any]], *, require_run_end: bool = True
) -> dict[str, Any]:
    """Check one run's records, in arrival order, against the invariants the contract promises.

    Takes decoded records rather than a path, so the same check applies to whatever
    carries them. Each emitter's ``seq`` must arrive as exactly 0, 1, 2, ... and each
    island's ``eval_index`` likewise: that one rule catches a lost record, a duplicate
    and a reordering alike.
    """
    counts: dict[str, int] = {}
    next_seq: dict[str, int] = {}
    next_eval_index: dict[int, int] = {}
    islands: set[int] = set()
    run_ids: set[str] = set()
    send_ids: set[str] = set()
    arrive_ids: set[str] = set()
    first_type: str | None = None
    total = 0

    for number, record in enumerate(records, start=1):
        version = record.get("schema_version", "")
        if version != SCHEMA_VERSION:
            raise LogValidationError(
                f"record {number}: schema_version {version!r}; this reader reads "
                f"{SCHEMA_VERSION} only"
            )
        try:
            event = parse_event(record)
        except ValueError as exc:
            raise LogValidationError(f"record {number}: invalid {record.get('type')!r}: {exc}") from exc

        total += 1
        if first_type is None:
            first_type = event.type
        counts[event.type] = counts.get(event.type, 0) + 1
        run_ids.add(event.run_id)

        expected = next_seq.get(event.emitter, 0)
        if event.seq != expected:
            raise LogValidationError(
                f"record {number}: {event.emitter} sent seq {event.seq} where {expected} was "
                "due; its records were lost, repeated or reordered in transit"
            )
        next_seq[event.emitter] = expected + 1
        if event.emitter.startswith("island:"):
            islands.add(event.island_id)

        if event.type == EventType.EVALUATION.value:
            island = event.island_id
            expected_index = next_eval_index.get(island, 0)
            if event.eval_index != expected_index:
                raise LogValidationError(
                    f"record {number}: island {island} sent eval_index {event.eval_index} "
                    f"where {expected_index} was due"
                )
            next_eval_index[island] = expected_index + 1
        elif event.type == EventType.MIGRATION_SEND.value:
            send_ids.add(event.migration_id)
        elif event.type == EventType.MIGRATION_ARRIVE.value:
            arrive_ids.add(event.migration_id)

    if total == 0:
        raise LogValidationError("empty log")
    if len(run_ids) != 1:
        raise LogValidationError(f"log mixes {len(run_ids)} run ids: {sorted(run_ids)}")
    if counts.get(EventType.RUN_START.value, 0) != 1:
        raise LogValidationError(
            f"expected exactly one run_start, found {counts.get(EventType.RUN_START.value, 0)}"
        )
    if first_type != EventType.RUN_START.value:
        raise LogValidationError(f"first record is {first_type!r}, expected 'run_start'")
    if require_run_end and counts.get(EventType.RUN_END.value, 0) != 1:
        raise LogValidationError(
            f"expected exactly one run_end, found {counts.get(EventType.RUN_END.value, 0)}"
        )
    orphan_arrivals = arrive_ids - send_ids
    if orphan_arrivals:
        raise LogValidationError(
            f"{len(orphan_arrivals)} migration_arrive without migration_send: "
            f"{sorted(orphan_arrivals)[:5]}"
        )

    return {
        "records": total,
        "run_id": next(iter(run_ids)),
        "counts": counts,
        "emitters": sorted(next_seq),
        "islands": sorted(islands),
        "evaluations_per_island": dict(sorted(next_eval_index.items())),
        "total_evaluations": counts.get(EventType.EVALUATION.value, 0),
        "migration_sends": len(send_ids),
        "migration_arrivals": len(arrive_ids),
        "undelivered_migrations": len(send_ids - arrive_ids),
    }
