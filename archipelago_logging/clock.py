"""Time sources for asynchronous, multi-container island runs."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "wall_now",
    "mono_ns",
    "iso_now",
    "ClockOffset",
    "measure_clock_offset",
]


def wall_now() -> float:
    """Current wall-clock time as Unix epoch seconds (float)."""
    return time.time()


def mono_ns() -> int:
    """Node-local monotonic clock in nanoseconds. Origin is arbitrary."""
    return time.monotonic_ns()


def iso_now() -> str:
    """Current UTC time as an RFC 3339 / ISO 8601 string with ``Z`` suffix."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1e6):06d}Z"


@dataclass(frozen=True)
class ClockOffset:
    """Result of a clock-offset measurement against a reference (the master)."""

    offset_ns: int
    rtt_ns: int
    samples: int


def measure_clock_offset(
    probe: Callable[[], float],
    samples: int = 5,
) -> ClockOffset:
    """Estimate this node's clock offset relative to a reference clock."""
    best: ClockOffset | None = None
    for _ in range(max(1, samples)):
        t0 = time.time()
        t_ref = probe()
        t1 = time.time()
        rtt_ns = int((t1 - t0) * 1e9)
        offset_ns = int((t_ref - (t0 + t1) / 2.0) * 1e9)
        if best is None or rtt_ns < best.rtt_ns:
            best = ClockOffset(offset_ns=offset_ns, rtt_ns=rtt_ns, samples=0)
    assert best is not None
    return ClockOffset(offset_ns=best.offset_ns, rtt_ns=best.rtt_ns, samples=samples)
