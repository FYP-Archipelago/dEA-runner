"""The log format is the contract between this phase and every later one."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from archipelago_logging import (
    EvaluationEvent,
    EventType,
    MigrationArriveEvent,
    MigrationSendEvent,
    Operator,
    RunStartEvent,
    parse_event,
)
from archipelago_logging.events import EVENT_MODELS, IslandEvent

ENVELOPE_FIELDS = {"type", "run_id", "emitter", "seq", "t_wall", "t_mono_ns"}


def _minimal_envelope(**extra):
    return {
        "run_id": "run-test",
        "emitter": "island:0",
        "seq": 0,
        "t_wall": 1.0,
        "t_mono_ns": 2,
        **extra,
    }


def test_every_event_type_is_registered():
    """EventType and EVENT_MODELS must not drift apart."""
    assert set(EVENT_MODELS) == {member.value for member in EventType}


def test_every_model_emits_the_full_envelope():
    evaluation = EvaluationEvent(
        **_minimal_envelope(
            island_id=0,
            generation=0,
            individual_id="i00-g000000-n000000000",
            operator=Operator.INIT.value,
            fitness=1.0,
            eval_index=0,
            genome_encoding="real_vector",
            genome_hash="ab" * 16,
            genome_repr="AAAAAAAA8D8=",
            genome_repr_mode="compact",
        )
    )
    record = json.loads(evaluation.to_json_line())
    assert ENVELOPE_FIELDS <= set(record)
    assert "schema_version" not in record
    assert record["type"] == "evaluation"


def test_none_valued_fields_are_omitted_not_null():
    """Absent key == null is part of the contract; it is what makes 10^8 records affordable."""
    evaluation = EvaluationEvent(
        **_minimal_envelope(
            island_id=0,
            generation=0,
            individual_id="i00-g000000-n000000000",
            operator=Operator.INIT.value,
            fitness=1.0,
            eval_index=0,
            genome_encoding="real_vector",
            genome_hash="ab" * 16,
            genome_repr="AAAAAAAA8D8=",
            genome_repr_mode="compact",
        )
    )
    record = json.loads(evaluation.to_json_line())
    assert "objectives" not in record
    assert "feasible" not in record
    assert not any(value is None for value in record.values())


def test_parse_event_round_trips_every_type():
    for type_name, model in EVENT_MODELS.items():
        required = {
            "run_start": dict(
                started_at="now",
                harness_version="0.2.0",
                algorithm="de",
                benchmark="sphere",
                num_islands=5,
                population_size=10,
                evaluation_budget=1000,
                evaluation_budget_per_island=200,
                target_generations=20,
                max_generations=500,
                backend="docker",
                migration={},
                datasets={"benchmark": "sphere"},
                config={},
            ),
            "island_start": dict(
                algorithm="de",
                algorithm_params={},
                benchmark="sphere",
                genome_encoding="real_vector",
                benchmark_params={"name": "sphere"},
                population_size=10,
                island_seed=1,
                hostname="h",
                pid=1,
            ),
            "evaluation": dict(
                individual_id="i00-g000000-n000000000",
                operator="init",
                fitness=1.0,
                eval_index=0,
                genome_encoding="real_vector",
                genome_hash="ab" * 16,
                genome_repr="AAAAAAAA8D8=",
                genome_repr_mode="compact",
            ),
            "generation_end": dict(
                best_fitness=1.0,
                mean_fitness=1.0,
                worst_fitness=1.0,
                median_fitness=1.0,
                std_fitness=0.0,
                maximising=False,
                population_size=10,
                evaluations_this_generation=10,
                evaluations_total=10,
                wallclock_seconds=0.1,
            ),
            "migration_send": dict(
                migration_id="mig-00-01-000000",
                source_island=0,
                dest_island=1,
                source_generation=1,
                migrant_individual_ids=[],
                migrant_genome_hashes=[],
                migrant_fitnesses=[],
                num_migrants=0,
                topology="ring",
                selection_policy="best",
                migration_interval=5,
            ),
            "migration_arrive": dict(
                migration_id="mig-00-01-000000",
                source_island=0,
                dest_island=1,
                source_generation=1,
                dest_generation=2,
                origin_individual_ids=[],
                arrived_individual_ids=[],
                replacement_policy="worst",
                accepted=True,
            ),
            "island_end": dict(
                termination_reason="completed",
                generations_completed=1,
                evaluations_total=1,
                wallclock_seconds=1.0,
            ),
            "run_end": dict(
                ended_at="now",
                termination_reason="completed",
                wallclock_seconds=1.0,
                islands_completed=1,
                islands_failed=0,
                total_evaluations=1,
                total_migration_events=0,
                total_migrants=0,
            ),
            "diagnostic": dict(message="hello"),
        }[type_name]

        island = {"island_id": 0, "generation": 0} if issubclass(model, IslandEvent) else {}
        event = model(**_minimal_envelope(**island, **required))
        parsed = parse_event(json.loads(event.to_json_line()))
        assert type(parsed) is model, type_name
        assert parsed.type == type_name


def test_an_unknown_event_type_is_rejected():
    """Every record type is one this code defines; there are no older logs to tolerate."""
    with pytest.raises(ValidationError, match="some_future_event"):
        parse_event(_minimal_envelope(type="some_future_event"))


def test_an_undeclared_field_is_rejected():
    """A misspelled field name must fail where it is written, not reach the log."""
    with pytest.raises(ValidationError, match="best_fitnes"):
        parse_event(
            _minimal_envelope(
                type="generation_end",
                best_fitness=1.0,
                mean_fitness=1.0,
                worst_fitness=1.0,
                median_fitness=1.0,
                std_fitness=0.0,
                maximising=False,
                population_size=1,
                evaluations_this_generation=1,
                evaluations_total=1,
                wallclock_seconds=0.0,
                best_fitnes=1.0,
            )
        )


def test_migration_is_two_events_joined_by_one_id():
    """The send/arrive split is deliberate; the join key is what makes it usable."""
    send = MigrationSendEvent(
        **_minimal_envelope(
            island_id=0,
            generation=5,
            migration_id="mig-00-01-000000",
            source_island=0,
            dest_island=1,
            source_generation=5,
            migrant_individual_ids=["x"],
            migrant_genome_hashes=["h"],
            migrant_fitnesses=[1.0],
            num_migrants=1,
            topology="ring",
            selection_policy="best",
            migration_interval=5,
        )
    )
    arrive = MigrationArriveEvent(
        **_minimal_envelope(
            island_id=1,
            generation=9,
            migration_id="mig-00-01-000000",
            source_island=0,
            dest_island=1,
            source_generation=5,
            dest_generation=9,
            origin_individual_ids=["x"],
            arrived_individual_ids=["y"],
            replacement_policy="worst",
            accepted=True,
        )
    )
    assert send.migration_id == arrive.migration_id
    assert arrive.dest_generation - send.source_generation == 4


def test_run_start_carries_the_resolved_config_and_the_budget():
    """The log alone must describe how the run was sized and which dataset it used."""
    event = RunStartEvent(
        **_minimal_envelope(
            started_at="now",
            harness_version="0.2.0",
            algorithm="de",
            benchmark="sphere",
            num_islands=5,
            population_size=100,
            evaluation_budget=100_000,
            evaluation_budget_per_island=20_000,
            target_generations=200,
            max_generations=2000,
            backend="docker",
            migration={"topology": "ring"},
            config={"algorithm": "de"},
            datasets={"name": "berlin52", "instance_sha256": "ab" * 32},
        )
    )
    record = json.loads(event.to_json_line())
    assert record["evaluation_budget"] == 100_000
    assert record["evaluation_budget_per_island"] == 20_000
    assert record["migration"]["topology"] == "ring"
    assert record["datasets"]["instance_sha256"]


def test_run_start_has_no_seed_field():
    """Search reproducibility was removed; a seed field would imply otherwise."""
    assert "seed" not in RunStartEvent.model_fields


def test_device_metrics_is_gone():
    """Telemetry is not part of the record format (LOGGING_FORMAT.md §11)."""
    assert "device_metrics" not in EVENT_MODELS
    assert not any(member.value == "device_metrics" for member in EventType)


def _evaluation(**overrides) -> EvaluationEvent:
    fields = dict(
        individual_id="i02-g000004-n000000123",
        parent_ids=["i02-g000003-n000000001", "i02-g000003-n000000002"],
        operator=Operator.CROSSOVER.value,
        fitness=1.25,
        eval_index=123,
        genome_encoding="real_vector",
        genome_hash="ab" * 16,
        genome_repr=[0.5, 1.5],
        genome_repr_mode="full",
    )
    fields.update(overrides)
    return EvaluationEvent(**_minimal_envelope(island_id=2, generation=4, **fields))


def test_an_evaluation_round_trips_through_json_unchanged():
    original = _evaluation(
        objective=3.5,
        feasible=True,
        constraint_violation=0.0,
        is_island_best=True,
        genome_repr="AAAAgD8=",
        genome_repr_mode="compact",
        genome_precision_decimals=4,
        genome_dim=3,
        origin_island=1,
        origin_individual_id="i01-g000002-n000000005",
    )
    assert parse_event(json.loads(original.to_json_line())) == original


@pytest.mark.parametrize(
    "genome_repr",
    [[1.5, -2.25, 3.0], [1, 0, 1, 1], {"x": [1.0, 2.0], "v": [0.5, -0.5]}, "add(x,1.5)", "AAAAgD8="],
    ids=["real vector", "bitstring", "vector with state", "expression tree", "compact"],
)
def test_every_genome_shape_round_trips(genome_repr):
    """A consumer reads genome_repr as a string, a list or an object, by encoding."""
    original = _evaluation(genome_repr=genome_repr, genome_repr_mode="full")
    assert parse_event(json.loads(original.to_json_line())).genome_repr == genome_repr


def test_float_precision_survives_json():
    """A truncated fitness would quietly change every downstream statistic."""
    value = 0.1 + 0.2
    restored = json.loads(_evaluation(fitness=value).to_json_line())
    assert restored["fitness"] == value


def test_initial_individuals_have_an_empty_parent_list():
    record = json.loads(_evaluation(parent_ids=[], operator=Operator.INIT.value).to_json_line())
    assert record["parent_ids"] == []


def test_an_evaluation_row_must_carry_everything_the_contract_lists():
    """REQS: every row carries island_id, generation and a genome body, among others."""
    complete = _minimal_envelope(
        island_id=0,
        generation=0,
        individual_id="i00-g000000-n000000000",
        operator=Operator.INIT.value,
        fitness=1.0,
        eval_index=0,
        genome_encoding="real_vector",
        genome_hash="ab" * 16,
        genome_repr="AAAAAAAA8D8=",
        genome_repr_mode="compact",
    )
    EvaluationEvent(**complete)
    for field in ("island_id", "generation", "genome_repr", "genome_repr_mode"):
        with pytest.raises(ValidationError, match=field):
            EvaluationEvent(**{key: value for key, value in complete.items() if key != field})
    for empty in (None, "", []):
        with pytest.raises(ValidationError, match="must carry a genome body"):
            EvaluationEvent(**{**complete, "genome_repr": empty})


def test_island_records_say_which_island_and_generation_and_master_records_need_not():
    for type_name, model in EVENT_MODELS.items():
        fields = model.model_fields
        written_by_an_island = issubclass(model, IslandEvent)
        assert fields["island_id"].is_required() is written_by_an_island, type_name
        assert fields["generation"].is_required() is written_by_an_island, type_name
    assert not issubclass(EVENT_MODELS["diagnostic"], IslandEvent), "the master writes these too"


def test_run_end_carries_the_reason_a_run_failed():
    """The backend's workers read run_end first; the reason must be there, not only earlier."""
    import json

    from archipelago_logging import CallbackSink, EventLogger, new_run_id

    lines: list[str] = []
    logger = EventLogger(new_run_id(), "master", CallbackSink(lines.append))
    totals = dict(
        ended_at="2026-10-31T12:00:00Z", wallclock_seconds=1.0, islands_completed=3, islands_failed=1,
        total_evaluations=10, total_migration_events=0, total_migrants=0,
    )
    logger.run_end(termination_reason="error", error="island 2 failed: boom", **totals)
    logger.run_end(termination_reason="completed", **totals)
    failed, completed = (json.loads(line) for line in lines)
    assert failed["error"] == "island 2 failed: boom"
    assert "error" not in completed, "absent when the run did not fail"
