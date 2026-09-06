"""Migrant selection and replacement policies."""

from __future__ import annotations

from typing import Any, Callable, Sequence

import numpy as np

from algorithms.base import Individual
from benchmarks.base import Problem

__all__ = [
    "SELECTION_POLICIES",
    "REPLACEMENT_POLICIES",
    "select_migrants",
    "choose_replacements",
]


def _order_by_fitness(population: Sequence[Individual], problem: Problem) -> np.ndarray:
    fitnesses = np.fromiter((ind.fitness for ind in population), dtype=float, count=len(population))
    return np.argsort(-fitnesses if problem.maximising else fitnesses)


def _select_best(
    population: Sequence[Individual], count: int, rng: np.random.Generator, problem: Problem, **_: Any
) -> list[int]:
    """The ``count`` fittest individuals. The standard, and the most elitist."""
    return [int(index) for index in _order_by_fitness(population, problem)[:count]]


def _select_worst(
    population: Sequence[Individual], count: int, rng: np.random.Generator, problem: Problem, **_: Any
) -> list[int]:
    """The ``count`` least fit. Rarely a good policy; included as an explicit control."""
    return [int(index) for index in _order_by_fitness(population, problem)[-count:]]


def _select_random(
    population: Sequence[Individual], count: int, rng: np.random.Generator, problem: Problem, **_: Any
) -> list[int]:
    """Uniform without replacement. Maximum diversity preservation."""
    size = min(count, len(population))
    return [int(index) for index in rng.choice(len(population), size=size, replace=False)]


def _select_tournament(
    population: Sequence[Individual],
    count: int,
    rng: np.random.Generator,
    problem: Problem,
    tournament_size: int = 3,
    **_: Any,
) -> list[int]:
    """Tournament selection without replacement across draws."""
    chosen: list[int] = []
    available = list(range(len(population)))
    for _ in range(min(count, len(available))):
        contenders = rng.choice(
            available, size=min(tournament_size, len(available)), replace=False
        )
        winner = min(
            contenders,
            key=lambda index: -population[index].fitness if problem.maximising else population[index].fitness,
        )
        chosen.append(int(winner))
        available.remove(int(winner))
    return chosen


def _select_roulette(
    population: Sequence[Individual], count: int, rng: np.random.Generator, problem: Problem, **_: Any
) -> list[int]:
    """Fitness-proportionate selection over rank-transformed fitness."""
    order = _order_by_fitness(population, problem)
    weights = np.empty(len(population), dtype=float)
    weights[order] = np.arange(len(population), 0, -1, dtype=float)
    weights /= weights.sum()
    size = min(count, len(population))
    return [int(index) for index in rng.choice(len(population), size=size, replace=False, p=weights)]


def _select_diverse(
    population: Sequence[Individual],
    count: int,
    rng: np.random.Generator,
    problem: Problem,
    candidate_pool: int = 5,
    **_: Any,
) -> list[int]:
    """Greedy max-min: fit individuals that are also far apart from each other."""
    order = _order_by_fitness(population, problem)
    pool = [int(index) for index in order[: max(count, min(len(population), count * candidate_pool))]]
    if not pool:
        return []

    try:
        genomes = np.array([np.asarray(population[index].genome, dtype=float) for index in pool])
        if genomes.ndim != 2:
            raise ValueError
    except (TypeError, ValueError):
        return pool[:count]

    chosen = [0]
    while len(chosen) < min(count, len(pool)):
        remaining = [position for position in range(len(pool)) if position not in chosen]
        distances = [
            min(float(np.linalg.norm(genomes[position] - genomes[picked])) for picked in chosen)
            for position in remaining
        ]
        chosen.append(remaining[int(np.argmax(distances))])
    return [pool[position] for position in chosen]


SELECTION_POLICIES: dict[str, Callable[..., list[int]]] = {
    "best": _select_best,
    "worst": _select_worst,
    "random": _select_random,
    "tournament": _select_tournament,
    "roulette": _select_roulette,
    "diverse": _select_diverse,
}


def select_migrants(
    policy: str,
    population: Sequence[Individual],
    count: int,
    rng: np.random.Generator,
    problem: Problem,
    **params: Any,
) -> list[int]:
    """Return the indices of the individuals to emigrate."""
    key = policy.lower()
    if key not in SELECTION_POLICIES:
        raise ValueError(f"unknown migrant selection policy {policy!r}; available: {sorted(SELECTION_POLICIES)}")
    if not population or count <= 0:
        return []
    return SELECTION_POLICIES[key](population, min(count, len(population)), rng, problem, **params)


def _replace_worst(
    population: Sequence[Individual],
    incoming: Sequence[Individual],
    rng: np.random.Generator,
    problem: Problem,
    **_: Any,
) -> list[int]:
    """Evict the least fit. The standard pairing with ``best`` selection."""
    return [int(index) for index in _order_by_fitness(population, problem)[-len(incoming) :]]


def _replace_random(
    population: Sequence[Individual],
    incoming: Sequence[Individual],
    rng: np.random.Generator,
    problem: Problem,
    **_: Any,
) -> list[int]:
    """Evict uniformly at random. Weakest selection pressure from migration."""
    size = min(len(incoming), len(population))
    return [int(index) for index in rng.choice(len(population), size=size, replace=False)]


def _replace_similar(
    population: Sequence[Individual],
    incoming: Sequence[Individual],
    rng: np.random.Generator,
    problem: Problem,
    candidate_pool: int = 10,
    **_: Any,
) -> list[int]:
    """Crowding: each immigrant displaces the individual most like itself."""
    try:
        incoming_genomes = np.array([np.asarray(ind.genome, dtype=float) for ind in incoming])
        if incoming_genomes.ndim != 2:
            raise ValueError
    except (TypeError, ValueError):
        return _replace_worst(population, incoming, rng, problem)

    pool_size = min(len(population), max(len(incoming), candidate_pool * len(incoming)))
    pool = [int(index) for index in rng.choice(len(population), size=pool_size, replace=False)]
    try:
        pool_genomes = np.array([np.asarray(population[index].genome, dtype=float) for index in pool])
    except (TypeError, ValueError):
        return _replace_worst(population, incoming, rng, problem)

    victims: list[int] = []
    taken: set[int] = set()
    for genome in incoming_genomes:
        distances = np.linalg.norm(pool_genomes - genome, axis=1)
        for position in np.argsort(distances):
            candidate = pool[int(position)]
            if candidate not in taken:
                taken.add(candidate)
                victims.append(candidate)
                break
    return victims


def _replace_none(
    population: Sequence[Individual],
    incoming: Sequence[Individual],
    rng: np.random.Generator,
    problem: Problem,
    **_: Any,
) -> list[int]:
    """Displace nobody: immigrants are appended and the population grows."""
    return []


REPLACEMENT_POLICIES: dict[str, Callable[..., list[int]]] = {
    "worst": _replace_worst,
    "random": _replace_random,
    "similar": _replace_similar,
    "none": _replace_none,
}


def choose_replacements(
    policy: str,
    population: Sequence[Individual],
    incoming: Sequence[Individual],
    rng: np.random.Generator,
    problem: Problem,
    **params: Any,
) -> list[int]:
    """Return the indices of the individuals the immigrants displace."""
    key = policy.lower()
    if key not in REPLACEMENT_POLICIES:
        raise ValueError(
            f"unknown migrant replacement policy {policy!r}; available: {sorted(REPLACEMENT_POLICIES)}"
        )
    if not population or not incoming:
        return []
    return REPLACEMENT_POLICIES[key](population, incoming, rng, problem, **params)
