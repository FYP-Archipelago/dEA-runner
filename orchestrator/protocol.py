"""Wire protocol between the master and its islands."""

from __future__ import annotations

import json
import socket
from typing import Any, Iterator

__all__ = [
    "PROTOCOL_VERSION",
    "Op",
    "encode",
    "send_message",
    "recv_message",
    "iter_messages",
    "ProtocolError",
]

PROTOCOL_VERSION = "1.0"


class ProtocolError(RuntimeError):
    """Raised on a malformed or truncated control message."""


class Op:
    """Control operations, in the ``op`` field of a control request."""

    TIME = "time"
    READY = "ready"
    MIGRATE = "migrate"
    POLL = "poll"
    HEARTBEAT = "heartbeat"
    BYE = "bye"


def encode(message: dict[str, Any]) -> bytes:
    """Serialise one message to a newline-terminated UTF-8 frame."""
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def send_message(sock: socket.socket, message: dict[str, Any]) -> None:
    """Send one message. Raises :class:`OSError` if the peer has gone."""
    sock.sendall(encode(message))


def recv_message(reader: Any) -> dict[str, Any]:
    """Read exactly one message from a buffered file-like reader."""
    line = reader.readline()
    if not line:
        raise ProtocolError("connection closed by peer")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"malformed control message: {line[:200]!r}") from exc
    if not isinstance(message, dict):
        raise ProtocolError(f"control message must be a JSON object, got {type(message).__name__}")
    return message


def iter_messages(reader: Any) -> Iterator[dict[str, Any]]:
    """Yield messages until the peer closes the connection."""
    while True:
        try:
            yield recv_message(reader)
        except ProtocolError:
            return
