#!/usr/bin/env python3
"""Regenerate the field reference and worked examples in ``docs/LOGGING_FORMAT.md``.

The examples come from a real run, so regenerating them needs Docker and a worker
image built from the current source; ``--fields-only`` and ``--check`` need neither.
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
    EVALUATION_CSV_COLUMNS,
    EVENT_MODELS,
    BaseEvent,
    evaluation_csv_schema,
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


def build_csv_column_table() -> str:
    """Column order of ``evaluations.csv``, in file order."""
    fields = EVENT_MODELS["evaluation"].model_fields
    lines = [
        "| # | Column | Type | Meaning |",
        "| --- | --- | --- | --- |",
    ]
    for index, column in enumerate(EVALUATION_CSV_COLUMNS):
        field = fields.get(column)
        if field is not None:
            type_name = _render_type(field.annotation).replace("|", "\\|")
            description = re.sub(r"\s+", " ", (field.description or "").strip())
        else:
            envelope = BaseEvent.model_fields[column]
            type_name = _render_type(envelope.annotation).replace("|", "\\|")
            description = re.sub(r"\s+", " ", (envelope.description or "").strip())
        lines.append(f"| {index} | `{column}` | `{type_name}` | {description.replace('|', chr(92) + '|') or '—'} |")

    conventions = evaluation_csv_schema()["conventions"]
    lines.append("")
    lines.append("Encoding conventions, also written into `evaluations.schema.json`:")
    lines.append("")
    for key, value in conventions.items():
        lines.append(f"* **`{key}`** — {value}")
    return "\n".join(lines) + "\n"


def build_field_reference() -> str:
    """Render the CSV columns, the envelope, and every event's fields."""
    parts: list[str] = [
        "### `evaluations.csv` columns\n",
        "The CSV stream. Column order is fixed by "
        "`archipelago_logging.EVALUATION_CSV_COLUMNS` and is part of the contract: "
        "a consumer may read it positionally.\n",
        build_csv_column_table(),
        "\n### The JSON Lines envelope\n",
        "Present on **every** record in `run.jsonl`, in this order.\n",
        _table(_field_rows(BaseEvent, skip_envelope=False)),
    ]
    for type_name in EVENT_ORDER:
        model = EVENT_MODELS[type_name]
        parts.append(f"\n### `{type_name}`\n")
        parts.append(f"{_model_summary(model)}\n")
        if type_name == "evaluation":
            parts.append(
                "_Written as a CSV row, not a JSON object; the columns above are the "
                "physical form. The fields below are the same data as the reader "
                "returns it._\n"
            )
        parts.append(_table(_field_rows(model, skip_envelope=True)))
    return "\n".join(parts)


def _example_records(image: str) -> dict[str, dict[str, Any]]:
    """Capture one real record of each type by running a tiny archipelago."""
    import shutil
    import tempfile

    from archipelago_logging import iter_raw
    from orchestrator.config import RunConfig
    from orchestrator.master import Master

    workspace = Path(tempfile.mkdtemp(prefix="archipelago-docs-"))
    try:
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
            logging={"output_dir": str(workspace)},
            docker={"image": image, "build": False},
        )
        summary = Master(config, repository_root=REPOSITORY_ROOT).run()

        examples: dict[str, dict[str, Any]] = {}
        for record in iter_raw(summary.aggregate_log):
            examples.setdefault(record["type"], record)

        global _CSV_EXAMPLE
        csv_lines = summary.evaluations_csv.read_text().splitlines()
        _CSV_EXAMPLE = (csv_lines[0], csv_lines[1], csv_lines[-1])

        return examples
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


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


_CSV_EXAMPLE: tuple[str, str, str] | None = None


def build_examples(examples: dict[str, dict[str, Any]]) -> str:
    """Render one real record of each type as a fenced example."""
    parts: list[str] = []
    if _CSV_EXAMPLE is not None:
        header, first, last = _CSV_EXAMPLE
        parts.append(
            "\n#### `evaluations.csv`\n\nThe header and two real rows -- an initial "
            "individual (no parents) and a later one:\n\n"
            f"```csv\n{header}\n{first}\n{last}\n```\n"
        )
    for type_name in EVENT_ORDER:
        if type_name == "evaluation":
            continue
        record = examples.get(type_name)
        if record is None:
            continue
        rendered = json.dumps(_truncate_for_display(record), indent=2)
        parts.append(f"\n#### `{type_name}`\n\n```json\n{rendered}\n```\n")
    return "\n".join(parts)


def _replace_region(text: str, begin: str, end: str, replacement: str) -> str:
    pattern = re.compile(
        re.escape(begin) + r".*?" + re.escape(end), re.DOTALL
    )
    if not pattern.search(text):
        raise SystemExit(f"markers {begin} / {end} not found in {DOCUMENT}")
    return pattern.sub(f"{begin}\n{replacement}\n{end}", text)


def render(current: str, image: str) -> str:
    """Replace both generated regions in the document."""
    updated = _replace_region(
        current, FIELD_REFERENCE_BEGIN, FIELD_REFERENCE_END, build_field_reference()
    )
    updated = _replace_region(
        updated, EXAMPLES_BEGIN, EXAMPLES_END, build_examples(_example_records(image))
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
        default="archipelago-worker:latest",
        help="Worker image for the example run. It must already exist and be current.",
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
        updated = render(current, args.image)
    DOCUMENT.write_text(updated, encoding="utf-8")
    print(f"Regenerated {DOCUMENT} for schema {SCHEMA_VERSION}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
