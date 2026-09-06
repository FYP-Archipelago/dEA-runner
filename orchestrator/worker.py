"""Island worker entrypoint."""

from __future__ import annotations

import argparse
import dataclasses
import json
import signal
import sys
from pathlib import Path
from typing import Any

from orchestrator.config import RunConfig, load_config
from orchestrator.island_runner import IslandRunner

__all__ = ["main", "run_worker"]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="orchestrator.worker",
        description="Run a single Archipelago island. Normally launched by the master, "
        "but runnable by hand for debugging one island in isolation.",
    )
    parser.add_argument("--config", required=True, help="Path to the resolved run config YAML.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--island-id", type=int, required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--control-host",
        default=None,
        help="Master control host. Omit to run this island standalone, with no migration.",
    )
    parser.add_argument("--control-port", type=int, default=0)
    return parser.parse_args(argv)


def run_worker(
    config: RunConfig,
    *,
    run_id: str,
    island_id: int,
    output_dir: Path,
    control_address: tuple[str, int] | None,
) -> dict[str, Any]:
    """Run one island and return its result as a plain dict."""
    runner = IslandRunner(
        config,
        island_id,
        run_id,
        output_dir=output_dir,
        control_address=control_address,
    )

    def _handle_stop(signum: int, _frame: Any) -> None:
        runner.config.termination.max_generations = min(
            runner.config.termination.max_generations, max(1, runner.ctx.generation)
        )
        runner.logger.diagnostic(
            f"received signal {signum}; winding down at the end of this generation",
            level="warning",
        )

    for received in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(received, _handle_stop)
        except (ValueError, OSError):
            pass

    return dataclasses.asdict(runner.run())


def main(argv: list[str] | None = None) -> int:
    """Run one island and write its result file."""
    args = _parse_args(argv)
    output_dir = Path(args.output_dir)
    control_address = (
        (args.control_host, args.control_port) if args.control_host and args.control_port else None
    )

    config = load_config(args.config)

    result_path = output_dir / "islands" / f"island-{args.island_id:02d}.result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = run_worker(
            config,
            run_id=args.run_id,
            island_id=args.island_id,
            output_dir=output_dir,
            control_address=control_address,
        )
    except Exception as exc:
        import traceback

        result_path.write_text(
            json.dumps(
                {
                    "island_id": args.island_id,
                    "termination_reason": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        traceback.print_exc()
        return 1

    result_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return 0 if result.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
