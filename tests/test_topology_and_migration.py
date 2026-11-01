"""Topologies, migrant selection and replacement policies."""

from __future__ import annotations

import numpy as np
import pytest

from conftest import KNAPSACK_INSTANCE
from algorithms.base import Individual
from benchmarks.registry import create_problem
from orchestrator.migration import (
    REPLACEMENT_POLICIES,
    SELECTION_POLICIES,
    choose_replacements,
    select_migrants,
)
from orchestrator.topology import TOPOLOGIES, build_topology, describe_topology


def _params_for(name: str, count: int) -> dict:
    return {"torus": {"rows": 2}, "random": {"degree": 2}}.get(name, {})


@pytest.mark.parametrize("name", sorted(TOPOLOGIES))
def test_topologies_reference_only_valid_islands(name):
    count = 6
    topology = build_topology(name, count, **_params_for(name, count))
    assert set(topology) == set(range(count))
    for island, neighbours in topology.items():
        assert island not in neighbours, "no island may migrate to itself"
        assert all(0 <= neighbour < count for neighbour in neighbours)
        assert len(neighbours) == len(set(neighbours)), "no duplicate edges"


def test_ring_is_a_single_directed_cycle():
    topology = build_topology("ring", 5)
    assert topology == {0: [1], 1: [2], 2: [3], 3: [4], 4: [0]}


def test_star_makes_one_island_structurally_distinct():
    topology = build_topology("star", 5, hub=2)
    assert sorted(topology[2]) == [0, 1, 3, 4]
    assert all(topology[spoke] == [2] for spoke in (0, 1, 3, 4))


def test_fully_connected_has_n_times_n_minus_one_edges():
    topology = build_topology("fully_connected", 5)
    assert sum(len(neighbours) for neighbours in topology.values()) == 20


def test_lattice_end_islands_have_a_single_neighbour():
    topology = build_topology("lattice", 5)
    assert topology[0] == [1] and topology[4] == [3]
    assert topology[2] == [1, 3]


def test_single_island_topology_is_empty_not_a_self_loop():
    for name in ("ring", "bidirectional_ring", "fully_connected", "lattice"):
        assert build_topology(name, 1) == {0: []}


def test_random_topology_is_reproducible_from_its_seed():
    a = build_topology("random", 6, degree=2, seed=42)
    b = build_topology("random", 6, degree=2, seed=42)
    c = build_topology("random", 6, degree=2, seed=43)
    assert a == b and a != c


def test_impossible_topology_parameters_are_rejected():
    with pytest.raises(ValueError, match="exceeds"):
        build_topology("random", 3, degree=5)
    with pytest.raises(ValueError, match="not a valid island id"):
        build_topology("star", 3, hub=9)
    with pytest.raises(ValueError, match="does not divide"):
        build_topology("torus", 6, rows=4)
    with pytest.raises(ValueError, match="unknown topology"):
        build_topology("hypercube", 4)


def test_describe_topology_reports_both_adjacency_directions():
    topology = build_topology("ring", 4)
    described = describe_topology("ring", 4, topology)
    assert described["neighbours_out"]["0"] == [1]
    assert described["neighbours_in"]["0"] == [3]
    assert described["num_edges"] == 4
    assert described["in_degree"] == {"0": 1, "1": 1, "2": 1, "3": 1}


def _population(fitnesses, dimension=3):
    return [
        Individual(
            genome=np.full(dimension, float(index)),
            fitness=float(fitness),
            individual_id=f"i00-g000000-n{index:09d}",
        )
        for index, fitness in enumerate(fitnesses)
    ]


@pytest.fixture
def minimisation_problem():
    return create_problem("sphere", {"dimension": 3})


@pytest.fixture
def maximisation_problem():
    return create_problem("knapsack", KNAPSACK_INSTANCE)


@pytest.mark.parametrize("policy", sorted(SELECTION_POLICIES))
def test_every_selection_policy_returns_valid_distinct_indices(policy, rng, minimisation_problem):
    population = _population([5.0, 1.0, 9.0, 3.0, 7.0, 2.0])
    indices = select_migrants(policy, population, 3, rng, minimisation_problem)
    assert len(indices) == 3
    assert len(set(indices)) == 3
    assert all(0 <= index < len(population) for index in indices)


def test_best_selection_respects_the_minimisation_direction(rng, minimisation_problem):
    population = _population([5.0, 1.0, 9.0, 3.0])
    assert sorted(select_migrants("best", population, 2, rng, minimisation_problem)) == [1, 3]


def test_best_selection_respects_the_maximisation_direction(rng, maximisation_problem):
    population = _population([5.0, 1.0, 9.0, 3.0])
    assert sorted(select_migrants("best", population, 2, rng, maximisation_problem)) == [0, 2]


def test_worst_selection_is_the_mirror_of_best(rng, minimisation_problem):
    population = _population([5.0, 1.0, 9.0, 3.0])
    assert sorted(select_migrants("worst", population, 2, rng, minimisation_problem)) == [0, 2]


def test_diverse_selection_spreads_out_in_genome_space(rng, minimisation_problem):
    """`best` on a converged island ships one point dressed up as several."""
    population = [
        Individual(genome=np.array([0.0, 0.0]), fitness=1.0, individual_id="a"),
        Individual(genome=np.array([0.01, 0.0]), fitness=1.1, individual_id="b"),
        Individual(genome=np.array([0.02, 0.0]), fitness=1.2, individual_id="c"),
        Individual(genome=np.array([50.0, 50.0]), fitness=1.3, individual_id="d"),
    ]
    chosen = select_migrants("diverse", population, 2, rng, minimisation_problem, candidate_pool=4)
    assert 3 in chosen, "the distant individual should be preferred over a near-clone"


def test_selection_never_exceeds_the_population(rng, minimisation_problem):
    population = _population([1.0, 2.0])
    assert len(select_migrants("best", population, 10, rng, minimisation_problem)) == 2


def test_selection_of_an_empty_population_is_empty(rng, minimisation_problem):
    assert select_migrants("best", [], 3, rng, minimisation_problem) == []


@pytest.mark.parametrize("policy", sorted(REPLACEMENT_POLICIES))
def test_every_replacement_policy_returns_valid_distinct_indices(policy, rng, minimisation_problem):
    population = _population([5.0, 1.0, 9.0, 3.0, 7.0])
    incoming = _population([0.5, 0.6])
    victims = choose_replacements(policy, population, incoming, rng, minimisation_problem)
    assert len(set(victims)) == len(victims)
    assert all(0 <= index < len(population) for index in victims)
    assert len(victims) <= len(incoming)


def test_worst_replacement_evicts_the_least_fit(rng, minimisation_problem):
    population = _population([5.0, 1.0, 9.0, 3.0])
    incoming = _population([0.5])
    assert choose_replacements("worst", population, incoming, rng, minimisation_problem) == [2]


def test_similar_replacement_evicts_the_nearest_neighbour(rng, minimisation_problem):
    population = [
        Individual(genome=np.array([0.0, 0.0]), fitness=1.0, individual_id="a"),
        Individual(genome=np.array([100.0, 100.0]), fitness=2.0, individual_id="b"),
    ]
    incoming = [Individual(genome=np.array([99.0, 99.0]), fitness=0.1, individual_id="in")]
    victims = choose_replacements(
        "similar", population, incoming, rng, minimisation_problem, candidate_pool=2
    )
    assert victims == [1], "the immigrant should displace its own nearest neighbour"


def test_none_replacement_grows_the_population(rng, minimisation_problem):
    population = _population([1.0, 2.0])
    incoming = _population([0.5])
    assert choose_replacements("none", population, incoming, rng, minimisation_problem) == []


def test_policies_fall_back_gracefully_for_non_numeric_genomes(rng, minimisation_problem):
    """Tree genomes have no meaningful Euclidean distance; the policy must not crash."""
    population = [
        Individual(genome="add(x,1)", fitness=1.0, individual_id="a"),
        Individual(genome="mul(x,x)", fitness=2.0, individual_id="b"),
    ]
    incoming = [Individual(genome="sin(x)", fitness=0.5, individual_id="in")]
    assert select_migrants("diverse", population, 1, rng, minimisation_problem) == [0]
    assert choose_replacements("similar", population, incoming, rng, minimisation_problem) == [1]


def test_unknown_policies_name_the_alternatives(rng, minimisation_problem):
    population = _population([1.0])
    with pytest.raises(ValueError, match="available"):
        select_migrants("psychic", population, 1, rng, minimisation_problem)
    with pytest.raises(ValueError, match="available"):
        choose_replacements("psychic", population, population, rng, minimisation_problem)
