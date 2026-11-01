# Runner reply to `REQS.md`

> **Historical record (October 2026): what we sent the backend, before their reply.** Several
> answers below were later changed, so do not quote this to them as current:
>
> - **§6 container naming:** withdrawn. The backend kept the last 8 hex characters of `run_id`,
>   and the runner validates the id's format instead.
> - **Q6 concurrency:** replaced by the backend's rule — reject only a run that could never fit
>   under the 15-container ceiling; queue the rest.
> - **Q7 `local` backend:** removed (the owner's decision); Docker is the only backend.
> - **§7 and Q8 seeds:** a seed does **not** make a run with migration reproducible record for
>   record — migrants arrive on timing-dependent generations. Seeded runs with migration are
>   statistically comparable, not identical. The backend has not been told yet.
>
> What was finally agreed, and what is still open, is in `HANDOFF.md` §9. Names here predate the
> platform's naming: "worker" here means an island. The stream is `run:{id}:runner_logs`, as here.

What the runner will do, and what we need back from you. Detail and evidence for every verdict
is in [`STREAMING_CONTRACT_REVIEW.md`](STREAMING_CONTRACT_REVIEW.md); this is the short version
plus answers to all eight open questions.

Nothing here is built yet. This is the plan we'd like signed off before we start.

---

## What we're committing to

| Area | Decision |
| --- | --- |
| Ordering | Islands write to Redis **directly**, one connection each |
| Stream boundaries | `run_start` / `run_end` written by the master, with a real barrier before `run_end` |
| Delivery | No record is ever dropped. Back-pressure, then fail — never silent loss |
| Entry shape | `kind` + `data`, where `data` is the record's **native JSON**, not a CSV row converted to a dict |
| Container naming | Derived from a hash of the full `run_id`, never from a substring of it |
| Seeds | Optional `seed` override; per-island seeds derived from it |
| Concurrency | 2 runs at a time, hard ceiling of 15 containers, reject beyond |

---

## 1. Ordering — islands write to Redis directly

We're taking your proposal. Each island opens **one** pipelined Redis connection and appends its
own records to `run:{run_id}:runner_logs`. The master stops relaying logs entirely and goes back
to doing only what it's for: brokering migration.

This gives you the per-island `seq` guarantee by construction rather than by maintenance. The
record is handed to its outbound queue inside the same lock that assigns `seq`, so a single
queue drained by a single thread over a single connection is in exact `seq` order at every link.

Two implementation rules we'll hold ourselves to, because either would silently break it:

- **One connection per island, one pump thread per connection.** Never two threads sharing a
  connection, never two connections per island.
- **Rows and events go down the same connection**, as one record shape (§4).

Flush policy as you proposed: every ~200 records or ~150 ms, whichever comes first.

## 2. Stream boundaries

`run_start` first is already safe — the master writes it before any island exists.

For `run_end` last we need a real barrier, because "every island has ended" has to mean
*"every island's records are in Redis"*, not *"every island's process exited"*. Those are
different moments and the gap between them is exactly where records would land after `run_end`.

What we'll change:

1. An island's shutdown becomes **`island_end` → final flush → `BYE`**, in that order. `BYE` on
   the control channel then carries the meaning the master needs: *everything I will ever write
   is durably in Redis.*
2. The master waits for every island's `BYE` before writing `run_end`.
3. That wait is **bounded**. An island that dies without ever saying goodbye cannot hang the
   run; on timeout the master writes `run_end` anyway and notes which islands went silent.

Point 3 means a crashed island could in principle write a record after `run_end`. That's the
same race as `abort`, and the same rule covers both — see open question 3.

## 3. Delivery guarantee

**No record is ever dropped or sampled.** Every evaluation row and every event that a run
produces appears in the stream exactly once, in order.

We're stating that as a guarantee rather than a best effort because of what the records are
for. The clustering module reconstructs search trajectories: each evaluation is a node, each
`parent_ids` reference is an edge. A record that goes missing doesn't degrade the output — it
produces a graph that is **wrong in a way nothing downstream can detect**. There's no checksum
on a trajectory. A hole looks exactly like a search that genuinely jumped.

So when the broker can't keep up, the runner has two honest options and takes them in order:

1. **Slow down.** The producing island waits. Because islands are already asynchronous by
   design — nothing synchronises their generations — one island running slower than its
   neighbours is a state the island model tolerates natively. The other islands keep evolving.
2. **Fail the run.** If a stall outlasts a configured threshold, the run stops and reports
   `failed` with the broker error. Loud and unambiguous.

What the runner will never do is continue while discarding records, because that trades a
visible problem for an invisible one.

Two consequences worth naming now:

- **Run duration becomes partly a property of broker health.** A degraded Redis makes runs
  slower, not wrong. If anyone is holding the runner to a wall-clock budget, that budget now has
  an infrastructure dependency in it.
- **Stream trimming is a form of dropping.** If `maxlen` trims entries while the clustering
  worker is still reading, records are lost just as surely — and more quietly, because the
  runner never learns it happened. Under this guarantee `maxlen` must be unbounded, or set
  above the largest run you intend to support. A 100k-evaluation run is ~100k entries; a cap of
  1M is fine today and breaks the first time someone runs a 10× experiment. Our preference is
  unbounded plus your existing delete-after-clustering step.

## 4. Entry format — counter-proposal

We'd like to keep `kind` and change what goes in `data`.

**Your proposal:** `data` is the CSV row parsed into a dict.
**Ours:** `data` is the record's own JSON object, for both kinds.

The reason is that the CSV only ever existed because rows were written to a file. It's a compact
on-disk encoding of a record the runner already holds as a structured object. Converting that
object to CSV and then parsing it back into a dict is three format conversions to arrive at
something we had before the first one.

Measured on real rows:

| Shape | Mean size |
| --- | --- |
| CSV row, verbatim | 386 B |
| CSV parsed into a dict (your proposal) | 766 B |
| **Native record JSON (ours)** | **700 B** |

Ours is smaller than the proposal for identical information, and it also removes:

- the **column-order contract** — no `evaluation_columns` header to ship or keep in sync, and
  adding a field later stops being a breaking change
- **two layers of escaping** — today `parent_ids` is a `|`-joined list inside a CSV field inside
  a JSON string
- the **second record shape**, which is a large part of why rows and events travel separately
  today and therefore part of why §1 is needed at all

The cost is ~1.8× the bytes of raw CSV — about 70 MB instead of 39 MB in Redis for a
100k-evaluation run. Given you delete the stream after clustering, that looks like a good trade.
If it isn't, the lever is `genome_repr` (which is most of a row's size), not the envelope.

`kind` stays as `row` | `event` for cheap routing, though note the record's own `type` field
already distinguishes them, so `kind` is a convenience rather than the only discriminator.

## 5. Complications of islands talking to Redis directly

This is the part of §1 that isn't free, and it needs decisions outside the runner.

**Network reachability.** Today an island's only dependency is the master's control socket, and
islands run on a private bridge network created fresh per run. Redis isn't on that network. One
of three things has to happen, and we don't mind which:

- attach the Redis container to each run's network as the run creates it
- put islands on a shared long-lived network alongside Redis
- run everything on the host network

**Credentials.** Every island container now needs the Redis URL, and a password if there is one.
Today only the master holds it. Island images deliberately carry the minimum — they don't even
get the Docker SDK, so that a buggy island can't create containers. Handing N islands broker
credentials widens that surface. Probably fine; we'd rather it be a decision than a side effect.

**Two failure domains per island.** An island now depends on the master *and* on Redis. If an
island can't reach the broker, the master no longer finds out by watching a log socket fail —
there isn't one. We'll add an explicit diagnostic over the control channel so an unreachable
broker surfaces as a run-level error instead of an island that quietly produces nothing.

**The master still needs its own connection.** Beyond `run_start` and `run_end`, the master
emits diagnostics throughout a run — *"island worker launched"*, *"migration queue full"*,
*"evaluation budget reached"*, *"no island ever connected"*. That last one is how you diagnose a
run that produced zero migrations. These are part of the log today and we'd like to keep them in
the stream, so the master writes to Redis too, not just two bookend entries.

**Migration events need no special handling** — islands emit both halves, so they ride the
island connections. `migration_send` on island A and `migration_arrive` on island B land on
different connections with no relative ordering, which your contract already permits, and the
two halves join on `migration_id`.

## 6. Container naming — changing it

Container and network names are currently built from the **last 8 characters** of the run id.
With ids minted by you and several runs in flight, two ids sharing those 8 characters collide,
and a Docker name collision means the run fails to launch. The id's character set is also
constrained by Docker's naming rules, which would otherwise become a rule you'd have to follow.

We'll change it so the id's shape stops mattering:

```
short    = blake2b(run_id, digest_size=6).hexdigest()      # 12 hex chars, charset-safe
network  = archipelago-{short}
container= archipelago-{short}-island-{NN}
label    archipelago.run_id = <the full run id, verbatim>
```

Hashing the **whole** id rather than slicing it removes the collision mode and the charset
constraint together — you can mint any string you like. The full id stays on a label, so finding
or cleaning up a run's containers by exact id still works. We'll also check the generated name
isn't already taken and fail with a message naming the other run, rather than letting Docker
return something opaque.

## 7. Seeds

Yes, we'll add it. It's smaller than it looks: each island already draws a seed and **already
logs it** in `island_start.island_seed`. What's missing is only that the seed comes from OS
entropy, so the value is recorded but not chosen reproducibly.

What we'll add:

- an optional `seed` on the run config, and in the override whitelist
- per-island seeds derived deterministically as `f(seed, island_id)`, so islands stay
  independent but the whole run is reproducible from one number
- absent a seed, behaviour is unchanged: entropy, still logged

This is what makes deleting the stream safe (open question 8). Without it, a run that's been
clustered and had its stream deleted can never be reproduced, re-clustered or audited — and for
a paper, reproducibility is the first question a reviewer asks.

---

## Answers to the open questions

**1 — entry format.** `kind` + `data` is the right shape. We'd like `data` to be the record's
native JSON rather than a CSV row converted to a dict. See §4.

**2 — HTTP contract.** `GET /presets`, `POST /runs` → 202, `GET /runs/{run_id}` → `{state, error}`
works for us as proposed. One note: the `queued` state in your enum is load-bearing given our
answer to question 6 — a run that arrives while two are already running will sit in `queued`,
so please don't treat `queued` as a transient that can be collapsed into `running`.

**3 — abort.** Your split is right: a crashed runner genuinely can't report its own death, so
the backend has to. Two refinements. First, the runner *will* write `run_end` with
`termination_reason: error` whenever it catches the failure itself, which is most cases — it's a
supervisor. `abort` should be the fallback for *"no terminator appeared within N seconds"*, not
the normal failure path, otherwise you end up inferring reasons we already knew precisely.
Second, there's an unavoidable race — we write `run_end` just as your poll concludes we're dead
— so please have the consumer **treat the first terminator it sees as authoritative** and ignore
anything after it. That same rule covers the bounded-wait case in §2.

**4 — override whitelist.** The six are right: `num_islands`, `evaluation_budget`,
`target_generations`, `migration.topology`, `migration.interval`, `migration.num_migrants`.
We'd like to add `seed` (question 5). We'll validate values and reject anything else.

**5 — seed.** Yes. See §7.

**6 — concurrency.** **2 concurrent runs**, with a hard ceiling of **15 containers** across all
of them. A third run is accepted and reported as `queued` until a slot frees. A run that would
push the container count past 15 — a 6-island run arriving when 10 containers are already up —
is **rejected** rather than queued, with an error saying so, because queueing it behind a limit
it may never fit under just moves the failure later. Both numbers configurable.

**7 — backend and `validate_events`.** Keep **docker**: one container per island is what the
island model means by a node here, and it's what makes the deployment honest. `local` stays as
the testing path. On `validate_events`: keep it **always on**. It's the only thing standing
between a malformed record and your clustering worker, and now that the contract guarantees
specific fields on every record, that check is doing real work. If it ever shows up as a
measured cost, that's the time to revisit — nobody has measured it, us included.

**8 — raw log retention.** Deleting the stream after clustering is fine **provided question 5
lands**. With a seed, a deleted stream costs a re-run; without one, it's unrecoverable, and any
result that's ever questioned can't be re-derived. If the seed doesn't happen, we'd argue for
keeping raw streams for at least some window instead.

---

## What we need from you

| # | Decision | Blocks |
| --- | --- | --- |
| 1 | `data` as native record JSON instead of CSV-as-dict (§4) | entry format, and part of §1 |
| 2 | How islands reach Redis: per-run network, shared, or host (§5) | all of §1 |
| 3 | `maxlen` unbounded, or sized above the largest run (§3) | delivery guarantee |
| 4 | Confirm "first terminator wins" on the consumer side (§2, Q3) | stream boundaries |
| 5 | Sign-off on blocking-not-dropping, and what it implies for run duration (§3) | delivery guarantee |

Once 1, 2 and 5 are settled we can start on §1 and §2 — they're the two blockers, and
everything else above is independent of them.
