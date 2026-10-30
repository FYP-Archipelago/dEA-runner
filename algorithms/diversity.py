"""Population diversity measures, one."""

from __future__ import annotations

from typing import Sequence

import numpy as np

__all__ = [
    "DEFAULT_SAMPLE_SIZE",
    "sample_indices",
    "mean_pairwise_euclidean",
    "mean_pairwise_hamming",
    "mean_pairwise_position_mismatch",
    "unique_signature_ratio",
]

DEFAULT_SAMPLE_SIZE = 256


def sample_indices(
    count: int, rng: np.random.Generator, sample_size: int = DEFAULT_SAMPLE_SIZE
) -> tuple[np.ndarray, int | None]:
    """Return ``(indices, reported_sample_size)``."""
    if count <= sample_size:
        return np.arange(count), None
    return rng.choice(count, size=sample_size, replace=False), sample_size


def _mean_pairwise(distances: np.ndarray) -> float:
    """Mean of the strict up."""
    count = distances.shape[0]
    if count < 2:
        return 0.0
    upper = np.triu_indices(count, k=1)
    return float(np.mean(distances[upper]))


def mean_pairwise_euclidean(
    genomes: Sequence[np.ndarray],
    rng: np.random.Generator,
    *,
    normaliser: float | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> tuple[float, str, int | None]:
    """Mean pairwise Euclidean distance, optionally divided by ``normaliser``."""
    matrix = np.asarray(genomes, dtype=float)
    indices, reported = sample_indices(matrix.shape[0], rng, sample_size)
    sampled = matrix[indices]
    differences = sampled[:, None, :] - sampled[None, :, :]
    distances = np.sqrt(np.sum(differences * differences, axis=-1))
    value = _mean_pairwise(distances)
    if normaliser and normaliser > 0:
        return value / normaliser, "mean_pairwise_euclidean_normalised", reported
    return value, "mean_pairwise_euclidean", reported


def mean_pairwise_hamming(
    genomes: Sequence[np.ndarray],
    rng: np.random.Generator,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> tuple[float, str, int | None]:
    """Mean pairwise Hamming distance over bitstrings, divided by string length."""
    matrix = np.asarray(genomes, dtype=np.int8)
    indices, reported = sample_indices(matrix.shape[0], rng, sample_size)
    sampled = matrix[indices]
    differences = (sampled[:, None, :] != sampled[None, :, :]).sum(axis=-1).astype(float)
    return _mean_pairwise(differences) / max(1, matrix.shape[1]), "mean_pairwise_hamming_normalised", reported


def mean_pairwise_position_mismatch(
    genomes: Sequence[np.ndarray],
    rng: np.random.Generator,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> tuple[float, str, int | None]:
    """Mean fraction of positions at which two permutations differ."""
    matrix = np.asarray(genomes, dtype=int)
    indices, reported = sample_indices(matrix.shape[0], rng, sample_size)
    sampled = matrix[indices]
    differences = (sampled[:, None, :] != sampled[None, :, :]).sum(axis=-1).astype(float)
    return (
        _mean_pairwise(differences) / max(1, matrix.shape[1]),
        "mean_pairwise_position_mismatch_normalised",
        reported,
    )


def unique_signature_ratio(signatures: Sequence[str]) -> tuple[float, str, int | None]:
    """Fraction of the population that is structurally distinct."""
    if not signatures:
        return 0.0, "unique_signature_ratio", None
    return len(set(signatures)) / len(signatures), "unique_signature_ratio", None
