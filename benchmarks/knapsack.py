"""0/1 Knapsack: OR-Library / Pisinger instance files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .base import BinaryProblem, ProblemEvaluation, resolve_instance_path

__all__ = ["KnapsackInstance", "Knapsack", "load_knapsack_file"]


@dataclass
class KnapsackInstance:
    """Item values, item weights and the sack capacity."""

    values: np.ndarray
    weights: np.ndarray
    capacity: float
    name: str = "knapsack"
    optimum: float | None = None
    source: str = "file"

    def __post_init__(self) -> None:
        self.values = np.asarray(self.values, dtype=float)
        self.weights = np.asarray(self.weights, dtype=float)
        if self.values.shape != self.weights.shape:
            raise ValueError("values and weights must have the same length")
        if self.values.ndim != 1:
            raise ValueError("values and weights must be 1-dimensional")
        if np.any(self.weights <= 0):
            raise ValueError("item weights must be strictly positive")
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")

    @property
    def n(self) -> int:
        """Number of items."""
        return int(self.values.size)


def load_knapsack_file(path: str | Path) -> KnapsackInstance:
    """Parse a 0/1 knapsack instance file. Relative paths are repo-root-relative."""
    path = resolve_instance_path(path, what="knapsack instance file")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("-")]
    if not lines:
        raise ValueError(f"{path}: empty instance file")

    if re.match(r"^n\s+\d+", lines[1] if len(lines) > 1 else "", flags=re.IGNORECASE):
        return _parse_pisinger(lines, path)
    if len(lines) > 1 and len(re.split(r"[\s,]+", lines[0])) == 1:
        return _parse_kplib(lines, path)
    return _parse_plain(lines, path)


def _parse_kplib(lines: list[str], path: Path) -> KnapsackInstance:
    """Parse the Knapsack Problem Library layout."""
    try:
        count = int(lines[0])
        capacity = float(lines[1])
    except (IndexError, ValueError) as exc:
        raise ValueError(
            f"{path}: kplib-format file must begin with the item count and the capacity "
            "on separate lines"
        ) from exc

    values: list[float] = []
    weights: list[float] = []
    for line in lines[2 : count + 2]:
        parts = re.split(r"[\s,]+", line)
        if len(parts) < 2:
            raise ValueError(f"{path}: malformed item line {line!r}; expected 'value weight'")
        values.append(float(parts[0]))
        weights.append(float(parts[1]))

    if len(values) != count:
        raise ValueError(f"{path}: header declares n={count} but {len(values)} item lines were read")

    return KnapsackInstance(
        values=np.array(values),
        weights=np.array(weights),
        capacity=capacity,
        name=path.stem,
        source=f"file:{path}",
    )


def _parse_pisinger(lines: list[str], path: Path) -> KnapsackInstance:
    name = lines[0]
    header: dict[str, float] = {}
    item_start = 1
    for index in range(1, min(len(lines), 8)):
        match = re.match(r"^([a-zA-Z]+)\s+(-?\d+(?:\.\d+)?)$", lines[index])
        if match is None:
            item_start = index
            break
        header[match.group(1).lower()] = float(match.group(2))
        item_start = index + 1

    if "n" not in header or "c" not in header:
        raise ValueError(f"{path}: Pisinger-format file missing 'n' or 'c' header")

    count = int(header["n"])
    values: list[float] = []
    weights: list[float] = []
    for line in lines[item_start : item_start + count]:
        parts = re.split(r"[\s,]+", line)
        if len(parts) < 3:
            raise ValueError(f"{path}: malformed item line {line!r}; expected 'index value weight'")
        values.append(float(parts[1]))
        weights.append(float(parts[2]))

    if len(values) != count:
        raise ValueError(f"{path}: header declares n={count} but {len(values)} item lines were read")

    return KnapsackInstance(
        values=np.array(values),
        weights=np.array(weights),
        capacity=header["c"],
        name=name or path.stem,
        optimum=header.get("z"),
        source=f"file:{path}",
    )


def _parse_plain(lines: list[str], path: Path) -> KnapsackInstance:
    head = re.split(r"[\s,]+", lines[0])
    if len(head) < 2:
        raise ValueError(f"{path}: first line must be 'n capacity'")
    count, capacity = int(float(head[0])), float(head[1])

    values: list[float] = []
    weights: list[float] = []
    for line in lines[1 : count + 1]:
        parts = re.split(r"[\s,]+", line)
        if len(parts) < 2:
            raise ValueError(f"{path}: malformed item line {line!r}; expected 'value weight'")
        values.append(float(parts[0]))
        weights.append(float(parts[1]))

    if len(values) != count:
        raise ValueError(f"{path}: header declares n={count} but {len(values)} item lines were read")

    return KnapsackInstance(
        values=np.array(values),
        weights=np.array(weights),
        capacity=capacity,
        name=path.stem,
        source=f"file:{path}",
    )


class Knapsack(BinaryProblem):
    """0/1 Knapsack as a binary-encoded maximisation problem."""

    maximising = True

    def __init__(self, instance_file: str, **params: Any) -> None:
        instance = load_knapsack_file(instance_file)

        super().__init__(
            dimension=instance.n,
            instance_file=str(instance_file),
            **params,
        )
        self.instance = instance
        self.name = f"knapsack:{instance.name}"
        self.known_optimum = instance.optimum

        self.penalty = float(np.max(instance.values / instance.weights))

    def evaluate(self, genome: Sequence[int]) -> ProblemEvaluation:
        """Score one selection, penalising any capacity overflow."""
        self._evaluations += 1
        bits = np.asarray(genome, dtype=np.int8)
        total_value = float(bits @ self.instance.values)
        total_weight = float(bits @ self.instance.weights)
        excess = max(0.0, total_weight - self.instance.capacity)
        feasible = excess <= 0.0
        return ProblemEvaluation(
            fitness=total_value - self.penalty * excess,
            objective=total_value,
            feasible=feasible,
            constraint_violation=excess,
        )

    def describe(self) -> dict[str, Any]:
        """Instance facts recorded in island_start.benchmark_params."""
        return {
            **super().describe(),
            "instance_name": self.instance.name,
            "instance_source": self.instance.source,
            "num_items": self.instance.n,
            "capacity": self.instance.capacity,
            "total_weight": float(np.sum(self.instance.weights)),
            "total_value": float(np.sum(self.instance.values)),
            "penalty_multiplier": self.penalty,
        }
