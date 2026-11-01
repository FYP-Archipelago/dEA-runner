"""The Run Queue: runs wait in arrival order, a few execute at once, each run's master in its own
container.

The service is only the queue and the bookkeeping. A run's master container does the run, and
the run's stream says how it ended: when the container exits, its ``run_end`` gives the state,
and the error diagnostic just before it gives the error text. Running masters are independent of
the service, so a restarted service finds them again by their labels.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import redis

from orchestrator.backends import MASTER_CONTAINER_NAME_ENV
from orchestrator.config import RUN_CONFIG_ENV, RunConfig
from orchestrator.redis_stream import stream_key
from orchestrator.settings import ServiceSettings

__all__ = [
    "RunQueue",
    "Conflict",
    "BrokerUnavailable",
    "QUEUED",
    "RUNNING",
    "FINISHED",
    "FAILED",
    "CANCELLED",
    "master_container_name",
]

QUEUED, RUNNING, FINISHED, FAILED, CANCELLED = "queued", "running", "finished", "failed", "cancelled"

DOCKER_SOCKET = "/var/run/docker.sock"
_POLL_SECONDS = 0.5
_RESIGNAL_SECONDS = 2.0
_BACKSTOP_MARGIN_SECONDS = 60.0
"""How long past its time limit and goodbye timeout a master may run before the service kills it:
the master enforces the limit itself, so this only catches a master that has stopped working."""


class Conflict(Exception):
    """The run id is taken (409)."""


class BrokerUnavailable(Exception):
    """Redis cannot be reached, so the request cannot be answered safely (503)."""


_TAIL_ENTRIES = 8
"""How many of a stream's last entries are searched for the Runner's own last record."""


def _runner_record(entry: tuple[bytes, dict[bytes, bytes]]) -> dict[str, Any] | None:
    """The record a stream entry carries if the Runner wrote it, else None."""
    data = entry[1].get(b"data")
    try:
        record = json.loads(data) if data is not None else None
    except ValueError:
        return None
    return record if isinstance(record, dict) and "type" in record and "seq" in record else None


def master_container_name(run_id: str) -> str:
    """The run's master container, named like its islands."""
    return f"archipelago-{run_id[-8:]}-master"


def _started_at(container: Any) -> float:
    """When a container started, as a Unix time; now if Docker's timestamp cannot be read."""
    stamp = container.attrs.get("State", {}).get("StartedAt", "")
    try:
        return datetime.fromisoformat(stamp[:26].rstrip("Z") + "+00:00").timestamp()
    except ValueError:
        return time.time()


@dataclass
class Run:
    """What the service knows about one run."""

    run_id: str
    request: str
    num_islands: int
    config: RunConfig | None = None
    state: str = QUEUED
    error: str | None = None
    container: Any | None = None
    started_at: float | None = None
    cancel_requested: bool = False
    last_signal: float = 0.0

    def view(self) -> dict[str, Any]:
        """The run object every endpoint answers with."""
        return {"run_id": self.run_id, "state": self.state, "error": self.error}


class RunQueue:
    """Queues, starts and watches runs. Start with :meth:`start`, stop with :meth:`stop`.

    Stopping leaves running masters alone: they finish on their own, and the next service to
    start finds them. Queued runs live only here, and are lost.
    """

    def __init__(self, settings: ServiceSettings) -> None:
        self.settings = settings
        self.redis = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=5
        )
        self._docker: Any | None = None
        self._runs: dict[str, Run] = {}
        self._waiting: deque[str] = deque()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def docker(self) -> Any:
        if self._docker is None:
            import docker

            self._docker = docker.from_env()
        return self._docker

    def start(self) -> None:
        """Find the masters a previous service left running, then start watching."""
        with self._lock:
            self._adopt()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="run-queue", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)


    def submit(self, run_id: str, request: str, config: RunConfig) -> dict[str, Any]:
        """Queue a run, and start it at once if there is room. An exact repeat starts nothing."""
        with self._lock:
            known = self._runs.get(run_id)
            if known is not None:
                if known.request != request:
                    raise Conflict(
                        f"run_id {run_id!r} was already used with a different preset or overrides"
                    )
                return known.view()
            entries = self._stream_length(run_id)
            if entries:
                raise Conflict(
                    f"{stream_key(run_id)} already holds {entries} entries, but this Runner has no "
                    "record of the run (it may have restarted); choose a new run_id"
                )
            self._runs[run_id] = Run(run_id, request, config.num_islands, config=config)
            self._waiting.append(run_id)
            self._launch_waiting()
            return self._runs[run_id].view()

    def get(self, run_id: str) -> dict[str, Any] | None:
        """The run object, from memory or, for a run this service never saw, from its stream."""
        with self._lock:
            known = self._runs.get(run_id)
            if known is not None:
                return known.view()
        outcome = self._stream_outcome(run_id)
        if outcome is None:
            return None
        state, error = outcome
        return {"run_id": run_id, "state": state, "error": error}

    def cancel(self, run_id: str) -> tuple[int, dict[str, Any]] | None:
        """Cancel a run: 202 while a running run winds down, 200 once it is terminal, None if unknown."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                if run.state == QUEUED:
                    self._waiting.remove(run_id)
                    run.state = CANCELLED
                    return 200, run.view()
                if run.state == RUNNING:
                    run.cancel_requested = True
                    self._signal(run)
                    return 202, run.view()
                return 200, run.view()
        view = self.get(run_id)
        return None if view is None else (200, view)

    def redis_reachable(self) -> str | None:
        """None when Redis answers, else why not."""
        try:
            self.redis.ping()
        except redis.RedisError as exc:
            return f"cannot reach Redis: {exc}"
        return None


    def _loop(self) -> None:
        while not self._stop.wait(_POLL_SECONDS):
            try:
                with self._lock:
                    self._reap()
                    self._launch_waiting()
                    self._resignal()
            except Exception:
                # Keep watching: one failed poll (Docker or Redis briefly unreachable) must not
                # leave every run unwatched for good.
                traceback.print_exc(file=sys.stderr)

    def _stream_length(self, run_id: str) -> int:
        try:
            return int(self.redis.xlen(stream_key(run_id)))
        except redis.RedisError as exc:
            raise BrokerUnavailable(f"cannot reach Redis: {exc}") from exc

    def _stream_outcome(self, run_id: str) -> tuple[str, str | None] | None:
        """How the run ended, from its stream's ``run_end``; None if the stream has none.

        ``completed`` is finished and ``aborted`` cancelled; anything else — ``error``, and
        ``time_limit``, which the backend also treats as a failure — is failed, with
        ``run_end.error`` as the reason. Entries the Runner did not write (the backend's
        ``abort``, in whatever shape) are looked past: the backend may write one while a master is
        still running, during a Runner restart say, and the master's ``run_end`` lands after it.
        """
        try:
            entries = self.redis.xrevrange(stream_key(run_id), count=_TAIL_ENTRIES)
        except redis.RedisError as exc:
            raise BrokerUnavailable(f"cannot reach Redis: {exc}") from exc
        last = next((record for record in map(_runner_record, entries) if record is not None), None)
        if last is None or last.get("type") != "run_end":
            return None
        reason = last.get("termination_reason")
        if reason == "completed":
            return FINISHED, None
        if reason == "aborted":
            return CANCELLED, None
        return FAILED, last.get("error") or f"the run ended with termination_reason {reason!r}"

    def _capacity_for(self, run: Run) -> bool:
        running = [other for other in self._runs.values() if other.state == RUNNING]
        if len(running) >= self.settings.max_concurrent_runs:
            return False
        in_use = sum(other.num_islands + 1 for other in running)
        return in_use + run.num_islands + 1 <= self.settings.max_containers

    def _launch_waiting(self) -> None:
        while self._waiting:
            run = self._runs[self._waiting[0]]
            if not self._capacity_for(run):
                return
            self._waiting.popleft()
            try:
                run.container = self._create_master(run)
            except Exception as exc:
                run.state = FAILED
                run.error = f"could not start the run's master container: {exc}"
                continue
            run.state = RUNNING
            run.started_at = time.time()

    def _create_master(self, run: Run) -> Any:
        """Start the run's master container, on the service's own networks."""
        assert run.config is not None
        name = master_container_name(run.run_id)
        settings = self.settings
        options: dict[str, Any] = {
            "command": ["python", "-m", "orchestrator.master"],
            "name": name,
            "environment": {
                **settings.to_env(),
                RUN_CONFIG_ENV: run.config.model_dump_json(),
                MASTER_CONTAINER_NAME_ENV: name,
                "PYTHONUNBUFFERED": "1",
            },
            "labels": {
                "app": "archipelago",
                "component": "master",
                "archipelago.run_id": run.run_id,
                "archipelago.request": run.request,
                "archipelago.num_islands": str(run.num_islands),
            },
            "volumes": {DOCKER_SOCKET: {"bind": DOCKER_SOCKET, "mode": "rw"}},
        }
        own = self._own_container()
        networks = sorted(own.attrs["NetworkSettings"]["Networks"]) if own is not None else []
        if networks:
            options["network"] = networks[0]
        else:
            # The service runs on the host, so the master and its islands share the host's network.
            options["network_mode"] = "host"
        container = self.docker.containers.create(settings.master_image, **options)
        try:
            for network in networks[1:]:
                self.docker.networks.get(network).connect(container)
            container.start()
        except Exception:
            container.remove(force=True)
            raise
        return container

    def _own_container(self) -> Any | None:
        """The container the service runs in, or None when it runs on the host."""
        if not Path("/.dockerenv").exists():
            return None
        try:
            return self.docker.containers.get(socket.gethostname())
        except Exception:
            return None

    def _adopt(self) -> None:
        """Register every master container a previous service left behind."""
        containers = self.docker.containers.list(
            all=True, filters={"label": ["app=archipelago", "component=master"]}
        )
        for container in containers:
            labels = container.labels
            run_id = labels["archipelago.run_id"]
            self._runs[run_id] = Run(
                run_id,
                labels.get("archipelago.request", ""),
                int(labels.get("archipelago.num_islands", "6")),
                state=RUNNING,
                container=container,
                started_at=_started_at(container),
            )

    def _reap(self) -> None:
        """Settle every run whose master has exited; kill one that outlived the time limit."""
        import docker.errors

        backstop = (
            self.settings.run_time_limit_seconds
            + self.settings.bye_timeout_seconds
            + _BACKSTOP_MARGIN_SECONDS
        )
        for run in list(self._runs.values()):
            if run.state != RUNNING:
                continue
            try:
                run.container.reload()
                running = bool(run.container.attrs.get("State", {}).get("Running"))
            except docker.errors.NotFound:
                running = False
            if running:
                if time.time() - (run.started_at or time.time()) > backstop:
                    try:
                        run.container.kill()
                    except Exception:
                        pass  # it exited in the meantime
                    run.state = FAILED
                    run.error = f"the run's master did not stop within {backstop:g} s; killed"
                    self._remove_containers(run.run_id)
                continue
            try:
                outcome = self._stream_outcome(run.run_id)
            except BrokerUnavailable:
                continue  # read it on a later poll, once Redis answers again
            run.state, run.error = outcome or (FAILED, self._master_failure(run))
            self._remove_containers(run.run_id)

    def _master_failure(self, run: Run) -> str:
        """Why a master stopped without ending its stream: its exit code and last line."""
        try:
            code = run.container.attrs.get("State", {}).get("ExitCode")
            lines = run.container.logs(tail=20).decode("utf-8", errors="replace").strip().splitlines()
        except Exception:
            return "the run's master container disappeared before ending the run's stream"
        last = lines[-1].strip() if lines else ""
        reason = f"the run's master stopped without ending the run's stream (exit code {code})"
        return f"{reason}: {last}" if last else reason

    def _remove_containers(self, run_id: str) -> None:
        """Remove the run's master and anything of its run still left (normally nothing)."""
        for container in self.docker.containers.list(
            all=True, filters={"label": f"archipelago.run_id={run_id}"}
        ):
            try:
                container.remove(force=True)
            except Exception:
                pass
        for network in self.docker.networks.list(names=[f"archipelago-{run_id[-8:]}"]):
            try:
                network.remove()
            except Exception:
                pass

    def _resignal(self) -> None:
        """Repeat SIGTERM to cancelled masters: one sent before a master installs its handler is lost."""
        for run in self._runs.values():
            if run.state == RUNNING and run.cancel_requested:
                if time.time() - run.last_signal >= _RESIGNAL_SECONDS:
                    self._signal(run)

    def _signal(self, run: Run) -> None:
        run.last_signal = time.time()
        try:
            run.container.kill(signal="SIGTERM")
        except Exception:
            pass  # it has exited already; the next poll settles it
