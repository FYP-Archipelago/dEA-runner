"""EventLogger sequencing and sink behaviour."""

from __future__ import annotations

import json
import threading

import pytest
from pydantic import ValidationError

from archipelago_logging import (
    CallbackSink,
    DiagnosticEvent,
    EventLogger,
    GenomeEncoding,
    GenomePolicy,
    IndividualIdMinter,
    Operator,
    RecordKind,
    Sink,
    parse_individual_id,
)


class _RecordingSink(Sink):
    """Keeps every record with its kind, and notes flushes and closes."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[str, RecordKind]] = []
        self.calls: list[str] = []

    def write(self, line: str, kind: RecordKind) -> None:
        self.records.append((line, kind))

    def flush(self) -> None:
        self.calls.append("flush")

    def close(self) -> None:
        self.calls.append("close")


def test_seq_is_gap_free_and_ordered(logger, captured_lines):
    for index in range(20):
        logger.diagnostic(f"message {index}")
    seqs = [json.loads(line)["seq"] for line in captured_lines]
    assert seqs == list(range(20))


def test_seq_stays_ordered_under_concurrent_emitters(logger, captured_lines):
    """The master logs from one thread per island connection; seq must still be exact."""
    def emit(count: int) -> None:
        for _ in range(count):
            logger.diagnostic("concurrent")

    threads = [threading.Thread(target=emit, args=(50,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    seqs = sorted(json.loads(line)["seq"] for line in captured_lines)
    assert seqs == list(range(200))
    assert [json.loads(line)["seq"] for line in captured_lines] == list(range(200))


def test_eval_index_is_separate_from_seq_and_gap_free(logger, captured_lines):
    policy = GenomePolicy()
    minter = IndividualIdMinter(0)
    for index in range(5):
        logger.diagnostic("interleaved non-evaluation record")
        logger.evaluation(
            individual_id=minter.mint(0),
            operator=Operator.INIT.value,
            fitness=float(index),
            generation=0,
            genome=policy.encode(GenomeEncoding.REAL_VECTOR, [float(index)]),
        )
    records = [json.loads(line) for line in captured_lines]
    evaluations = [r for r in records if r["type"] == "evaluation"]
    assert [r["eval_index"] for r in evaluations] == list(range(5))
    assert [r["seq"] for r in evaluations] != [r["eval_index"] for r in evaluations]


def test_envelope_defaults_can_be_overridden_per_call(logger, captured_lines):
    """The master writes records about islands other than its own."""
    logger.emit(DiagnosticEvent, message="about island 3", level="info", context={}, island_id=3)
    record = json.loads(captured_lines[-1])
    assert record["island_id"] == 3


def test_counts_by_type(logger):
    logger.diagnostic("a")
    logger.diagnostic("b")
    logger.generation_end(
        generation=0,
        best_fitness=1.0,
        mean_fitness=1.0,
        worst_fitness=1.0,
        median_fitness=1.0,
        std_fitness=0.0,
        maximising=False,
        population_size=4,
        evaluations_this_generation=4,
        evaluations_total=4,
        wallclock_seconds=0.01,
    )
    assert logger.counts == {"diagnostic": 2, "generation_end": 1}


def test_every_event_is_validated_before_it_is_written(logger, captured_lines):
    """The only guard between a malformed record and whatever reads the log."""
    with pytest.raises(ValidationError):
        logger.emit(DiagnosticEvent, level="info", context={})
    assert captured_lines == [], "a rejected event must not reach the sink"


def test_a_rejected_event_leaves_no_gap_in_the_counters(logger, captured_lines):
    """The log validator treats a seq or eval_index gap as corruption."""
    genome = GenomePolicy().encode(GenomeEncoding.REAL_VECTOR, [1.0])
    with pytest.raises(ValidationError):
        logger.emit(DiagnosticEvent, level="info", context={})
    with pytest.raises(ValidationError):
        logger.evaluation(
            individual_id="i00-g000000-n000000000",
            operator="init",
            fitness=1.0,
            generation="not a generation",
            genome=genome,
        )
    logger.emit(DiagnosticEvent, message="accepted", level="info", context={})
    logger.evaluation(
        individual_id="i00-g000000-n000000000", operator="init", fitness=1.0, generation=0, genome=genome
    )
    written = [json.loads(line) for line in captured_lines]
    assert [record["seq"] for record in written] == [0, 1]
    assert written[1]["eval_index"] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_number_is_refused_at_write_time(logger, captured_lines, value):
    """JSON has no inf or NaN; written, it would come out as a null no reader accepts."""
    genome = GenomePolicy().encode(GenomeEncoding.REAL_VECTOR, [1.0])
    with pytest.raises(ValidationError, match="finite number"):
        logger.evaluation(
            individual_id="i00-g000000-n000000000",
            operator="init",
            fitness=value,
            generation=0,
            genome=genome,
        )
    assert captured_lines == []


def test_individual_ids_are_parseable_and_unique_per_island():
    minter = IndividualIdMinter(3)
    ids = [minter.mint(generation=12) for _ in range(100)]
    assert len(set(ids)) == 100
    parsed = parse_individual_id(ids[0])
    assert parsed.island_id == 3 and parsed.generation == 12


def test_individual_ids_sort_lexicographically_by_generation_then_ordinal():
    minter = IndividualIdMinter(1)
    ids = [minter.mint(generation) for generation in (0, 1, 2) for _ in range(3)]
    assert ids == sorted(ids)


def test_evaluations_and_events_share_one_stream_in_write_order():
    """One ordered stream per island: the order on the wire is the order of seq."""
    captured: list[str] = []
    logger = EventLogger("run-x", "island:0", CallbackSink(captured.append), island_id=0)
    policy = GenomePolicy()
    minter = IndividualIdMinter(0)
    for index in range(4):
        logger.diagnostic("tick")
        logger.evaluation(
            individual_id=minter.mint(0),
            operator=Operator.INIT.value,
            fitness=float(index),
            generation=0,
            genome=policy.encode(GenomeEncoding.REAL_VECTOR, [float(index)]),
        )

    records = [json.loads(line) for line in captured]
    assert [record["seq"] for record in records] == list(range(8))
    assert [record["type"] for record in records] == ["diagnostic", "evaluation"] * 4


def test_every_record_is_filed_under_its_kind():
    """``row`` for an evaluation, ``event`` for everything else, so consumers can route cheaply."""
    sink = _RecordingSink()
    logger = EventLogger("run-x", "island:0", sink, island_id=0)
    logger.diagnostic("before")
    logger.evaluation(
        individual_id="i00-g000000-n000000000",
        operator=Operator.INIT.value,
        fitness=1.0,
        generation=0,
        genome=GenomePolicy().encode(GenomeEncoding.REAL_VECTOR, [1.0]),
    )
    assert [(json.loads(line)["type"], kind) for line, kind in sink.records] == [
        ("diagnostic", RecordKind.EVENT),
        ("evaluation", RecordKind.ROW),
    ]


def test_the_logger_passes_flush_and_close_to_its_sink():
    sink = _RecordingSink()
    logger = EventLogger("run-x", "island:0", sink, island_id=0)
    logger.flush()
    logger.close()
    assert sink.calls == ["flush", "close"]
