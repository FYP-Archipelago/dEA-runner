"""Append one emitter's log records to the run's Redis stream, in order, dropping none.

Each emitter (every island, and the master) has its own writer: one connection, one pump
thread. The producer hands records to a bounded buffer; the pump drains it in batches, each
closed at ``batch_records`` records or ``batch_seconds`` after its first record, and appends a
batch as one MULTI/EXEC transaction of XADDs. One buffer, one thread and one connection keep the
records in the order they were written. Each entry has two fields: ``kind`` (``row`` | ``event``)
and ``data``, the record's JSON. There is no MAXLEN: trimming the stream would be dropping.

**Nothing is dropped.** While the buffer is full the producer waits. A batch that fails is
retried with backoff, and when no batch has been acknowledged for ``stall_seconds`` the writer
gives up: the producer's next call raises :class:`BrokerError`.

**Nothing is written twice.** When a connection fails after a batch was sent, whether Redis
applied it is unknown: the reply may be the part that was lost. Resending blindly could
duplicate the batch. So before resending, the writer kills the old connection on the server,
which discards a batch still queued there (behind ``CLIENT PAUSE``, say) so that it can never run
later, and then searches the stream after its last acknowledged entry for the batch's first
record. A transaction applies all of its commands or none, so finding that record means the
whole batch landed.

redis-py's own retry is switched off: it resends a pipeline after a connection error without
knowing whether the first attempt was applied.
"""

from __future__ import annotations

import queue
import threading
import time

import redis
from redis.backoff import NoBackoff
from redis.exceptions import (
    AuthenticationError,
    AuthorizationError,
    OutOfMemoryError,
    ReadOnlyError,
    ResponseError,
    TryAgainError,
)
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

from archipelago_logging import RecordKind
from archipelago_logging.sinks import SinkStats

__all__ = ["BATCH_RECORDS", "BATCH_SECONDS", "BrokerError", "RedisStreamWriter", "stream_key"]

BATCH_RECORDS = 200
BATCH_SECONDS = 0.15

_POLL_SECONDS = 0.25
_FIRST_BACKOFF_SECONDS = 0.1
_MAX_BACKOFF_SECONDS = 2.0
_SEARCH_CHUNK = 1000

# Error replies to a refused transaction (so it did not run) whose cause can clear by itself.
# OOM, READONLY and TRYAGAIN arrive as their own exception classes.
_TRANSIENT_CODES = ("BUSY ", "MASTERDOWN ", "NOREPLICAS ")


def stream_key(run_id: str) -> str:
    """The run's log stream, as agreed with the backend."""
    return f"run:{run_id}:runner_logs"


class BrokerError(RuntimeError):
    """Records cannot be appended to Redis, so the run cannot continue without losing them."""


class _Retryable(Exception):
    """A failed attempt that a later one may get past."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _LostReply(_Retryable):
    """The batch was sent, but whether Redis applied it is unknown."""

    def __init__(self, cause: Exception, client_id: int) -> None:
        super().__init__(cause)
        self.client_id = client_id


class _Barrier:
    """Queued behind the records it covers; reached once Redis has acknowledged all of them."""

    def __init__(self, closing: bool) -> None:
        self.closing = closing
        self.reached = threading.Event()


class RedisStreamWriter:
    """Appends one emitter's records to a Redis stream, exactly once, in the order written."""

    def __init__(
        self,
        url: str,
        key: str,
        *,
        buffer_records: int,
        stall_seconds: float,
        socket_timeout: float = 5.0,
        batch_records: int = BATCH_RECORDS,
        batch_seconds: float = BATCH_SECONDS,
    ) -> None:
        self.key = key
        self.stats = SinkStats()
        self._stall_seconds = stall_seconds
        self._batch_records = batch_records
        self._batch_seconds = batch_seconds
        pool = redis.ConnectionPool.from_url(
            url,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_timeout,
            retry=Retry(NoBackoff(), 0),
            decode_responses=True,
        )
        self._connection = pool.make_connection()
        self._client_id: int | None = None
        self._last_id: str | None = None
        self._queue: queue.Queue[tuple[str, str] | _Barrier] = queue.Queue(maxsize=buffer_records)
        self._failure: BrokerError | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._pump, name="archipelago-redis-writer", daemon=True)
        self._thread.start()

    # -- producer side ------------------------------------------------------------------------

    def write(self, line: str, kind: RecordKind) -> None:
        """Hand one record to the pump, waiting while the buffer is full."""
        self._raise_if_closed()
        self._put((kind.value, line))

    def flush(self) -> None:
        """Return once Redis has acknowledged every record written so far."""
        self._raise_if_closed()
        self._await(_Barrier(closing=False))

    def close(self) -> None:
        """Flush, then release the connection. Calling it again does nothing."""
        if self._closed:
            return
        self._closed = True
        try:
            self._await(_Barrier(closing=True))
        finally:
            self._thread.join(timeout=self._stall_seconds)

    def _put(self, item: tuple[str, str] | _Barrier) -> None:
        while True:
            self._raise_if_failed()
            try:
                self._queue.put(item, timeout=_POLL_SECONDS)
                return
            except queue.Full:
                continue

    def _await(self, barrier: _Barrier) -> None:
        self._put(barrier)
        while not barrier.reached.wait(_POLL_SECONDS):
            self._raise_if_failed()

    def _raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError(f"the writer for {self.key} is closed")

    # -- pump side ----------------------------------------------------------------------------

    def _pump(self) -> None:
        try:
            while True:
                batch, barrier = self._next_batch()
                if batch and not self._deliver(batch):
                    return
                if barrier is not None:
                    barrier.reached.set()
                    if barrier.closing:
                        return
        except Exception as exc:
            self._fail(f"the writer for {self.key} stopped: {exc!r}", exc)
        finally:
            self._disconnect()

    def _next_batch(self) -> tuple[list[tuple[str, str]], _Barrier | None]:
        """Wait for a record, then gather until the batch is full, its time is up, or a barrier."""
        item = self._queue.get()
        if isinstance(item, _Barrier):
            return [], item
        batch = [item]
        closes_at = time.monotonic() + self._batch_seconds
        while len(batch) < self._batch_records:
            remaining = closes_at - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            if isinstance(item, _Barrier):
                return batch, item
            batch.append(item)
        return batch, None

    def _deliver(self, batch: list[tuple[str, str]]) -> bool:
        """Append one batch exactly once; False once the writer has failed."""
        gives_up_at = time.monotonic() + self._stall_seconds
        backoff = _FIRST_BACKOFF_SECONDS
        unresolved: int | None = None  # a connection that sent this batch and lost the reply
        while True:
            try:
                if unresolved is not None:
                    if self._landed(batch, unresolved):
                        return True
                    unresolved = None
                self._append(batch)
                return True
            except _LostReply as lost:
                unresolved = lost.client_id
                self._note(lost.cause)
            except _Retryable as retryable:
                self._note(retryable.cause)
            except BrokerError as error:
                self._failure = error
                return False
            now = time.monotonic()
            if now >= gives_up_at:
                undelivered = len(batch) + self._queue.qsize()
                self._fail(
                    f"no record reached Redis stream {self.key} for {self._stall_seconds:g} s, "
                    f"with about {undelivered} waiting; last error: {self.stats.last_error}"
                )
                return False
            time.sleep(min(backoff, gives_up_at - now))
            backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

    def _append(self, batch: list[tuple[str, str]]) -> None:
        """Send the batch as one transaction and read every reply."""
        self._connect()
        sent_on = self._client_id
        commands = [
            ("MULTI",),
            *(("XADD", self.key, "*", "kind", kind, "data", line) for kind, line in batch),
            ("EXEC",),
        ]
        try:
            self._connection.send_packed_command(self._connection.pack_commands(commands))
            replies = [self._reply() for _ in commands]
        except (AuthenticationError, AuthorizationError) as exc:
            raise BrokerError(f"Redis refused the runner's credentials: {exc}") from exc
        except (RedisConnectionError, RedisTimeoutError) as exc:
            self._disconnect()
            raise _LostReply(exc, sent_on) from exc

        outcome = replies[-1]
        if isinstance(outcome, ResponseError):
            # EXECABORT: a command was refused while being queued, so none of them ran.
            cause = next((reply for reply in replies if isinstance(reply, ResponseError)), outcome)
            raise self._classify(cause) from cause
        refused = [reply for reply in outcome if isinstance(reply, ResponseError)]
        if refused:
            raise BrokerError(f"Redis refused records for {self.key}: {refused[0]}") from refused[0]
        self._acknowledge(batch, outcome[-1])

    def _landed(self, batch: list[tuple[str, str]], client_id: int) -> bool:
        """Whether a batch whose reply was lost is in the stream; afterwards it can never arrive."""
        self._command("CLIENT", "KILL", "ID", client_id)  # 0 when that connection is already gone
        first, last = batch[0][1], batch[-1][1]
        start = f"({self._last_id}" if self._last_id is not None else "-"
        found_first = False
        while True:
            entries = self._command("XRANGE", self.key, start, "+", "COUNT", _SEARCH_CHUNK)
            for entry_id, fields in entries:
                data = dict(zip(fields[::2], fields[1::2])).get("data")
                found_first = found_first or data == first
                if found_first and data == last:
                    self._acknowledge(batch, entry_id)
                    return True
            if len(entries) < _SEARCH_CHUNK:
                if found_first:
                    raise BrokerError(
                        f"part of a batch is in {self.key}, which a transaction cannot leave"
                    )
                return False
            start = f"({entries[-1][0]}"

    def _command(self, *args: object) -> object:
        """Run one command, for the search after a lost reply."""
        self._connect()
        try:
            self._connection.send_command(*args)
            return self._connection.read_response()
        except (RedisConnectionError, RedisTimeoutError) as exc:
            self._disconnect()
            raise _Retryable(exc) from exc
        except ResponseError as exc:
            raise self._classify(exc) from exc

    def _connect(self) -> None:
        """Open the connection if it is closed, and learn its id on the server."""
        if self._client_id is not None:
            return
        try:
            self._connection.connect()
            self._connection.send_command("CLIENT", "ID")
            self._client_id = int(self._connection.read_response())
        except (AuthenticationError, AuthorizationError) as exc:
            raise BrokerError(f"Redis refused the runner's credentials: {exc}") from exc
        except (RedisConnectionError, RedisTimeoutError) as exc:
            self._disconnect()
            raise _Retryable(exc) from exc
        except ResponseError as exc:
            raise self._classify(exc) from exc

    def _disconnect(self) -> None:
        self._connection.disconnect()
        self._client_id = None

    def _reply(self) -> object:
        """One reply; an error reply is returned rather than raised, so the rest can be read."""
        try:
            return self._connection.read_response()
        except ResponseError as exc:
            return exc

    def _classify(self, exc: ResponseError) -> Exception:
        if isinstance(exc, (OutOfMemoryError, ReadOnlyError, TryAgainError)) or str(exc).startswith(
            _TRANSIENT_CODES
        ):
            return _Retryable(exc)
        return BrokerError(f"Redis refused records for {self.key}: {exc}")

    def _acknowledge(self, batch: list[tuple[str, str]], last_entry_id: str) -> None:
        self._last_id = last_entry_id
        self.stats.written += len(batch)

    def _note(self, exc: Exception) -> None:
        self.stats.errors += 1
        self.stats.last_error = f"{type(exc).__name__}: {exc}"

    def _fail(self, message: str, cause: Exception | None = None) -> None:
        failure = BrokerError(message)
        failure.__cause__ = cause
        self._failure = failure
