"""``EventLogger`` -- the single entry point for emitting Archipelago events."""

from __future__ import annotations

import threading
from typing import Any

from .clock import mono_ns, wall_now
from .events import (
    BaseEvent,
    DiagnosticEvent,
    DiagnosticLevel,
    EvaluationEvent,
    GenerationEndEvent,
    IslandEndEvent,
    IslandStartEvent,
    MigrationArriveEvent,
    MigrationSendEvent,
    RecordKind,
    RunEndEvent,
    RunStartEvent,
)
from .genome import EncodedGenome
from .sinks import Sink
from .version import SCHEMA_VERSION

__all__ = ["EventLogger"]


class EventLogger:
    """Stamps envelopes onto events and hands serialised lines to a sink."""

    def __init__(
        self,
        run_id: str,
        emitter: str,
        sink: Sink,
        *,
        island_id: int | None = None,
        node_id: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.emitter = emitter
        self.sink = sink
        self.island_id = island_id
        self.node_id = node_id

        self._seq = 0
        self._lock = threading.RLock()
        self._eval_index = 0
        self._counts: dict[str, int] = {}


    @property
    def seq(self) -> int:
        """Number of records emitted so far by this logger."""
        return self._seq

    @property
    def eval_index(self) -> int:
        """Number of ``evaluation`` records emitted so far by this logger."""
        return self._eval_index

    @property
    def counts(self) -> dict[str, int]:
        """Records emitted, by event type. Reported in the run summary."""
        return dict(self._counts)

    def flush(self) -> None:
        """Return once every record emitted so far has been delivered."""
        self.sink.flush()

    def close(self) -> None:
        """Flush and close the sink."""
        self.sink.close()

    def __enter__(self) -> "EventLogger":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


    def emit(self, model: type[BaseEvent], **fields: Any) -> BaseEvent:
        """Build, stamp and write one event of type ``model``."""
        fields.setdefault("island_id", self.island_id)
        fields.setdefault("node_id", self.node_id)

        with self._lock:
            fields["schema_version"] = SCHEMA_VERSION
            fields["run_id"] = self.run_id
            fields["emitter"] = self.emitter
            fields["seq"] = self._seq
            fields["t_wall"] = wall_now()
            fields["t_mono_ns"] = mono_ns()

            event = model(**fields)
            self._seq += 1
            type_name = getattr(event, "type", model.__name__)
            self._counts[type_name] = self._counts.get(type_name, 0) + 1

            self.sink.write(event.to_json_line(), RecordKind.of(event.type))

        return event


    def run_start(self, **fields: Any) -> RunStartEvent:
        """Emit the run's first record."""
        return self.emit(RunStartEvent, **fields)

    def island_start(self, **fields: Any) -> IslandStartEvent:
        """Emit an island's first record."""
        return self.emit(IslandStartEvent, **fields)

    def island_end(self, **fields: Any) -> IslandEndEvent:
        """Emit an island's last record."""
        return self.emit(IslandEndEvent, **fields)

    def run_end(self, **fields: Any) -> RunEndEvent:
        """Emit the run's last record."""
        return self.emit(RunEndEvent, **fields)


    def evaluation(
        self,
        *,
        individual_id: str,
        operator: str,
        fitness: float,
        generation: int,
        genome: EncodedGenome,
        parent_ids: list[str] | None = None,
        **fields: Any,
    ) -> EvaluationEvent:
        """Emit one ``evaluation`` record."""
        with self._lock:
            event = self.emit(
                EvaluationEvent,
                individual_id=individual_id,
                parent_ids=list(parent_ids or []),
                operator=operator,
                fitness=float(fitness),
                generation=generation,
                eval_index=self._eval_index,
                genome_encoding=genome.encoding.value,
                genome_hash=genome.genome_hash,
                genome_repr=genome.genome_repr,
                genome_repr_mode=genome.genome_repr_mode.value,
                genome_precision_decimals=genome.precision_decimals,
                genome_dim=genome.dim,
                **fields,
            )
            self._eval_index += 1
        return event

    def generation_end(self, **fields: Any) -> GenerationEndEvent:
        """Emit one generation's summary."""
        return self.emit(GenerationEndEvent, **fields)


    def migration_send(self, **fields: Any) -> MigrationSendEvent:
        """Emit the dispatch half of a migration."""
        return self.emit(MigrationSendEvent, **fields)

    def migration_arrive(self, **fields: Any) -> MigrationArriveEvent:
        """Emit the arrival half of a migration."""
        return self.emit(MigrationArriveEvent, **fields)


    def diagnostic(
        self,
        message: str,
        level: str = DiagnosticLevel.INFO.value,
        **context: Any,
    ) -> DiagnosticEvent:
        """Emit a free-form Runner note, in band with the run."""
        return self.emit(
            DiagnosticEvent, message=message, level=level, context=context
        )
