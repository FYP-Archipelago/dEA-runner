# The record format

**Normative.** Implemented by `archipelago_logging/`. The format is not versioned: this is the
format.

These records are the only interface between the Runner (orchestrator, algorithms, benchmarks) and
everything downstream: clustering, STN construction, migration edges, MMD. Nothing in the Runner
imports downstream code, and downstream code should read nothing but these records. The field
reference (§3) and the worked examples (§4) are generated from the code by
`scripts/generate_logging_docs.py`; `tests/test_docs_are_current.py` fails if they drift.

---

## 1. The stream

Every record of a run is appended to one Redis stream, `run:{run_id}:runner_logs`. Each entry has
exactly two fields:

| Field | Value |
| --- | --- |
| `kind` | `row` for an `evaluation` record, `event` for every other type (the record's `type` says the same) |
| `data` | The record, as one JSON object |

**Writers.** Each island appends its own records over its own connection. The master appends
`run_start`, its `diagnostic`s and `run_end` over another. Nothing else in the Runner writes. The
backend may append one `abort` entry when it decides a run is dead; a consumer treats the first
terminator it meets (`run_end` or `abort`) as final.

**Order.** Each emitter (`master`, `island:<k>`) numbers its records `seq` 0, 1, 2, … and they
reach the stream in that order: the logger assigns `seq` and hands the record to one queue, one
thread and one connection under one lock. Within an island the stream is therefore in exact `seq`
order (a migrant that arrived between evaluations 40 and 41 is between them in the stream).
Islands interleave freely.

**Boundaries.**

* `run_start` is first. The master writes it and waits for Redis to acknowledge it before any
  island exists, and refuses a run whose stream already has entries. Nothing reaches the stream
  before it, not even a diagnostic: a run that fails earlier (a missing island image, say) has no
  stream, and the API's `failed` state carries the reason.
* `run_end` is last, on every path: `completed`, `aborted` (cancelled), `time_limit`, `error`.
  Each island writes `island_end`, waits until Redis has acknowledged everything it wrote, then
  says goodbye (`BYE`) to the master with its result. The master writes `run_end` once every
  island has said goodbye, exited, or been killed after `BYE_TIMEOUT_SECONDS` of silence once
  asked to stop. Nothing follows `run_end`.

**Failures say why.** For `error`, the entry before `run_end` is an `error`-level `diagnostic` with
the reason (`run failed: …`, `island 2 failed: …`) and `run_end.error` repeats it. `time_limit`
also carries `run_end.error` (`stopped by the Runner's time limit of … s`). An island that died
without reporting is explained by its container's exit code and last 40 lines, in a diagnostic of
its own (`context.log_tail`). A master that loses Redis cannot write `run_end`: the stream just
stops, and the backend's `abort` ends it. A run cancelled while queued has no stream.

**Delivery.** No record is dropped, sampled or written twice.

* A slow Redis fills an island's buffer and the island waits. If nothing is acknowledged for
  `BROKER_STALL_SECONDS`, the island (or master) fails with Redis's error. Out of memory in Redis
  counts as a stall.
* Records go in batches (200 records or 150 ms), each one MULTI/EXEC transaction: a batch lands
  whole or not at all. After a lost reply the writer kills the old connection, then looks for the
  batch in the stream before resending.
* No `MAXLEN`; Redis must run `maxmemory-policy noeviction`. Trimming or eviction would be
  dropping.

**Reading.** `orchestrator.redis_stream.read_stream(url, key)` yields the records in stream order
and checks each entry's shape; `archipelago_logging.validate_log(records)` checks the invariants
(§8). Nothing is written to disk: the stream is the whole record of the run.

```python
import pandas as pd
from orchestrator.redis_stream import read_stream, stream_key

records = list(read_stream("redis://:<password>@<host>:6379/0", stream_key(run_id)))
frame = pd.DataFrame([r for r in records if r["type"] == "evaluation"])
```

---

## 2. Records

**Types:** `run_start`, `island_start`, `evaluation`, `generation_end`, `migration_send`,
`migration_arrive`, `island_end`, `run_end`, `diagnostic`. Evaluations dominate: a
100,000-evaluation run has ~100,000 `evaluation` records and a few hundred of the rest.

**Flat.** No nested payload, so `json.loads`, pandas and `jq` project columns cheaply.

**Absent means null.** Records are written with `exclude_none`: a null field is left out, so read
optional fields with `record.get(...)`. A literal `null` appears only inside the free-form objects
(`config`, `benchmark_params`, `control_channel`, `per_island`), meaning "not set".

**No `inf` or `NaN`.** JSON cannot carry them; the models refuse them, and an island that produces
one fails.

**Time.** Three clocks, for three purposes:

| Field | Across islands? | Monotonic? | Use it for |
| --- | --- | --- | --- |
| `seq` | no (per emitter) | yes, gap-free | ordering one emitter's records |
| `t_mono_ns` | no (per node) | yes | durations on one node |
| `t_wall` | yes, ± clock skew | no | aligning islands, after `island_start.clock_offset_ns` |

There is no global sequence number: a counter shared between containers would need the
synchronisation the island model exists to avoid.

**No version field.** The format is not versioned, so no record carries one, and a record with an
unknown field (`schema_version` included) is refused.

**Open vocabularies.** `operator`, `termination_reason`, `topology`, `selection_policy`,
`replacement_policy` and `diversity_metric` are strings, not enums, so a new algorithm or policy can
emit a new label. Treat an unknown value as its general category, not as an error.

---

## 3. Field reference

<!-- BEGIN GENERATED FIELD REFERENCE -->
### The envelope

Present on **every** record, in this order.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | `string` | yes | Event discriminator; one of EventType. |
| `run_id` | `string` | yes | Identifies the run. Every record in a run shares this value. |
| `emitter` | `string` | yes | Who wrote the record: 'master', or 'island:<id>'. Distinguishes a master-observed fact (run_start, run_end) from an island-observed one, and scopes `seq`, which counts per emitter. |
| `seq` | `integer` | yes | Per-emitter monotonic counter starting at 0. The authoritative ordering of one emitter's records; unaffected by clock skew or NTP steps. |
| `t_wall` | `number` | yes | Unix epoch seconds (float) on the emitting node. Comparable across nodes only after applying island_start.clock_offset_ns. |
| `t_mono_ns` | `integer` | yes | time.monotonic_ns() on the emitting node. Origin is node-local; differences are meaningful only within one emitter. |
| `island_id` | `integer \| null` | no | Island this record concerns. Null for whole-run master records; required on every record an island writes. |
| `generation` | `integer \| null` | no | Logical generation on island_id at emit time. Null for whole-run master records; required on every record an island writes. |
| `node_id` | `string \| null` | no | Container id or hostname of the physical node, for joining a record against the container it ran in (`docker ps`) and against per-node device metrics. |


### `run_start`

First record of a run. Carries the fully resolved configuration.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `started_at` | `string` | yes | Human-facing UTC ISO 8601 timestamp. |
| `harness_version` | `string` | yes | Version of the Runner that produced this record (the field keeps its historical name). |
| `algorithm` | `string` | yes | Registry name of the EA, e.g. 'de'. |
| `benchmark` | `string` | yes | Registry name of the problem, e.g. 'sphere'. |
| `num_islands` | `integer` | yes | — |
| `population_size` | `integer` | yes | Per-island population size, as resolved. Usually derived from evaluation_budget rather than set directly. |
| `evaluation_budget` | `integer` | yes | Total objective evaluations allowed across the WHOLE archipelago -- every island, every generation, until termination. This, not a generation count, is what sizes a run. |
| `evaluation_budget_per_island` | `integer` | yes | evaluation_budget divided among the islands; each island's hard ceiling. |
| `target_generations` | `integer` | yes | The generation count the population size was derived to land near. A target, not a limit: a run normally ends on stagnation before reaching it. |
| `max_generations` | `integer` | yes | Safety cap on generations. |
| `backend` | `string` | yes | Execution backend that launched the islands: 'docker'. |
| `datasets` | `object` | yes | Provenance of the benchmark instance actually used: name, source (file path or generator), and the SHA-256 of the instance file where there is one. |
| `migration` | `object` | yes | Resolved migration policy: topology, interval, selection, replacement, number of migrants, and the resolved neighbour lists. |
| `config` | `object` | yes | Complete resolved run configuration. |
| `host` | `object` | no | Master host facts: platform, python version, cpu count, total memory. |


### `island_start`

One island has come up and is about to initialise its population.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `algorithm` | `string` | yes | — |
| `algorithm_params` | `object` | yes | Resolved operator parameters actually in force on this island. Logged per-island because heterogeneous-island configurations are a planned experiment, and a run-level copy would then be wrong. |
| `benchmark` | `string` | yes | — |
| `benchmark_params` | `object` | yes | The benchmark's own description of the instance this island searches. |
| `genome_encoding` | `string` | yes | GenomeEncoding family this island's individuals use. |
| `population_size` | `integer` | yes | — |
| `evaluation_budget` | `integer \| null` | no | This island's share of the run's evaluation budget; its hard ceiling. |
| `island_seed` | `integer` | yes | The seed this island's generator was created from: derived from the run's `seed` when one is set, otherwise drawn from OS entropy. `numpy.random.default_rng(island_seed)` recreates the island's random stream either way. |
| `hostname` | `string` | yes | — |
| `pid` | `integer` | yes | — |
| `container_id` | `string \| null` | no | — |
| `neighbours_out` | `integereger[]` | no | Islands this island may send migrants to. |
| `neighbours_in` | `integereger[]` | no | Islands this island may receive migrants from. |
| `clock_offset_ns` | `integer \| null` | no | Estimated master_clock - local_clock in nanoseconds. Add to this island's t_wall (x1e9) to place its records on the master timeline. |
| `clock_offset_rtt_ns` | `integer \| null` | no | Round-trip time of the offset probe; uncertainty is ~rtt/2. |


### `evaluation`

One individual was evaluated. The atomic unit of the whole format.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `individual_id` | `string` | yes | Globally unique within the run. See archipelago_logging.ids. |
| `parent_ids` | `stringing[]` | no | Always a list: empty for init, one element for mutation, two or more for recombination. A uniform type means downstream never branches on the operator just to read the parents. |
| `operator` | `string` | yes | What produced this individual; see Operator. |
| `fitness` | `number` | yes | The value the search optimises. For a constrained problem this is the PENALISED objective; see `objective` for the raw one. Orientation is given by generation_end.maximising. |
| `objective` | `number \| null` | no | The raw, unpenalised objective value. Equal to `fitness` for an unconstrained problem, and omitted there. |
| `feasible` | `boolean \| null` | no | Constraint satisfaction, where the problem defines constraints (e.g. the knapsack capacity). None where the problem is unconstrained. |
| `constraint_violation` | `number \| null` | no | Total violation magnitude; 0.0 when feasible. |
| `eval_index` | `integer` | yes | Per-island monotonic evaluation counter, gap-free. This is the index of the point in the island's search trajectory. |
| `genome_encoding` | `string` | yes | — |
| `genome_hash` | `string` | yes | 128-bit blake2b of the location signature. Always present. |
| `genome_repr` | `any` | yes | Genome body, present on every row; interpretation depends on genome_repr_mode. |
| `genome_repr_mode` | `string` | yes | 'compact' \| 'full'. See archipelago_logging.genome. |
| `genome_precision_decimals` | `integer \| null` | no | Rounding used to form the signature for real encodings. Hashes from runs with different precision are NOT comparable. |
| `genome_dim` | `integer \| null` | no | — |
| `origin_island` | `integer \| null` | no | For an individual that arrived by migration, the island it was born on. Denormalised on purpose: it lets a streaming consumer recognise a migrant without having buffered the migration_arrive record. |
| `origin_individual_id` | `string \| null` | no | This individual's id on its island of birth. |
| `is_island_best` | `boolean \| null` | no | True when this evaluation improved the island's best-so-far. |


### `generation_end`

Per-generation summary so downstream need not replay every evaluation.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `best_fitness` | `number` | yes | — |
| `mean_fitness` | `number` | yes | — |
| `worst_fitness` | `number` | yes | — |
| `median_fitness` | `number` | yes | — |
| `std_fitness` | `number` | yes | — |
| `maximising` | `boolean` | yes | Orientation of `fitness`. Recorded. |
| `best_individual_id` | `string \| null` | no | — |
| `best_genome_hash` | `string \| null` | no | — |
| `best_so_far_fitness` | `number \| null` | no | Island best across all generations to date, not just this one. |
| `generations_since_improvement` | `integer \| null` | no | Generations since best_so_far_fitness last improved. |
| `diversity` | `number \| null` | no | Population diversity; see diversity_metric for its definition. |
| `diversity_metric` | `string \| null` | no | Name of the diversity measure, e.g. 'mean_pairwise_euclidean', 'mean_pairwise_hamming', 'fitness_std'. Representation-dependent, so the name must be logged with the value. |
| `diversity_sample_size` | `integer \| null` | no | Number of individuals sampled for the O(n^2) pairwise measures. Null means the whole population was used. |
| `unique_genome_hashes` | `integer \| null` | no | Distinct location signatures in the population; a cheap premature-convergence indicator. |
| `population_size` | `integer` | yes | — |
| `evaluations_this_generation` | `integer` | yes | — |
| `evaluations_total` | `integer` | yes | — |
| `wallclock_seconds` | `number` | yes | Wall-clock duration of this generation on this island. |
| `migrants_received_this_generation` | `integer` | no | — |
| `migrants_sent_this_generation` | `integer` | no | — |


### `migration_send`

Migrants left the source island. Emitted by the source island.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `migration_id` | `string` | yes | Join key linking this to its migration_arrive. |
| `source_island` | `integer` | yes | — |
| `dest_island` | `integer` | yes | — |
| `source_generation` | `integer` | yes | — |
| `migrant_individual_ids` | `stringing[]` | yes | — |
| `migrant_genome_hashes` | `stringing[]` | yes | — |
| `migrant_fitnesses` | `number[]` | yes | — |
| `num_migrants` | `integer` | yes | — |
| `topology` | `string` | yes | — |
| `selection_policy` | `string` | yes | How migrants were chosen, e.g. 'best', 'random', 'tournament'. |
| `migration_interval` | `integer` | yes | Generations between migration attempts on this island. |
| `payload_bytes` | `integer \| null` | no | Serialised size of the transferred payload. Makes the cost of a migration policy measurable -- sending 5 tree genomes is not the same transfer as sending 5 bitstrings. |


### `migration_arrive`

Migrants were integrated into the destination island's population.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `migration_id` | `string` | yes | — |
| `source_island` | `integer` | yes | — |
| `dest_island` | `integer` | yes | — |
| `source_generation` | `integer` | yes | — |
| `dest_generation` | `integer` | yes | — |
| `origin_individual_ids` | `stringing[]` | yes | Ids the migrants carried on the source island. |
| `arrived_individual_ids` | `stringing[]` | yes | New ids minted on arrival. A migrant is a distinct individual at its destination -- it has its own evaluation record there -- so it gets its own id, and origin_individual_ids carries the cross-island... |
| `replaced_individual_ids` | `stringing[]` | no | Individuals evicted from the destination population to make room. Empty if the replacement policy grew the population instead. |
| `migrant_fitnesses` | `number[]` | no | — |
| `replacement_policy` | `string` | yes | — |
| `latency_seconds` | `number \| null` | no | Wall-clock in-flight time, corrected with the two islands' clock offsets. Null if either offset is unknown. |
| `generational_drift` | `integer \| null` | no | dest_generation - source_generation. |


### `island_end`

One island finished (or gave up). Absent for an island that crashed.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `termination_reason` | `string` | yes | — |
| `generations_completed` | `integer` | yes | — |
| `evaluations_total` | `integer` | yes | — |
| `best_fitness` | `number \| null` | no | — |
| `best_genome_hash` | `string \| null` | no | — |
| `best_individual_id` | `string \| null` | no | — |
| `best_genome` | `any \| null` | no | Best genome in FULL representation, regardless of genome mode. |
| `migrations_sent` | `integer` | no | — |
| `migrations_received` | `integer` | no | — |
| `migrants_sent` | `integer` | no | — |
| `migrants_received` | `integer` | no | — |
| `wallclock_seconds` | `number` | yes | — |
| `generations_since_improvement` | `integer \| null` | no | Generations the island's best-so-far went without improving before it stopped. Read together with termination_reason it says whether the island converged or simply ran out of budget. |
| `algorithm_summary` | `object` | no | Algorithm-specific end-of-run facts that have no cross-algorithm analogue -- GP tree size and depth, a held-out test error, ES step-size statistics. |
| `control_channel` | `object` | no | Transport health for this island: failed control calls and the last control error, and under `broker` the records Redis had acknowledged before this one, the errors its writer retried past, and the last of them. |


### `run_end`

Last record of a run, written by the master.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `ended_at` | `string` | yes | — |
| `termination_reason` | `string` | yes | — |
| `error` | `string \| null` | no | Why the run failed, in words fit to show a user; absent unless termination_reason is error or time_limit. For error: what the master hit, then why each failed island failed, the same text as the error diagnostic just before run_end. For time_limit: that the Runner's time limit stopped the run. |
| `wallclock_seconds` | `number` | yes | — |
| `islands_completed` | `integer` | yes | — |
| `islands_failed` | `integer` | yes | — |
| `total_evaluations` | `integer` | yes | — |
| `total_migration_events` | `integer` | yes | — |
| `total_migrants` | `integer` | yes | — |
| `global_best_fitness` | `number \| null` | no | — |
| `global_best_island` | `integer \| null` | no | — |
| `global_best_genome_hash` | `string \| null` | no | — |
| `global_best_genome` | `any \| null` | no | — |
| `per_island` | `object` | no | Per-island summary keyed by island id as a string (JSON object keys are strings; do not expect integer keys after a round trip). |


### `diagnostic`

Free-form Runner diagnostics kept in-band with the run.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `level` | `string` | no | — |
| `message` | `string` | yes | — |
| `context` | `object` | no | — |

<!-- END GENERATED FIELD REFERENCE -->

---

## 4. Worked examples

Emitted by a real run of `scripts/generate_logging_docs.py`; long fields are elided where marked.

<!-- BEGIN GENERATED EXAMPLES -->

#### `run_start`

```json
{
  "type": "run_start",
  "run_id": "run-20261101T090713Z-4c31b828",
  "emitter": "master",
  "seq": 0,
  "t_wall": 1793524033.2350464,
  "t_mono_ns": 36286538682791,
  "started_at": "2026-11-01T09:07:13.233055Z",
  "harness_version": "0.5.0",
  "algorithm": "de",
  "benchmark": "sphere",
  "num_islands": 4,
  "population_size": 9,
  "evaluation_budget": 320,
  "evaluation_budget_per_island": 80,
  "target_generations": 8,
  "max_generations": 50,
  "backend": "docker",
  "datasets": {
    "benchmark": "sphere",
    "name": "sphere",
    "domain": "continuous",
    "source": "definition",
    "maximising": false,
    "known_optimum": 0.0
  },
  "migration": {
    "topology": "ring",
    "interval": 2,
    "num_migrants": 2,
    "selection": "best",
    "replacement": "worst",
    "neighbours_out": {
      "0": [
        1
      ],
      "1": [
        2
      ],
      "2": [
        3
      ],
      "3": [
        0
      ]
    }
  },
  "config": {
    "seed": null,
    "algorithm": "de",
    "benchmark": "sphere",
    "population_size": null,
    "__elided__": "the complete resolved configuration; see docs/CONFIGURATION.md"
  },
  "host": {
    "__elided__": "platform, cpu count, total memory"
  }
}
```


#### `island_start`

```json
{
  "type": "island_start",
  "run_id": "run-20261101T090713Z-4c31b828",
  "emitter": "island:0",
  "seq": 0,
  "t_wall": 1793524035.3639762,
  "t_mono_ns": 36288667614518,
  "island_id": 0,
  "generation": 0,
  "node_id": "archipelago-4c31b828-island-00",
  "algorithm": "de",
  "algorithm_params": {
    "algorithm": "de",
    "genome_encoding": "real_vector",
    "strategy": "rand/1",
    "f": 0.5,
    "cr": 0.9,
    "crossover": "bin",
    "dither": false,
    "dither_range": [
      0.5,
      1.0
    ],
    "bound_handling": "midpoint"
  },
  "benchmark": "sphere",
  "benchmark_params": {
    "name": "sphere",
    "domain": "continuous",
    "maximising": false,
    "known_optimum": 0.0,
    "dimension": 3,
    "lower": -5.12,
    "upper": 5.12
  },
  "genome_encoding": "real_vector",
  "population_size": 9,
  "evaluation_budget": 80,
  "island_seed": 4318636685596506825,
  "hostname": "yendelevium-HP-Laptop-14s-dq2xxx",
  "pid": 1,
  "container_id": "archipelago-4c31b828-island-00",
  "neighbours_out": [
    1
  ],
  "neighbours_in": [
    3
  ],
  "clock_offset_ns": 12874,
  "clock_offset_rtt_ns": 216722
}
```


#### `evaluation`

Two real records: an initial individual, which has no parents, and a later one.

```json
{
  "type": "evaluation",
  "run_id": "run-20261101T090713Z-4c31b828",
  "emitter": "island:0",
  "seq": 1,
  "t_wall": 1793524035.3650198,
  "t_mono_ns": 36288668656845,
  "island_id": 0,
  "generation": 0,
  "node_id": "archipelago-4c31b828-island-00",
  "individual_id": "i00-g000000-n000000000",
  "parent_ids": [],
  "operator": "init",
  "fitness": 39.7955765815177,
  "eval_index": 0,
  "genome_encoding": "real_vector",
  "genome_hash": "4ad707569cb011e38c51571007b82166",
  "genome_repr": [
    -4.674155859064901,
    3.4213107596483363,
    2.4984948013957275
  ],
  "genome_repr_mode": "full",
  "genome_precision_decimals": 4,
  "genome_dim": 3,
  "is_island_best": true
}
```
```json
{
  "type": "evaluation",
  "run_id": "run-20261101T090713Z-4c31b828",
  "emitter": "island:0",
  "seq": 11,
  "t_wall": 1793524035.3892024,
  "t_mono_ns": 36288692838683,
  "island_id": 0,
  "generation": 1,
  "node_id": "archipelago-4c31b828-island-00",
  "individual_id": "i00-g000001-n000000009",
  "parent_ids": [
    "i00-g000000-n000000000",
    "i00-g000000-n000000005",
    "i00-g000000-n000000003",
    "i00-g000000-n000000006"
  ],
  "operator": "de_trial",
  "fitness": 40.58050470350709,
  "eval_index": 9,
  "genome_encoding": "real_vector",
  "genome_hash": "b6e68aaea21efde3552faa579dc36ee2",
  "genome_repr": "tVzc6VWyEsDiI/Es2F4LQIRs5kQYNQXA",
  "genome_repr_mode": "compact",
  "genome_precision_decimals": 4,
  "genome_dim": 3
}
```


#### `generation_end`

```json
{
  "type": "generation_end",
  "run_id": "run-20261101T090713Z-4c31b828",
  "emitter": "island:0",
  "seq": 10,
  "t_wall": 1793524035.3886437,
  "t_mono_ns": 36288692280620,
  "island_id": 0,
  "generation": 0,
  "node_id": "archipelago-4c31b828-island-00",
  "best_fitness": 17.216678323239403,
  "mean_fitness": 29.25497686638786,
  "worst_fitness": 50.87596058335943,
  "median_fitness": 28.510405905283683,
  "std_fitness": 9.983927677625331,
  "maximising": false,
  "best_individual_id": "i00-g000000-n000000002",
  "best_genome_hash": "d75c7692bb03f4820ce649d1c3f29d1a",
  "best_so_far_fitness": 17.216678323239403,
  "generations_since_improvement": 0,
  "diversity": 0.410259253546962,
  "diversity_metric": "mean_pairwise_euclidean_normalised",
  "unique_genome_hashes": 9,
  "population_size": 9,
  "evaluations_this_generation": 9,
  "evaluations_total": 9,
  "wallclock_seconds": 0.0336802909951075,
  "migrants_received_this_generation": 0,
  "migrants_sent_this_generation": 0
}
```


#### `migration_send`

```json
{
  "type": "migration_send",
  "run_id": "run-20261101T090713Z-4c31b828",
  "emitter": "island:0",
  "seq": 30,
  "t_wall": 1793524035.396734,
  "t_mono_ns": 36288700369812,
  "island_id": 0,
  "generation": 2,
  "node_id": "archipelago-4c31b828-island-00",
  "migration_id": "mig-00-01-000000",
  "source_island": 0,
  "dest_island": 1,
  "source_generation": 2,
  "migrant_individual_ids": [
    "i00-g000002-n000000018",
    "i00-g000001-n000000011"
  ],
  "migrant_genome_hashes": [
    "53fd5382bdd30590f2e57c4352177812",
    "cb4e60a281d92191776d794bf4eb185a"
  ],
  "migrant_fitnesses": [
    3.242138302249632,
    4.108123381596498
  ],
  "num_migrants": 2,
  "topology": "ring",
  "selection_policy": "best",
  "migration_interval": 2,
  "payload_bytes": 297
}
```


#### `migration_arrive`

```json
{
  "type": "migration_arrive",
  "run_id": "run-20261101T090713Z-4c31b828",
  "emitter": "island:1",
  "seq": 22,
  "t_wall": 1793524035.5714638,
  "t_mono_ns": 36288875100790,
  "island_id": 1,
  "generation": 1,
  "node_id": "archipelago-4c31b828-island-01",
  "migration_id": "mig-00-01-000000",
  "source_island": 0,
  "dest_island": 1,
  "source_generation": 2,
  "dest_generation": 1,
  "origin_individual_ids": [
    "i00-g000002-n000000018",
    "i00-g000001-n000000011"
  ],
  "arrived_individual_ids": [
    "i01-g000001-n000000018",
    "i01-g000001-n000000019"
  ],
  "replaced_individual_ids": [
    "i01-g000000-n000000006",
    "i01-g000001-n000000010"
  ],
  "migrant_fitnesses": [
    3.242138302249632,
    4.108123381596498
  ],
  "replacement_policy": "worst",
  "latency_seconds": 0.17498493194580078,
  "generational_drift": -1
}
```


#### `island_end`

```json
{
  "type": "island_end",
  "run_id": "run-20261101T090713Z-4c31b828",
  "emitter": "island:0",
  "seq": 84,
  "t_wall": 1793524035.4484835,
  "t_mono_ns": 36288752120339,
  "island_id": 0,
  "generation": 7,
  "node_id": "archipelago-4c31b828-island-00",
  "termination_reason": "max_evaluations",
  "generations_completed": 7,
  "evaluations_total": 72,
  "best_fitness": 1.3665181771460935,
  "best_genome_hash": "701c1f318b031dccef5cfd184d9e8f24",
  "best_individual_id": "i00-g000004-n000000036",
  "best_genome": [
    0.02692798283036213,
    -1.110566695673992,
    0.36391575308927226
  ],
  "migrations_sent": 3,
  "migrations_received": 0,
  "migrants_sent": 6,
  "migrants_received": 0,
  "wallclock_seconds": 0.1150836189990514,
  "generations_since_improvement": 3,
  "algorithm_summary": {},
  "control_channel": {
    "control_available": true,
    "failed_control_calls": 0,
    "last_control_error": null,
    "broker": {
      "acknowledged": 0,
      "errors": 0,
      "last_error": null
    }
  }
}
```


#### `run_end`

```json
{
  "type": "run_end",
  "run_id": "run-20261101T090713Z-4c31b828",
  "emitter": "master",
  "seq": 5,
  "t_wall": 1793524036.8907266,
  "t_mono_ns": 36290194363162,
  "ended_at": "2026-11-01T09:07:16.890691Z",
  "termination_reason": "completed",
  "wallclock_seconds": 3.769692796005984,
  "islands_completed": 4,
  "islands_failed": 0,
  "total_evaluations": 306,
  "total_migration_events": 12,
  "total_migrants": 24,
  "global_best_fitness": 0.0018592297354982907,
  "global_best_island": 3,
  "global_best_genome_hash": "cf76f6d310fe84cfe1eabafdd1fe107e",
  "global_best_genome": [
    -0.031179214747033807,
    -0.008956297954688153,
    0.028405475356055723
  ],
  "per_island": {
    "__elided__": "one summary object per island, keyed by island id"
  }
}
```


#### `diagnostic`

```json
{
  "type": "diagnostic",
  "run_id": "run-20261101T090713Z-4c31b828",
  "emitter": "master",
  "seq": 1,
  "t_wall": 1793524033.4645689,
  "t_mono_ns": 36286768206215,
  "level": "info",
  "message": "island container launched",
  "context": {
    "island_id": 0,
    "node_id": "archipelago-4c31b828-island-00",
    "backend": "docker"
  }
}
```

<!-- END GENERATED EXAMPLES -->

---

## 5. Genomes

### `genome_hash`: where an individual is

On every row: a 128-bit blake2b digest (32 hex characters) of the individual's **location
signature**, with real values rounded to `genome_precision_decimals`. This is the standard STN
location mapping (Ochoa, Malan & Blum, *Search trajectory networks*, Applied Soft Computing 109,
2021), so a `groupby` on it is already a coarse STN node assignment.

| Encoding | Signature (prefixed with the encoding, so encodings never collide) |
| --- | --- |
| `bitstring` | the bits, joined |
| `permutation` | the indices, comma-joined |
| `real_vector` | each coordinate at `precision` decimals, comma-joined |
| `real_vector_velocity` (PSO) | **position only** |
| `real_vector_sigma` (ES) | **object variables only** |
| `expression_tree` (GP) | canonical prefix string, constants at fixed precision |

Velocity and step sizes are how an individual will move, not where it is: two particles at one
point are one STN node. Because the signature rounds, two rows can share a hash while their exact
genomes differ slightly; a clusterer working on exact values may then label them differently.
Hashes from runs with different `genome_precision_decimals` are not comparable (it is a column on
every row).

### `genome_repr`: what an individual is

Every row carries the genome. Its JSON type depends on `genome_encoding` and on the row's
`genome_repr_mode`, which varies within a run: rows are `compact` (the default), but each island's
best-so-far individuals are always `full`. Read both fields before decoding; `decode_compact()`
inverts `compact`.

| Encoding | `compact` | `full` |
| --- | --- | --- |
| `real_vector` (DE) | string: base64 of little-endian float64 | list of numbers |
| `bitstring` (GA knapsack) | string `"<n>:<base64>"`, bit *i* in byte *i*//8 at bit *i*%8 | list of 0/1 |
| `permutation` (GA TSP) | string: base64 of little-endian uint32 | list of ints |
| `real_vector_velocity` (PSO) | `{"x": <float64 base64>, "v": <float64 base64>}` | `{"x": [...], "v": [...]}` |
| `real_vector_sigma` (ES) | `{"x": <float64 base64>, "sigma": <float64 base64>}` | `{"x": [...], "sigma": [...]}` |
| `expression_tree` (GP) | canonical prefix string | the same string |

`compact` is lossless and about half the size of `full`.

---

## 6. What downstream derives from the records

**STN nodes.** Group evaluations by `genome_hash` (no clustering), or decode `genome_repr` into a
feature matrix and cluster it (LSH → BIRCH → DenStream, in the backend).

**STN edges, within an island.** Every row carries `parent_ids` (always a list):

| `operator` | `parent_ids` |
| --- | --- |
| `init` | empty: a trajectory root |
| `mutation`, `clone`, `elite_copy` | one |
| `crossover`, `crossover_mutation` | two or more |
| `de_trial` | four: `[target, r1, r2, r3]` |
| `pso_move` | two: `[previous position, social informant]` |
| `es_mutation` | ρ recombination parents |
| `migration_arrival` | one: the individual's id on its source island |

`parent_ids` means the individuals whose material or information determined this one, so PSO's
informant links are edges too. `eval_index` is gap-free per island, so completeness can be checked.

**Migration edges.** Join `migration_send` and `migration_arrive` on `migration_id`:

```python
from archipelago_logging import join_migration_edges, parse_records

for edge in join_migration_edges(parse_records(records)):
    edge.source_island, edge.dest_island, edge.delivered
    edge.generational_drift    # dest_generation - source_generation; negative = destination behind
    edge.latency_seconds       # clock-corrected time in flight
```

Between STN nodes rather than islands: each arrival's own evaluation record has
`operator: migration_arrival`, its destination `genome_hash`, and `origin_island` /
`origin_individual_id`. An undelivered send is normal: the run ended before the destination polled,
or the destination had stopped. The intended topology, with `neighbours_out` / `neighbours_in`, is
in `run_start.migration`; the difference between intended and observed edges is itself a finding.
With `migration.enabled: false` there are no migration records at all.

**MMD inputs.** Two islands' evaluations in a window, by `generation` (logical) or by
`t_wall` + `clock_offset_ns` (wall clock); the genome space is common to all islands of a run.
Before/after a migration: the windows either side of a `migration_arrive`.
`generation_end.unique_genome_hashes` and `.diversity` shortlist island pairs cheaply.

---

## 7. Incomplete runs

There is no fault event and no liveness monitoring (fault tolerance is out of scope); an
incomplete run is visible from the records' structure:

* **An island finished:** its `island_end.termination_reason`, repeated in
  `run_end.per_island[<id>]`.
* **An island failed but could report:** `run_end.per_island[<id>].error`, the closing
  diagnostic, and `island_end.termination_reason: error` if it could still write.
* **An island did not finish:** no `island_end`; its container's exit code and last output in an
  error diagnostic; `finished: false` in `run_end.per_island`; counted in `run_end.islands_failed`.
  Its `seq` is contiguous up to where it stopped.
* **No island ever reached the master** (a network problem): an error diagnostic from the master
  at the end of the run.

---

## 8. Invariants

`validate_log()` checks these; `read_stream` also checks that each entry has exactly `kind` and
`data`, and that `kind` matches `type`.

1. Every record validates against its type.
2. Exactly one `run_start`, first.
3. Exactly one `run_end` (unless the run is still going).
4. Each emitter's `seq` arrives as exactly 0, 1, 2, …: no gaps, repeats or reordering.
5. Each island's `eval_index` arrives as exactly 0, 1, 2, ….
6. Every `migration_arrive` has a matching `migration_send`.
7. Every record has the same `run_id`.

Guaranteed but not checked (they need the whole run in memory): every id in `parent_ids` was itself
logged; only `init` rows have empty `parent_ids`; an individual id's island is the row's
`island_id`.

---

## 9. Size and the evaluation budget

A run is sized by `evaluation_budget`, the total across all islands, split evenly. Each island's
population is derived so its share lasts about `target_generations`:

```
population = budget_per_island / ((target_generations + 1) × offspring_multiplier)
```

(`offspring_multiplier` is 7 for the (μ,λ)-ES, 1 otherwise.) An island stops at the first of:

| `termination_reason` | When |
| --- | --- |
| `stagnation` | best-so-far stopped improving (the intended stop) |
| `max_evaluations` | its budget cannot cover another generation |
| `target_fitness_reached` | a target objective value was reached |
| `aborted` | the master asked it to stop (cancel, time limit, a failed run) |
| `max_generations` | the safety cap (default 5,000) |

So a run lands at or below its budget; `run_end.total_evaluations` is exact. In Redis a stream
takes 0.74–1.3 KB per evaluation depending on the preset (about 325 MB for the heaviest at the
250,000 cap); see [`MEMORY_MEASUREMENTS.md`](MEMORY_MEASUREMENTS.md). A slow Redis makes a run
slower, never incomplete, and that waiting does distort the islands' relative timing;
`island_end.control_channel.broker` counts the errors an island's writer retried past.

---

## 10. Randomness and the dataset

**With a `seed`** (in `run_start.config`) every random stream derives from it. With migration off,
two runs produce identical evaluation records apart from `run_id`, timestamps and node identity.
With migration on, the per-island streams match but trajectories do not, because migrants arrive
on timing-dependent generations. Without a seed each island draws OS entropy. Each island's seed is
in `island_start.island_seed`.

**The problem is pinned** by `run_start.datasets` (`benchmark`, `name`, `source`,
`instance_sha256`, `known_optimum`, `maximising`). Instances come only from committed files
(`benchmarks/data/MANIFEST.json` has every source URL and SHA-256); there is no generator.

---

## 11. Deviations from the original specification, and limitations

* **`migration` is split into `migration_send` + `migration_arrive`.** Under asynchrony the
  destination generation is unknowable at send time; one record would need a blocked source or a
  back-dated field. The split makes latency and generational drift (which can be negative)
  measurable.
* **No `device_metrics`.** Telemetry interleaved into the search record complicated every
  consumer. Host provenance is in `run_start.host`. Whether telemetry returns is open
  (`HANDOFF.md` §8).
* **Added:** `seq` and `t_mono_ns` (reliable ordering and durations), `island_end` (so a finished
  island is distinguishable from a truncated log) and `diagnostic` (operational messages in the
  same ordered stream; analysis ignores them).
* **Single-objective.** An evaluation carries one `fitness` (and its raw `objective`); there is no
  objective vector.
* **Clock correction is advisory:** `clock_offset_ns` is a round-trip estimate taken once at island
  start (± `clock_offset_rtt_ns / 2`); timestamps are raw, and long runs drift.
* **Population membership is implicit:** rows say who was created; survival shows in later
  references, and `generation_end` summarises each generation.
* **Elites are logged once**, not every generation they survive (GA's `log_elites: true` changes
  this); re-logging would fabricate STN self-loops.

---

## 12. Reference

* Code: `archipelago_logging/` (`events.py` models, `genome.py` signatures and codecs, `logger.py`
  emission, `sinks.py`, `reader.py` parsing, validation and migration joins);
  `orchestrator/redis_stream.py` (the writer, `read_stream`).
* Regenerate §3: `python scripts/generate_logging_docs.py --fields-only`. §3 and §4 from a real run:
  `--image <island image> --broker-url <redis url>`.
* Validate a run: `validate_log(read_stream(url, stream_key(run_id)))`.
