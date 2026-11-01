"""Run configuration: validation and derived values."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.config import RunConfig


def _base(**overrides) -> dict:
    return {
        "run_id": "run-20261031T120000Z-0000c0f1",
        "algorithm": "de",
        "benchmark": "sphere",
        "num_islands": 5,
        "evaluation_budget": 10_000,
        "target_generations": 20,
        "migration": {"interval": 5},
        **overrides,
    }


def test_a_minimal_config_validates():
    config = RunConfig(**_base())
    assert config.algorithm == "de" and config.num_islands == 5


def test_unknown_top_level_keys_are_rejected():
    """A silently ignored key runs a sweep at the default while the file claims otherwise."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RunConfig(**_base(populaton_size=100))


def test_incompatible_algorithm_and_benchmark_are_rejected_at_config_time():
    with pytest.raises(ValidationError, match="cannot search"):
        RunConfig(**_base(algorithm="de", benchmark="tsp"))
    with pytest.raises(ValidationError, match="cannot search"):
        RunConfig(**_base(algorithm="gp", benchmark="sphere"))


def test_compatible_pairings_are_accepted():
    RunConfig(**_base(algorithm="ga", benchmark="knapsack"))
    RunConfig(**_base(algorithm="ga", benchmark="tsp"))
    RunConfig(**_base(algorithm="gp", benchmark="nguyen7"))
    RunConfig(**_base(algorithm="pso", benchmark="ackley"))
    RunConfig(**_base(algorithm="es", benchmark="schwefel"))


def test_unknown_names_are_rejected():
    with pytest.raises(ValidationError):
        RunConfig(**_base(algorithm="simulated_annealing"))
    with pytest.raises(ValidationError, match="unknown benchmark"):
        RunConfig(**_base(benchmark="himmelblau"))


def test_nested_operator_params_are_validated():
    with pytest.raises(ValidationError, match="unknown algorithm_params"):
        RunConfig(**_base(algorithm_params={"mutaton_rate": 0.1}))
    with pytest.raises(ValidationError, match="unknown benchmark_params"):
        RunConfig(**_base(benchmark_params={"dimensionn": 10}))


def test_the_island_count_is_fixed_to_the_project_range():
    for outside in (3, 7):
        with pytest.raises(ValidationError, match="num_islands"):
            RunConfig(**_base(num_islands=outside))
    for inside in (4, 5, 6):
        assert RunConfig(**_base(num_islands=inside)).num_islands == inside


def test_impossible_migration_settings_are_rejected():
    with pytest.raises(ValidationError, match="unknown migration topology"):
        RunConfig(**_base(migration={"topology": "hypercube", "interval": 2}))
    with pytest.raises(ValidationError, match="must be smaller than the per-island population"):
        RunConfig(**_base(population_size=10, migration={"num_migrants": 10, "interval": 2}))
    with pytest.raises(ValidationError, match="exceeds"):
        RunConfig(**_base(target_generations=10, migration={"interval": 50}))
    with pytest.raises(ValidationError, match="unknown migrant selection"):
        RunConfig(**_base(migration={"selection": "vibes", "interval": 2}))
    with pytest.raises(ValidationError, match="unknown migrant replacement"):
        RunConfig(**_base(migration={"replacement": "vibes", "interval": 2}))


def test_a_topology_impossible_for_the_island_count_is_rejected_before_launch():
    with pytest.raises(ValidationError):
        RunConfig(
            **_base(num_islands=5, migration={"topology": "random", "topology_params": {"degree": 9}, "interval": 2})
        )


def test_migration_can_be_disabled_for_a_control_run():
    """A no-migration run is the baseline every migration claim is measured against."""
    config = RunConfig(**_base(migration={"enabled": False, "interval": 999}))
    assert not config.migration.enabled


def test_termination_defaults_to_stagnation_rather_than_a_generation_count():
    config = RunConfig(**_base())
    assert config.termination.stagnation_generations > 0
    assert config.termination.max_generations >= config.target_generations


def test_stagnation_can_be_disabled():
    config = RunConfig(**_base(termination={"stagnation_generations": 0}))
    assert config.termination.stagnation_generations == 0


def test_there_is_no_synchronous_migration_mode():
    """It existed only to make runs with migration bit-reproducible. A seed covers runs
    without migration; with migration, comparable rather than identical is accepted."""
    from orchestrator.config import MigrationConfig

    assert "synchronous" not in MigrationConfig.model_fields


def test_there_are_no_telemetry_settings():
    from orchestrator.config import LoggingConfig

    assert not [name for name in LoggingConfig.model_fields if "telemetry" in name]


def test_population_is_derived_so_the_run_lands_near_the_budget():
    """The whole point: total evaluations across the archipelago approximate the budget."""
    config = RunConfig(**_base(evaluation_budget=100_000, target_generations=100, num_islands=5))
    plan = config.budget_plan()
    assert plan["population_size_source"] == "derived"
    assert 0.95 <= plan["projected_total_evaluations"] / 100_000 <= 1.0
    assert abs(plan["projected_generations_if_no_early_stop"] - 100) <= 5


@pytest.mark.parametrize("islands", [4, 5, 6])
@pytest.mark.parametrize("generations", [50, 100, 250])
def test_the_budget_holds_across_island_and_generation_counts(islands, generations):
    config = RunConfig(
        **_base(evaluation_budget=100_000, target_generations=generations, num_islands=islands)
    )
    plan = config.budget_plan()
    assert 0.9 <= plan["projected_total_evaluations"] / 100_000 <= 1.0


def test_the_budget_accounts_for_the_es_offspring_multiplier():
    """An ES evaluates lambda = 7 x mu per generation; without this it burns 7x the budget."""
    de = RunConfig(**_base(algorithm="de")).budget_plan()
    es = RunConfig(
        **_base(algorithm="es", algorithm_params={"offspring_per_parent": 7})
    ).budget_plan()

    assert es["offspring_multiplier"] == 7.0
    assert es["population_size"] < de["population_size"]
    assert abs(es["projected_total_evaluations"] - de["projected_total_evaluations"]) < 0.1 * 10_000


def test_an_explicit_population_size_overrides_the_derivation():
    config = RunConfig(**_base(population_size=37))
    plan = config.budget_plan()
    assert plan["population_size"] == 37
    assert plan["population_size_source"] == "explicit"


def test_a_tiny_budget_is_clamped_to_a_workable_population():
    """DE/rand/1 needs four individuals; a derived size below that cannot run."""
    config = RunConfig(
        **_base(
            evaluation_budget=100,
            target_generations=50,
            num_islands=5,
            migration={"interval": 5, "num_migrants": 1},
        )
    )
    assert config.resolved_population_size() >= 4


def test_de_rand_2_needs_a_larger_minimum_population():
    config = RunConfig(
        **_base(
            algorithm="de",
            algorithm_params={"strategy": "rand/2"},
            evaluation_budget=100,
            target_generations=50,
            migration={"interval": 5, "num_migrants": 1},
        )
    )
    assert config.resolved_population_size() >= 6


def test_a_budget_too_small_for_the_migrant_count_is_rejected_with_the_arithmetic():
    """The error has to name the numbers, or the fix is guesswork."""
    with pytest.raises(ValidationError, match="derived from evaluation_budget"):
        RunConfig(
            **_base(
                evaluation_budget=100,
                target_generations=50,
                migration={"interval": 5, "num_migrants": 5},
            )
        )


def test_budget_is_split_evenly_across_islands():
    config = RunConfig(**_base(evaluation_budget=10_000, num_islands=5))
    assert config.budget_per_island() == 2_000


def test_the_run_id_is_required_and_must_have_the_backends_shape():
    """The backend mints every run id; the Runner never makes one up."""
    document = _base()
    del document["run_id"]
    with pytest.raises(ValidationError, match="run_id"):
        RunConfig(**document)
    with pytest.raises(ValidationError, match="must look like run-YYYYMMDDTHHMMSSZ"):
        RunConfig(**_base(run_id="my-run"))


@pytest.mark.parametrize(
    "deployment",
    [
        {"broker": {"url": "redis://redis:6379/0"}},
        {"execution": {"heartbeat_interval_seconds": 1.0}},
        {"docker": {"image": "archipelago-island:latest"}},
        {"termination": {"time_limit_seconds": 60.0}},
    ],
)
def test_deployment_settings_are_not_part_of_a_run(deployment):
    """They are ServiceSettings, read from the environment, the same for every run."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RunConfig(**_base(**deployment))


def test_a_master_and_its_islands_receive_exactly_the_runs_config():
    """RUN_CONFIG and ISLAND_CONFIG carry the config as JSON."""
    config = RunConfig(**_base(seed=7, description="round trip"))
    assert RunConfig.model_validate_json(config.model_dump_json()) == config


def test_every_evaluation_row_must_carry_a_genome():
    """The clustering module positions rows by their genome, and needs every row."""
    with pytest.raises(ValidationError, match="Input should be 'compact' or 'full'"):
        RunConfig(**_base(logging={"genome_mode": "hashed"}))
    for removed in ({"genome_sample_every": 5}, {"log_evaluations": False}):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RunConfig(**_base(logging=removed))
    for mode in ("compact", "full"):
        assert RunConfig(**_base(logging={"genome_mode": mode})).logging.genome_mode.value == mode


def test_the_relay_settings_are_gone():
    """Records go straight to Redis; nothing is queued for or flushed to the master."""
    for removed in ({"network_queue_size": 1000}, {"flush_interval_seconds": 1.0}):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RunConfig(**_base(logging=removed))


def _torus(num_islands: int, rows: int | None = None) -> dict:
    params = {} if rows is None else {"rows": rows}
    return _base(
        num_islands=num_islands,
        migration={"topology": "torus", "interval": 5, "topology_params": params},
    )


@pytest.mark.parametrize(("num_islands", "rows"), [(4, None), (5, None), (6, 1), (6, 6)])
def test_a_torus_that_collapses_into_a_ring_is_rejected(num_islands, rows):
    """Otherwise run_start says torus while the islands migrate around a ring."""
    with pytest.raises(ValidationError, match="identical to bidirectional_ring"):
        RunConfig(**_torus(num_islands, rows))


@pytest.mark.parametrize("rows", [None, 2, 3])
def test_a_six_island_torus_is_accepted(rows):
    config = RunConfig(**_torus(6, rows))
    assert config.migration.topology == "torus"


def test_the_torus_rule_rejects_exactly_the_tori_that_are_rings():
    """The rule checks degree; this checks it against graph isomorphism, case by case."""
    import itertools

    from orchestrator.topology import build_topology

    def edges(topology):
        return {(a, b) for a, outs in topology.items() for b in outs}

    def isomorphic(first, second, count):
        target = edges(second)
        return any(
            {(order[a], order[b]) for a, b in edges(first)} == target
            for order in itertools.permutations(range(count))
        )

    for count in (4, 5, 6):
        ring = build_topology("bidirectional_ring", count)
        for rows in [None] + [d for d in range(1, count + 1) if count % d == 0]:
            params = {} if rows is None else {"rows": rows}
            is_ring = isomorphic(build_topology("torus", count, **params), ring, count)
            try:
                RunConfig(**_torus(count, rows))
                rejected = False
            except ValidationError:
                rejected = True
            assert rejected == is_ring, f"{count} islands, rows={rows}"
