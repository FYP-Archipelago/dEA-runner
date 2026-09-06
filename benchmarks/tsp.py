"""Travelling Salesman Problem, read from a TSPLIB instance file."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .base import PermutationProblem, ProblemEvaluation, resolve_instance_path

__all__ = ["TSPInstance", "TSP", "load_tsplib_file"]


@dataclass
class TSPInstance:
    """A TSP instance as an explicit distance matrix, plus coordinates when known."""

    distances: np.ndarray
    name: str = "tsp"
    coordinates: np.ndarray | None = None
    edge_weight_type: str = "EUC_2D"
    optimum: float | None = None
    source: str = "file"

    def __post_init__(self) -> None:
        self.distances = np.asarray(self.distances, dtype=float)
        if self.distances.ndim != 2 or self.distances.shape[0] != self.distances.shape[1]:
            raise ValueError("distance matrix must be square")

    @property
    def n(self) -> int:
        """Number of cities."""
        return int(self.distances.shape[0])


def _euc_2d(a: np.ndarray, b: np.ndarray) -> float:
    return float(round(math.hypot(a[0] - b[0], a[1] - b[1])))


def _ceil_2d(a: np.ndarray, b: np.ndarray) -> float:
    return float(math.ceil(math.hypot(a[0] - b[0], a[1] - b[1])))


def _man_2d(a: np.ndarray, b: np.ndarray) -> float:
    return float(round(abs(a[0] - b[0]) + abs(a[1] - b[1])))


def _max_2d(a: np.ndarray, b: np.ndarray) -> float:
    return float(max(round(abs(a[0] - b[0])), round(abs(a[1] - b[1]))))


def _att(a: np.ndarray, b: np.ndarray) -> float:
    """TSPLIB pseudo-Euclidean distance, used by the ``att48``/``att532`` instances."""
    dx, dy = a[0] - b[0], a[1] - b[1]
    rij = math.sqrt((dx * dx + dy * dy) / 10.0)
    tij = float(round(rij))
    return tij + 1.0 if tij < rij else tij


def _geo(a: np.ndarray, b: np.ndarray) -> float:
    """TSPLIB geographical distance on a sphere of radius 6378.388 km."""
    def to_radians(value: float) -> float:
        """TSPLIB GEO convention: degrees and minutes, not decimal degrees."""
        degrees = int(value)
        minutes = value - degrees
        return math.pi * (degrees + 5.0 * minutes / 3.0) / 180.0

    lat_a, lon_a = to_radians(a[0]), to_radians(a[1])
    lat_b, lon_b = to_radians(b[0]), to_radians(b[1])
    radius = 6378.388
    q1 = math.cos(lon_a - lon_b)
    q2 = math.cos(lat_a - lat_b)
    q3 = math.cos(lat_a + lat_b)
    return float(int(radius * math.acos(0.5 * ((1.0 + q1) * q2 - (1.0 - q1) * q3)) + 1.0))


_DISTANCE_FUNCTIONS = {
    "EUC_2D": _euc_2d,
    "CEIL_2D": _ceil_2d,
    "MAN_2D": _man_2d,
    "MAX_2D": _max_2d,
    "ATT": _att,
    "GEO": _geo,
}


def _matrix_from_coordinates(coordinates: np.ndarray, edge_weight_type: str) -> np.ndarray:
    distance = _DISTANCE_FUNCTIONS.get(edge_weight_type.upper())
    if distance is None:
        raise ValueError(
            f"unsupported EDGE_WEIGHT_TYPE {edge_weight_type!r}; "
            f"supported: {sorted(_DISTANCE_FUNCTIONS)}"
        )
    count = coordinates.shape[0]
    matrix = np.zeros((count, count), dtype=float)
    for i in range(count):
        for j in range(i + 1, count):
            value = distance(coordinates[i], coordinates[j])
            matrix[i, j] = matrix[j, i] = value
    return matrix


def _matrix_from_explicit(values: list[float], count: int, weight_format: str) -> np.ndarray:
    """Expand a TSPLIB ``EDGE_WEIGHT_SECTION`` into a full symmetric matrix."""
    matrix = np.zeros((count, count), dtype=float)
    fmt = weight_format.upper()
    stream = iter(values)

    def take() -> float:
        """Pull the next weight, failing loudly if the section is short."""
        try:
            return next(stream)
        except StopIteration as exc:
            raise ValueError(f"EDGE_WEIGHT_SECTION too short for {fmt} with n={count}") from exc

    if fmt == "FULL_MATRIX":
        for i in range(count):
            for j in range(count):
                matrix[i, j] = take()
    elif fmt == "UPPER_ROW":
        for i in range(count):
            for j in range(i + 1, count):
                matrix[i, j] = matrix[j, i] = take()
    elif fmt == "LOWER_ROW":
        for i in range(1, count):
            for j in range(i):
                matrix[i, j] = matrix[j, i] = take()
    elif fmt == "UPPER_DIAG_ROW":
        for i in range(count):
            for j in range(i, count):
                value = take()
                matrix[i, j] = matrix[j, i] = value
    elif fmt == "LOWER_DIAG_ROW":
        for i in range(count):
            for j in range(i + 1):
                value = take()
                matrix[i, j] = matrix[j, i] = value
    else:
        raise ValueError(f"unsupported EDGE_WEIGHT_FORMAT {weight_format!r}")

    np.fill_diagonal(matrix, 0.0)
    return matrix


def load_tsplib_file(path: str | Path) -> TSPInstance:
    """Parse a TSPLIB ``.tsp`` file. Relative paths are repo-root-relative."""
    path = resolve_instance_path(path, what="TSPLIB instance file")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    header: dict[str, str] = {}
    coordinate_tokens: list[str] = []
    weight_tokens: list[str] = []
    section: str | None = None

    for raw in lines:
        line = raw.strip()
        if not line or line == "EOF":
            continue
        if ":" in line and not line.split(":")[0].strip().endswith("SECTION"):
            key, _, value = line.partition(":")
            header[key.strip().upper()] = value.strip()
            continue
        upper = line.upper()
        if upper.endswith("_SECTION") or upper in {"NODE_COORD_SECTION", "EDGE_WEIGHT_SECTION"}:
            section = upper
            continue
        if section == "NODE_COORD_SECTION":
            coordinate_tokens.extend(line.split())
        elif section == "EDGE_WEIGHT_SECTION":
            weight_tokens.extend(line.split())

    problem_type = header.get("TYPE", "TSP").upper()
    if "TSP" not in problem_type:
        raise ValueError(f"{path}: TYPE is {problem_type!r}; only symmetric TSP is supported")

    count = int(header.get("DIMENSION", "0"))
    if count <= 0:
        raise ValueError(f"{path}: missing or invalid DIMENSION")
    edge_weight_type = header.get("EDGE_WEIGHT_TYPE", "EUC_2D").upper()

    if coordinate_tokens:
        numbers = [float(token) for token in coordinate_tokens]
        if len(numbers) % 3 != 0:
            raise ValueError(f"{path}: NODE_COORD_SECTION is not a multiple of 3 tokens")
        coordinates = np.array(numbers, dtype=float).reshape(-1, 3)[:, 1:]
        if coordinates.shape[0] != count:
            raise ValueError(
                f"{path}: DIMENSION is {count} but {coordinates.shape[0]} coordinate rows were read"
            )
        matrix = _matrix_from_coordinates(coordinates, edge_weight_type)
    elif weight_tokens:
        coordinates = None
        matrix = _matrix_from_explicit(
            [float(token) for token in weight_tokens],
            count,
            header.get("EDGE_WEIGHT_FORMAT", "FULL_MATRIX"),
        )
    else:
        raise ValueError(f"{path}: neither NODE_COORD_SECTION nor EDGE_WEIGHT_SECTION found")

    return TSPInstance(
        distances=matrix,
        name=header.get("NAME", path.stem),
        coordinates=coordinates,
        edge_weight_type=edge_weight_type,
        source=f"file:{path}",
    )


class TSP(PermutationProblem):
    """Symmetric TSP as a permutation-encoded minimisation problem."""

    maximising = False

    def __init__(self, instance_file: str, **params: Any) -> None:
        instance = load_tsplib_file(instance_file)

        super().__init__(
            dimension=instance.n,
            instance_file=str(instance_file),
            **params,
        )
        self.instance = instance
        self.name = f"tsp:{instance.name}"
        self.known_optimum = instance.optimum
        self._distances = instance.distances

    def evaluate(self, genome: Sequence[int]) -> ProblemEvaluation:
        """Score one tour by its closed length."""
        self._evaluations += 1
        tour = np.asarray(genome, dtype=int)
        length = float(np.sum(self._distances[tour, np.roll(tour, -1)]))
        return ProblemEvaluation(fitness=length)

    def describe(self) -> dict[str, Any]:
        """Instance facts recorded in island_start.benchmark_params."""
        return {
            **super().describe(),
            "instance_name": self.instance.name,
            "instance_source": self.instance.source,
            "num_cities": self.instance.n,
            "edge_weight_type": self.instance.edge_weight_type,
        }
