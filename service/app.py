"""The Runner's HTTP API, as agreed with the Archipelago Backend.

``GET /presets``, ``POST /runs``, ``GET /runs/{run_id}``, ``POST /runs/{run_id}/cancel`` and
``GET /health``. Every response about a run is the run object ``{run_id, state, error}``, and
every error response is ``{"error": "<readable message>"}``. The OpenAPI document is served at
``/openapi.json`` and the Swagger UI at ``/docs``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Path as PathParameter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from archipelago_logging.ids import check_run_id
from archipelago_logging.version import HARNESS_VERSION
from orchestrator.settings import ServiceSettings

from .presets import OVERRIDES, PRESETS_DIR, InvalidRequest, canonical_request, load_presets
from .runs import BrokerUnavailable, Conflict, RunQueue

__all__ = ["create_app", "RunRequest", "RunView", "ErrorBody"]

_EXAMPLE_RUN_ID = "run-20261031T120000Z-1a2b3c4d"

_DESCRIPTION = """
The Runner runs island-model evolutionary algorithms for the Archipelago Backend. Each run is a
master container and one container per island; every record it produces is appended to the
Redis stream `run:{run_id}:runner_logs`.

* Every response about a run is the **run object** `{run_id, state, error}`.
* Every error response, whatever the status, is `{"error": "<readable message>"}`.
* At most two runs execute at once; the rest wait, in arrival order, as `queued`.
* Poll `GET /runs/{run_id}` every 2 s; a terminal state (`finished`, `failed`, `cancelled`)
  never changes.
"""


class RunRequest(BaseModel):
    """The body of `POST /runs`: a preset, and at most the published overrides."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "run_id": _EXAMPLE_RUN_ID,
                    "preset": "docker_de_sphere",
                    "overrides": {"evaluation_budget": 5000, "seed": 42},
                }
            ]
        },
    )

    run_id: str = Field(
        description="Minted by the backend as run-YYYYMMDDTHHMMSSZ-xxxxxxxx (UTC timestamp, 8 "
        "lowercase hex digits) and used verbatim in every record, container and the stream key."
    )
    preset: str = Field(description="A preset name from GET /presets.")
    overrides: dict[str, Any] = Field(
        default={},
        description="Optional. Dotted keys from GET /presets `overrides`, each within its bounds and "
        "of the exact JSON type; a key left out takes the preset's default.",
    )

    @field_validator("run_id")
    @classmethod
    def _run_id_shape(cls, run_id: str) -> str:
        return check_run_id(run_id)


class RunView(BaseModel):
    """A run, as every endpoint about one answers."""

    run_id: str = Field(description="The run's id, as given to POST /runs.", examples=[_EXAMPLE_RUN_ID])
    state: Literal["queued", "running", "finished", "failed", "cancelled"] = Field(
        description="queued: waiting for a slot. running: its containers are up and its records "
        "are streaming. finished: ended normally. failed: ended with an error, or stopped by the "
        "Runner's time limit. cancelled: cancelled through POST /runs/{run_id}/cancel."
    )
    error: str | None = Field(
        description="Why the run failed, in words fit to show a user; null unless state is failed. "
        "The same text as the stream's run_end.error.",
        examples=[None],
    )


class ErrorBody(BaseModel):
    """Every error response, whatever its status."""

    error: str = Field(description="What went wrong, in words fit to show a user.")


class PresetView(BaseModel):
    """One preset, with its value for every override key."""

    preset: str = Field(description="The name to send as `preset` in POST /runs.", examples=["docker_de_sphere"])
    algorithm: str = Field(description="ga, de, pso, gp or es.", examples=["de"])
    benchmark: str = Field(description="The problem it solves.", examples=["sphere"])
    description: str = Field(description="What the preset runs, in a sentence.")
    defaults: dict[str, Any] = Field(
        description="The preset's value for every override key that has a default (all but seed).",
        examples=[{"num_islands": 5, "evaluation_budget": 20000, "target_generations": 60,
                   "migration.topology": "ring", "migration.interval": 5, "migration.num_migrants": 3}]
    )


class PresetsView(BaseModel):
    """The presets, and every key a request may override with its bounds."""

    overrides: dict[str, dict[str, Any]] = Field(
        description="Each override key's type (int or choice) and its bounds: min, max, choices; "
        "a bound with no limit is left out, and optional: true marks a key with no default.",
        examples=[OVERRIDES],
    )
    presets: list[PresetView] = Field(description="Every preset, in name order.")


class HealthView(BaseModel):
    """Whether the Runner can serve runs."""

    status: Literal["ok", "unavailable"] = Field(description="ok, or unavailable when Redis cannot be reached.")
    error: str | None = Field(default=None, description="Why not; present only when unavailable.")


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _documented(status: int, description: str, example: str) -> dict[int, dict]:
    """An error response for the OpenAPI document, with a real message as its example."""
    return {
        status: {
            "model": ErrorBody,
            "description": description,
            "content": {"application/json": {"example": {"error": example}}},
        }
    }


def _validation_message(exc: RequestValidationError) -> str:
    parts = []
    for item in exc.errors():
        if item["type"] == "json_invalid":
            return "the request body is not valid JSON"
        if tuple(item["loc"]) == ("body",):
            return "the request body must be a JSON object with run_id, preset and overrides"
        location = ".".join(str(part) for part in item["loc"] if part != "body")
        message = str(item["msg"]).removeprefix("Value error, ")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) or "the request body is not valid"


_RUN_ID_PATH = PathParameter(description="The run's id, as given to POST /runs.", examples=[_EXAMPLE_RUN_ID])
_REDIS_DOWN = _documented(
    503,
    "Redis is unreachable, so a run this Runner no longer remembers cannot be looked up in its "
    "stream. Retry; never treat it as failed.",
    "cannot reach Redis: Error 111 connecting to redis:6379. Connection refused.",
)
_UNKNOWN_RUN = _documented(404, "No such run; treat it as failed.", f"unknown run {_EXAMPLE_RUN_ID!r}")


def create_app(settings: ServiceSettings, *, presets_dir: Path = PRESETS_DIR) -> FastAPI:
    """The Runner service. Its Run Queue starts when the app starts and stops when it stops."""
    presets = load_presets(presets_dir)
    queue = RunQueue(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        queue.start()
        try:
            yield
        finally:
            queue.stop()

    app = FastAPI(
        title="Archipelago Runner",
        version=HARNESS_VERSION,
        description=_DESCRIPTION,
        lifespan=lifespan,
        redoc_url=None,
    )
    app.state.queue = queue

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, _validation_message(exc))

    @app.get(
        "/presets",
        response_model=PresetsView,
        tags=["presets"],
        summary="List the presets",
        responses={200: {"description": "The presets and the override bounds."}},
    )
    def get_presets() -> dict[str, Any]:
        """The presets the frontend can offer, each with its default for every override key, and
        the overrides a run may set with their bounds."""
        return {"overrides": OVERRIDES, "presets": [preset.describe() for preset in presets.values()]}

    @app.post(
        "/runs",
        status_code=202,
        response_model=RunView,
        tags=["runs"],
        summary="Start a run",
        responses={
            202: {"description": "Accepted: queued, or running if a slot was free. An exact repeat "
                  "of an earlier request with the same run_id returns that run's current state "
                  "and starts nothing."},
            **_documented(
                422,
                "The body is not valid JSON or not this shape; run_id is malformed; unknown preset; "
                "unknown override key; a value of the wrong type or out of bounds; a combination "
                "that cannot run (num_migrants not below the per-island population, interval above "
                "target_generations, a torus with fewer than 6 islands); or more containers than "
                "the Runner's limit.",
                "override 'evaluation_budget' must be at least 1,000, not 999",
            ),
            **_documented(
                409,
                "The run_id was already used with a different preset or overrides, or its stream "
                "already exists but the Runner has no record of the run (it restarted).",
                f"run_id {_EXAMPLE_RUN_ID!r} was already used with a different preset or overrides",
            ),
            **_documented(
                503,
                "Redis is unreachable. Nothing was started, so retrying with the same run_id is safe.",
                "cannot reach Redis: Error 111 connecting to redis:6379. Connection refused.; nothing "
                "was started, so retrying with the same run_id is safe",
            ),
        },
    )
    def post_run(body: RunRequest) -> Any:
        """Starts the run and answers at once; the run itself is asynchronous."""
        preset = presets.get(body.preset)
        if preset is None:
            return _error(422, f"unknown preset {body.preset!r}; known presets: {', '.join(presets)}")
        try:
            config = preset.build(body.run_id, body.overrides)
        except InvalidRequest as exc:
            return _error(422, str(exc))
        needed = config.num_islands + 1
        if needed > settings.max_containers:
            return _error(
                422,
                f"the run needs {needed} containers (a master and {config.num_islands} islands), "
                f"more than the Runner's limit of {settings.max_containers}",
            )
        try:
            return queue.submit(body.run_id, canonical_request(body.preset, body.overrides), config)
        except Conflict as exc:
            return _error(409, str(exc))
        except BrokerUnavailable as exc:
            return _error(503, f"{exc}; nothing was started, so retrying with the same run_id is safe")

    @app.get(
        "/runs/{run_id}",
        response_model=RunView,
        tags=["runs"],
        summary="Get a run's state",
        responses={200: {"description": "The run."}, **_UNKNOWN_RUN, **_REDIS_DOWN},
    )
    def get_run(run_id: str = _RUN_ID_PATH) -> Any:
        """Poll every 2 s. A run this Runner no longer remembers (it restarted) is answered from its
        stream's run_end."""
        try:
            view = queue.get(run_id)
        except BrokerUnavailable as exc:
            return _error(503, str(exc))
        return view if view is not None else _error(404, f"unknown run {run_id!r}")

    @app.post(
        "/runs/{run_id}/cancel",
        response_model=RunView,
        tags=["runs"],
        summary="Cancel a run",
        responses={
            200: {"description": "The run is now terminal: a queued run is cancelled at once and "
                  "never writes a stream; a run already finished, failed or cancelled is left as it "
                  "is."},
            202: {"model": RunView, "description": "The run was running and is winding down: each "
                  "island finishes its current generation, then the stream ends with run_end "
                  "(termination_reason aborted). state stays running until then, then becomes "
                  "cancelled."},
            **_documented(404, "No such run.", f"unknown run {_EXAMPLE_RUN_ID!r}"),
            **_REDIS_DOWN,
        },
    )
    def cancel_run(run_id: str = _RUN_ID_PATH) -> Any:
        """No body. Idempotent: repeating it changes nothing."""
        try:
            outcome = queue.cancel(run_id)
        except BrokerUnavailable as exc:
            return _error(503, str(exc))
        if outcome is None:
            return _error(404, f"unknown run {run_id!r}")
        status, view = outcome
        return JSONResponse(status_code=status, content=view)

    @app.get(
        "/health",
        response_model=HealthView,
        response_model_exclude_none=True,
        tags=["health"],
        summary="Check the Runner can serve runs",
        responses={
            200: {"description": "The Runner can reach Redis: `{\"status\": \"ok\"}`."},
            503: {"model": HealthView, "description": "Redis is unreachable."},
        },
    )
    def health() -> Any:
        """For the container healthcheck: 200 when the Runner is up and can reach Redis."""
        problem = queue.redis_reachable()
        if problem is not None:
            return JSONResponse(status_code=503, content={"status": "unavailable", "error": problem})
        return {"status": "ok"}

    def openapi() -> dict[str, Any]:
        """The OpenAPI document, without FastAPI's automatic 422 for path parameters.

        A run id in the path is a plain string and never fails validation, and every error body
        is ``ErrorBody``: the automatic response would document neither truthfully.
        """
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title, version=app.version, description=app.description, routes=app.routes
            )
            for operations in schema["paths"].values():
                for operation in operations.values():
                    automatic = operation["responses"].get("422", {})
                    if "HTTPValidationError" in str(automatic):
                        del operation["responses"]["422"]
            for unused in ("HTTPValidationError", "ValidationError"):
                schema["components"]["schemas"].pop(unused, None)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi
    return app
