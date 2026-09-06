"""Master orchestration: container/process lifecycle, migration, log aggregation."""

from .config import (
    DockerConfig,
    ExecutionConfig,
    LoggingConfig,
    MigrationConfig,
    RunConfig,
    load_config,
)
from .island_runner import IslandResult, IslandRunner
from .master import Master, RunSummary, run
from .migration import choose_replacements, select_migrants
from .topology import build_topology, describe_topology

__all__ = [
    "RunConfig",
    "MigrationConfig",
    "LoggingConfig",
    "ExecutionConfig",
    "DockerConfig",
    "load_config",
    "Master",
    "RunSummary",
    "run",
    "IslandRunner",
    "IslandResult",
    "build_topology",
    "describe_topology",
    "select_migrants",
    "choose_replacements",
]
