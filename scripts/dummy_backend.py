#!/usr/bin/env python3
"""Stand in for the Archipelago Backend: start runs through the Runner's API and follow them.

    python scripts/dummy_backend.py presets
    python scripts/dummy_backend.py run docker_de_sphere
    python scripts/dummy_backend.py run full_dataset --set evaluation_budget=250000 --set seed=7
    python scripts/dummy_backend.py status <run id>
    python scripts/dummy_backend.py cancel <run id>

``run`` mints a run id as the backend does, starts the run, polls it every 2 s until it ends, then
checks the run's stream with ``validate_log``: every record present, each island in ``seq``
order, ``run_start`` first and ``run_end`` last. It exits 0 only for a finished run with a valid
stream.

The Runner is found from ``--runner-url``, else ``$RUNNER_URL``, else the compose stack's ``.env``
(``RUNNER_HOST_PORT``); Redis likewise from ``--redis-url``, ``$REDIS_URL`` or ``.env``
(``REDIS_PASSWORD``, ``REDIS_HOST_PORT``).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

TERMINAL = {"finished", "failed", "cancelled"}
POLL_SECONDS = 2.0


def _env_file() -> dict[str, str]:
    settings: dict[str, str] = {}
    path = REPOSITORY_ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.lstrip().startswith("#"):
                settings[key.strip()] = value.strip().strip("'\"")
    return settings


def runner_url(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/")
    if os.environ.get("RUNNER_URL"):
        return os.environ["RUNNER_URL"].rstrip("/")
    return f"http://127.0.0.1:{_env_file().get('RUNNER_HOST_PORT', '8000')}"


def redis_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("REDIS_URL"):
        return os.environ["REDIS_URL"]
    settings = _env_file()
    password = settings.get("REDIS_PASSWORD")
    auth = f":{password}@" if password else ""
    return f"redis://{auth}127.0.0.1:{settings.get('REDIS_HOST_PORT', '6380')}/0"


def new_run_id() -> str:
    """A run id in the form the backend mints."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{secrets.token_hex(4)}"


def parse_overrides(items: list[str]) -> dict[str, Any]:
    """``key=value`` pairs; a value that parses as JSON is used as JSON (numbers), else as text."""
    overrides: dict[str, Any] = {}
    for item in items:
        key, separator, raw = item.partition("=")
        if not separator:
            raise SystemExit(f"--set needs key=value, not {item!r}")
        try:
            overrides[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            overrides[key.strip()] = raw
    return overrides


def call(base: str, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """One request to the Runner: its status and decoded JSON body."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def verify(url: str, run_id: str) -> bool:
    """Check the run's stream; print what was found."""
    from archipelago_logging import LogValidationError, validate_log
    from orchestrator.redis_stream import read_stream, stream_key

    key = stream_key(run_id)
    try:
        report = validate_log(read_stream(url, key))
    except LogValidationError as exc:
        print(f"stream INVALID: {key}: {exc}")
        return False
    print(
        f"stream valid: {key}: {report['records']:,} records, "
        f"{report['total_evaluations']:,} evaluations, islands {report['islands']}"
    )
    return True


def follow(base: str, run_id: str) -> dict[str, Any]:
    """Poll a run every 2 s, printing each change of state, until it ends."""
    last = None
    while True:
        status, view = call(base, "GET", f"/runs/{run_id}")
        if status != 200:
            print(f"GET /runs/{run_id} -> {status} {view}")
            return {"run_id": run_id, "state": "failed", "error": view.get("error")}
        if view["state"] != last:
            last = view["state"]
            print(f"{time.strftime('%H:%M:%S')}  {run_id}  {last}" + (f": {view['error']}" if view["error"] else ""))
        if view["state"] in TERMINAL:
            return view
        time.sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runner-url", default=None)
    parser.add_argument("--redis-url", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("presets", help="List the presets and the overrides a run may set.")
    run = commands.add_parser("run", help="Start a run, follow it to the end, check its stream.")
    run.add_argument("preset")
    run.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    run.add_argument("--run-id", default=None, help="Default: a fresh one, as the backend mints.")
    run.add_argument("--no-wait", action="store_true", help="Start the run and return.")
    status = commands.add_parser("status", help="Show a run.")
    status.add_argument("run_id")
    cancel = commands.add_parser("cancel", help="Cancel a run.")
    cancel.add_argument("run_id")
    args = parser.parse_args(argv)
    base = runner_url(args.runner_url)

    if args.command == "presets":
        code, body = call(base, "GET", "/presets")
        print(json.dumps(body, indent=2))
        return 0 if code == 200 else 1
    if args.command in ("status", "cancel"):
        path = f"/runs/{args.run_id}" + ("/cancel" if args.command == "cancel" else "")
        code, body = call(base, "POST" if args.command == "cancel" else "GET", path)
        print(code, json.dumps(body))
        return 0 if code in (200, 202) else 1

    run_id = args.run_id or new_run_id()
    request = {"run_id": run_id, "preset": args.preset, "overrides": parse_overrides(args.overrides)}
    code, body = call(base, "POST", "/runs", request)
    print(f"POST /runs {json.dumps(request)} -> {code} {json.dumps(body)}")
    if code != 202 or args.no_wait:
        return 0 if code == 202 else 1
    view = follow(base, run_id)
    valid = verify(redis_url(args.redis_url), run_id)
    return 0 if valid and view["state"] == "finished" else 1


if __name__ == "__main__":
    sys.exit(main())
