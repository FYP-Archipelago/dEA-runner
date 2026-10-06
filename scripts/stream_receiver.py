#!/usr/bin/env python3
"""Reference receiver for the Archipelago live run stream.

Listens on a TCP port and consumes a run as it happens. A stand-in for whatever
eventually reads the stream -- the clustering pipeline, a dashboard -- and a
worked example of the protocol in docs/STREAM_PROTOCOL.md.

    python scripts/stream_receiver.py --port 9900

Then, in another shell:

    python -m orchestrator.cli run -c config/smoke_local.yaml \\
        --set logging.live_stream.enabled=true

The stream is newline-delimited JSON. Every line is an object with a ``kind``:
``hello`` once at the start, then ``event`` (a run.jsonl record) and
``evaluation`` (an evaluations.csv row) interleaved as the run produces them,
then ``summary`` and ``bye``.

    --out DIR         rebuild the run's output directory from the stream alone
    --events TYPES    print only these event types, e.g. generation_end
    --raw             emit evaluations.csv on stdout, and nothing else
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import socketserver
import sys
import threading
import time
from pathlib import Path

_print_lock = threading.Lock()


def _emit(text: str) -> None:
    with _print_lock:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


class RunStreamHandler(socketserver.StreamRequestHandler):
    """Consumes one complete run from one connection."""

    every: int = 1
    show: list[str] | None = None
    quiet: bool = False
    raw: bool = False
    out_dir: Path | None = None
    events: set[str] | None = None

    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        self.columns: list[str] = []
        self.counts = {"event": 0, "evaluation": 0}
        self.by_type: dict[str, int] = {}
        self._files: dict[str, io.TextIOBase] = {}
        started = time.monotonic()

        if not (self.raw or self.quiet):
            _emit(f"\n=== connected: {peer} ===")

        try:
            for line in self.rfile:
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    _emit(f"  <not JSON, ignoring> {text[:120]}")
                    continue
                if not isinstance(message, dict):
                    continue
                self._dispatch(message)
        finally:
            self._close_files()

        elapsed = max(time.monotonic() - started, 1e-9)
        if not self.raw:
            _emit(
                f"=== {peer} closed: {self.counts['evaluation']:,} evaluations + "
                f"{self.counts['event']:,} events in {elapsed:.1f}s "
                f"({self.counts['evaluation'] / elapsed:,.0f} rows/s) ==="
            )

    # -- dispatch ----------------------------------------------------------- #

    def _dispatch(self, message: dict) -> None:
        kind = message.get("kind")
        if kind == "hello":
            self._hello(message)
        elif kind == "evaluation":
            self._evaluation(message.get("row", ""))
        elif kind == "event":
            self._event(message.get("record") or {})
        elif kind == "artifact":
            self._artifact(message.get("path", ""), message.get("content", ""))
        elif kind == "summary":
            self._summary(message.get("summary") or {})
        elif kind == "bye":
            pass

    def _hello(self, message: dict) -> None:
        self.columns = list(message.get("evaluation_columns") or [])
        if self.out_dir is not None:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self._files["csv"] = (self.out_dir / "evaluations.csv").open("w", encoding="utf-8")
            self._files["csv"].write(",".join(self.columns) + "\n")
            self._files["jsonl"] = (self.out_dir / "run.jsonl").open("w", encoding="utf-8")
        if self.raw:
            _emit(",".join(self.columns))
            return
        if self.quiet:
            return
        _emit(
            f"  run        {message.get('run_id')}\n"
            f"  algorithm  {message.get('algorithm')} on {message.get('benchmark')}"
            f"  ({message.get('num_islands')} islands)\n"
            f"  protocol   {message.get('protocol')}"
            f"  schema {message.get('schema_version')}"
            f"  harness {message.get('harness_version')}\n"
            f"  columns    {len(self.columns)}\n"
        )

    def _evaluation(self, row: str) -> None:
        self.counts["evaluation"] += 1
        handle = self._files.get("csv")
        if handle is not None:
            handle.write(row + "\n")
            island = self._island_of(row)
            if island is not None:
                self._shard(island, "evaluations.csv").write(row + "\n")
        if self.raw:
            _emit(row)
            return
        if self.quiet:
            return
        if self.counts["evaluation"] % self.every:
            return
        if not self.columns:
            _emit(f"  {row}")
            return
        try:
            values = next(csv.reader(io.StringIO(row)))
        except (csv.Error, StopIteration):
            _emit(f"  <unparseable> {row}")
            return
        record = dict(zip(self.columns, values))
        keys = self.show or ["island_id", "generation", "eval_index", "operator", "fitness"]
        _emit("  " + "  ".join(f"{key}={record.get(key, '')}" for key in keys))

    def _event(self, record: dict) -> None:
        self.counts["event"] += 1
        event_type = str(record.get("type", "?"))
        self.by_type[event_type] = self.by_type.get(event_type, 0) + 1
        handle = self._files.get("jsonl")
        if handle is not None:
            line = json.dumps(record, separators=(",", ":"))
            handle.write(line + "\n")
            emitter = str(record.get("emitter", ""))
            if emitter.startswith("island:"):
                self._shard(int(emitter.split(":", 1)[1]), "jsonl").write(line + "\n")
        if self.raw or self.quiet:
            return
        if self.events is not None and event_type not in self.events:
            return
        _emit(f"  [{event_type}] {self._gist(record)}")

    def _artifact(self, relative: str, content: str) -> None:
        """A file the master wrote that is not part of a stream."""
        self.counts["artifact"] = self.counts.get("artifact", 0) + 1
        if self.out_dir is not None and relative:
            target = self.out_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if self.raw or self.quiet:
            return
        _emit(f"  [artifact] {relative}  ({len(content):,} bytes)")

    def _shard(self, island_id: int, suffix: str) -> io.TextIOBase:
        """Open, once, a per-island shard derived from the aggregate streams."""
        key = f"shard-{island_id}-{suffix}"
        handle = self._files.get(key)
        if handle is None:
            assert self.out_dir is not None
            islands = self.out_dir / "islands"
            islands.mkdir(parents=True, exist_ok=True)
            handle = (islands / f"island-{island_id:02d}.{suffix}").open("w", encoding="utf-8")
            if suffix == "evaluations.csv":
                handle.write(",".join(self.columns) + "\n")
            self._files[key] = handle
        return handle

    def _summary(self, summary: dict) -> None:
        handle = self._files.get("summary")
        if self.out_dir is not None:
            (self.out_dir / "summary.json").write_text(
                json.dumps(summary, indent=2, default=str), encoding="utf-8"
            )
        if self.raw:
            return
        _emit(
            f"\n  SUMMARY  {summary.get('termination_reason')}"
            f"  in {float(summary.get('wallclock_seconds') or 0):.1f}s"
            f"  |  {summary.get('islands_completed')} islands completed,"
            f" {summary.get('islands_failed')} failed"
            f"  |  {int(summary.get('total_evaluations') or 0):,} evaluations"
            f"  |  best {summary.get('global_best_fitness')}"
            f" (island {summary.get('global_best_island')})"
        )
        if self.by_type:
            spread = "  ".join(f"{name}={count}" for name, count in sorted(self.by_type.items()))
            _emit(f"  events   {spread}")
        if self.out_dir is not None:
            written = sorted(
                str(f.relative_to(self.out_dir)) for f in self.out_dir.rglob("*") if f.is_file()
            )
            _emit(f"  wrote    {self.out_dir}/  ({len(written)} files)")

    # -- helpers ------------------------------------------------------------ #

    def _island_of(self, row: str) -> int | None:
        """Read island_id out of a CSV row, for shard routing."""
        try:
            index = self.columns.index("island_id")
            values = next(csv.reader(io.StringIO(row)))
            return int(values[index])
        except (ValueError, IndexError, StopIteration, csv.Error):
            return None

    @staticmethod
    def _gist(record: dict) -> str:
        """A one-line précis of an event, chosen per type."""
        kind = record.get("type")
        if kind == "run_start":
            return f"{record.get('algorithm')} on {record.get('benchmark')}, {record.get('num_islands')} islands"
        if kind == "island_start":
            return f"island {record.get('island_id')} up, pop {record.get('population_size')}"
        if kind == "generation_end":
            return (
                f"island {record.get('island_id')} gen {record.get('generation')}"
                f"  best={record.get('best_fitness')}  diversity={record.get('diversity')}"
            )
        if kind in ("migration_send", "migration_arrive"):
            return (
                f"{record.get('source_island')} -> {record.get('dest_island')}"
                f"  n={record.get('num_migrants') or len(record.get('migrant_fitnesses') or [])}"
                f"  {record.get('migration_id')}"
            )
        if kind == "island_end":
            return (
                f"island {record.get('island_id')} {record.get('termination_reason')}"
                f"  evals={record.get('evaluations_total')}  best={record.get('best_fitness')}"
            )
        if kind == "run_end":
            return f"{record.get('termination_reason')}, best {record.get('global_best_fitness')}"
        if kind == "diagnostic":
            return f"{record.get('level')}: {record.get('message')}"
        return json.dumps(record, separators=(",", ":"))[:120]

    def _close_files(self) -> None:
        for handle in self._files.values():
            try:
                handle.close()
            except OSError:
                pass
        self._files.clear()


class ReusableServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default="0.0.0.0", help="Address to bind. Default: all interfaces.")
    parser.add_argument("--port", type=int, default=9900, help="Port to listen on. Default: 9900.")
    parser.add_argument(
        "--every",
        type=int,
        default=1,
        help="Print only every n-th evaluation row. Events are always printed.",
    )
    parser.add_argument(
        "--show",
        default=None,
        help="Comma-separated evaluation columns to print. Default: island_id, "
        "generation, eval_index, operator, fitness.",
    )
    parser.add_argument(
        "--events",
        default=None,
        metavar="TYPES",
        help="Comma-separated event types to print, e.g. 'generation_end' for the "
        "per-generation bests alone. 'none' prints no events. Default: all of them. "
        "Does not affect --out, which always records everything.",
    )
    parser.add_argument("--quiet", action="store_true", help="Print the summary only.")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Emit evaluations.csv on stdout and nothing else: the header, then every row "
        "verbatim. Redirect it to a file for a live copy of the CSV.",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="DIR",
        help="Rebuild the run's output directory from the stream alone: evaluations.csv, "
        "run.jsonl and summary.json, byte-for-byte what the master wrote.",
    )
    args = parser.parse_args(argv)

    RunStreamHandler.every = max(1, args.every)
    RunStreamHandler.quiet = args.quiet
    RunStreamHandler.raw = args.raw
    RunStreamHandler.out_dir = Path(args.out) if args.out else None
    if args.raw:
        RunStreamHandler.every = 1
    if args.show:
        RunStreamHandler.show = [part.strip() for part in args.show.split(",") if part.strip()]
    if args.events is not None:
        chosen = {part.strip() for part in args.events.split(",") if part.strip()}
        RunStreamHandler.events = set() if chosen == {"none"} else chosen

    server = ReusableServer((args.host, args.port), RunStreamHandler)
    host, port = server.server_address[0], server.server_address[1]
    if not args.raw:
        _emit(f"listening on {host}:{port} -- waiting for a run (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if not args.raw:
            _emit("\nstopped")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
