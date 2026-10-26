"""Identifier minting for runs, islands, individuals and migration events."""

from __future__ import annotations

import itertools
import os
import re
import time
from dataclasses import dataclass

__all__ = [
    "new_run_id",
    "IndividualIdMinter",
    "MigrationIdMinter",
    "ParsedIndividualId",
    "parse_individual_id",
]

_INDIVIDUAL_RE = re.compile(r"^i(?P<island>\d{2,})-g(?P<generation>\d{6,})-n(?P<ordinal>\d{9,})$")


def new_run_id() -> str:
    """Mint a run id: UTC timestamp for sortability plus 8 hex chars for uniqueness."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"run-{stamp}-{os.urandom(4).hex()}"


class IndividualIdMinter:
    """Mints individual ids for a single island."""

    __slots__ = ("_island_id", "_counter")

    def __init__(self, island_id: int, start: int = 0) -> None:
        self._island_id = int(island_id)
        self._counter = itertools.count(start)

    @property
    def island_id(self) -> int:
        """The island these ids are minted for."""
        return self._island_id

    def mint(self, generation: int) -> str:
        """Return the next individual id for ``generation`` on this island."""
        return f"i{self._island_id:02d}-g{int(generation):06d}-n{next(self._counter):09d}"


class MigrationIdMinter:
    """Mints migration ids on the master."""

    __slots__ = ("_counter",)

    def __init__(self, start: int = 0) -> None:
        self._counter = itertools.count(start)

    def mint(self, source_island: int, dest_island: int) -> str:
        """Mint an id encoding the source and destination islands."""
        return f"mig-{int(source_island):02d}-{int(dest_island):02d}-{next(self._counter):06d}"


@dataclass(frozen=True)
class ParsedIndividualId:
    """Decomposed individual id."""

    island_id: int
    generation: int
    ordinal: int


def parse_individual_id(individual_id: str) -> ParsedIndividualId:
    """Recover ``(island_id, generation, ordinal)`` from an individual id."""
    match = _INDIVIDUAL_RE.match(individual_id)
    if match is None:
        raise ValueError(f"not a well-formed Archipelago individual id: {individual_id!r}")
    return ParsedIndividualId(
        island_id=int(match["island"]),
        generation=int(match["generation"]),
        ordinal=int(match["ordinal"]),
    )
