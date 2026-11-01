"""The run's Redis stream, against real Redis 7 and Valkey 8.

The writer lands every record exactly once, in the order written: whether Redis is paused, out
of memory, or the connection drops on either side of the server. The reader checks each entry's
shape, and the reachability check fails fast.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid

import pytest

from archipelago_logging import LogValidationError, RecordKind, Sink
from orchestrator.redis_stream import (
    BrokerError,
    RedisStreamWriter,
    ping_broker,
    read_stream,
    stream_key,
    stream_length,
)

needs_broker = pytest.mark.docker


def _key() -> str:
    return stream_key(f"test-{uuid.uuid4().hex}")


def _line(seq: int, emitter: str = "island:0") -> str:
    record_type = "generation_end" if seq % 10 == 9 else "evaluation"
    return json.dumps({"type": record_type, "emitter": emitter, "seq": seq})


def _kind(line: str) -> RecordKind:
    return RecordKind.of(json.loads(line)["type"])


def _writer(url: str, key: str, **overrides) -> RedisStreamWriter:
    settings = {"buffer_records": 1000, "stall_seconds": 30.0, "socket_timeout": 5.0}
    settings.update(overrides)
    return RedisStreamWriter(url, key, **settings)


def _write(writer: RedisStreamWriter, lines: list[str]) -> None:
    for line in lines:
        writer.write(line, _kind(line))


def _entries(broker, key: str) -> list[dict[str, str]]:
    return [fields for _, fields in broker.client().xrange(key)]


def _data(broker, key: str) -> list[str]:
    return [fields["data"] for fields in _entries(broker, key)]


def _wait_for(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.02)


def _close(*sockets: socket.socket) -> None:
    for sock in sockets:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


class _Link:
    """One relayed connection, and how its next transaction is severed."""

    def __init__(self, client: socket.socket, upstream: socket.socket) -> None:
        self.client = client
        self.upstream = upstream
        self.sever: str | None = None


class SeveringProxy:
    """Relays TCP to Redis, and can sever the next transaction in one of three ways.

    ``lose_next_request``: the EXEC is dropped and the connection closed, so Redis never runs it.
    ``lose_next_reply``: Redis runs it, and the connection closes before the reply gets back.
    ``strand_next``: the transaction reaches Redis and the connection goes silent without closing
    on Redis's side, as in a network partition, so Redis holds it until the connection is killed.
    """

    _EXEC = b"*1\r\n$4\r\nEXEC\r\n"

    def __init__(self, upstream_port: int) -> None:
        self._upstream_port = upstream_port
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self.transactions_forwarded = 0
        self._sever: str | None = None
        self._links: list[_Link] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def lose_next_request(self) -> None:
        self._sever = "request"

    def lose_next_reply(self) -> None:
        self._sever = "reply"

    def strand_next(self) -> None:
        self._sever = "strand"

    def close(self) -> None:
        self._listener.close()
        for link in self._links:
            _close(link.client, link.upstream)

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            link = _Link(client, socket.create_connection(("127.0.0.1", self._upstream_port)))
            self._links.append(link)
            threading.Thread(target=self._requests, args=(link,), daemon=True).start()
            threading.Thread(target=self._replies, args=(link,), daemon=True).start()

    def _requests(self, link: _Link) -> None:
        tail = b""
        try:
            while chunk := link.client.recv(65536):
                window = tail + chunk
                tail = window[-(len(self._EXEC) - 1):]
                if self._EXEC in window:
                    sever, self._sever = self._sever, None
                    if sever == "request":
                        return  # Redis discards a transaction whose connection closes before EXEC
                    link.sever = sever
                    self.transactions_forwarded += 1
                link.upstream.sendall(chunk)
        except OSError:
            pass
        finally:
            _close(link.client)
            if link.sever != "strand":
                _close(link.upstream)

    def _replies(self, link: _Link) -> None:
        swallowed = False
        try:
            while True:
                link.upstream.settimeout(0.2 if swallowed and link.sever == "reply" else None)
                try:
                    chunk = link.upstream.recv(65536)
                except socket.timeout:
                    return  # Redis has replied in full, and the writer never hears it
                if not chunk:
                    return
                if link.sever is not None:
                    swallowed = True
                    continue
                link.client.sendall(chunk)
        except OSError:
            pass
        finally:
            _close(link.client, link.upstream)


@needs_broker
def test_records_land_in_order_with_their_kind(broker):
    key = _key()
    lines = [_line(seq) for seq in range(500)]
    writer = _writer(broker.url, key)
    _write(writer, lines)
    writer.close()

    entries = _entries(broker, key)
    assert [fields["data"] for fields in entries] == lines
    assert [fields["kind"] for fields in entries] == [_kind(line).value for line in lines]
    assert all(set(fields) == {"kind", "data"} for fields in entries)
    assert writer.stats.written == 500
    assert isinstance(writer, Sink)


@needs_broker
def test_a_full_batch_is_sent_without_waiting_for_its_timer(broker):
    key = _key()
    client = broker.client()
    writer = _writer(broker.url, key, batch_seconds=30)
    _write(writer, [_line(seq) for seq in range(450)])

    _wait_for(lambda: client.xlen(key) == 400, timeout=10)
    time.sleep(0.3)
    assert client.xlen(key) == 400, "the last 50 wait for their batch to fill or its time to run out"
    writer.flush()
    assert client.xlen(key) == 450
    writer.close()


@needs_broker
def test_a_partial_batch_is_sent_once_its_time_is_up(broker):
    key = _key()
    client = broker.client()
    writer = _writer(broker.url, key, batch_seconds=0.15)
    _write(writer, [_line(seq) for seq in range(5)])

    _wait_for(lambda: client.xlen(key) == 5, timeout=5)
    writer.close()


@needs_broker
def test_flush_returns_only_once_redis_has_acknowledged_everything(broker):
    key = _key()
    writer = _writer(broker.url, key, batch_seconds=30)
    _write(writer, [_line(seq) for seq in range(1050)])
    writer.flush()
    assert broker.client().xlen(key) == 1050
    writer.close()


@needs_broker
def test_writers_interleave_but_each_keeps_its_own_order(broker):
    """Islands share one stream; only each island's own order is promised."""
    key = _key()
    writers = {f"island:{island}": _writer(broker.url, key) for island in range(4)}

    def produce(emitter: str) -> None:
        _write(writers[emitter], [_line(seq, emitter) for seq in range(2000)])
        writers[emitter].close()

    threads = [threading.Thread(target=produce, args=(emitter,)) for emitter in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    order: dict[str, list[int]] = {}
    emitters = []
    for data in _data(broker, key):
        record = json.loads(data)
        order.setdefault(record["emitter"], []).append(record["seq"])
        emitters.append(record["emitter"])
    assert order == {emitter: list(range(2000)) for emitter in writers}
    switches = sum(1 for a, b in zip(emitters, emitters[1:]) if a != b)
    assert switches > len(writers), "the writers should have interleaved"


@needs_broker
def test_the_producer_waits_while_redis_is_paused_and_nothing_is_lost(broker):
    key = _key()
    client = broker.client()
    lines = [_line(seq) for seq in range(1000)]
    writer = _writer(broker.url, key, buffer_records=50, socket_timeout=10)
    handed_over = [0]

    def produce() -> None:
        for line in lines:
            writer.write(line, _kind(line))
            handed_over[0] += 1

    client.client_pause(1500, all=False)
    try:
        producer = threading.Thread(target=produce)
        producer.start()
        time.sleep(0.75)
        assert client.xlen(key) == 0
        assert handed_over[0] <= 50 + 200 + 1, "the producer must wait, not run ahead or drop"
    finally:
        producer.join(timeout=30)
        client.client_unpause()
    writer.close()

    assert _data(broker, key) == lines


@needs_broker
def test_a_batch_held_past_the_socket_timeout_lands_once(broker):
    """Each timeout leaves a batch queued behind the pause; none of those copies may run."""
    key = _key()
    client = broker.client()
    lines = [_line(seq) for seq in range(300)]
    writer = _writer(broker.url, key, socket_timeout=0.5)

    client.client_pause(2500, all=False)
    try:
        _write(writer, lines)
        writer.flush()
    finally:
        client.client_unpause()
    writer.close()

    assert _data(broker, key) == lines
    assert writer.stats.errors >= 1, "the pause should have timed the writer out at least once"


@needs_broker
def test_a_batch_whose_reply_is_lost_after_redis_ran_it_is_not_sent_again(broker):
    proxy = SeveringProxy(broker.port)
    key = _key()
    lines = [_line(seq) for seq in range(20)]
    writer = _writer(f"redis://127.0.0.1:{proxy.port}/0", key, batch_seconds=30)
    try:
        _write(writer, lines[:10])
        writer.flush()
        proxy.lose_next_reply()
        _write(writer, lines[10:])
        writer.flush()
        writer.close()
    finally:
        proxy.close()

    assert _data(broker, key) == lines
    assert proxy.transactions_forwarded == 2, "the writer should have found the batch, not resent it"
    assert writer.stats.errors >= 1


@needs_broker
def test_a_batch_lost_before_redis_received_it_is_sent_again(broker):
    proxy = SeveringProxy(broker.port)
    key = _key()
    lines = [_line(seq) for seq in range(20)]
    writer = _writer(f"redis://127.0.0.1:{proxy.port}/0", key, batch_seconds=30)
    try:
        _write(writer, lines[:10])
        writer.flush()
        proxy.lose_next_request()
        _write(writer, lines[10:])
        writer.flush()
        writer.close()
    finally:
        proxy.close()

    assert _data(broker, key) == lines
    assert proxy.transactions_forwarded == 2, "the dropped batch was never forwarded; its resend was"
    assert writer.stats.errors >= 1


@needs_broker
def test_a_batch_stranded_on_a_silent_connection_never_runs_late(broker):
    """Redis keeps a partitioned connection, and the batch it holds, until something kills it."""
    proxy = SeveringProxy(broker.port)
    key = _key()
    client = broker.client()
    lines = [_line(seq) for seq in range(20)]
    writer = _writer(f"redis://127.0.0.1:{proxy.port}/0", key, socket_timeout=0.5, batch_seconds=30)
    try:
        _write(writer, lines[:10])
        writer.flush()
        client.client_pause(2500, all=False)
        try:
            proxy.strand_next()
            _write(writer, lines[10:])
            writer.flush()
        finally:
            client.client_unpause()
        writer.close()
        time.sleep(0.5)  # long enough for a stranded copy, had it survived, to run
    finally:
        proxy.close()

    assert _data(broker, key) == lines
    assert writer.stats.errors >= 1


@needs_broker
def test_out_of_memory_is_retried_until_it_clears(broker):
    """Memory can come back, as finished runs' streams are deleted."""
    key = _key()
    client = broker.client()
    lines = [_line(seq) for seq in range(10)]
    client.config_set("maxmemory", 1)
    try:
        writer = _writer(broker.url, key)
        _write(writer, lines)
        time.sleep(1.0)
        assert client.xlen(key) == 0
        assert "OutOfMemoryError" in (writer.stats.last_error or "")
    finally:
        client.config_set("maxmemory", 0)
    writer.flush()
    writer.close()
    assert _data(broker, key) == lines


@needs_broker
def test_out_of_memory_that_outlasts_the_stall_threshold_fails_the_writer(broker):
    key = _key()
    client = broker.client()
    client.config_set("maxmemory", 1)
    try:
        writer = _writer(broker.url, key, stall_seconds=1)
        writer.write(_line(0), RecordKind.ROW)
        with pytest.raises(BrokerError, match=r"no record reached Redis stream .* for 1 s") as caught:
            writer.flush()
        assert "OutOfMemoryError" in str(caught.value)
        with pytest.raises(BrokerError):
            writer.write(_line(1), RecordKind.ROW)
    finally:
        client.config_set("maxmemory", 0)
    assert client.xlen(key) == 0


@needs_broker
def test_a_refusal_that_cannot_clear_fails_the_writer_at_once(broker):
    key = _key()
    broker.client().set(key, "not a stream")
    writer = _writer(broker.url, key)
    writer.write(_line(0), RecordKind.ROW)
    started = time.monotonic()
    with pytest.raises(BrokerError, match="WRONGTYPE"):
        writer.flush()
    assert time.monotonic() - started < 5, "a refusal that cannot clear must not wait for the stall"


def test_an_unreachable_broker_fails_the_writer_after_the_stall_threshold():
    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]
    writer = _writer(f"redis://127.0.0.1:{port}/0", _key(), stall_seconds=1, socket_timeout=0.5)
    writer.write(_line(0), RecordKind.ROW)
    with pytest.raises(BrokerError, match="no record reached Redis stream") as caught:
        writer.flush()
    assert "ConnectionError" in str(caught.value)


@needs_broker
def test_close_is_idempotent_and_the_writer_refuses_use_afterwards(broker):
    writer = _writer(broker.url, _key())
    writer.close()
    writer.close()
    with pytest.raises(RuntimeError, match="closed"):
        writer.write(_line(0), RecordKind.ROW)
    with pytest.raises(RuntimeError, match="closed"):
        writer.flush()


@needs_broker
def test_read_stream_yields_every_record_in_order(broker):
    key = _key()
    lines = [_line(seq) for seq in range(2500)]
    writer = _writer(broker.url, key)
    _write(writer, lines)
    writer.close()
    assert [json.dumps(record) for record in read_stream(broker.url, key)] == lines


@needs_broker
def test_read_stream_rejects_an_entry_filed_under_the_wrong_kind(broker):
    key = _key()
    broker.client().xadd(key, {"kind": "event", "data": _line(0)})
    with pytest.raises(LogValidationError, match="kind 'event' on a 'evaluation' record"):
        list(read_stream(broker.url, key))


@needs_broker
def test_read_stream_rejects_an_entry_with_other_fields(broker):
    key = _key()
    broker.client().xadd(key, {"kind": "row", "data": _line(0), "extra": "x"})
    with pytest.raises(LogValidationError, match="expected kind and data"):
        list(read_stream(broker.url, key))


@needs_broker
def test_stream_length_is_zero_for_a_stream_that_does_not_exist(broker):
    key = _key()
    assert stream_length(broker.url, key) == 0
    broker.client().xadd(key, {"kind": "row", "data": _line(0)})
    assert stream_length(broker.url, key) == 1


@needs_broker
def test_ping_broker_returns_when_redis_answers(broker):
    ping_broker(broker.url, timeout=5)


def test_ping_broker_fails_after_its_timeout_when_nothing_listens():
    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]
    started = time.monotonic()
    with pytest.raises(BrokerError, match="cannot reach Redis within 1 s"):
        ping_broker(f"redis://127.0.0.1:{port}/0", timeout=1)
    assert time.monotonic() - started < 5
