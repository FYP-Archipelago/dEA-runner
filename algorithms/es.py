"""Evolution Strategy: self-adaptive ``(mu, lambda)`` and ``(mu + lambda)``."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from archipelago_logging import GenomeEncoding, Operator
from benchmarks.base import Domain

from .base import Algorithm, ESIndividual, Individual, IslandContext
from .diversity import mean_pairwise_euclidean

__all__ = ["EvolutionStrategy"]

SIGMA_FLOOR = 1e-12


class EvolutionStrategy(Algorithm):
    """Multimembered ES with self-adaptive step sizes."""

    name = "es"
    supported_domains = frozenset({Domain.CONTINUOUS})
    genome_encoding = GenomeEncoding.REAL_VECTOR_SIGMA

    def __init__(
        self,
        ctx: IslandContext,
        selection: str = "comma",
        offspring_per_parent: int = 7,
        sigma_mode: str = "n_sigma",
        recombination: str = "discrete",
        strategy_recombination: str = "intermediate",
        rho: int = 2,
        initial_sigma_ratio: float = 0.1,
        tau: float | None = None,
        tau_prime: float | None = None,
    ) -> None:
        if selection not in ("comma", "plus"):
            raise ValueError(f"unknown ES selection {selection!r}; expected 'comma' or 'plus'")
        if sigma_mode not in ("n_sigma", "single"):
            raise ValueError(f"unknown sigma_mode {sigma_mode!r}; expected 'n_sigma' or 'single'")
        for label, value in (("recombination", recombination), ("strategy_recombination", strategy_recombination)):
            if value not in ("discrete", "intermediate", "none"):
                raise ValueError(f"unknown {label} {value!r}")

        self.selection = selection
        self.offspring_per_parent = max(1, int(offspring_per_parent))
        self.sigma_mode = sigma_mode
        self.recombination = recombination
        self.strategy_recombination = strategy_recombination
        self.rho = max(1, int(rho))
        self.initial_sigma_ratio = float(initial_sigma_ratio)

        super().__init__(ctx)
        problem = ctx.problem
        self.lower = problem.lower
        self.upper = problem.upper
        dimension = problem.dimension
        self.dimension = dimension
        self._diagonal = float(np.linalg.norm(self.upper - self.lower))

        self.tau = float(tau) if tau is not None else 1.0 / np.sqrt(2.0 * np.sqrt(dimension))
        self.tau_prime = float(tau_prime) if tau_prime is not None else 1.0 / np.sqrt(2.0 * dimension)
        self.tau_single = 1.0 / np.sqrt(dimension)

        self.params = {
            "selection": selection,
            "offspring_per_parent": self.offspring_per_parent,
            "sigma_mode": sigma_mode,
            "recombination": recombination,
            "strategy_recombination": strategy_recombination,
            "rho": self.rho,
            "initial_sigma_ratio": self.initial_sigma_ratio,
            "tau": float(self.tau),
            "tau_prime": float(self.tau_prime),
            "tau_single": float(self.tau_single),
        }


    def genome_for_log(self, individual: Individual) -> Any:
        """Log the object variables together with their step sizes."""
        sigma = getattr(individual, "sigma", None)
        if sigma is None:
            sigma = np.zeros_like(individual.genome)
        return {"x": individual.genome, "sigma": sigma}

    def _initial_sigma(self) -> np.ndarray | float:
        scale = self.initial_sigma_ratio * (self.upper - self.lower)
        return scale if self.sigma_mode == "n_sigma" else float(np.mean(scale))


    def initialize(self, population_size: int) -> list[Individual]:
        """Uniform random object variables; step sizes at ``initial_sigma_ratio`` of the range."""
        return self.ctx.evaluate_all(
            ESIndividual(
                genome=self.problem.random_solution(self.rng),
                sigma=self._initial_sigma(),
                operator=Operator.INIT.value,
            )
            for _ in range(population_size)
        )


    def _recombine(self, parents: Sequence[ESIndividual]) -> tuple[np.ndarray, np.ndarray | float]:
        """Combine ``rho`` parents into one offspring's object variables and sigmas."""
        genomes = np.array([parent.genome for parent in parents], dtype=float)

        if self.recombination == "none" or len(parents) == 1:
            child_genome = genomes[0].copy()
        elif self.recombination == "intermediate":
            child_genome = genomes.mean(axis=0)
        else:
            choice = self.rng.integers(0, len(parents), size=self.dimension)
            child_genome = genomes[choice, np.arange(self.dimension)]

        sigmas = [parent.sigma for parent in parents]
        if self.sigma_mode == "single":
            values = np.array([float(np.mean(sigma)) for sigma in sigmas], dtype=float)
            if self.strategy_recombination == "none" or len(parents) == 1:
                child_sigma: np.ndarray | float = float(values[0])
            elif self.strategy_recombination == "discrete":
                child_sigma = float(values[self.rng.integers(0, len(parents))])
            else:
                child_sigma = float(values.mean())
        else:
            stacked = np.array(
                [np.broadcast_to(np.asarray(sigma, dtype=float), (self.dimension,)) for sigma in sigmas]
            )
            if self.strategy_recombination == "none" or len(parents) == 1:
                child_sigma = stacked[0].copy()
            elif self.strategy_recombination == "discrete":
                choice = self.rng.integers(0, len(parents), size=self.dimension)
                child_sigma = stacked[choice, np.arange(self.dimension)]
            else:
                child_sigma = stacked.mean(axis=0)

        return child_genome, child_sigma

    def _mutate(
        self, genome: np.ndarray, sigma: np.ndarray | float
    ) -> tuple[np.ndarray, np.ndarray | float]:
        """Self-adaptive mutation: step sizes first, then object variables."""
        if self.sigma_mode == "single":
            new_sigma_scalar = max(
                SIGMA_FLOOR, float(sigma) * float(np.exp(self.tau_single * self.rng.standard_normal()))
            )
            mutated = genome + new_sigma_scalar * self.rng.standard_normal(self.dimension)
            return np.clip(mutated, self.lower, self.upper), new_sigma_scalar

        sigma_vector = np.broadcast_to(np.asarray(sigma, dtype=float), (self.dimension,))
        global_step = self.tau_prime * self.rng.standard_normal()
        local_steps = self.tau * self.rng.standard_normal(self.dimension)
        new_sigma = np.maximum(SIGMA_FLOOR, sigma_vector * np.exp(global_step + local_steps))
        mutated = genome + new_sigma * self.rng.standard_normal(self.dimension)
        return np.clip(mutated, self.lower, self.upper), new_sigma

    def step(self, population: list[Individual]) -> list[Individual]:
        """Produce lambda offspring, then truncation-select mu survivors."""
        mu = len(population)
        offspring_count = self.offspring_per_parent * mu
        parents: list[ESIndividual] = [ind for ind in population]

        offspring: list[Individual] = []
        for _ in range(offspring_count):
            chosen = [
                parents[index]
                for index in self.rng.choice(mu, size=min(self.rho, mu), replace=False)
            ]
            genome, sigma = self._recombine(chosen)
            genome, sigma = self._mutate(genome, sigma)
            offspring.append(
                ESIndividual(
                    genome=genome,
                    sigma=sigma,
                    parent_ids=[parent.individual_id for parent in chosen],
                    operator=Operator.ES_MUTATION.value,
                )
            )

        offspring = self.ctx.evaluate_all(offspring)

        pool: list[Individual] = offspring if self.selection == "comma" else list(population) + offspring
        fitnesses = np.fromiter((ind.fitness for ind in pool), dtype=float, count=len(pool))
        order = np.argsort(-fitnesses if self.problem.maximising else fitnesses)
        return [pool[index] for index in order[:mu]]


    def diversity(self, population: Sequence[Individual]) -> tuple[float | None, str | None, int | None]:
        """Mean pairwise Euclidean distance across the object variables."""
        return mean_pairwise_euclidean(
            [ind.genome for ind in population], self.rng, normaliser=self._diagonal
        )

    def serialize_migrant(self, individual: Individual) -> dict[str, Any]:
        """Transfer object variables *and* step sizes."""
        sigma = getattr(individual, "sigma", None)
        if sigma is None:
            serialised_sigma: Any = None
        elif np.isscalar(sigma):
            serialised_sigma = float(sigma)
        else:
            serialised_sigma = [float(value) for value in np.asarray(sigma).ravel()]
        return {
            "genome": [float(value) for value in individual.genome],
            "sigma": serialised_sigma,
            "fitness": float(individual.fitness),
            "individual_id": individual.individual_id,
        }

    def deserialize_migrant(self, payload: dict[str, Any]) -> Individual:
        """Rebuild an individual, and its step sizes, from a payload."""
        sigma = payload.get("sigma")
        if sigma is None:
            restored: np.ndarray | float = self._initial_sigma()
        elif isinstance(sigma, list):
            restored = np.asarray(sigma, dtype=float)
            if self.sigma_mode == "single":
                restored = float(np.mean(restored))
        else:
            restored = float(sigma) if self.sigma_mode == "single" else np.full(self.dimension, float(sigma))
        return ESIndividual(
            genome=np.asarray(payload["genome"], dtype=float),
            sigma=restored,
            operator=Operator.MIGRATION_ARRIVAL.value,
            parent_ids=[payload["individual_id"]],
            origin_individual_id=payload["individual_id"],
        )
