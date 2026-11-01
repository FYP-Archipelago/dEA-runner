# Archipelago — Baseline dEA Integration

The Runner for Archipelago, an explainability platform for island-model evolutionary algorithms.
It is an HTTP service the Archipelago Backend calls to run five literature-standard EAs (GA, DE,
PSO, GP, ES) as an island model — a master container and one container per island for each run —
appending every evaluation, migration and summary record, live, to the Redis stream
`run:{run_id}:runner_logs` that the backend's Worker Pool reads.

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

## Production

[`docker-compose.prod.yml`](docker-compose.prod.yml) runs the Runner alone: no Redis (the
backend's is used) and no dummy worker. It also builds the island image, restarts the Runner unless
stopped, and caps its logs at 5 × 10 MB.

```bash
cp .env.prod.example .env.prod        # set REDIS_URL and RUNNER_BIND; .env.prod is gitignored
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
curl -s http://127.0.0.1:8000/health  # {"status":"ok"} once it reaches Redis
docker compose -f docker-compose.prod.yml --env-file .env.prod down
```

Other settings (`MAX_CONCURRENT_RUNS`, `RUN_TIME_LIMIT_SECONDS`, …) go in `.env.prod` too; see
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md). Don't run this and the development stack on one
Docker daemon: each Runner would adopt the other's runs.
