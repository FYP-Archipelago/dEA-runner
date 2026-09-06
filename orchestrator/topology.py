"""Island interconnection topologies."""

from __future__ import annotations

from typing import Any, Callable

import numpy as np

__all__ = ["build_topology", "TOPOLOGIES", "describe_topology", "Topology"]

Topology = dict[int, list[int]]


def _validate(count: int) -> None:
    if count < 1:
        raise ValueError(f"num_islands must be at least 1, got {count}")


def ring(count: int, **_: Any) -> Topology:
    """Unidirectional ring: ``i -> (i + 1) mod n``."""
    _validate(count)
    if count == 1:
        return {0: []}
    return {island: [(island + 1) % count] for island in range(count)}


def bidirectional_ring(count: int, **_: Any) -> Topology:
    """Ring with edges in both directions. Mixes about twice as fast as ``ring``."""
    _validate(count)
    if count == 1:
        return {0: []}
    if count == 2:
        return {0: [1], 1: [0]}
    return {island: [(island - 1) % count, (island + 1) % count] for island in range(count)}


def fully_connected(count: int, **_: Any) -> Topology:
    """Every island sends to every other. Fastest mixing, least diversity."""
    _validate(count)
    return {island: [other for other in range(count) if other != island] for island in range(count)}


def star(count: int, hub: int = 0, **_: Any) -> Topology:
    """All spokes exchange with a single hub island."""
    _validate(count)
    if not 0 <= hub < count:
        raise ValueError(f"star hub {hub} is not a valid island id for {count} islands")
    topology: Topology = {island: [] for island in range(count)}
    for island in range(count):
        if island == hub:
            topology[island] = [other for other in range(count) if other != hub]
        else:
            topology[island] = [hub]
    return topology


def random_graph(count: int, degree: int = 2, seed: int = 0, **_: Any) -> Topology:
    """Each island sends to ``degree`` others, chosen uniformly without replacement."""
    _validate(count)
    if degree < 0:
        raise ValueError("random topology degree must be non-negative")
    if count > 1 and degree > count - 1:
        raise ValueError(
            f"random topology degree={degree} exceeds the {count - 1} available neighbours "
            f"for {count} islands"
        )
    rng = np.random.default_rng(seed)
    topology: Topology = {}
    for island in range(count):
        candidates = [other for other in range(count) if other != island]
        if not candidates or degree == 0:
            topology[island] = []
        else:
            topology[island] = sorted(
                int(choice) for choice in rng.choice(candidates, size=degree, replace=False)
            )
    return topology


def torus(count: int, rows: int | None = None, **_: Any) -> Topology:
    """2-D wrap-around grid; each island's four grid neighbours."""
    _validate(count)
    if rows is None:
        rows = max(
            (candidate for candidate in range(1, int(count**0.5) + 1) if count % candidate == 0),
            default=1,
        )
    if count % rows != 0:
        raise ValueError(f"torus needs rows to divide num_islands; {rows} does not divide {count}")
    columns = count // rows

    topology: Topology = {}
    for island in range(count):
        row, column = divmod(island, columns)
        neighbours = {
            ((row + 1) % rows) * columns + column,
            ((row - 1) % rows) * columns + column,
            row * columns + (column + 1) % columns,
            row * columns + (column - 1) % columns,
        }
        neighbours.discard(island)
        topology[island] = sorted(neighbours)
    return topology


def lattice(count: int, **_: Any) -> Topology:
    """Open 1-D chain: like ``bidirectional_ring`` but without the wrap-around."""
    _validate(count)
    topology: Topology = {}
    for island in range(count):
        neighbours = []
        if island > 0:
            neighbours.append(island - 1)
        if island < count - 1:
            neighbours.append(island + 1)
        topology[island] = neighbours
    return topology


TOPOLOGIES: dict[str, Callable[..., Topology]] = {
    "ring": ring,
    "bidirectional_ring": bidirectional_ring,
    "fully_connected": fully_connected,
    "star": star,
    "random": random_graph,
    "torus": torus,
    "lattice": lattice,
}


def build_topology(name: str, count: int, **params: Any) -> Topology:
    """Construct a topology by name."""
    key = name.lower()
    if key not in TOPOLOGIES:
        raise ValueError(f"unknown topology {name!r}; available: {sorted(TOPOLOGIES)}")
    try:
        return TOPOLOGIES[key](count, **params)
    except TypeError as exc:
        raise ValueError(f"invalid parameters for topology {name!r}: {exc}") from exc


def describe_topology(name: str, count: int, topology: Topology, **params: Any) -> dict[str, Any]:
    """Summary written into ``run_start.migration``."""
    incoming: dict[int, list[int]] = {island: [] for island in range(count)}
    for source, destinations in topology.items():
        for destination in destinations:
            incoming[destination].append(source)

    return {
        "topology": name,
        "topology_params": params,
        "num_islands": count,
        "neighbours_out": {str(island): sorted(targets) for island, targets in topology.items()},
        "neighbours_in": {str(island): sorted(sources) for island, sources in incoming.items()},
        "num_edges": sum(len(targets) for targets in topology.values()),
        "out_degree": {str(island): len(targets) for island, targets in topology.items()},
        "in_degree": {str(island): len(sources) for island, sources in incoming.items()},
    }
