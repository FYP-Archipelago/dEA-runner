# Handoff: the Archipelago baseline dEA Runner

**Written** 2026-10-27 by the project owner (GitHub `yendelevium`) and Claude (Claude Code);
**updated** after M5 (2026-10-31), M6 and the end-to-end test against the real backend
(2026-11-01). **Repository** `git@github.com:FYP-Archipelago/baseline-dEA.git`, checkout
`~/Desktop/fyp-baselineDEA`, branch `yb/streaming`, HEAD `950941b`; uncommitted: `schema_version` removed, the budget cap at 250,000 (§5.1). The
branch has no upstream set and `origin/yb/streaming` is at `c8068b1` (one commit behind); `main` is
further behind and no pull request exists.

> **Read this first**
>
> 1. **`docs/` (this file too), `tests/` and `CLAUDE.md` are gitignored.** A clone has no docs, no
>    tests and no plan: get them from the owner.
> 2. **Working agreement:** module by module (`docs/IMPLEMENTATION_PLAN.md`); after each, report,
>    propose a commit message and **stop until the owner has committed**. Claude never commits.
> 3. **`CLAUDE.md` is deprecated** (background only; it is still loaded into every Claude Code
>    session). Where it and `docs/` disagree, `docs/` wins.
> 4. **Names follow the platform architecture diagram** (§2.1), except the stream:
>    `run:{run_id}:runner_logs`.

| Document | What it is |
| --- | --- |
| **This file** | Context, decisions, status, the backend, gotchas: what the code does not say |
| `IMPLEMENTATION_PLAN.md` | Modules M1–M6 (done), what is left, **the list sent to the backend** |
| `ARCHITECTURE.md` | Why the code is shaped as it is |
| `LOGGING_FORMAT.md` | The record format; field tables and examples generated from the code |
| `API.md` | The API's behaviour, and a reference generated from the service |
| `DEPLOYMENT.md` | Deploying, operating, upgrading, stopping; security; go-live checklist |
| `RUNNING.md` | Running, reading runs, the real backend, troubleshooting |
| `CONFIGURATION.md` | Presets, overrides, every config field and setting |
| `ALGORITHMS.md`, `BENCHMARKS.md` | The five EAs with citations; the problems and instance files |
| `MEMORY_MEASUREMENTS.md` | Redis per preset and at the cap; the whole platform by component |
| `STREAMING_CONTRACT_REVIEW.md`, `RUNNER_REPLY.md` | Historical: our review of `REQS.md` and our reply. Not updated |
| Runner API Contract (Claude Doc) | The backend-facing contract: <https://claude.ai/artifact/1u35AQwrukjXHfDnUxeB7f> |
| `REQS.md` | The backend's requirements (`~/Downloads/`), verbatim in Appendix A |

---

## 1. Overview

**The research project, Archipelago.** A final-year project at Amrita Vishwa Vidyapeetham (Team 21,
Dept. of CSE, guide Dr. Ritwik M.), *"Enhancing Explainability for Distributed Evolutionary
Algorithms"*, aiming at a GECCO-quality paper. It explains **island-model dEAs** (sub-populations
evolving in parallel and exchanging individuals by migration): it turns the islands' search into
**Search Trajectory Networks** (directed graphs of clustered solutions), adds **migration edges**
between islands, compares islands with **MMD**, and shows device telemetry alongside.

**This repository, the Runner.** The data-generating substrate: an HTTP service the Archipelago
Backend calls to run five literature-standard EAs as an island model (a master container and one
container per island), streaming every record live to Redis. Everything downstream (clustering,
STNs, MMD, the dashboard) reads only those records; none of it lives here.

**Why.** Existing EA explainability tools assume one synchronous population; STNs use hierarchical
clustering (O(N²) memory) and, applied to a dEA, merge every island into one graph and hide the
migration dynamics. Archipelago clusters in a stream (LSH → BIRCH → DenStream, in the backend) and
makes migration edges first-class, which needs the Runner's records complete, ordered and precise.

| Who | Needs from this repo |
| --- | --- |
| The FYP team and guide | A correct, citable dEA whose records support STNs, migration edges and MMD |
| GECCO reviewers | Canonical operators; pinned datasets; a methodology that holds up |
| The backend team | The Runner service and the stream contract (§9) |
| Platform users | Indirectly: a preset and a few overrides in the Archipelago Dashboard |

**Goals, all met:** five EAs with distinct genomes, each operator cited (`ALGORITHMS.md`);
benchmarks from committed instance files with a checksum manifest (`BENCHMARKS.md`); one record
format defined once (`archipelago_logging/`), the only coupling with downstream; the API as agreed
with the backend; every record in `run:{run_id}:runner_logs` exactly once, in per-island order,
`run_start` first and `run_end` last; memory measured; two concurrent runs with queueing;
production compose and deployment guide; run end to end against the real backend.

**Out of scope:** clustering, STNs, migration-edge extraction, MMD, the telemetry dashboard and
VolPE integration (later phases / the backend; and no visualisation or clustering code in this
repo, even as an experiment, owner's rule); fault tolerance (a dead island is a missing
`island_end`); repair operators; intra-island multi-threading; non-island topologies; instance
generators; synchronous migration; backwards compatibility with older logs; a non-Docker backend;
hardware telemetry (`device_metrics`, an open question, §8).

---

## 2. How it works

### 2.1 The platform

The owner's architecture diagram, transcribed (the image is not in the repo; the stream is shown
under its real name, where the diagram says `run:{id}:raw`):

```
 Archipelago Dashboard ──POST /runs──► Archipelago Backend ◄──SSE── (to the dashboard)
                                       API (REST + SSE relay) · Auth · Persistor ──► PostgreSQL
                                         │ POST /runs, GET /runs/{id}      ▲ run:{id}:clustered
                                         ▼                                 │
                                       Runner (VolPE / Baseline dEA)      │
                                         │ Run Queue                       │
                                         ▼                                 │
                                       Docker containers (master + islands)
                                         │ XADD                            │
                                         ▼                                 │
   [Redis] pending_runs ──────────►  Worker Pool: workers running the Clustering Module
   (from the backend)                    ▲ reads [Redis] run:{id}:runner_logs
```

The dashboard talks only to the backend. The backend starts runs with the Runner's `POST /runs`,
polls `GET /runs/{id}` every 2 s, and hands each run to its Worker Pool through `pending_runs`;
workers read `runner_logs` and write `clustered`, which the backend saves to PostgreSQL and relays
over SSE. The Runner writes only its stream. The backend appends `abort` when it decides a run is
dead (the first terminator wins) and expires both streams 60 s after the run.

**Naming** (owner, 2026-10-27): the Runner, its Run Queue, the master and islands, the Archipelago
Backend, the Archipelago Dashboard, the Worker Pool. The stream is `runner_logs`, as `REQS.md` names
it (owner, 2026-10-31; the diagram's `raw` was used 10-27 → 10-31). Each run's master is its own
container, as drawn. **"Worker" always means a Worker Pool worker**; the island side says "island"
(`orchestrator/island.py`, `Dockerfile.island`, image `archipelago-island`). The box reads "VolPE /
Baseline dEA": the Runner is swappable, so the stream contract, not this code, is the interface.

### 2.2 The Runner

```
 Archipelago Backend ──HTTP──► Runner service (python -m service, port 8000)
                               │ GET /presets · POST /runs · GET /runs/{id} · cancel · /health · /docs
                               │ preset + overrides → RunConfig (422 with the reason)
                               │ Run Queue: ≤ MAX_CONCURRENT_RUNS, FIFO, ≤ MAX_CONTAINERS
                               │ per run: a master container (archipelago-runner, RUN_CONFIG)
                               │ outcome from the stream's run_end; cancel = SIGTERM every 2 s
                               │ restart: adopt running masters by label
                               ▼
            ┌──────────── master container (one per run) ────────────┐
            │ Redis answers? stream empty? island image there?        │
            │ ControlServer (TCP): the migration broker               │
            │ run_start (flushed) ─────────────────────────────┐      │
            │ launch islands (ISLAND_CONFIG)                    │      │
            │ supervise: exits, writer, cancel, time limit      │      │
            │ stop islands if needed: flag + SIGTERM, BYE wait  │      │
            │ results from BYE, else exit code + log tail       │      │
            │ [error diagnostic] run_end{error} ────────────────┤ XADD │
            └───────────────────────┬───────────────────────────┼──────┘
                                    │ sibling containers        ▼
             ┌──────────────────────▼─────────────┐   Redis: run:{id}:runner_logs
             │ island (orchestrator.island)        │        ▲
             │ control: clock offset, ready        │        │ XADD
             │ island_start; per generation:       │ ───────┘
             │   evaluations, migrate/poll,        │
             │   generation_end, heartbeat         │
             │ island_end → flush → BYE{result}    │
             └─────────────────────────────────────┘
```

Nothing is written to disk. `run_end` is last because the master writes it only after every
island's `BYE` (*all my records are in Redis*), exit or kill. The service's states come from
`run_end`: `completed` → finished, `aborted` → cancelled, otherwise failed with `run_end.error`.
The control plane is in `ARCHITECTURE.md` §4, the records in `LOGGING_FORMAT.md`.

**Networking** (`orchestrator/backends/docker_backend.py::_ensure_network`):

| Master runs… | Islands get… | Reach the master at | Reach Redis at |
| --- | --- | --- | --- |
| in a container (deployment) | a per-run bridge `archipelago-<last 8 hex>` plus every network the master is on | the alias `archipelago-master` | the service's `REDIS_URL` (e.g. `redis` on a compose network) |
| on the host (tests, development) | the host's network | `127.0.0.1:<port>` | the same URL as the master |

### 2.3 Stack

Python 3.12 (3.11+), NumPy 2.5, Pydantic 2.13, PyYAML 6, docker SDK 7.2, FastAPI 0.162 / uvicorn
0.54, redis-py 8.1, pytest 9.1 (httpx2 for FastAPI's test client), Redis 7 and Valkey 8 (tests run
against both), base image `python:3.12-slim`. Owner's machine: Ubuntu 24.04.5, kernel 7.0, Docker
Engine 29.8.1, Compose 5.5.1.

### 2.4 Key files

| Path | What |
| --- | --- |
| `archipelago_logging/events.py` | **The contract**: one Pydantic model per record type, enums, `parse_event`; unknown fields and non-finite numbers refused |
| `archipelago_logging/logger.py`, `sinks.py` | `EventLogger` (envelope, `seq`/`eval_index`, validation, hand-off); the sink interface |
| `archipelago_logging/reader.py` | `parse_records`, `validate_log`, `join_migration_edges` |
| `archipelago_logging/genome.py`, `ids.py`, `clock.py`, `version.py` | Genome codecs and the location hash; id minting and checks; clocks; `HARNESS_VERSION = "0.5.0"` |
| `algorithms/` | `ga`, `de`, `pso`, `gp` (+ `gp_tree`), `es`; `base.py` (`Algorithm`, `IslandContext`, `Individual`); `registry.py`; `diversity.py` |
| `benchmarks/` | Problem classes, `registry.py` (31 benchmarks), `data/` (instances + `MANIFEST.json`) |
| `orchestrator/config.py`, `settings.py` | `RunConfig` (a preset and its overrides); `ServiceSettings` (environment) |
| `orchestrator/master.py` | The run lifecycle, the shutdown barrier, cancel, `run_end.error`; `main()` (SIGTERM = cancel) |
| `orchestrator/island_runner.py`, `island.py` | The island loop and its container entrypoint |
| `orchestrator/redis_stream.py` | `RedisStreamWriter`, `read_stream`, `stream_key`, `BrokerError` |
| `orchestrator/control_server.py`, `control_client.py`, `protocol.py` | The control plane |
| `orchestrator/migration.py`, `topology.py`, `seeding.py` | Migrant policies; the 7 topologies; seed derivation |
| `orchestrator/backends/docker_backend.py` | Image check, networks, island launch, signals, exit reports, cleanup |
| `service/app.py`, `presets.py`, `runs.py` | The API and OpenAPI document; presets and `OVERRIDES`; the Run Queue |
| `config/*.yaml` | The 9 presets (`CONFIGURATION.md`) |
| `docker/`, `docker-compose.yml`, `docker-compose.prod.yml`, `.env*.example` | Images; the dev stack (Redis, Runner, dummy worker); production (the Runner alone) |
| `scripts/dummy_backend.py`, `dummy_worker.py` | Stand-ins for the backend and a Worker Pool worker |
| `scripts/measure_memory.py`, `generate_logging_docs.py`, `generate_api_docs.py` | Redis memory per run; the generated doc regions |
| `tests/` (gitignored) | 425 tests without Docker, 135 with |

### 2.5 Configuration

A run is a preset (`config/<name>.yaml`) plus at most the published overrides: `num_islands` 4–6,
`evaluation_budget` 1,000–250,000 (across the whole archipelago), `target_generations` ≥ 2,
`migration.enabled`, `migration.topology`, `migration.interval`, `migration.num_migrants`, `seed`
(optional); the backend supplies `run_id`. The deployment's `ServiceSettings` come from the
environment: `REDIS_URL`, images, `ISLAND_CPUS`/`MEMORY`, `MAX_CONCURRENT_RUNS` (2),
`MAX_CONTAINERS` (15), `BROKER_*`, `HEARTBEAT_SECONDS`, `BYE_TIMEOUT_SECONDS` (20),
`RUN_TIME_LIMIT_SECONDS` (3600, always on). `CONFIGURATION.md` has everything.

---

## 3. Context

### 3.1 History

| When | What |
| --- | --- |
| 2026-09-06 | Initial harness: algorithms, datasets, master–worker island runner, Docker, logger (`2e3c9e8` … `1a67fd7`) |
| Sept 2026 | Settled in earlier sessions: no fault tolerance; no instance generation; containerised master; LOGGING_FORMAT generated from the models |
| 2026-10-06 | A TCP live stream to an external receiver (`ef031e2`, the last commit on `main`) |
| Oct 2026 | A Redis publisher stub and run pool, then removed (M1) when the backend's requirements arrived. Measured: packing records into batch entries skewed a consumer group's load (27/26/47 %), so one record per entry |
| 2026-10-23 | The backend's `REQS.md` (Appendix A); our review and reply; their replies (Appendix B) |
| 2026-10-25 → 27 | The plan; M1–M4.3 |
| 2026-10-27 | `CLAUDE.md` deprecated; names per the diagram; M4.3b–M4.8 |
| 2026-10-30 | `verify-log`'s error; the Runner API Contract doc for the backend |
| 2026-10-31 | M5 (the service; stream renamed `runner_logs`); Swagger UI, `run_end.error`, the time limit always on |
| 2026-11-01 | Production compose and ufw guide; `API.md`, `DEPLOYMENT.md`, review (M6). The backend team ran the Runner ("chill") and asked for three changes; done. Owner: no versioning (fixed marker until the backend drops its check). End-to-end test against the real backend, the platform's memory by component, docs rewritten. Committed (`950941b`); the list sent to the backend |

### 3.2 Constraints

- **Docker-native** ("the entire system must be docker native"): the service and masters drive the
  host's Docker through the mounted socket; islands are sibling containers.
- **Correctness first** ("doesn't matter if it requires a re-write").
- **No dead code, no alternates**: infrastructure has one path. The EA itself (operators,
  topologies, policies) is the product and keeps its options.
- **Literature fidelity**: every operator cites its formulation.
- **Reproducibility**: the problem is pinned by file and SHA-256; search is reproducible only with a
  seed and without migration (D17).
- **Machine quirks**: §10.

### 3.3 Working assumptions

- Redis is reachable from wherever the Runner runs; where that is, is open (§8).
- The backend's Redis uses the default user, so `CLIENT ID` / `CLIENT KILL` are allowed.
- Two runs of at most six islands use at most 14 containers (each with its master), under the 15
  ceiling.
- The backend's Redis behaves like `redis:7-alpine` (it is one in their compose).

### 3.4 Glossary

| Term | Meaning |
| --- | --- |
| Island / master | One sub-population in its own container / the per-run container that starts islands, brokers migration, writes `run_start` and `run_end` |
| Runner / Run Queue | This service / its queue (at most two running) |
| Worker | **Always** a Worker Pool worker, never an island |
| Row / event | An `evaluation` record (`kind: row`) / any other record |
| `seq`, `eval_index` | Gap-free per-emitter record counter / per-island evaluation counter |
| Emitter | `master` or `island:<k>` |
| Terminator | `run_end` (Runner) or `abort` (backend); the first wins |
| Broker | Careful: Redis, or the master's migration-broker role |
| Stall | A writer that cannot get anything acknowledged; past `BROKER_STALL_SECONDS` the run fails |
| `genome_hash` | Hash of the rounded genome: the STN location signature |
| Compact / full | `genome_repr_mode`: packed base64 / plain JSON; best-so-far rows are always full |
| DooD | Docker-outside-of-Docker: a container driving the host's daemon via the socket |
| STN / MMD / LSH, BIRCH, DenStream | Search Trajectory Network / Maximum Mean Discrepancy / the backend's streaming clusterers |
| `pending_runs` | The backend's Redis key handing runs to its workers |
| EvOC, VolPE, EvoLabs | Other parts of the wider project; not integrated |

---

## 4. Decisions

Who: **Owner**, **Backend**, or **Claude** (proposed by the assistant; ⚠ = only reported at a
checkpoint, never discussed). All are done unless marked.

| # | Decision | Why (rejected alternative) | Who |
| --- | --- | --- | --- |
| D1 | Remove the TCP stream and the Redis stub | The backend contract replaced both | Owner |
| D2 | One record per stream entry | Consumer groups distribute per entry; packing skewed load (packing) | Claude; agreed |
| D3 | Islands write to Redis directly, one connection each | Per-island order by construction (a master relay) | Backend |
| D4 | Entry = `kind` + `data` (the record's JSON) | Smaller, no column-order contract (CSV-as-dict) | Claude; Backend agreed |
| D5 | No CSV; evaluations are JSON records | Two channels per island reordered records | Claude |
| D6 | Batches close at 200 records or 150 ms | The backend's numbers | Backend |
| D7 | Each batch is one MULTI/EXEC | "First record found ⇒ batch landed" (a plain pipeline) | Claude ⚠ |
| D8 | Never drop: block, fail after the stall window | A missing record corrupts the graph undetectably (drop and count) | Claude; Backend agreed |
| D9 | Never duplicate: kill the old connection, search before resending | A lost reply is ambiguous (blind resend; consumer dedupe) | Claude. Needs `CLIENT ID`/`KILL` |
| D10 | redis-py's retry off | It resends whole pipelines | Claude |
| D11 | No `MAXLEN`; Redis `noeviction` | Trimming is dropping | Agreed |
| D12 | The backend writes `abort`; first terminator wins | A crashed Runner cannot report itself | Backend |
| D13 | Shutdown barrier: `island_end` → flush → `BYE` → `run_end` | `run_end` really last | Agreed |
| D14 | Names use the run id's last 8 hex; ids must match the format | Negligible collisions (hashing the id) | Backend |
| D15 | A reused `run_id` (non-empty stream) is refused | "`run_start` first" | Claude |
| D16 | Optional `seed`; `SeedSequence` spawn keys; broker RNG per source island | Independent, arrival-order-free streams (`seed + id`) | Claude; Owner approved |
| D17 | With migration, a seeded run is not bit-reproducible; documented | Arrivals depend on timing (synchronous migration) | Owner. **The backend may believe otherwise** |
| D18 | A torus that is a ring (4–5 islands) is refused | It would be logged as a torus (their unreachable rule) | Claude; Owner |
| D19 | No `local` backend | Docker is the only deployment (Claude wanted it for tests) | **Owner overrode** |
| D20 | No dead code or unused options | Owner's rule | Owner |
| D21 | No `hashed` genomes, sampling, or rows switched off | The clustering module needs every genome | REQS |
| D22 | Every record validated, always | Faster than skipping (≈5 vs ≈15 µs); the only guard | Claude; REQS said "check with Adithya", no answer |
| D23 | No backwards compatibility for logs | "A full rewrite" | Owner |
| D24 | The fields the clustering module reads are required | The models enforce what the backend relies on | Claude |
| D25 | inf/NaN refused when written | JSON cannot carry them (write `null`) | Claude ⚠ |
| D26 | ~~Schema stays 2.0~~ → superseded by D67 | | |
| D27 | Redis code in `orchestrator/` | Keeps the contract package dependency-light | Claude ⚠ |
| D28 | Writer tests against real Redis 7 and Valkey 8 | A fake writer is an alternate implementation | Claude |
| D29 | Islands join every network of the master | One Redis URL for all (host networking; Redis on each run's network) | Claude ⚠ |
| D30 | Password redacted in records; real URL in container environments | The stream is read by others | Claude ⚠ |
| D31 | 2 runs, 15 containers; reject only what could never fit, queue the rest | The backend's rule | Backend |
| D32 | The HTTP contract as the backend specified, written up as the contract doc | — | Backend; doc by Claude |
| D33 | `POST /runs/{id}/cancel`, state `cancelled`; no cancel-all | Owner asked | Owner; **backend to agree** |
| D34 | Bounds: budget 1,000–250,000 (500,000 until 2026-11-01); seed ≤ 2³²−1 | 10 evolves nothing; JS loses precision above 2⁵³; the max is a memory bound, lowered by the owner after the platform's memory was measured (backend workers ~3.8 GB, Redis near 2 GB at 500,000) | Claude; Owner |
| D35 | M4.8 gate before M5 | — | Owner (passed 2026-10-30) |
| D36 | Checkpoint per module; the owner commits | — | Owner |
| D37, D48 | Compose stack with a dummy worker; several configs per CLI run | Owner asked (M4.3b); the CLI went in M5 | Owner |
| D38 | No fault tolerance; no generators; relative instance paths from the repo root; generated field tables | Earlier sessions | Owner |
| D39 | Names follow the diagram, except the stream (`runner_logs`) | Owner | Owner |
| D40 | Each run's master is its own container | The diagram; the backend's "max − 1 islands" rule (a service subprocess) | Owner |
| D41, D42 | `CLAUDE.md` deprecated; `docs/` gitignored | It misled sessions; owner's choice (Claude wanted docs committed) | Owner |
| D43 | Islands get their config in `ISLAND_CONFIG` and return results in `BYE` | No shared filesystem (a bind mount) | Claude ⚠ |
| D44 | An island that exits without `BYE` is explained by its exit code and last 40 lines | Its traceback would vanish | Claude ⚠ |
| D45 | Islands stopped by the heartbeat flag and a non-blocking SIGTERM that only sets a flag | Stops long generations; no logging in handlers (Docker `stop`) | Claude ⚠ |
| D46 | `run_start` flushed before any island starts | A broken Redis fails before containers exist | Claude ⚠ |
| D47 | Writer socket timeout `min(5 s, stall)` | A silent Redis fails within the window | Claude ⚠ |
| D49 | M4.8 with `scripts/measure_memory.py` | Set the cap | Claude |
| D50 | Deployment settings are `ServiceSettings` from the environment, passed down | REQS: no deployment knobs in runs (per-run sections; pydantic-settings) | Claude ⚠ |
| D51 | The Runner never builds images | A run's start must not depend on the builder | Claude ⚠ |
| D52 | One image for the service and every master | Same code | Claude ⚠ |
| D53 | The outcome comes from the stream's `run_end` alone | One source of truth, also after a restart | Claude |
| D54 | Cancel = SIGTERM to the master every 2 s | One sent before the handler is lost (Docker `stop` kills) | Claude |
| D55 | A restarted service adopts masters by label | Running runs survive; queued ones are lost (404) | Claude |
| D56 | 503 when Redis is unreachable (POST, GET, cancel, health) | A 404 would fail a healthy run | Claude; **backend to agree** |
| D57 | Overriding the topology drops the preset's `topology_params` | They belong to its topology | Claude ⚠ |
| D58 | Swagger UI at `/docs`, OpenAPI with every documented status | Owner asked | Owner |
| D59 | `run_end.error` for `error` and `time_limit` | Their workers read `run_end` first | Owner; **backend to agree** |
| D60 | `time_limit` is `failed`, always; the limit always on (1 h default, ≤ 1 day) | The backend treats it as failure; a hung island never says goodbye | Owner |
| D61 | Production compose: the Runner alone, `.env.prod` | Redis is the backend's | Owner |
| D62 | Temporary ufw access by an unsaved iptables rule, with its revert | Owner wanted temporary | Owner |
| D63 | `API.md`'s reference generated; a test fails on drift | A drifting contract is believed | Claude ⚠ |
| D64 | The outcome reader looks past entries the Runner did not write | The backend's `abort` can follow `run_end` | Claude |
| D65 | `migration.enabled` is an override, of type `bool` | Baselines; exact seeded repeats | Backend asked |
| D66 | Nothing reaches a stream before `run_start`, not even diagnostics | REQS: `run_start` first | Backend asked |
| D67 | **The record format is not versioned**, and records carry no `schema_version` (a fixed `"2"` from `950941b` until the owner had it removed, the backend dropping their check) | Owner: "i don't want schema versioning … this to be the final one" (bumping 2.0 → 2.1) | Owner (2026-11-01) |

---

## 5. Status

### 5.1 Work

All modules M1–M6 are done and committed, the last being `950941b` (2026-11-01: the backend team's
three requests, D65, D66 and `run_end` behind `abort`; no versioning, D67). Since then, uncommitted:
`schema_version` removed from the records entirely (D67); the budget cap lowered to 250,000 (D34). The docs rewrite is in
`docs/` (gitignored). The list for the backend was sent on 2026-11-01; their answers are awaited.
The deployment itself waits on where (§8). The table is in `IMPLEMENTATION_PLAN.md`.

### 5.2 Verified (2026-11-01)

- **Tests:** 425 pass without Docker, 135 with: the API against real master containers, whole runs
  against Redis 7 and Valkey 8, writer faults (paused Redis, out of memory, lost replies, half-open
  connections). pyflakes clean.
- **The dev and production stacks:** builds, a run end to end, a cancelled 500,000-evaluation run
  ending cleanly, a Runner `down` mid-run with the run adopted by the next one.
- **The real backend:** the whole pipeline, §9.6.
- **Memory:** `MEMORY_MEASUREMENTS.md`.

### 5.3 Known gaps

| Item | Effect |
| --- | --- |
| A master that loses Redis cannot write `run_end` (by design) | The stream has no terminator until the backend's `abort` |
| Queued runs live only in memory | A Runner restart loses them (404; the backend fails them) |
| `/docs` loads Swagger's script from a CDN | Blank offline; `/openapi.json` works |
| The backend has not agreed the contract's open items | Their worker may not handle `cancelled` as we mean it (§9.6) |
| `docs/`, `tests/` gitignored | A clone has neither |
| The backend's memory at the Runner's limits (§9.6) | Two cap runs need ~9–10 GB on their side; their Redis is near its 2 GB limit |

Nothing is stubbed or half-built.

---

## 6. How to work on it

```bash
git clone git@github.com:FYP-Archipelago/baseline-dEA.git && cd baseline-dEA
git checkout yb/streaming                        # then get docs/ and tests/ from the owner
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'                          # + pytest, pyflakes, httpx2

cp .env.example .env                             # REDIS_PASSWORD; RUNNER_HOST_PORT, REDIS_HOST_PORT
docker compose up -d --build                     # Redis, the Runner, the dummy worker; both images
python scripts/dummy_backend.py run docker_de_sphere
docker compose down                              # removes Redis and every stream

pytest -m "not docker"                           # ~15 s
pytest -m docker                                 # builds :pytest images, throwaway Redis 7 / Valkey 8
python -m pyflakes archipelago_logging algorithms benchmarks orchestrator service scripts tests
```

After changing an API response, model or preset: `python scripts/generate_api_docs.py`. After
changing a field description in `events.py`: `python scripts/generate_logging_docs.py
--fields-only` (the examples need Docker: `--image archipelago-island:latest --broker-url …`).
Otherwise `tests/test_docs_are_current.py` fails. Environment variables the code sets for itself:
`RUN_CONFIG`, `MASTER_CONTAINER_NAME` (masters), `ISLAND_CONFIG`, `ISLAND_CONTAINER_NAME`,
`IN_ARCHIPELAGO_CONTAINER` (islands).

**Next:** the backend's answers to the list sent on 2026-11-01, starting with dropping their
`schema_version` check (until then their module refuses our records); deploy (`DEPLOYMENT.md`) once
the target is known.

---

## 7. Remaining work

`IMPLEMENTATION_PLAN.md`, "Left".

---

## 8. Open questions

| Question | Recommendation | Decides |
| --- | --- | --- |
| Commit `tests/`? | **Yes**: they are the only proof the contract holds; a teammate or CI needs them | Owner |
| Where do the Runner and Redis run? | Unknown. A containerised Runner on Redis's compose network needs no firewall change (tested) | Owner + Backend |
| `abort` also when the Runner says `failed` without a `run_end`? | **Yes**: a Runner that lost Redis answers `failed` and cannot end its stream; otherwise their worker waits 900 s | Backend |
| What does `cancelled` mean to the backend? | Their worker clusters it, their supervisor fails it (§9.6). Pick one | Backend |
| Sizing the backend's host and Redis at the cap | ~9–10 GB at peak with two workers; Redis `COUNT` on reads or 3 GB (`MEMORY_MEASUREMENTS.md` §3). Otherwise a lower cap | Backend + Owner |
| A Redis ACL user? | Must allow `PING`, `XADD`, `XLEN`, `XRANGE`, `XREVRANGE`, `CLIENT ID`, `CLIENT KILL` | Backend |
| `validate_events` | Always on; confirm with Adithya | Backend |
| Hardware telemetry (`device_metrics`) | Unknown: required in the original spec, removed from the records, documented as a deviation | Owner / team |
| Synchronous migration for reproducibility | **No**, unless the paper needs bit-identical reruns with migration | Owner |

---

## 9. The backend

### 9.1 Who and how

The backend side builds the Archipelago Backend (API, Auth, Persistor, PostgreSQL), the Dashboard
and the Worker Pool, at `~/Desktop/archipelago-backend` on the owner's machine (FastAPI, psycopg,
redis-py; the clustering module `archipelago`, pinned). `REQS.md` addresses the runner owner
("Yooooo Yaaaash") and mentions an Adithya. Messages are relayed by the owner, who sent them the
plan's list on 2026-11-01 (§9.8). What was forwarded between their reply and then is not recorded.

### 9.2 Sequence

1. `REQS.md` (Appendix A). 2. Our review and `RUNNER_REPLY.md`. 3. Their proposal for direct island
writes, then their full reply (Appendix B). 4. The Runner API Contract doc (2026-10-30/31). 5. They
ran the Runner against their backend (2026-11-01, "chill") and asked for `migration.enabled`,
`run_end` found behind their `abort`, and nothing before `run_start`: done. 6. Our end-to-end test
against their backend (§9.6), and the list of what it found, sent 2026-11-01.

### 9.3 The stream contract

| Aspect | Agreed |
| --- | --- |
| Key | `run:{run_id}:runner_logs` |
| Entry | One per record: `kind` (`row` / `event`) and `data` (the record's JSON) |
| Writers | Each island, own connection; the master for `run_start`, diagnostics, `run_end`. The backend writes `abort` |
| Order and boundaries | Per-island `seq` order; `run_start` first; `run_end` last after the BYE barrier; first terminator wins |
| Delivery | Nothing dropped; a slow Redis slows runs; a stall past the threshold fails the run |
| Trimming, retention | No `MAXLEN`, `noeviction`; the backend expires streams after clustering |
| `schema_version` | None: the format is not versioned (D67). The backend is dropping its check |
| Rows | `island_id, seq, eval_index, generation, t_wall, individual_id, parent_ids, fitness, genome_encoding, genome_hash, genome_repr, genome_repr_mode` required |
| `genome_repr` | Its type varies by encoding and mode (`LOGGING_FORMAT.md` §5); their module decodes every one (verified) |
| Ids, seed | The backend mints `run_id`; the seed is an optional override |

### 9.4 The HTTP contract

As implemented: `API.md` and the Swagger UI; for the backend, the Runner API Contract doc. Their
original specification is summarised in Appendix B; it differed in an `evaluation_budget` minimum
of 10, no cancel, no 503s and no `cancelled`.

### 9.5 What they committed to

Native JSON `data` and `genome_repr` objects; `abort` when the Runner is missing or answers 404,
first terminator wins; `noeviction` and no `MAXLEN`; slower runs under a degraded Redis; deleting
streams after clustering.

### 9.6 The end-to-end test (2026-11-01)

**Setup.** Their repository untouched, at `f40542c`. Their `compose.yaml` as project
`archipelago-it`, with an override file only for local ports: `!override` to `127.0.0.1:6391`
(Redis) and `127.0.0.1:8011` (API), because their `0.0.0.0:6379` and `:8000` collide here, plus
`build.network: host`. A scratch env file: `POSTGRES_PASSWORD`, `REDIS_PASSWORD`,
`ARCHIPELAGO_RUNNER_URL=http://runner:8000`. The Runner from `docker-compose.prod.yml` on their
network (`archipelago-it_default`, RUNNING.md "With the real Archipelago Backend"). A driver posted
runs to their API, followed their SSE, polled our API, read our stream with `validate_log` before
their 60 s expiry, and fetched their result.

| Case | Result |
| --- | --- |
| All 9 presets, batch | Every streamed evaluation clustered; every genome encoding decoded; no errors |
| Streaming (DenStream; LSH + BIRCH + DenStream); batch with all three | Updates over SSE, then `final`; counts equal |
| `migration.enabled: false` with a seed | 19,800 evaluations, no migration records or edges |
| Cancel mid-run, batch and streaming | **Runner correct:** ~5 s wind-down, `run_end` `aborted`, `cancelled`, their `abort` after it looked past. **Backend inconsistent** (below) |
| Time limit (8 s) | `run_end` `time_limit` with `error`; `failed`; their SSE shows our text verbatim |
| Runner restart mid-run | Adopted; invisible to them; 187,362 evaluations clustered |
| Three concurrent | The third `queued` → `running` → `finished`; all clustered |
| Runner down at their `POST` | Their 502 "runner unreachable"; their `abort`-only stream expires |
| Invalid overrides (bounds, type, key, combination) | Our 422 text, through their 422 `{"detail": …}` |
| 500,000 × 6 islands, batch and streaming; two at once | Correct; the memory is `MEMORY_MEASUREMENTS.md` §2 |

**Nothing broke on the Runner's side**, and nothing needed changing. **What the backend does
differently or wrong** (all on the list sent to them on 2026-11-01):

1. **`cancelled` contradicts itself.** Their supervisor fails the run ("run cancelled") and appends
   `abort`; their worker's `split()` treats `run_end` `aborted` as a normal end and clusters.
   Batch: the clustering is discarded. Streaming: SSE delivers `final`, then `GET /result` is 404.
2. **Redis near its limit at the cap.** `XREAD` without `COUNT` buffered 581 MB in one reply;
   Redis peaked at 1.81 GB of `--maxmemory 2gb`.
3. **Worker memory**: 3.8 GB per worker for 500,000 evaluations, kept after the job.
4. **Our 503 at `POST`** (retryable) becomes their 502 and fails the run.
5. **Their compose exposes Redis and the API on all interfaces.**
6. **`result.run.topology`** shows the preset's topology with migration off.
7. **No cancel endpoint** on their API.
8. *Expected, not a bug:* `pso_ackley`'s "more than one cluster label" warning (the hash rounds,
   their features do not).

### 9.7 Mismatches found in their earlier reply

- `genome_repr` is not just "an object": its type varies with encoding and mode (now verified
  decoded by their module).
- The abort rule's gap (§8): a Runner that lost Redis answers `failed` without a `run_end`.
- Their torus rule could never trigger; the real problem (tori at 4–5 islands are rings) is fixed
  on our side (D18).
- Restarts: a retried `POST` could have started a second run into an old stream (we answer 409),
  and a finished run could read as 404 (we answer from `run_end`).
- A minimum budget of 10 evolves nothing (D34).
- Islands reaching a host Redis "through the host" does not happen on its own (ufw; D29).
- Stale claims in `RUNNER_REPLY.md` they may still believe: hash-based container names
  (withdrawn), rejecting runs that don't fit now (replaced by their rule), keeping `local`
  (removed), "a seed makes a deleted stream re-creatable" (not with migration, D17).

### 9.8 Sent

Sent (2026-11-01): `IMPLEMENTATION_PLAN.md`, "Sent to the backend". Awaiting their answers. The
`schema_version` field is already gone from our records; they are dropping the check.

---

## 10. Gotchas

**Repository and process**
- `docs/`, `tests/`, `CLAUDE.md` are gitignored, and tools honouring `.gitignore` (ripgrep) skip
  them: search them by path.
- "Worker" means a Worker Pool worker. Older documents (`REQS.md`, the historical reviews) say
  "worker" for an island and `raw` for the stream.
- Commit `aab59b8` (the naming change of 2026-10-27) is not on any branch; it was folded into later
  commits.

**Docker on the owner's machine**
- Builds need `--network=host` (the builder has no DNS).
- The Docker **Python SDK** cannot build images (`~/.docker/config.json` names a broken gcloud
  credential helper); the CLI works, and the Runner never builds.
- **Stale images run old code** while everything looks healthy: every master runs from
  `archipelago-runner`, even with the service on the host. Rebuild both after code changes.
- ufw drops traffic from Docker's networks to the host except at `172.17.0.1`, and does **not**
  filter ports Docker publishes. The temporary rule for a host Redis is in README "ufw".
- Port 6379 is an unrelated Redis on the host. The dev stack uses 6380 and 8000; the backend's
  compose wants 6379 and 8000 too (§9.6 for the override).
- The dev stack's Redis has no persistence: stopping it deletes every stream, and `GET` then
  answers 404 for runs that were fine. Start with `up -d`.
- A container on `--network host` cannot join other networks and inherits the host's hostname,
  hence `ISLAND_CONTAINER_NAME` and `MASTER_CONTAINER_NAME`.
- After a Wi-Fi reconnect (2026-10-30) NetworkManager stripped the Docker bridges' addresses, and
  published ports hung; a reboot fixed it. Marking `docker0`, `br-*`, `veth*` unmanaged prevents it.
- DooD: bind-mount sources resolve on the host, which is why nothing is mounted into islands.
- In zsh a `$VAR` holding a command is not word-split; use bash scripts. Set `PYTHONPATH` to the
  repo for scratch scripts, or a script's own directory shadows the package.

**The backend in testing**
- They expire both streams 60 s after the run and the worker are done: read ours quickly.
- Their Redis runs `appendonly yes`: streams reach their disk too (1.2 GB of AOF after the cap test).
- Their worker processes keep memory between jobs; restart them before measuring.

**Intentional, though it looks wrong**
- `EventLogger.emit` advances `seq` before the sink: a raising sink has failed for good, so the gap
  cannot matter.
- `RedisStreamWriter.write` blocks when full and raises once failed; it never drops (D8). Redis OOM
  is retried until the stall window (memory frees when the backend expires streams).
- A raw redis-py `Connection`, retries off, `CLIENT ID` on every connect: all three are needed for
  no duplicates (D9, D10).
- A NaN or infinite fitness fails the island (D25).
- `migration_send` without `migration_arrive` is normal (final-generation sends; stopped
  destinations).
- `max_generations` (5,000) is a safety cap; the budget and stagnation stop runs.
- `run_start.config._runtime.settings.redis_url` shows `***`.
- After `BYE` an island's container may run a moment longer; `BYE` is final.
- `node_id` and `container_id` are both the container name; `RunSummary.stream_key` is the full
  key.
- Three Docker tests use fixed run ids and rely on fresh broker containers per session.
- A bug pattern seen in M4.3: a loop variable shadowing a parameter (`key`) in a long function.
  Prefer distinct names.

---

## Information still needed from the owner

- [ ] The platform diagram as a file (`docs/architecture.png`); §2.1 is a transcription.
- [ ] Whether the Persistor stores raw records, or only results.
- [ ] Names and roles: the backend author, Adithya.
- [ ] What was forwarded to the backend between their reply and the list of 2026-11-01.
- [ ] Deadlines: semester milestones, the GECCO date, reviews.
- [ ] The deployment target: machine, resources, firewall, where Redis is.
- [ ] How teammates get `docs/` and `tests/`.
- [ ] The paper's experiment plan, if one exists; plans for telemetry and VolPE.

---

## Appendix A: `REQS.md` (the backend's requirements), verbatim

Source: `~/Downloads/REQS.md`, dated 2026-10-23. Reproduced so this document stands alone.

```markdown
# Runner requirements (for the Archipelago rewrite)

Yooooo Yaaaash, this says what the runner must do in the new system, not how to build it. Items marked
"proposal" are the backend's suggestion and are not agreed yet (see Open questions).

## Context

The backend (archipelago-api) mints a `run_id` and starts the runner with that id plus a problem
(a preset and a few overrides). The runner runs the island EA and appends its logs, as they are
produced, to the Redis stream `run:{run_id}:runner_logs`. A clustering worker reads that stream
live and writes `run:{run_id}:clustered`. Nothing reads log files any more. The runner writes
only `runner_logs` (plus the backend's one `abort` entry on a crash, see below). It never touches
Postgres or `clustered`.

## Requirements

### Service interface (what the backend calls, and nothing more)
- Start a run: `run_id` (given by the backend, used as-is everywhere in the logs) + `preset` +
  `overrides`. Respond immediately (async run). Unknown preset or bad override value: reject, or
  fail the run with an error text.
- Status of a run: `queued | running | finished | failed` + error text. The backend polls it
  every 2 s.
- List presets: `[{preset, algorithm, benchmark, description}]`. This feeds the frontend's
  problem picker.
- Proposal: HTTP, `GET /presets`, `POST /runs {run_id, preset, overrides}` -> 202,
  `GET /runs/{run_id}` -> `{state, error}`.
- Same Redis as the backend.
- Several runs may be in flight at once; each is isolated by `run_id`.

### Output to `run:{run_id}:runner_logs`
- One stream entry per record. Proposal: fields `kind` (`row` or `event`) and `data` (the record
  as one JSON object: the CSV row as a dict, or the `run.jsonl` line).
- Every evaluation row and every event is appended, unchanged from today's records. Nothing
  dropped or sampled.
- Ordering: per island, records must arrive in `seq` order. Rows and events share one per-island
  `seq` counter. Different islands may interleave freely.
- Rows must carry: `island_id, seq, eval_index, generation, t_wall, individual_id, parent_ids,
  fitness, genome_encoding, genome_hash, genome_repr, genome_repr_mode`. `genome_repr` must
  always be present; rows whose `genome_repr_mode` is `hashed` cannot be positioned by the
  clustering module.
- Events the module reads: `run_start` (`run_id, schema_version, algorithm, benchmark,
  migration.topology, num_islands, evaluation_budget, evaluation_budget_per_island,
  datasets.maximising`), `island_start` (`island_id, genome_encoding, benchmark_params,
  clock_offset_ns`), `generation_end` (`maximising`), `migration_arrive` (`island_id, seq, t_wall,
  origin_individual_ids, arrived_individual_ids`), `island_end`, `run_end`.
- `run_start` is the first thing in the stream and `run_end` the last. After `run_end`, nothing
  more is appended.
- Log schema major version 2 (today `SCHEMA_VERSION = "2.0"`). `run_start` states it.
- Appends should happen continuously during the run (live), not batched at the end.

### Failure and abort
- If the run fails (island crash, bad config, runner error), status becomes `failed` with a
  readable error text. The runner should still try to end the stream cleanly where it can.
- Proposal: the backend, not the runner, writes an `abort` entry to `runner_logs` when it sees a
  failed or dead runner (a crashed runner cannot report its own death). The runner should not
  write `abort`. The worker also has a 900 s idle timeout.
- Runs are not reproducible today (islands seed from OS entropy). The backend plan flags this;
  a seed setting is optional but would be welcome.

### Presets and overrides
- Presets are the sample configs in `config/` (de_rastrigin, es_schwefel, ga_knapsack, ga_tsp,
  gp_nguyen7, gp_yacht, pso_ackley, full_dataset, ...). Each needs a one-line description.
- Overrides the frontend may send (proposal, carried over from the old plan): `num_islands`,
  `evaluation_budget`, `target_generations`, `migration.topology`, `migration.interval`,
  `migration.num_migrants`. Anything else is rejected. The runner validates values.

## Keep as is
- Record fields and semantics of `evaluations.csv` rows and `run.jsonl` events
  (`archipelago_logging`, `LOGGING_FORMAT.md`, schema 2.0). The clustering module depends on them.
- The per-island `seq` counter shared by rows and events, `t_wall`, `clock_offset_ns`.
- `genome_mode: compact` (or `full`) as the logged mode, the genome hash, and
  `genome_precision_decimals`.
- Run id format `run-{UTC %Y%m%dT%H%M%SZ}-{8 hex}` (now from `archipelago_logging/ids.py`, but the
  id must come from the backend).
- `algorithms/` (ga, de, pso, gp, es), `benchmarks/` (incl. committed datasets), the migration
  logic (`topology.py`, `migration.py`), and the config validation in `RunConfig` for the
  options that stay.
- Island model: one island per worker, migration between islands, termination rules
  (budget, stagnation, target fitness, max generations).

## Remove / don't need
Verified to exist in the repo; nothing in the new system uses them.
- File outputs: `runs/<id>/`, aggregate `run.jsonl`, `evaluations.csv`, `summary.json`
  (`master.py`), `resolved_config.yaml` (`RunConfig.write_resolved`), per-island shards
  (`logging.write_island_shards`), and `logging.output_dir`, `run_subdir`, `compress`,
  `flush_interval_seconds` (file-only knobs). Redis replaces all of these.
- The whole `live_stream` TCP mechanism (`LiveStreamConfig`, `Master._make_stream_sink`,
  `_send_artifact`, `_send_stream_tail`, `scripts/stream_receiver.py`, `Dockerfile.receiver`).
  Redis `runner_logs` is the stream now. Its `hello`/`artifact`/`summary` messages and the
  drop-on-overflow queue are not part of the new contract.
- CLI-only commands and flags: `validate`, `estimate` (log volume), `list`, `verify-log`, `--yes`,
  `--set`, the large-volume warning, and `estimate_log_volume` (it exists to size files on disk).
  Replaced by the service interface. Keep `list`'s information as the presets endpoint.
- Logging options that break clustering or add nothing: `genome_mode: hashed`,
  `genome_sample_every` (drops genome bodies from rows), `log_evaluations: false` (no rows).
  The module needs a genome on every row and every row present, so these should not be
  settable.
- `validate_events` (Pydantic check per record): a cost option, not a feature. Check with
  Adithya whether to keep it always-on or drop it.
- `stream_to_master` and `network_queue_size`: the islands-to-master log path stays in some
  form, but the toggle does not (it is always on, and today live_stream even requires it).
  Note the queue drops records on overflow, which is not allowed in the new contract.
- Deployment knobs only the CLI user needed: `execution.control_host/port/advertise_host`,
  `docker.*` fields (cpus, memory, network, labels, build, build_network, mount_output_dir...),
  `worker_python`. Whether the docker backend itself stays (one container per island) or only
  `local` remains is not decided here: check with Adithya / the team.
- `RunConfig.run_id` auto-generation: the id is always given by the backend. `description` goes
  into `run_start` and may stay as the preset description.

## Open questions
1. `runner_logs` entry format: is `kind` + `data` (JSON) acceptable, or do you prefer another
   shape? Proposal only.
2. HTTP contract (`/presets`, `POST /runs`, `GET /runs/{id}`): proposal only, do you want
   anything different?
3. Abort: is it fine that the backend writes `abort` into `runner_logs`, or will the runner
   catch its own failures and write it?
4. Override whitelist: are the six keys above right for the new runner?
5. Seed: add an optional seed override so runs can be reproduced?
6. Concurrency: how many simultaneous runs can the runner service take, and what does it do
   beyond that (queue and report `queued`, or reject)?
7. Docker vs local island backend in the service, and the `validate_events` question above.
8. Are the raw logs ever kept? The backend deletes the stream shortly after the result is
   saved, so a run cannot be re-clustered without re-running.
```

## Appendix B: the backend's messages, verbatim

**First message (proposing direct writes):**

> Small, frequent batches, as records are produced:
> - Who writes: each island process appends its own records straight to run:{id}:runner_logs,
>   over one pipelined Redis connection per island. One connection per island keeps that
>   island's seq order for free.
> - How often: flush every ~100-250 ms or every ~200 records, whichever comes first, plus a
>   final flush before island_end.
> - Run start and end: the master writes run_start before any island starts, and run_end after
>   every island has ended.

**Second message (their reply to `RUNNER_REPLY.md`):**

> Okay so we expose redis (redis://:<password>@<main-server>:6379/0) and if it is same machine
> then anyways it can access port through host. also if island cant reach redis then u can end
> and give error rmsg and yea master will also connect to send run_start and run_end and if
> master also cant reach tehn abort will kick in anyways
>
> So islands send island_end then push to redis and once all pushed BYE and on recieving all BYE,
> master write run_end, this is fine. (there is also a timeout for BYE i suppose so if it dies b4
> sending BYE it goons the run, the master can just send run_end then)
>
> MAXLEN is unbounded and noeviction. If a producing island stalls goes past ur threshold, u can
> fail the run. if runs get slower bc of degraded redis, that is okay. runner isnt held to a wall
> clock budget.
>
> For entry format, keep kind + data in ur native json format thats fine. For genome_repr it
> expects like a string but u will send like an object as per native format, thats fine, ill
> change module to accept and read the object.
>
> why changing conatiner naming? last 8 is random hex anyways idt collision will happen, so just
> use the last 8 hex.
>
> optional seed. done.
>
> for failed runs if u catch a failed run just run_end with termination reason error and set
> status to failed. if runner doesnt answwer and goes MIA or replies 404 then backend will abort
> run. either way first terminator wins. also add some readbale error text so we show user.
>
> also 15 containters thing accept and queue any run that is less than equal to max-1 islands
> otherwise reject if more than that. yea 2 concurrent runs fine ig.
>
> run logs not needed after clustering so its deleted after result is saved so fine.
>
> why do u need 2 things docker and local setting for execution? remove local?
>
> [Their HTTP contract followed, summarised here and superseded by the Runner API Contract doc: `GET /presets` with the override schema
> and the `de_rastrigin` example; `POST /runs` 202 / 422 / 409 with idempotency on `run_id` and the
> cross-field rules; `GET /runs/{run_id}` 200 / 404, polled every 2 s, `queued` a real state.]
>
> no cancel endpoint for now ig, we see later?

## Appendix C: what was removed from `CLAUDE.md`

On 2026-10-27 ("anything that conflicts just remove") `CLAUDE.md` gained a deprecation banner and
lost: the "phase you're starting now" framing; "versioned configs/logs"; the EvoLabs flow; log
aggregation by the master, "worker containers" and the `local` backend; "search is deliberately
not reproducible"; the JSON Lines file format, one `migration` event and `device_metrics`; the
`population_size` default of 100,000, `num_generations`, "log output location" and CLI flags. It
keeps the research background, scope, glossary, the five algorithms, the benchmark policy and the
decoupling principle.

## Appendix D: commits

```
950941b  —      feat: drop schema versioning, take the backend's requests
c8068b1  M6     docs: generated API reference (+ DEPLOYMENT.md, docs pass, review fix)
6f836c7  M6     feat: production compose file
f18b07e  M5.9   feat: Swagger UI, run_end.error, time limit always on
fc3a11e  M5     feat: add runner as a HTTP service
a028b03  —      fix: clearer error for verify-log
b6fdff2  M4     feat: finish up streaming + memory testing (M4.3b–M4.8)
2e3ff24  M4.3b  feat: add dummy worker to help test redis stream
62f182b  M4.3   feat: islands and master append directly to Redis
21c5519  M4.2   feat: redis stream writer never drops or duplicates
d53874d  M4.1   feat: streaming; one record shape and one ordered channel per island
55b8960  M4.0   refactor: remove the log-volume estimate
a7132cf  M3.5   feat: validate supplied run ids and refuse reused ones
6b656f1  M3.4c  feat: require the fields the clustering module reads
5739253  M3.4b  refactor: remove backwards compatibility for logs
46b2e67  M3.4   refactor: always validate log events
fa24ac8  M3.3   refactor: require a genome body on every evaluation row
ea650be  M3.2   feat: add an optional run seed for run-reproducibility
6f2ed43  M3.1   fix: reject a torus that collapses into a ring
f3c58d1  M2.1   refactor: remove dead code and unused alternate options
8f870f4  M2     refactor: clean residual local runtime code to make it purely docker
b01e41b  M2     refactor: remove the local execution backend
3be38ce  M1     refactor: delete old streaming contract
ef031e2  —      feat: log-streaming (TCP live stream; last commit on main)
1a67fd7 … 2e3c9e8  initial harness (2026-09-06)
```
