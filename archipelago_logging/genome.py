"""Genome encoding, precision reduction and hashing."""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from enum import Enum
from hashlib import blake2b
from typing import Any, Mapping, Sequence

__all__ = [
    "GenomeEncoding",
    "GenomeLogMode",
    "GenomePolicy",
    "EncodedGenome",
    "genome_signature",
    "genome_hash",
    "encode_compact",
    "decode_compact",
]

_DIGEST_BYTES = 16

DEFAULT_PRECISION_DECIMALS = 4


class GenomeEncoding(str, Enum):
    """The structural family of a genome."""

    BITSTRING = "bitstring"
    PERMUTATION = "permutation"
    REAL_VECTOR = "real_vector"
    REAL_VECTOR_VELOCITY = "real_vector_velocity"
    REAL_VECTOR_SIGMA = "real_vector_sigma"
    EXPRESSION_TREE = "expression_tree"


class GenomeLogMode(str, Enum):
    """How much of the genome is written into each ``evaluation`` record."""

    HASHED = "hashed"
    COMPACT = "compact"
    FULL = "full"


@dataclass(frozen=True)
class EncodedGenome:
    """The genome fields of one ``evaluation`` record."""

    encoding: GenomeEncoding
    genome_hash: str
    genome_repr: Any | None
    genome_repr_mode: GenomeLogMode
    precision_decimals: int | None
    dim: int | None


def _position_of(encoding: GenomeEncoding, genome: Any) -> Sequence[Any]:
    """Extract the search-space *position* component of a genome."""
    if encoding in (GenomeEncoding.REAL_VECTOR_VELOCITY, GenomeEncoding.REAL_VECTOR_SIGMA):
        if not isinstance(genome, Mapping) or "x" not in genome:
            raise TypeError(
                f"encoding {encoding.value!r} expects a mapping with an 'x' key, got {type(genome).__name__}"
            )
        return genome["x"]
    return genome


def _format_real(value: float, decimals: int) -> str:
    """Round a float to ``decimals`` places as a canonical decimal string."""
    text = f"{float(value):.{decimals}f}"
    if text.startswith("-") and float(text) == 0.0:
        text = text[1:]
    return text


def genome_signature(
    encoding: GenomeEncoding,
    genome: Any,
    precision_decimals: int | None = DEFAULT_PRECISION_DECIMALS,
) -> str:
    """Return the canonical location signature string for ``genome``."""
    position = _position_of(encoding, genome)

    if encoding is GenomeEncoding.BITSTRING:
        body = "".join("1" if int(bit) else "0" for bit in position)
    elif encoding is GenomeEncoding.PERMUTATION:
        body = ",".join(str(int(index)) for index in position)
    elif encoding in (
        GenomeEncoding.REAL_VECTOR,
        GenomeEncoding.REAL_VECTOR_VELOCITY,
        GenomeEncoding.REAL_VECTOR_SIGMA,
    ):
        decimals = DEFAULT_PRECISION_DECIMALS if precision_decimals is None else precision_decimals
        body = ",".join(_format_real(coord, decimals) for coord in position)
    elif encoding is GenomeEncoding.EXPRESSION_TREE:
        body = position if isinstance(position, str) else str(position)
    else:
        raise ValueError(f"unhandled genome encoding: {encoding!r}")

    return f"{encoding.value}|{body}"


def genome_hash(
    encoding: GenomeEncoding,
    genome: Any,
    precision_decimals: int | None = DEFAULT_PRECISION_DECIMALS,
) -> str:
    """128-bit blake2b digest of :func:`genome_signature`, as 32 hex characters."""
    signature = genome_signature(encoding, genome, precision_decimals)
    return blake2b(signature.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _pack_floats(values: Sequence[float]) -> str:
    """Pack floats as little-endian float64 and base64 them."""
    values = [float(v) for v in values]
    return _b64(struct.pack(f"<{len(values)}d", *values))


def _unpack_floats(text: str) -> list[float]:
    raw = _unb64(text)
    return list(struct.unpack(f"<{len(raw) // 8}d", raw))


def _pack_bits(bits: Sequence[int]) -> str:
    """Pack a bitstring 8 bits to the byte; the length prefix disambiguates padding."""
    length = len(bits)
    packed = bytearray((length + 7) // 8)
    for index, bit in enumerate(bits):
        if int(bit):
            packed[index // 8] |= 1 << (index % 8)
    return f"{length}:{_b64(bytes(packed))}"


def _unpack_bits(text: str) -> list[int]:
    length_text, _, payload = text.partition(":")
    length = int(length_text)
    packed = _unb64(payload)
    return [(packed[index // 8] >> (index % 8)) & 1 for index in range(length)]


def _as_float_list(value: Any) -> list[float]:
    """Coerce a scalar-or-vector strategy parameter into a list of floats."""
    if hasattr(value, "__len__"):
        return [float(item) for item in value]
    return [float(value)]


def _pack_ints(values: Sequence[int]) -> str:
    """Pack a permutation as little-endian uint32."""
    values = [int(v) for v in values]
    return _b64(struct.pack(f"<{len(values)}I", *values))


def _unpack_ints(text: str) -> list[int]:
    raw = _unb64(text)
    return list(struct.unpack(f"<{len(raw) // 4}I", raw))


def encode_compact(encoding: GenomeEncoding, genome: Any) -> Any:
    """Encode ``genome`` into its lossless compact representation."""
    if encoding is GenomeEncoding.BITSTRING:
        return _pack_bits(genome)
    if encoding is GenomeEncoding.PERMUTATION:
        return _pack_ints(genome)
    if encoding is GenomeEncoding.REAL_VECTOR:
        return _pack_floats(genome)
    if encoding is GenomeEncoding.REAL_VECTOR_VELOCITY:
        return {"x": _pack_floats(genome["x"]), "v": _pack_floats(genome["v"])}
    if encoding is GenomeEncoding.REAL_VECTOR_SIGMA:
        return {"x": _pack_floats(genome["x"]), "sigma": _pack_floats(_as_float_list(genome["sigma"]))}
    if encoding is GenomeEncoding.EXPRESSION_TREE:
        return genome if isinstance(genome, str) else str(genome)
    raise ValueError(f"unhandled genome encoding: {encoding!r}")


def decode_compact(encoding: GenomeEncoding, payload: Any) -> Any:
    """Inverse of :func:`encode_compact`. Provided so logs are round-trippable."""
    if encoding is GenomeEncoding.BITSTRING:
        return _unpack_bits(payload)
    if encoding is GenomeEncoding.PERMUTATION:
        return _unpack_ints(payload)
    if encoding is GenomeEncoding.REAL_VECTOR:
        return _unpack_floats(payload)
    if encoding is GenomeEncoding.REAL_VECTOR_VELOCITY:
        return {"x": _unpack_floats(payload["x"]), "v": _unpack_floats(payload["v"])}
    if encoding is GenomeEncoding.REAL_VECTOR_SIGMA:
        return {"x": _unpack_floats(payload["x"]), "sigma": _unpack_floats(payload["sigma"])}
    if encoding is GenomeEncoding.EXPRESSION_TREE:
        return payload
    raise ValueError(f"unhandled genome encoding: {encoding!r}")


def _dim_of(encoding: GenomeEncoding, genome: Any) -> int | None:
    if encoding is GenomeEncoding.EXPRESSION_TREE:
        return None
    try:
        return len(_position_of(encoding, genome))
    except TypeError:
        return None


def _to_jsonable(encoding: GenomeEncoding, genome: Any) -> Any:
    """Convert a genome to plain JSON types for ``full`` mode."""
    if encoding is GenomeEncoding.BITSTRING:
        return [int(bit) for bit in genome]
    if encoding is GenomeEncoding.PERMUTATION:
        return [int(index) for index in genome]
    if encoding is GenomeEncoding.REAL_VECTOR:
        return [float(value) for value in genome]
    if encoding is GenomeEncoding.REAL_VECTOR_VELOCITY:
        return {"x": [float(v) for v in genome["x"]], "v": [float(v) for v in genome["v"]]}
    if encoding is GenomeEncoding.REAL_VECTOR_SIGMA:
        return {
            "x": [float(v) for v in genome["x"]],
            "sigma": _as_float_list(genome["sigma"]),
        }
    return genome if isinstance(genome, str) else str(genome)


@dataclass
class GenomePolicy:
    """How much of each evaluated genome an ``evaluation`` record carries."""

    mode: GenomeLogMode = GenomeLogMode.COMPACT
    precision_decimals: int | None = DEFAULT_PRECISION_DECIMALS
    always_full_for_best: bool = True

    def encode(
        self,
        encoding: GenomeEncoding,
        genome: Any,
        *,
        is_best: bool = False,
    ) -> EncodedGenome:
        """Produce the genome fields for one ``evaluation`` record."""
        effective = self.mode
        if is_best and self.always_full_for_best:
            effective = GenomeLogMode.FULL

        precision = (
            self.precision_decimals
            if encoding
            in (
                GenomeEncoding.REAL_VECTOR,
                GenomeEncoding.REAL_VECTOR_VELOCITY,
                GenomeEncoding.REAL_VECTOR_SIGMA,
            )
            else None
        )

        if effective is GenomeLogMode.HASHED:
            representation: Any | None = None
        elif effective is GenomeLogMode.COMPACT:
            representation = encode_compact(encoding, genome)
        else:
            representation = _to_jsonable(encoding, genome)

        return EncodedGenome(
            encoding=encoding,
            genome_hash=genome_hash(encoding, genome, self.precision_decimals),
            genome_repr=representation,
            genome_repr_mode=effective,
            precision_decimals=precision,
            dim=_dim_of(encoding, genome),
        )
