"""The Runner's HTTP API, as agreed with the Archipelago Backend.

``GET /presets``, ``POST /runs``, ``GET /runs/{run_id}``, ``POST /runs/{run_id}/cancel`` and
``GET /health``. Every response about a run is the run object ``{run_id, state, error}``, and
every error response is ``{"error": "<readable message>"}``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator

from archipelago_logging.ids import check_run_id
from orchestrator.settings import ServiceSettings

from .presets import OVERRIDES, PRESETS_DIR, InvalidRequest, canonical_request, load_presets
from .runs import BrokerUnavailable, Conflict, RunQueue

__all__ = ["create_app", "RunRequest"]


class RunRequest(BaseModel):
    """The body of ``POST /runs``."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    preset: str
    overrides: dict[str, Any] = {}

    @field_validator("run_id")
    @classmethod
    def _run_id_shape(cls, run_id: str) -> str:
        return check_run_id(run_id)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


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

    app = FastAPI(title="Archipelago Runner", lifespan=lifespan)
    app.state.queue = queue

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, _validation_message(exc))

    @app.get("/presets")
    def get_presets() -> dict[str, Any]:
        return {"overrides": OVERRIDES, "presets": [preset.describe() for preset in presets.values()]}

    @app.post("/runs", status_code=202)
    def post_run(body: RunRequest) -> Any:
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

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> Any:
        try:
            view = queue.get(run_id)
        except BrokerUnavailable as exc:
            return _error(503, str(exc))
        return view if view is not None else _error(404, f"unknown run {run_id!r}")

    @app.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> Any:
        try:
            outcome = queue.cancel(run_id)
        except BrokerUnavailable as exc:
            return _error(503, str(exc))
        if outcome is None:
            return _error(404, f"unknown run {run_id!r}")
        status, view = outcome
        return JSONResponse(status_code=status, content=view)

    @app.get("/health")
    def health() -> Any:
        problem = queue.redis_reachable()
        if problem is not None:
            return JSONResponse(status_code=503, content={"status": "unavailable", "error": problem})
        return {"status": "ok"}

    return app
