# Archipelago — Baseline dEA Integration

The Runner for Archipelago, an explainability platform for island-model evolutionary algorithms.
It runs five literature-standard EAs (GA, DE, PSO, GP, ES) as an island model — a master and one
Docker container per island — and appends every evaluation, migration and summary record, live, to
the Redis stream `run:{id}:raw` that the Archipelago Backend's Worker Pool reads.

**Status:** mid-rewrite into an HTTP service for the Archipelago Backend. Start with
[`docs/HANDOFF.md`](docs/HANDOFF.md): what this is, how it works, every decision so far, current
status, and what is left.

## Quick start

Everything runs in Docker: Redis, the master, and one container per island. All you need is a
Docker daemon; the benchmark instances are committed to the repository, so nothing is downloaded.

```bash
cp .env.example .env              # set REDIS_PASSWORD, and RUN_CONFIGS to the configs to run
docker compose up --build
```

That starts Redis (password from `.env`, `noeviction`), a dummy worker that prints every record
as it lands, and the master, which runs each config in `RUN_CONFIGS` in turn and exits. The
master creates each run's islands itself, as sibling containers through the Docker socket.
Every record goes to the Redis stream `run:<run id>:raw`, and nothing is written to disk.

```bash
docker compose run --rm master run --config config/ga_tsp.yaml config/es_schwefel.yaml
docker compose run --rm master verify-log <run id>     # every record present, each island in order
docker compose logs -f dummy-worker                     # the records as the Worker Pool sees them
docker compose down
```

[`docs/RUNNING.md`](docs/RUNNING.md) covers the rest: running the master on the host, reading a
run, debugging one island, and troubleshooting.

## Documentation

| Document | Covers |
| --- | --- |
| `docs/HANDOFF.md` | Context, architecture, decisions, status, open questions, gotchas |
| `docs/RUNNING.md` | Running, reading and checking a run, Docker details, troubleshooting |
| `docs/CONFIGURATION.md` | Every configuration field |
| `docs/LOGGING_FORMAT.md` | The record contract: every record type and field, the stream entry format, invariants |
| `docs/ARCHITECTURE.md` | Why the code is shaped as it is |
| `docs/ALGORITHMS.md`, `docs/BENCHMARKS.md` | The five algorithms with citations; the benchmarks and instance files |
| `docs/MEMORY_MEASUREMENTS.md` | What runs cost in Redis memory, per preset and at the proposed budget cap |
| `docs/IMPLEMENTATION_PLAN.md` | The remaining work, module by module |

`docs/` (including the handoff) and `tests/` are gitignored, so they exist only in the project
owner's checkout.

## Coming next

The Runner becomes an HTTP service for the Archipelago Backend (`GET /presets`, `POST /runs`,
`GET /runs/{id}`, cancel) with a Run Queue of at most two runs, each run's master in its own
container; the CLI goes.
