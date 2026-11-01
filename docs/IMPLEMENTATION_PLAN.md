# Runner rewrite: the plan

The plan that turned the baseline harness into the Runner service agreed with the Archipelago
Backend (`REQS.md` → `RUNNER_REPLY.md` → their reply; `docs/HANDOFF.md` §9). **M1–M6 are done and
committed** (`950941b`), and the list below was sent to the backend on 2026-11-01. What is left
waits on others: the backend's answers, and where to deploy.

## Working agreement

- **Checkpoint per module.** At the end of each: the full suite green, a report, a proposed commit
  message, then **stop** until the owner has committed. Claude never commits.
- **Docs move with the code**, in the same checkpoint.
- **Names follow the platform architecture diagram** (`HANDOFF.md` §2.1), except the stream:
  `run:{run_id}:runner_logs`.
- `docs/`, `tests/` and `CLAUDE.md` are gitignored. `CLAUDE.md` is deprecated; `docs/` is the
  source of truth.

---

## Done

| Module | What | Commit |
| --- | --- | --- |
| M1 | Removed the TCP live stream and the Redis publisher stub | `3be38ce` |
| M2, M2.1 | Removed the `local` backend; dead code and unused alternates | `b01e41b`, `8f870f4`, `f3c58d1` |
| M3.1 | A torus that collapses into a ring is refused | `6f2ed43` |
| M3.2 | Optional run `seed` (`SeedSequence` spawn keys; per-source broker RNG) | `ea650be` |
| M3.3 | A genome body on every row (no `hashed`, sampling, or rows switched off) | `fa24ac8` |
| M3.4, M3.4b, M3.4c | Always validate; no backwards compatibility; the fields the clustering module reads are required | `46b2e67`, `5739253`, `6b656f1` |
| M3.5 | Supplied run ids validated; reused ones refused | `a7132cf` |
| M4.0–M4.3 | One record shape, one ordered channel per island; the Redis writer (never drops, never duplicates); islands and master append directly | `55b8960`, `d53874d`, `21c5519`, `62f182b` |
| M4.3b–M4.8 | Names per the diagram; compose stack and dummy worker; no shared filesystem; the shutdown barrier; validator; contract tests on Redis 7 and Valkey 8; memory measured, cap 500,000 | `2e3ff24`, `b6fdff2` |
| — | `verify-log` explains a missing stream; compose started detached | `a028b03` |
| M5 | The HTTP service: presets, `POST /runs`, the Run Queue (master container per run), status, cancel, restart adoption; CLI removed; stream renamed `runner_logs` | `fc3a11e` |
| M5.9 | Swagger UI; `run_end.error`; the time limit always on | `f18b07e` |
| M6 | Production compose and the ufw guide; `API.md` generated; `DEPLOYMENT.md`; outcome read past foreign entries | `6f836c7`, `c8068b1` |
| — | The backend team's three requests; no versioning (2026-11-01) | `950941b` |

## 2026-11-01 (`950941b`, and `docs/`)

- [x] **The backend team's three requests.** `migration.enabled` is an override of a new `bool`
      type; `_stream_outcome` finds `run_end` behind their `abort` (skipped by `kind` too);
      nothing reaches a stream before `run_start`, and results are collected only for islands
      that were launched.
- [x] **No versioning.** `schema_version` was a fixed `"2"` (`SCHEMA_MARKER`), not a version; the
      reader no longer checks it; `_meta` carries only `harness_version`. The field goes once the
      backend drops its check (below).
- [x] **End-to-end test against the real backend** (`f40542c`): every preset, batch and streaming,
      cancel, time limit, restart, queueing, Runner down, invalid overrides, migration off
      (`HANDOFF.md` §9.6). Nothing on the Runner's side broke.
- [x] **The whole platform's memory, by component** (`MEMORY_MEASUREMENTS.md` §2–3).
- [x] **Docs rewritten to the point**: every document, with the generated regions regenerated.

## Left

- [x] Commit the above (the owner, `950941b`; not yet pushed: `origin/yb/streaming` is at `c8068b1`)
- [x] Send the backend the list below (the owner, 2026-11-01)
- [ ] The backend's answers to it
- [ ] Deploy where the backend's Redis runs, once the target is known (`DEPLOYMENT.md`;
      `HANDOFF.md` §8). Not now (owner, 2026-11-01)
- [x] Remove `schema_version` from the records (owner: the backend will drop its check):
      `BaseEvent.schema_version`, `SCHEMA_MARKER`, the logger's stamp, the tests, the docs and
      their examples. Not yet committed. Until the backend's check is gone, their module refuses
      every run
- [x] `evaluation_budget` capped at 250,000 instead of 500,000 (owner, 2026-11-01), for the
      backend's memory (`MEMORY_MEASUREMENTS.md` §3). Fast suite and API reference only; the Docker
      tests' long run (`LONG` in `test_service_runs.py`) now uses 250,000 and was not re-run
- [ ] Decide whether `tests/` stays gitignored

---

## Sent to the backend (2026-11-01)

Sent by the owner; ticked as sent, not as answered.

**From the end-to-end test (2026-11-01):**

- [x] **Remove your `schema_version` check.** The record format is not versioned. Records carry a
      fixed `schema_version: "2"` only because the clustering module requires it
      (`records.py:152`); once that check is gone we remove the field.
- [x] **Cancelled runs contradict themselves.** On the Runner's `cancelled`, the supervisor fails
      the run ("run cancelled") and appends an `abort`, but the worker treats `run_end` with
      `aborted` as a normal end and clusters the run. Batch: a whole clustering done and thrown
      away. Streaming: SSE sends `final`, then `GET /result` answers 404 "run failed". Pick one
      meaning (fail it in `split()`, or keep the partial result), and don't relay a `final` the API
      won't serve.
- [x] **Redis headroom at the cap.** The worker's `XREAD` has no `COUNT`: picking up a complete
      644 MB stream made a 581 MB output buffer, and with two runs at the cap Redis peaked at
      1.81 GB of its 2 GB `maxmemory`. A little more and our `XADD`s get OOM and the runs fail
      after `BROKER_STALL_SECONDS`. Add `COUNT` (a few thousand), or give Redis 3 GB.
- [x] **Worker memory.** Batch clustering of 500,000 evaluations peaked at 3.8 GB per worker
      (streaming 3.5 GB), so two at once need ~7.9 GB, and a worker keeps it afterwards (1.4 GB
      idle). A memory limit per worker and a fresh process per job, or a lower cap from us.
- [x] **A 503 from `POST /runs` is retryable.** Our 503 (Redis unreachable) and 409 both become
      your 502 and fail the run; after a 503 nothing started, and retrying the same `run_id` is
      safe.
- [x] **Your compose publishes Redis on `0.0.0.0:6379`** (ufw does not filter Docker-published
      ports) and the API on `0.0.0.0:8000`. Bind them to `127.0.0.1`, or leave Redis unpublished:
      the Runner can join your compose network.
- [x] **`result.run.topology`** names the preset's topology for a run with
      `migration.enabled: false`.
- [x] **No cancel on your API**, so the dashboard cannot cancel; only your cleanup uses ours.
- [x] FYI: "N location(s) received more than one cluster label" (seen on `pso_ackley`) is expected.
      `genome_hash` rounds to `genome_precision_decimals`, your features use exact values.
- [x] FYI, verified: all nine presets decode and cluster every streamed evaluation; our
      `run_end.error` reaches the user verbatim; a Runner restart mid-run, queueing, invalid
      overrides (our 422 text) and a Runner that is down (your 502, the stream expires) all behave.

**Older items, sent with it:**

- [x] The Runner API Contract doc (<https://claude.ai/artifact/1u35AQwrukjXHfDnUxeB7f>) and its
      open items: `cancelled`, the 503s, `run_end.error`, `time_limit` as a failure, the bounds
      (budget 1,000–500,000, seed ≤ 2³²−1), `abort` also for `failed` without `run_end`.
- [x] The seed correction: with migration on, a seed makes runs comparable, not identical, so a
      deleted stream cannot be recreated exactly.
- [x] The `genome_repr` types per encoding (`LOGGING_FORMAT.md` §5) and the torus rule.
- [x] Redis permissions for a restricted user: `PING`, `XADD`, `XLEN`, `XRANGE`, `XREVRANGE`,
      `CLIENT ID`, `CLIENT KILL`.
- [x] Ask: their deployment topology; whether the Persistor Service stores raw records.

---

## Open decisions (defaults in bold)

| Question | Default |
| --- | --- |
| A synchronous-migration mode, for bit-reproducible seeded runs with migration? | **No**; documented |
| How islands reach Redis | **They join the master's networks** and use its URL |
| HTTP framework | **FastAPI + uvicorn** |
| `validate_events` | **Always on** |
| Cancel endpoint | **`POST /runs/{run_id}/cancel`**, state `cancelled`; no cancel-all |

## REQS coverage

| REQS item | Where |
| --- | --- |
| Service interface: presets, start, status, concurrent runs | M5 |
| One entry per record, `kind` + `data`; nothing dropped; live appends | M4.1, M4.2 |
| Per-island `seq` order; `run_start` first, `run_end` last | M4.1, M4.3, M4.5 |
| Row and event fields | preserved; required since M3.4c |
| Schema major version 2 | dropped: no version field; the backend drops its check |
| Failed status with readable error; stream ended cleanly | M4.5, M5.4, M5.9 |
| `abort` by the backend, first terminator wins | backend; the Runner looks past it |
| Seed; presets and the override whitelist; backend-minted run ids | M3.2, M5.2, M5.3, M3.5 |
| Removed: files, `live_stream`, the CLI, `hashed` / sampling / rows off, `stream_to_master`, deployment knobs in runs, id generation, `local` | M1–M5 |
| Agreed: `noeviction`, no `MAXLEN`, fail past the stall threshold; 2 runs, 15 containers; 202/422/409/404 and idempotency | M4.2, M5.3–M5.5 |
| Cancel | M5.6 |
| Deployed test and memory measurement | M4.8; the whole platform 2026-11-01 |
