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

**`RUNNER_BIND` matters.** The Runner has no authentication, and ufw does not filter ports that
Docker publishes: whatever address the port is bound to is open to whoever can reach it. Bind to
`127.0.0.1` when the backend is on the same machine, or to a private address it reaches.

### Reaching Redis

Each run's master joins the Runner's networks and its islands join the master's, so wherever the
Runner reaches Redis, every run does:

| Redis is… | `REDIS_URL` | This host's firewall |
| --- | --- | --- |
| on another machine | `redis://:<password>@<that host>:6379/0` | Nothing to do here; that machine must accept this one on 6379 |
| a container of another compose project on this host | `redis://:<password>@<its service name>:6379/0`, after uncommenting the `backend` network in `docker-compose.prod.yml` | Nothing: the traffic stays inside Docker |
| a container publishing its port on this host (on `0.0.0.0` or `172.17.0.1`, not `127.0.0.1`) | `redis://:<password>@172.17.0.1:<port>/0` | Normally nothing: Docker forwards published ports itself |
| installed on this host, outside Docker | `redis://:<password>@172.17.0.1:6379/0`, with Redis listening there too (`bind 127.0.0.1 172.17.0.1` in `redis.conf`) | ufw blocks it: see below |

`172.17.0.1` is the host's address on Docker's default bridge, there whenever Docker runs.

### ufw: letting containers reach a service on this host (temporary)

ufw drops traffic from Docker's networks to services on the host. Tested on the owner's machine
(2026-10-31), a container reached the host only at `172.17.0.1`, which an earlier rule there
allows; every other host address was blocked, and a fresh server would block that one too. To let
the Runner's containers reach Redis on port 6379 of this host — every network Docker creates with
its default address pools lies in `172.16.0.0/12`:

```bash
sudo iptables -I ufw-user-input -s 172.16.0.0/12 -p tcp --dport 6379 -m comment --comment archipelago-temporary -j ACCEPT
```

This adds the rule to ufw's own chain without saving it: it is gone after a reboot or
`sudo ufw reload`, and `ufw status` does not list it. To check it is there:

```bash
sudo iptables -S ufw-user-input | grep archipelago-temporary
```

To revert it now (the same rule, with `-D` for `-I`):

```bash
sudo iptables -D ufw-user-input -s 172.16.0.0/12 -p tcp --dport 6379 -m comment --comment archipelago-temporary -j ACCEPT
```

Use Redis's port if it is not 6379. If `docker network inspect` ever shows a `192.168.x.x`
subnet (Docker moves there once 172.17–172.31 are taken), add the same rule for that subnet. To see
what ufw already allows: `sudo ufw status numbered`.

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
