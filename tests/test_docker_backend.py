"""Docker backend: one container per island, created programmatically per run."""

from __future__ import annotations


import pytest

from archipelago_logging import new_run_id, parse_records, validate_log
from orchestrator.config import RunConfig
from orchestrator.master import Master
from orchestrator.redis_stream import read_stream, stream_key

from conftest import TEST_ISLAND_IMAGE, settings_for

pytestmark = [pytest.mark.slow, pytest.mark.docker, pytest.mark.usefixtures("island_image")]

IMAGE = TEST_ISLAND_IMAGE


def _config(**overrides) -> RunConfig:
    base = {
        "run_id": new_run_id(),
        "algorithm": "de",
        "benchmark": "sphere",
        "benchmark_params": {"dimension": 5},
        "evaluation_budget": 600,
        "target_generations": 8,
        "num_islands": 4,
        "termination": {"stagnation_generations": 0, "max_generations": 100},
        "migration": {"topology": "ring", "interval": 2, "num_migrants": 2},
    }
    base.update(overrides)
    return RunConfig(**base)


@pytest.fixture(scope="module")
def docker_run(island_image, run_broker):
    config = _config()
    summary = Master(config, settings_for(run_broker)).run()
    return summary, config, list(read_stream(run_broker.url, summary.stream_key))


def test_all_islands_complete_in_containers(docker_run):
    summary, config, _records = docker_run
    assert summary.islands_completed == config.num_islands
    assert summary.islands_failed == 0


def test_the_container_log_satisfies_the_schema_contract(docker_run):
    """Every island container's records pass the log validator."""
    _summary, config, records = docker_run
    report = validate_log(records)
    assert set(report["islands"]) == set(range(config.num_islands))
    assert report["counts"]["evaluation"] > 0


def test_every_containers_evaluations_reach_the_stream(docker_run):
    """Each island container appends its own evaluation records to Redis."""
    summary, config, records = docker_run
    evaluations = [record for record in records if record["type"] == "evaluation"]
    assert {record["island_id"] for record in evaluations} == set(range(config.num_islands))
    assert len(evaluations) == summary.total_evaluations


def test_the_run_respects_its_budget_in_containers(docker_run):
    summary, config, _records = docker_run
    assert summary.total_evaluations <= config.evaluation_budget


def test_run_start_records_the_docker_backend(docker_run):
    _summary, _config, records = docker_run
    first = records[0]
    assert first["backend"] == "docker"
    assert first["config"]["_runtime"]["backend"]["image"] == IMAGE
    assert first["config"]["_runtime"]["backend"]["network"] == "host"
    assert first["config"]["_runtime"]["advertise_host"] == "127.0.0.1"


def test_islands_report_their_container_name_as_node_id(docker_run):
    """node_id must join against `docker ps`, or a record cannot be placed on a node."""
    from orchestrator.backends.docker_backend import DockerBackend

    summary, config, records = docker_run
    starts = [record for record in records if record["type"] == "island_start"]
    assert len(starts) == config.num_islands
    node_ids = {record["node_id"] for record in starts}
    assert len(node_ids) == config.num_islands, "each island must be on its own container"
    for record in starts:
        assert record["container_id"] == record["node_id"]
        assert record["node_id"] == DockerBackend.container_name(
            summary.run_id, record["island_id"]
        )


def test_migration_works_across_containers(docker_run):
    _summary, _config, records = docker_run
    from archipelago_logging import join_migration_edges

    edges = join_migration_edges(parse_records(records))
    assert edges
    assert any(edge.delivered for edge in edges), "migrants must cross the container boundary"


def test_containers_are_removed_after_the_run(docker_run):
    import docker

    summary, _config, _records = docker_run
    client = docker.from_env()
    leftovers = client.containers.list(
        all=True, filters={"label": f"archipelago.run_id={summary.run_id}"}
    )
    assert leftovers == [], "every island container must be removed after the run"


def test_run_start_records_the_service_settings_without_the_password(docker_run):
    _summary, _config, records = docker_run
    settings = records[0]["config"]["_runtime"]["settings"]
    assert settings["island_image"] == IMAGE
    assert settings["heartbeat_seconds"] == 0.5


def test_a_missing_island_image_fails_with_an_actionable_message(run_broker):
    """Images are built before runs start (compose builds both); the Runner never builds one."""
    from orchestrator.backends import DockerUnavailable

    settings = settings_for(run_broker, island_image="archipelago-does-not-exist:latest")
    config = _config()
    with pytest.raises(DockerUnavailable, match="not found. Build it with:  docker build"):
        Master(config, settings).run()
    # It failed before run_start, so nothing at all reaches the stream: no diagnostics either.
    assert run_broker.client().xlen(stream_key(config.run_id)) == 0
