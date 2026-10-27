"""Command-line interface to the baseline dEA Runner."""

from __future__ import annotations

import argparse
import json
import sys
from .config import load_config, redact_url

__all__ = ["main"]

def _add_config_arguments(parser: argparse.ArgumentParser, *, many: bool = False) -> None:
    if many:
        parser.add_argument(
            "--config",
            "-c",
            dest="configs",
            action="extend",
            nargs="+",
            default=[],
            help="Run config YAML. Give several (repeated, or space-separated) to run them in turn.",
        )
    else:
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
    print(f"  island image   {config.docker.image}")
    print(f"  broker         {redact_url(config.broker.url)}")


def _command_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config, overrides=args.overrides)
    print(f"OK: configuration is valid ({args.config or 'defaults'})")
    _print_plan(config)
    return 0


def _command_run(args: argparse.Namespace) -> int:
    from .master import Master

    paths = args.configs or [None]
    configs = [load_config(path, overrides=args.overrides) for path in paths]

    outcomes: list[tuple[str, str, bool]] = []
    for number, (path, config) in enumerate(zip(paths, configs), start=1):
        if len(configs) > 1:
            print(f"=== Run {number} of {len(configs)}: {path}")
        print(f"Starting run: {config.algorithm} on {config.benchmark}")
        _print_plan(config)
        print()
        try:
            summary = Master(config).run()
        except Exception as exc:
            print(f"\nRun failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            outcomes.append((str(path), "failed to run", False))
            continue
        _print_summary(config, summary)
        outcomes.append(
            (
                f"{path} -> {summary.run_id}",
                f"{summary.termination_reason}, {summary.islands_failed} island(s) failed",
                summary.islands_failed == 0,
            )
        )

    if len(configs) > 1:
        print("\n=== All runs")
        for label, outcome, _ok in outcomes:
            print(f"  {label}: {outcome}")
    return 0 if all(ok for _, _, ok in outcomes) else 1


def _print_summary(config, summary) -> None:
    print(f"\nRun {summary.run_id} finished: {summary.termination_reason} "
          f"in {summary.wallclock_seconds:.1f}s")
    print(f"  records          {summary.stream_key}")
    print(f"  islands          {summary.islands_completed} completed, {summary.islands_failed} failed")
    print(f"  evaluations      {summary.total_evaluations:,} "
          f"({100.0 * summary.total_evaluations / max(1, config.evaluation_budget):.1f}% of budget)")
    print(f"  migration events {summary.total_migration_events} "
          f"({summary.total_migrants} individuals)")
    print(f"  global best      {summary.global_best_fitness} (island {summary.global_best_island})")
    if summary.error is not None:
        print(f"  error            {summary.error}")
    for island, entry in sorted(summary.per_island.items()):
        print(f"    island {island}  {entry.get('termination_reason', '?'):<24} "
              f"gen {entry.get('generation', 0):<5} "
              f"evals {entry.get('evaluations', 0):<7,} "
              f"best {entry.get('best_fitness')}")


def _command_verify_log(args: argparse.Namespace) -> int:
    from archipelago_logging import LogValidationError, validate_log

    from .redis_stream import read_stream, stream_key

    url = args.url or load_config().broker.url
    key = stream_key(args.run_id)
    try:
        report = validate_log(read_stream(url, key), require_run_end=not args.allow_incomplete)
    except LogValidationError as exc:
        print(f"INVALID: {key}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the chosen subcommand."""
    parser = argparse.ArgumentParser(
        prog="orchestrator.cli",
        description="Archipelago baseline dEA Runner.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Execute one run, or several in turn.")
    _add_config_arguments(run_parser, many=True)
    run_parser.set_defaults(handler=_command_run)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate a config without running anything."
    )
    _add_config_arguments(validate_parser)
    validate_parser.set_defaults(handler=_command_validate)

    list_parser = subparsers.add_parser(
        "list", help="List registered algorithms, benchmarks and migration policies."
    )
    list_parser.set_defaults(handler=_command_list)

    verify_parser = subparsers.add_parser(
        "verify-log",
        help="Check a run's records in Redis against the schema's structural invariants, "
        "including that every emitter's records arrived in seq order.",
    )
    verify_parser.add_argument("run_id", help="The run whose stream to check.")
    verify_parser.add_argument(
        "--url",
        default=None,
        help="Redis URL. Defaults to broker.url, which ARCHIPELAGO_BROKER__URL overrides.",
    )
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
