# Running the Runner

The Runner is an HTTP service ([`API.md`](API.md)). Each run gets a master container and one
container per island; every record goes to the Redis stream `run:<run id>:runner_logs`
([`LOGGING_FORMAT.md`](LOGGING_FORMAT.md)). Nothing is written to disk and nothing is downloaded:
the benchmark instances are committed (`pytest tests/test_datasets.py` checks them against
`benchmarks/data/MANIFEST.json`).

## The compose stack (development)

```bash
cp .env.example .env              # set REDIS_PASSWORD
docker compose up -d --build
docker compose logs -f dummy-worker
```

| Service | What it does |
| --- | --- |
| `redis` | Redis 7, password from `.env`, `noeviction`, no persistence, on `127.0.0.1:${REDIS_HOST_PORT}` (6380) |
| `runner` | The Runner service on `127.0.0.1:${RUNNER_HOST_PORT}` (8000), with the Docker socket mounted |
| `dummy-worker` | Stands in for a Worker Pool worker: prints every record of every `run:*:runner_logs`, then a per-run summary |
| `island` | Build-only: makes `up --build` build `archipelago-island`. Exits at once |

* **Stopping Redis deletes every stream** (no persistence): `stop`, `restart`, `down` and Ctrl+C on
  an attached `up` all empty it. Start detached; Ctrl+C on `logs -f` stops only the logs.
* The presets are baked into the image: after editing `config/`, `docker compose up -d --build`.
* Builds pass `--network=host`, for builders without the host's resolver.

**Production** is `docker-compose.prod.yml`: the Runner alone, against the backend's Redis
([`DEPLOYMENT.md`](DEPLOYMENT.md); README "Production" for where Redis can be, and ufw).

## Starting runs

[`scripts/dummy_backend.py`](../scripts/dummy_backend.py) stands in for the Archipelago Backend
(this repository's Python dependencies only):

```bash
python scripts/dummy_backend.py presets
python scripts/dummy_backend.py run docker_de_sphere
python scripts/dummy_backend.py run ga_tsp --set evaluation_budget=50000 --set migration.topology=star
python scripts/dummy_backend.py run de_rastrigin --set migration.enabled=false --set seed=7
python scripts/dummy_backend.py run full_dataset --no-wait
python scripts/dummy_backend.py status <run id>
python scripts/dummy_backend.py cancel <run id>
```

`run` mints a run id as the backend does, posts the run, polls every 2 s, and at the end checks the
stream with `validate_log`. It exits 0 only for a finished run with a valid stream:

```
POST /runs {"run_id": "run-20261031T114053Z-f1f4dbb3", "preset": "docker_de_sphere", "overrides": {}} -> 202 {... "state": "running", "error": null}
17:10:53  run-20261031T114053Z-f1f4dbb3  running
17:11:03  run-20261031T114053Z-f1f4dbb3  finished
stream valid: run:run-20261031T114053Z-f1f4dbb3:runner_logs: 20,372 records, 19,950 evaluations, islands [0, 1, 2, 3, 4]
```

It finds the Runner and Redis through `.env`, or `--runner-url` / `$RUNNER_URL` and `--redis-url` /
`$REDIS_URL`. Any HTTP client works too, with a hand-minted id:

```bash
curl -s -X POST localhost:8000/runs -H 'Content-Type: application/json' \
  -d '{"run_id": "run-20261031T120000Z-1a2b3c4d", "preset": "docker_de_sphere"}'
```

The Swagger UI at <http://127.0.0.1:8000/docs> sends real requests (its script comes from a CDN;
`/openapi.json` does not need one).

### With the real Archipelago Backend

The backend's own `compose.yaml` starts its API, workers, PostgreSQL and Redis. Put the Runner on
that compose network (uncomment the `backend` network in `docker-compose.prod.yml`, named
`<backend project>_default`; or put the same lines in an override file passed with a second `-f`),
then point each at the other:

```bash
# .env.prod:        REDIS_URL=redis://:<the backend's REDIS_PASSWORD>@redis:6379/0
# the backend's env: ARCHIPELAGO_RUNNER_URL=http://runner:8000
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

Runs then start through the backend's `POST /runs` (`{preset, overrides, stream, clustering}`), and
its `GET /runs/{id}/events` streams progress. The end-to-end test of 2026-11-01, and what it found,
is in `docs/HANDOFF.md` §9.

## Reading a run

```python
import pandas as pd
from orchestrator.redis_stream import read_stream, stream_key
from archipelago_logging import join_migration_edges, parse_records, validate_log

URL = "redis://:<password>@127.0.0.1:6380/0"
records = list(read_stream(URL, stream_key("<run id>")))
print(validate_log(records))

frame = pd.DataFrame([r for r in records if r["type"] == "evaluation"])
frame.groupby("island_id").fitness.min()               # best per island
frame.groupby("genome_hash").size().sort_values()      # STN node occupancy
frame[frame.operator == "migration_arrival"]           # migrants, with origin_island

undelivered = [e.migration_id for e in join_migration_edges(parse_records(records)) if not e.delivered]
ends = {r["island_id"]: r["termination_reason"] for r in records if r["type"] == "island_end"}
```

`run_start.config` holds the full resolved configuration (the settings under `_runtime`, password
redacted) and `run_start.datasets` the SHA-256 of every instance file.

## How a run ends

* **Normally**, each island stops (stagnation, its budget share, a target fitness, the generation
  cap), writes `island_end`, flushes, and says goodbye; when the archipelago's budget is spent the
  master's heartbeat reply stops the rest after their current generation. Then `run_end`
  (`completed`): `finished`.
* **Cancelled, at `RUN_TIME_LIMIT_SECONDS`, or the master fails:** the master asks every island to
  stop (heartbeat flag and SIGTERM); each finishes its generation and flushes; one still running
  after `BYE_TIMEOUT_SECONDS` is killed. `run_end` is `aborted` (`cancelled`), `time_limit`
  (`failed`) or `error` (`failed`). The service cancels by sending the master SIGTERM every 2 s.
* **An island dies:** the others carry on; the run is `failed`, and the island's error is in
  `run_end.per_island` and the closing diagnostic.
* **Redis unavailable or full** for `BROKER_STALL_SECONDS`: that writer fails, and the run with it.
  If it is the master's, the stream has no `run_end`; the run is `failed` and the backend's `abort`
  ends the stream.
* **The master stops before `run_start`** (a missing island image, say): no stream; `failed`, with
  the master's last line as the error.

No fault tolerance (no restarts, no liveness probes): out of scope. A hung island is ended by the
time limit, always on (an hour by default). As a backstop, the service kills a master still running
`RUN_TIME_LIMIT_SECONDS + BYE_TIMEOUT_SECONDS + 60 s` after it started.

**A Runner restart** keeps running runs: the new service finds their masters by label. Queued runs
are lost (404). [`API.md`](API.md), "Restarts".

## Experiment sweeps

```bash
for i in $(seq 1 30); do python scripts/dummy_backend.py run de_rastrigin; done           # 30 independent samples
for i in $(seq 1 30); do python scripts/dummy_backend.py run de_rastrigin --set seed=$i; done  # the same 30 every time
```

Without a seed every run draws fresh entropy. With one, the loop repeats exactly with migration off,
and with identical per-island random streams with it on. The problem is pinned either way
(`run_start.datasets`). Every arm of an override sweep is sized by the same budget; anything else
(an algorithm parameter, another instance) is a new preset in `config/`.

## The service on the host

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
docker build --network=host -f docker/Dockerfile.island -t archipelago-island:latest .
docker build --network=host -f docker/Dockerfile.runner -t archipelago-runner:latest .
docker compose up -d redis
REDIS_URL=redis://:<password>@127.0.0.1:6380/0 python -m service      # port 8000
```

Masters still run in containers from `archipelago-runner`, so rebuild both images after changing
code. Off Docker the service starts masters on the host's network, and they put their islands
there, so one Redis address works for all. Settings: [`CONFIGURATION.md`](CONFIGURATION.md).

## Docker, in detail

```bash
docker ps --filter label=app=archipelago                  # every master and island
docker logs archipelago-<last 8 hex>-master               # a run's master, while it runs
docker logs archipelago-<last 8 hex>-island-00            # one island
docker ps -a --filter label=archipelago.run_id=<run id>   # orphans
```

`node_id` in the records is the island's container name. The master removes its islands and the
service removes the master when the run ends; an island that exited without saying goodbye is in
the stream with its exit code and last 40 lines (`log_tail`).

| Container | Gets |
| --- | --- |
| Runner service | The Docker socket; the settings as environment variables |
| A run's master | The socket; the settings; the run's config in `RUN_CONFIG`; its name in `MASTER_CONTAINER_NAME`. Joins every network the service is on |
| An island | The settings; the run's config in `ISLAND_CONFIG`. Joins the run's network and every network the master is on. Nothing mounted, no files written; its result travels in its goodbye |

**Networks.** With the service in a container, each run gets a bridge network
`archipelago-<last 8 hex>` on which the master is `archipelago-master`; islands also join the
master's networks, so they reach Redis at the service's own `REDIS_URL`. With the service on the
host, masters and islands use the host's network. (Bridged islands reaching the host through
`host.docker.internal` would depend on the host's firewall; Ubuntu's ufw rejects it.)

The socket gives the service and the masters root on the host, and the Redis URL with its password
is in the containers' environment: both deliberate ([`DEPLOYMENT.md`](DEPLOYMENT.md), "Security").

## Debugging one island

The island image runs one island alone (no master, so no migration):

```bash
docker run --rm --network host \
  -e REDIS_URL=redis://:<password>@127.0.0.1:6380/0 \
  -e ISLAND_CONFIG="$(python -c 'from service.presets import load_presets; print(load_presets()["docker_de_sphere"].build("run-20261031T120000Z-0000dbg1", {}).model_dump_json())')" \
  archipelago-island:latest --run-id run-20261031T120000Z-0000dbg1 --island-id 0
```

Its records go to `run:run-20261031T120000Z-0000dbg1:runner_logs` without a `run_start`, so read
them with `read_stream` (`validate_log` would refuse them). It exits 1 if the island failed.

## Tests

```bash
pytest                                  # everything, ~6 min, needs Docker
pytest -m "not docker"                  # no daemon, ~15 s
pytest tests/test_service_requests.py   # the API up to starting a run
pytest tests/test_service_runs.py       # the API end to end: queue, cancel, restart, time limit
pytest tests/test_stream_contract.py    # whole runs: order, the run_end barrier, stalls, a full Redis
pytest tests/test_redis_stream.py       # the writer and reader, against Redis 7 and Valkey 8
```

The suite builds its own `archipelago-island:pytest` and `archipelago-runner:pytest` once per session
(so it never tests stale code or replaces deployed images) and starts throwaway Redis 7 and Valkey 8
containers on random ports. Without a daemon, Docker tests skip with the reason.

## Troubleshooting

| Message | Cause and fix |
| --- | --- |
| `set REDIS_PASSWORD in .env` | No `.env` or no password: `cp .env.example .env` |
| `Bind for 127.0.0.1:8000 failed: port is already allocated` | Set `RUNNER_HOST_PORT` / `REDIS_HOST_PORT` in `.env` |
| `GET /runs/<id>` 404 for a run that was running | It was still queued when the Runner restarted, or Redis restarted and took its stream |
| 409 `run:… already holds N entries, but this Runner has no record of the run` | The run id was used before; mint a new one |
| `island image 'archipelago-island:latest' not found` / `could not start the run's master container: … 404` | The Runner never builds images: `docker compose up -d --build` |
| `unknown algorithm_params for 'de': ['ff']` at startup | A preset names a parameter that does not exist; the service refuses to start until every preset validates |
| `TSPLIB instance file not found` | Instances are committed; restore it from git |
| 422 `migration.num_migrants=5 must be smaller than the per-island population (4)` | Budget too small for the migrants: raise `evaluation_budget`, lower `target_generations`, or send fewer |
| A run finishes at 40 % of its budget | The islands stagnated, the intended stop |
| `is not an orthogonal matrix … so it is not a rotation` | A bad CEC rotation file; see `benchmarks/data/README.md` |
| `failed` with every island failed | Its `error` says why each failed; a silent island has its own diagnostic with `log_tail` |
| `cannot reach Redis within 10 s` | Use the address the service's container reaches Redis at (masters and islands join its networks). A Redis installed on the host must listen on `172.17.0.1` and ufw must allow Docker's networks (README "ufw") |
| `no island ever connected to the master` | The control address is unreachable from the islands; with the service on the host, a firewall on loopback |
| `islands [N] did not say goodbye within 20 s of being asked to stop` | Slow generations or a hang; raise `BYE_TIMEOUT_SECONDS` |
| `no record reached Redis stream … for 120 s` | Redis unavailable or full (`OutOfMemoryError`) for `BROKER_STALL_SECONDS`; records are never dropped, so the run fails |
| Undelivered migrations in `validate_log`'s report | Expected: final-generation sends, and sends to islands that had stopped |
