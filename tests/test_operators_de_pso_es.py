"""DE, PSO and ES operator correctness."""

from __future__ import annotations

import numpy as np
import pytest

from algorithms.de import BOUND_HANDLERS, MUTATION_STRATEGIES, DifferentialEvolution, _apply_bounds
from algorithms.es import SIGMA_FLOOR, EvolutionStrategy
from algorithms.pso import ParticleSwarmOptimization
from archipelago_logging import GenomeEncoding, genome_hash


@pytest.mark.parametrize("strategy", MUTATION_STRATEGIES)
def test_every_de_strategy_runs_and_stays_in_bounds(strategy, make_context):
    ctx = make_context("sphere", {"dimension": 8})
    algorithm = DifferentialEvolution(ctx, strategy=strategy)
    population = algorithm.initialize(20)
    for generation in range(1, 6):
        ctx.generation = generation
        population = algorithm.step(population)
        for individual in population:
            assert np.all(individual.genome >= algorithm.lower - 1e-9)
            assert np.all(individual.genome <= algorithm.upper + 1e-9)
    assert len(population) == 20


def test_de_rejects_a_population_too_small_for_its_strategy(make_context):
    ctx = make_context("sphere", {"dimension": 5})
    with pytest.raises(ValueError, match="at least 6"):
        DifferentialEvolution(ctx, strategy="rand/2").initialize(5)


def test_de_binomial_crossover_always_takes_at_least_one_donor_component(make_context):
    """The forced j_rand index; without it a trial can equal its target exactly."""
    ctx = make_context("sphere", {"dimension": 20})
    algorithm = DifferentialEvolution(ctx, cr=0.0)
    target = np.zeros(20)
    donor = np.ones(20)
    for _ in range(200):
        trial = algorithm._binomial_crossover(target, donor)
        assert int(np.sum(trial == 1.0)) == 1


def test_de_difference_vector_indices_are_distinct_and_exclude_the_target(make_context):
    ctx = make_context("sphere", {"dimension": 4})
    algorithm = DifferentialEvolution(ctx)
    for target in range(10):
        for _ in range(50):
            indices = algorithm._distinct_indices(10, exclude=target, count=3)
            assert len(set(indices.tolist())) == 3
            assert target not in indices.tolist()


def test_de_exponential_crossover_takes_a_contiguous_circular_block(make_context):
    ctx = make_context("sphere", {"dimension": 12})
    algorithm = DifferentialEvolution(ctx, crossover="exp", cr=0.7)
    target, donor = np.zeros(12), np.ones(12)
    for _ in range(200):
        trial = algorithm._exponential_crossover(target, donor)
        taken = trial == 1.0
        assert taken.sum() >= 1

        starts = [
            index for index in range(12) if taken[index] and not taken[(index - 1) % 12]
        ]
        assert len(starts) <= 1
        if starts:
            rotated = np.roll(taken, -starts[0])
            assert np.array_equal(rotated, np.arange(12) < taken.sum())


@pytest.mark.parametrize("method", BOUND_HANDLERS)
def test_every_bound_handler_returns_a_point_inside_the_box(method, rng):
    lower, upper = np.full(6, -5.0), np.full(6, 5.0)
    target = np.zeros(6)
    for _ in range(200):
        trial = rng.uniform(-20, 20, size=6)
        repaired = _apply_bounds(trial, target, lower, upper, method, rng)
        assert np.all(repaired >= lower - 1e-9) and np.all(repaired <= upper + 1e-9)


def test_de_selection_is_greedy_and_never_worsens_the_population(make_context):
    ctx = make_context("sphere", {"dimension": 6})
    algorithm = DifferentialEvolution(ctx)
    population = algorithm.initialize(20)
    previous = sorted(individual.fitness for individual in population)
    for generation in range(1, 10):
        ctx.generation = generation
        population = algorithm.step(population)
        current = sorted(individual.fitness for individual in population)
        assert all(new <= old + 1e-12 for new, old in zip(current, previous))
        previous = current


def test_de_records_the_full_differential_provenance(make_context):
    """A trial has four parents; recording only the target erases DE's structure."""
    ctx = make_context("sphere", {"dimension": 5})
    algorithm = DifferentialEvolution(ctx, strategy="rand/1")
    population = algorithm.initialize(10)
    ctx.generation = 1
    updated = algorithm.step(population)
    trials = [ind for ind in updated if ind.operator == "de_trial"]
    assert trials and all(len(trial.parent_ids) == 4 for trial in trials)


def test_pso_velocity_is_clamped(make_context):
    ctx = make_context("sphere", {"dimension": 10})
    algorithm = ParticleSwarmOptimization(ctx, velocity_clamp=0.1)
    population = algorithm.initialize(20)
    for generation in range(1, 15):
        ctx.generation = generation
        population = algorithm.step(population)
        for particle in population:
            assert np.all(np.abs(particle.velocity) <= algorithm.v_max + 1e-9)


def test_pso_positions_stay_in_bounds(make_context):
    ctx = make_context("rastrigin", {"dimension": 8})
    algorithm = ParticleSwarmOptimization(ctx)
    population = algorithm.initialize(20)
    for generation in range(1, 20):
        ctx.generation = generation
        population = algorithm.step(population)
        for particle in population:
            assert np.all(particle.genome >= algorithm.lower - 1e-9)
            assert np.all(particle.genome <= algorithm.upper + 1e-9)


def test_pso_personal_best_is_monotone(make_context):
    ctx = make_context("sphere", {"dimension": 5})
    algorithm = ParticleSwarmOptimization(ctx)
    population = algorithm.initialize(15)
    bests = [particle.best_fitness for particle in population]
    for generation in range(1, 10):
        ctx.generation = generation
        population = algorithm.step(population)
        current = [particle.best_fitness for particle in population]
        assert all(new <= old + 1e-12 for new, old in zip(current, bests))
        bests = current


def test_pso_random_coefficients_are_drawn_per_component(make_context):
    """Per-particle draws would turn the stochastic search into a scaled line search."""
    ctx = make_context("sphere", {"dimension": 30})
    algorithm = ParticleSwarmOptimization(ctx, inertia=0.0, constriction=False)
    population = algorithm.initialize(10)
    ctx.generation = 1
    moved = algorithm.step(population)
    particle = moved[0]
    displacement = particle.velocity
    assert np.std(displacement) > 0


def test_pso_ring_topology_uses_local_informants(make_context):
    ctx = make_context("sphere", {"dimension": 4})
    algorithm = ParticleSwarmOptimization(ctx, topology="ring", neighbourhood_size=1)
    population = algorithm.initialize(12)
    algorithm._population = population
    informants = algorithm._informant_indices(12)
    for index, informant in enumerate(informants):
        assert informant in ((index - 1) % 12, index, (index + 1) % 12)


def test_pso_gbest_topology_gives_every_particle_the_same_informant(make_context):
    ctx = make_context("sphere", {"dimension": 4})
    algorithm = ParticleSwarmOptimization(ctx, topology="gbest")
    population = algorithm.initialize(12)
    algorithm._population = population
    assert len(set(algorithm._informant_indices(12).tolist())) == 1


def test_pso_constriction_requires_phi_above_four(make_context):
    ctx = make_context("sphere", {"dimension": 4})
    with pytest.raises(ValueError, match="cognitive \\+ social > 4"):
        ParticleSwarmOptimization(ctx, constriction=True, cognitive=1.0, social=1.0)


def test_pso_constriction_coefficient_matches_clerc_and_kennedy(make_context):
    ctx = make_context("sphere", {"dimension": 4})
    algorithm = ParticleSwarmOptimization(ctx, constriction=True, cognitive=2.05, social=2.05)
    assert algorithm.chi == pytest.approx(0.72984, abs=1e-4)


def test_pso_inertia_decreases_linearly_over_the_run(make_context):
    ctx = make_context("sphere", {"dimension": 4})
    algorithm = ParticleSwarmOptimization(
        ctx, inertia=0.9, inertia_final=0.4, total_generations=11, constriction=False
    )
    assert algorithm._inertia() == pytest.approx(0.9)
    algorithm._generation = 5
    assert algorithm._inertia() == pytest.approx(0.65)
    algorithm._generation = 10
    assert algorithm._inertia() == pytest.approx(0.4)


def test_pso_migrant_carries_velocity_and_personal_best(make_context):
    """A migrant stripped of momentum would make migration look far more disruptive."""
    ctx = make_context("sphere", {"dimension": 6})
    algorithm = ParticleSwarmOptimization(ctx)
    population = algorithm.initialize(10)
    payload = algorithm.serialize_migrant(population[0])
    assert payload["velocity"] is not None and payload["best_genome"] is not None
    restored = algorithm.deserialize_migrant(payload)
    assert np.allclose(restored.velocity, population[0].velocity)
    assert np.allclose(restored.best_genome, population[0].best_genome)


def test_pso_logged_genome_hash_ignores_velocity(make_context):
    ctx = make_context("sphere", {"dimension": 4})
    algorithm = ParticleSwarmOptimization(ctx)
    population = algorithm.initialize(4)
    particle = population[0]
    before = genome_hash(GenomeEncoding.REAL_VECTOR_VELOCITY, algorithm.genome_for_log(particle))
    particle.velocity = particle.velocity * -3.0
    after = genome_hash(GenomeEncoding.REAL_VECTOR_VELOCITY, algorithm.genome_for_log(particle))
    assert before == after


def test_es_learning_rates_match_the_standard_settings(make_context):
    ctx = make_context("sphere", {"dimension": 16})
    algorithm = EvolutionStrategy(ctx)
    assert algorithm.tau == pytest.approx(1.0 / np.sqrt(2.0 * np.sqrt(16)))
    assert algorithm.tau_prime == pytest.approx(1.0 / np.sqrt(2.0 * 16))


def test_es_mutation_changes_the_step_sizes(make_context):
    ctx = make_context("sphere", {"dimension": 10})
    algorithm = EvolutionStrategy(ctx)
    sigma = np.full(10, 0.5)
    _genome, new_sigma = algorithm._mutate(np.zeros(10), sigma)
    assert not np.allclose(new_sigma, sigma)
    assert np.all(new_sigma > 0)


def test_es_step_sizes_never_reach_zero(make_context):
    """A zero step size freezes a lineage permanently."""
    ctx = make_context("sphere", {"dimension": 6})
    algorithm = EvolutionStrategy(ctx)
    sigma = np.full(6, 1e-300)
    for _ in range(200):
        _genome, sigma = algorithm._mutate(np.zeros(6), sigma)
        assert np.all(sigma >= SIGMA_FLOOR)


def test_es_mutation_uses_the_new_sigma_for_the_object_variables(make_context):
    """Order matters: selection must judge a step size by the offspring it produced."""
    ctx = make_context("sphere", {"dimension": 20, "lower": -1e6, "upper": 1e6})
    algorithm = EvolutionStrategy(ctx)
    genome, sigma = algorithm._mutate(np.zeros(20), np.full(20, 1000.0))
    scale = float(np.mean(np.abs(genome)))
    assert scale > 100.0
    assert np.mean(sigma) > 100.0


@pytest.mark.parametrize("selection", ["comma", "plus"])
@pytest.mark.parametrize("sigma_mode", ["n_sigma", "single"])
def test_es_selection_variants_preserve_mu_and_improve(selection, sigma_mode, make_context):
    ctx = make_context("sphere", {"dimension": 8})
    algorithm = EvolutionStrategy(
        ctx, selection=selection, sigma_mode=sigma_mode, offspring_per_parent=5
    )
    population = algorithm.initialize(8)
    initial = ctx.best_fitness
    for generation in range(1, 25):
        ctx.generation = generation
        population = algorithm.step(population)
        assert len(population) == 8
    assert ctx.best_fitness < initial


def test_es_plus_selection_never_loses_the_best(make_context):
    ctx = make_context("sphere", {"dimension": 6})
    algorithm = EvolutionStrategy(ctx, selection="plus", offspring_per_parent=3)
    population = algorithm.initialize(6)
    best = min(individual.fitness for individual in population)
    for generation in range(1, 10):
        ctx.generation = generation
        population = algorithm.step(population)
        current = min(individual.fitness for individual in population)
        assert current <= best + 1e-12
        best = current


def test_es_produces_lambda_offspring_per_generation(make_context):
    ctx = make_context("sphere", {"dimension": 5})
    algorithm = EvolutionStrategy(ctx, offspring_per_parent=7)
    population = algorithm.initialize(10)
    before = ctx.evaluations
    ctx.generation = 1
    algorithm.step(population)
    assert ctx.evaluations - before == 70


@pytest.mark.parametrize("recombination", ["discrete", "intermediate", "none"])
def test_es_recombination_variants(recombination, make_context):
    ctx = make_context("sphere", {"dimension": 6})
    algorithm = EvolutionStrategy(ctx, recombination=recombination, rho=3)
    population = algorithm.initialize(6)
    ctx.generation = 1
    assert len(algorithm.step(population)) == 6


def test_es_migrant_carries_its_step_sizes(make_context):
    """Resetting sigma on arrival would confound any measurement of migration's effect."""
    ctx = make_context("sphere", {"dimension": 7})
    algorithm = EvolutionStrategy(ctx)
    population = algorithm.initialize(6)
    payload = algorithm.serialize_migrant(population[0])
    assert payload["sigma"] is not None
    restored = algorithm.deserialize_migrant(payload)
    assert np.allclose(np.asarray(restored.sigma), np.asarray(population[0].sigma))


def test_es_logged_genome_hash_ignores_sigma(make_context):
    ctx = make_context("sphere", {"dimension": 5})
    algorithm = EvolutionStrategy(ctx)
    population = algorithm.initialize(4)
    individual = population[0]
    before = genome_hash(GenomeEncoding.REAL_VECTOR_SIGMA, algorithm.genome_for_log(individual))
    individual.sigma = np.asarray(individual.sigma) * 1000.0
    after = genome_hash(GenomeEncoding.REAL_VECTOR_SIGMA, algorithm.genome_for_log(individual))
    assert before == after
