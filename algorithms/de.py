"""Differential Evolution."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from archipelago_logging import GenomeEncoding, Operator
from benchmarks.base import Domain

from .base import Algorithm, Individual, IslandContext
from .diversity import mean_pairwise_euclidean

__all__ = ["DifferentialEvolution", "MUTATION_STRATEGIES", "BOUND_HANDLERS"]

MUTATION_STRATEGIES = (
    "rand/1",
    "rand/2",
    "best/1",
    "best/2",
    "current-to-best/1",
    "current-to-rand/1",
)

BOUND_HANDLERS = ("clip", "reflect", "reinitialize", "midpoint")


def _apply_bounds(
    trial: np.ndarray,
    target: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    method: str,
    rng: np.random.Generator,
) -> np.ndarray:
    """Return ``trial`` projected back into the box."""
    if method == "clip":
        return np.clip(trial, lower, upper)
    if method == "reflect":
        span = upper - lower
        folded = trial.copy()
        below, above = folded < lower, folded > upper
        folded[below] = lower[below] + (lower[below] - folded[below]) % span[below]
        folded[above] = upper[above] - (folded[above] - upper[above]) % span[above]
        return np.clip(folded, lower, upper)
    if method == "reinitialize":
        outside = (trial < lower) | (trial > upper)
        replacement = trial.copy()
        replacement[outside] = rng.uniform(lower[outside], upper[outside])
        return replacement
    if method == "midpoint":
        adjusted = trial.copy()
        below, above = adjusted < lower, adjusted > upper
        adjusted[below] = (target[below] + lower[below]) / 2.0
        adjusted[above] = (target[above] + upper[above]) / 2.0
        return adjusted
    raise ValueError(f"unknown bound handler {method!r}; available: {list(BOUND_HANDLERS)}")


class DifferentialEvolution(Algorithm):
    """``DE/x/y/z`` over real vectors."""

    name = "de"
    supported_domains = frozenset({Domain.CONTINUOUS})
    genome_encoding = GenomeEncoding.REAL_VECTOR

    def __init__(
        self,
        ctx: IslandContext,
        strategy: str = "rand/1",
        f: float = 0.5,
        cr: float = 0.9,
        crossover: str = "bin",
        dither: bool = False,
        dither_range: tuple[float, float] = (0.5, 1.0),
        bound_handling: str = "midpoint",
    ) -> None:
        if strategy not in MUTATION_STRATEGIES:
            raise ValueError(f"unknown DE strategy {strategy!r}; available: {list(MUTATION_STRATEGIES)}")
        if crossover not in ("bin", "exp"):
            raise ValueError(f"unknown DE crossover {crossover!r}; expected 'bin' or 'exp'")
        if bound_handling not in BOUND_HANDLERS:
            raise ValueError(f"unknown bound handling {bound_handling!r}; available: {list(BOUND_HANDLERS)}")

        self.strategy = strategy
        self.f = float(f)
        self.cr = float(cr)
        self.crossover = crossover
        self.dither = bool(dither)
        self.dither_range = (float(dither_range[0]), float(dither_range[1]))
        self.bound_handling = bound_handling

        super().__init__(ctx)
        self.lower = ctx.problem.lower
        self.upper = ctx.problem.upper
        self._diagonal = float(np.linalg.norm(self.upper - self.lower))
        self.params = {
            "strategy": strategy,
            "f": self.f,
            "cr": self.cr,
            "crossover": crossover,
            "dither": self.dither,
            "dither_range": list(self.dither_range),
            "bound_handling": bound_handling,
        }


    def initialize(self, population_size: int) -> list[Individual]:
        """Uniform random initialisation."""
        minimum = 4 if "/2" not in self.strategy else 6
        if population_size < minimum:
            raise ValueError(
                f"DE strategy {self.strategy!r} needs at least {minimum} individuals per island, "
                f"got population_size={population_size}"
            )
        return self.ctx.evaluate_all(
            Individual(genome=self.problem.random_solution(self.rng), operator=Operator.INIT.value)
            for _ in range(population_size)
        )


    def _distinct_indices(self, population_size: int, exclude: int, count: int) -> np.ndarray:
        """Draw ``count`` indices distinct from each other and from ``exclude``."""
        candidates = self.rng.permutation(population_size)
        candidates = candidates[candidates != exclude]
        return candidates[:count]

    def _donor(
        self, population: Sequence[Individual], index: int, best_index: int, scale: float
    ) -> tuple[np.ndarray, list[int]]:
        """Build the donor vector and report which individuals contributed."""
        genomes = [ind.genome for ind in population]
        target = genomes[index]

        if self.strategy == "rand/1":
            r = self._distinct_indices(len(population), index, 3)
            return genomes[r[0]] + scale * (genomes[r[1]] - genomes[r[2]]), list(r)
        if self.strategy == "rand/2":
            r = self._distinct_indices(len(population), index, 5)
            donor = (
                genomes[r[0]]
                + scale * (genomes[r[1]] - genomes[r[2]])
                + scale * (genomes[r[3]] - genomes[r[4]])
            )
            return donor, list(r)
        if self.strategy == "best/1":
            r = self._distinct_indices(len(population), index, 2)
            return genomes[best_index] + scale * (genomes[r[0]] - genomes[r[1]]), [best_index, *r]
        if self.strategy == "best/2":
            r = self._distinct_indices(len(population), index, 4)
            donor = (
                genomes[best_index]
                + scale * (genomes[r[0]] - genomes[r[1]])
                + scale * (genomes[r[2]] - genomes[r[3]])
            )
            return donor, [best_index, *r]
        if self.strategy == "current-to-best/1":
            r = self._distinct_indices(len(population), index, 2)
            donor = (
                target
                + scale * (genomes[best_index] - target)
                + scale * (genomes[r[0]] - genomes[r[1]])
            )
            return donor, [best_index, *r]
        r = self._distinct_indices(len(population), index, 3)
        k = self.rng.random()
        donor = target + k * (genomes[r[0]] - target) + scale * (genomes[r[1]] - genomes[r[2]])
        return donor, list(r)

    def _binomial_crossover(self, target: np.ndarray, donor: np.ndarray) -> np.ndarray:
        mask = self.rng.random(target.size) <= self.cr
        mask[self.rng.integers(0, target.size)] = True
        return np.where(mask, donor, target)

    def _exponential_crossover(self, target: np.ndarray, donor: np.ndarray) -> np.ndarray:
        """Exponential crossover: one contiguous circular block from the donor."""
        dimension = target.size
        trial = target.copy()
        start = int(self.rng.integers(0, dimension))
        length = 1
        while length < dimension and self.rng.random() <= self.cr:
            length += 1
        indices = (start + np.arange(length)) % dimension
        trial[indices] = donor[indices]
        return trial

    def step(self, population: list[Individual]) -> list[Individual]:
        """One DE generation: mutate, cross, greedily select."""
        fitnesses = np.fromiter((ind.fitness for ind in population), dtype=float, count=len(population))
        best_index = int(np.argmax(fitnesses) if self.problem.maximising else np.argmin(fitnesses))

        next_population: list[Individual] = []
        for index, target_individual in enumerate(population):
            scale = (
                self.rng.uniform(*self.dither_range) if self.dither else self.f
            )
            donor, contributors = self._donor(population, index, best_index, scale)

            if self.strategy == "current-to-rand/1":
                trial_genome = donor
            elif self.crossover == "bin":
                trial_genome = self._binomial_crossover(target_individual.genome, donor)
            else:
                trial_genome = self._exponential_crossover(target_individual.genome, donor)

            trial_genome = _apply_bounds(
                trial_genome, target_individual.genome, self.lower, self.upper, self.bound_handling, self.rng
            )

            trial = self.ctx.evaluate(
                Individual(
                    genome=trial_genome,
                    parent_ids=[target_individual.individual_id]
                    + [population[contributor].individual_id for contributor in contributors],
                    operator=Operator.DE_TRIAL.value,
                )
            )

            if not self.problem.is_better(target_individual.fitness, trial.fitness):
                next_population.append(trial)
            else:
                next_population.append(target_individual)

        return next_population


    def diversity(self, population: Sequence[Individual]) -> tuple[float | None, str | None, int | None]:
        """Mean pairwise Euclidean distance across the population."""
        return mean_pairwise_euclidean(
            [ind.genome for ind in population], self.rng, normaliser=self._diagonal
        )

    def serialize_migrant(self, individual: Individual) -> dict[str, Any]:
        """Pack an individual into a JSON-safe payload for transfer."""
        return {
            "genome": [float(value) for value in individual.genome],
            "fitness": float(individual.fitness),
            "individual_id": individual.individual_id,
        }

    def deserialize_migrant(self, payload: dict[str, Any]) -> Individual:
        """Rebuild an individual from a transferred payload."""
        return Individual(
            genome=np.asarray(payload["genome"], dtype=float),
            operator=Operator.MIGRATION_ARRIVAL.value,
            parent_ids=[payload["individual_id"]],
            origin_individual_id=payload["individual_id"],
        )
