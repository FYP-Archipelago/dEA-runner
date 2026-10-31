# Archipelago — Baseline dEA Integration

The Runner for Archipelago, an explainability platform for island-model evolutionary algorithms.
It is an HTTP service the Archipelago Backend calls to run five literature-standard EAs (GA, DE,
PSO, GP, ES) as an island model — a master container and one container per island for each run —
appending every evaluation, migration and summary record, live, to the Redis stream
`run:{run_id}:runner_logs` that the backend's Worker Pool reads.

**Status:** the service is complete (M5); deployment and the final docs pass (M6) remain. Start
with [`docs/HANDOFF.md`](docs/HANDOFF.md): what this is, how it works, every decision so far,
current status, and what is left.

## Quick start

Everything runs in Docker: Redis, the Runner, and the containers each run starts. All you need
is a Docker daemon; the benchmark instances are committed to the repository, so nothing is
downloaded.

```bash
cp .env.example .env                  # set REDIS_PASSWORD
docker compose up -d --build
docker compose logs -f dummy-worker   # the records as the Worker Pool sees them
```

That starts Redis (password from `.env`, `noeviction`), the Runner on `127.0.0.1:8000`, and a
dummy worker that prints every record as it lands. Then start runs as the backend would. The
dummy backend mints a run id, starts the run, follows it to the end and checks its stream:

```bash
python scripts/dummy_backend.py presets
python scripts/dummy_backend.py run docker_de_sphere
python scripts/dummy_backend.py run full_dataset --set evaluation_budget=250000 --set seed=7
python scripts/dummy_backend.py cancel <run id>
docker compose down                   # removes Redis, and every stream with it
```

The API (all JSON; every error is `{"error": "<readable message>"}`):

| Endpoint | Does | Answers |
| --- | --- | --- |
| `GET /presets` | The presets and the overrides a run may set, with bounds | 200 |
| `POST /runs` `{run_id, preset, overrides}` | Starts a run (at most 2 at once, the rest queue) | 202 run · 422 · 409 · 503 |
| `GET /runs/{run_id}` | The run: `{run_id, state, error}`, state `queued` · `running` · `finished` · `failed` · `cancelled` | 200 · 404 · 503 |
| `POST /runs/{run_id}/cancel` | Cancels it; a running run winds down and its stream still ends with `run_end` | 202 · 200 · 404 · 503 |
| `GET /health` | Whether the Runner can reach Redis | 200 · 503 |

The Swagger UI at `http://127.0.0.1:8000/docs` documents every endpoint, status and body, and can
send requests; the OpenAPI document is `/openapi.json`.

**Stopping Redis deletes every stream.** It keeps everything in memory, so `docker compose
stop`, `restart` and `down`, and Ctrl+C on an attached `docker compose up`, all empty it.

[`docs/RUNNING.md`](docs/RUNNING.md) covers the rest: the API in detail, reading a run, running
the service on the host, debugging one island, and troubleshooting.

## Documentation

| Document | Covers |
| --- | --- |
| `docs/HANDOFF.md` | Context, architecture, decisions, status, open questions, gotchas |
| `docs/RUNNING.md` | The API, running and reading runs, Docker details, troubleshooting |
| `docs/CONFIGURATION.md` | Presets, overrides, and the Runner's settings |
| `docs/LOGGING_FORMAT.md` | The record contract: every record type and field, the stream entry format, invariants |
| `docs/ARCHITECTURE.md` | Why the code is shaped as it is |
| `docs/ALGORITHMS.md`, `docs/BENCHMARKS.md` | The five algorithms with citations; the benchmarks and instance files |
| `docs/MEMORY_MEASUREMENTS.md` | What runs cost in Redis memory, per preset and at the budget cap |
| `docs/IMPLEMENTATION_PLAN.md` | The remaining work, module by module |

`docs/` (including the handoff) and `tests/` are gitignored, so they exist only in the project
owner's checkout.

## Coming next

M6: deployment alongside the backend's Redis, and a final pass over the docs.
