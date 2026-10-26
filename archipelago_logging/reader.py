"""Read side of the log contract: parse, validate, and reconcile."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .events import (
    BaseEvent,
    EvaluationEvent,
    EventType,
    MigrationArriveEvent,
    MigrationSendEvent,
    evaluation_from_csv_row,
    parse_event,
)
from .version import SCHEMA_VERSION

__all__ = [
    "iter_lines",
    "iter_events",
    "iter_evaluations",
    "iter_evaluation_rows",
    "companion_evaluations_path",
    "iter_raw",
    "LogValidationError",
    "validate_log",
    "MigrationEdge",
    "join_migration_edges",
]


class LogValidationError(Exception):
    """Raised by :func:`validate_log` when a log violates the schema contract."""


def iter_lines(path: str | Path) -> Iterator[str]:
    """Yield non-empty lines from a JSON Lines log."""
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield line


def iter_raw(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield decoded JSON objects without model validation."""
    for line in iter_lines(path):
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def companion_evaluations_path(jsonl_path: str | Path) -> Path | None:
    """Locate the CSV evaluation stream that belongs with a JSON Lines file."""
    path = Path(jsonl_path)
    stem = path.name
    if stem.endswith(".jsonl"):
        stem = stem[: -len(".jsonl")]

    candidates = [path.with_name(f"{stem}.evaluations.csv")]
    if stem == "run":
        candidates.append(path.with_name("evaluations.csv"))

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def iter_evaluation_rows(path: str | Path) -> Iterator[dict[str, str]]:
    """Yield raw ``dict`` rows from an ``evaluations.csv``, unvalidated."""
    with open(path, "r", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


def iter_evaluations(path: str | Path, *, strict: bool = False) -> Iterator[EvaluationEvent]:
    """Yield validated :class:`EvaluationEvent` models from an ``evaluations.csv``."""
    for number, row in enumerate(iter_evaluation_rows(path), start=2):
        try:
            yield evaluation_from_csv_row(row)
        except (KeyError, ValueError, TypeError) as exc:
            if strict:
                raise LogValidationError(f"{path}:{number}: malformed evaluation row: {exc}") from exc
            continue


def iter_events(
    path: str | Path, *, strict: bool = False, include_evaluations: bool = True
) -> Iterator[BaseEvent]:
    """Yield validated event models from a run's JSON Lines file."""
    for index, line in enumerate(iter_lines(path)):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            if strict:
                raise LogValidationError(f"{path}:{index + 1}: undecodable JSON: {exc}") from exc
            continue

        version = record.get("schema_version", "")
        if version != SCHEMA_VERSION:
            if strict:
                raise LogValidationError(
                    f"{path}:{index + 1}: schema_version {version!r}; this reader reads "
                    f"{SCHEMA_VERSION} only"
                )
            continue

        yield parse_event(record)

    if include_evaluations:
        companion = companion_evaluations_path(path)
        if companion is not None:
            yield from iter_evaluations(companion, strict=strict)


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
    path: str | Path,
    *,
    require_run_end: bool = True,
    evaluations_path: str | Path | None = None,
) -> dict[str, Any]:
    """Check a run's two streams against the invariants the contract promises."""
    counts: dict[str, int] = {}
    seq_by_emitter: dict[str, set[int]] = {}
    eval_index_by_island: dict[int, set[int]] = {}
    run_ids: set[str] = set()
    send_ids: set[str] = set()
    arrive_ids: set[str] = set()
    first_type: str | None = None
    total = 0

    def note_seq(emitter: str, seq: int) -> None:
        """Record one emitter's seq, rejecting a repeat."""
        seen = seq_by_emitter.setdefault(emitter, set())
        if seq in seen:
            raise LogValidationError(f"{path}: emitter {emitter!r} repeats seq {seq}")
        seen.add(seq)

    for event in iter_events(path, strict=True, include_evaluations=False):
        total += 1
        if first_type is None:
            first_type = event.type
        counts[event.type] = counts.get(event.type, 0) + 1
        run_ids.add(event.run_id)
        note_seq(event.emitter, event.seq)

        if event.type == EventType.MIGRATION_SEND.value:
            send_ids.add(event.migration_id)
        elif event.type == EventType.MIGRATION_ARRIVE.value:
            arrive_ids.add(event.migration_id)

    if total == 0:
        raise LogValidationError(f"{path}: empty log")

    companion = (
        Path(evaluations_path) if evaluations_path is not None else companion_evaluations_path(path)
    )
    evaluations = 0
    if companion is not None:
        for event in iter_evaluations(companion, strict=True):
            evaluations += 1
            counts[event.type] = counts.get(event.type, 0) + 1
            run_ids.add(event.run_id)
            note_seq(event.emitter, event.seq)

            island = event.island_id
            seen = eval_index_by_island.setdefault(island, set())
            if event.eval_index in seen:
                raise LogValidationError(
                    f"{companion}: island {island} repeats eval_index {event.eval_index}"
                )
            seen.add(event.eval_index)
        total += evaluations

    for emitter, seen in seq_by_emitter.items():
        expected = set(range(len(seen)))
        if seen != expected:
            missing = sorted(expected - seen)[:5]
            raise LogValidationError(
                f"{path}: emitter {emitter!r} has {len(expected - seen)} seq gap(s), "
                f"first missing: {missing}. Records were lost in transit."
            )

    for island, seen in eval_index_by_island.items():
        expected = set(range(len(seen)))
        if seen != expected:
            missing = sorted(expected - seen)[:5]
            raise LogValidationError(
                f"{companion}: island {island} has {len(expected - seen)} eval_index gap(s), "
                f"first missing: {missing}"
            )

    if len(run_ids) != 1:
        raise LogValidationError(f"{path}: log mixes {len(run_ids)} run ids: {sorted(run_ids)}")
    if counts.get(EventType.RUN_START.value, 0) != 1:
        raise LogValidationError(
            f"{path}: expected exactly one run_start, found {counts.get(EventType.RUN_START.value, 0)}"
        )
    if first_type != EventType.RUN_START.value:
        raise LogValidationError(f"{path}: first record is {first_type!r}, expected 'run_start'")
    if require_run_end and counts.get(EventType.RUN_END.value, 0) != 1:
        raise LogValidationError(
            f"{path}: expected exactly one run_end, found {counts.get(EventType.RUN_END.value, 0)}"
        )
    orphan_arrivals = arrive_ids - send_ids
    if orphan_arrivals:
        raise LogValidationError(
            f"{path}: {len(orphan_arrivals)} migration_arrive without migration_send: "
            f"{sorted(orphan_arrivals)[:5]}"
        )

    return {
        "path": str(path),
        "evaluations_path": None if companion is None else str(companion),
        "records": total,
        "run_id": next(iter(run_ids)),
        "counts": counts,
        "emitters": sorted(seq_by_emitter),
        "islands": sorted(eval_index_by_island),
        "evaluations_per_island": {
            island: len(seen) for island, seen in sorted(eval_index_by_island.items())
        },
        "total_evaluations": evaluations,
        "migration_sends": len(send_ids),
        "migration_arrivals": len(arrive_ids),
        "undelivered_migrations": len(send_ids - arrive_ids),
    }
