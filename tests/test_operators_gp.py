"""GP tree representation and Koza operators."""

from __future__ import annotations

import numpy as np
import pytest

from algorithms.gp import GeneticProgramming
from algorithms.gp_tree import (
    FUNCTION_SETS,
    Node,
    canonical,
    depth,
    evaluate,
    iter_nodes,
    parse,
    replace_at,
    size,
)


def test_canonical_and_parse_round_trip():
    for text in ("x", "1.5", "add(x,1.5)", "mul(add(x,y),sin(x))", "div(1,sub(x,x))"):
        assert canonical(parse(text)) == text


def test_parse_rejects_unknown_primitives_and_arity_errors():
    with pytest.raises(ValueError, match="unknown primitive"):
        parse("frobnicate(x)")
    with pytest.raises(ValueError, match="takes 2 arguments"):
        parse("add(x)")
    with pytest.raises(ValueError, match="unbalanced"):
        parse("add(x,y")


def test_evaluation_is_vectorised_over_samples():
    tree = parse("add(mul(x,x),1.5)")
    x = np.array([[1.0], [2.0], [3.0]])
    assert evaluate(tree, x, ["x"]).tolist() == [2.5, 5.5, 10.5]


def test_protected_division_returns_one_near_zero():
    """Koza's closure requirement: no expression may be unevaluable."""
    x = np.array([[0.0], [1e-15], [2.0]])
    assert evaluate(parse("div(5,x)"), x, ["x"]).tolist() == [1.0, 1.0, 2.5]


def test_protected_log_and_sqrt_accept_negatives():
    x = np.array([[-4.0], [0.0]])
    assert np.all(np.isfinite(evaluate(parse("log(x)"), x, ["x"])))
    assert evaluate(parse("sqrt(x)"), x, ["x"]).tolist() == [2.0, 0.0]


def test_protected_exp_does_not_overflow_to_inf():
    """Collapsing a whole region onto `inf` would destroy the ranking selection needs."""
    x = np.array([[1000.0], [-1000.0]])
    assert np.all(np.isfinite(evaluate(parse("exp(x)"), x, ["x"])))


def test_size_and_depth():
    tree = parse("add(mul(x,x),1.5)")
    assert size(tree) == 5
    assert depth(tree) == 2
    assert size(parse("x")) == 1 and depth(parse("x")) == 0


def test_replace_at_targets_the_right_preorder_index():
    tree = parse("add(mul(x,x),1.5)")
    assert [canonical(node) for node in iter_nodes(tree)] == [
        "add(mul(x,x),1.5)",
        "mul(x,x)",
        "x",
        "x",
        "1.5",
    ]
    assert canonical(replace_at(tree, 1, parse("y"))) == "add(y,1.5)"
    assert canonical(replace_at(tree, 3, parse("sin(x)"))) == "add(mul(x,sin(x)),1.5)"
    assert canonical(replace_at(tree, 4, parse("z"))) == "add(mul(x,x),z)"


def test_replace_at_does_not_mutate_the_original():
    """Trees are frozen precisely so a child's edit cannot reach into a parent."""
    tree = parse("add(mul(x,x),1.5)")
    replace_at(tree, 1, parse("y"))
    assert canonical(tree) == "add(mul(x,x),1.5)"


def test_constants_are_rendered_at_fixed_precision():
    """The tree analogue of rounding a real vector for the location signature."""
    a = canonical(Node("const", (), 1.0 / 3.0))
    b = canonical(Node("const", (), 1.0 / 3.0 + 1e-12))
    assert a == b


@pytest.mark.parametrize("function_set", sorted(FUNCTION_SETS))
def test_every_function_set_produces_evaluable_trees(function_set, make_context):
    ctx = make_context("nguyen9")
    algorithm = GeneticProgramming(ctx, function_set=function_set)
    population = algorithm.initialize(40)
    assert all(np.isfinite(individual.fitness) for individual in population)


def test_ramped_half_and_half_produces_varied_depths(make_context):
    ctx = make_context("nguyen7")
    algorithm = GeneticProgramming(ctx, min_init_depth=2, max_init_depth=6)
    population = algorithm.initialize(120)
    depths = {depth(individual.genome.root) for individual in population}
    assert len(depths) >= 3


def test_depth_limit_is_enforced_on_offspring(make_context):
    """Koza's limit: an over-deep offspring is discarded, not repaired."""
    ctx = make_context("nguyen7")
    algorithm = GeneticProgramming(ctx, max_depth=5, max_init_depth=4)
    population = algorithm.initialize(60)
    for generation in range(1, 20):
        ctx.generation = generation
        population = algorithm.step(population)
        assert all(depth(individual.genome.root) <= 5 for individual in population)


def test_internal_node_bias_prefers_internal_nodes(make_context):
    """Without the 90/10 rule, crossover degenerates into swapping single leaves."""
    ctx = make_context("nguyen7")
    algorithm = GeneticProgramming(ctx, internal_node_bias=0.9)
    tree = parse("add(mul(x,x),sub(x,1.0))")
    nodes = list(iter_nodes(tree))
    picks = [algorithm._select_node_index(tree) for _ in range(2000)]
    internal_fraction = sum(1 for index in picks if not nodes[index].is_terminal) / len(picks)
    assert 0.85 < internal_fraction < 0.95


def test_hoist_mutation_can_only_shrink(make_context):
    ctx = make_context("nguyen7")
    algorithm = GeneticProgramming(ctx)
    tree = parse("add(mul(x,x),sub(sin(x),1.0))")
    for _ in range(50):
        assert size(algorithm._hoist_mutation(tree)) < size(tree)


def test_point_mutation_preserves_structure(make_context):
    ctx = make_context("nguyen7")
    algorithm = GeneticProgramming(ctx, function_set="arithmetic")
    tree = parse("add(mul(x,x),sub(x,1.0))")
    for _ in range(50):
        mutant = algorithm._point_mutation(tree)
        assert size(mutant) == size(tree)
        assert depth(mutant) == depth(tree)


def test_gp_operator_probabilities_must_not_exceed_one(make_context):
    ctx = make_context("nguyen7")
    with pytest.raises(ValueError, match="exceeds 1.0"):
        GeneticProgramming(ctx, crossover_rate=0.9, subtree_mutation_rate=0.5)


def test_gp_rejects_unknown_primitives(make_context):
    ctx = make_context("nguyen7")
    with pytest.raises(ValueError, match="unknown primitives"):
        GeneticProgramming(ctx, function_set=["add", "teleport"])


def test_gp_improves_on_a_symbolic_regression_problem(make_context):
    ctx = make_context("nguyen1")
    algorithm = GeneticProgramming(ctx)
    population = algorithm.initialize(200)
    initial = ctx.best_fitness
    for generation in range(1, 25):
        ctx.generation = generation
        population = algorithm.step(population)
    assert ctx.best_fitness < initial


def test_parsimony_pressure_affects_selection_but_not_the_logged_fitness(make_context):
    """A log whose fitness silently included a size term would not match published RMSEs."""
    ctx = make_context("nguyen7")
    algorithm = GeneticProgramming(ctx, parsimony_coefficient=0.5)
    population = algorithm.initialize(30)
    raw = np.array([individual.fitness for individual in population])
    adjusted = algorithm._selection_fitnesses(population)
    assert not np.allclose(raw, adjusted)
    assert np.allclose(raw, [individual.fitness for individual in population])


def test_gp_diversity_is_the_unique_signature_ratio(make_context):
    ctx = make_context("nguyen7")
    algorithm = GeneticProgramming(ctx)
    population = algorithm.initialize(50)
    value, metric, sample = algorithm.diversity(population)
    assert metric == "unique_signature_ratio"
    assert 0.0 < value <= 1.0
    assert sample is None


def test_gp_migrant_round_trips_through_the_canonical_string(make_context):
    ctx = make_context("nguyen7")
    algorithm = GeneticProgramming(ctx)
    population = algorithm.initialize(20)
    payload = algorithm.serialize_migrant(population[0])
    assert isinstance(payload["genome"], str)
    restored = algorithm.deserialize_migrant(payload)
    assert canonical(restored.genome.root) == canonical(population[0].genome.root)


def test_gp_population_statistics_expose_bloat(make_context):
    ctx = make_context("nguyen7")
    algorithm = GeneticProgramming(ctx)
    population = algorithm.initialize(40)
    stats = algorithm.population_statistics(population)
    assert set(stats) == {"mean_tree_size", "max_tree_size", "mean_tree_depth", "max_tree_depth"}
    assert stats["max_tree_size"] >= stats["mean_tree_size"] > 0
