"""Seeds for every random stream in a run, derived from one run seed.

Streams come from ``numpy.random.SeedSequence`` with a distinct spawn key each,
which NumPy guarantees are independent. ``seed + island_id`` is deliberately not
used: such streams can overlap.
"""

from __future__ import annotations

import os

import numpy as np

__all__ = ["seed_for_island", "seed_for_broker"]

_ISLAND = 0
_BROKER = 1


def _derive(run_seed: int, *spawn_key: int) -> int:
    """A 64-bit seed for the stream identified by ``spawn_key``."""
    sequence = np.random.SeedSequence(run_seed, spawn_key=spawn_key)
    return int(sequence.generate_state(1, dtype=np.uint64)[0])


def seed_for_island(run_seed: int | None, island_id: int) -> int:
    """The seed for one island's generator; fresh OS entropy when the run has no seed."""
    if run_seed is None:
        return int.from_bytes(os.urandom(8), "little")
    return _derive(run_seed, _ISLAND, island_id)


def seed_for_broker(run_seed: int | None, source_island: int) -> int | None:
    """The seed for the master's destination choices for one source island, or None.

    One stream per source island, so island k's n-th migration picks the same
    destination however the islands' requests happen to interleave.
    """
    if run_seed is None:
        return None
    return _derive(run_seed, _BROKER, source_island)
