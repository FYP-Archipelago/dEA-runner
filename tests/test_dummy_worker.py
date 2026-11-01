"""The dummy Worker Pool worker used by the local compose stack."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import uuid
from pathlib import Path

import pytest

from archipelago_logging import RecordKind
from orchestrator.redis_stream import RedisStreamWriter, stream_key

pytestmark = pytest.mark.docker

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dummy_worker.py"
_spec = importlib.util.spec_from_file_location("dummy_worker", _SCRIPT)
dummy_worker = importlib.util.module_from_spec(_spec)
sys.modules["dummy_worker"] = dummy_worker
_spec.loader.exec_module(dummy_worker)


def _record(record_type: str, emitter: str, seq: int, **fields) -> str:
    return json.dumps({"type": record_type, "emitter": emitter, "seq": seq, **fields})


def _write_run(url: str, key: str, evaluations: int = 3) -> None:
    writer = RedisStreamWriter(url, key, buffer_records=100, stall_seconds=10)
    writer.write(_record("run_start", "master", 0), RecordKind.EVENT)
    for seq in range(evaluations):
        writer.write(
            _record("evaluation", "island:0", seq, island_id=0, generation=0, fitness=1.5 * seq,
                    operator="init"),
            RecordKind.ROW,
        )
    writer.write(_record("run_end", "master", 1), RecordKind.EVENT)
    writer.close()


def test_the_dummy_worker_prints_every_record_and_a_summary_per_run(broker):
    prefix = f"test-{uuid.uuid4().hex}"
    keys = [stream_key(f"{prefix}-{n}") for n in range(2)]
    for key in keys:
        _write_run(broker.url, key)

    out = io.StringIO()
    client = broker.client()
    dummy_worker.follow(client, pattern=f"run:{prefix}-*:runner_logs", out=out, stop_after_runs=2)
    printed = out.getvalue()

    for key in keys:
        assert f"=== following {key}" in printed
        assert printed.count(f"{key} island:0 seq=") == 3
        assert f"=== {key} ended: 5 records" in printed
    assert "every emitter in seq order" in printed
    assert "evaluation gen=0 fitness=1.5 op=init" in printed


def test_the_dummy_worker_flags_a_record_after_the_terminator(broker):
    key = stream_key(f"test-{uuid.uuid4().hex}")
    _write_run(broker.url, key, evaluations=1)
    broker.client().xadd(key, {"kind": "event", "data": _record("diagnostic", "master", 2)})

    out = io.StringIO()
    client = broker.client()
    dummy_worker.follow(client, pattern=key, out=out, stop_after_runs=1)
    # The late record arrives in the same read as run_end, so one pass shows both.
    assert f"{key} !! AFTER THE TERMINATOR: master seq=2 diagnostic" in out.getvalue()


def test_the_backends_abort_ends_a_run_whatever_its_shape(broker):
    """A worker treats the first terminator as final; abort is the backend's, not the Runner's."""
    key = stream_key(f"test-{uuid.uuid4().hex}")
    client = broker.client()
    client.xadd(key, {"kind": "event", "data": _record("run_start", "master", 0)})
    client.xadd(key, {"kind": "event", "data": "not json"})
    client.xadd(key, {"abort": "runner missing"})

    out = io.StringIO()
    dummy_worker.follow(client, pattern=key, out=out, stop_after_runs=1)
    printed = out.getvalue()
    assert "(data is not JSON)" in printed
    assert f'{key} {{"abort": "runner missing"}}' in printed
    assert f"=== {key} ended" in printed
