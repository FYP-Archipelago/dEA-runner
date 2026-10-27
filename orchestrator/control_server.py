"""Master-side control server: the migration broker."""

from __future__ import annotations

import random
import socketserver
import threading
from dataclasses import dataclass
from typing import Any

from archipelago_logging import EventLogger, MigrationIdMinter, wall_now

from .protocol import Op, ProtocolError, iter_messages, recv_message, send_message
from .seeding import seed_for_broker
from .topology import Topology

__all__ = ["ControlServer", "IslandStatus", "PendingMigration"]


@dataclass
class PendingMigration:
    """One dispatched migration waiting for its destination island to collect it."""

    migration_id: str
    source_island: int
    dest_island: int
    source_generation: int
    dispatched_master_wall: float
    source_wall: float
    migrants: list[dict[str, Any]]
    individual_ids: list[str]
    fitnesses: list[float]
    genome_hashes: list[str]


@dataclass
class IslandStatus:
    """What the master knows about one island."""

    island_id: int
    connected: bool = False
    ready: bool = False
    finished: bool = False
    generation: int = 0
    evaluations: int = 0
    best_fitness: float | None = None
    node_id: str | None = None
    clock_offset_ns: int = 0
    migrations_dispatched: int = 0
    migrations_delivered: int = 0
    migrants_dropped: int = 0


class ControlServer:
    """The master's TCP endpoint. Start with :meth:`start`, stop with :meth:`stop`."""

    def __init__(
        self,
        *,
        run_id: str,
        topology: Topology,
        logger: EventLogger,
        host: str = "127.0.0.1",
        port: int = 0,
        max_pending_per_island: int = 1000,
        fanout: str = "all",
        evaluation_budget: int | None = None,
        seed: int | None = None,
    ) -> None:
        self.run_id = run_id
        self.topology = topology
        self.logger = logger
        self.max_pending = max_pending_per_island
        self.fanout = fanout
        self.evaluation_budget = evaluation_budget
        self._rngs = {
            island: random.Random(seed_for_broker(seed, island)) for island in topology
        }

        self._lock = threading.RLock()
        self._migration_ids = MigrationIdMinter()
        self._pending: dict[int, list[PendingMigration]] = {
            island: [] for island in range(len(topology))
        }
        self._round_robin: dict[int, int] = {island: 0 for island in topology}
        self.islands: dict[int, IslandStatus] = {
            island: IslandStatus(island_id=island) for island in topology
        }
        self.stop_requested = threading.Event()

        self._server: socketserver.ThreadingTCPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._host = host
        self._port = port

        self.total_migration_events = 0
        self.total_migrants = 0


    @property
    def address(self) -> tuple[str, int]:
        """The bound ``(host, port)``. Valid only after :meth:`start`."""
        if self._server is None:
            raise RuntimeError("control server has not been started")
        return self._server.server_address[0], self._server.server_address[1]

    def start(self) -> tuple[str, int]:
        """Bind and begin accepting connections."""
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            """Serves one island's control connection, introduced by its first line."""

            timeout = None
            disable_nagle_algorithm = True

            def handle(self) -> None:
                """Read the island's introduction, then serve its requests."""
                try:
                    hello = recv_message(self.rfile)
                except (ProtocolError, OSError):
                    return
                outer._handle_control(self, int(hello["island_id"]), hello)

        class Server(socketserver.ThreadingTCPServer):
            """Threaded TCP server, one thread per island connection."""

            allow_reuse_address = True
            daemon_threads = True

        self._server = Server((self._host, self._port), Handler)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, name="archipelago-control", daemon=True
        )
        self._server_thread.start()

        return self.address

    def stop(self, timeout: float = 10.0) -> None:
        """Stop accepting connections and join the server thread."""
        self._shutdown.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=timeout)


    def _handle_control(
        self, handler: socketserver.StreamRequestHandler, island_id: int, hello: dict[str, Any]
    ) -> None:
        """Serve one island's request/response control channel."""
        with self._lock:
            status = self.islands.setdefault(island_id, IslandStatus(island_id=island_id))
            status.connected = True
            status.node_id = hello.get("node_id")

        try:
            send_message(handler.request, {"ok": True, "t_wall": wall_now()})
            for message in iter_messages(handler.rfile):
                response = self._dispatch(island_id, message)
                send_message(handler.request, response)
        except (OSError, ProtocolError):
            pass
        finally:
            with self._lock:
                self.islands[island_id].connected = False

    def _dispatch(self, island_id: int, message: dict[str, Any]) -> dict[str, Any]:
        operation = message.get("op")

        if operation == Op.TIME:
            return {"ok": True, "t_wall": wall_now()}
        if operation == Op.READY:
            with self._lock:
                status = self.islands[island_id]
                status.ready = True
                status.clock_offset_ns = int(message.get("clock_offset_ns", 0))
            return {"ok": True}
        if operation == Op.MIGRATE:
            return self._handle_migrate(island_id, message)
        if operation == Op.POLL:
            return self._handle_poll(island_id, message)
        if operation == Op.HEARTBEAT:
            with self._lock:
                status = self.islands[island_id]
                status.generation = int(message.get("generation", status.generation))
                status.evaluations = int(message.get("evaluations", status.evaluations))
                if message.get("best_fitness") is not None:
                    status.best_fitness = float(message["best_fitness"])
                spent = sum(other.evaluations for other in self.islands.values())

            if self.evaluation_budget is not None and spent >= self.evaluation_budget:
                if not self.stop_requested.is_set():
                    self.stop_requested.set()
                    self.logger.diagnostic(
                        "archipelago evaluation budget reached; asking islands to wind down",
                        evaluation_budget=self.evaluation_budget,
                        evaluations_spent=spent,
                    )
            return {"ok": True, "stop": self.stop_requested.is_set()}
        if operation == Op.BYE:
            with self._lock:
                status = self.islands[island_id]
                status.finished = True
                status.generation = int(message.get("generation", status.generation))
                status.evaluations = int(message.get("evaluations", status.evaluations))
            return {"ok": True}
        return {"ok": False, "error": f"unknown control operation {operation!r}"}


    def _destinations(self, source_island: int) -> list[int]:
        """Resolve which neighbours receive this island's migrants."""
        neighbours = list(self.topology.get(source_island, []))
        if not neighbours:
            return []
        if self.fanout == "all":
            return neighbours
        with self._lock:
            if self.fanout == "round_robin":
                position = self._round_robin[source_island] % len(neighbours)
                self._round_robin[source_island] = position + 1
                return [neighbours[position]]
        with self._lock:
            return [self._rngs[source_island].choice(neighbours)]

    def _handle_migrate(self, source_island: int, message: dict[str, Any]) -> dict[str, Any]:
        migrants = message.get("migrants") or []
        if not migrants:
            return {"ok": True, "dispatched": []}

        source_generation = int(message.get("source_generation", 0))
        source_wall = float(message.get("source_wall", wall_now()))
        individual_ids = list(message.get("individual_ids") or [])
        fitnesses = [float(value) for value in (message.get("fitnesses") or [])]
        genome_hashes = list(message.get("genome_hashes") or [])

        dispatched: list[dict[str, Any]] = []
        now = wall_now()
        for destination in self._destinations(source_island):
            migration_id = self._migration_ids.mint(source_island, destination)
            pending = PendingMigration(
                migration_id=migration_id,
                source_island=source_island,
                dest_island=destination,
                source_generation=source_generation,
                dispatched_master_wall=now,
                source_wall=source_wall,
                migrants=migrants,
                individual_ids=individual_ids,
                fitnesses=fitnesses,
                genome_hashes=genome_hashes,
            )
            with self._lock:
                queue = self._pending.setdefault(destination, [])
                if len(queue) >= self.max_pending:
                    dropped = queue.pop(0)
                    self.islands[destination].migrants_dropped += len(dropped.migrants)
                    self.logger.diagnostic(
                        "migration queue full; dropped the oldest pending transfer",
                        level="warning",
                        island_id=destination,
                        dropped_migration_id=dropped.migration_id,
                        queue_limit=self.max_pending,
                    )
                queue.append(pending)
                self.islands[source_island].migrations_dispatched += 1
                self.total_migration_events += 1
                self.total_migrants += len(migrants)
            dispatched.append({"migration_id": migration_id, "dest_island": destination})

        return {"ok": True, "dispatched": dispatched}

    def _handle_poll(self, island_id: int, message: dict[str, Any]) -> dict[str, Any]:
        """Hand over everything queued for this island, and clear the queue."""
        with self._lock:
            queue = self._pending.get(island_id, [])
            self._pending[island_id] = []
            self.islands[island_id].migrations_delivered += len(queue)

        queue.sort(key=lambda item: (item.source_island, item.source_generation, item.migration_id))

        return {
            "ok": True,
            "deliveries": [
                {
                    "migration_id": item.migration_id,
                    "source_island": item.source_island,
                    "dest_island": item.dest_island,
                    "source_generation": item.source_generation,
                    "dispatched_master_wall": item.dispatched_master_wall,
                    "source_wall": item.source_wall,
                    "migrants": item.migrants,
                    "individual_ids": item.individual_ids,
                    "fitnesses": item.fitnesses,
                    "genome_hashes": item.genome_hashes,
                }
                for item in queue
            ],
        }


    def summary(self) -> dict[str, Any]:
        """Per-island status for ``run_end.per_island``."""
        with self._lock:
            return {
                str(island): {
                    "connected": status.connected,
                    "ready": status.ready,
                    "finished": status.finished,
                    "generation": status.generation,
                    "evaluations": status.evaluations,
                    "best_fitness": status.best_fitness,
                    "node_id": status.node_id,
                    "clock_offset_ns": status.clock_offset_ns,
                    "migrations_dispatched": status.migrations_dispatched,
                    "migrations_delivered": status.migrations_delivered,
                    "migrants_dropped": status.migrants_dropped,
                }
                for island, status in sorted(self.islands.items())
            }
