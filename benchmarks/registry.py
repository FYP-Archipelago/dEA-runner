"""Name -> problem-factory registry."""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from .base import Domain, Problem
from .continuous import CONTINUOUS_FUNCTIONS, ShiftedRotated
from .knapsack import Knapsack
from .symbolic import REAL_REGRESSION_DATASETS, SYMBOLIC_BENCHMARKS, SymbolicRegression
from .tsp import TSP

__all__ = [
    "register",
    "register_problem",
    "create_problem",
    "available_benchmarks",
    "benchmark_domain",
    "BenchmarkNotFound",
]


class BenchmarkNotFound(KeyError, ValueError):
    """Raised when a config names a benchmark that is not registered."""


_REGISTRY: dict[str, Callable[..., Problem]] = {}
_DOMAINS: dict[str, Domain] = {}


def register(name: str, factory: Callable[..., Problem], domain: Domain) -> None:
    """Register ``factory`` under ``name``."""
    key = name.lower()
    if key in _REGISTRY:
        raise ValueError(f"benchmark {name!r} is already registered")
    _REGISTRY[key] = factory
    _DOMAINS[key] = domain


def register_problem(name: str, domain: Domain) -> Callable[[Callable[..., Problem]], Callable[..., Problem]]:
    """Decorator form of :func:`register`."""
    def decorator(factory: Callable[..., Problem]) -> Callable[..., Problem]:
        """Register the decorated factory and return it unchanged."""
        register(name, factory, domain)
        return factory

    return decorator


def available_benchmarks() -> dict[str, str]:
    """Registered benchmark names mapped to their domain, for error messages and ``--list``."""
    return {name: domain.value for name, domain in sorted(_DOMAINS.items())}


def benchmark_domain(name: str) -> Domain:
    """Domain of a registered benchmark, without instantiating it."""
    key = name.lower()
    if key not in _DOMAINS:
        raise BenchmarkNotFound(
            f"unknown benchmark {name!r}; available: {sorted(_REGISTRY)}"
        )
    return _DOMAINS[key]


def _accepted_parameters(factory: Callable[..., Problem]) -> set[str] | None:
    """Names ``factory`` accepts as keyword arguments, or ``None`` if undecidable."""
    target = factory
    while isinstance(target, functools.partial):
        target = target.func

    initialisers = (
        [target.__init__ for target in target.__mro__ if "__init__" in vars(target)]
        if inspect.isclass(target)
        else [target]
    )

    names: set[str] = set()
    for initialiser in initialisers:
        try:
            signature = inspect.signature(initialiser)
        except (TypeError, ValueError):
            return None
        for parameter in signature.parameters.values():
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ) and parameter.name not in ("self", "cls"):
                names.add(parameter.name)
    return names


def create_problem(name: str, params: dict[str, Any] | None = None) -> Problem:
    """Instantiate a registered benchmark."""
    key = name.lower()
    if key not in _REGISTRY:
        raise BenchmarkNotFound(f"unknown benchmark {name!r}; available: {sorted(_REGISTRY)}")

    factory = _REGISTRY[key]
    supplied = dict(params or {})
    accepted = _accepted_parameters(factory)
    if accepted is not None:
        unknown = sorted(set(supplied) - accepted)
        if unknown:
            raise TypeError(
                f"invalid parameters for benchmark {name!r}: {unknown}; "
                f"accepted: {sorted(accepted)}"
            )
    try:
        return factory(**supplied)
    except TypeError as exc:
        raise TypeError(f"invalid parameters for benchmark {name!r}: {exc}") from exc


for _function_name, _function_class in CONTINUOUS_FUNCTIONS.items():
    register(_function_name, _function_class, Domain.CONTINUOUS)

for _function_name in CONTINUOUS_FUNCTIONS:
    register(
        f"shifted_rotated_{_function_name}",
        functools.partial(ShiftedRotated, function=_function_name),
        Domain.CONTINUOUS,
    )

register("shifted_rotated", ShiftedRotated, Domain.CONTINUOUS)

register("knapsack", Knapsack, Domain.BINARY)
register("tsp", TSP, Domain.PERMUTATION)

for _symbolic_name in SYMBOLIC_BENCHMARKS:
    register(
        _symbolic_name,
        functools.partial(SymbolicRegression, benchmark=_symbolic_name),
        Domain.SYMBOLIC_REGRESSION,
    )

for _real_name in REAL_REGRESSION_DATASETS:
    register(
        _real_name,
        functools.partial(SymbolicRegression, benchmark=_real_name),
        Domain.SYMBOLIC_REGRESSION,
    )

register("symbolic", SymbolicRegression, Domain.SYMBOLIC_REGRESSION)
