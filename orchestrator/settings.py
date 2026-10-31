"""Deployment settings: read from the environment by the Runner service, each run's master and
each island.

A run's own configuration (:class:`~orchestrator.config.RunConfig`) is what a preset and the
backend's overrides decide. Everything here is decided by whoever deploys the Runner, is the same
for every run, and is never part of a request.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ServiceSettings", "redact_url"]


def redact_url(url: str) -> str:
    """The URL with any password replaced, so it can be logged."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    user = parts.username or ""
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port is not None else ""
    return urlunsplit(parts._replace(netloc=f"{user}:***@{host}{port}"))


class ServiceSettings(BaseModel):
    """Each field is read from the environment variable of the same name in capitals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        description="redis://[user:password@]host:port/db. The master and every island append to it directly; each run's master joins the service's Docker networks and its islands join the master's, so this one address works for all of them. The password is redacted wherever the settings are logged.",
    )
    island_image: str = Field(default="archipelago-island:latest", description="Image each island container runs.")
    master_image: str = Field(
        default="archipelago-runner:latest",
        description="Image each run's master container runs: the Runner's own image.",
    )
    island_cpus: float = Field(default=1.0, gt=0.0, description="CPU quota per island container.")
    island_memory: str = Field(default="2g", description="Memory limit per island container, e.g. '2g'.")
    max_concurrent_runs: int = Field(default=2, ge=1, description="Runs executing at once; the rest wait in arrival order.")
    max_containers: int = Field(
        default=15,
        ge=2,
        description="Containers the runs may use at once, a master and its islands each. A run that could never fit, even alone, is refused.",
    )
    broker_connect_timeout_seconds: float = Field(
        default=10.0,
        gt=0.0,
        description="How long the master, and then each island, keeps trying to reach Redis at startup before the run fails.",
    )
    broker_stall_seconds: float = Field(
        default=120.0,
        gt=0.0,
        description="How long a writer keeps retrying with nothing acknowledged before the run fails. Records are never dropped, so a Redis that stays unavailable fails the run instead.",
    )
    broker_buffer_records: int = Field(
        default=10_000,
        ge=1,
        description="Records an island holds while Redis catches up. When the buffer is full the island waits.",
    )
    heartbeat_seconds: float = Field(
        default=2.0,
        gt=0.0,
        description="How often an island reports its progress to the master; the reply carries the master's wind-down flag. Not a liveness probe.",
    )
    bye_timeout_seconds: float = Field(
        default=20.0,
        gt=0.0,
        description="How long an island gets to finish its generation, flush its records and say goodbye after being asked to stop, before it is killed.",
    )
    run_time_limit_seconds: float = Field(
        default=3600.0,
        gt=0.0,
        description="Backstop: a run still going after this long is wound down and reported failed, so a run nobody cancels cannot hold a slot forever.",
    )

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "ServiceSettings":
        """The settings named in the environment; a variable that is not set keeps its default."""
        environ = os.environ if environ is None else environ
        return cls(**{name: environ[name.upper()] for name in cls.model_fields if name.upper() in environ})

    def to_env(self) -> dict[str, str]:
        """The environment that reproduces these settings, for the containers a run starts."""
        return {name.upper(): str(getattr(self, name)) for name in type(self).model_fields}

    def described(self) -> dict[str, Any]:
        """JSON-safe settings with the password redacted, for ``run_start``."""
        payload = self.model_dump(mode="json")
        payload["redis_url"] = redact_url(self.redis_url)
        return payload
