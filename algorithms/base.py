"""Algorithm abstraction and the island-side evaluation/logging context."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from archipelago_logging import (
    EncodedGenome,
    EventLogger,
    GenomeEncoding,
    GenomePolicy,
    IndividualIdMinter,
    Operator,
)
from benchmarks.base import Domain, Problem, ProblemEvaluation

__all__ = [
    "Individual",
    "Particle",
    "ESIndividual",
    "IslandContext",
    "Algorithm",
]


@dataclass
class Individual:
    """One candidate solution and its provenance."""

    genome: Any
    fitness: float = float("nan")
    objective: float | None = None
    feasible: bool | None = None
    constraint_violation: float | None = None

    individual_id: str = ""
    parent_ids: list[str] = field(default_factory=list)
    operator: str = Operator.INIT.value

    origin_island: int | None = None
    origin_individual_id: str | None = None

    def copy(self) -> "Individual":
        """Shallow copy with an independent genome array where that matters."""
        clone = type(self)(**{key: getattr(self, key) for key in self.__dataclass_fields__})
        if isinstance(self.genome, np.ndarray):
            clone.genome = self.genome.copy()
        clone.parent_ids = list(self.parent_ids)
        return clone


@dataclass
class Particle(Individual):
    """A PSO particle: position (``genome``) plus velocity and personal best."""

    velocity: np.ndarray | None = None
    best_genome: np.ndarray | None = None
    best_fitness: float = float("nan")


@dataclass
class ESIndividual(Individual):
    """An Evolution Strategy individual: object variables plus self-adaptive step sizes."""

    sigma: np.ndarray | float | None = None


class IslandContext:
    """Everything one island's algorithm is allowed to touch."""

    def __init__(
        self,
        *,
        island_id: int,
        problem: Problem,
        rng: np.random.Generator,
        logger: EventLogger,
        genome_policy: GenomePolicy,
        genome_encoding: GenomeEncoding,
        id_minter: IndividualIdMinter | None = None,
        max_evaluations: int | None = None,
    ) -> None:
        self.island_id = island_id
        self.problem = problem
        self.rng = rng
        self.logger = logger
        self.genome_policy = genome_policy
        self.genome_encoding = genome_encoding
        self.id_minter = id_minter or IndividualIdMinter(island_id)
        self.max_evaluations = max_evaluations

        self.generation = 0
        self.evaluations = 0
        self.evaluations_this_generation = 0
        self.best_fitness: float = problem.worst_possible()
        self.best_individual: Individual | None = None

        self.genome_for_log: Callable[[Individual], Any] = lambda ind: ind.genome


    def evaluate(self, individual: Individual) -> Individual:
        """Score ``individual``, record it, and update the island's best-so-far."""
        result: ProblemEvaluation = self.problem.evaluate(individual.genome)

        individual.fitness = result.fitness
        individual.objective = result.objective
        individual.feasible = result.feasible
        individual.constraint_violation = result.constraint_violation

        if not individual.individual_id:
            individual.individual_id = self.id_minter.mint(self.generation)

        self.evaluations += 1
        self.evaluations_this_generation += 1

        improved = self.problem.is_better(individual.fitness, self.best_fitness)
        if improved:
            self.best_fitness = individual.fitness
            self.best_individual = individual.copy()

        encoded: EncodedGenome = self.genome_policy.encode(
            self.genome_encoding,
            self.genome_for_log(individual),
            is_best=improved,
        )
        self.logger.evaluation(
            individual_id=individual.individual_id,
            parent_ids=individual.parent_ids,
            operator=individual.operator,
            fitness=individual.fitness,
            objective=(
                None
                if individual.objective is None or individual.objective == individual.fitness
                else individual.objective
            ),
            generation=self.generation,
            genome=encoded,
            feasible=individual.feasible,
            constraint_violation=individual.constraint_violation,
            origin_island=individual.origin_island,
            origin_individual_id=individual.origin_individual_id,
            is_island_best=True if improved else None,
        )

        return individual

    def evaluate_all(self, individuals: Iterable[Individual]) -> list[Individual]:
        """Evaluate a batch in order. Order is the trajectory order in the log."""
        return [self.evaluate(individual) for individual in individuals]

    def budget_exhausted(self) -> bool:
        """``True`` once the optional evaluation budget is spent."""
        return self.max_evaluations is not None and self.evaluations >= self.max_evaluations


class Algorithm(ABC):
    """Base class for the five island algorithms."""

    name: str = "unnamed"
    supported_domains: frozenset[Domain] = frozenset()
    genome_encoding: GenomeEncoding = GenomeEncoding.REAL_VECTOR

    def __init__(self, ctx: IslandContext, **params: Any) -> None:
        self.ctx = ctx
        self.problem = ctx.problem
        self.rng = ctx.rng
        self.params: dict[str, Any] = {}
        self.attach()

    def attach(self) -> None:
        """Hook the algorithm's genome view into the context."""
        self.ctx.genome_encoding = self.genome_encoding
        self.ctx.genome_for_log = self.genome_for_log


    @abstractmethod
    def initialize(self, population_size: int) -> list[Individual]:
        """Build and evaluate the initial population."""

    @abstractmethod
    def step(self, population: list[Individual]) -> list[Individual]:
        """Advance one generation and return the new population."""


    def genome_for_log(self, individual: Individual) -> Any:
        """The object handed to the genome policy for this individual."""
        return individual.genome

    def diversity(self, population: Sequence[Individual]) -> tuple[float | None, str | None, int | None]:
        """Return ``(value, metric_name, sample_size)`` for ``generation_end``."""
        return (None, None, None)

    @abstractmethod
    def serialize_migrant(self, individual: Individual) -> dict[str, Any]:
        """Convert an individual to a JSON-safe payload for transfer to another island."""

    @abstractmethod
    def deserialize_migrant(self, payload: dict[str, Any]) -> Individual:
        """Rebuild an individual from a transferred payload."""


    def describe(self) -> dict[str, Any]:
        """Resolved parameters, logged in ``island_start.algorithm_params``."""
        return {"algorithm": self.name, "genome_encoding": self.genome_encoding.value, **self.params}

    def best_genome_full(self, individual: Individual) -> Any:
        """Best genome in full JSON form, for ``island_end`` and ``run_end``."""
        from archipelago_logging.genome import _to_jsonable

        return _to_jsonable(self.genome_encoding, self.genome_for_log(individual))
