"""Read-side helpers: parsing, validation and migration-edge reconstruction."""

from __future__ import annotations

import json

import pytest

from archipelago_logging import (
    CallbackSink,
    EventLogger,
    GenomeEncoding,
    GenomePolicy,
    IndividualIdMinter,
    LogValidationError,
    join_migration_edges,
    parse_records,
    validate_log,
)


def _valid_records(*, with_run_end=True, migrations=True, evaluations=0) -> list[dict]:
    """A small, well-formed run, as decoded records in the order they were written."""
    lines: list[str] = []
    sink = CallbackSink(lines.append)
    master = EventLogger("run-x", "master", sink)
    island0 = EventLogger("run-x", "island:0", sink, island_id=0)
    island1 = EventLogger("run-x", "island:1", sink, island_id=1)

    master.run_start(
        started_at="now",
        harness_version="0.2.0",
        algorithm="de",
        benchmark="sphere",
        num_islands=2,
        population_size=4,
        evaluation_budget=100,
        evaluation_budget_per_island=50,
        target_generations=10,
        max_generations=100,
        backend="docker",
        migration={},
        datasets={"benchmark": "sphere", "maximising": False},
        config={},
    )

    for island, logger in ((0, island0), (1, island1)):
        logger.island_start(
            algorithm="de",
            algorithm_params={},
            benchmark="sphere",
            genome_encoding="real_vector",
            benchmark_params={"name": "sphere"},
            generation=0,
            population_size=4,
            island_seed=island,
            hostname="h",
            pid=1,
        )

    policy = GenomePolicy()
    for island, logger in ((0, island0), (1, island1)):
        minter = IndividualIdMinter(island)
        for index in range(evaluations):
            logger.evaluation(
                individual_id=minter.mint(0),
                operator="init",
                fitness=float(index),
                generation=0,
                genome=policy.encode(GenomeEncoding.REAL_VECTOR, [float(index)]),
            )
    if migrations:
        island0.migration_send(
            generation=1,
            migration_id="mig-00-01-000000",
            source_island=0,
            dest_island=1,
            source_generation=1,
            migrant_individual_ids=["i00-g000001-n000000000"],
            migrant_genome_hashes=["ab" * 16],
            migrant_fitnesses=[1.0],
            num_migrants=1,
            topology="ring",
            selection_policy="best",
            migration_interval=1,
        )
        island1.migration_arrive(
            generation=3,
            migration_id="mig-00-01-000000",
            source_island=0,
            dest_island=1,
            source_generation=1,
            dest_generation=3,
            origin_individual_ids=["i00-g000001-n000000000"],
            arrived_individual_ids=["i01-g000003-n000000009"],
            replacement_policy="worst",
            accepted=True,
            generational_drift=2,
            latency_seconds=0.25,
        )
        island1.migration_send(
            generation=2,
            migration_id="mig-01-00-000001",
            source_island=1,
            dest_island=0,
            source_generation=2,
            migrant_individual_ids=["i01-g000002-n000000001"],
            migrant_genome_hashes=["cd" * 16],
            migrant_fitnesses=[2.0],
            num_migrants=1,
            topology="ring",
            selection_policy="best",
            migration_interval=1,
        )
    if with_run_end:
        master.run_end(
            ended_at="now",
            termination_reason="completed",
            wallclock_seconds=1.0,
            islands_completed=2,
            islands_failed=0,
            total_evaluations=0,
            total_migration_events=2,
            total_migrants=2,
        )
    return [json.loads(line) for line in lines]


def _renumbered(records: list[dict]) -> list[dict]:
    """The records with each emitter's seq renumbered from 0, as if nothing had been removed."""
    seqs: dict[str, int] = {}
    for record in records:
        record["seq"] = seqs.get(record["emitter"], 0)
        seqs[record["emitter"]] = record["seq"] + 1
    return records


def test_validate_log_accepts_a_well_formed_log():
    report = validate_log(_valid_records())
    assert report["run_id"] == "run-x"
    assert report["migration_sends"] == 2
    assert report["migration_arrivals"] == 1
    assert report["undelivered_migrations"] == 1
    assert sorted(report["emitters"]) == ["island:0", "island:1", "master"]


def test_validate_log_rejects_a_seq_gap():
    """A seq gap means the stream lost records; that must be loud."""
    records = [record for record in _valid_records() if record["type"] != "island_start"]
    with pytest.raises(LogValidationError, match="lost, repeated or reordered"):
        validate_log(records)


def test_validate_log_requires_run_start_first():
    records = _valid_records()
    with pytest.raises(LogValidationError):
        validate_log(records[1:] + records[:1])


def test_validate_log_can_allow_an_in_progress_run():
    records = _valid_records(with_run_end=False)
    with pytest.raises(LogValidationError, match="run_end"):
        validate_log(records)
    assert validate_log(records, require_run_end=False)["records"] > 0


def test_validate_log_rejects_an_orphan_arrival():
    records = _renumbered(
        [record for record in _valid_records() if record["type"] != "migration_send"]
    )
    with pytest.raises(LogValidationError, match="migration_arrive without migration_send"):
        validate_log(records)


def test_join_migration_edges_pairs_send_and_arrive():
    edges = join_migration_edges(parse_records(_valid_records()))
    by_id = {edge.migration_id: edge for edge in edges}

    delivered = by_id["mig-00-01-000000"]
    assert delivered.delivered
    assert delivered.source_island == 0 and delivered.dest_island == 1
    assert delivered.generational_drift == 2
    assert delivered.latency_seconds == 0.25

    dangling = by_id["mig-01-00-000001"]
    assert dangling.arrive is None
    assert not dangling.delivered
    assert dangling.generational_drift is None


def test_records_carry_no_schema_version():
    """The format is not versioned, so no record names a version, and one that does is refused."""
    records = _valid_records()
    assert not any("schema_version" in record for record in records)
    records[-1]["schema_version"] = "2"
    with pytest.raises(LogValidationError, match="schema_version"):
        validate_log(records)


def test_empty_log_is_rejected():
    with pytest.raises(LogValidationError, match="empty log"):
        validate_log([])


def test_validate_log_counts_evaluations_per_island():
    report = validate_log(_valid_records(evaluations=5))
    assert report["total_evaluations"] == 10
    assert report["counts"]["evaluation"] == 10
    assert report["evaluations_per_island"] == {0: 5, 1: 5}


def test_validate_log_rejects_a_lost_evaluation():
    records = _valid_records(evaluations=5)
    lost = next(i for i, record in enumerate(records) if record["type"] == "evaluation")
    del records[lost + 2]
    with pytest.raises(LogValidationError, match="lost, repeated or reordered"):
        validate_log(records)


def test_validate_log_rejects_a_repeated_record():
    records = _valid_records(evaluations=4)
    repeated = next(i for i, record in enumerate(records) if record["type"] == "evaluation")
    records.insert(repeated + 1, records[repeated])
    with pytest.raises(LogValidationError, match="lost, repeated or reordered"):
        validate_log(records)


def test_validate_log_rejects_two_records_of_one_island_swapped():
    """Records of one island must arrive in the order the island wrote them."""
    records = _valid_records(evaluations=4)
    island0 = [i for i, record in enumerate(records) if record["emitter"] == "island:0"]
    first, second = island0[1], island0[2]
    records[first], records[second] = records[second], records[first]
    with pytest.raises(LogValidationError, match="island:0 sent seq 2 where 1 was due"):
        validate_log(records)


def test_validate_log_accepts_islands_interleaved_with_each_other():
    """Order is per island; how islands interleave in the merged stream is arbitrary."""
    records = _valid_records(evaluations=3)
    head, body, tail = records[:1], records[1:-1], records[-1:]
    island1 = [record for record in body if record["emitter"] == "island:1"]
    others = [record for record in body if record["emitter"] != "island:1"]
    assert validate_log(head + island1 + others + tail)["total_evaluations"] == 6


def test_validate_log_rejects_an_eval_index_that_skips():
    records = _valid_records(evaluations=3)
    last = max(i for i, record in enumerate(records) if record["type"] == "evaluation")
    records[last]["eval_index"] += 1
    with pytest.raises(LogValidationError, match="eval_index"):
        validate_log(records)


def test_validate_log_lists_an_island_that_evaluated_nothing():
    """An island that failed before its first evaluation still wrote island_start."""
    records = _valid_records(evaluations=0)
    assert validate_log(records)["islands"] == [0, 1]
    assert validate_log(records)["evaluations_per_island"] == {}
