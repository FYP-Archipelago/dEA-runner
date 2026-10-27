"""Island-side client for the master's control channel."""

from __future__ import annotations

import socket
import time
from typing import Any

from archipelago_logging import ClockOffset, measure_clock_offset

from .protocol import Op, ProtocolError, recv_message, send_message

__all__ = ["ControlClient"]


class ControlClient:
    """Request/response client for one island."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        run_id: str,
        island_id: int,
        node_id: str | None = None,
        connect_timeout: float = 30.0,
        request_timeout: float = 60.0,
        connect_retries: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.run_id = run_id
        self.island_id = island_id
        self.node_id = node_id
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.connect_retries = connect_retries

        self._socket: socket.socket | None = None
        self._reader: Any = None
        self.available = False
        self.failed_calls = 0
        self.last_error: str | None = None


    def connect(self) -> bool:
        """Connect and introduce this island, retrying while the master starts."""
        deadline = time.monotonic() + self.connect_timeout
        attempt = 0
        while time.monotonic() < deadline and attempt <= self.connect_retries:
            attempt += 1
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
                sock.settimeout(self.request_timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._socket = sock
                self._reader = sock.makefile("rb")
                send_message(
                    sock,
                    {
                        "run_id": self.run_id,
                        "island_id": self.island_id,
                        "node_id": self.node_id,
                    },
                )
                recv_message(self._reader)
                self.available = True
                return True
            except (OSError, ProtocolError) as exc:
                self.last_error = repr(exc)
                self._close_socket()
                time.sleep(min(0.1 * attempt, 1.0))
        self.available = False
        return False

    def _close_socket(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            except OSError:
                pass
            self._reader = None
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def close(self) -> None:
        """Close the control socket."""
        self.available = False
        self._close_socket()

    def __enter__(self) -> "ControlClient":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


    def call(self, operation: str, **payload: Any) -> dict[str, Any] | None:
        """Send one request and return its response, or ``None`` if the call failed."""
        if not self.available or self._socket is None:
            self.failed_calls += 1
            return None
        try:
            send_message(
                self._socket,
                {"op": operation, "island_id": self.island_id, "run_id": self.run_id, **payload},
            )
            return recv_message(self._reader)
        except (OSError, ProtocolError) as exc:
            self.last_error = repr(exc)
            self.failed_calls += 1
            self.available = False
            self._close_socket()
            return None


    def measure_offset(self, samples: int = 5) -> ClockOffset:
        """Estimate this island's clock offset against the master's clock."""
        def probe() -> float:
            """Ask the master for its wall clock."""
            response = self.call(Op.TIME)
            if response is None or "t_wall" not in response:
                raise ProtocolError("clock probe failed")
            return float(response["t_wall"])

        try:
            return measure_clock_offset(probe, samples=samples)
        except ProtocolError:
            return ClockOffset(offset_ns=0, rtt_ns=0, samples=0)

    def ready(self, clock_offset_ns: int) -> bool:
        """Tell the master this island is initialised."""
        return self.call(Op.READY, clock_offset_ns=clock_offset_ns) is not None

    def migrate(
        self,
        *,
        migrants: list[dict[str, Any]],
        individual_ids: list[str],
        fitnesses: list[float],
        genome_hashes: list[str],
        source_generation: int,
        source_wall: float,
    ) -> list[dict[str, Any]]:
        """Dispatch migrants; return ``[{migration_id, dest_island}, ...]``."""
        response = self.call(
            Op.MIGRATE,
            migrants=migrants,
            individual_ids=individual_ids,
            fitnesses=fitnesses,
            genome_hashes=genome_hashes,
            source_generation=source_generation,
            source_wall=source_wall,
        )
        if response is None:
            return []
        return list(response.get("dispatched") or [])

    def poll(self, generation: int) -> list[dict[str, Any]]:
        """Collect any migrants waiting for this island. Never blocks on the master."""
        response = self.call(Op.POLL, generation=generation)
        if response is None:
            return []
        return list(response.get("deliveries") or [])

    def heartbeat(
        self, *, generation: int, evaluations: int, best_fitness: float | None
    ) -> bool:
        """Report progress. Returns ``True`` if the master has asked islands to stop."""
        response = self.call(
            Op.HEARTBEAT,
            generation=generation,
            evaluations=evaluations,
            best_fitness=best_fitness,
        )
        return bool(response and response.get("stop"))

    def bye(self, *, generation: int, evaluations: int) -> None:
        """Tell the master this island finished cleanly."""
        self.call(Op.BYE, generation=generation, evaluations=evaluations)
