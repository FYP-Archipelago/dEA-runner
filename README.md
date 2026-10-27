# Archipelago — Baseline dEA Integration

The Runner for Archipelago, an explainability platform for island-model evolutionary algorithms.
It runs five literature-standard EAs (GA, DE, PSO, GP, ES) as an island model — a master and one
Docker container per island — and appends every evaluation, migration and summary record, live, to
the Redis stream `run:{id}:raw` that the Archipelago Backend's Worker Pool reads.

**Status:** mid-rewrite into an HTTP service for the Archipelago Backend. Start with
[`docs/HANDOFF.md`](docs/HANDOFF.md): what this is, how it works, every decision so far, current
status, and what is left.

## Quick start

Everything runs in Docker by default: the master in one container, each island in
its own, every record appended to a Redis stream. All you need is a Docker daemon —
the benchmark instances are committed to the repository, so nothing is downloaded.

```bash
docker build --network=host -f docker/Dockerfile.master -t archipelago-master:latest .
docker network create archipelago
docker run -d --name redis --network archipelago redis:7-alpine --maxmemory-policy noeviction

# The reference dataset run: 100,000 evaluations on a real TSPLIB instance
docker run --rm --network archipelago \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/runs:/archipelago/runs" \
  -e HOST_RUNS_DIR="$PWD/runs" \
  -e ARCHIPELAGO_BROKER__URL=redis://redis:6379/0 \
  archipelago-master:latest run --config config/full_dataset.yaml
```

The records land in the Redis stream `run:<run id>:raw`.

```bash
# check the run's stream: every record present, each island in order
docker run --rm --network archipelago -e ARCHIPELAGO_BROKER__URL=redis://redis:6379/0 \
  archipelago-master:latest verify-log <run id>
```

## Documentation

| Document | Covers |
| --- | --- |
| `docs/HANDOFF.md` | Context, architecture, decisions, status, open questions, gotchas |
| `docs/RUNNING.md` | Running, reading and checking a run, Docker details, troubleshooting |
| `docs/CONFIGURATION.md` | Every configuration field |
| `docs/LOGGING_FORMAT.md` | The record contract: every record type and field, the stream entry format, invariants |
| `docs/ARCHITECTURE.md` | Why the code is shaped as it is |
| `docs/ALGORITHMS.md`, `docs/BENCHMARKS.md` | The five algorithms with citations; the benchmarks and instance files |
| `docs/IMPLEMENTATION_PLAN.md` | The remaining work, module by module |

`docs/` (including the handoff) and `tests/` are gitignored, so they exist only in the project
owner's checkout.

## Coming next

- `docker compose up` replaces the quick start above (Redis, the master and a dummy worker that
  prints the stream, credentials in `.env`).
- No run directory: everything about a run will be in its stream.
- The Runner becomes an HTTP service for the Archipelago Backend (`GET /presets`, `POST /runs`,
  `GET /runs/{id}`, cancel) with a Run Queue of at most two runs, each run's master in its own
  container; the CLI goes.
