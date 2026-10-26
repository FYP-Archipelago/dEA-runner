"""Command-line interface to the baseline dEA harness."""

from __future__ import annotations

import argparse
import json
import sys
from .config import load_config

__all__ = ["main"]

VOLUME_WARNING_MEGABYTES = 2000.0


def _add_config_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", "-c", default=None, help="Path to a run config YAML.")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY.PATH=VALUE",
        help="Override a config field. Repeatable. Values are parsed as JSON, "
        "e.g. --set migration.interval=5 --set benchmark_params='{\"dimension\": 50}'.",
    )


def _command_list(_args: argparse.Namespace) -> int:
    from algorithms.registry import available_algorithms
    from benchmarks.registry import available_benchmarks
    from orchestrator.migration import REPLACEMENT_POLICIES, SELECTION_POLICIES
    from orchestrator.topology import TOPOLOGIES

    print("Algorithms (name: searchable domains)")
    for name, domains in available_algorithms().items():
        print(f"  {name:6s} {', '.join(domains)}")

    print("\nBenchmarks (name: domain)")
    for name, domain in available_benchmarks().items():
        print(f"  {name:32s} {domain}")

    print("\nMigration topologies")
    print("  " + ", ".join(sorted(TOPOLOGIES)))
    print("\nMigrant selection policies")
    print("  " + ", ".join(sorted(SELECTION_POLICIES)))
    print("\nMigrant replacement policies")
    print("  " + ", ".join(sorted(REPLACEMENT_POLICIES)))
    return 0


def _print_plan(config) -> None:
    plan = config.budget_plan()
    print(f"  algorithm      {config.algorithm}  params={config.algorithm_params or '{}'}")
    print(f"  benchmark      {config.benchmark}  params={config.benchmark_params or '{}'}")
    print(f"  budget         {plan['evaluation_budget']:,} evaluations total"
          f"  ({plan['budget_per_island']:,} per island x {config.num_islands} islands)")
    print(f"  population     {plan['population_size']} per island ({plan['population_size_source']})"
          f"  -> {plan['evaluations_per_generation_per_island']} evaluations/generation")
    print(f"  generations    ~{plan['projected_generations_if_no_early_stop']} if nothing stops early"
          f"  (target {plan['target_generations']}, cap {plan['max_generations']})")
    print(f"  projected      {plan['projected_total_evaluations']:,} evaluations across the run")
    print(f"  stops on       stagnation after {config.termination.stagnation_generations} "
          f"generations without improvement, or the budget")
    print(f"  migration      {'off' if not config.migration.enabled else config.migration.topology}"
          f"  every {config.migration.interval} gen"
          f"  x{config.migration.num_migrants} ({config.migration.selection} -> "
          f"{config.migration.replacement}, fanout={config.migration.fanout})")
    print(f"  worker image   {config.docker.image}")


def _command_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config, overrides=args.overrides)
    print(f"OK: configuration is valid ({args.config or 'defaults'})")
    _print_plan(config)
    return 0


def _command_estimate(args: argparse.Namespace) -> int:
    config = load_config(args.config, overrides=args.overrides)
    estimate = config.estimate_log_volume()
    print(json.dumps(estimate, indent=2))
    if estimate["estimated_megabytes_on_disk"] > VOLUME_WARNING_MEGABYTES:
        print(
            f"\nWARNING: this configuration is projected to write "
            f"{estimate['estimated_megabytes_on_disk'] / 1000:.1f} GB.\n"
            "  Reduce it with a smaller evaluation_budget, or logging.genome_mode: compact\n"
            "  if it is set to full.",
            file=sys.stderr,
        )
    return 0


def _command_run(args: argparse.Namespace) -> int:
    from .master import Master

    config = load_config(args.config, overrides=args.overrides)
    estimate = config.estimate_log_volume()

    if estimate["estimated_megabytes_on_disk"] > VOLUME_WARNING_MEGABYTES and not args.yes:
        print(
            f"This configuration is projected to write about "
            f"{estimate['estimated_megabytes_on_disk'] / 1000:.1f} GB "
            f"({estimate['projected_total_evaluations']:,} evaluation rows).\n"
            "Re-run with --yes to proceed, or see `estimate` for how to reduce it.",
            file=sys.stderr,
        )
        return 2

    print(f"Starting run: {config.algorithm} on {config.benchmark}")
    _print_plan(config)
    print()
    summary = Master(config).run()

    print(f"\nRun {summary.run_id} finished: {summary.termination_reason} "
          f"in {summary.wallclock_seconds:.1f}s")
    print(f"  output           {summary.output_dir}")
    print(f"  events           {summary.aggregate_log}")
    print(f"  evaluations      {summary.evaluations_csv}")
    print(f"  islands          {summary.islands_completed} completed, {summary.islands_failed} failed")
    print(f"  evaluations      {summary.total_evaluations:,} "
          f"({100.0 * summary.total_evaluations / max(1, config.evaluation_budget):.1f}% of budget)")
    print(f"  migration events {summary.total_migration_events} "
          f"({summary.total_migrants} individuals)")
    print(f"  global best      {summary.global_best_fitness} (island {summary.global_best_island})")
    for island, entry in sorted(summary.per_island.items()):
        print(f"    island {island}  {entry.get('termination_reason', '?'):<24} "
              f"gen {entry.get('generation', 0):<5} "
              f"evals {entry.get('evaluations', 0):<7,} "
              f"best {entry.get('best_fitness')}")
    return 0 if summary.islands_failed == 0 else 1


def _command_verify_log(args: argparse.Namespace) -> int:
    from archipelago_logging.reader import LogValidationError, validate_log

    try:
        report = validate_log(args.path, require_run_end=not args.allow_incomplete)
    except LogValidationError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the chosen subcommand."""
    parser = argparse.ArgumentParser(
        prog="orchestrator.cli",
        description="Archipelago baseline distributed EA harness.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Execute a run.")
    _add_config_arguments(run_parser)
    run_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Proceed without confirming a large projected log volume.",
    )
    run_parser.set_defaults(handler=_command_run)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate a config without running anything."
    )
    _add_config_arguments(validate_parser)
    validate_parser.set_defaults(handler=_command_validate)

    estimate_parser = subparsers.add_parser(
        "estimate", help="Project the log volume a config would produce."
    )
    _add_config_arguments(estimate_parser)
    estimate_parser.set_defaults(handler=_command_estimate)

    list_parser = subparsers.add_parser(
        "list", help="List registered algorithms, benchmarks and migration policies."
    )
    list_parser.set_defaults(handler=_command_list)

    verify_parser = subparsers.add_parser(
        "verify-log",
        help="Check a run's logs against the schema's structural invariants. Reads the "
        "JSON Lines file and its companion evaluations.csv together, since `seq` "
        "spans both.",
    )
    verify_parser.add_argument("path", help="Path to a run's .jsonl file.")
    verify_parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Do not require a run_end record (for a run still in progress).",
    )
    verify_parser.set_defaults(handler=_command_verify_log)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
