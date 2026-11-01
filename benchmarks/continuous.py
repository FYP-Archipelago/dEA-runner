"""Classic continuous black-box benchmark functions, plus CEC-style transforms."""

from __future__ import annotations

from typing import Any

import numpy as np

from .base import ContinuousProblem, ProblemEvaluation, resolve_instance_path

__all__ = [
    "Sphere",
    "Rastrigin",
    "Ackley",
    "Rosenbrock",
    "Griewank",
    "Schwefel",
    "ShiftedRotated",
    "CONTINUOUS_FUNCTIONS",
]


class _AnalyticFunction(ContinuousProblem):
    """Shared plumbing: count evaluations, return a plain unconstrained result."""

    maximising = False
    known_optimum = 0.0

    def evaluate(self, genome: np.ndarray) -> ProblemEvaluation:
        """Score one point against the analytic function."""
        value = float(self._function(np.asarray(genome, dtype=float)))
        return ProblemEvaluation(fitness=value)

    def _function(self, x: np.ndarray) -> float:
        raise NotImplementedError


class Sphere(_AnalyticFunction):
    r"""De Jong's F1: :math:`f(x) = \sum_{i=1}^{n} x_i^2`."""

    name = "sphere"

    def __init__(self, dimension: int = 30, lower: float = -5.12, upper: float = 5.12, **params: Any) -> None:
        super().__init__(dimension, lower, upper, **params)

    def _function(self, x: np.ndarray) -> float:
        return float(np.sum(x * x))


class Rastrigin(_AnalyticFunction):
    r"""Rastrigin: :math:`f(x) = 10n + \sum_{i=1}^{n} \left(x_i^2 - 10\cos(2\pi x_i)\right)`."""

    name = "rastrigin"

    def __init__(self, dimension: int = 30, lower: float = -5.12, upper: float = 5.12, **params: Any) -> None:
        super().__init__(dimension, lower, upper, **params)

    def _function(self, x: np.ndarray) -> float:
        return float(10.0 * x.size + np.sum(x * x - 10.0 * np.cos(2.0 * np.pi * x)))


class Ackley(_AnalyticFunction):
    """Ackley's function."""

    name = "ackley"

    def __init__(
        self,
        dimension: int = 30,
        lower: float = -32.768,
        upper: float = 32.768,
        a: float = 20.0,
        b: float = 0.2,
        c: float = 2.0 * np.pi,
        **params: Any,
    ) -> None:
        super().__init__(dimension, lower, upper, a=a, b=b, c=c, **params)
        self.a, self.b, self.c = float(a), float(b), float(c)

    def _function(self, x: np.ndarray) -> float:
        n = x.size
        term1 = -self.a * np.exp(-self.b * np.sqrt(np.sum(x * x) / n))
        term2 = -np.exp(np.sum(np.cos(self.c * x)) / n)
        return float(term1 + term2 + self.a + np.e)


class Rosenbrock(_AnalyticFunction):
    """Rosenbrock's valley (De Jong's F2 generalised to n dimensions)."""

    name = "rosenbrock"
    known_optimum = 0.0

    def __init__(self, dimension: int = 30, lower: float = -5.0, upper: float = 10.0, **params: Any) -> None:
        if dimension < 2:
            raise ValueError("Rosenbrock requires dimension >= 2")
        super().__init__(dimension, lower, upper, **params)

    def _function(self, x: np.ndarray) -> float:
        head, tail = x[:-1], x[1:]
        return float(np.sum(100.0 * (tail - head * head) ** 2 + (head - 1.0) ** 2))


class Griewank(_AnalyticFunction):
    """Griewank's function."""

    name = "griewank"

    def __init__(self, dimension: int = 30, lower: float = -600.0, upper: float = 600.0, **params: Any) -> None:
        super().__init__(dimension, lower, upper, **params)

    def _function(self, x: np.ndarray) -> float:
        indices = np.arange(1, x.size + 1, dtype=float)
        return float(1.0 + np.sum(x * x) / 4000.0 - np.prod(np.cos(x / np.sqrt(indices))))


class Schwefel(_AnalyticFunction):
    """Schwefel's function 2.26."""

    name = "schwefel"

    def __init__(self, dimension: int = 30, lower: float = -500.0, upper: float = 500.0, **params: Any) -> None:
        super().__init__(dimension, lower, upper, **params)

    def _function(self, x: np.ndarray) -> float:
        return float(418.9828872724339 * x.size - np.sum(x * np.sin(np.sqrt(np.abs(x)))))


CONTINUOUS_FUNCTIONS: dict[str, type[_AnalyticFunction]] = {
    "sphere": Sphere,
    "rastrigin": Rastrigin,
    "ackley": Ackley,
    "rosenbrock": Rosenbrock,
    "griewank": Griewank,
    "schwefel": Schwefel,
}


CEC_DOMAIN = (-100.0, 100.0)


def _load_rotation_matrix(path: str, dimension: int) -> np.ndarray:
    """Load a ``dimension x dimension`` rotation matrix and verify it *is* one."""
    path = resolve_instance_path(path, what="CEC rotation file")
    values = np.loadtxt(path).astype(float)
    expected = dimension * dimension
    if values.size < expected:
        raise ValueError(
            f"{path}: holds {values.size} values, too few for a {dimension}x{dimension} matrix"
        )
    matrix = values.ravel()[:expected].reshape(dimension, dimension)

    error = float(np.max(np.abs(matrix @ matrix.T - np.eye(dimension))))
    if error > 1e-6:
        raise ValueError(
            f"{path}: is not an orthogonal matrix (max |M M^T - I| = {error:.3g}), so it is not a "
            "rotation. Using it would rescale and shear the search space rather than rotate it, "
            "producing a problem that resembles the named benchmark without being it. Check that "
            "the file matches the requested dimension and comes from the official CEC "
            "distribution; several public mirrors carry files that fail this test."
        )
    return matrix


class ShiftedRotated(ContinuousProblem):
    """CEC-style shifted and rotated wrapper: :math:`g(x) = f(M(x - o))`."""

    maximising = False
    known_optimum = 0.0

    def __init__(
        self,
        function: str = "rastrigin",
        dimension: int = 30,
        shift_file: str | None = None,
        rotation_file: str | None = None,
        lower: float | None = None,
        upper: float | None = None,
        **params: Any,
    ) -> None:
        key = function.lower()
        if key not in CONTINUOUS_FUNCTIONS:
            raise ValueError(
                f"unknown base function {function!r}; available: {sorted(CONTINUOUS_FUNCTIONS)}"
            )
        base = CONTINUOUS_FUNCTIONS[key](dimension=dimension)

        if lower is not None and upper is not None:
            domain_lower, domain_upper = float(lower), float(upper)
        elif shift_file or rotation_file:
            domain_lower, domain_upper = CEC_DOMAIN
        else:
            domain_lower, domain_upper = base.lower, base.upper

        super().__init__(
            dimension,
            domain_lower,
            domain_upper,
            function=key,
            shift_file=shift_file,
            rotation_file=rotation_file,
            **params,
        )
        self.name = f"shifted_rotated_{key}" if rotation_file else f"shifted_{key}"
        self._base = base

        half_width = (self.upper - self.lower) / 2.0

        if shift_file:
            shift_path = resolve_instance_path(shift_file, what="CEC shift file")
            values = np.loadtxt(shift_path).ravel().astype(float)
            if values.size < dimension:
                raise ValueError(
                    f"{shift_file}: holds {values.size} values but dimension={dimension} was "
                    "requested. CEC shift files carry the maximum dimension (usually 100) and "
                    "are truncated, never padded."
                )
            shift = values[:dimension]
            if np.any(np.abs(shift) > half_width):
                raise ValueError(
                    f"{shift_file}: shift lies outside the domain of {key!r} "
                    f"(|shift| max {np.max(np.abs(shift)):.3f} > half-width "
                    f"{float(np.max(half_width)):.3f}). The file is for a function with a "
                    "different domain, or it is not a shift vector."
                )
        else:
            shift = np.zeros(dimension, dtype=float)
        self.shift = shift

        self.rotation = (
            _load_rotation_matrix(rotation_file, dimension)
            if rotation_file
            else np.eye(dimension)
        )

    def evaluate(self, genome: np.ndarray) -> ProblemEvaluation:
        """Score one point through the shift and rotation."""
        x = np.asarray(genome, dtype=float)
        value = float(self._base._function(self.rotation @ (x - self.shift)))
        return ProblemEvaluation(fitness=value)

    def describe(self) -> dict[str, Any]:
        """Instance facts recorded in island_start.benchmark_params."""
        return {
            **super().describe(),
            "base_function": self._base.name,
            "shift_l2_norm": float(np.linalg.norm(self.shift)),
            "shifted": bool(np.any(self.shift)),
            "rotated": not np.allclose(self.rotation, np.eye(self.dimension)),
        }
