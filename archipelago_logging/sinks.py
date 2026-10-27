"""Where log records go."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from .events import RecordKind

__all__ = ["Sink", "SinkStats", "CallbackSink"]


@dataclass
class SinkStats:
    """Delivery accounting for one sink, reported in ``island_end``."""

    written: int = 0
    errors: int = 0
    last_error: str | None = None


class Sink(ABC):
    """Accepts serialised records, each filed under its :class:`RecordKind`.

    A sink never drops a record. One that cannot keep up makes ``write`` wait; one that can
    no longer deliver raises.
    """

    def __init__(self) -> None:
        self.stats = SinkStats()

    @abstractmethod
    def write(self, line: str, kind: RecordKind) -> None:
        """Accept one record."""

    def flush(self) -> None:
        """Return once every record written so far has been delivered."""

    def close(self) -> None:
        """Flush and release resources. Calling it again does nothing."""


class CallbackSink(Sink):
    """Hands each line to a Python callable. For tests and in-process consumers."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__()
        self._callback = callback

    def write(self, line: str, kind: RecordKind) -> None:
        """Hand the line to the callback."""
        self._callback(line)
        self.stats.written += 1
