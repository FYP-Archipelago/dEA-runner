"""Name -> algorithm-class registry."""

from __future__ import annotations

import inspect
from typing import Any

from benchmarks.base import Domain

from .base import Algorithm, IslandContext
from .de import DifferentialEvolution
from .es import EvolutionStrategy
from .ga import GeneticAlgorithm
from .gp import GeneticProgramming
from .pso import ParticleSwarmOptimization

__all__ = [
    "register",
    "create_algorithm",
    "accepted_parameters",
    "available_algorithms",
    "algorithm_class",
    "AlgorithmNotFound",
    "IncompatibleAlgorithm",
]


class AlgorithmNotFound(KeyError, ValueError):
    """Raised when a config names an algorithm that is not registered."""


class IncompatibleAlgorithm(ValueError):
    """Raised when an algorithm cannot search the selected benchmark's domain."""


_REGISTRY: dict[str, type[Algorithm]] = {}


def register(algorithm_class_: type[Algorithm]) -> type[Algorithm]:
    """Register an :class:`~algorithms.base.Algorithm` subclass under its ``name``."""
    key = algorithm_class_.name.lower()
    if key in _REGISTRY:
        raise ValueError(f"algorithm {key!r} is already registered")
    _REGISTRY[key] = algorithm_class_
    return algorithm_class_


def available_algorithms() -> dict[str, list[str]]:
    """Registered algorithm names mapped to the domains each can search."""
    return {
        name: sorted(domain.value for domain in cls.supported_domains)
        for name, cls in sorted(_REGISTRY.items())
    }


def algorithm_class(name: str) -> type[Algorithm]:
    """Look up an algorithm class without instantiating it."""
    key = name.lower()
    if key not in _REGISTRY:
        raise AlgorithmNotFound(f"unknown algorithm {name!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[key]


def check_compatible(name: str, domain: Domain) -> None:
    """Raise if ``name`` cannot search ``domain``."""
    cls = algorithm_class(name)
    if domain not in cls.supported_domains:
        raise IncompatibleAlgorithm(
            f"algorithm {name!r} cannot search the {domain.value!r} domain; "
            f"it supports {sorted(d.value for d in cls.supported_domains)}"
        )


def accepted_parameters(name: str) -> set[str]:
    """Names an algorithm accepts under ``algorithm_params``."""
    cls = algorithm_class(name)
    signature = inspect.signature(cls.__init__)
    return {
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and parameter.name not in ("self", "ctx")
    }


def create_algorithm(name: str, ctx: IslandContext, params: dict[str, Any] | None = None) -> Algorithm:
    """Instantiate a registered algorithm against an island context."""
    cls = algorithm_class(name)
    check_compatible(name, ctx.problem.domain)

    supplied = dict(params or {})
    unknown = sorted(set(supplied) - accepted_parameters(name))
    if unknown:
        raise TypeError(
            f"invalid algorithm_params for {name!r}: {unknown}; "
            f"accepted: {sorted(accepted_parameters(name))}"
        )
    return cls(ctx, **supplied)


register(GeneticAlgorithm)
register(DifferentialEvolution)
register(ParticleSwarmOptimization)
register(GeneticProgramming)
register(EvolutionStrategy)
