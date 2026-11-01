"""Shared fixtures."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from archipelago_logging import CallbackSink, EventLogger, GenomePolicy, new_run_id
from algorithms.base import IslandContext
from benchmarks.registry import create_problem


KNAPSACK_INSTANCE = {"instance_file": "benchmarks/data/knapsack/uncorrelated_n100_R1000.kp"}
TSP_INSTANCE = {"instance_file": "benchmarks/data/tsp/eil51.tsp"}

TEST_ISLAND_IMAGE = "archipelago-island:pytest"
TEST_RUNNER_IMAGE = "archipelago-runner:pytest"

BROKER_IMAGES = {"redis": "redis:7-alpine", "valkey": "valkey/valkey:8-alpine"}
"""The brokers the runner must work against: the backend's Redis, and its open-source fork."""


def _docker_problem() -> str | None:
    """Why Docker cannot be used here, or None when it can."""
    try:
        import docker
    except ImportError:
        return "the docker SDK is not installed"
    try:
        docker.from_env().ping()
    except Exception as exc:
        return f"no reachable Docker daemon ({exc})"
    return None


@pytest.fixture
def rng() -> np.random.Generator:
    """A seeded generator, so a failing test fails the same way twice."""
    return np.random.default_rng(20260902)


@pytest.fixture
def captured_lines() -> list[str]:
    return []


@pytest.fixture
def logger(captured_lines: list[str]) -> EventLogger:
    """An EventLogger writing into a list, for asserting on exact emitted bytes."""
    return EventLogger(
        new_run_id(), "island:0", CallbackSink(captured_lines.append), island_id=0
    )


@pytest.fixture
def make_context(logger: EventLogger):
    """Factory building an :class:`IslandContext` for a named benchmark."""
    def _make(benchmark: str, params: dict | None = None, seed: int = 7) -> IslandContext:
        problem = create_problem(benchmark, params or {})
        return IslandContext(
            island_id=0,
            problem=problem,
            rng=np.random.default_rng(seed),
            logger=logger,
            genome_policy=GenomePolicy(),
            genome_encoding=None,
        )

    return _make


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "runs"
    directory.mkdir()
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session")
def repository_root() -> Path:
    return REPOSITORY_ROOT


def _build_image(repository_root: Path, dockerfile: str, tag: str) -> str:
    """Build an image with the docker CLI under its own tag, so a test run can never execute
    stale code and never replaces the image a real deployment uses."""
    problem = _docker_problem()
    if problem is not None:
        pytest.skip(problem)
    build = subprocess.run(
        ["docker", "build", "--network=host", "--quiet", "-f", dockerfile, "-t", tag, "."],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if build.returncode != 0:
        raise RuntimeError(f"could not build {tag}:\n{build.stderr}")
    return tag


@pytest.fixture(scope="session")
def island_image(repository_root: Path) -> str:
    """The island image, built once per session from the current source."""
    return _build_image(repository_root, "docker/Dockerfile.island", TEST_ISLAND_IMAGE)


@pytest.fixture(scope="session")
def runner_image(repository_root: Path, island_image: str) -> str:
    """The Runner image (service and masters), built once per session from the current source."""
    return _build_image(repository_root, "docker/Dockerfile.runner", TEST_RUNNER_IMAGE)


def settings_for(broker: "Broker", **overrides):
    """Service settings for a test run: the session's images, small containers, quick timeouts."""
    from orchestrator.settings import ServiceSettings

    return ServiceSettings(
        **{
            "redis_url": broker.url,
            "island_image": TEST_ISLAND_IMAGE,
            "master_image": TEST_RUNNER_IMAGE,
            "island_memory": "1g",
            "heartbeat_seconds": 0.5,
            "bye_timeout_seconds": 10.0,
            **overrides,
        }
    )


@dataclass(frozen=True)
class Broker:
    """A throwaway broker container, configured as the backend runs Redis."""

    name: str
    url: str
    port: int

    def client(self):
        import redis

        return redis.Redis.from_url(self.url, decode_responses=True)


class _BrokerPool:
    """Broker containers, each started on first use and removed when the session ends."""

    def __init__(self) -> None:
        self._started: dict[str, tuple[str, Broker]] = {}

    def get(self, name: str) -> Broker:
        if name not in self._started:
            self._started[name] = self._start(name)
        return self._started[name][1]

    def close(self) -> None:
        for container, _ in self._started.values():
            subprocess.run(["docker", "rm", "--force", container], capture_output=True)

    @staticmethod
    def _start(name: str) -> tuple[str, Broker]:
        container = subprocess.run(
            [
                "docker", "run", "--detach", "--rm", "--label", "app=archipelago-test",
                "--publish", "127.0.0.1::6379", BROKER_IMAGES[name],
                "--maxmemory-policy", "noeviction", "--save", "", "--appendonly", "no",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        mapping = subprocess.run(
            ["docker", "port", container, "6379/tcp"], check=True, capture_output=True, text=True
        ).stdout.splitlines()[0]
        port = int(mapping.rsplit(":", 1)[1])
        broker = Broker(name, f"redis://127.0.0.1:{port}/0", port)
        client = broker.client()
        deadline = time.monotonic() + 30
        while True:
            try:
                client.ping()
                return container, broker
            except Exception:
                if time.monotonic() > deadline:
                    subprocess.run(["docker", "rm", "--force", container], capture_output=True)
                    raise
                time.sleep(0.1)


@pytest.fixture(scope="session")
def _broker_pool():
    problem = _docker_problem()
    if problem is not None:
        pytest.skip(problem)
    pool = _BrokerPool()
    yield pool
    pool.close()


@pytest.fixture(scope="session", params=sorted(BROKER_IMAGES))
def broker(request, _broker_pool) -> Broker:
    """Redis 7 and Valkey 8 in turn, with ``noeviction`` and no persistence."""
    return _broker_pool.get(request.param)


@pytest.fixture(scope="session")
def run_broker(_broker_pool) -> Broker:
    """The Redis that whole runs append to. Islands use host networking, so they reach it."""
    return _broker_pool.get("redis")
