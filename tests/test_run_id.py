"""Run ids: the format the backend mints and the Runner accepts and never reuses."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from archipelago_logging import check_run_id, new_run_id
from orchestrator.config import RunConfig
from orchestrator.master import Master
from orchestrator.settings import ServiceSettings

SUPPLIED = "run-20261026T120000Z-0123abcd"


def test_every_minted_run_id_passes_the_check():
    for _ in range(200):
        run_id = new_run_id()
        assert check_run_id(run_id) == run_id


def test_a_supplied_run_id_is_used_as_is():
    assert RunConfig(run_id=SUPPLIED).run_id == SUPPLIED
    assert Master(RunConfig(run_id=SUPPLIED), ServiceSettings()).run_id == SUPPLIED


@pytest.mark.parametrize(
    "malformed",
    [
        "run-20261026T120000Z-0123ABCD",
        "run-20261026T120000Z-0123abc",
        "run-20261326T120000Z-0123abcd",
        "run-20261026T250000Z-0123abcd",
        "job-20261026T120000Z-0123abcd",
        "run-20261026T120000Z-0123abcd-extra",
        "run-20261026 120000Z-0123abcd",
        "",
    ],
)
def test_a_malformed_run_id_is_rejected(malformed):
    """Container names are built from the last 8 hex digits, so the shape is load-bearing."""
    with pytest.raises(ValidationError, match="must look like run-YYYYMMDDTHHMMSSZ"):
        RunConfig(run_id=malformed)


@pytest.mark.docker
def test_a_used_run_id_is_refused_before_anything_is_written(run_broker):
    """A second run in one stream would break run_start-first for the backend's workers."""
    from orchestrator.redis_stream import stream_key

    key = stream_key(SUPPLIED)
    client = run_broker.client()
    client.delete(key)
    client.xadd(key, {"kind": "event", "data": '{"the first run": true}'})
    config = RunConfig(run_id=SUPPLIED)

    with pytest.raises(FileExistsError, match=f"has already been used: {key} holds 1 entries"):
        Master(config, ServiceSettings(redis_url=run_broker.url)).run()
    assert client.xlen(key) == 1
    client.delete(key)
