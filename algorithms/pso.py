"""Particle Swarm Optimization."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from archipelago_logging import GenomeEncoding, Operator
from benchmarks.base import Domain

from .base import Algorithm, Individual, IslandContext, Particle
from .diversity import mean_pairwise_euclidean

__all__ = ["ParticleSwarmOptimization"]


class ParticleSwarmOptimization(Algorithm):
    """PSO with inertia or constriction, and a global or ring topology."""

    name = "pso"
    supported_domains = frozenset({Domain.CONTINUOUS})
    genome_encoding = GenomeEncoding.REAL_VECTOR_VELOCITY

    def __init__(
        self,
        ctx: IslandContext,
        topology: str = "gbest",
        neighbourhood_size: int = 1,
        inertia: float = 0.7298,
        inertia_final: float | None = None,
        cognitive: float = 1.49445,
        social: float = 1.49445,
        constriction: bool = False,
        velocity_clamp: float = 0.5,
        bound_handling: str = "clip_zero_velocity",
        total_generations: int | None = None,
    ) -> None:
        if topology not in ("gbest", "ring"):
            raise ValueError(f"unknown PSO topology {topology!r}; expected 'gbest' or 'ring'")
        if bound_handling not in ("clip", "clip_zero_velocity"):
            raise ValueError(f"unknown bound handling {bound_handling!r}")

        self.topology = topology
        self.neighbourhood_size = max(1, int(neighbourhood_size))
        self.inertia_initial = float(inertia)
        self.inertia_final = float(inertia if inertia_final is None else inertia_final)
        self.cognitive = float(cognitive)
        self.social = float(social)
        self.constriction = bool(constriction)
        self.velocity_clamp = float(velocity_clamp)
        self.bound_handling = bound_handling
        self.total_generations = total_generations

        if self.constriction:
            phi = self.cognitive + self.social
            if phi <= 4.0:
                raise ValueError(
                    f"constriction requires cognitive + social > 4, got {phi}. "
                    "The standard setting is c1 = c2 = 2.05."
                )
            self.chi = 2.0 / abs(2.0 - phi - np.sqrt(phi * phi - 4.0 * phi))
        else:
            self.chi = 1.0

        super().__init__(ctx)
        self.lower = ctx.problem.lower
        self.upper = ctx.problem.upper
        self.v_max = self.velocity_clamp * (self.upper - self.lower)
        self._diagonal = float(np.linalg.norm(self.upper - self.lower))
        self._generation = 0

        self.params = {
            "topology": topology,
            "neighbourhood_size": self.neighbourhood_size,
            "inertia": self.inertia_initial,
            "inertia_final": self.inertia_final,
            "cognitive": self.cognitive,
            "social": self.social,
            "constriction": self.constriction,
            "constriction_chi": float(self.chi),
            "velocity_clamp": self.velocity_clamp,
            "bound_handling": bound_handling,
        }


    def genome_for_log(self, individual: Individual) -> Any:
        """Position and velocity together; the hash uses position alone."""
        velocity = getattr(individual, "velocity", None)
        return {
            "x": individual.genome,
            "v": np.zeros_like(individual.genome) if velocity is None else velocity,
        }


    def initialize(self, population_size: int) -> list[Individual]:
        """Uniform positions; velocities uniform in ``+/- v_max``."""
        particles: list[Particle] = []
        for _ in range(population_size):
            position = self.problem.random_solution(self.rng)
            particles.append(
                Particle(
                    genome=position,
                    velocity=self.rng.uniform(-self.v_max, self.v_max),
                    operator=Operator.INIT.value,
                )
            )
        evaluated = self.ctx.evaluate_all(particles)
        for particle in evaluated:
            particle.best_genome = particle.genome.copy()
            particle.best_fitness = particle.fitness
        return evaluated

    def _informant_indices(self, population_size: int) -> np.ndarray:
        """Index of each particle's best informant under the configured topology."""
        best_fitnesses = np.array(
            [getattr(particle, "best_fitness", particle.fitness) for particle in self._population],
            dtype=float,
        )
        picker = np.argmax if self.problem.maximising else np.argmin

        if self.topology == "gbest":
            return np.full(population_size, int(picker(best_fitnesses)), dtype=int)

        informants = np.empty(population_size, dtype=int)
        span = self.neighbourhood_size
        for index in range(population_size):
            window = [(index + offset) % population_size for offset in range(-span, span + 1)]
            local = picker(best_fitnesses[window])
            informants[index] = window[int(local)]
        return informants

    def _inertia(self) -> float:
        """Linearly decreasing inertia weight."""
        if self.total_generations is None or self.total_generations <= 1:
            return self.inertia_initial
        progress = min(1.0, self._generation / (self.total_generations - 1))
        return self.inertia_initial + progress * (self.inertia_final - self.inertia_initial)

    def step(self, population: list[Individual]) -> list[Individual]:
        """One swarm update: velocities, positions, personal bests."""
        self._population = population
        informants = self._informant_indices(len(population))
        weight = self._inertia()

        updated: list[Individual] = []
        for index, particle in enumerate(population):
            informant = population[informants[index]]
            informant_best = getattr(informant, "best_genome", informant.genome)
            personal_best = getattr(particle, "best_genome", particle.genome)
            velocity = getattr(particle, "velocity", None)
            if velocity is None:
                velocity = np.zeros_like(particle.genome)

            dimension = particle.genome.size
            r1 = self.rng.random(dimension)
            r2 = self.rng.random(dimension)

            new_velocity = self.chi * (
                weight * velocity
                + self.cognitive * r1 * (personal_best - particle.genome)
                + self.social * r2 * (informant_best - particle.genome)
            )
            new_velocity = np.clip(new_velocity, -self.v_max, self.v_max)
            new_position = particle.genome + new_velocity

            if self.bound_handling == "clip_zero_velocity":
                outside = (new_position < self.lower) | (new_position > self.upper)
                new_position = np.clip(new_position, self.lower, self.upper)
                new_velocity = np.where(outside, 0.0, new_velocity)
            else:
                new_position = np.clip(new_position, self.lower, self.upper)

            moved = Particle(
                genome=new_position,
                velocity=new_velocity,
                parent_ids=[particle.individual_id, informant.individual_id],
                operator=Operator.PSO_MOVE.value,
                origin_island=particle.origin_island,
                origin_individual_id=particle.origin_individual_id,
            )
            self.ctx.evaluate(moved)

            previous_best = getattr(particle, "best_fitness", float("nan"))
            if np.isnan(previous_best) or self.problem.is_better(moved.fitness, previous_best):
                moved.best_genome = moved.genome.copy()
                moved.best_fitness = moved.fitness
            else:
                moved.best_genome = personal_best.copy()
                moved.best_fitness = previous_best

            updated.append(moved)

        self._generation += 1
        return updated


    def diversity(self, population: Sequence[Individual]) -> tuple[float | None, str | None, int | None]:
        """Mean pairwise Euclidean distance across particle positions."""
        return mean_pairwise_euclidean(
            [ind.genome for ind in population], self.rng, normaliser=self._diagonal
        )

    def serialize_migrant(self, individual: Individual) -> dict[str, Any]:
        """Transfer position, velocity and personal best."""
        velocity = getattr(individual, "velocity", None)
        best_genome = getattr(individual, "best_genome", None)
        return {
            "genome": [float(value) for value in individual.genome],
            "velocity": None if velocity is None else [float(value) for value in velocity],
            "best_genome": None if best_genome is None else [float(value) for value in best_genome],
            "best_fitness": float(getattr(individual, "best_fitness", individual.fitness)),
            "fitness": float(individual.fitness),
            "individual_id": individual.individual_id,
        }

    def deserialize_migrant(self, payload: dict[str, Any]) -> Individual:
        """Rebuild a particle, and its velocity, from a payload."""
        position = np.asarray(payload["genome"], dtype=float)
        velocity = payload.get("velocity")
        best_genome = payload.get("best_genome")
        particle = Particle(
            genome=position,
            velocity=(
                self.rng.uniform(-self.v_max, self.v_max)
                if velocity is None
                else np.asarray(velocity, dtype=float)
            ),
            operator=Operator.MIGRATION_ARRIVAL.value,
            parent_ids=[payload["individual_id"]],
            origin_individual_id=payload["individual_id"],
        )
        particle.best_genome = (
            position.copy() if best_genome is None else np.asarray(best_genome, dtype=float)
        )
        particle.best_fitness = float(payload.get("best_fitness", payload.get("fitness", float("nan"))))
        return particle
