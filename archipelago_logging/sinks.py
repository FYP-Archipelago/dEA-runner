"""Where log lines go."""

from __future__ import annotations

import io
import os
import queue
import socket
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

__all__ = [
    "Sink",
    "SinkStats",
    "NullSink",
    "CallbackSink",
    "TextLineFileSink",
    "JsonlFileSink",
    "NetworkSink",
    "MultiSink",
]


@dataclass
class SinkStats:
    """Delivery accounting for one sink, reported in the run summary."""

    written: int = 0
    dropped: int = 0
    errors: int = 0
    last_error: str | None = None
    degraded: bool = False


class Sink(ABC):
    """Accepts serialised JSON Lines records."""

    def __init__(self) -> None:
        self.stats = SinkStats()

    @abstractmethod
    def write(self, line: str) -> None:
        """Accept one record. Must not raise; failures are recorded in ``stats``."""

    def flush(self) -> None:
        """Push any buffered data onward. May be a no-op."""

    def close(self) -> None:
        """Flush and release resources. Must be idempotent."""

    def __enter__(self) -> "Sink":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class NullSink(Sink):
    """Discards everything. Used to disable a stream without branching on ``None``."""

    def write(self, line: str) -> None:
        """Discard the line."""
        self.stats.written += 1


class CallbackSink(Sink):
    """Hands each line to a Python callable."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__()
        self._callback = callback

    def write(self, line: str) -> None:
        """Hand the line to the callback."""
        try:
            self._callback(line)
            self.stats.written += 1
        except Exception as exc:
            self.stats.errors += 1
            self.stats.last_error = repr(exc)


class TextLineFileSink(Sink):
    """Append-only, buffered, line-oriented text file."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        buffer_bytes: int = 1 << 20,
        flush_interval_seconds: float = 2.0,
    ) -> None:
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._buffer_bytes = buffer_bytes
        self._flush_interval = flush_interval_seconds
        self._buffer = io.StringIO()
        self._buffered = 0
        self._lock = threading.Lock()
        self._closed = False

        self._handle = open(self.path, "a", encoding="utf-8")

        self._timer: threading.Timer | None = None
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._closed or self._flush_interval <= 0:
            return
        self._timer = threading.Timer(self._flush_interval, self._timed_flush)
        self._timer.daemon = True
        self._timer.start()

    def _timed_flush(self) -> None:
        self.flush()
        self._schedule_flush()

    def write(self, line: str) -> None:
        """Append the line, flushing on the configured interval."""
        if self._closed:
            self.stats.dropped += 1
            return
        with self._lock:
            self._buffer.write(line)
            self._buffer.write("\n")
            self._buffered += len(line) + 1
            self.stats.written += 1
            should_flush = self._buffered >= self._buffer_bytes
        if should_flush:
            self.flush()

    def flush(self) -> None:
        """Flush buffered lines to disk."""
        with self._lock:
            if self._closed or self._buffered == 0:
                return
            payload = self._buffer.getvalue()
            self._buffer = io.StringIO()
            self._buffered = 0
        try:
            self._handle.write(payload)
            self._handle.flush()
        except Exception as exc:
            self.stats.errors += 1
            self.stats.last_error = repr(exc)
            self.stats.degraded = True

    def close(self) -> None:
        """Flush and close the file."""
        if self._closed:
            return
        self.flush()
        with self._lock:
            self._closed = True
        if self._timer is not None:
            self._timer.cancel()
        try:
            self._handle.close()
        except Exception:
            pass


class JsonlFileSink(TextLineFileSink):
    """Append-only JSON Lines file. One JSON object."""


class NetworkSink(Sink):
    """Streams lines to the master's log-collector socket."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        queue_size: int = 100_000,
        connect_timeout: float = 10.0,
        max_reconnect_attempts: int = 5,
        handshake: str | None = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._handshake = handshake
        self._connect_timeout = connect_timeout
        self._max_reconnect = max_reconnect_attempts
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=queue_size)
        self._socket: socket.socket | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._pump, name="archipelago-netsink", daemon=True)
        self._thread.start()


    def _connect(self) -> bool:
        try:
            sock = socket.create_connection((self._host, self._port), timeout=self._connect_timeout)
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self._handshake:
                sock.sendall(self._handshake.encode("utf-8"))
            self._socket = sock
            return True
        except OSError as exc:
            self.stats.errors += 1
            self.stats.last_error = repr(exc)
            self._socket = None
            return False

    def _pump(self) -> None:
        attempts = 0
        while attempts <= self._max_reconnect and not self._connect():
            attempts += 1
            if attempts > self._max_reconnect:
                self.stats.degraded = True
                self._drain_silently()
                return
            threading.Event().wait(min(2.0 ** attempts * 0.1, 2.0))

        stop = False
        while not stop:
            item = self._queue.get()
            if item is None:
                break
            batch = [item]
            while len(batch) < 1024:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop = True
                    break
                batch.append(nxt)
            self._send("".join(line + "\n" for line in batch))

        self._shutdown_socket()

    def _send(self, payload: str) -> None:
        if self._socket is None or self.stats.degraded:
            return
        try:
            self._socket.sendall(payload.encode("utf-8"))
        except OSError as exc:
            self.stats.errors += 1
            self.stats.last_error = repr(exc)
            self.stats.degraded = True
            self._shutdown_socket()

    def _shutdown_socket(self) -> None:
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def _drain_silently(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self.stats.dropped += 1


    def write(self, line: str) -> None:
        """Queue the line, dropping and counting it if the queue is full."""
        if self._closed:
            self.stats.dropped += 1
            return
        try:
            self._queue.put_nowait(line)
            self.stats.written += 1
        except queue.Full:
            self.stats.dropped += 1

    def close(self) -> None:
        """Drain the queue, then close the socket."""
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(None, timeout=5.0)
        except queue.Full:
            pass
        self._thread.join(timeout=15.0)


class MultiSink(Sink):
    """Fans one line out to several sinks. One sink failing never affects another."""

    def __init__(self, sinks: Iterable[Sink]) -> None:
        super().__init__()
        self.sinks: list[Sink] = list(sinks)

    def write(self, line: str) -> None:
        """Fan the line out to every component sink."""
        for sink in self.sinks:
            sink.write(line)
        self.stats.written += 1

    def flush(self) -> None:
        """Flush every component sink."""
        for sink in self.sinks:
            sink.flush()

    def close(self) -> None:
        """Close every component sink."""
        for sink in self.sinks:
            sink.close()

    def describe(self) -> dict[str, dict[str, object]]:
        """Per-sink delivery stats, for the run summary."""
        return {
            f"{type(sink).__name__}#{index}": {
                "written": sink.stats.written,
                "dropped": sink.stats.dropped,
                "errors": sink.stats.errors,
                "degraded": sink.stats.degraded,
                "last_error": sink.stats.last_error,
            }
            for index, sink in enumerate(self.sinks)
        }
