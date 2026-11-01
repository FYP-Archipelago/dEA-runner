"""How a run ends when it does not simply finish."""

from __future__ import annotations

import time

import pytest

from archipelago_logging import new_run_id
from orchestrator.config import RunConfig
from orchestrator.master import Master
from orchestrator.redis_stream import read_stream, stream_key

from conftest import Broker, settings_for

pytestmark = [pytest.mark.slow, pytest.mark.docker]


def _config(broker: Broker, **overrides) -> RunConfig:
    base = {
        "run_id": new_run_id(),
        "algorithm": "de",
        "benchmark": "rastrigin",
        "benchmark_params": {"dimension": 10},
        "evaluation_budget": 200_000,
        "target_generations": 1000,
        "num_islands": 4,
        "termination": {"stagnation_generations": 0, "max_generations": 5000},
        "migration": {"topology": "ring", "interval": 5, "num_migrants": 2},
    }
    base.update(overrides)
    return RunConfig(**base)


def test_a_wall_clock_limit_stops_the_run_cleanly(tmp_path, repository_root, island_image, run_broker):
    """The time limit is the one condition that ends a healthy run early."""
    config = _config(run_broker, termination={"stagnation_generations": 0, "max_generations": 100000})
    settings = settings_for(run_broker, run_time_limit_seconds=8.0)
    started = time.monotonic()
    summary = Master(config, settings).run()
    elapsed = time.monotonic() - started

    assert elapsed < 8.0 + settings.bye_timeout_seconds + 30
    assert summary.termination_reason == "time_limit"
    assert summary.islands_failed == 0
    records = list(read_stream(run_broker.url, summary.stream_key))
    assert records[-1]["type"] == "run_end", "nothing may follow run_end"
    island_ends = [r for r in records if r["type"] == "island_end"]
    assert sorted(r["island_id"] for r in island_ends) == list(range(config.num_islands)), (
        "every island winds down and writes island_end before run_end"
    )
    assert {r["termination_reason"] for r in island_ends} <= {"aborted", "time_limit"}


def test_an_island_that_raises_reports_the_error_rather_than_vanishing(
    tmp_path, repository_root, monkeypatch, run_broker
):
    """An island failure must appear in the log, not only on a lost stderr."""
    from orchestrator import island_runner

    config = _config(run_broker)
    run_id = "run-20261027T120000Z-e1404e40"
    runner = island_runner.IslandRunner(config, settings_for(run_broker), 0, run_id)

    def explode(_population):
        raise RuntimeError("deliberate failure for the error path")

    monkeypatch.setattr(runner.algorithm, "step", explode)
    result = runner.run()

    assert result.termination_reason == "error"
    assert "deliberate failure" in result.error
    records = list(read_stream(run_broker.url, stream_key(run_id)))
    assert any(
        record["type"] == "diagnostic" and record["level"] == "error" for record in records
    )
    assert any(record["type"] == "island_end" for record in records), (
        "an island_end must be written even on failure, so the log ends cleanly"
    )


def _launch_island_0_with(monkeypatch, *, config=None, settings=None):
    """Launch island 0 with its config JSON passed through ``config`` and its settings updated
    with ``settings``; the other islands as normal."""
    from orchestrator.backends import DockerBackend

    original = DockerBackend.launch

    def launch(self, spec):
        if spec.island_id != 0:
            return original(self, spec)
        if config is not None:
            spec.config_json = config(spec.config_json)
        shared = self.settings
        self.settings = shared.model_copy(update=settings or {})
        try:
            return original(self, spec)
        finally:
            self.settings = shared

    monkeypatch.setattr(DockerBackend, "launch", launch)


def test_an_island_that_dies_before_reporting_is_explained_by_its_container_log(
    repository_root, island_image, run_broker, monkeypatch
):
    """No goodbye means no result; the container's last output says what happened."""
    _launch_island_0_with(monkeypatch, config=lambda _json: "this is not a config")
    config = _config(run_broker, evaluation_budget=2000, target_generations=10)
    summary = Master(config, settings_for(run_broker)).run()

    assert summary.termination_reason == "error"
    assert summary.islands_failed == 1
    failed = summary.island_results[0]
    assert "exited without reporting a result (container exit code 1)" in failed["error"]
    assert "ValidationError" in failed["error"]

    records = list(read_stream(run_broker.url, summary.stream_key))
    diagnostics = [r for r in records if r["type"] == "diagnostic" and r.get("level") == "error"]
    assert any("Traceback" in d["context"].get("log_tail", "") for d in diagnostics)
    assert records[-1]["type"] == "run_end"


def test_an_island_that_cannot_reach_redis_says_so_over_the_control_channel(
    repository_root, island_image, run_broker, monkeypatch
):
    import socket

    with socket.create_server(("127.0.0.1", 0)) as probe:
        closed_port = probe.getsockname()[1]

    _launch_island_0_with(
        monkeypatch,
        settings={
            "redis_url": f"redis://127.0.0.1:{closed_port}/0",
            "broker_connect_timeout_seconds": 2.0,
        },
    )
    config = _config(run_broker, evaluation_budget=2000, target_generations=10)
    summary = Master(config, settings_for(run_broker)).run()

    failed = summary.island_results[0]
    assert failed["termination_reason"] == "error"
    assert failed["error"].startswith("BrokerError: cannot reach Redis within 2 s")
    assert summary.islands_failed == 1
    assert all(result["error"] is None for result in summary.island_results[1:])


def _run_in_background(config, settings):
    """Start a run on a thread; returns the thread and a dict that will hold its outcome."""
    import threading

    outcome: dict = {}

    def target():
        try:
            outcome["summary"] = Master(config, settings).run()
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


def _wait_for_evaluations(broker, run_id: str, at_least: int, timeout: float = 120.0) -> None:
    client = broker.client()
    deadline = time.monotonic() + timeout
    while client.xlen(stream_key(run_id)) < at_least:
        assert time.monotonic() < deadline, "the run never got going"
        time.sleep(0.2)


def test_an_island_killed_mid_run_ends_the_run_cleanly_with_the_reason(
    repository_root, island_image, run_broker
):
    import docker

    from orchestrator.backends import DockerBackend

    run_id = "run-20261027T120000Z-0dead001"
    config = _config(run_broker, run_id=run_id, evaluation_budget=200_000, target_generations=400)
    thread, outcome = _run_in_background(config, settings_for(run_broker))
    _wait_for_evaluations(run_broker, run_id, 2000)
    docker.from_env().containers.get(DockerBackend.container_name(run_id, 0)).kill()
    thread.join(timeout=180)

    summary = outcome["summary"]
    assert summary.termination_reason == "error"
    assert "island 0 failed: island 0 exited without reporting a result (container exit code 137)" in summary.error
    records = list(read_stream(run_broker.url, summary.stream_key))
    assert records[-1]["type"] == "run_end"
    assert records[-2]["type"] == "diagnostic" and records[-2]["level"] == "error"
    assert records[-2]["message"] == summary.error
    assert records[-1]["error"] == summary.error
    assert not any(r["type"] == "island_end" and r["island_id"] == 0 for r in records)


def test_an_island_that_never_says_goodbye_is_killed_after_the_grace_period(
    repository_root, island_image, run_broker, monkeypatch
):
    """A hung island must not hold the run: the barrier waits shutdown_grace_seconds, no more."""
    import json

    def slow_generations(config_json):
        config = json.loads(config_json)
        config["evaluation_budget"] = 50_000_000
        config["population_size"] = 400_000  # one generation outlasts the grace period
        return json.dumps(config)

    _launch_island_0_with(monkeypatch, config=slow_generations)
    config = _config(run_broker, termination={"stagnation_generations": 0, "max_generations": 100000})
    settings = settings_for(run_broker, run_time_limit_seconds=6.0, bye_timeout_seconds=3.0)
    started = time.monotonic()
    summary = Master(config, settings).run()
    assert time.monotonic() - started < 60

    records = list(read_stream(run_broker.url, summary.stream_key))
    assert any(
        r["type"] == "diagnostic" and "did not say goodbye within 3 s" in r["message"] for r in records
    )
    assert summary.termination_reason == "error"
    assert "island 0 failed" in summary.error
    assert records[-1]["type"] == "run_end"
