"""Presets, and the overrides the Archipelago Backend may apply to them.

A preset is one YAML file in ``config/``, named by its file name. A run is a preset plus at most
the override keys in :data:`OVERRIDES`, within their bounds; everything else about the run comes
from the preset.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from orchestrator.config import RunConfig
from orchestrator.topology import TOPOLOGIES

__all__ = ["OVERRIDES", "Preset", "InvalidRequest", "load_presets", "canonical_request"]

PRESETS_DIR = Path(__file__).resolve().parents[1] / "config"

OVERRIDES: dict[str, dict[str, Any]] = {
    "num_islands": {"type": "int", "min": 4, "max": 6},
    # The maximum is a memory bound, on the backend's side: at 500,000 one worker needed ~3.8 GB to
    # cluster a run and two concurrent runs nearly filled its 2 GB Redis (docs/MEMORY_MEASUREMENTS.md).
    "evaluation_budget": {"type": "int", "min": 1000, "max": 250_000},
    "target_generations": {"type": "int", "min": 2},
    "migration.enabled": {"type": "bool"},
    "migration.topology": {"type": "choice", "choices": sorted(TOPOLOGIES)},
    "migration.interval": {"type": "int", "min": 1},
    "migration.num_migrants": {"type": "int", "min": 1},
    "seed": {"type": "int", "min": 0, "max": 4_294_967_295, "optional": True},
}
"""Every key a request may override, with its bounds, exactly as ``GET /presets`` publishes them."""

_PLACEHOLDER_RUN_ID = "run-20000101T000000Z-00000000"


class InvalidRequest(ValueError):
    """A request the Runner refuses (422); the message is fit to show a user."""


def canonical_request(preset: str, overrides: dict[str, Any]) -> str:
    """The request as compared for idempotency: preset and overrides, key order ignored."""
    return json.dumps({"preset": preset, "overrides": overrides}, sort_keys=True, separators=(",", ":"))


def _set_path(document: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, leaf = dotted.split(".")
    cursor = document
    for part in parents:
        cursor = cursor.setdefault(part, {})
    cursor[leaf] = value


def _get_path(config: RunConfig, dotted: str) -> Any:
    value: Any = config
    for part in dotted.split("."):
        value = getattr(value, part)
    return value


def _check_override(key: str, value: Any) -> None:
    rule = OVERRIDES[key]
    if value is None and rule.get("optional"):
        return
    if rule["type"] == "bool":
        if not isinstance(value, bool):
            raise InvalidRequest(f"override {key!r} must be true or false, not {value!r}")
        return
    if rule["type"] == "choice":
        if value not in rule["choices"]:
            raise InvalidRequest(f"override {key!r} must be one of {rule['choices']}, not {value!r}")
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidRequest(f"override {key!r} must be an integer, not {value!r}")
    if "min" in rule and value < rule["min"]:
        raise InvalidRequest(f"override {key!r} must be at least {rule['min']:,}, not {value:,}")
    if "max" in rule and value > rule["max"]:
        raise InvalidRequest(f"override {key!r} must be at most {rule['max']:,}, not {value:,}")


def _readable(error: ValidationError) -> str:
    """A ValidationError as one sentence or a few, without Pydantic's framing."""
    parts = []
    for item in error.errors():
        message = str(item["msg"]).removeprefix("Value error, ")
        location = ".".join(str(part) for part in item["loc"])
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts)


@dataclass(frozen=True)
class Preset:
    """One preset: its YAML document, and that document's values for every override key."""

    name: str
    document: dict[str, Any]
    algorithm: str
    benchmark: str
    description: str
    defaults: dict[str, Any]

    def build(self, run_id: str, overrides: dict[str, Any]) -> RunConfig:
        """The run's config: this preset with ``overrides`` applied, validated as a whole."""
        unknown = sorted(set(overrides) - set(OVERRIDES))
        if unknown:
            raise InvalidRequest(f"unknown overrides {unknown}; allowed: {sorted(OVERRIDES)}")
        for key, value in overrides.items():
            _check_override(key, value)
        document = copy.deepcopy(self.document)
        if overrides.get("migration.topology", self.defaults["migration.topology"]) != self.defaults[
            "migration.topology"
        ]:
            # The preset's topology parameters (a star's hub, say) belong to its own topology.
            document.get("migration", {}).pop("topology_params", None)
        for key, value in overrides.items():
            _set_path(document, key, value)
        document["run_id"] = run_id
        try:
            return RunConfig.model_validate(document)
        except ValidationError as exc:
            raise InvalidRequest(_readable(exc)) from None

    def describe(self) -> dict[str, Any]:
        """This preset as ``GET /presets`` lists it."""
        return {
            "preset": self.name,
            "algorithm": self.algorithm,
            "benchmark": self.benchmark,
            "description": self.description,
            "defaults": self.defaults,
        }


def _load_preset(path: Path) -> Preset:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path}: a preset must be a YAML mapping")
    config = RunConfig.model_validate({**document, "run_id": _PLACEHOLDER_RUN_ID})
    defaults = {
        key: _get_path(config, key) for key, rule in OVERRIDES.items() if not rule.get("optional")
    }
    return Preset(
        name=path.stem,
        document=document,
        algorithm=config.algorithm,
        benchmark=config.benchmark,
        description=config.description,
        defaults=defaults,
    )


def load_presets(directory: Path = PRESETS_DIR) -> dict[str, Preset]:
    """Every preset in ``directory``, by name. A preset that does not validate stops the service."""
    presets = {path.stem: _load_preset(path) for path in sorted(directory.glob("*.yaml"))}
    if not presets:
        raise ValueError(f"no presets found in {directory}")
    return presets
