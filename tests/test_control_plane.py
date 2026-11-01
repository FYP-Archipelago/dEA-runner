"""Master control server and island client, exercised over a real socket."""

from __future__ import annotations

import json
import time

import pytest

from archipelago_logging import CallbackSink, EventLogger
from orchestrator.control_client import ControlClient
from orchestrator.control_server import ControlServer
from orchestrator.topology import build_topology


@pytest.fixture
def server():
    master_records: list[str] = []
    logger = EventLogger("run-test", "master", CallbackSink(master_records.append))
    control = ControlServer(
        run_id="run-test",
        topology=build_topology("ring", 3),
        logger=logger,
        host="127.0.0.1",
        port=0,
    )
    host, port = control.start()
    control.test_master_records = master_records
    control.test_address = (host, port)
    yield control
    control.stop()


def _client(server, island_id: int) -> ControlClient:
    client = ControlClient(
        *server.test_address, run_id="run-test", island_id=island_id, connect_timeout=5.0
    )
    assert client.connect()
    return client


def test_clock_offset_probe_returns_a_bounded_estimate(server):
    client = _client(server, 0)
    offset = client.measure_offset(samples=3)
    assert offset.samples == 3
    assert offset.rtt_ns >= 0
    assert abs(offset.offset_ns) < max(offset.rtt_ns, 10_000_000)
    client.close()


def test_ready_marks_the_island_and_records_its_offset(server):
    client = _client(server, 1)
    assert client.ready(clock_offset_ns=1234)
    assert server.islands[1].ready
    assert server.islands[1].clock_offset_ns == 1234
    client.close()


def test_migration_is_routed_along_the_topology_only(server):
    """Islands never talk directly, so the topology is enforced in exactly one place."""
    source = _client(server, 0)
    source.ready(0)
    dispatched = source.migrate(
        migrants=[{"genome": [1.0], "fitness": 1.0, "individual_id": "i00-g000001-n000000000"}],
        individual_ids=["i00-g000001-n000000000"],
        fitnesses=[1.0],
        genome_hashes=["ab" * 16],
        source_generation=1,
        source_wall=time.time(),
    )
    assert [record["dest_island"] for record in dispatched] == [1]

    wrong_destination = _client(server, 2)
    assert wrong_destination.poll(generation=1) == []

    destination = _client(server, 1)
    deliveries = destination.poll(generation=4)
    assert len(deliveries) == 1
    assert deliveries[0]["source_island"] == 0
    assert deliveries[0]["source_generation"] == 1
    assert deliveries[0]["migration_id"] == dispatched[0]["migration_id"]

    for client in (source, destination, wrong_destination):
        client.close()


def test_polling_clears_the_queue(server):
    source = _client(server, 0)
    source.migrate(
        migrants=[{"genome": [1.0]}],
        individual_ids=["x"],
        fitnesses=[1.0],
        genome_hashes=["h"],
        source_generation=1,
        source_wall=time.time(),
    )
    destination = _client(server, 1)
    assert len(destination.poll(1)) == 1
    assert destination.poll(2) == [], "a delivered transfer must not be delivered twice"
    source.close()
    destination.close()


def test_polling_never_blocks_when_the_inbox_is_empty(server):
    """Asynchrony: an island carries on if nothing has arrived."""
    client = _client(server, 1)
    started = time.monotonic()
    assert client.poll(generation=1) == []
    assert time.monotonic() - started < 1.0
    client.close()


def test_migration_ids_are_unique_and_encode_the_direction(server):
    source = _client(server, 0)
    ids = []
    for generation in range(5):
        ids.extend(
            record["migration_id"]
            for record in source.migrate(
                migrants=[{"genome": [1.0]}],
                individual_ids=["x"],
                fitnesses=[1.0],
                genome_hashes=["h"],
                source_generation=generation,
                source_wall=time.time(),
            )
        )
    assert len(set(ids)) == 5
    assert all(identifier.startswith("mig-00-01-") for identifier in ids)
    source.close()


def test_fanout_one_random_sends_to_exactly_one_neighbour():
    logger = EventLogger("run-test", "master", CallbackSink([].append))
    control = ControlServer(
        run_id="run-test",
        topology=build_topology("fully_connected", 4),
        logger=logger,
        host="127.0.0.1",
        port=0,
        fanout="one_random",
    )
    control.start()
    try:
        assert len(control._destinations(0)) == 1
        assert len(build_topology("fully_connected", 4)[0]) == 3
    finally:
        control.stop()


def test_round_robin_fanout_cycles_through_neighbours():
    logger = EventLogger("run-test", "master", CallbackSink([].append))
    control = ControlServer(
        run_id="run-test",
        topology=build_topology("fully_connected", 4),
        logger=logger,
        host="127.0.0.1",
        port=0,
        fanout="round_robin",
    )
    control.start()
    try:
        picks = [control._destinations(0)[0] for _ in range(6)]
        assert picks == [1, 2, 3, 1, 2, 3]
    finally:
        control.stop()


def test_pending_queue_is_bounded_and_reports_drops():
    """One stalled island must not be able to exhaust the master's memory."""
    records: list[str] = []
    logger = EventLogger("run-test", "master", CallbackSink(records.append))
    control = ControlServer(
        run_id="run-test",
        topology=build_topology("ring", 2),
        logger=logger,
        host="127.0.0.1",
        port=0,
        max_pending_per_island=3,
    )
    host, port = control.start()
    try:
        client = ControlClient(host, port, run_id="run-test", island_id=0)
        assert client.connect()
        for generation in range(10):
            client.migrate(
                migrants=[{"genome": [1.0]}],
                individual_ids=["x"],
                fitnesses=[1.0],
                genome_hashes=["h"],
                source_generation=generation,
                source_wall=time.time(),
            )
        assert len(control._pending[1]) == 3
        assert control.islands[1].migrants_dropped > 0
        warnings = [
            json.loads(line)
            for line in records
            if json.loads(line)["type"] == "diagnostic"
        ]
        assert any("queue full" in record["message"] for record in warnings)
        client.close()
    finally:
        control.stop()


def test_heartbeat_carries_progress_and_the_stop_flag(server):
    client = _client(server, 0)
    client.ready(0)
    assert client.heartbeat(generation=5, evaluations=100, best_fitness=1.5) is False
    assert server.islands[0].generation == 5
    assert server.islands[0].evaluations == 100
    assert server.islands[0].best_fitness == 1.5

    server.stop_requested.set()
    assert client.heartbeat(generation=6, evaluations=120, best_fitness=1.4) is True
    client.close()


def test_an_island_that_vanishes_is_recorded_as_unfinished_and_nothing_else(server):
    """Fault handling is out of scope: a dropped island leaves no extra records."""
    client = _client(server, 0)
    client.ready(0)
    client.close()
    time.sleep(0.5)

    assert server.islands[0].connected is False
    assert server.islands[0].finished is False, (
        "an island that never said goodbye must not be counted as finished"
    )
    assert server.test_master_records == [], (
        "the master observes the disconnect but does not editorialise about it"
    )


def test_a_clean_goodbye_marks_the_island_finished(server):
    client = _client(server, 0)
    client.ready(0)
    client.bye(generation=10, evaluations=100, result={"island_id": 0, "error": None})
    client.close()
    time.sleep(0.5)

    assert server.islands[0].finished
    assert server.islands[0].generation == 10
    assert server.islands[0].evaluations == 100
    assert server.islands[0].result == {"island_id": 0, "error": None}


def test_client_degrades_instead_of_raising_when_the_master_is_gone():
    """A lost broker must cost migration, not the island's whole search."""
    client = ControlClient(
        "127.0.0.1", 1, run_id="run-test", island_id=0, connect_timeout=0.5, connect_retries=1
    )
    assert client.connect() is False
    assert client.poll(1) == []
    assert client.migrate(
        migrants=[], individual_ids=[], fitnesses=[], genome_hashes=[], source_generation=1, source_wall=0.0
    ) == []
    assert client.heartbeat(generation=1, evaluations=1, best_fitness=None) is False
    assert client.failed_calls > 0
    assert client.last_error is not None


def test_unknown_control_operations_are_answered_not_fatal(server):
    client = _client(server, 0)
    response = client.call("teleport")
    assert response is not None and response["ok"] is False
    assert "unknown control operation" in response["error"]
    assert client.heartbeat(generation=1, evaluations=1, best_fitness=None) is False
    client.close()


def test_summary_reports_every_island(server):
    summary = server.summary()
    assert set(summary) == {"0", "1", "2"}
    assert all("migrations_dispatched" in entry for entry in summary.values())
