# Architecture

How the Runner is put together, and why: the decisions that were not obvious, or that a reasonable
person would have made differently. The platform around it, and the history of these decisions, are
in `docs/HANDOFF.md` (§2.1 for the names used here).

The Runner is an HTTP service the Archipelago Backend calls. Each run is an island-model EA, a
master container and one container per island, appending every record to the Redis stream
`run:{run_id}:runner_logs`, which the backend's Worker Pool reads live.

---

## 1. Layers, and the one permitted coupling

```
  ┌──────────────────────────────────────────────────────────────┐
  │  service/            the HTTP API and the Run Queue           │
  │  orchestrator/       master, islands, config, migration,      │
  │                      Docker, the Redis writer and reader      │
  ├──────────────────────────────────────────────────────────────┤
  │  algorithms/         ga  de  pso  gp  es                      │
  │  benchmarks/         problems and instance loaders            │
  ├──────────────────────────────────────────────────────────────┤
  │  archipelago_logging/    the contract: stdlib + Pydantic      │
  └──────────────────────────────────────────────────────────────┘
                              │  JSON records on a Redis stream; nothing else crosses
                              ▼
     clustering · STNs · migration edges · MMD · dashboard (the backend; not in this repo)
```

Dependencies point downward only, and `orchestrator` reaches algorithms and problems only through
registries. `archipelago_logging` depends on nothing but the standard library and Pydantic (no
numpy, no Redis client), so a consumer can validate records without the Runner's scientific stack;
that is also why the Redis code lives in `orchestrator/`. An execution-layer import of anything
clustering- or graph-related is a decoupling violation.

---

## 2. Algorithms cannot write records

An algorithm has no reference to the logger. It has an `IslandContext`, whose `evaluate()` is the
only way to score a candidate: it mints the id, calls the objective, applies the genome policy and
emits the `evaluation` record, as one step.

```python
child = ctx.evaluate(Individual(genome=..., parent_ids=[...], operator=Operator.MUTATION.value))
```

So every evaluated individual is logged (evaluating *is* logging), `eval_index` has no gaps, five
algorithms cannot disagree about genome verbosity, and a sixth algorithm needs no logging code.
What it cannot enforce is honest `parent_ids` and `operator`; the operator tests check provenance
explicitly (`test_de_records_the_full_differential_provenance`).

---

## 3. A container for each run's master, and for each island

### The service and its Run Queue

The service (`service/`) is the API and the bookkeeping; it runs no EA code. `POST /runs`
validates the request (the preset, each override against its bounds, then the whole `RunConfig`)
and hands it to the **Run Queue** (`service/runs.py`), which starts a run when fewer than
`MAX_CONCURRENT_RUNS` are running and its containers fit under `MAX_CONTAINERS`; otherwise it waits,
in arrival order.

**Each run's master is its own container** (as the platform diagram draws it), from the Runner's
own image, with the run's config in `RUN_CONFIG`. A crashing run cannot take the service with it,
and a restarted service loses no running run: the masters carry on, and the new service finds them
by their labels (run id, island count, the original request, so idempotency survives). Queued runs
live only in memory.

**The stream is the master's only report.** When a master exits, the service reads the end of the
stream, past any entry it did not write (the backend's `abort` can follow `run_end`):
`run_end.termination_reason` gives the state (`completed` → finished, `aborted` → cancelled,
anything else → failed) and `run_end.error` the reason. A master that exited without `run_end`
failed first; its exit code and last line are the error. No second channel can disagree with the
stream, and the same reading answers for a run the service never saw.

**Cancelling is a signal:** SIGTERM to the master every 2 s until it exits (one sent before Python
installs its handler is lost). The handler only sets a flag; the master winds down as on the time
limit and `run_end` says `aborted`. The master enforces `RUN_TIME_LIMIT_SECONDS` itself, so a
time-limited run ends cleanly; the service kills a master only well past that.

**The service never builds images.** Compose builds both, the tests build their own: a build at
run time would make a run's start depend on the network and the builder.

### One container per island

Each island is its own container, the only execution path, so no second backend can drift from it.
The tests run real containers too. Masters and islands are created **programmatically per run**
(Docker SDK), never from a compose file: the island count is per run, and CPU, memory and images
are settings.

**Nothing is shared through the file system.** The master gets `RUN_CONFIG`, an island
`ISLAND_CONFIG`, both the settings as environment variables; an island's records go to Redis and
its result back in its goodbye. The only mount is the Docker socket, so Docker-outside-of-Docker's
path and ownership problems never arise.

### One Redis address for all

An island talks to the master (control) and to Redis (records), and one Redis address must work for
the service, the master and the islands (`_ensure_network`):

* **Service in a container** (deployment, compose): the master joins every network the service is
  on; a per-run bridge network carries the master as `archipelago-master`; each island joins it
  and every network the master is on. A compose name like `redis` resolves for all, and traffic
  stays inside Docker.
* **Service on the host** (development, tests): masters run on the host's network (the master
  learns its network mode from its own container, named in `MASTER_CONTAINER_NAME`), and their
  islands too, reaching the master at `127.0.0.1`.

Bridged islands crossing to the host via `host.docker.internal` were tried and rejected: Ubuntu's
ufw drops that traffic, and the failure is nearly invisible (every island runs, logs and exits 0;
only migration never happens). The master binds `0.0.0.0`, because a loopback listener is
unreachable from a container and the symptom is a hang. On the host network every island would
report the host's hostname, so each gets its container name in `ISLAND_CONTAINER_NAME`; that name is
its `node_id`, which `docker ps` and `docker logs` take directly.

---

## 4. The control plane

Newline-delimited JSON over TCP, one strict request/response connection per island:

| Op | Purpose |
| --- | --- |
| `time` | Clock probe; the island estimates `clock_offset_ns` |
| `ready` | About to start; reports the offset |
| `migrate` | Send emigrants; the master picks destinations and mints `migration_id`s |
| `poll` | Collect migrants waiting for this island |
| `heartbeat` | Progress; the reply carries `stop: true` once the budget is spent or the master wants the run wound down |
| `bye` | Done, with every record acknowledged by Redis; carries the result |

Records never travel over it, so a burst of evaluations cannot delay a migration. TCP needs nothing
installed, and a closed socket tells the master an island died.

**Islands never talk to each other.** Every migrant passes through the master, the only party that
sees both halves of a transfer and so the only one that can mint the id joining them. The topology
is enforced in one place, and adding an island reconfigures nobody.

---

## 5. Records: straight to Redis, nothing dropped

Each emitter (every island; the master for `run_start`, diagnostics and `run_end`) appends through
its own `RedisStreamWriter`: one connection, one pump thread, batches of 200 records or 150 ms, each
one MULTI/EXEC of `XADD`s.

* **Order holds by construction:** `seq` is assigned and the record handed to the writer under one
  lock, then one queue, one thread, one connection. Islands interleave arbitrarily.
* **Nothing is dropped:** a missing record makes a graph wrong in a way nothing downstream can
  detect. A slow Redis fills the buffer and the island waits (asynchronous islands tolerate one
  running slower); nothing acknowledged for `BROKER_STALL_SECONDS` fails the island. No trimming,
  `noeviction`.
* **Nothing is written twice:** after a lost reply the writer kills the old connection on the
  server, so a queued batch cannot land late, then looks for the batch's first record before
  resending. redis-py's own retry, which resends blindly, is off.
* **`run_start` is first:** the master checks that Redis answers and the stream is empty, and
  writes nothing at all before `run_start`, so a reused id or an unreachable Redis fails the run
  before any container starts. Each island checks Redis before it evolves.
* **`run_end` is last:** an island's shutdown is `island_end`, a flush that returns once Redis has
  acknowledged everything, then its goodbye. The master writes `run_end` after every goodbye, exit,
  or kill after `BYE_TIMEOUT_SECONDS`; it stops its control server first and closes its writer
  right after.
* **The password never reaches a record:** `run_start` shows `***`.

---

## 6. Asynchrony

An island sends every `migration.interval` generations but checks its inbox every generation.
That is for measurement: with polling tied to sending, `generational_drift` would mostly measure
how long a migrant waited for the destination's next migration point, an artefact of the cadence.

---

## 7. What the master does not do

**No fault tolerance**: no liveness monitoring, fault events, restarts or recovery. A deliberate
scope decision; the mechanisms that once described dying nodes were removed rather than left
half-built, because a half-built one invites belief.

`Master._supervise` polls until every island has said goodbye or exited, the run is cancelled, or
the time limit passes. Stopping (cancel, time limit, master error) asks each island twice, the
heartbeat flag and a SIGTERM that only sets a flag checked at the end of its generation, and kills
it after `BYE_TIMEOUT_SECONDS`.

| Outcome | Reported as |
| --- | --- |
| every island said goodbye cleanly | `run_end`: `completed` |
| cancelled | islands wind down; `aborted` |
| time limit | islands wind down; `time_limit`, with `error` |
| an island raised in its loop | its `diagnostic: error` and `island_end` (`error`); its goodbye carries the error |
| an island lost Redis | no `island_end`; its goodbye carries the error; exit 1 |
| an island died without a goodbye | its exit code and last output in an error diagnostic |
| any of the three above, or a master error | `error`, the reason in the diagnostic before `run_end` and in `run_end.error` |
| the master's own writer failed | the islands are stopped, the master exits 1; the backend's `abort` ends the stream |

A vanished island is not announced, but not hidden: no `island_end`, `finished: false` in
`run_end.per_island`, dangling sends to it. Its unspent budget is not redistributed, which would
make island balance depend on which node failed; the run ends under budget. The master does
volunteer a diagnostic when **no** island ever connected (a networking problem that otherwise shows
only as no migration). Containers are created without `auto_remove` and removed explicitly, so an
exited one can still be inspected while the run finishes.

---

## 8. Registries, and why typos are errors

`benchmarks.registry` and `algorithms.registry` map names to factories, so adding either is one
registration. Both **reject a parameter the target does not accept**, walking the constructor's
MRO so `**params` cannot reopen the hole, at validation time: `dimensionn: 50` would otherwise run a
sweep at the default dimension. `RunConfig` forbids extra fields throughout, and an unpublished
override key is a 422.

---

## 9. Seeding

`seed` is optional. Without it, each island draws 64 bits of OS entropy. With it, nothing draws
entropy: every stream derives from it by `numpy.random.SeedSequence` spawn keys, which are
independent (`seed + island_id` streams can overlap, so it is not used). One stream per island
drives its operators, migrant selection and replacement, diversity sampling and jitter; the broker
has one stream per **source** island for `fanout: one_random`, so island *k*'s *n*-th migration
always picks the same destination whatever the arrival order. `island_start.island_seed` records
each island's seed.

**What a seed reproduces:** without migration, everything (identical evaluation records apart from
ids and timestamps). With migration, each island's random stream but not its trajectory: a migrant
joins on whatever generation its destination has reached, a fact about container scheduling.
Making that identical would need synchronous migration, which changes the model being studied.

---

## 10. Deliberately not done

* **Intra-island parallelism**: out of scope; each island is single-threaded apart from its
  writer's pump.
* **Hardware telemetry**: interleaved into the search record, it made every consumer filter it out.
  `run_start.host` has the provenance; whether telemetry returns is open (`HANDOFF.md` §8).
* **Bit-identical runs with migration**: see §9; the dataset is pinned regardless.
* **Repair operators**: out of scope; infeasibility is a graded penalty (knapsack) or bound
  clipping.
* **Non-island topologies** (master–slave, cellular): future work.
* **A global sequence number**: it would need the cross-container synchronisation the design
  avoids; order is per emitter, plus clock-corrected `t_wall`.
* **Multi-objective**: every algorithm is single-objective, so records carry one `fitness`, no
  objective vector.
* **A versioned record format**: there is one format, and records carry no version.
