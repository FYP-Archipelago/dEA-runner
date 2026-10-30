#!/usr/bin/env python3
"""Regenerate the field reference and worked examples in ``docs/LOGGING_FORMAT.md``.

The examples come from a real run, so regenerating them needs Docker, an island image built
from the current source, and a Redis the islands can reach; ``--fields-only`` and ``--check``
need none of them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from archipelago_logging.events import (
    EVENT_MODELS,
    BaseEvent,
)
from archipelago_logging.version import SCHEMA_VERSION

DOCUMENT = REPOSITORY_ROOT / "docs" / "LOGGING_FORMAT.md"

FIELD_REFERENCE_BEGIN = "<!-- BEGIN GENERATED FIELD REFERENCE -->"
FIELD_REFERENCE_END = "<!-- END GENERATED FIELD REFERENCE -->"
EXAMPLES_BEGIN = "<!-- BEGIN GENERATED EXAMPLES -->"
EXAMPLES_END = "<!-- END GENERATED EXAMPLES -->"

EVENT_ORDER = [
    "run_start",
    "island_start",
    "evaluation",
    "generation_end",
    "migration_send",
    "migration_arrive",
    "island_end",
    "run_end",
    "diagnostic",
]


def _render_type(annotation: Any) -> str:
    """Render a type annotation as something a reader of JSON would recognise."""
    text = str(annotation)
    text = text.replace("typing.", "").replace("<class '", "").replace("'>", "")
    text = re.sub(r"\bOptional\[(.*)\]", r"\1 | null", text)
    text = text.replace("NoneType", "null")
    text = text.replace("dict[str, Any]", "object")
    text = text.replace("list[str]", "string[]")
    text = text.replace("list[float]", "number[]")
    text = text.replace("list[int]", "integer[]")
    text = text.replace("str", "string")
    text = text.replace("float", "number")
    text = text.replace("bool", "boolean")
    text = text.replace("int", "integer")
    text = text.replace("Any", "any")
    text = re.sub(r"Literal\[([^\]]*)\]", r"constant \1", text)
    text = re.sub(r"\bNone\b", "null", text)
    return text.strip()


def _field_rows(model: type[BaseEvent], skip_envelope: bool) -> list[tuple[str, str, str, str]]:
    envelope = set(BaseEvent.model_fields)
    rows: list[tuple[str, str, str, str]] = []
    for name, field in model.model_fields.items():
        if skip_envelope and name in envelope:
            continue
        required = "yes" if field.is_required() else "no"
        description = (field.description or "").replace("\n", " ").strip()
        description = re.sub(r"\s+", " ", description)
        rows.append((name, _render_type(field.annotation), required, description))
    return rows


def _table(rows: list[tuple[str, str, str, str]]) -> str:
    if not rows:
        return "_No fields beyond the envelope._\n"
    lines = ["| Field | Type | Required | Meaning |", "| --- | --- | --- | --- |"]
    for name, type_name, required, description in rows:
        description = description.replace("|", "\\|") or "—"
        type_name = type_name.replace("|", "\\|")
        lines.append(f"| `{name}` | `{type_name}` | {required} | {description} |")
    return "\n".join(lines) + "\n"


def _model_summary(model: type[BaseEvent]) -> str:
    """First paragraph of the model's docstring, as the event's one-line purpose."""
    doc = textwrap.dedent(model.__doc__ or "").strip()
    paragraph = doc.split("\n\n")[0]
    return re.sub(r"\s+", " ", paragraph).strip()


def build_field_reference() -> str:
    """Render the envelope and every event's fields."""
    parts: list[str] = [
        "### The envelope\n",
        "Present on **every** record, in this order.\n",
        _table(_field_rows(BaseEvent, skip_envelope=False)),
    ]
    for type_name in EVENT_ORDER:
        model = EVENT_MODELS[type_name]
        parts.append(f"\n### `{type_name}`\n")
        parts.append(f"{_model_summary(model)}\n")
        parts.append(_table(_field_rows(model, skip_envelope=True)))
    return "\n".join(parts)


def _example_records(image: str, broker_url: str) -> dict[str, list[dict[str, Any]]]:
    """Capture one real record of each type by running a tiny archipelago."""
    from orchestrator.config import RunConfig
    from orchestrator.master import Master
    from orchestrator.redis_stream import read_stream

    config = RunConfig(
        algorithm="de",
        benchmark="sphere",
        benchmark_params={"dimension": 3},
        evaluation_budget=320,
        target_generations=8,
        num_islands=4,
        description="documentation example run.",
        termination={"stagnation_generations": 0, "max_generations": 50},
        migration={
            "topology": "ring",
            "interval": 2,
            "num_migrants": 2,
            "selection": "best",
            "replacement": "worst",
        },
        docker={"image": image, "build": False},
        broker={"url": broker_url},
    )
    summary = Master(config, repository_root=REPOSITORY_ROOT).run()

    examples: dict[str, list[dict[str, Any]]] = {}
    for record in read_stream(broker_url, summary.stream_key):
        examples.setdefault(record["type"], []).append(record)

    evaluations = examples.get("evaluation", [])
    with_parents = next((record for record in evaluations if record.get("parent_ids")), None)
    examples["evaluation"] = [evaluations[0]] + ([with_parents] if with_parents else [])
    return {type_name: found[:2] if type_name == "evaluation" else found[:1]
            for type_name, found in examples.items()}


def _truncate_for_display(record: dict[str, Any]) -> dict[str, Any]:
    """Shorten fields whose full value would swamp the example."""
    shown = dict(record)
    if record["type"] == "run_start":
        config = shown.get("config", {})
        shown["config"] = {
            "seed": config.get("seed"),
            "algorithm": config.get("algorithm"),
            "benchmark": config.get("benchmark"),
            "population_size": config.get("population_size"),
            "__elided__": "the complete resolved configuration; see docs/CONFIGURATION.md",
        }
        migration = shown.get("migration", {})
        shown["migration"] = {
            key: migration[key]
            for key in ("topology", "interval", "num_migrants", "selection", "replacement", "neighbours_out")
            if key in migration
        }
        shown["host"] = {"__elided__": "platform, cpu count, total memory"}
    if record["type"] == "run_end":
        shown["per_island"] = {"__elided__": "one summary object per island, keyed by island id"}
    return shown


def build_examples(examples: dict[str, list[dict[str, Any]]]) -> str:
    """Render real records of each type as fenced examples."""
    parts: list[str] = []
    for type_name in EVENT_ORDER:
        records = examples.get(type_name)
        if not records:
            continue
        intro = (
            "Two real records: an initial individual, which has no parents, and a later "
            "one.\n\n"
            if type_name == "evaluation" and len(records) > 1
            else ""
        )
        rendered = "\n".join(
            f"```json\n{json.dumps(_truncate_for_display(record), indent=2)}\n```"
            for record in records
        )
        parts.append(f"\n#### `{type_name}`\n\n{intro}{rendered}\n")
    return "\n".join(parts)


def _replace_region(text: str, begin: str, end: str, replacement: str) -> str:
    pattern = re.compile(
        re.escape(begin) + r".*?" + re.escape(end), re.DOTALL
    )
    if not pattern.search(text):
        raise SystemExit(f"markers {begin} / {end} not found in {DOCUMENT}")
    return pattern.sub(f"{begin}\n{replacement}\n{end}", text)


def render(current: str, image: str, broker_url: str) -> str:
    """Replace both generated regions in the document."""
    updated = _replace_region(
        current, FIELD_REFERENCE_BEGIN, FIELD_REFERENCE_END, build_field_reference()
    )
    updated = _replace_region(
        updated, EXAMPLES_BEGIN, EXAMPLES_END, build_examples(_example_records(image, broker_url))
    )
    return updated


def main() -> int:
    """Regenerate the document, or check it for staleness."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the document is stale instead of rewriting it.",
    )
    parser.add_argument(
        "--fields-only",
        action="store_true",
        help="Regenerate only the field tables; do not execute a run for examples. "
        "The examples are the slow part, and they only change when a model does.",
    )
    parser.add_argument(
        "--image",
        default="archipelago-island:latest",
        help="Island image for the example run. It must already exist and be current.",
    )
    parser.add_argument(
        "--broker-url",
        default="redis://127.0.0.1:6379/0",
        help="Redis for the example run; the islands use host networking, so a local one works.",
    )
    args = parser.parse_args()

    current = DOCUMENT.read_text(encoding="utf-8")

    if args.check:
        expected = _replace_region(
            current, FIELD_REFERENCE_BEGIN, FIELD_REFERENCE_END, build_field_reference()
        )
        if expected != current:
            print(
                f"{DOCUMENT} is out of date with archipelago_logging.events "
                f"(schema {SCHEMA_VERSION}).\n"
                "Regenerate it with: python scripts/generate_logging_docs.py --fields-only",
                file=sys.stderr,
            )
            return 1
        print(f"{DOCUMENT} is current (schema {SCHEMA_VERSION}).")
        return 0

    if args.fields_only:
        updated = _replace_region(
            current, FIELD_REFERENCE_BEGIN, FIELD_REFERENCE_END, build_field_reference()
        )
    else:
        updated = render(current, args.image, args.broker_url)
    DOCUMENT.write_text(updated, encoding="utf-8")
    print(f"Regenerated {DOCUMENT} for schema {SCHEMA_VERSION}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
