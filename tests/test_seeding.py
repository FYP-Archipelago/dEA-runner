"""Seed derivation: one run seed, independent and repeatable streams."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from orchestrator.config import RunConfig
from orchestrator.seeding import seed_for_broker, seed_for_island


def test_a_run_seed_always_derives_the_same_island_seed():
    assert seed_for_island(42, 3) == seed_for_island(42, 3)


def test_every_island_gets_its_own_stream():
    seeds = {seed_for_island(42, island) for island in range(6)}
    assert len(seeds) == 6


def test_different_run_seeds_give_different_islands():
    assert seed_for_island(1, 0) != seed_for_island(2, 0)


def test_island_and_broker_streams_are_distinct():
    """Same run seed, same index: the two must still be independent streams."""
    assert seed_for_island(42, 0) != seed_for_broker(42, 0)


def test_an_island_seed_recreates_the_island_generator():
    """island_start.island_seed is enough to reproduce an island's random stream."""
    seed = seed_for_island(7, 2)
    first = np.random.default_rng(seed).random(5)
    second = np.random.default_rng(seed).random(5)
    assert np.array_equal(first, second)


def test_without_a_run_seed_islands_draw_fresh_entropy():
    assert seed_for_island(None, 0) != seed_for_island(None, 0)


def test_without_a_run_seed_the_broker_draws_fresh_entropy():
    assert seed_for_broker(None, 0) is None


def test_the_seed_is_optional_and_bounded():
    run_id = "run-20261031T120000Z-5eed5eed"
    assert RunConfig(run_id=run_id).seed is None
    assert RunConfig(run_id=run_id, seed=0).seed == 0
    assert RunConfig(run_id=run_id, seed=4_294_967_295).seed == 4_294_967_295
    for invalid in (-1, 4_294_967_296):
        with pytest.raises(ValidationError, match="seed"):
            RunConfig(run_id=run_id, seed=invalid)
