"""Full runs through the real master, real island containers and real sockets."""

from __future__ import annotations

import subprocess

import pytest

from archipelago_logging import (
    EvaluationEvent,
    EventType,
    join_migration_edges,
    new_run_id,
    parse_individual_id,
    parse_records,
    validate_log,
)
from orchestrator.config import RunConfig
from orchestrator.master import Master, RunSummary
from orchestrator.redis_stream import read_stream, stream_key

from conftest import Broker, settings_for

pytestmark = [pytest.mark.slow, pytest.mark.docker, pytest.mark.usefixtures("island_image")]


def _config(broker: Broker, **overrides) -> RunConfig:
    base = {
        "run_id": new_run_id(),
        "algorithm": "de",
        "benchmark": "sphere",
        "benchmark_params": {"dimension": 5},
        "evaluation_budget": 480,
        "target_generations": 8,
        "num_islands": 4,
        "termination": {"stagnation_generations": 0, "max_generations": 100},
        "migration": {
            "topology": "ring",
            "interval": 2,
            "num_migrants": 2,
            "selection": "best",
            "replacement": "worst",
        },
    }
    mergeable = {"migration", "logging", "termination"}
    for key, value in overrides.items():
        if key in mergeable and isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return RunConfig(**base)


def _run(config: RunConfig, broker: Broker) -> tuple[RunSummary, list[dict]]:
    """Run to completion, then read back every record the run appended, in stream order."""
    summary = Master(config, settings_for(broker)).run()
    return summary, list(read_stream(broker.url, summary.stream_key))


def _evaluation_records(records: list[dict]) -> list[dict]:
    return [record for record in records if record["type"] == "evaluation"]


def _evaluations(records: list[dict]) -> list[EvaluationEvent]:
    return [event for event in parse_records(records) if isinstance(event, EvaluationEvent)]


@pytest.fixture(scope="module")
def completed_run(tmp_path_factory, repository_root, island_image, run_broker):
    """One shared run; the assertions below all read the same records."""
    config = _config(run_broker)
    summary, records = _run(config, run_broker)
    return summary, config, records


def test_run_completes_with_every_island_finishing(completed_run):
    summary, config, records = completed_run
    assert summary.islands_completed == config.num_islands
    assert summary.islands_failed == 0
    assert summary.termination_reason == "completed"
    assert summary.total_evaluations > 0


def test_every_island_reports_its_result_in_its_goodbye(completed_run):
    """No result files: each island hands the master its result over the control channel."""
    summary, config, records = completed_run
    assert summary.stream_key == stream_key(summary.run_id)
    island_ends = {record["island_id"]: record for record in records if record["type"] == "island_end"}
    assert [result["island_id"] for result in summary.island_results] == list(range(config.num_islands))
    for result in summary.island_results:
        assert result["error"] is None
        assert result["evaluations_total"] == island_ends[result["island_id"]]["evaluations_total"]
        assert result["termination_reason"] == island_ends[result["island_id"]]["termination_reason"]


def test_a_run_writes_no_files(tmp_path, repository_root, run_broker, monkeypatch):
    """Everything about a run is in its stream; nothing lands where the master runs."""
    monkeypatch.chdir(tmp_path)
    summary, records = _run(_config(run_broker, evaluation_budget=240), run_broker)
    assert summary.islands_failed == 0
    assert records[-1]["type"] == "run_end"
    assert list(tmp_path.iterdir()) == []


def test_the_run_spends_close_to_its_evaluation_budget(completed_run):
    """The whole sizing model: the dataset is a known number of rows."""
    summary, config, records = completed_run
    assert summary.total_evaluations <= config.evaluation_budget
    assert summary.total_evaluations >= 0.8 * config.evaluation_budget

    assert len(_evaluation_records(records)) == summary.total_evaluations


def test_no_island_exceeds_its_share_of_the_budget(completed_run):
    summary, config, records = completed_run
    ceiling = config.budget_per_island()
    for entry in summary.per_island.values():
        assert entry["evaluations"] <= ceiling


def test_every_island_arrives_in_the_order_it_wrote(completed_run):
    """Each island appends over its own connection, so its seq never steps back in the stream."""
    summary, config, records = completed_run
    seqs: dict[str, list[int]] = {}
    for record in records:
        seqs.setdefault(record["emitter"], []).append(record["seq"])
    assert {f"island:{island}" for island in range(config.num_islands)} <= set(seqs)
    for emitter, found in seqs.items():
        assert found == list(range(len(found))), emitter


def test_the_dataset_is_identified_in_run_start(completed_run):
    """Search randomness is not reproducible; which problem was solved must be."""
    summary, _config, records = completed_run
    first = records[0]
    assert first["type"] == "run_start"
    assert first["datasets"]["benchmark"] == "sphere"
    assert first["datasets"]["domain"] == "continuous"


def test_the_stream_satisfies_the_schema_contract(completed_run):
    summary, config, records = completed_run
    report = validate_log(records)
    assert report["run_id"] == summary.run_id
    assert set(report["islands"]) == set(range(config.num_islands))
    for required in (
        EventType.RUN_START.value,
        EventType.ISLAND_START.value,
        EventType.EVALUATION.value,
        EventType.GENERATION_END.value,
        EventType.ISLAND_END.value,
        EventType.RUN_END.value,
    ):
        assert report["counts"].get(required, 0) > 0, required


def test_run_start_comes_first_and_run_end_last(completed_run):
    """The stream's boundaries, as the backend reads them."""
    _summary, _config, records = completed_run
    assert records[0]["type"] == "run_start"
    assert records[-1]["type"] == "run_end"


def test_run_start_carries_the_config_and_the_budget_plan(completed_run):
    summary, config, records = completed_run
    first = records[0]
    assert first["type"] == "run_start"
    assert first["config"]["algorithm"] == config.algorithm
    assert first["evaluation_budget"] == config.evaluation_budget
    assert first["evaluation_budget_per_island"] == config.budget_per_island()
    assert first["population_size"] == config.resolved_population_size()
    assert first["config"]["_runtime"]["budget_plan"]["population_size_source"] == "derived"
    assert first["migration"]["neighbours_out"]["0"] == [1]


def test_evaluation_records_carry_complete_trajectory_provenance(completed_run):
    summary, _config, records = completed_run
    evaluations = _evaluations(records)
    assert evaluations

    known_ids = {event.individual_id for event in evaluations}
    for event in evaluations:
        assert event.genome_hash
        assert event.operator
        assert isinstance(event.parent_ids, list)
        assert parse_individual_id(event.individual_id).island_id == event.island_id

    for event in evaluations:
        for parent in event.parent_ids:
            assert parent in known_ids, f"unlogged parent {parent}"


def test_initial_individuals_are_the_only_parentless_ones(completed_run):
    summary, config, records = completed_run
    evaluations = _evaluations(records)
    parentless = [event for event in evaluations if not event.parent_ids]
    assert all(event.operator == "init" for event in parentless)
    assert len(parentless) == config.resolved_population_size() * config.num_islands


def test_eval_index_is_gap_free_per_island(completed_run):
    """Downstream must be able to assert completeness, not hope for it."""
    summary, _config, records = completed_run
    per_island: dict[int, list[int]] = {}
    for event in _evaluations(records):
        per_island.setdefault(event.island_id, []).append(event.eval_index)
    for island, indices in per_island.items():
        assert sorted(indices) == list(range(len(indices))), island


def test_migration_edges_reconstruct_and_respect_the_topology(completed_run):
    summary, _config, records = completed_run
    edges = join_migration_edges(parse_records(records))
    assert edges

    for edge in edges:
        assert edge.dest_island == (edge.source_island + 1) % 4
        assert edge.send.num_migrants > 0

    delivered = [edge for edge in edges if edge.delivered]
    assert delivered, "at least some migrants must have been integrated"
    for edge in delivered:
        assert edge.arrive.origin_individual_ids == edge.send.migrant_individual_ids
        assert len(edge.arrive.arrived_individual_ids) == len(edge.send.migrant_individual_ids)
        assert edge.latency_seconds is not None


def test_a_migrant_arrival_is_evaluated_on_its_destination_island(completed_run):
    """The cross-island link: a new id here, with origin_individual_id pointing home."""
    summary, _config, records = completed_run
    arrivals = [
        event
        for event in _evaluations(records)
        if event.operator == "migration_arrival"
    ]
    assert arrivals
    for event in arrivals:
        assert event.origin_island != event.island_id
        assert event.origin_individual_id
        assert parse_individual_id(event.individual_id).island_id == event.island_id
        assert (
            parse_individual_id(event.origin_individual_id).island_id == event.origin_island
        )


def test_generation_end_summaries_are_complete_and_consistent(completed_run):
    summary, config, records = completed_run
    per_island: dict[int, list[dict]] = {}
    for record in records:
        if record["type"] == "generation_end":
            per_island.setdefault(record["island_id"], []).append(record)

    for island, records in per_island.items():
        assert [record["generation"] for record in records] == list(range(len(records)))
        for record in records:
            assert record["worst_fitness"] >= record["best_fitness"]
            assert record["best_fitness"] <= record["mean_fitness"] <= record["worst_fitness"]
            assert record["diversity_metric"]
            assert record["population_size"] == config.resolved_population_size()
            assert record["generations_since_improvement"] >= 0
        best_so_far = [record["best_so_far_fitness"] for record in records]
        assert best_so_far == sorted(best_so_far, reverse=True) or all(
            later <= earlier + 1e-12 for earlier, later in zip(best_so_far, best_so_far[1:])
        )


def test_island_end_and_run_end_agree(completed_run):
    summary, config, records = completed_run
    island_ends = {record["island_id"]: record for record in records if record["type"] == "island_end"}
    run_end = next(record for record in records if record["type"] == "run_end")

    assert set(island_ends) == set(range(config.num_islands))
    assert run_end["total_evaluations"] == sum(
        record["evaluations_total"] for record in island_ends.values()
    )
    assert run_end["global_best_fitness"] == min(
        record["best_fitness"] for record in island_ends.values()
    )


def _per_island_best(records: list[dict]) -> dict[int, float]:
    return {
        record["island_id"]: record["best_fitness"]
        for record in records
        if record["type"] == "island_end"
    }


def test_two_runs_of_the_same_config_differ(tmp_path, repository_root, run_broker):
    """Without a seed each island draws fresh OS entropy, so two runs search differently."""
    results = []
    for attempt in range(2):
        _summary, records = _run(_config(run_broker), run_broker)
        results.append(_per_island_best(records))
    assert results[0] != results[1]


_TIMING_FIELDS = {
    "run_id",
    "t_wall",
    "t_mono_ns",
    "wallclock_seconds",
    "node_id",
    "container_id",
    "hostname",
    "pid",
    "clock_offset_ns",
    "clock_offset_rtt_ns",
    "control_channel",
}
"""Fields that record when, where and over what a record was produced, which no seed can pin.
``island_end.control_channel`` is transport health: its count of records Redis had acknowledged is
a snapshot of the writer's thread, so two seeded runs can differ there (seen once in a full run)."""


def _evaluations_by_island(records: list[dict]) -> dict[int, list[dict]]:
    """Each island's evaluation records in its own order, minus identifiers and timestamps."""
    return _events_by_island(records, "evaluation")


def _events_by_island(records: list[dict], event_type: str) -> dict[int, list[dict]]:
    found: dict[int, list[dict]] = {}
    for record in records:
        if record["type"] == event_type and record.get("island_id") is not None:
            found.setdefault(record["island_id"], []).append(
                {key: value for key, value in record.items() if key not in _TIMING_FIELDS}
            )
    return found


@pytest.mark.parametrize(
    ("algorithm", "benchmark", "benchmark_params", "algorithm_params"),
    [
        ("de", "sphere", {"dimension": 5}, {}),
        ("pso", "ackley", {"dimension": 5}, {}),
        ("es", "sphere", {"dimension": 5}, {"offspring_per_parent": 3}),
        ("gp", "nguyen7", {}, {}),
        ("ga", "tsp", {"instance_file": "benchmarks/data/tsp/eil51.tsp"}, {}),
        (
            "ga",
            "knapsack",
            {"instance_file": "benchmarks/data/knapsack/uncorrelated_n100_R1000.kp"},
            {},
        ),
    ],
)
def test_a_seeded_run_without_migration_repeats_exactly(
    tmp_path, repository_root, run_broker, algorithm, benchmark, benchmark_params, algorithm_params
):
    """The seed's guarantee: same seed, same evaluations, record for record."""
    runs = []
    for attempt in range(2):
        config = _config(
            run_broker,
            algorithm=algorithm,
            algorithm_params=algorithm_params,
            benchmark=benchmark,
            benchmark_params=benchmark_params,
            evaluation_budget=600,
            target_generations=6,
            migration={"enabled": False},
            seed=20261026,
        )
        runs.append(_run(config, run_broker))

    (first_summary, first), (second_summary, second) = runs
    assert first_summary.run_id != second_summary.run_id
    assert _evaluations_by_island(first) == _evaluations_by_island(second)
    assert _events_by_island(first, "generation_end") == _events_by_island(second, "generation_end")
    assert _events_by_island(first, "island_end") == _events_by_island(second, "island_end")


def test_a_seeded_run_with_migration_repeats_until_migrants_arrive(
    tmp_path, repository_root, run_broker
):
    """Islands are asynchronous, so arrivals land on timing-dependent generations.

    What a seed still pins: every island's seed, and everything before the first
    migrant can have arrived -- generation 0 is the part that is guaranteed.
    """
    runs = [
        _run(_config(run_broker, seed=7), run_broker)[1]
        for attempt in range(2)
    ]
    starts = [_events_by_island(records, "island_start") for records in runs]
    seeds = [
        {island: records[0]["island_seed"] for island, records in found.items()} for found in starts
    ]
    assert seeds[0] == seeds[1]
    assert len(set(seeds[0].values())) == len(seeds[0]), "each island gets its own stream"

    initial = [
        {
            island: [row for row in rows if row["generation"] == 0]
            for island, rows in _evaluations_by_island(records).items()
        }
        for records in runs
    ]
    assert initial[0] == initial[1]


def test_different_seeds_give_different_runs(tmp_path, repository_root, run_broker):
    results = [
        _per_island_best(
            _run(
                _config(run_broker, seed=seed, migration={"enabled": False}),
                run_broker,
            )[1]
        )
        for seed in (1, 2)
    ]
    assert results[0] != results[1]


def test_the_dataset_is_identical_across_runs_even_though_the_search_is_not(
    tmp_path, repository_root, run_broker
):
    """The distinction the design draws: random search, fixed problem."""
    provenance = []
    for attempt in range(2):
        _summary, records = _run(_config(run_broker), run_broker)
        provenance.append(records[0]["datasets"])
    assert provenance[0] == provenance[1]


def test_islands_search_independently(tmp_path, repository_root, run_broker):
    """Two islands drawing the same random numbers would defeat the island model."""
    config = _config(
        run_broker, migration={"enabled": False, "interval": 1}
    )
    _summary, records = _run(config, run_broker)
    first_genomes: dict[int, str] = {}
    for event in _evaluations(records):
        if event.eval_index == 0:
            first_genomes[event.island_id] = event.genome_hash
    assert len(set(first_genomes.values())) == len(first_genomes)


def test_stagnation_stops_an_island_before_its_budget_is_spent(tmp_path, repository_root, run_broker):
    """The intended stopping condition: a converged island stops adding rows."""
    config = _config(
        run_broker,
        benchmark="sphere",
        benchmark_params={"dimension": 2},
        evaluation_budget=20_000,
        target_generations=500,
        termination={"stagnation_generations": 3, "max_generations": 2000},
        migration={"enabled": False, "interval": 1},
    )
    summary, records = _run(config, run_broker)

    reasons = {entry["termination_reason"] for entry in summary.per_island.values()}
    assert "stagnation" in reasons
    assert summary.total_evaluations < config.evaluation_budget

    ends = [r for r in records if r["type"] == "island_end"]
    stagnated = [r for r in ends if r["termination_reason"] == "stagnation"]
    assert stagnated
    for record in stagnated:
        assert record["generations_since_improvement"] >= 3


def test_stagnation_can_be_disabled_so_a_run_fills_its_budget(tmp_path, repository_root, run_broker):
    config = _config(
        run_broker,
        termination={"stagnation_generations": 0, "max_generations": 1000},
    )
    summary = Master(config, settings_for(run_broker)).run()
    assert summary.total_evaluations >= 0.85 * config.evaluation_budget
    reasons = {entry["termination_reason"] for entry in summary.per_island.values()}
    assert "stagnation" not in reasons


def test_generations_since_improvement_is_tracked_per_generation(
    tmp_path, repository_root, run_broker
):
    config = _config(run_broker, termination={"stagnation_generations": 0})
    _summary, records = _run(config, run_broker)
    island_0 = [r for r in records if r["type"] == "generation_end" and r["island_id"] == 0]
    counters = [r["generations_since_improvement"] for r in island_0]
    assert counters[0] == 0
    assert 0 in counters[1:]
    assert all(later - earlier <= 1 for earlier, later in zip(counters, counters[1:]))


def test_a_run_with_migration_disabled_produces_no_migration_events(
    tmp_path, repository_root, run_broker
):
    config = _config(
        run_broker,
        migration={"enabled": False, "interval": 1},
    )
    summary, records = _run(config, run_broker)
    counts = validate_log(records)["counts"]
    assert counts.get("migration_send", 0) == 0
    assert counts.get("migration_arrive", 0) == 0
    assert summary.islands_completed == config.num_islands


@pytest.mark.parametrize(
    ("algorithm", "benchmark", "benchmark_params", "algorithm_params"),
    [
        ("pso", "ackley", {"dimension": 5}, {}),
        ("es", "sphere", {"dimension": 5}, {"offspring_per_parent": 3}),
        ("gp", "nguyen7", {}, {}),
        ("ga", "tsp", {"instance_file": "benchmarks/data/tsp/eil51.tsp"}, {}),
        ("ga", "tsp", {"instance_file": "benchmarks/data/tsp/berlin52.tsp"}, {}),
        (
            "ga",
            "knapsack",
            {"instance_file": "benchmarks/data/knapsack/uncorrelated_n100_R1000.kp"},
            {},
        ),
        (
            "ga",
            "knapsack",
            {"instance_file": "benchmarks/data/knapsack/weakly_correlated_n100_R1000.kp"},
            {},
        ),
        ("gp", "yacht", {}, {}),
    ],
)
def test_every_algorithm_runs_distributed_and_logs_validly(
    tmp_path, repository_root, run_broker, algorithm, benchmark, benchmark_params, algorithm_params
):
    config = _config(
        run_broker,
        algorithm=algorithm,
        algorithm_params=algorithm_params,
        benchmark=benchmark,
        benchmark_params=benchmark_params,
        evaluation_budget=600,
        target_generations=6,
    )
    summary, records = _run(config, run_broker)
    assert summary.islands_failed == 0
    report = validate_log(records)
    assert report["counts"]["evaluation"] > 0
    encodings = {event.genome_encoding for event in _evaluations(records)}
    assert len(encodings) == 1, "one island algorithm produces one genome encoding"
    assert summary.total_evaluations <= config.evaluation_budget


def test_an_island_can_be_run_standalone_for_debugging(tmp_path, island_image, run_broker):
    """One island, no master: the image's entrypoint must not require a control plane."""
    config = _config(run_broker, target_generations=4)
    run_id = "run-20261027T120000Z-5ad1a10e"

    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "host",
            "-e", f"ISLAND_CONFIG={config.model_dump_json()}",
            "-e", f"REDIS_URL={run_broker.url}",
            island_image,
            "--run-id", run_id,
            "--island-id", "0",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    types = {record["type"] for record in read_stream(run_broker.url, stream_key(run_id))}
    assert {"island_start", "evaluation", "generation_end", "island_end"} <= types
    assert "migration_send" not in types


def test_a_supplied_run_id_is_used_everywhere(tmp_path, repository_root, run_broker):
    """REQS: the backend mints the id, and the runner uses it as-is in every record."""
    from orchestrator.backends import DockerBackend

    supplied = "run-20261026T120000Z-feedf00d"
    summary, records = _run(_config(run_broker, run_id=supplied), run_broker)

    assert summary.run_id == supplied
    assert summary.stream_key == f"run:{supplied}:runner_logs"
    assert {record["run_id"] for record in records} == {supplied}
    containers = {record["container_id"] for record in records if record["type"] == "island_start"}
    assert containers == {DockerBackend.container_name(supplied, island) for island in range(4)}
    assert all("feedf00d" in name for name in containers)
