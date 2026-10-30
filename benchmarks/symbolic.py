"""Symbolic-regression benchmarks for Genetic Programming."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

import numpy as np

from .base import ProblemEvaluation, RegressionProblem, resolve_instance_path

__all__ = [
    "SymbolicRegression",
    "SYMBOLIC_BENCHMARKS",
    "REAL_REGRESSION_DATASETS",
    "load_regression_table",
    "INVALID_FITNESS",
]

INVALID_FITNESS = 1e12


def _uniform(rng: np.random.Generator, low: float, high: float, count: int, variables: int = 1) -> np.ndarray:
    return rng.uniform(low, high, size=(count, variables))


def _grid(low: float, high: float, step: float, variables: int) -> np.ndarray:
    axis = np.arange(low, high + step / 2.0, step)
    mesh = np.meshgrid(*([axis] * variables), indexing="ij")
    return np.stack([component.ravel() for component in mesh], axis=1)


SYMBOLIC_BENCHMARKS: dict[str, dict[str, Any]] = {
    "nguyen1": {
        "target": lambda x: x[:, 0] ** 3 + x[:, 0] ** 2 + x[:, 0],
        "sampler": lambda rng: _uniform(rng, -1.0, 1.0, 20),
        "variables": ["x"],
        "formula": "x^3 + x^2 + x",
        "test_sampler": lambda rng: _uniform(rng, -1.0, 1.0, 20),
    },
    "nguyen2": {
        "target": lambda x: x[:, 0] ** 4 + x[:, 0] ** 3 + x[:, 0] ** 2 + x[:, 0],
        "sampler": lambda rng: _uniform(rng, -1.0, 1.0, 20),
        "variables": ["x"],
        "formula": "x^4 + x^3 + x^2 + x",
    },
    "nguyen3": {
        "target": lambda x: x[:, 0] ** 5 + x[:, 0] ** 4 + x[:, 0] ** 3 + x[:, 0] ** 2 + x[:, 0],
        "sampler": lambda rng: _uniform(rng, -1.0, 1.0, 20),
        "variables": ["x"],
        "formula": "x^5 + x^4 + x^3 + x^2 + x",
    },
    "nguyen4": {
        "target": lambda x: (
            x[:, 0] ** 6 + x[:, 0] ** 5 + x[:, 0] ** 4 + x[:, 0] ** 3 + x[:, 0] ** 2 + x[:, 0]
        ),
        "sampler": lambda rng: _uniform(rng, -1.0, 1.0, 20),
        "variables": ["x"],
        "formula": "x^6 + x^5 + x^4 + x^3 + x^2 + x",
    },
    "nguyen5": {
        "target": lambda x: np.sin(x[:, 0] ** 2) * np.cos(x[:, 0]) - 1.0,
        "sampler": lambda rng: _uniform(rng, -1.0, 1.0, 20),
        "variables": ["x"],
        "formula": "sin(x^2) * cos(x) - 1",
    },
    "nguyen6": {
        "target": lambda x: np.sin(x[:, 0]) + np.sin(x[:, 0] + x[:, 0] ** 2),
        "sampler": lambda rng: _uniform(rng, -1.0, 1.0, 20),
        "variables": ["x"],
        "formula": "sin(x) + sin(x + x^2)",
    },
    "nguyen7": {
        "target": lambda x: np.log(x[:, 0] + 1.0) + np.log(x[:, 0] ** 2 + 1.0),
        "sampler": lambda rng: _uniform(rng, 0.0, 2.0, 20),
        "variables": ["x"],
        "formula": "ln(x + 1) + ln(x^2 + 1)",
    },
    "nguyen8": {
        "target": lambda x: np.sqrt(x[:, 0]),
        "sampler": lambda rng: _uniform(rng, 0.0, 4.0, 20),
        "variables": ["x"],
        "formula": "sqrt(x)",
    },
    "nguyen9": {
        "target": lambda x: np.sin(x[:, 0]) + np.sin(x[:, 1] ** 2),
        "sampler": lambda rng: _uniform(rng, 0.0, 1.0, 100, variables=2),
        "variables": ["x", "y"],
        "formula": "sin(x) + sin(y^2)",
    },
    "nguyen10": {
        "target": lambda x: 2.0 * np.sin(x[:, 0]) * np.cos(x[:, 1]),
        "sampler": lambda rng: _uniform(rng, 0.0, 1.0, 100, variables=2),
        "variables": ["x", "y"],
        "formula": "2 * sin(x) * cos(y)",
    },
    "pagie1": {
        "target": lambda x: 1.0 / (1.0 + x[:, 0] ** -4.0) + 1.0 / (1.0 + x[:, 1] ** -4.0),
        "sampler": lambda rng: _grid(-5.0, 5.0, 0.4, 2),
        "variables": ["x", "y"],
        "formula": "1/(1 + x^-4) + 1/(1 + y^-4)",
        "deterministic_sample": True,
    },
    "keijzer6": {
        "target": lambda x: np.array(
            [float(np.sum(1.0 / np.arange(1, int(value) + 1))) for value in x[:, 0]]
        ),
        "sampler": lambda rng: np.arange(1, 51, dtype=float).reshape(-1, 1),
        "test_sampler": lambda rng: np.arange(1, 121, dtype=float).reshape(-1, 1),
        "variables": ["x"],
        "formula": "sum_{i=1}^{x} 1/i",
        "deterministic_sample": True,
    },
    "korns1": {
        "target": lambda x: 1.57 + 24.3 * x[:, 3],
        "sampler": lambda rng: _uniform(rng, -50.0, 50.0, 1000, variables=5),
        "variables": ["x0", "x1", "x2", "x3", "x4"],
        "formula": "1.57 + 24.3 * x3",
    },
}


def load_regression_table(
    path: str | Path,
    target_column: str | int = -1,
    max_rows: int | None = None,
    delimiter: str | None = None,
    has_header: bool | None = None,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Load a numeric regression table from a delimited text file."""
    path = resolve_instance_path(path, what="regression dataset")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        raise ValueError(f"{path}: empty regression table")

    if delimiter is None:
        first = lines[0]
        if "\t" in first:
            delimiter = "\t"
        elif "," in first:
            delimiter = ","
        else:
            delimiter = None

    def split(line: str) -> list[str]:
        """Split one line on commas, semicolons or whitespace."""
        return line.split(delimiter) if delimiter else line.split()

    def is_numeric(fields: list[str]) -> bool:
        """True when every field on the line parses as a float."""
        try:
            [float(field) for field in fields]
            return True
        except ValueError:
            return False

    if has_header is None:
        has_header = not is_numeric(split(lines[0]))

    header = split(lines[0]) if has_header else None
    body = lines[1:] if has_header else lines
    if max_rows is not None:
        body = body[:max_rows]

    rows: list[list[float]] = []
    for number, line in enumerate(body, start=2 if has_header else 1):
        fields = split(line)
        try:
            rows.append([float(field) for field in fields])
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: non-numeric field in {line!r}") from exc

    widths = {len(row) for row in rows}
    if len(widths) != 1:
        raise ValueError(f"{path}: rows have inconsistent widths {sorted(widths)}")

    matrix = np.array(rows, dtype=float)
    names = [name.strip() for name in header] if header else [f"x{i}" for i in range(matrix.shape[1])]

    index = names.index(target_column) if isinstance(target_column, str) else int(target_column)
    index %= matrix.shape[1]

    y = matrix[:, index]
    x = np.delete(matrix, index, axis=1)
    variable_names = [name for position, name in enumerate(names) if position != index]
    return x, y, variable_names


REAL_REGRESSION_DATASETS: dict[str, dict[str, Any]] = {
    "yacht": {
        "path": "benchmarks/data/regression/yacht_hydrodynamics.data",
        "target_column": -1,
        "formula": "UCI Yacht Hydrodynamics (Gerritsma et al.); "
        "target = residuary resistance per unit weight",
        "variable_names": [
            "longitudinal_position",
            "prismatic_coefficient",
            "length_displacement_ratio",
            "beam_draught_ratio",
            "length_beam_ratio",
            "froude_number",
        ],
    },
    "airfoil": {
        "path": "benchmarks/data/regression/airfoil_self_noise.dat",
        "target_column": -1,
        "formula": "UCI Airfoil Self-Noise (Brooks, Pope & Marcolini, NASA 1989); "
        "target = scaled sound pressure level in dB",
        "variable_names": [
            "frequency",
            "angle_of_attack",
            "chord_length",
            "free_stream_velocity",
            "suction_side_displacement",
        ],
    },
}


class SymbolicRegression(RegressionProblem):
    """Fit a target function from sampled points, scored by an error metric."""

    maximising = False
    known_optimum = 0.0

    def __init__(
        self,
        benchmark: str = "nguyen7",
        instance_seed: int = 1,
        metric: str = "rmse",
        csv_path: str | None = None,
        csv_target_column: str | int = -1,
        csv_max_rows: int | None = None,
        **params: Any,
    ) -> None:
        key = benchmark.lower()
        metric = metric.lower()
        if metric not in ("rmse", "mse", "mae"):
            raise ValueError(f"unknown metric {metric!r}; expected 'rmse', 'mse' or 'mae'")

        if key in REAL_REGRESSION_DATASETS:
            spec = REAL_REGRESSION_DATASETS[key]
            path = Path(csv_path) if csv_path else Path(spec["path"])
            if not path.is_absolute():
                path = _REPOSITORY_ROOT / path
            if not path.exists():
                raise FileNotFoundError(
                    f"real regression dataset {key!r} is not present at {path}. "
                    "It is committed under benchmarks/data/; restore it from git."
                )
            x, y, names = load_regression_table(
                path, spec["target_column"], csv_max_rows
            )
            names = spec.get("variable_names", names)
            if len(names) != x.shape[1]:
                raise ValueError(
                    f"{path}: expected {len(names)} feature columns for {key!r}, found {x.shape[1]}"
                )
            test_x = test_y = None
            formula = spec["formula"]
        elif key == "csv":
            if not csv_path:
                raise ValueError("benchmark='csv' requires csv_path")
            x, y, names = load_regression_table(csv_path, csv_target_column, csv_max_rows)
            test_x = test_y = None
            formula = f"file:{csv_path}"
        else:
            if key not in SYMBOLIC_BENCHMARKS:
                raise ValueError(
                    f"unknown symbolic benchmark {benchmark!r}; available: {sorted(SYMBOLIC_BENCHMARKS)}"
                )
            spec = SYMBOLIC_BENCHMARKS[key]
            rng = np.random.default_rng(instance_seed)
            x = spec["sampler"](rng)
            y = spec["target"](x)
            names = spec["variables"]
            formula = spec["formula"]
            test_sampler = spec.get("test_sampler")
            if test_sampler is not None:
                test_x = test_sampler(np.random.default_rng(instance_seed + 10_000))
                test_y = spec["target"](test_x)
            else:
                test_x = test_y = None

        super().__init__(
            x,
            y,
            names,
            test_x=test_x,
            test_y=test_y,
            benchmark=key,
            instance_seed=instance_seed,
            metric=metric,
            csv_path=csv_path,
            **params,
        )
        self.name = f"symbolic:{key}"
        self.metric = metric
        self.formula = formula


    def score(self, y_true: np.ndarray, y_pred: np.ndarray) -> float:
        """Error of ``y_pred`` against ``y_true`` under this problem's metric."""
        residual = np.asarray(y_pred, dtype=float) - y_true
        if self.metric == "mae":
            return float(np.mean(np.abs(residual)))
        mean_square = float(np.mean(residual * residual))
        return float(np.sqrt(mean_square)) if self.metric == "rmse" else mean_square

    def evaluate_predictions(self, y_pred: np.ndarray) -> ProblemEvaluation:
        """Score a prediction vector directly, without going through a predictor."""
        predictions = np.asarray(y_pred, dtype=float)
        if predictions.shape != self.y.shape or not np.all(np.isfinite(predictions)):
            return ProblemEvaluation(
                fitness=INVALID_FITNESS,
                objective=INVALID_FITNESS,
                feasible=False,
                constraint_violation=1.0,
            )
        error = self.score(self.y, predictions)
        if not np.isfinite(error):
            return ProblemEvaluation(
                fitness=INVALID_FITNESS, objective=INVALID_FITNESS, feasible=False, constraint_violation=1.0
            )
        return ProblemEvaluation(fitness=error, objective=error, feasible=True, constraint_violation=0.0)

    def evaluate(self, genome: Callable[[np.ndarray], np.ndarray]) -> ProblemEvaluation:
        """Evaluate a predictor callable on the training set."""
        try:
            with np.errstate(all="ignore"):
                predictions = np.asarray(genome(self.x), dtype=float)
                if predictions.ndim == 0:
                    predictions = np.full(self.y.shape, float(predictions))
        except (ArithmeticError, ValueError, TypeError):
            return ProblemEvaluation(
                fitness=INVALID_FITNESS, objective=INVALID_FITNESS, feasible=False, constraint_violation=1.0
            )
        return self.evaluate_predictions(predictions)

    def evaluate_test(self, genome: Callable[[np.ndarray], np.ndarray]) -> float | None:
        """Held-out error, or ``None`` when the benchmark defines no test set."""
        if self.test_x is None or self.test_y is None:
            return None
        try:
            with np.errstate(all="ignore"):
                predictions = np.asarray(genome(self.test_x), dtype=float)
                if predictions.ndim == 0:
                    predictions = np.full(self.test_y.shape, float(predictions))
            if predictions.shape != self.test_y.shape or not np.all(np.isfinite(predictions)):
                return INVALID_FITNESS
            return self.score(self.test_y, predictions)
        except (ArithmeticError, ValueError, TypeError):
            return INVALID_FITNESS

    def describe(self) -> dict[str, Any]:
        """Instance facts recorded in island_start.benchmark_params."""
        return {**super().describe(), "formula": self.formula, "metric": self.metric}
