"""Execution-backend abstraction: how island workers get launched and watched."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["WorkerSpec", "WorkerStatus", "ExecutionBackend"]


@dataclass
class WorkerSpec:
    """Everything one island worker needs to start."""

    island_id: int
    run_id: str
    config_path: Path
    output_dir: Path
    control_host: str
    control_port: int
    environment: dict[str, str] = field(default_factory=dict)


@dataclass
class WorkerStatus:
    """Backend's view of one worker: is this island's process still going?"""

    island_id: int
    node_id: str
    running: bool


class ExecutionBackend(ABC):
    """Launches island workers and reports whether they are still running."""

    name: str = "abstract"

    @abstractmethod
    def prepare(self) -> None:
        """One-time setup before any worker starts (image build, directory creation)."""

    @abstractmethod
    def launch(self, spec: WorkerSpec) -> str:
        """Start one worker and return its node id (container id or ``host:pid``)."""

    @abstractmethod
    def statuses(self) -> dict[int, WorkerStatus]:
        """Current status of every launched worker, keyed by island id."""

    @abstractmethod
    def stop_all(self, grace_seconds: float = 15.0) -> None:
        """Ask every worker to exit, then force any that do not."""

    def cleanup(self) -> None:
        """Release backend resources. Must be safe to call more than once."""

    def describe(self) -> dict[str, Any]:
        """Backend facts recorded in ``run_start.config``."""
        return {"backend": self.name}

    def __enter__(self) -> "ExecutionBackend":
        self.prepare()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop_all()
        self.cleanup()
