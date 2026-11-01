"""The stream contract, end to end: whole runs against Redis 7 and Valkey 8.

What the Archipelago Backend's Worker Pool relies on, checked on real runs:

- each island's records arrive in ``seq`` order, rows and events alike;
- ``run_start`` is the first entry and ``run_end`` the last, on the clean, time-limit and error
  paths, and a failed run says why in the diagnostic just before ``run_end``;
- a broker stall loses nothing; islands whose broker stays silent past the stall threshold fail
  the run with a readable reason;
- an unreachable or out-of-memory Redis fails the run before any island starts.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid

import pytest

from archipelago_logging import validate_log
from orchestrator.config import RunConfig
from orchestrator.master import Master
from orchestrator.redis_stream import BrokerError, read_stream, stream_key

from conftest import Broker, settings_for

pytestmark = [pytest.mark.slow, pytest.mark.docker, pytest.mark.usefixtures("island_image")]


def _config(**overrides) -> RunConfig:
    base = {
        "run_id": _run_id(),
        "algorithm": "de",
        "benchmark": "sphere",
        "benchmark_params": {"dimension": 5},
        "evaluation_budget": 2000,
        "target_generations": 10,
        "num_islands": 4,
        "termination": {"stagnation_generations": 0, "max_generations": 1000},
        "migration": {"topology": "ring", "interval": 2, "num_migrants": 2},
    }
    base.update(overrides)
    return RunConfig(**base)


def _run_id() -> str:
    return f"run-20261027T120000Z-{uuid.uuid4().hex[:8]}"


def _records(broker: Broker, run_id: str) -> list[dict]:
    return list(read_stream(broker.url, stream_key(run_id)))


def _assert_bounded(records: list[dict], num_islands: int, *, every_island_ended: bool = True) -> dict:
    """The contract every finished run keeps; returns validate_log's report."""
    report = validate_log(records)  # each emitter's seq and each island's eval_index exact
    assert records[0]["type"] == "run_start"
    assert records[-1]["type"] == "run_end"
    assert sum(1 for record in records if record["type"] == "run_end") == 1
    if every_island_ended:
        ended = sorted(r["island_id"] for r in records if r["type"] == "island_end")
        assert ended == list(range(num_islands))
    return report


def _start(config: RunConfig, settings) -> tuple[threading.Thread, dict]:
    outcome: dict = {}

    def target():
        try:
            outcome["summary"] = Master(config, settings).run()
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


def _wait_for_entries(broker: Broker, run_id: str, at_least: int, timeout: float = 120.0) -> None:
    client = broker.client()
    deadline = time.monotonic() + timeout
    while client.xlen(stream_key(run_id)) < at_least:
        assert time.monotonic() < deadline, "the run never got going"
        time.sleep(0.1)


class _FreezableProxy:
    """A TCP relay to Redis that can go silent: after ``freeze``, nothing is forwarded either
    way and no connection is closed, as in a network partition."""

    def __init__(self, upstream_port: int) -> None:
        self._upstream_port = upstream_port
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self.frozen = threading.Event()
        self._sockets: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def freeze(self) -> None:
        self.frozen.set()

    def close(self) -> None:
        self._listener.close()
        for sock in self._sockets:
            try:
                sock.close()
            except OSError:
                pass

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            upstream = socket.create_connection(("127.0.0.1", self._upstream_port))
            self._sockets += [client, upstream]
            threading.Thread(target=self._pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=self._pipe, args=(upstream, client), daemon=True).start()

    def _pipe(self, source: socket.socket, sink: socket.socket) -> None:
        try:
            while chunk := source.recv(65536):
                if self.frozen.is_set():
                    continue  # swallowed: the far side hears nothing, and nothing closes
                sink.sendall(chunk)
        except OSError:
            pass


def test_each_island_arrives_in_seq_order_and_the_run_is_bounded(broker):
    config = _config()
    summary = Master(config, settings_for(broker)).run()
    records = _records(broker, config.run_id)

    report = _assert_bounded(records, config.num_islands)
    assert summary.termination_reason == "completed"
    assert "error" not in records[-1], "a run that did not fail carries no error"
    assert report["total_evaluations"] == summary.total_evaluations
    for island in range(config.num_islands):
        seqs = [r["seq"] for r in records if r["emitter"] == f"island:{island}"]
        assert seqs == list(range(len(seqs))), island
        kinds = {r["type"] for r in records if r["emitter"] == f"island:{island}"}
        assert {"evaluation", "generation_end", "island_start", "island_end"} <= kinds


def test_a_time_limited_run_ends_with_run_end_last(broker):
    config = _config(
        evaluation_budget=2_000_000,
        target_generations=2000,
        termination={"stagnation_generations": 0, "max_generations": 100000},
    )
    summary = Master(config, settings_for(broker, run_time_limit_seconds=4.0)).run()
    records = _records(broker, config.run_id)

    _assert_bounded(records, config.num_islands)
    assert summary.termination_reason == "time_limit"
    assert records[-1]["termination_reason"] == "time_limit"
    assert records[-1]["error"] == summary.error == "stopped by the Runner's time limit of 4 s"


def test_a_run_whose_master_fails_says_why_just_before_run_end(broker, monkeypatch):
    def failing_supervise(self, started):
        time.sleep(2.0)
        raise RuntimeError("deliberate master failure")

    monkeypatch.setattr(Master, "_supervise", failing_supervise)
    config = _config(evaluation_budget=2_000_000, target_generations=2000)
    with pytest.raises(RuntimeError, match="deliberate master failure"):
        Master(config, settings_for(broker)).run()
    records = _records(broker, config.run_id)

    _assert_bounded(records, config.num_islands)
    assert records[-1]["termination_reason"] == "error"
    assert records[-2]["type"] == "diagnostic" and records[-2]["level"] == "error"
    assert records[-2]["message"] == "run failed: RuntimeError: deliberate master failure"
    assert records[-1]["error"] == records[-2]["message"], "the reason is in run_end too"


def test_a_broker_stall_slows_the_run_and_loses_nothing(broker):
    config = _config(evaluation_budget=40_000, target_generations=200)
    run_id = config.run_id
    thread, outcome = _start(config, settings_for(broker))
    _wait_for_entries(broker, run_id, 2000)
    broker.client().client_pause(3000, all=False)
    thread.join(timeout=300)

    summary = outcome["summary"]
    records = _records(broker, run_id)
    report = _assert_bounded(records, config.num_islands)
    assert summary.termination_reason == "completed"
    assert summary.islands_failed == 0
    assert report["total_evaluations"] == summary.total_evaluations


def test_islands_whose_broker_goes_silent_fail_the_run_with_the_reason(broker, monkeypatch):
    """The islands' Redis stays silent past stall_seconds; the master's does not."""
    from orchestrator.backends import DockerBackend

    proxy = _FreezableProxy(broker.port)
    original = DockerBackend.launch

    def launch_behind_the_proxy(self, spec):
        shared = self.settings
        self.settings = shared.model_copy(update={"redis_url": f"redis://127.0.0.1:{proxy.port}/0"})
        try:
            return original(self, spec)
        finally:
            self.settings = shared

    monkeypatch.setattr(DockerBackend, "launch", launch_behind_the_proxy)
    config = _config(evaluation_budget=2_000_000, target_generations=2000)
    run_id = config.run_id
    try:
        thread, outcome = _start(config, settings_for(broker, broker_stall_seconds=2.0))
        _wait_for_entries(broker, run_id, 2000)
        proxy.freeze()
        thread.join(timeout=300)
    finally:
        proxy.close()

    summary = outcome["summary"]
    records = _records(broker, run_id)
    _assert_bounded(records, config.num_islands, every_island_ended=False)
    assert summary.termination_reason == "error"
    assert summary.islands_failed == config.num_islands
    assert f"no record reached Redis stream {stream_key(run_id)} for 2 s" in summary.error
    assert records[-2]["type"] == "diagnostic" and records[-2]["message"] == summary.error
    assert records[-1]["error"] == summary.error


def test_an_unreachable_broker_fails_the_run_before_anything_starts(broker):
    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]
    settings = settings_for(
        broker, redis_url=f"redis://127.0.0.1:{port}/0", broker_connect_timeout_seconds=1.0
    )
    with pytest.raises(BrokerError, match="cannot reach Redis within 1 s"):
        Master(_config(), settings).run()


def test_an_out_of_memory_broker_fails_the_run_before_any_island_starts(broker):
    """noeviction: a full Redis refuses writes, and the run fails rather than lose records."""
    import docker

    config = _config()
    run_id = config.run_id
    client = broker.client()
    client.config_set("maxmemory", 1)
    try:
        with pytest.raises(BrokerError, match="OutOfMemoryError"):
            Master(config, settings_for(broker, broker_stall_seconds=2.0)).run()
    finally:
        client.config_set("maxmemory", 0)
    assert client.xlen(stream_key(run_id)) == 0
    leftovers = docker.from_env().containers.list(
        all=True, filters={"label": f"archipelago.run_id={run_id}"}
    )
    assert leftovers == [], "no island container was started, or it was removed"
