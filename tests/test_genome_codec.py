"""Genome signatures, hashes and compact encodings."""

from __future__ import annotations

import math

import pytest

from archipelago_logging.genome import (
    DEFAULT_PRECISION_DECIMALS,
    GenomeEncoding,
    GenomeLogMode,
    GenomePolicy,
    decode_compact,
    encode_compact,
    genome_hash,
    genome_signature,
)


def test_signature_is_deterministic():
    a = genome_signature(GenomeEncoding.REAL_VECTOR, [1.0, 2.0, 3.0])
    b = genome_signature(GenomeEncoding.REAL_VECTOR, [1.0, 2.0, 3.0])
    assert a == b
    assert genome_hash(GenomeEncoding.REAL_VECTOR, [1.0, 2.0, 3.0]) == genome_hash(
        GenomeEncoding.REAL_VECTOR, [1.0, 2.0, 3.0]
    )


def test_hash_is_32_hex_characters():
    digest = genome_hash(GenomeEncoding.BITSTRING, [1, 0, 1])
    assert len(digest) == 32
    int(digest, 16)


def test_encodings_do_not_collide_on_identical_numbers():
    """[1, 0, 1] as a bitstring and as a permutation are different points."""
    assert genome_hash(GenomeEncoding.BITSTRING, [1, 0, 1]) != genome_hash(
        GenomeEncoding.PERMUTATION, [1, 0, 1]
    )


def test_precision_collapses_nearby_points():
    """Two points closer than the precision are the same STN node, by design."""
    near = genome_hash(GenomeEncoding.REAL_VECTOR, [1.000001, 2.0], precision_decimals=4)
    exact = genome_hash(GenomeEncoding.REAL_VECTOR, [1.0, 2.0], precision_decimals=4)
    assert near == exact

    finer = genome_hash(GenomeEncoding.REAL_VECTOR, [1.000001, 2.0], precision_decimals=8)
    assert finer != exact


def test_negative_zero_is_normalised():
    """-1e-9 and +1e-9 must not become distinct nodes at 4 decimal places."""
    assert genome_signature(GenomeEncoding.REAL_VECTOR, [-1e-9]) == genome_signature(
        GenomeEncoding.REAL_VECTOR, [1e-9]
    )
    assert "-0.0000" not in genome_signature(GenomeEncoding.REAL_VECTOR, [-1e-9])


def test_velocity_is_excluded_from_the_signature():
    """Two particles at the same point are the same node, whatever their momentum."""
    slow = {"x": [1.0, 2.0], "v": [0.0, 0.0]}
    fast = {"x": [1.0, 2.0], "v": [9.9, -9.9]}
    assert genome_hash(GenomeEncoding.REAL_VECTOR_VELOCITY, slow) == genome_hash(
        GenomeEncoding.REAL_VECTOR_VELOCITY, fast
    )


def test_strategy_parameters_are_excluded_from_the_signature():
    small = {"x": [1.0, 2.0], "sigma": [0.001, 0.001]}
    large = {"x": [1.0, 2.0], "sigma": [5.0, 5.0]}
    assert genome_hash(GenomeEncoding.REAL_VECTOR_SIGMA, small) == genome_hash(
        GenomeEncoding.REAL_VECTOR_SIGMA, large
    )


@pytest.mark.parametrize(
    ("encoding", "genome"),
    [
        (GenomeEncoding.BITSTRING, [1, 0, 1, 1, 0, 0, 0, 1, 1]),
        (GenomeEncoding.PERMUTATION, [3, 1, 4, 0, 2]),
        (GenomeEncoding.REAL_VECTOR, [1.5, -2.25, math.pi]),
    ],
)
def test_compact_encoding_is_lossless(encoding, genome):
    packed = encode_compact(encoding, genome)
    assert decode_compact(encoding, packed) == pytest.approx(genome)


def test_compact_bitstring_preserves_length_through_padding():
    """A 9-bit string packs into 2 bytes; the length prefix is what recovers it."""
    for length in range(1, 20):
        bits = [i % 2 for i in range(length)]
        assert decode_compact(GenomeEncoding.BITSTRING, encode_compact(GenomeEncoding.BITSTRING, bits)) == bits


def test_compact_is_smaller_than_full_for_real_vectors():
    genome = [1.23456789012345] * 30
    compact = encode_compact(GenomeEncoding.REAL_VECTOR, genome)
    assert len(compact) < len(str(genome))


def test_policy_modes():
    genome = [1.0, 2.0, 3.0]

    compact = GenomePolicy(mode=GenomeLogMode.COMPACT).encode(GenomeEncoding.REAL_VECTOR, genome)
    assert isinstance(compact.genome_repr, str)

    full = GenomePolicy(mode=GenomeLogMode.FULL).encode(GenomeEncoding.REAL_VECTOR, genome)
    assert full.genome_repr == genome


def test_hash_is_present_in_every_mode():
    """The hash is never optional: it is the only field that makes records joinable."""
    for mode in GenomeLogMode:
        encoded = GenomePolicy(mode=mode).encode(GenomeEncoding.REAL_VECTOR, [1.0])
        assert len(encoded.genome_hash) == 32


def test_best_individuals_are_always_logged_in_full():
    policy = GenomePolicy(mode=GenomeLogMode.COMPACT, always_full_for_best=True)
    ordinary = policy.encode(GenomeEncoding.REAL_VECTOR, [1.0])
    best = policy.encode(GenomeEncoding.REAL_VECTOR, [1.0], is_best=True)
    assert ordinary.genome_repr_mode is GenomeLogMode.COMPACT
    assert isinstance(ordinary.genome_repr, str)
    assert best.genome_repr_mode is GenomeLogMode.FULL
    assert best.genome_repr == [1.0]


def test_precision_is_recorded_only_for_real_encodings():
    real = GenomePolicy().encode(GenomeEncoding.REAL_VECTOR, [1.0])
    bits = GenomePolicy().encode(GenomeEncoding.BITSTRING, [1, 0])
    assert real.precision_decimals == DEFAULT_PRECISION_DECIMALS
    assert bits.precision_decimals is None


def test_expression_tree_signature_is_the_canonical_string():
    signature = genome_signature(GenomeEncoding.EXPRESSION_TREE, "add(x,1.5)")
    assert signature.endswith("add(x,1.5)")
    assert genome_hash(GenomeEncoding.EXPRESSION_TREE, "add(x,1.5)") != genome_hash(
        GenomeEncoding.EXPRESSION_TREE, "add(1.5,x)"
    )
