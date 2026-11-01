"""The Runner's HTTP API, up to the point where a run would start: presets, validation, errors.

Nothing here needs Docker: the app is driven without starting its Run Queue, and every request
is answered before a container would be created.
"""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from orchestrator.settings import ServiceSettings
from orchestrator.topology import TOPOLOGIES
from service import create_app
from service.presets import OVERRIDES, canonical_request, load_presets

RUN_ID = "run-20261031T120000Z-1a2b3c4d"
PRESETS = ["de_rastrigin", "docker_de_sphere", "es_schwefel", "full_dataset", "ga_knapsack",
           "ga_tsp", "gp_nguyen7", "gp_yacht", "pso_ackley"]


def _closed_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as probe:
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def client() -> TestClient:
    """The API with Redis unreachable, so a request that passes validation gets a 503."""
    return TestClient(create_app(ServiceSettings(redis_url=f"redis://127.0.0.1:{_closed_port()}/0")))


def _post(client: TestClient, overrides: dict | None = None, **body) -> tuple[int, dict]:
    response = client.post(
        "/runs", json={"run_id": RUN_ID, "preset": "docker_de_sphere", "overrides": overrides or {}, **body}
    )
    return response.status_code, response.json()


def test_presets_lists_every_preset_with_its_defaults(client):
    body = client.get("/presets").json()
    assert body["overrides"] == OVERRIDES
    assert [preset["preset"] for preset in body["presets"]] == PRESETS
    sphere = next(p for p in body["presets"] if p["preset"] == "docker_de_sphere")
    assert sphere == {
        "preset": "docker_de_sphere",
        "algorithm": "de",
        "benchmark": "sphere",
        "description": "DE on Sphere, 5 islands, one Docker container per island.",
        "defaults": {"num_islands": 5, "evaluation_budget": 20000, "target_generations": 60,
                     "migration.enabled": True, "migration.topology": "ring", "migration.interval": 5,
                     "migration.num_migrants": 3},
    }


def test_the_published_bounds_are_the_agreed_ones():
    assert OVERRIDES["num_islands"] == {"type": "int", "min": 4, "max": 6}
    assert OVERRIDES["evaluation_budget"] == {"type": "int", "min": 1000, "max": 250_000}
    assert OVERRIDES["target_generations"] == {"type": "int", "min": 2}
    assert OVERRIDES["migration.enabled"] == {"type": "bool"}
    assert OVERRIDES["migration.topology"] == {"type": "choice", "choices": sorted(TOPOLOGIES)}
    assert OVERRIDES["seed"] == {"type": "int", "min": 0, "max": 4_294_967_295, "optional": True}


def test_every_default_is_within_the_published_bounds():
    for preset in load_presets().values():
        for key, value in preset.defaults.items():
            rule = OVERRIDES[key]
            if rule["type"] == "choice":
                assert value in rule["choices"], (preset.name, key)
            elif rule["type"] == "bool":
                assert isinstance(value, bool), (preset.name, key)
            else:
                assert rule.get("min", value) <= value <= rule.get("max", value), (preset.name, key)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("{not json", "the request body is not valid JSON"),
        ("[1, 2]", "the request body must be a JSON object with run_id, preset and overrides"),
        ('{"preset": "docker_de_sphere"}', "run_id: Field required"),
        ('{"run_id": "my-run", "preset": "docker_de_sphere"}', "must look like run-YYYYMMDDTHHMMSSZ"),
        (f'{{"run_id": "{RUN_ID}", "preset": "docker_de_sphere", "extra": 1}}', "extra: Extra inputs"),
        (f'{{"run_id": "{RUN_ID}", "preset": "ga_tps"}}', "unknown preset 'ga_tps'; known presets: de_rastrigin"),
    ],
)
def test_a_malformed_request_is_refused_with_the_reason(client, body, message):
    response = client.post("/runs", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert message in response.json()["error"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"population_size": 50}, "unknown overrides ['population_size']"),
        ({"evaluation_budget": "5000"}, "'evaluation_budget' must be an integer, not '5000'"),
        ({"num_islands": True}, "'num_islands' must be an integer, not True"),
        ({"evaluation_budget": 999}, "'evaluation_budget' must be at least 1,000, not 999"),
        ({"evaluation_budget": 250_001}, "'evaluation_budget' must be at most 250,000, not 250,001"),
        ({"num_islands": 7}, "'num_islands' must be at most 6, not 7"),
        ({"num_islands": 3}, "'num_islands' must be at least 4, not 3"),
        ({"target_generations": 1}, "'target_generations' must be at least 2, not 1"),
        ({"seed": -1}, "'seed' must be at least 0, not -1"),
        ({"seed": 4_294_967_296}, "'seed' must be at most 4,294,967,295"),
        ({"migration.topology": "mesh"}, "'migration.topology' must be one of"),
        ({"migration.interval": 0}, "'migration.interval' must be at least 1, not 0"),
        ({"migration.num_migrants": 0}, "'migration.num_migrants' must be at least 1, not 0"),
        ({"migration.enabled": 0}, "'migration.enabled' must be true or false, not 0"),
        ({"migration.enabled": "false"}, "'migration.enabled' must be true or false, not 'false'"),
        ({"migration.enabled": None}, "'migration.enabled' must be true or false, not None"),
    ],
)
def test_an_override_out_of_bounds_is_refused(client, overrides, message):
    status, body = _post(client, overrides)
    assert status == 422
    assert message in body["error"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"migration.num_migrants": 100}, "must be smaller than the per-island population (66)"),
        ({"migration.interval": 61}, "exceeds target_generations=60"),
        ({"migration.topology": "torus"}, "a torus of 5 islands gives every island only two neighbours"),
    ],
)
def test_a_combination_that_cannot_run_is_refused(client, overrides, message):
    status, body = _post(client, overrides)
    assert status == 422
    assert message in body["error"]


def test_a_torus_with_six_islands_passes_validation(client):
    status, body = _post(client, {"migration.topology": "torus", "num_islands": 6})
    assert status == 503, body  # valid: it gets as far as Redis, which is unreachable here


def test_a_valid_request_without_redis_is_refused_and_safe_to_retry(client):
    status, body = _post(client, {"evaluation_budget": 5000, "seed": None})
    assert status == 503
    assert body["error"].startswith("cannot reach Redis")
    assert "retrying with the same run_id is safe" in body["error"]


def test_a_run_that_could_never_fit_the_container_limit_is_refused():
    settings = ServiceSettings(redis_url=f"redis://127.0.0.1:{_closed_port()}/0", max_containers=5)
    client = TestClient(create_app(settings))
    status, body = _post(client)
    assert status == 422
    assert body["error"] == (
        "the run needs 6 containers (a master and 5 islands), more than the Runner's limit of 5"
    )
    assert _post(client, {"num_islands": 4})[0] == 503  # 5 containers fit


def test_health_says_when_redis_is_unreachable(client):
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["error"].startswith("cannot reach Redis")


def test_a_status_or_cancel_without_redis_is_a_503_not_a_404(client):
    """A run this service never saw is looked up in Redis; a 404 would wrongly mean 'failed'."""
    assert client.get(f"/runs/{RUN_ID}").status_code == 503
    assert client.post(f"/runs/{RUN_ID}/cancel").status_code == 503


def test_repeats_are_compared_as_sent_with_key_order_ignored():
    first = canonical_request("ga_tsp", {"seed": 7, "evaluation_budget": 5000})
    assert first == canonical_request("ga_tsp", {"evaluation_budget": 5000, "seed": 7})
    assert first != canonical_request("ga_tsp", {"evaluation_budget": 5000})
    assert first != canonical_request("ga_knapsack", {"seed": 7, "evaluation_budget": 5000})


def test_overriding_the_topology_drops_the_presets_topology_parameters():
    """es_schwefel's star has a hub; a ring overriding it has no such parameter."""
    preset = load_presets()["es_schwefel"]
    assert preset.build(RUN_ID, {}).migration.topology_params == {"hub": 0}
    assert preset.build(RUN_ID, {"migration.topology": "ring"}).migration.topology_params == {}
    assert preset.build(RUN_ID, {"migration.topology": "star"}).migration.topology_params == {"hub": 0}


def test_an_override_reaches_the_runs_config():
    config = load_presets()["ga_tsp"].build(
        RUN_ID, {"evaluation_budget": 50_000, "migration.topology": "star", "seed": 42, "num_islands": 6}
    )
    assert (config.run_id, config.evaluation_budget, config.migration.topology, config.seed) == (
        RUN_ID, 50_000, "star", 42,
    )
    assert config.num_islands == 6
    assert config.benchmark == "tsp"  # everything else comes from the preset


def test_the_openapi_document_lists_exactly_the_contracts_responses(client):
    spec = client.get("/openapi.json").json()
    responses = {
        (method.upper(), path): sorted(operation["responses"])
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
    }
    assert responses == {
        ("GET", "/presets"): ["200"],
        ("POST", "/runs"): ["202", "409", "422", "503"],
        ("GET", "/runs/{run_id}"): ["200", "404", "503"],
        ("POST", "/runs/{run_id}/cancel"): ["200", "202", "404", "503"],
        ("GET", "/health"): ["200", "503"],
    }
    schemas = spec["components"]["schemas"]
    assert set(schemas) == {"ErrorBody", "HealthView", "PresetView", "PresetsView", "RunRequest", "RunView"}
    assert schemas["RunView"]["properties"]["state"]["enum"] == [
        "queued", "running", "finished", "failed", "cancelled",
    ]
    error_bodies = {
        response["content"]["application/json"]["schema"]["$ref"]
        for operations in spec["paths"].values()
        for operation in operations.values()
        for status, response in operation["responses"].items()
        if status in ("404", "409", "422") or (status == "503" and "health" not in str(operation["tags"]))
    }
    assert error_bodies == {"#/components/schemas/ErrorBody"}, "every error is {error}"


def test_the_swagger_ui_is_served_at_docs(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert "swagger-ui" in response.text and "/openapi.json" in response.text


def test_the_request_example_in_the_docs_is_a_valid_request():
    """What the Swagger UI's 'Try it out' sends must pass validation."""
    from service.app import RunRequest

    example = RunRequest.model_config["json_schema_extra"]["examples"][0]
    request = RunRequest(**example)
    load_presets()[request.preset].build(request.run_id, request.overrides)


def test_migration_can_be_switched_off_for_baseline_and_exactly_repeatable_runs():
    """With migration off a seeded run repeats record for record (LOGGING_FORMAT §12)."""
    preset = load_presets()["docker_de_sphere"]
    config = preset.build(RUN_ID, {"migration.enabled": False, "seed": 7})
    assert (config.migration.enabled, config.seed) == (False, 7)
    assert preset.build(RUN_ID, {}).migration.enabled is True
    # The migration rules only bind a run that migrates: interval and migrants go unchecked.
    off = preset.build(RUN_ID, {"migration.enabled": False, "migration.interval": 500})
    assert off.migration.interval == 500
