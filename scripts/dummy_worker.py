#!/usr/bin/env python3
"""A dummy Worker Pool worker: follows every run's stream and prints what lands in it.

Stands in for the Archipelago Backend's clustering workers in the local compose stack. It
depends only on redis-py, as a real worker would, and reads ``run:*:raw`` streams without a
consumer group: it watches, and takes nothing from anyone.

Each evaluation row is printed as one short line (they are almost all of a run's volume); every
other record is printed in full. When a run's terminator arrives (``run_end``, or the backend's
``abort``), a summary follows: records by type, evaluations per island, and whether every
emitter's ``seq`` arrived as 0, 1, 2, ... A record arriving after the terminator breaks the
stream contract, and is flagged.

    REDIS_URL=redis://:password@127.0.0.1:6380/0 python scripts/dummy_worker.py

``STREAM_PATTERN`` narrows what it watches, e.g. to one run: ``run:run-2026...-1a2b3c4d:raw``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TextIO

import redis

PATTERN = "run:*:raw"
_BLOCK_MS = 1000
_BATCH = 500


@dataclass
class _Run:
    """What the dummy worker has seen of one run so far."""

    last_id: str = "0"
    ended: bool = False
    types: Counter = field(default_factory=Counter)
    evaluations: Counter = field(default_factory=Counter)
    next_seq: dict[str, int] = field(default_factory=dict)
    out_of_order: int = 0


def describe(fields: dict[str, str], run: _Run) -> tuple[str, bool]:
    """One printable line for a stream entry, and whether it ends the run."""
    if set(fields) != {"kind", "data"}:
        return json.dumps(fields), fields.get("kind") == "abort" or "abort" in fields.values()
    record = json.loads(fields["data"])
    record_type = record.get("type", "?")
    emitter = record.get("emitter", "?")
    run.types[record_type] += 1
    expected = run.next_seq.get(emitter, 0)
    if record.get("seq") != expected:
        run.out_of_order += 1
    run.next_seq[emitter] = record.get("seq", expected) + 1
    if fields["kind"] == "row":
        run.evaluations[record.get("island_id")] += 1
        line = (
            f"{emitter} seq={record.get('seq')} evaluation gen={record.get('generation')} "
            f"fitness={record.get('fitness'):.6g} op={record.get('operator')}"
        )
    else:
        line = f"{emitter} seq={record.get('seq')} {record_type} {fields['data']}"
    return line, record_type in ("run_end", "abort")


def summary(key: str, run: _Run) -> str:
    """The closing report for one run."""
    islands = ", ".join(f"{island}: {count}" for island, count in sorted(run.evaluations.items()))
    order = "every emitter in seq order" if run.out_of_order == 0 else f"{run.out_of_order} out of order"
    return (
        f"=== {key} ended: {sum(run.types.values())} records "
        f"({', '.join(f'{name} {count}' for name, count in sorted(run.types.items()))}); "
        f"evaluations by island {{{islands}}}; {order}"
    )


def follow(
    client: redis.Redis,
    *,
    pattern: str = PATTERN,
    out: TextIO = sys.stdout,
    stop_after_runs: int | None = None,
) -> None:
    """Follow every stream matching ``pattern`` until stopped, or until ``stop_after_runs`` have ended."""
    runs: dict[str, _Run] = {}
    ended = 0
    while True:
        for key in client.scan_iter(match=pattern, _type="stream"):
            if key not in runs:
                runs[key] = _Run()
                print(f"=== following {key}", file=out, flush=True)
        if not runs:
            time.sleep(_BLOCK_MS / 1000)
            continue
        positions = {key: run.last_id for key, run in runs.items()}
        for key, entries in client.xread(positions, count=_BATCH, block=_BLOCK_MS) or []:
            run = runs[key]
            for entry_id, fields in entries:
                run.last_id = entry_id
                line, terminal = describe(fields, run)
                if run.ended:
                    print(f"{key} !! AFTER THE TERMINATOR: {line}", file=out, flush=True)
                    continue
                print(f"{key} {line}", file=out, flush=True)
                if terminal:
                    run.ended = True
                    ended += 1
                    print(summary(key, run), file=out, flush=True)
        if stop_after_runs is not None and ended >= stop_after_runs:
            return


def main() -> int:
    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    client = redis.Redis.from_url(url, decode_responses=True)
    while True:
        try:
            client.ping()
            break
        except redis.ConnectionError as exc:
            print(f"waiting for Redis: {exc}", file=sys.stderr, flush=True)
            time.sleep(1)
    pattern = os.environ.get("STREAM_PATTERN", PATTERN)
    print(f"dummy worker watching {pattern}", flush=True)
    follow(client, pattern=pattern)
    return 0


if __name__ == "__main__":
    sys.exit(main())
