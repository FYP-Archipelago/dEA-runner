"""Problem abstraction shared by every algorithm."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

import numpy as np

__all__ = [
    "Domain",
    "ProblemEvaluation",
    "Problem",
    "ContinuousProblem",
    "BinaryProblem",
    "PermutationProblem",
    "RegressionProblem",
    "DATA_ROOT",
    "resolve_instance_path",
]

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

DATA_ROOT = REPOSITORY_ROOT / "benchmarks" / "data"


def resolve_instance_path(path: str | Path, *, what: str = "instance file") -> Path:
    """Resolve a configured instance path, and fail loudly if it is not there."""
    candidate = Path(path)
    resolved = candidate if candidate.is_absolute() else REPOSITORY_ROOT / candidate
    if not resolved.exists():
        raise FileNotFoundError(
            f"{what} not found: {resolved}. Instance files are required -- the Runner "
            f"does not generate substitutes. The shipped instances are committed under "
            f"benchmarks/data/; restore a missing one from git."
        )
    return resolved


class Domain(str, Enum):
    """The kind of search space a problem defines."""

    CONTINUOUS = "continuous"
    BINARY = "binary"
    PERMUTATION = "permutation"
    SYMBOLIC_REGRESSION = "symbolic_regression"


@dataclass(frozen=True)
class ProblemEvaluation:
    """Result of evaluating one candidate solution."""

    fitness: float
    objective: float | None = None
    feasible: bool | None = None
    constraint_violation: float | None = None

    def __post_init__(self) -> None:
        if self.objective is None:
            object.__setattr__(self, "objective", self.fitness)


class Problem(ABC):
    """Base class for every benchmark problem."""

    name: str = "unnamed"
    domain: Domain = Domain.CONTINUOUS
    maximising: bool = False
    known_optimum: float | None = None

    def __init__(self, **params: Any) -> None:
        self.params: dict[str, Any] = dict(params)


    @abstractmethod
    def evaluate(self, genome: Any) -> ProblemEvaluation:
        """Evaluate one candidate solution."""

    @abstractmethod
    def random_solution(self, rng: np.random.Generator) -> Any:
        """Sample one uniformly random legal point in the search space."""


    def is_better(self, a: float, b: float) -> bool:
        """``True`` if fitness ``a`` is strictly better than ``b``."""
        return a > b if self.maximising else a < b

    def worst_possible(self) -> float:
        """The identity element for "best so far" accumulation."""
        return float("-inf") if self.maximising else float("inf")

    def describe(self) -> dict[str, Any]:
        """JSON-serialisable description, logged in ``island_start.benchmark_params``."""
        return {
            "name": self.name,
            "domain": self.domain.value,
            "maximising": self.maximising,
            "known_optimum": self.known_optimum,
            **{key: _jsonable(value) for key, value in self.params.items()},
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r} domain={self.domain.value}>"


def _jsonable(value: Any) -> Any:
    """Reduce numpy and other non-JSON types for the ``describe`` payload."""
    if isinstance(value, np.ndarray):
        if value.size > 64:
            return {
                "__array__": True,
                "shape": list(value.shape),
                "dtype": str(value.dtype),
                "note": "elided; regenerate from the instance seed",
            }
        return value.tolist()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


class ContinuousProblem(Problem):
    """A box-constrained real-valued minimisation (or maximisation) problem."""

    domain = Domain.CONTINUOUS

    def __init__(self, dimension: int, lower: float | Sequence[float], upper: float | Sequence[float], **params: Any) -> None:
        super().__init__(dimension=dimension, lower=lower, upper=upper, **params)
        self.dimension = int(dimension)
        self.lower = np.full(self.dimension, lower, dtype=float) if np.isscalar(lower) else np.asarray(lower, dtype=float)
        self.upper = np.full(self.dimension, upper, dtype=float) if np.isscalar(upper) else np.asarray(upper, dtype=float)
        if self.lower.shape != (self.dimension,) or self.upper.shape != (self.dimension,):
            raise ValueError("bounds must be scalar or of length `dimension`")
        if np.any(self.lower >= self.upper):
            raise ValueError("every lower bound must be strictly below its upper bound")

    def random_solution(self, rng: np.random.Generator) -> np.ndarray:
        """Draw a point uniformly from the box."""
        return rng.uniform(self.lower, self.upper)

    @property
    def range(self) -> np.ndarray:
        """Width of the box in each dimension."""
        return self.upper - self.lower


class BinaryProblem(Problem):
    """A fixed-length binary-string problem."""

    domain = Domain.BINARY

    def __init__(self, dimension: int, **params: Any) -> None:
        super().__init__(dimension=dimension, **params)
        self.dimension = int(dimension)

    def random_solution(self, rng: np.random.Generator) -> np.ndarray:
        """Draw a uniform random bitstring."""
        return rng.integers(0, 2, size=self.dimension, dtype=np.int8)


class PermutationProblem(Problem):
    """A problem whose solutions are permutations of ``0 .. dimension - 1``."""

    domain = Domain.PERMUTATION

    def __init__(self, dimension: int, **params: Any) -> None:
        super().__init__(dimension=dimension, **params)
        self.dimension = int(dimension)

    def random_solution(self, rng: np.random.Generator) -> np.ndarray:
        """Draw a uniform random permutation."""
        return rng.permutation(self.dimension)


class RegressionProblem(Problem):
    """A symbolic-regression problem: fit ``y`` from ``X`` with an expression tree."""

    domain = Domain.SYMBOLIC_REGRESSION

    def __init__(
        self,
        x: np.ndarray,
        y: np.ndarray,
        variable_names: Sequence[str],
        *,
        test_x: np.ndarray | None = None,
        test_y: np.ndarray | None = None,
        **params: Any,
    ) -> None:
        super().__init__(**params)
        self.x = np.asarray(x, dtype=float)
        self.y = np.asarray(y, dtype=float)
        self.variable_names = list(variable_names)
        self.test_x = None if test_x is None else np.asarray(test_x, dtype=float)
        self.test_y = None if test_y is None else np.asarray(test_y, dtype=float)
        if self.x.ndim != 2:
            raise ValueError("x must be 2-dimensional (samples x variables)")
        if self.x.shape[0] != self.y.shape[0]:
            raise ValueError("x and y must have the same number of samples")
        if self.x.shape[1] != len(self.variable_names):
            raise ValueError("variable_names must name every column of x")

    def random_solution(self, rng: np.random.Generator) -> Any:
        """Unused: GP builds its own trees rather than sampling genomes."""
        raise NotImplementedError(
            "tree initialisation belongs to the GP algorithm (ramped half-and-half), "
            "not to the benchmark"
        )

    def describe(self) -> dict[str, Any]:
        """Instance facts recorded in island_start.benchmark_params."""
        return {
            **super().describe(),
            "num_samples": int(self.x.shape[0]),
            "variable_names": list(self.variable_names),
            "has_test_set": self.test_x is not None,
        }
