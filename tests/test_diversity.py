"""Population diversity measures."""

from __future__ import annotations

import numpy as np
import pytest

from algorithms.diversity import (
    DEFAULT_SAMPLE_SIZE,
    mean_pairwise_euclidean,
    mean_pairwise_hamming,
    mean_pairwise_position_mismatch,
    sample_indices,
    unique_signature_ratio,
)


def test_a_converged_population_has_zero_diversity(rng):
    identical = [np.array([1.0, 2.0, 3.0]) for _ in range(20)]
    value, _metric, _sample = mean_pairwise_euclidean(identical, rng)
    assert value == pytest.approx(0.0)

    bits = [np.array([1, 0, 1, 1], dtype=np.int8) for _ in range(20)]
    assert mean_pairwise_hamming(bits, rng)[0] == pytest.approx(0.0)

    tours = [np.arange(8) for _ in range(20)]
    assert mean_pairwise_position_mismatch(tours, rng)[0] == pytest.approx(0.0)

    assert unique_signature_ratio(["add(x,1)"] * 20)[0] == pytest.approx(0.05)


def test_random_bitstrings_have_about_half_hamming_diversity(rng):
    """0.5 is the reference point a reader compares a diversity trace against."""
    population = [rng.integers(0, 2, 200, dtype=np.int8) for _ in range(60)]
    value, metric, _sample = mean_pairwise_hamming(population, rng)
    assert metric == "mean_pairwise_hamming_normalised"
    assert 0.45 < value < 0.55


def test_random_permutations_have_high_position_mismatch(rng):
    population = [rng.permutation(50) for _ in range(60)]
    value, _metric, _sample = mean_pairwise_position_mismatch(population, rng)
    assert value > 0.9


def test_normalised_euclidean_is_comparable_across_problem_scales(rng):
    """Sphere on [-5.12, 5.12] and Griewank on [-600, 600] must be readable together."""
    small = [rng.uniform(-5.12, 5.12, 10) for _ in range(50)]
    large = [rng.uniform(-600, 600, 10) for _ in range(50)]
    small_value, _m, _s = mean_pairwise_euclidean(
        small, rng, normaliser=float(np.linalg.norm(np.full(10, 10.24)))
    )
    large_value, _m, _s = mean_pairwise_euclidean(
        large, rng, normaliser=float(np.linalg.norm(np.full(10, 1200.0)))
    )
    assert abs(small_value - large_value) < 0.05


def test_unnormalised_and_normalised_report_different_metric_names(rng):
    population = [rng.uniform(-1, 1, 5) for _ in range(20)]
    assert mean_pairwise_euclidean(population, rng)[1] == "mean_pairwise_euclidean"
    assert (
        mean_pairwise_euclidean(population, rng, normaliser=2.0)[1]
        == "mean_pairwise_euclidean_normalised"
    )


def test_sampling_reports_null_when_the_whole_population_was_used(rng):
    """Null means exact; a number means estimated. Downstream must be able to tell."""
    indices, reported = sample_indices(100, rng, sample_size=DEFAULT_SAMPLE_SIZE)
    assert reported is None and len(indices) == 100

    indices, reported = sample_indices(5000, rng, sample_size=64)
    assert reported == 64 and len(indices) == 64
    assert len(set(indices.tolist())) == 64


def test_pairwise_measures_stay_cheap_on_a_large_population(rng):
    """An O(n^2) measure on 100,000 individuals would dominate the generation."""
    import time

    population = [rng.uniform(-1, 1, 30) for _ in range(20_000)]
    started = time.monotonic()
    value, _metric, sample = mean_pairwise_euclidean(population, rng)
    assert time.monotonic() - started < 5.0
    assert sample == DEFAULT_SAMPLE_SIZE
    assert value > 0


def test_single_individual_populations_do_not_divide_by_zero(rng):
    assert mean_pairwise_euclidean([np.array([1.0])], rng)[0] == 0.0
    assert mean_pairwise_hamming([np.array([1], dtype=np.int8)], rng)[0] == 0.0
    assert unique_signature_ratio(["x"])[0] == 1.0
    assert unique_signature_ratio([])[0] == 0.0
