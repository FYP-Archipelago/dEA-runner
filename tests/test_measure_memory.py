"""The M4.8 memory measurement script."""

from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure_memory.py"
_spec = importlib.util.spec_from_file_location("measure_memory", _SCRIPT)
measure_memory = importlib.util.module_from_spec(_spec)
sys.modules["measure_memory"] = measure_memory
_spec.loader.exec_module(measure_memory)

# Stands in for `docker compose run --rm master run ...`: appends one run, as the Runner would.
_FAKE_RUN = '''
import json, sys
import redis
url, key, evaluations, finish = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4] == "1"
client = redis.Redis.from_url(url)
def add(kind, record):
    client.xadd(key, {"kind": kind, "data": json.dumps(record)})
add("event", {"type": "run_start", "config": {"description": "fake", "algorithm": "de",
    "benchmark": "sphere", "evaluation_budget": evaluations, "num_islands": 4,
    "logging": {"genome_mode": "compact"}}})
for index in range(evaluations):
    add("row", {"type": "evaluation", "eval_index": index, "genome_repr": [0.5] * 10})
if finish:
    add("event", {"type": "run_end", "termination_reason": "completed", "wallclock_seconds": 1.5})
'''


@pytest.fixture
def fake_run(tmp_path):
    script = tmp_path / "fake_run.py"
    script.write_text(_FAKE_RUN, encoding="utf-8")

    def command(url: str, key: str, evaluations: int, *, finish: bool = True) -> str:
        return f"{sys.executable} {script} {url} {key} {evaluations} {int(finish)}"

    return command


def _prefix() -> str:
    return f"run:mm{uuid.uuid4().hex[:8]}"


@pytest.mark.docker
def test_a_run_started_by_the_script_is_measured(run_broker, fake_run):
    prefix = _prefix()
    key = f"{prefix}-a:runner_logs"
    result = measure_memory.measure(
        run_broker.url, runs=1, commands=[fake_run(run_broker.url, key, 500)], pattern=f"{prefix}*"
    )

    [run] = result["runs"]
    client = run_broker.client()
    assert run["stream"] == key
    assert (run["entries"], run["evaluations"]) == (502, 500)
    assert run["memory_usage_bytes"] == client.memory_usage(key, samples=0)
    assert run["bytes_per_entry"] == pytest.approx(run["memory_usage_bytes"] / 502)
    assert run["projected_bytes_at_500k"] == round(run["memory_usage_bytes"] / 500 * 500_000)
    assert (run["algorithm"], run["genome_mode"], run["termination_reason"]) == ("de", "compact", "completed")
    records = [json.loads(fields["data"]) for _, fields in client.xrange(key)]
    assert run["record_bytes"] == sum(len(json.dumps(r)) for r in records) + 500 * 3 + 2 * 5
    memory = result["redis"]
    assert memory["peak"] >= memory["after"] >= memory["before"] - 1_000_000
    assert memory["samples"] > 0


@pytest.mark.docker
def test_streams_that_existed_before_are_ignored(run_broker, fake_run):
    prefix = _prefix()
    subprocess.run(shlex.split(fake_run(run_broker.url, f"{prefix}-old:runner_logs", 10)), check=True)
    result = measure_memory.measure(
        run_broker.url,
        runs=1,
        commands=[fake_run(run_broker.url, f"{prefix}-new:runner_logs", 10)],
        pattern=f"{prefix}*",
    )
    assert [run["stream"] for run in result["runs"]] == [f"{prefix}-new:runner_logs"]


@pytest.mark.docker
def test_concurrent_commands_are_measured_together(run_broker, fake_run):
    prefix = _prefix()
    keys = [f"{prefix}-{name}:runner_logs" for name in ("a", "b")]
    result = measure_memory.measure(
        run_broker.url,
        runs=2,
        commands=[fake_run(run_broker.url, key, 100) for key in keys],
        pattern=f"{prefix}*",
    )
    assert sorted(run["stream"] for run in result["runs"]) == keys


@pytest.mark.docker
def test_commands_that_exit_before_enough_runs_end_fail_the_measurement(run_broker, fake_run):
    prefix = _prefix()
    with pytest.raises(SystemExit, match="exited after only 0 of 1 runs ended"):
        measure_memory.measure(
            run_broker.url,
            runs=1,
            commands=[fake_run(run_broker.url, f"{prefix}-x:runner_logs", 10, finish=False)],
            pattern=f"{prefix}*",
        )


def test_the_redis_url_comes_from_the_compose_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "# a comment\nREDIS_PASSWORD='s3cret'\nRUN_CONFIGS=config/a.yaml config/b.yaml\n"
        "REDIS_HOST_PORT=6390\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(measure_memory, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert measure_memory._redis_url(None) == "redis://:s3cret@127.0.0.1:6390/0"
    monkeypatch.setenv("REDIS_URL", "redis://elsewhere:6379/0")
    assert measure_memory._redis_url(None) == "redis://elsewhere:6379/0"
    assert measure_memory._redis_url("redis://given:1/0") == "redis://given:1/0"
