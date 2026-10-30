"""Island container entrypoint.

The master starts one of these per island. The run's configuration arrives as JSON in the
``ISLAND_CONFIG`` environment variable, records go straight to Redis, and the island's result goes
back to the master in its goodbye message. Nothing is read from or written to disk.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import traceback
from typing import Any

from orchestrator.config import ISLAND_CONFIG_ENV, RunConfig
from orchestrator.island_runner import IslandResult, IslandRunner

__all__ = ["main", "run_island"]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="orchestrator.island",
        description=f"Run a single Archipelago island, configured by ${ISLAND_CONFIG_ENV}. "
        "Normally launched by the master, but runnable by hand for debugging one island.",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--island-id", type=int, required=True)
    parser.add_argument(
        "--control-host",
        default=None,
        help="Master control host. Omit to run this island standalone, with no migration.",
    )
    parser.add_argument("--control-port", type=int, default=0)
    return parser.parse_args(argv)


def run_island(
    config: RunConfig,
    *,
    run_id: str,
    island_id: int,
    control_address: tuple[str, int] | None,
) -> IslandResult:
    """Run one island to completion and return its result."""
    runner = IslandRunner(config, island_id, run_id, control_address=control_address)

    def _handle_stop(signum: int, _frame: Any) -> None:
        runner.request_stop(f"received signal {signum}")

    for received in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(received, _handle_stop)
        except (ValueError, OSError):
            pass

    return runner.run()


def main(argv: list[str] | None = None) -> int:
    """Run one island; exit non-zero if it failed."""
    args = _parse_args(argv)
    control_address = (
        (args.control_host, args.control_port) if args.control_host and args.control_port else None
    )
    try:
        config = RunConfig.model_validate_json(os.environ[ISLAND_CONFIG_ENV])
        result = run_island(
            config, run_id=args.run_id, island_id=args.island_id, control_address=control_address
        )
    except Exception:
        # The island never got as far as reporting; the master reads this from the
        # container's log when the container exits without a goodbye.
        traceback.print_exc()
        return 1
    if result.error is not None:
        print(f"island {args.island_id} failed: {result.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
