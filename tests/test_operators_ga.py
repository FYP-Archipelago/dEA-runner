"""GA operator correctness."""

from __future__ import annotations

import numpy as np
import pytest

from conftest import KNAPSACK_INSTANCE, TSP_INSTANCE
from algorithms.ga import (
    GeneticAlgorithm,
    bit_flip_mutation,
    cycle_crossover,
    insertion_mutation,
    inversion_mutation,
    linear_rank_selection,
    one_point_crossover,
    order_crossover,
    partially_mapped_crossover,
    scramble_mutation,
    swap_mutation,
    tournament_selection,
    two_point_crossover,
)

PERMUTATION_CROSSOVERS = [order_crossover, partially_mapped_crossover, cycle_crossover]
PERMUTATION_MUTATIONS = [swap_mutation, inversion_mutation, scramble_mutation, insertion_mutation]


@pytest.mark.parametrize("crossover", PERMUTATION_CROSSOVERS)
def test_permutation_crossover_always_returns_permutations(crossover, rng):
    for _ in range(200):
        length = int(rng.integers(3, 30))
        a, b = rng.permutation(length), rng.permutation(length)
        child_a, child_b = crossover(a, b, rng)
        assert sorted(child_a.tolist()) == list(range(length))
        assert sorted(child_b.tolist()) == list(range(length))


@pytest.mark.parametrize("mutation", PERMUTATION_MUTATIONS)
def test_permutation_mutation_always_returns_permutations(mutation, rng):
    for _ in range(200):
        length = int(rng.integers(2, 30))
        mutant = mutation(rng.permutation(length), rng)
        assert sorted(mutant.tolist()) == list(range(length))


@pytest.mark.parametrize("crossover", PERMUTATION_CROSSOVERS)
def test_permutation_crossover_does_not_mutate_its_parents(crossover, rng):
    a, b = rng.permutation(12), rng.permutation(12)
    before_a, before_b = a.copy(), b.copy()
    crossover(a, b, rng)
    assert np.array_equal(a, before_a) and np.array_equal(b, before_b)


class _FixedCuts:
    """RNG stub returning predetermined cut points, so OX can be checked exactly."""

    def __init__(self, cuts):
        self._cuts = np.array(cuts)

    def choice(self, *_args, **_kwargs):
        return self._cuts


def test_order_crossover_copies_the_segment_and_keeps_the_donor_relative_order():
    """OX's defining property, stated exactly (Davis 1985)."""
    a = np.arange(10)
    b = np.array([9, 8, 7, 6, 5, 4, 3, 2, 1, 0])
    start, end = 3, 7
    child, _ = order_crossover(a, b, _FixedCuts([start, end]))

    assert np.array_equal(child[start:end], a[start:end])

    copied = set(a[start:end].tolist())
    expected_fill = [value for value in np.roll(b, -end).tolist() if value not in copied]
    actual_fill = [int(child[(end + offset) % 10]) for offset in range(10 - (end - start))]
    assert actual_fill == expected_fill


def test_cycle_crossover_gives_every_allele_a_parental_position(rng):
    """CX's defining property: no allele lands anywhere neither parent had it."""
    for _ in range(50):
        a, b = rng.permutation(10), rng.permutation(10)
        child_a, child_b = cycle_crossover(a, b, rng)
        for position in range(10):
            assert child_a[position] in (a[position], b[position])
            assert child_b[position] in (a[position], b[position])


def test_inversion_mutation_reverses_a_contiguous_block(rng):
    original = np.arange(20)
    mutant = inversion_mutation(original, np.random.default_rng(3))
    differing = np.flatnonzero(original != mutant)
    if differing.size:
        start, end = differing[0], differing[-1]
        assert np.array_equal(mutant[start : end + 1], original[start : end + 1][::-1])


def test_swap_mutation_changes_exactly_two_positions(rng):
    for _ in range(100):
        original = rng.permutation(15)
        mutant = swap_mutation(original, rng)
        assert int(np.sum(original != mutant)) in (0, 2)


@pytest.mark.parametrize("crossover", [one_point_crossover, two_point_crossover])
def test_binary_crossover_conserves_total_allele_content(crossover, rng):
    """Every bit exchanged from one child appears in the other."""
    for _ in range(100):
        length = int(rng.integers(3, 40))
        a = rng.integers(0, 2, length, dtype=np.int8)
        b = rng.integers(0, 2, length, dtype=np.int8)
        child_a, child_b = crossover(a, b, rng)
        assert child_a.sum() + child_b.sum() == a.sum() + b.sum()


def test_one_point_crossover_always_exchanges_something(rng):
    """A cut at 0 or L would silently reduce the effective crossover rate."""
    a = np.zeros(20, dtype=np.int8)
    b = np.ones(20, dtype=np.int8)
    for _ in range(200):
        child_a, _ = one_point_crossover(a, b, rng)
        assert 0 < child_a.sum() < 20


def test_uniform_crossover_swaps_about_half_the_loci(rng):
    from algorithms.ga import uniform_crossover

    a = np.zeros(2000, dtype=np.int8)
    b = np.ones(2000, dtype=np.int8)
    child_a, _ = uniform_crossover(a, b, rng, swap_probability=0.5)
    assert 900 < child_a.sum() < 1100


def test_bit_flip_mutation_rate_is_per_bit(rng):
    genome = np.zeros(10_000, dtype=np.int8)
    mutant = bit_flip_mutation(genome, 0.01, rng)
    assert 60 < mutant.sum() < 140


def test_bit_flip_at_rate_one_over_l_flips_about_one_bit(rng):
    """The standard recommendation: one expected flip per genome."""
    length = 500
    flips = [
        int(bit_flip_mutation(np.zeros(length, dtype=np.int8), 1.0 / length, rng).sum())
        for _ in range(400)
    ]
    assert 0.8 < float(np.mean(flips)) < 1.2


def test_tournament_selection_pressure_rises_with_k(rng):
    fitnesses = np.arange(50, dtype=float)
    picks_small = tournament_selection(fitnesses, 2, rng, maximising=True, count=4000)
    picks_large = tournament_selection(fitnesses, 10, rng, maximising=True, count=4000)
    assert float(np.mean(fitnesses[picks_large])) > float(np.mean(fitnesses[picks_small]))


def test_tournament_selection_honours_the_minimisation_direction(rng):
    fitnesses = np.arange(50, dtype=float)
    picks = tournament_selection(fitnesses, 8, rng, maximising=False, count=4000)
    assert float(np.mean(fitnesses[picks])) < 25.0


def test_linear_rank_selection_gives_the_worst_zero_probability_at_s_equals_2(rng):
    fitnesses = np.arange(20, dtype=float)
    picks = linear_rank_selection(fitnesses, 2.0, rng, maximising=True, count=5000)
    assert 0 not in set(picks.tolist())


def test_linear_rank_selection_at_s_equals_1_is_uniform(rng):
    fitnesses = np.arange(10, dtype=float)
    picks = linear_rank_selection(fitnesses, 1.0, rng, maximising=True, count=20000)
    counts = np.bincount(picks, minlength=10) / 20000
    assert np.all(np.abs(counts - 0.1) < 0.02)


def test_ga_auto_selects_representation_from_the_problem_domain(make_context):
    binary = GeneticAlgorithm(make_context("knapsack", KNAPSACK_INSTANCE))
    assert binary.representation == "binary"
    permutation = GeneticAlgorithm(make_context("tsp", TSP_INSTANCE))
    assert permutation.representation == "permutation"


def test_ga_rejects_a_representation_the_problem_cannot_use(make_context):
    with pytest.raises(ValueError, match="permutation representation requires"):
        GeneticAlgorithm(make_context("knapsack", KNAPSACK_INSTANCE), representation="permutation")


def test_ga_preserves_population_size_and_improves_fitness(make_context):
    ctx = make_context("knapsack", KNAPSACK_INSTANCE)
    algorithm = GeneticAlgorithm(ctx, elitism=2)
    population = algorithm.initialize(50)
    initial_best = ctx.best_fitness
    for generation in range(1, 30):
        ctx.generation = generation
        population = algorithm.step(population)
        assert len(population) == 50
    assert ctx.best_fitness > initial_best


def test_ga_elites_are_carried_forward_without_being_re_logged(make_context, captured_lines):
    """Re-logging an elite would fabricate an STN self-loop every generation."""
    ctx = make_context("knapsack", KNAPSACK_INSTANCE)
    algorithm = GeneticAlgorithm(ctx, elitism=3, log_elites=False)
    population = algorithm.initialize(20)
    before = ctx.evaluations
    ctx.generation = 1
    algorithm.step(population)
    assert ctx.evaluations - before == 17


def test_ga_can_re_log_elites_when_asked(make_context):
    ctx = make_context("knapsack", KNAPSACK_INSTANCE)
    algorithm = GeneticAlgorithm(ctx, elitism=3, log_elites=True)
    population = algorithm.initialize(20)
    before = ctx.evaluations
    ctx.generation = 1
    algorithm.step(population)
    assert ctx.evaluations - before == 20


def test_ga_migrant_round_trip(make_context):
    ctx = make_context("tsp", TSP_INSTANCE)
    algorithm = GeneticAlgorithm(ctx)
    population = algorithm.initialize(10)
    payload = algorithm.serialize_migrant(population[0])
    import json

    json.dumps(payload)
    restored = algorithm.deserialize_migrant(payload)
    assert sorted(restored.genome.tolist()) == list(range(ctx.problem.dimension))
    assert restored.origin_individual_id == population[0].individual_id
