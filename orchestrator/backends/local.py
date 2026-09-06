"""Local backend: each island is a subprocess on this host."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import ExecutionBackend, WorkerSpec, WorkerStatus

__all__ = ["LocalBackend"]


class LocalBackend(ExecutionBackend):
    """Runs island workers as subprocesses of the master."""

    name = "local"

    def __init__(
        self,
        *,
        python_executable: str | None = None,
        repository_root: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        self.python = python_executable or sys.executable
        self.repository_root = Path(repository_root or Path(__file__).resolve().parents[2])
        self.environment = dict(environment or {})
        self._processes: dict[int, subprocess.Popen[bytes]] = {}
        self._log_handles: dict[int, Any] = {}
        self._node_ids: dict[int, str] = {}

    def prepare(self) -> None:
        """Nothing to build; the code is already on this filesystem."""

    def launch(self, spec: WorkerSpec) -> str:
        """Spawn one island as a subprocess."""
        command = [
            self.python,
            "-m",
            "orchestrator.worker",
            "--config",
            str(spec.config_path),
            "--run-id",
            spec.run_id,
            "--island-id",
            str(spec.island_id),
            "--output-dir",
            str(spec.output_dir),
            "--control-host",
            spec.control_host,
            "--control-port",
            str(spec.control_port),
        ]

        environment = dict(os.environ)
        environment.update(self.environment)
        environment.update(spec.environment)
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            f"{self.repository_root}{os.pathsep}{existing}" if existing else str(self.repository_root)
        )

        log_dir = spec.output_dir / "islands"
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = open(log_dir / f"island-{spec.island_id:02d}.stderr.log", "wb")

        process = subprocess.Popen(
            command,
            cwd=str(self.repository_root),
            env=environment,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        self._processes[spec.island_id] = process
        self._log_handles[spec.island_id] = handle
        node_id = f"local:{process.pid}"
        self._node_ids[spec.island_id] = node_id
        return node_id

    def statuses(self) -> dict[int, WorkerStatus]:
        """Poll each subprocess for liveness and exit code."""
        result: dict[int, WorkerStatus] = {}
        for island_id, process in self._processes.items():
            result[island_id] = WorkerStatus(
                island_id=island_id,
                node_id=self._node_ids.get(island_id, f"local:{process.pid}"),
                running=process.poll() is None,
            )
        return result

    def stop_all(self, grace_seconds: float = 15.0) -> None:
        """SIGTERM, wait, then SIGKILL anything still alive."""
        for process in self._processes.values():
            if process.poll() is None:
                process.terminate()
        for process in self._processes.values():
            if process.poll() is None:
                try:
                    process.wait(timeout=grace_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()

    def cleanup(self) -> None:
        """Close the captured stderr files."""
        for handle in self._log_handles.values():
            try:
                handle.close()
            except OSError:
                pass
        self._log_handles.clear()

    def describe(self) -> dict[str, Any]:
        """Backend facts recorded in run_start."""
        return {
            "backend": self.name,
            "python": self.python,
            "repository_root": str(self.repository_root),
        }
