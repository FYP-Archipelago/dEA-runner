"""The Runner's HTTP API end to end: real master containers, real islands, real Redis.

The service runs in-process, on the host, so each run's master container shares the host's
network, and so do its islands; the session's runner and island images are used.
"""

from __future__ import annotations

import contextlib
import io
import socket
import threading
import time

import docker
import pytest
from fastapi.testclient import TestClient

from archipelago_logging import new_run_id, validate_log
from orchestrator.redis_stream import read_stream, stream_key
from service import create_app

from conftest import Broker, settings_for

pytestmark = [pytest.mark.slow, pytest.mark.docker, pytest.mark.usefixtures("runner_image")]

SHORT = {"evaluation_budget": 2000}
LONG = ("full_dataset", {"evaluation_budget": 250_000})
"""A run that cannot end on its own within any of these tests: it takes minutes."""


@contextlib.contextmanager
def _service(broker: Broker, **settings):
    with TestClient(create_app(settings_for(broker, **settings))) as client:
        yield client


def _post(client: TestClient, preset: str = "docker_de_sphere", overrides=None, run_id=None):
    run_id = run_id or new_run_id()
    response = client.post(
        "/runs", json={"run_id": run_id, "preset": preset, "overrides": dict(overrides or {})}
    )
    return run_id, response.status_code, response.json()


def _wait_for(client: TestClient, run_id: str, *states: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        view = client.get(f"/runs/{run_id}").json()
        if view.get("state") in states:
            return view
        assert time.monotonic() < deadline, f"{run_id} never reached {states}: {view}"
        time.sleep(0.5)


def _wait_for_records(broker: Broker, run_id: str, at_least: int = 50, timeout: float = 120.0) -> None:
    client = broker.client()
    deadline = time.monotonic() + timeout
    while client.xlen(stream_key(run_id)) < at_least:
        assert time.monotonic() < deadline, f"{run_id} never got going"
        time.sleep(0.2)


def _records(broker: Broker, run_id: str) -> list[dict]:
    return list(read_stream(broker.url, stream_key(run_id)))


def _containers(run_id: str) -> list:
    return docker.from_env().containers.list(all=True, filters={"label": f"archipelago.run_id={run_id}"})


def test_a_run_goes_from_post_to_finished_with_a_valid_stream(run_broker):
    with _service(run_broker) as client:
        run_id, status, body = _post(client, overrides=SHORT)
        assert status == 202
        assert body == {"run_id": run_id, "state": "running", "error": None}
        assert _wait_for(client, run_id, "finished", "failed") == {
            "run_id": run_id, "state": "finished", "error": None,
        }

        records = _records(run_broker, run_id)
        report = validate_log(records)
        assert report["islands"] == [0, 1, 2, 3, 4]
        assert records[0]["type"] == "run_start" and records[-1]["type"] == "run_end"
        assert records[-1]["termination_reason"] == "completed"
        assert records[0]["config"]["_runtime"]["backend"]["network"] == "host"
        assert records[0]["config"]["evaluation_budget"] == 2000
        assert _containers(run_id) == [], "the master and its islands are removed"


def test_a_run_without_migration_migrates_nothing(run_broker):
    """The baseline the backend asked for: migration.enabled false through the API."""
    with _service(run_broker) as client:
        run_id, status, _ = _post(client, overrides={**SHORT, "migration.enabled": False, "seed": 7})
        assert status == 202
        assert _wait_for(client, run_id, "finished", "failed")["state"] == "finished"
    records = _records(run_broker, run_id)
    validate_log(records)
    assert records[0]["migration"]["enabled"] is False
    assert records[0]["config"]["migration"]["enabled"] is False
    assert not [r for r in records if r["type"] in ("migration_send", "migration_arrive")]
    assert records[-1]["total_migration_events"] == 0


def test_a_repeat_is_idempotent_and_a_different_request_conflicts(run_broker):
    with _service(run_broker) as client:
        run_id, _, _ = _post(client, overrides=SHORT)
        _wait_for(client, run_id, "finished")

        again = _post(client, overrides=SHORT, run_id=run_id)
        assert again[1:] == (202, {"run_id": run_id, "state": "finished", "error": None})
        assert sum(r["type"] == "run_start" for r in _records(run_broker, run_id)) == 1

        status, body = _post(client, overrides={"evaluation_budget": 3000}, run_id=run_id)[1:]
        assert status == 409
        assert body["error"] == f"run_id {run_id!r} was already used with a different preset or overrides"


def test_a_stream_the_service_has_no_record_of_is_a_conflict(run_broker):
    """After a restart, a retried POST must never start a second run into an old stream."""
    run_id = new_run_id()
    run_broker.client().xadd(stream_key(run_id), {"kind": "event", "data": "{}"})
    with _service(run_broker) as client:
        status, body = _post(client, run_id=run_id)[1:]
    assert status == 409
    assert "already holds 1 entries, but this Runner has no record of the run" in body["error"]
    assert run_broker.client().xlen(stream_key(run_id)) == 1


def test_a_third_run_waits_for_one_of_the_two_slots(run_broker):
    with _service(run_broker) as client:
        runs = [_post(client, overrides=SHORT) for _ in range(3)]
        assert [body["state"] for _, _, body in runs] == ["running", "running", "queued"]
        for run_id, _, _ in runs:
            assert _wait_for(client, run_id, "finished", "failed")["state"] == "finished"


def test_cancelling_queued_and_running_runs(run_broker):
    with _service(run_broker, max_concurrent_runs=1) as client:
        running, _, body = _post(client, *LONG)
        assert body["state"] == "running"
        queued, _, body = _post(client, overrides=SHORT)
        assert body["state"] == "queued"
        waiting, _, _ = _post(client, overrides=SHORT)

        # A queued run is cancelled at once and never writes a stream.
        response = client.post(f"/runs/{queued}/cancel")
        assert (response.status_code, response.json()["state"]) == (200, "cancelled")
        assert run_broker.client().xlen(stream_key(queued)) == 0

        # A running run winds down: 202 while it does, then cancelled.
        _wait_for_records(run_broker, running)
        response = client.post(f"/runs/{running}/cancel")
        assert (response.status_code, response.json()["state"]) == (202, "running")
        assert _wait_for(client, running, "cancelled", "failed", "finished")["state"] == "cancelled"
        records = _records(run_broker, running)
        validate_log(records)
        assert records[-1]["type"] == "run_end"
        assert records[-1]["termination_reason"] == "aborted"
        assert sorted(r["island_id"] for r in records if r["type"] == "island_end") == [0, 1, 2, 3, 4]

        # The freed slot goes to the run still waiting.
        assert _wait_for(client, waiting, "finished", "failed")["state"] == "finished"

        # Cancelling a terminal run changes nothing; an unknown run is a 404.
        response = client.post(f"/runs/{waiting}/cancel")
        assert (response.status_code, response.json()["state"]) == (200, "finished")
        unknown = new_run_id()
        assert client.post(f"/runs/{unknown}/cancel").status_code == 404
        assert client.get(f"/runs/{unknown}").json() == {"error": f"unknown run {unknown!r}"}


def test_a_run_that_cannot_start_its_islands_fails_with_the_reason(run_broker):
    with _service(run_broker, island_image="archipelago-island:does-not-exist") as client:
        run_id, status, _ = _post(client, overrides=SHORT)
        assert status == 202
        view = _wait_for(client, run_id, "failed", "finished")
    assert view["state"] == "failed"
    assert "the run's master stopped without ending the run's stream (exit code 1)" in view["error"]
    assert "island image 'archipelago-island:does-not-exist' not found" in view["error"]
    assert _containers(run_id) == []


def test_the_time_limit_stops_a_run_and_fails_it_even_after_a_restart(run_broker):
    """The backend treats run_end's time_limit as a failure; so does the Runner, always."""
    failed = {"state": "failed", "error": "stopped by the Runner's time limit of 5 s"}
    with _service(run_broker, run_time_limit_seconds=5.0) as client:
        run_id, _, _ = _post(client, *LONG)
        assert _wait_for(client, run_id, "failed", "finished", "cancelled") == {"run_id": run_id, **failed}
    records = _records(run_broker, run_id)
    validate_log(records)
    assert records[-1]["termination_reason"] == "time_limit"
    assert records[-1]["error"] == failed["error"]

    # A restarted service, with another limit, answers from run_end alone.
    with _service(run_broker, run_time_limit_seconds=900.0) as restarted:
        assert restarted.get(f"/runs/{run_id}").json() == {"run_id": run_id, **failed}


def test_a_restarted_service_keeps_running_runs_and_loses_queued_ones(run_broker):
    with _service(run_broker, max_concurrent_runs=1) as first:
        running, _, _ = _post(first, *LONG)
        queued, _, body = _post(first, overrides=SHORT)
        assert body["state"] == "queued"
    _wait_for_records(run_broker, running)

    with _service(run_broker, max_concurrent_runs=1) as second:
        assert second.get(f"/runs/{running}").json()["state"] == "running"
        assert second.get(f"/runs/{queued}").status_code == 404
        # The request survives the restart, in the master container's labels.
        assert _post(second, *LONG, run_id=running)[1:] == (
            202, {"run_id": running, "state": "running", "error": None},
        )
        assert _post(second, "full_dataset", run_id=running)[1] == 409
        assert second.post(f"/runs/{running}/cancel").status_code == 202
        assert _wait_for(second, running, "cancelled", "failed", "finished")["state"] == "cancelled"

    with _service(run_broker) as third:
        # Nothing in memory and no container left: the stream's run_end answers.
        assert third.get(f"/runs/{running}").json() == {
            "run_id": running, "state": "cancelled", "error": None,
        }


def test_a_failed_run_the_service_never_saw_is_answered_from_run_end(run_broker):
    """After a restart the stream is all there is: run_end's error is the run's error."""
    import json

    run_id = new_run_id()
    key = stream_key(run_id)
    for record in (
        {"type": "run_start", "seq": 0},
        {"type": "diagnostic", "seq": 1, "level": "error", "message": "island 2 failed: boom"},
        {"type": "run_end", "seq": 2, "termination_reason": "error", "error": "island 2 failed: boom"},
    ):
        run_broker.client().xadd(key, {"kind": "event", "data": json.dumps(record)})
    with _service(run_broker) as client:
        assert client.get(f"/runs/{run_id}").json() == {
            "run_id": run_id, "state": "failed", "error": "island 2 failed: boom",
        }
        response = client.post(f"/runs/{run_id}/cancel")
        assert (response.status_code, response.json()["state"]) == (200, "failed")


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        # The backend's abort, exactly as it writes one, after the master's run_end.
        ([("event", {"type": "run_end", "seq": 9, "termination_reason": "completed"}),
          ("abort", {"error": "runner unreachable: ConnectError"})],
         {"state": "finished", "error": None}),
        # The same after a failed run: its reason still comes from run_end.
        ([("event", {"type": "run_end", "seq": 9, "termination_reason": "error", "error": "island 1 failed: boom"}),
          ("abort", {"error": "runner answered 503"})],
         {"state": "failed", "error": "island 1 failed: boom"}),
        # An entry in a shape nobody defined, after run_end.
        ([("event", {"type": "run_end", "seq": 9, "termination_reason": "completed"}), ("raw", None)],
         {"state": "finished", "error": None}),
        # An abort with no run_end: the Runner does not know how the run ended.
        ([("event", {"type": "generation_end", "seq": 8}), ("raw", None)], None),
        # Not JSON at all.
        ([("event", {"type": "generation_end", "seq": 8}), ("event", "not json")], None),
    ],
)
def test_entries_the_runner_did_not_write_are_looked_past(run_broker, tail, expected):
    import json

    run_id = new_run_id()
    for kind, record in tail:
        fields = {"abort": "runner missing"} if record is None else {
            "kind": kind, "data": record if isinstance(record, str) else json.dumps(record),
        }
        run_broker.client().xadd(stream_key(run_id), fields)
    with _service(run_broker) as client:
        response = client.get(f"/runs/{run_id}")
    if expected is None:
        assert response.status_code == 404
    else:
        assert response.json() == {"run_id": run_id, **expected}


def test_health_is_ok_with_redis_reachable(run_broker):
    with _service(run_broker) as client:
        response = client.get("/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_the_dummy_backend_drives_a_real_server(run_broker, repository_root):
    """The service as deployed (uvicorn, a real port), driven over HTTP as the backend would."""
    import importlib.util
    import sys

    import uvicorn

    spec = importlib.util.spec_from_file_location("dummy_backend", repository_root / "scripts" / "dummy_backend.py")
    dummy_backend = importlib.util.module_from_spec(spec)
    sys.modules["dummy_backend"] = dummy_backend
    spec.loader.exec_module(dummy_backend)

    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings_for(run_broker)), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            assert time.monotonic() < deadline, "the server never started"
            time.sleep(0.1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dummy_backend.main([
                "--runner-url", f"http://127.0.0.1:{port}", "--redis-url", run_broker.url,
                "run", "docker_de_sphere", "--set", "evaluation_budget=2000", "--set", "seed=7",
            ])
        printed = out.getvalue()
        assert code == 0, printed
        assert '-> 202 {"run_id": "run-' in printed
        assert "  finished" in printed
        assert "stream valid: run:run-" in printed
    finally:
        server.should_exit = True
        thread.join(timeout=30)
