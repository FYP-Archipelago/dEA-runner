# The runner's streaming contract: how it works now, what the backend wants, and what has to change

> **Historical record (October 2026).** This review describes the runner *as it was* before the
> rewrite — CSV evaluation rows, two sockets per island, a log relay through the master, log
> files — and argues for the changes. Most of them are now built (modules M4.1–M4.3 of
> `docs/IMPLEMENTATION_PLAN.md`). For how the runner works today, read `ARCHITECTURE.md` and
> `LOGGING_FORMAT.md`; for what was finally agreed with the backend, `HANDOFF.md` §9. **Names here predate the
> platform's naming:** "worker" here means an island; in current documents a worker is always a
> Worker Pool worker. The stream is `run:{id}:runner_logs`, as here. Kept as the decision record;
> not updated.

A response to `REQS.md`. This covers the **streaming half** only — how a run's log records get
out of the runner and into Redis. The service interface (`/presets`, `POST /runs`), the preset
list and the override whitelist are deliberately not reviewed here, so please don't read
silence on those as agreement.

Written to be readable without knowing the runner's internals. **Part 1** explains how the
thing works today from the ground up; skip it if you already know. **Part 2** restates what
the backend is asking for. **Part 3** goes requirement by requirement. **Part 4** works through
the direct-to-Redis proposal. **Part 5** is the list of decisions someone has to make.

Everything here was checked against the code at commit `ef031e2`, and measured where a
measurement was possible. File and line references point at the current tree.

---
---

# Part 1 — How the runner works today

## 1.1 The island model

A run is **one master process and N island processes** (N defaults to 5, constrained to 4–6).

- An **island** is one independent evolutionary algorithm with its own population. It
  initialises a population, then loops: produce offspring, evaluate them, select survivors.
  That loop body is one **generation**.
- The **master** does not evolve anything. It starts the islands, brokers **migration** between
  them, and collects their logs.

Islands are **asynchronous**. Nothing synchronises generations, so at any wall-clock instant
island 2 might be on generation 40 while island 3 is on generation 12. This matters later: it
is why ordering guarantees are stated *per island* and never across islands.

**Migration** is the mechanism the whole Archipelago project exists to explain. Every
`migration.interval` generations, an island selects a few individuals and sends them to its
neighbours in a topology (ring by default). The master is the broker: an island hands migrants
to the master, the master queues them for the destination, and the destination collects them
on its next poll.

```
                      ┌────────────────┐
                      │     master     │   migration broker + log collector
                      └────────────────┘
                       ▲   ▲    ▲    ▲
                       │   │    │    │        control + log connections
            ┌──────────┘   │    │    └──────────┐
            │              │    │               │
      ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
      │ island 0 │  │ island 1 │  │ island 2 │  │ island 3 │
      │  own pop │  │  own pop │  │  own pop │  │  own pop │
      └──────────┘  └──────────┘  └──────────┘  └──────────┘
            └──────────────▶──────────────▶──────────────┘
                    migration (brokered by the master)
```

Under the `docker` backend each island is its own container, created per run. Under the
`local` backend each island is a subprocess. Same code, same protocol either way.

## 1.2 The three connections between an island and the master

Each island opens **three** separate TCP connections to the master. This is the single most
important fact in this document, because requirement §3.1 breaks on it.

| Role | Direction | Carries |
| --- | --- | --- |
| `CONTROL` | request/response | migration traffic and lifecycle: `TIME`, `READY`, `MIGRATE`, `POLL`, `HEARTBEAT`, `BYE` |
| `LOG` | island → master, one way | every **event** record this island produces |
| `EVALUATIONS` | island → master, one way | every **evaluation row** this island produces |

The split between `LOG` and `EVALUATIONS` exists only because the master writes them to two
different *files* in two different *formats*. It has no other justification, and §3.1 is the
bill for it.

The `CONTROL` channel is request/response and is how the master knows what an island is doing.
`HEARTBEAT` carries the island's progress and brings back a "wind down" flag. `BYE` is the
island saying it is finished — remember this one, it comes back in §4.3.

## 1.3 What a "record" is

A run emits nine kinds of record:

| Type | Emitted by | When | Roughly how many |
| --- | --- | --- | --- |
| `run_start` | master | once, before islands launch | 1 |
| `island_start` | island | once, when it comes up | N |
| `evaluation` | island | **every single fitness evaluation** | ~the entire evaluation budget |
| `generation_end` | island | once per generation | N × generations |
| `migration_send` | island | when it dispatches migrants | a few dozen |
| `migration_arrive` | island | when it integrates migrants | a few dozen |
| `island_end` | island | once, as it finishes | N |
| `run_end` | master | once, at the very end | 1 |
| `diagnostic` | master **and** islands | free-form notes | ~14 sites in the code |

`evaluation` dominates by three or four orders of magnitude. A 100,000-evaluation run produces
100,000 evaluation records and perhaps 500 of everything else combined. Any design decision
about volume is really a decision about `evaluation`.

## 1.4 Where evaluation records are born

There is exactly one place: `IslandContext.evaluate()` at `algorithms/base.py:107`. Every
algorithm — GA, DE, PSO, GP, ES — calls it to score an individual, and it is the only path to
the objective function.

That chokepoint is why the logs are useful at all. At the moment of evaluation the context
knows the individual's `parent_ids` and the `operator` that produced it (`de_trial`,
`crossover`, `subtree_mutation`, …). Those two fields are **one edge in the search trajectory
graph**. The clustering phase's entire input is this: a node derived from the genome, and an
edge derived from `parent_ids`.

## 1.5 The `seq` counter

Every record carries `seq`, assigned by `EventLogger.emit()` in `archipelago_logging/logger.py`:

```python
with self._lock:
    fields["seq"] = self._seq
    self._seq += 1
    event = model(**fields)
    if self.evaluation_sink is not None and isinstance(event, EvaluationEvent):
        self.evaluation_sink.write(format_evaluation_csv_row(event))   # CSV row
    else:
        self.sink.write(event.to_json_line())                          # JSON line
```

Three properties follow, and all three matter:

1. **`seq` is per-emitter, not global.** Each island has its own counter starting at 0, and the
   master has its own. There is no run-wide sequence number. "Island 3 record #41" is
   meaningful; "run record #41" is not.
2. **`seq` is shared between evaluation rows and events.** A single counter covers both. If
   island 3 emits an evaluation, a `generation_end`, then two more evaluations, those are
   `seq` 40, 41, 42, 43. This is what lets a consumer place a migration event *between* two
   specific evaluations.
3. **Assignment and the write happen inside the same lock.** The record is handed to its sink
   before the lock is released. So whatever is downstream of that call receives records in
   exact `seq` order — *if* there is only one "whatever".

There is not. See 1.7.

## 1.6 Why there are two record formats

Historical, and now vestigial.

- **Events** are serialised as **JSON objects**, one per line → `run.jsonl`.
- **Evaluation records** are serialised as **CSV rows**, 23 fixed columns → `evaluations.csv`.

The CSV exists because `evaluation` is the high-volume record and CSV is far more compact than
JSON when you are writing a hundred thousand of them to a file: no repeated field names, no
braces. That was a sound trade *for files*.

The backend is deleting the files. Once records go to Redis as structured entries, the reason
for CSV evaporates — and §3.5 shows that keeping it is now actively the worst of the available
options.

## 1.7 How records reach disk today — and where ordering dies

Each island builds **two** sink fan-outs (`island_runner.py:80–130`):

```
   EventLogger.emit()        ← assigns seq under one lock
          │
          ├── is it an evaluation? ──── yes ──▶ evaluation_sink
          │                                        │
          │                                        ├─▶ CsvFileSink   island-03.evaluations.csv
          │                                        └─▶ NetworkSink ──┐   role = EVALUATIONS
          │                                                          │   own queue, own thread
          └── no ──▶ sink                                            │
                       │                                             │
                       ├─▶ JsonlFileSink  island-03.jsonl            │
                       └─▶ NetworkSink ───────────────────┐          │
                                                          │          │
                               role = LOG                 │          │
                               own queue, own thread      │          │
                                                          ▼          ▼
                                                     ╔═══════════════════╗
                                                     ║      master       ║
                                                     ║ one handler thread║
                                                     ║   per connection  ║
                                                     ╚═══════════════════╝
                                                          │          │
                                                   run.jsonl    evaluations.csv
```

The lock in `emit()` guarantees records enter this picture in `seq` order. But they immediately
diverge into **two independent queues, drained by two independent threads, over two independent
sockets, reassembled by two independent handler threads on the master**.

Nothing reunites them in order. That is §3.1.

## 1.8 The lifecycle of a run, in order

```
master    run_start                                    ← before any island exists
master    diagnostic  "island worker launched" × N
          │
island k  island_start
island k  generation_end (generation 0, the initial population)
          │
          ├─ loop ─────────────────────────────────────────────────┐
          │    evaluation × population_size      ← the bulk        │
          │    migration_send      (every `interval` generations)  │
          │    migration_arrive    (when migrants are waiting)     │
          │    generation_end                                      │
          └────────────────────────────────────────────────────────┘
          │
island k  island_end                                   ← island_runner.py:629
island k  BYE on the control channel                   ← island_runner.py:647  ⚠ see §4.3
          │
master    run_end                                      ← master.py _shutdown
```

Termination is per island: an island stops on its evaluation budget, on stagnation (no
improvement for M generations), on reaching a target fitness, or on a generation cap. The
master stops when all islands have stopped, or on a wall-clock limit.

---
---

# Part 2 — What the backend is asking for

Restated plainly, grouped by what they actually constrain.

**Destination.** Every record goes to one Redis stream per run,
`run:{run_id}:runner_logs`. No files. The `run_id` comes from the backend, not the runner.

**Granularity.** One Redis stream entry per record. Proposed fields: `kind` (`row` or `event`)
and `data` (the record as a JSON object).

**Completeness.** Every evaluation row and every event is appended. Nothing dropped, nothing
sampled. `genome_repr` must be present on every row.

**Ordering.** Per island, records must arrive in `seq` order, with rows and events interleaved
correctly because they share one counter. Different islands may interleave freely.

**Boundaries.** `run_start` is the first entry in the stream; `run_end` is the last; nothing is
appended after `run_end`.

**Liveness.** Appends happen continuously during the run, not batched at the end.

**Fields.** A specific list of row fields and event fields the clustering module reads.

**Failure.** A failed run reports `failed` with readable error text. The backend — not the
runner — writes an `abort` entry when it detects a dead runner.

---
---

# Part 3 — Requirement by requirement

## Verdict summary

| # | Requirement | Verdict |
| --- | --- | --- |
| 3.1 | Per-island `seq` arrival ordering | 🔴 **impossible today — architectural** |
| 3.2 | `run_start` first, `run_end` last | 🔴 **true by luck, not by design** |
| 3.3 | Nothing dropped or sampled | 🟠 **reverses the current safety invariant** |
| 3.4 | One entry per record | ✅ **agree — and measurements back it** |
| 3.5 | `data` = CSV row as a JSON dict | 🟠 **worst of three options** |
| 3.6 | Row and event field lists | ✅ **every field already exists** |
| 3.7 | Live appends | ✅ **already true, with a caveat about wording** |

---

## 3.1 🔴 Per-island `seq` ordering

### What is being asked

> *"Ordering: per island, records must arrive in `seq` order. Rows and events share one
> per-island `seq` counter."*

The clustering module builds a trajectory graph. It needs to know that a migration arrived
*between* evaluation #40 and #41 on island 3, because that is the edge that makes a migration
visible in the graph. `seq` is how it knows.

### What happens today

The second sentence is correct — §1.5 — and the first is **false**, for the reason in §1.7.
Two queues, two threads, two sockets, two handlers. Once a record leaves the lock in `emit()`,
nothing preserves the relationship between the two streams.

### Evidence

I ran a 4-island run, published every record to one Redis stream in exact arrival order, and
checked each island's `seq` sequence for inversions:

```
island 0:   517 records    8 inversions, worst gap 32
island 1:   526 records   10 inversions, worst gap 24
island 2:   526 records    9 inversions, worst gap 24
island 3:   517 records   12 inversions, worst gap 25
                          ── 39 inversions total
```

Then the same data split by kind:

```
island 0 event         31 records   IN ORDER
island 0 evaluation   486 records   IN ORDER
```

Each stream is perfectly ordered on its own. Only the merge is broken. A real inversion from
that run:

```
seq=38   evaluation
seq=39   evaluation
seq=40   evaluation
seq=25   event        ← migration_arrive, arriving 15 records late
seq=41   evaluation
seq=42   evaluation
```

That migration genuinely happened at `seq=25`, between evaluations 24 and 26. A consumer
reading arrival order places it between 40 and 41. The resulting graph has the migration edge
attached to the wrong node — and nothing about the data looks wrong.

And this was a **tiny** run: 2,000 evaluations, four islands, everything on one host with no
network latency. Inversions scale with how far the two queues can drift apart, so a real run
across containers will be worse, not better.

### What has to change

The requirement is written as though it describes current behaviour. It does not, and no amount
of configuration gets there. **One ordered channel per island** is the fix, and §1.5 says why it
works: `emit()` hands the record to its sink inside the lock, so a single downstream queue
receives records in exact `seq` order for free. The two-socket split is the only thing breaking
it.

Two ways to get one channel:

- **(a) Merge the two sockets.** Island keeps talking only to the master; both record kinds go
  down one connection with a tag. Smallest change, keeps the master as log collector.
- **(b) Islands write to Redis directly.** The master stops relaying logs entirely. Larger
  change, deletes more code. **This is the backend's proposal — Part 4.**

Either fixes 3.1. (b) is better, for reasons in Part 4.

---

## 3.2 🔴 `run_start` first, `run_end` last

### What is being asked

> *"`run_start` is the first thing in the stream and `run_end` the last. After `run_end`,
> nothing more is appended."*

Reasonable: it gives the consumer an unambiguous start and a terminator, so it knows when to
stop reading and finalise.

### What happens today

**`run_start` first is genuinely safe.** The master writes it before launching any island
(§1.8), and an island that does not exist cannot write.

**`run_end` last is not.** The shutdown sequence in `master.py::_shutdown` is:

```
step  83   logger.run_end(...)          ← written here
step 100   backend.stop_all(grace)      ← islands asked to stop here
step 102   server.stop()                ← island connections closed here
```

`run_end` is written while every island is **still connected and still able to stream**. In my
test run nothing appeared after it — but only because the supervisor had already waited for the
islands to exit before calling `_shutdown`. That is luck, not a guarantee. It breaks on:

- **the wall-clock timeout path** — the supervisor asks islands to stop and returns immediately.
  Islands then get `shutdown_grace_seconds` (20 s by default) to finish their current generation
  and flush. All of that lands **after** `run_end`.
- **the error path** — `_shutdown` runs from a `finally`, with islands fully alive.

So the common case is fine and the two failure cases are exactly when you most want the log to
be trustworthy.

### What has to change

An explicit barrier. The master must not write `run_end` until every island's records are
*known to have landed*, not merely until the island process exited. §4.3 shows that today's
obvious signal for this — `BYE` — is sent at the wrong moment, and that the fix is a one-line
reorder.

Plus a bounded wait, so one island dying without ever saying goodbye cannot hang the run.

---

## 3.3 🟠 Nothing dropped or sampled

### What is being asked

> *"Every evaluation row and every event is appended... Nothing dropped or sampled."*

Correct for the use case. The clustering module reconstructs trajectories; a missing evaluation
is a missing node, and a missing node silently corrupts the graph.

### What happens today — and why

The runner currently guarantees the **opposite**, deliberately. Every outbound queue is bounded;
on overflow it drops the record and increments a counter. The reasoning was:

> A slow or absent log consumer must never be able to change the search. If the queue blocks,
> the evolutionary loop blocks, and the algorithm's behaviour becomes a function of how fast
> the dashboard is. Then the run is not reproducible and not comparable to other runs.

That is a defensible position when logs are a convenience and the files on disk are the real
output. **It is the wrong position once the stream is the only output.** `REQS.md` L100–102
spots the drop behaviour but files it under "remove a toggle". It is not a toggle. It is the
invariant the whole logging path is built around, and reversing it is the single largest
behavioural change in this document.

### The decision this forces

Once dropping is forbidden, a stalled broker leaves exactly two options:

| Option | What happens | Cost |
| --- | --- | --- |
| **Block the EA** | Back-pressure reaches the evolutionary loop; islands slow to broker speed | Run duration becomes a function of infrastructure health. Wall-clock timings are no longer comparable between runs. |
| **Fail the run** | Runner gives up, reports `failed` | Wasted compute, but loud and unambiguous |

**My recommendation: bounded buffer → block → fail on a timeout.** Block first, because short
stalls are normal and a few seconds of back-pressure is harmless. Fail if the stall exceeds a
threshold, because an indefinitely blocked run is worse than a dead one. Never drop, because a
graph with holes is *silently* wrong and nothing downstream can detect it.

Whoever owns "a run must finish in N minutes" needs to agree, since this makes run duration
partly an infrastructure property.

### The part that is easy to miss

**`maxlen` is dropping under another name.** Redis stream trimming discards the oldest entries
as new ones arrive. If the stream is trimmed while the clustering worker is still catching up,
records are lost exactly as a full queue loses them — and more insidiously, because nothing in
the runner even knows it happened.

Under "nothing dropped", `maxlen` must be **unbounded**, or provably larger than the largest
run. A 100,000-evaluation run is ~100,000 entries, so a cap of 1,000,000 is safe for current
budgets and unsafe the moment someone runs a 10× experiment. This ties directly to open
question 8 (how long are raw logs kept?) — see §5.

---

## 3.4 ✅ One entry per record

### What is being asked

> *"One stream entry per record."*

The alternative would be packing many records into one entry. I benchmarked both against a real
Redis container — 50,000 records averaging 235 bytes, batch size 500:

| Shape | Throughput | Entries | Memory |
| --- | --- | --- | --- |
| one entry per record (pipelined) | 76k rec/s | 50,000 | 294 B/rec |
| packed, JSON array | 851k rec/s | 100 | 329 B/rec |
| packed, newline-joined | 2.4M rec/s | 100 | 263 B/rec |

Packing is **11–32× faster**. That is a real result and I'd rather report it than bury it.

### Why one-per-record is still right

**The throughput is irrelevant here.** A 5-island Docker run produces about **3,000
records/second**. The slowest option is 25× faster than needed. Optimising it would be work on
a bottleneck that is two orders of magnitude away from being one.

**And packing costs three things that do matter:**

**(i) It destroys worker-pool balance.** Redis consumer groups distribute work *per entry*. If
an entry holds 500 records, your unit of parallelism is 500 records. Replaying the real batch
sizes one run produced — `[1, 4, 500, 500, 500, 400, 16]` — across three workers:

```
packed:      worker-0  26.9%    worker-1  26.2%    worker-2  46.9%
individual:  worker-0  33.3%    worker-1  33.3%    worker-2  33.3%
```

The busiest worker does 2× the quietest, because the *producer's* flush timer silently decided
the *consumer's* work unit. That is exactly the coupling you don't want between two services.

**(ii) Redelivery gets expensive.** A worker that dies mid-entry replays 1 record, or up to 500.

**(iii) `maxlen` stops meaning anything.** `maxlen: 1000000` currently retains exactly a million
records. Packed, it retains a million *batches* — somewhere between 1M and 500M records,
depending on run intensity. You lose the ability to reason about broker memory at all, which
§3.3 just established is load-bearing.

### The distinction that matters

Batching at the **transport** layer (one pipelined round trip carrying N entries) and packing at
the **message** layer (one entry containing N records) are independent choices. You can have the
first without the second, and that is what the runner should do: one network round trip per
~200 records, but still one entry per record. You get the latency amortisation and none of the
granularity cost.

---

## 3.5 🟠 `data` = CSV row as a JSON dict

### What is being asked

> *Proposal: fields `kind` (`row` or `event`) and `data` (the record as one JSON object: the
> CSV row as a dict, or the `run.jsonl` line).*

### The problem

This asks the runner to build a CSV row, then parse it back, then zip it against the column
list, then encode it as JSON. Three format conversions to produce something the runner already
had in hand — the `EvaluationEvent` object, before anyone serialised it.

Measured on real rows:

| Shape | Mean size | vs CSV |
| --- | --- | --- |
| CSV row, verbatim | 386 B | 1.00× |
| **CSV parsed into a JSON dict** (proposed) | **766 B** | **1.98×** |
| Native `EvaluationEvent.to_json_line()` | 700 B | 1.82× |

The proposal is the **largest** of the three *and* the most work. It pays full JSON size and a
round trip, for nothing.

### Counter-proposal

Keep `kind` — it is cheap and useful for routing. Make `data` **the event's own JSON line** for
both kinds.

What this buys:

- **Smaller** than the proposal (700 B vs 766 B) for identical information.
- **Lossless** — I verified the CSV → `EvaluationEvent` round trip reproduces the model exactly.
- **Already carries `type`**, so `kind` becomes a convenience rather than the only discriminator.
- **Deletes the positional column contract.** No `evaluation_columns` header to ship, no
  agreement about column order, no breakage when a column is added.
- **Deletes CSV quoting and escaping** — `parent_ids` is a `|`-joined list inside a CSV field
  inside a JSON string in the proposal. Two layers of escaping, both removable.
- **Makes §3.1's fix trivial.** One channel, one serializer, one record shape. The two formats
  are a large part of why there are two sockets in the first place.

What it costs: **1.82× the bytes of raw CSV** — roughly 70 MB instead of 39 MB in Redis for a
100,000-evaluation run. Since the backend deletes the stream shortly after clustering, that
seems clearly acceptable. If it is not, the lever is `genome_repr` (which is the bulk of a row),
not the envelope around it.

---

## 3.6 ✅ Field lists

Every field named in `REQS.md` already exists. I checked each one rather than assuming.

**Row fields.** `EVALUATION_CSV_COLUMNS` has 23 entries; all twelve required are present:
`island_id, seq, eval_index, generation, t_wall, individual_id, parent_ids, fitness,
genome_encoding, genome_hash, genome_repr, genome_repr_mode`.

**Event fields:**

| Event | Required by the module | Status |
| --- | --- | --- |
| `run_start` | `run_id, schema_version, algorithm, benchmark, migration.topology, num_islands, evaluation_budget, evaluation_budget_per_island, datasets.maximising` | ✅ — but `migration.topology` and `datasets.maximising` are **nested**, so read them by path, not as flat keys |
| `island_start` | `island_id, genome_encoding, benchmark_params, clock_offset_ns` | ✅ all four |
| `generation_end` | `maximising` | ✅ |
| `migration_arrive` | `island_id, seq, t_wall, origin_individual_ids, arrived_individual_ids` | ✅ all five |
| `island_end`, `run_end` | — | ✅ |

**`schema_version`** is on *every* record, not just `run_start`.

**One caveat on `genome_repr`.** The requirement that it is always present is satisfied under
`genome_mode: compact` or `full`, and violated by `genome_mode: hashed` and by
`genome_sample_every > 1` (which writes the genome body only every n-th row). `REQS.md` L94–97
already proposes removing both. Agreed — they should not be settable.

---

## 3.7 ✅ Live appends — with a wording caveat

> *"Appends should happen continuously during the run (live), not batched at the end."*

Already true. But two different things are being conflated and the requirement should say which
it means:

- **Batching at the end** — accumulate everything, write once when the run finishes. Nobody is
  doing this and nobody should.
- **Batching for transport** — accumulate for ~100–250 ms or ~200 records, then send as one
  pipelined round trip. This *is* live: bounded latency, continuous delivery. §3.4 shows why it
  is the right call.

If the requirement means "one network write per record", that needs to be stated explicitly,
because it is a much more expensive ask and §3.4 argues against it on measured grounds. I read
the backend's own proposal (`flush every ~100-250 ms or every ~200 records`) as agreeing that
transport batching is fine, which resolves this.

---
---

# Part 4 — The direct-to-Redis proposal

> *Each island appends its own records straight to `run:{id}:runner_logs` over one pipelined
> Redis connection per island. One connection per island keeps that island's `seq` order for
> free. Flush every ~100–250 ms or every ~200 records, whichever comes first, plus a final flush
> before `island_end`. The master writes `run_start` before any island starts and `run_end`
> after every island has ended.*

## 4.1 Assessment: take it

This is better than merging the two sockets (§3.1 option (a)). That option fixes the ordering
bug; this one **removes the thing that caused it**. The master stops being a log relay
altogether — `Role.LOG`, `Role.EVALUATIONS`, both master-side aggregate sinks and both handler
threads all disappear. Ordering stops being a property you maintain and becomes a property of
the topology.

```
BEFORE                                  AFTER

island ──┬─ events ──────┐              island ──── all records ────▶ Redis
         └─ rows ────────┤                           (one connection,
                         ▼                            one queue, one
                      master                          thread, ordered)
                         │
                  run.jsonl + CSV       master ───── its own records ──▶ Redis
                         │                          (run_start, run_end,
                         ▼                           diagnostics)
                      Redis
```

It also makes the master's remaining job coherent: brokering migration, which is what it is for.

Four things it needs to be correct. One of them would fail silently.

## 4.2 Why it actually fixes ordering

Worth being explicit, because "one connection per island" is necessary but not sufficient.

From §1.5: `emit()` assigns `seq` and writes to the sink **inside one lock**. So if there is a
single queue downstream, records enter it in exact `seq` order. A queue is FIFO. A single pump
thread drains it in order. Redis pipelining on one connection preserves submission order. Every
link holds.

The one way to break it: **more than one thread writing to the same connection**, or more than
one connection per island. Both would reintroduce the race. The implementation needs a single
pump thread per island connection, and that should be stated, not assumed.

## 4.3 🔴 `BYE` is sent before the final flush

This is the one that fails silently, and it defeats the proposal's own guarantee.

The master's signal that an island has finished is `BYE` on the control channel. Today, in
`island_runner.py`:

```
line 629    logger.island_end(...)       ← queued, not yet sent
line 647    _finish():
              control.bye(...)           ← master now believes this island is DONE
              control.close()
              logger.flush()             ← the island's records actually land HERE
              logger.close()
```

`BYE` fires **before** the flush.

Under the current architecture this is harmless: the master owns the log sockets, so it knows
when an island's stream is really finished — the socket closes.

Under direct-to-Redis the master loses that visibility entirely. It would collect every `BYE`,
conclude all islands have ended, write `run_end` — while islands are still pushing their last
~200 buffered records into Redis behind it. That is precisely the §3.2 violation the proposal
sets out to fix, and **nothing in the resulting stream would look wrong**. You would just
sometimes find records after `run_end`, depending on timing.

**The fix is a one-line reorder:** flush and close the logger, *then* send `BYE`. That changes
what `BYE` means, from

> "my process is about to exit"

to

> **"every record I will ever produce is durably in Redis"**

which is exactly the predicate the master needs before writing `run_end`.

The requirement should say so, because *"after every island has ended"* naturally reads as
"after every island process exited", and process exit is the wrong event to wait for.

**Also needs a bounded wait.** An island that crashes without sending `BYE` must not hang the
master forever. Timeout → write `run_end` regardless → the "first terminator is authoritative"
rule (§5) covers the zombie that writes afterwards.

## 4.4 Second-order: "final flush before `island_end`" is backwards

Minor, but it is about the exact record the master waits on.

Submit `island_end` **first**, then flush. Flushing first leaves `island_end` sitting in the
buffer as the one record that did not make it out — and combined with §4.3, the master would be
waiting for a record that is still in a queue.

Correct order: `island_end` → final flush → `BYE`.

## 4.5 🟠 The real cost: islands need a route to Redis, and the credentials

This changes the deployment, not just the code, and is the part most worth pushing back on.

**Network.** Today an island container's only dependency is the master's control socket, and
the docker backend puts islands on a **private bridge network created per run** —
`archipelago-{run_id[-8:]}` (`docker_backend.py:84`). Redis is not on that network and has no
reason to be. Direct writes need one of:

- attach the Redis container to each run's network as it is created, or
- put islands on a shared long-lived network alongside Redis, or
- use host networking throughout.

None are hard. All are decisions somebody has to own, and the third has security implications.

**Credentials.** Every island container now needs the Redis URL, possibly with a password.
Today only the master has it. Note that `requirements-worker.txt` *deliberately* excludes the
Docker SDK so that a buggy or compromised island cannot create containers — the same reasoning
applies here. This widens the blast radius from one process to N. Probably fine; should be a
conscious yes rather than a silent one.

**Failure domains.** Each island now depends on **two** services instead of one. An island that
cannot reach Redis must report that over the control channel, or nobody finds out — the master
no longer observes a log socket failing to connect. That needs an explicit diagnostic path.

## 4.6 The master writes more than `run_start` and `run_end`

The proposal says the master writes the two bookends. It also emits **diagnostics** from 14
sites across `master.py`, `control_server.py`, `island_runner.py` and `worker.py`:

- *"island worker launched"*
- *"migration queue full; dropped the oldest pending transfer"*
- *"archipelago evaluation budget reached; asking islands to wind down"*
- *"no island ever connected to the master, so no migration took place"*

These are `run.jsonl` records today. The clustering module may not read them, but they are the
only in-band record of **why a run behaved oddly** — that last one in particular is how you
diagnose a run that produced zero migrations. The master needs its own connection, not two
isolated writes. Worth stating explicitly so it doesn't get dropped during implementation.

**Migration events are emitted by islands, not the master** (`island_runner.py:346` and `:411`),
so they ride the island connections with no special handling. A consequence: `migration_send` on
island A and `migration_arrive` on island B land on different connections with no relative
ordering. That is allowed — the requirement permits islands to interleave freely — and the two
halves join on `migration_id`.

## 4.7 What gets better

**Back-pressure degrades more gracefully.** Under §3.3's "block, don't drop", an island blocked
on *its own* connection stalls only itself. The other islands keep evolving. In the island model
that is already a legal state (§1.1 — islands are asynchronous by design), so the §3.3 failure
mode degrades into something the system is explicitly built to tolerate. Far better than a
shared master queue, where one stall affects everyone.

**Less code.** This deletes the master's log-collection path rather than fixing it.

## 4.8 One ask in return

**Keep the publisher provider abstraction when this moves into the islands.**

Right now the entire end-to-end test suite runs with **no broker at all**. If islands hard-code
a Redis client, every one of those tests needs a live Redis — a significant tax on a suite that
currently runs anywhere, including CI with no services.

With a swappable provider, an island publishes to an in-memory implementation under test and to
Redis in production. The §3.1 ordering guarantee — the thing most worth regression-testing —
stays testable without infrastructure. The provider seam costs one interface and buys that.

---
---

# Part 5 — Open questions, decisions and work

## Open question 3 — who writes `abort`

The proposal (backend writes it) is right for the case it targets: a runner that has crashed
genuinely cannot report its own death.

But it collides with §3.2's *"nothing is appended after `run_end`"*. There is a race: the runner
writes `run_end` at the same moment the backend's 2-second poll concludes it is dead and writes
`abort`. Both entries exist, in either order.

**Rule needed: the consumer treats the first terminator it sees as authoritative** and ignores
anything after it.

And the division of labour should be sharper. The runner is a supervisor — it catches most
failures itself (island crash, bad config, broker unreachable). It **should** write `run_end`
with `termination_reason: error` whenever it can. `abort` should be strictly a fallback for
*"no terminator appeared within N seconds"*, not the normal failure path. Otherwise the backend
ends up inferring failure reasons the runner already knew precisely.

## Open questions 5 and 8 — seeds and retention are the same question

Stated separately in `REQS.md`, but they interact badly:

- Runs are **not reproducible** — islands seed from OS entropy (open question 5).
- The stream is **deleted** shortly after clustering (open question 8).

Together, a result can never be reproduced, re-clustered, or audited. Once the stream is gone,
the only record is the clustered output, and the run that produced it cannot be recreated.

For a GECCO-target paper that is a problem well beyond the runner — reproducibility is a
reviewer's first question. **Take the seed option.** It is cheap to add, and it is precisely
what makes "delete the stream" a safe choice rather than a lossy one.

## Not flagged by anyone yet

**Backend-minted `run_id` collides with Docker naming.** The docker backend derives container
and network names from `run_id[-8:]` (`docker_backend.py:84` and `:256`). With ids minted by the
backend and several runs in flight (`REQS.md` L28), two run ids that happen to share their last
8 characters produce a **container name collision and a failed launch**. Docker's name charset
(`[a-zA-Z0-9][a-zA-Z0-9_.-]*`) also constrains which ids are even legal.

Either the backend commits to a format rule, or that naming scheme changes. Worth deciding
before concurrent runs ship.

Good news: `individual_id` is `i{island}-g{gen}-n{ordinal}` and does **not** embed `run_id`, so
a backend-supplied id has no effect on record identity.

**`validate_events`** (`REQS.md` L98–99) is the only thing standing between a malformed record
and the clustering worker. Now that the contract guarantees specific fields on every record,
I'd keep it always-on and revisit only if it is ever a *measured* cost. Nobody has measured it,
including me.

## Decisions someone has to make

| # | Decision | Depends on | Owner |
| --- | --- | --- | --- |
| 1 | Back-pressure policy: block the EA, or fail the run? | §3.3 | backend + whoever owns run-duration expectations |
| 2 | `maxlen` unbounded, or sized above the largest run? | §3.3, OQ8 | backend |
| 3 | `data` = native event JSON instead of CSV-as-dict? | §3.5 | backend + clustering |
| 4 | How do islands reach Redis — per-run network, shared network, or host? | §4.5 | runner + infra |
| 5 | Add a seed override? | OQ5 + OQ8 | team — blocks reproducibility |
| 6 | `run_id` format rule, or change Docker naming? | §5 | backend + runner |
| 7 | Is `abort` fallback-only, with "first terminator wins"? | OQ3 | backend |

## Work the runner owns regardless of those answers

1. **One ordered channel per island** — blocker for §3.1, and the whole reason Part 4 exists.
2. **Reorder `BYE` to after the final flush** — blocker for §3.2. One line; fails silently if missed.
3. **Explicit quiesce barrier before `run_end`**, with a bounded wait for missing `BYE`s — §3.2, §4.3.
4. **Island-side diagnostic when the broker is unreachable** — §4.5, or the failure is invisible.
5. **Keep the provider seam** so islands stay testable without a live broker — §4.8.
6. **Remove `genome_mode: hashed` and `genome_sample_every`** as settable options — §3.6.
