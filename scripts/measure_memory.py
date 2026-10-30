#!/usr/bin/env python3
"""Measure what runs cost in Redis memory.

Watches a Redis while runs append to it: samples ``INFO memory`` throughout and, once the
expected number of new ``run:*:raw`` streams have ended (``run_end`` is their last entry),
reports each stream's ``XLEN``, ``MEMORY USAGE``, record bytes and bytes per entry, and Redis's
``used_memory`` before, at its peak and after. Streams that existed before it started are
ignored, and nothing is deleted.

The runs are started either by hand, in another terminal, or by the script itself: each
``--start`` command is launched once the baseline is taken, all of them at the same time.

    python scripts/measure_memory.py --runs 1 \\
        --start "docker compose run --rm master run --config config/full_dataset.yaml"

    python scripts/measure_memory.py --runs 2 --json two.json \\
        --start "docker compose run --rm master run --config config/ga_tsp.yaml" \\
        --start "docker compose run --rm master run --config config/gp_yacht.yaml"

Redis is found from ``--redis-url``, else ``$REDIS_URL``, else the compose stack's ``.env``
(``REDIS_PASSWORD``, ``REDIS_HOST_PORT``), so the password need not go on a command line. Uses
only redis-py, like a Worker Pool worker would.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import redis

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PATTERN = "run:*:raw"
PROJECTED_BUDGET = 500_000


def _redis_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("REDIS_URL"):
        return os.environ["REDIS_URL"]
    settings: dict[str, str] = {}
    env_file = REPOSITORY_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.lstrip().startswith("#"):
                settings[key.strip()] = value.strip().strip("'\"")
    password = settings.get("REDIS_PASSWORD")
    port = settings.get("REDIS_HOST_PORT", "6380")
    auth = f":{password}@" if password else ""
    return f"redis://{auth}127.0.0.1:{port}/0"


def _memory(client: redis.Redis) -> dict[str, int]:
    info = client.info("memory")
    return {key: int(info[key]) for key in ("used_memory", "used_memory_peak", "maxmemory")}


class Sampler:
    """Samples ``used_memory`` on a thread, keeping the highest value seen."""

    def __init__(self, client: redis.Redis, interval: float) -> None:
        self._client = client
        self._interval = interval
        self._stop = threading.Event()
        self.peak = 0
        self.samples = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, int(self._client.info("memory")["used_memory"]))
            self.samples += 1
            self._stop.wait(self._interval)


def _streams(client: redis.Redis, pattern: str) -> set[str]:
    return {key.decode() for key in client.scan_iter(match=pattern, _type="stream", count=1000)}


def _last_type(client: redis.Redis, key: str) -> str | None:
    entries = client.xrevrange(key, count=1)
    if not entries:
        return None
    return json.loads(entries[0][1][b"data"]).get("type")


def _describe(client: redis.Redis, key: str) -> dict[str, Any]:
    """Everything measured about one finished run's stream."""
    length = client.xlen(key)
    usage = int(client.memory_usage(key, samples=0) or 0)
    record_bytes = 0
    evaluations = 0
    run_start: dict[str, Any] = {}
    cursor = "-"
    while True:
        entries = client.xrange(key, min=cursor, count=5000)
        for entry_id, fields in entries:
            data = fields[b"data"]
            record_bytes += len(data) + len(fields[b"kind"])
            if fields[b"kind"] == b"row":
                evaluations += 1
            elif not run_start:
                run_start = json.loads(data)
            last_id, last_data = entry_id, data
        if len(entries) < 5000:
            break
        cursor = "(" + last_id.decode()
    run_end = json.loads(last_data)
    config = run_start.get("config", {})
    return {
        "stream": key,
        "description": config.get("description"),
        "algorithm": config.get("algorithm"),
        "benchmark": config.get("benchmark"),
        "genome_mode": config.get("logging", {}).get("genome_mode"),
        "evaluation_budget": config.get("evaluation_budget"),
        "num_islands": config.get("num_islands"),
        "termination_reason": run_end.get("termination_reason"),
        "wallclock_seconds": run_end.get("wallclock_seconds"),
        "entries": length,
        "evaluations": evaluations,
        "memory_usage_bytes": usage,
        "record_bytes": record_bytes,
        "bytes_per_entry": usage / length if length else None,
        "bytes_per_evaluation": usage / evaluations if evaluations else None,
        "overhead_ratio": usage / record_bytes if record_bytes else None,
        "projected_bytes_at_500k": round(usage / evaluations * PROJECTED_BUDGET) if evaluations else None,
    }


def _mb(value: float | None) -> str:
    return "—" if value is None else f"{value / 1_000_000:,.1f} MB"


def _report(result: dict[str, Any]) -> None:
    redis_memory = result["redis"]
    print(f"\nRedis used_memory   before {_mb(redis_memory['before'])}   "
          f"peak {_mb(redis_memory['peak'])}   after {_mb(redis_memory['after'])}   "
          f"(grew {_mb(redis_memory['after'] - redis_memory['before'])}; "
          f"peak over before {_mb(redis_memory['peak'] - redis_memory['before'])})")
    print(f"                    maxmemory {redis_memory['maxmemory'] or 'unlimited'}, "
          f"{redis_memory['samples']} samples over {result['seconds']:.1f} s")
    for run in result["runs"]:
        print(f"\n{run['stream']}")
        print(f"  {run['algorithm']} on {run['benchmark']}  ({run['genome_mode']} genomes, budget "
              f"{run['evaluation_budget']:,}, {run['num_islands']} islands)  "
              f"{run['termination_reason']} in {run['wallclock_seconds']:.1f} s")
        print(f"  entries {run['entries']:,}  evaluations {run['evaluations']:,}")
        print(f"  MEMORY USAGE {_mb(run['memory_usage_bytes'])}  record bytes {_mb(run['record_bytes'])}  "
              f"overhead x{run['overhead_ratio']:.2f}")
        print(f"  {run['bytes_per_entry']:,.0f} bytes/entry  {run['bytes_per_evaluation']:,.0f} bytes/evaluation  "
              f"-> {_mb(run['projected_bytes_at_500k'])} at {PROJECTED_BUDGET:,} evaluations")


def measure(
    url: str,
    *,
    runs: int,
    commands: list[str],
    pattern: str = PATTERN,
    interval: float = 0.1,
    timeout: float = 3600.0,
) -> dict[str, Any]:
    """Take a baseline, start ``commands``, and wait for ``runs`` new streams to end."""
    client = redis.Redis.from_url(url)
    client.ping()
    existing = _streams(client, pattern)
    before = _memory(client)
    sampler = Sampler(client, interval)
    sampler.start()
    started = time.monotonic()
    processes = [subprocess.Popen(shlex.split(command), cwd=REPOSITORY_ROOT) for command in commands]

    ended: list[str] = []
    try:
        while True:
            # Checked before looking, so the last look comes after every command has exited.
            exited = bool(processes) and all(process.poll() is not None for process in processes)
            ended += [
                key
                for key in sorted(_streams(client, pattern) - existing - set(ended))
                if _last_type(client, key) == "run_end"
            ]
            if len(ended) >= runs:
                break
            if exited:
                raise SystemExit(f"the commands exited after only {len(ended)} of {runs} runs ended")
            if time.monotonic() - started > timeout:
                raise SystemExit(f"only {len(ended)} of {runs} runs ended within {timeout:g} s")
            time.sleep(0.5)
    finally:
        for process in processes:
            process.wait()
        sampler.stop()

    after = _memory(client)
    peak = sampler.peak
    if after["used_memory_peak"] > before["used_memory_peak"]:
        peak = max(peak, after["used_memory_peak"])  # a new all-time peak: exact, between samples
    return {
        "redis": {
            "before": before["used_memory"],
            "peak": max(peak, after["used_memory"]),
            "after": after["used_memory"],
            "maxmemory": before["maxmemory"],
            "samples": sampler.samples,
        },
        "seconds": time.monotonic() - started,
        "commands": commands,
        "runs": [_describe(client, key) for key in ended],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--redis-url", default=None, help="Default: $REDIS_URL, else from .env.")
    parser.add_argument("--runs", type=int, default=1, help="New run streams to wait for.")
    parser.add_argument(
        "--start",
        dest="commands",
        action="append",
        default=[],
        metavar="COMMAND",
        help="A command that starts runs, launched after the baseline. Repeat to run several at once.",
    )
    parser.add_argument("--pattern", default=PATTERN)
    parser.add_argument("--interval", type=float, default=0.1, help="Sampling period, seconds.")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--json", type=Path, default=None, help="Also write the results here.")
    args = parser.parse_args(argv)

    result = measure(
        _redis_url(args.redis_url),
        runs=args.runs,
        commands=args.commands,
        pattern=args.pattern,
        interval=args.interval,
        timeout=args.timeout,
    )
    _report(result)
    if args.json is not None:
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
