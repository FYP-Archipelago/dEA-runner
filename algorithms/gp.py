"""Genetic Programming for symbolic regression."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from archipelago_logging import GenomeEncoding, Operator
from benchmarks.base import Domain

from .base import Algorithm, Individual, IslandContext
from .diversity import unique_signature_ratio
from .ga import tournament_selection
from .gp_tree import (
    FUNCTION_SETS,
    PRIMITIVES,
    Node,
    canonical,
    depth,
    iter_nodes,
    parse,
    replace_at,
    size,
)

__all__ = ["GeneticProgramming", "TreeIndividual"]


class TreeIndividual(Individual):
    """An individual whose genome is a :class:`~algorithms.gp_tree.Node` tree."""


class _CompiledTree:
    """Adapts a tree to the predictor interface the regression benchmark expects."""

    __slots__ = ("root", "variables")

    def __init__(self, root: Node, variables: Sequence[str]) -> None:
        self.root = root
        self.variables = list(variables)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return self.root(x, self.variables)


class GeneticProgramming(Algorithm):
    """Koza-style tree GP with tournament selection and elitism."""

    name = "gp"
    supported_domains = frozenset({Domain.SYMBOLIC_REGRESSION})
    genome_encoding = GenomeEncoding.EXPRESSION_TREE

    def __init__(
        self,
        ctx: IslandContext,
        function_set: str | Sequence[str] = "koza",
        min_init_depth: int = 2,
        max_init_depth: int = 6,
        max_depth: int = 17,
        tournament_size: int = 7,
        crossover_rate: float = 0.9,
        subtree_mutation_rate: float = 0.05,
        point_mutation_rate: float = 0.03,
        hoist_mutation_rate: float = 0.02,
        internal_node_bias: float = 0.9,
        constant_range: tuple[float, float] = (-1.0, 1.0),
        terminal_constant_probability: float = 0.3,
        parsimony_coefficient: float = 0.0,
        elitism: int = 1,
    ) -> None:
        if isinstance(function_set, str):
            if function_set not in FUNCTION_SETS:
                raise ValueError(
                    f"unknown function set {function_set!r}; available: {sorted(FUNCTION_SETS)}"
                )
            names = list(FUNCTION_SETS[function_set])
            function_set_name = function_set
        else:
            names = list(function_set)
            function_set_name = "custom"
        unknown = [name for name in names if name not in PRIMITIVES]
        if unknown:
            raise ValueError(f"unknown primitives {unknown}; available: {sorted(PRIMITIVES)}")

        total_rate = crossover_rate + subtree_mutation_rate + point_mutation_rate + hoist_mutation_rate
        if total_rate > 1.0 + 1e-9:
            raise ValueError(
                f"GP operator probabilities sum to {total_rate:.3f}, which exceeds 1.0; "
                "they are applied as a mutually exclusive choice per offspring"
            )

        self.function_names = names
        self.function_set_name = function_set_name
        self.min_init_depth = int(min_init_depth)
        self.max_init_depth = int(max_init_depth)
        self.max_depth = int(max_depth)
        self.tournament_size = max(2, int(tournament_size))
        self.crossover_rate = float(crossover_rate)
        self.subtree_mutation_rate = float(subtree_mutation_rate)
        self.point_mutation_rate = float(point_mutation_rate)
        self.hoist_mutation_rate = float(hoist_mutation_rate)
        self.internal_node_bias = float(internal_node_bias)
        self.constant_range = (float(constant_range[0]), float(constant_range[1]))
        self.terminal_constant_probability = float(terminal_constant_probability)
        self.parsimony_coefficient = float(parsimony_coefficient)
        self.elitism = max(0, int(elitism))

        super().__init__(ctx)
        self.variables = list(ctx.problem.variable_names)
        self.params = {
            "function_set": function_set_name,
            "primitives": names,
            "min_init_depth": self.min_init_depth,
            "max_init_depth": self.max_init_depth,
            "max_depth": self.max_depth,
            "tournament_size": self.tournament_size,
            "crossover_rate": self.crossover_rate,
            "subtree_mutation_rate": self.subtree_mutation_rate,
            "point_mutation_rate": self.point_mutation_rate,
            "hoist_mutation_rate": self.hoist_mutation_rate,
            "internal_node_bias": self.internal_node_bias,
            "constant_range": list(self.constant_range),
            "terminal_constant_probability": self.terminal_constant_probability,
            "parsimony_coefficient": self.parsimony_coefficient,
            "elitism": self.elitism,
        }


    def genome_for_log(self, individual: Individual) -> Any:
        """The canonical prefix string: this tree's location signature."""
        return canonical(individual.genome.root)


    def _random_terminal(self) -> Node:
        if self.rng.random() < self.terminal_constant_probability or not self.variables:
            return Node("const", (), float(self.rng.uniform(*self.constant_range)))
        return Node(self.variables[int(self.rng.integers(0, len(self.variables)))])

    def _random_function(self) -> str:
        return self.function_names[int(self.rng.integers(0, len(self.function_names)))]

    def _grow(self, max_depth: int, full: bool) -> Node:
        """Koza's ``full`` and ``grow`` tree builders."""
        if max_depth <= 0:
            return self._random_terminal()
        if not full and self.rng.random() < len(self.variables) / (
            len(self.variables) + len(self.function_names) + 1
        ):
            return self._random_terminal()
        symbol = self._random_function()
        arity = PRIMITIVES[symbol].arity
        return Node(symbol, tuple(self._grow(max_depth - 1, full) for _ in range(arity)))

    def _ramped_half_and_half(self, index: int, population_size: int) -> Node:
        """Ramped half-and-half initialisation."""
        depths = list(range(self.min_init_depth, self.max_init_depth + 1))
        chosen_depth = depths[index % len(depths)]
        use_full = (index // len(depths)) % 2 == 0
        return self._grow(chosen_depth, full=use_full)

    def _make(self, root: Node) -> _CompiledTree:
        return _CompiledTree(root, self.variables)


    def _select_node_index(self, root: Node) -> int:
        """Pick a crossover/mutation point, biased toward internal nodes."""
        nodes = list(iter_nodes(root))
        internal = [index for index, node in enumerate(nodes) if not node.is_terminal]
        terminal = [index for index, node in enumerate(nodes) if node.is_terminal]
        if internal and (self.rng.random() < self.internal_node_bias or not terminal):
            return internal[int(self.rng.integers(0, len(internal)))]
        return terminal[int(self.rng.integers(0, len(terminal)))]


    def _subtree_crossover(self, parent_a: Node, parent_b: Node) -> Node:
        """Swap a randomly chosen subtree of ``parent_b`` into ``parent_a``."""
        point_a = self._select_node_index(parent_a)
        point_b = self._select_node_index(parent_b)
        donor = list(iter_nodes(parent_b))[point_b]
        return replace_at(parent_a, point_a, donor)

    def _subtree_mutation(self, parent: Node) -> Node:
        """Replace a random subtree with a freshly grown one."""
        point = self._select_node_index(parent)
        replacement = self._grow(self.rng.integers(1, self.max_init_depth + 1), full=False)
        return replace_at(parent, point, replacement)

    def _point_mutation(self, parent: Node) -> Node:
        """Replace one node's symbol with another of the same arity."""
        nodes = list(iter_nodes(parent))
        index = int(self.rng.integers(0, len(nodes)))
        node = nodes[index]
        if node.is_terminal:
            return replace_at(parent, index, self._random_terminal())
        arity = PRIMITIVES[node.symbol].arity
        candidates = [name for name in self.function_names if PRIMITIVES[name].arity == arity]
        if not candidates:
            return parent
        symbol = candidates[int(self.rng.integers(0, len(candidates)))]
        return replace_at(parent, index, Node(symbol, node.children))

    def _hoist_mutation(self, parent: Node) -> Node:
        """Replace the tree with one of its own subtrees."""
        nodes = list(iter_nodes(parent))
        if len(nodes) < 2:
            return parent
        return nodes[int(self.rng.integers(1, len(nodes)))]


    def initialize(self, population_size: int) -> list[Individual]:
        """Build and evaluate the initial population of trees."""
        return self.ctx.evaluate_all(
            TreeIndividual(
                genome=self._make(self._ramped_half_and_half(index, population_size)),
                operator=Operator.INIT.value,
            )
            for index in range(population_size)
        )

    def _selection_fitnesses(self, population: Sequence[Individual]) -> np.ndarray:
        """Fitness used for *selection*, with parsimony pressure folded in."""
        raw = np.fromiter((ind.fitness for ind in population), dtype=float, count=len(population))
        if self.parsimony_coefficient == 0.0:
            return raw
        sizes = np.fromiter(
            (size(ind.genome.root) for ind in population), dtype=float, count=len(population)
        )
        penalty = self.parsimony_coefficient * sizes
        return raw - penalty if self.problem.maximising else raw + penalty

    def step(self, population: list[Individual]) -> list[Individual]:
        """Advance one generation of subtree crossover and mutation."""
        population_size = len(population)
        selection_fitnesses = self._selection_fitnesses(population)
        order = np.argsort(
            -selection_fitnesses if self.problem.maximising else selection_fitnesses
        )
        elites = [population[index] for index in order[: self.elitism]]

        needed = population_size - len(elites)
        parents = tournament_selection(
            selection_fitnesses, self.tournament_size, self.rng, self.problem.maximising, needed * 2
        )

        offspring: list[Individual] = []
        for index in range(needed):
            first = population[parents[2 * index]]
            second = population[parents[2 * index + 1]]
            draw = self.rng.random()

            if draw < self.crossover_rate:
                root = self._subtree_crossover(first.genome.root, second.genome.root)
                operator = Operator.CROSSOVER.value
                parent_ids = [first.individual_id, second.individual_id]
            elif draw < self.crossover_rate + self.subtree_mutation_rate:
                root = self._subtree_mutation(first.genome.root)
                operator = Operator.MUTATION.value
                parent_ids = [first.individual_id]
            elif draw < self.crossover_rate + self.subtree_mutation_rate + self.point_mutation_rate:
                root = self._point_mutation(first.genome.root)
                operator = Operator.MUTATION.value
                parent_ids = [first.individual_id]
            elif (
                draw
                < self.crossover_rate
                + self.subtree_mutation_rate
                + self.point_mutation_rate
                + self.hoist_mutation_rate
            ):
                root = self._hoist_mutation(first.genome.root)
                operator = Operator.MUTATION.value
                parent_ids = [first.individual_id]
            else:
                root = first.genome.root
                operator = Operator.CLONE.value
                parent_ids = [first.individual_id]

            if depth(root) > self.max_depth:
                root = first.genome.root
                operator = Operator.CLONE.value
                parent_ids = [first.individual_id]

            offspring.append(
                TreeIndividual(genome=self._make(root), parent_ids=parent_ids, operator=operator)
            )

        return elites + self.ctx.evaluate_all(offspring)


    def diversity(self, population: Sequence[Individual]) -> tuple[float | None, str | None, int | None]:
        """Spread of fitness values; trees have no cheap metric distance."""
        return unique_signature_ratio([canonical(ind.genome.root) for ind in population])

    def population_statistics(self, population: Sequence[Individual]) -> dict[str, float]:
        """Mean tree size and depth, reported in ``island_end``."""
        sizes = [size(ind.genome.root) for ind in population]
        depths = [depth(ind.genome.root) for ind in population]
        return {
            "mean_tree_size": float(np.mean(sizes)) if sizes else 0.0,
            "max_tree_size": float(np.max(sizes)) if sizes else 0.0,
            "mean_tree_depth": float(np.mean(depths)) if depths else 0.0,
            "max_tree_depth": float(np.max(depths)) if depths else 0.0,
        }

    def serialize_migrant(self, individual: Individual) -> dict[str, Any]:
        """Transfer the canonical prefix string -- compact, and human-readable in a capture."""
        return {
            "genome": canonical(individual.genome.root),
            "fitness": float(individual.fitness),
            "individual_id": individual.individual_id,
        }

    def deserialize_migrant(self, payload: dict[str, Any]) -> Individual:
        """Rebuild a tree individual from its prefix serialisation."""
        return TreeIndividual(
            genome=self._make(parse(payload["genome"])),
            operator=Operator.MIGRATION_ARRIVAL.value,
            parent_ids=[payload["individual_id"]],
            origin_individual_id=payload["individual_id"],
        )
