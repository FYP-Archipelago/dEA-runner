"""Genetic Algorithm with config-selectable binary or permutation encoding."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from archipelago_logging import GenomeEncoding, Operator
from benchmarks.base import Domain

from .base import Algorithm, Individual, IslandContext
from .diversity import mean_pairwise_hamming, mean_pairwise_position_mismatch

__all__ = [
    "GeneticAlgorithm",
    "tournament_selection",
    "linear_rank_selection",
    "one_point_crossover",
    "two_point_crossover",
    "uniform_crossover",
    "bit_flip_mutation",
    "order_crossover",
    "partially_mapped_crossover",
    "cycle_crossover",
    "swap_mutation",
    "inversion_mutation",
    "scramble_mutation",
    "insertion_mutation",
]


def tournament_selection(
    fitnesses: np.ndarray,
    tournament_size: int,
    rng: np.random.Generator,
    maximising: bool,
    count: int,
) -> np.ndarray:
    """Tournament selection with replacement: sample ``k``, take the best."""
    population_size = fitnesses.size
    contenders = rng.integers(0, population_size, size=(count, tournament_size))
    contender_fitnesses = fitnesses[contenders]
    picker = np.argmax if maximising else np.argmin
    winners = picker(contender_fitnesses, axis=1)
    return contenders[np.arange(count), winners]


def linear_rank_selection(
    fitnesses: np.ndarray,
    pressure: float,
    rng: np.random.Generator,
    maximising: bool,
    count: int,
) -> np.ndarray:
    """Linear ranking selection."""
    population_size = fitnesses.size
    if population_size < 2:
        return np.zeros(count, dtype=int)
    order = np.argsort(fitnesses)
    if maximising:
        ranks = np.empty(population_size, dtype=float)
        ranks[order] = np.arange(population_size, dtype=float)
    else:
        ranks = np.empty(population_size, dtype=float)
        ranks[order] = np.arange(population_size - 1, -1, -1, dtype=float)

    probabilities = (2.0 - pressure) / population_size + (
        2.0 * ranks * (pressure - 1.0) / (population_size * (population_size - 1))
    )
    probabilities = np.clip(probabilities, 0.0, None)
    probabilities /= probabilities.sum()
    return rng.choice(population_size, size=count, replace=True, p=probabilities)


def one_point_crossover(
    parent_a: np.ndarray, parent_b: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """Single-point crossover."""
    length = parent_a.size
    if length < 2:
        return parent_a.copy(), parent_b.copy()
    cut = int(rng.integers(1, length))
    return (
        np.concatenate([parent_a[:cut], parent_b[cut:]]),
        np.concatenate([parent_b[:cut], parent_a[cut:]]),
    )


def two_point_crossover(
    parent_a: np.ndarray, parent_b: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """Two-point crossover."""
    length = parent_a.size
    if length < 3:
        return one_point_crossover(parent_a, parent_b, rng)
    cut_a, cut_b = sorted(rng.choice(np.arange(1, length), size=2, replace=False))
    child_a, child_b = parent_a.copy(), parent_b.copy()
    child_a[cut_a:cut_b] = parent_b[cut_a:cut_b]
    child_b[cut_a:cut_b] = parent_a[cut_a:cut_b]
    return child_a, child_b


def uniform_crossover(
    parent_a: np.ndarray, parent_b: np.ndarray, rng: np.random.Generator, swap_probability: float = 0.5
) -> tuple[np.ndarray, np.ndarray]:
    """Uniform crossover: each locus is independently swapped with probability p."""
    mask = rng.random(parent_a.size) < swap_probability
    child_a, child_b = parent_a.copy(), parent_b.copy()
    child_a[mask] = parent_b[mask]
    child_b[mask] = parent_a[mask]
    return child_a, child_b


def bit_flip_mutation(genome: np.ndarray, rate: float, rng: np.random.Generator) -> np.ndarray:
    """Independent per-bit flip."""
    mutant = genome.copy()
    flips = rng.random(genome.size) < rate
    mutant[flips] ^= 1
    return mutant


def order_crossover(
    parent_a: np.ndarray, parent_b: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:

    """Order crossover (OX)."""
    def build(primary: np.ndarray, secondary: np.ndarray, start: int, end: int) -> np.ndarray:
        """Fill one child: the donor slice, then the other parent's order."""
        length = primary.size
        child = np.full(length, -1, dtype=int)
        child[start:end] = primary[start:end]
        taken = set(primary[start:end].tolist())
        write = end % length
        for offset in range(length):
            allele = int(secondary[(end + offset) % length])
            if allele in taken:
                continue
            child[write] = allele
            write = (write + 1) % length
        return child

    length = parent_a.size
    if length < 3:
        return parent_a.copy(), parent_b.copy()
    start, end = sorted(rng.choice(np.arange(1, length), size=2, replace=False))
    return build(parent_a, parent_b, start, end), build(parent_b, parent_a, start, end)


def partially_mapped_crossover(
    parent_a: np.ndarray, parent_b: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:

    """Partially mapped crossover (PMX)."""
    def build(primary: np.ndarray, secondary: np.ndarray, start: int, end: int) -> np.ndarray:
        """Fill one child from the mapping induced by the swapped slice."""
        length = primary.size
        child = primary.copy()
        child[start:end] = secondary[start:end]
        mapping = {int(secondary[i]): int(primary[i]) for i in range(start, end)}
        for position in list(range(0, start)) + list(range(end, length)):
            allele = int(child[position])
            seen = 0
            while allele in mapping and seen <= length:
                allele = mapping[allele]
                seen += 1
            child[position] = allele
        return child

    length = parent_a.size
    if length < 3:
        return parent_a.copy(), parent_b.copy()
    start, end = sorted(rng.choice(np.arange(1, length), size=2, replace=False))
    return build(parent_a, parent_b, start, end), build(parent_b, parent_a, start, end)


def cycle_crossover(
    parent_a: np.ndarray, parent_b: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """Cycle crossover (CX)."""
    length = parent_a.size
    position_of = {int(value): index for index, value in enumerate(parent_a)}
    child_a, child_b = parent_b.copy(), parent_a.copy()

    visited = np.zeros(length, dtype=bool)
    cycle_index = 0
    for start in range(length):
        if visited[start]:
            continue
        index = start
        cycle: list[int] = []
        while not visited[index]:
            visited[index] = True
            cycle.append(index)
            index = position_of[int(parent_b[index])]
        if cycle_index % 2 == 0:
            for position in cycle:
                child_a[position] = parent_a[position]
                child_b[position] = parent_b[position]
        cycle_index += 1
    return child_a, child_b


def swap_mutation(genome: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Exchange two randomly chosen positions. The minimal permutation move."""
    mutant = genome.copy()
    if mutant.size < 2:
        return mutant
    i, j = rng.choice(mutant.size, size=2, replace=False)
    mutant[i], mutant[j] = mutant[j], mutant[i]
    return mutant


def inversion_mutation(genome: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Reverse a random subsequence."""
    mutant = genome.copy()
    if mutant.size < 2:
        return mutant
    start, end = sorted(rng.choice(mutant.size, size=2, replace=False))
    mutant[start : end + 1] = mutant[start : end + 1][::-1]
    return mutant


def scramble_mutation(genome: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Randomly shuffle a subsequence. More disruptive than inversion."""
    mutant = genome.copy()
    if mutant.size < 2:
        return mutant
    start, end = sorted(rng.choice(mutant.size, size=2, replace=False))
    segment = mutant[start : end + 1]
    rng.shuffle(segment)
    mutant[start : end + 1] = segment
    return mutant


def insertion_mutation(genome: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Remove one element and reinsert it elsewhere, shifting the rest."""
    mutant = genome.copy()
    if mutant.size < 2:
        return mutant
    source, target = rng.choice(mutant.size, size=2, replace=False)
    value = mutant[source]
    mutant = np.delete(mutant, source)
    return np.insert(mutant, target, value)


_BINARY_CROSSOVERS = {
    "one_point": one_point_crossover,
    "two_point": two_point_crossover,
    "uniform": uniform_crossover,
}
_PERMUTATION_CROSSOVERS = {
    "order": order_crossover,
    "ox": order_crossover,
    "pmx": partially_mapped_crossover,
    "cycle": cycle_crossover,
    "cx": cycle_crossover,
}
_PERMUTATION_MUTATIONS = {
    "swap": swap_mutation,
    "inversion": inversion_mutation,
    "scramble": scramble_mutation,
    "insertion": insertion_mutation,
}


class GeneticAlgorithm(Algorithm):
    """Generational GA with elitism, over bitstrings or permutations."""

    name = "ga"
    supported_domains = frozenset({Domain.BINARY, Domain.PERMUTATION})
    genome_encoding = GenomeEncoding.BITSTRING

    def __init__(
        self,
        ctx: IslandContext,
        representation: str = "auto",
        selection: str = "tournament",
        tournament_size: int = 3,
        rank_pressure: float = 1.7,
        crossover: str | None = None,
        crossover_rate: float = 0.9,
        mutation: str = "inversion",
        mutation_rate: float | None = None,
        elitism: int = 1,
        log_elites: bool = False,
    ) -> None:
        problem = ctx.problem
        if representation == "auto":
            representation = "binary" if problem.domain is Domain.BINARY else "permutation"
        if representation not in ("binary", "permutation"):
            raise ValueError(f"unknown representation {representation!r}")
        if representation == "binary" and problem.domain is not Domain.BINARY:
            raise ValueError("binary representation requires a binary-domain problem")
        if representation == "permutation" and problem.domain is not Domain.PERMUTATION:
            raise ValueError("permutation representation requires a permutation-domain problem")

        self.representation = representation
        self.genome_encoding = (
            GenomeEncoding.BITSTRING if representation == "binary" else GenomeEncoding.PERMUTATION
        )

        table = _BINARY_CROSSOVERS if representation == "binary" else _PERMUTATION_CROSSOVERS
        crossover_name = crossover or ("uniform" if representation == "binary" else "order")
        if crossover_name not in table:
            raise ValueError(
                f"unknown {representation} crossover {crossover_name!r}; available: {sorted(table)}"
            )
        self._crossover = table[crossover_name]

        if representation == "permutation":
            if mutation not in _PERMUTATION_MUTATIONS:
                raise ValueError(
                    f"unknown permutation mutation {mutation!r}; "
                    f"available: {sorted(_PERMUTATION_MUTATIONS)}"
                )
            self._mutation = _PERMUTATION_MUTATIONS[mutation]
        self._mutation_name = mutation if representation == "permutation" else "bit_flip"

        if selection not in ("tournament", "rank"):
            raise ValueError(f"unknown selection {selection!r}; expected 'tournament' or 'rank'")
        self.selection = selection
        self.tournament_size = max(2, int(tournament_size))
        self.rank_pressure = float(rank_pressure)
        self.crossover_rate = float(crossover_rate)
        self.elitism = max(0, int(elitism))
        self.log_elites = bool(log_elites)

        length = getattr(problem, "dimension", 1)
        if mutation_rate is None:
            mutation_rate = 1.0 / max(1, length) if representation == "binary" else 0.2
        self.mutation_rate = float(mutation_rate)

        super().__init__(ctx)
        self.params = {
            "representation": representation,
            "selection": selection,
            "tournament_size": self.tournament_size,
            "rank_pressure": self.rank_pressure,
            "crossover": crossover_name,
            "crossover_rate": self.crossover_rate,
            "mutation": self._mutation_name,
            "mutation_rate": self.mutation_rate,
            "elitism": self.elitism,
            "log_elites": self.log_elites,
        }


    def initialize(self, population_size: int) -> list[Individual]:
        """Uniform random initialisation over the whole search space."""
        return self.ctx.evaluate_all(
            Individual(genome=self.problem.random_solution(self.rng), operator=Operator.INIT.value)
            for _ in range(population_size)
        )

    def _select_parents(self, population: Sequence[Individual], count: int) -> np.ndarray:
        fitnesses = np.fromiter((ind.fitness for ind in population), dtype=float, count=len(population))
        if self.selection == "tournament":
            return tournament_selection(
                fitnesses, self.tournament_size, self.rng, self.problem.maximising, count
            )
        return linear_rank_selection(
            fitnesses, self.rank_pressure, self.rng, self.problem.maximising, count
        )

    def _vary(self, parent_a: Individual, parent_b: Individual) -> list[Individual]:
        """Produce two offspring, labelling each with the operators that made it."""
        do_crossover = self.rng.random() < self.crossover_rate
        if do_crossover:
            genome_a, genome_b = self._crossover(parent_a.genome, parent_b.genome, self.rng)
            parent_ids = [parent_a.individual_id, parent_b.individual_id]
        else:
            genome_a, genome_b = parent_a.genome.copy(), parent_b.genome.copy()
            parent_ids = None

        offspring: list[Individual] = []
        for index, genome in enumerate((genome_a, genome_b)):
            sole_parent = parent_a if index == 0 else parent_b
            if self.representation == "binary":
                mutated_genome = bit_flip_mutation(genome, self.mutation_rate, self.rng)
                did_mutate = not np.array_equal(mutated_genome, genome)
                genome = mutated_genome
            else:
                did_mutate = self.rng.random() < self.mutation_rate
                if did_mutate:
                    genome = self._mutation(genome, self.rng)

            if do_crossover and did_mutate:
                operator = Operator.CROSSOVER_MUTATION.value
            elif do_crossover:
                operator = Operator.CROSSOVER.value
            elif did_mutate:
                operator = Operator.MUTATION.value
            else:
                operator = Operator.CLONE.value

            offspring.append(
                Individual(
                    genome=genome,
                    parent_ids=list(parent_ids) if parent_ids else [sole_parent.individual_id],
                    operator=operator,
                )
            )
        return offspring

    def step(self, population: list[Individual]) -> list[Individual]:
        """One generational cycle: elitism, selection, variation, replacement."""
        population_size = len(population)
        fitnesses = np.fromiter((ind.fitness for ind in population), dtype=float, count=population_size)
        order = np.argsort(-fitnesses if self.problem.maximising else fitnesses)

        elites = [population[index] for index in order[: self.elitism]]
        if self.log_elites:
            elites = self.ctx.evaluate_all(
                Individual(
                    genome=elite.genome.copy(),
                    parent_ids=[elite.individual_id],
                    operator=Operator.ELITE_COPY.value,
                )
                for elite in elites
            )

        needed = population_size - len(elites)
        parent_indices = self._select_parents(population, count=needed + (needed % 2))

        offspring: list[Individual] = []
        for pair in range(0, len(parent_indices), 2):
            children = self._vary(
                population[parent_indices[pair]], population[parent_indices[pair + 1]]
            )
            offspring.extend(children)

        offspring = self.ctx.evaluate_all(offspring[:needed])
        return elites + offspring


    def diversity(self, population: Sequence[Individual]) -> tuple[float | None, str | None, int | None]:
        """Hamming distance for bitstrings, position mismatch for permutations."""
        genomes = [ind.genome for ind in population]
        if self.representation == "binary":
            return mean_pairwise_hamming(genomes, self.rng)
        return mean_pairwise_position_mismatch(genomes, self.rng)

    def serialize_migrant(self, individual: Individual) -> dict[str, Any]:
        """Pack an individual into a JSON-safe payload for transfer."""
        return {
            "genome": [int(value) for value in individual.genome],
            "fitness": float(individual.fitness),
            "individual_id": individual.individual_id,
        }

    def deserialize_migrant(self, payload: dict[str, Any]) -> Individual:
        """Rebuild an individual from a transferred payload."""
        dtype = np.int8 if self.representation == "binary" else int
        return Individual(
            genome=np.asarray(payload["genome"], dtype=dtype),
            operator=Operator.MIGRATION_ARRIVAL.value,
            parent_ids=[payload["individual_id"]],
            origin_individual_id=payload["individual_id"],
        )
