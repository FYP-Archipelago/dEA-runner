# Configuration

Two kinds, kept apart:

* **A run's configuration** (`RunConfig`, `orchestrator/config.py`): what its **preset** and the
  backend's **overrides** decide (algorithm, problem, budget, migration). It is embedded in the
  run's `run_start`, so a run describes itself.
* **The Runner's settings** (`ServiceSettings`, `orchestrator/settings.py`): how it is deployed
  (Redis, images, limits, timeouts). Environment variables, the same for every run, never part of a
  request ([Service settings](#service-settings)).

**Unknown keys are errors**, in a request's overrides, in a preset, and inside `algorithm_params`
and `benchmark_params`: a silently ignored typo runs a whole sweep at the default.

---

## Presets and overrides

A preset is a YAML file in `config/`, named by its file name. A run is one preset plus at most
these overrides, exactly as `GET /presets` publishes them:

| Override key | Type | Bounds |
| --- | --- | --- |
| `num_islands` | int | 4 – 6 |
| `evaluation_budget` | int | 1,000 – 250,000 (the maximum is a memory bound, mostly the backend's: [`MEMORY_MEASUREMENTS.md`](MEMORY_MEASUREMENTS.md)) |
| `target_generations` | int | ≥ 2 |
| `migration.enabled` | bool | `false` gives a baseline with no migration, which with `seed` repeats record for record; the other `migration.*` rules then do not apply |
| `migration.topology` | choice | `bidirectional_ring` · `fully_connected` · `lattice` · `random` · `ring` · `star` · `torus` |
| `migration.interval` | int | ≥ 1, and ≤ `target_generations` |
| `migration.num_migrants` | int | ≥ 1, and below the per-island population |
| `seed` | int, optional | 0 – 4,294,967,295; no default (fresh randomness each run) |

```json
{"run_id": "run-20261031T120000Z-1a2b3c4d", "preset": "ga_tsp",
 "overrides": {"evaluation_budget": 50000, "migration.topology": "star", "seed": 42}}
```

* A key left out takes the preset's value. Values need the exact JSON type: `5000`, not `"5000"`
  or `5000.0`; `false`, not `0`.
* Overriding the topology drops the preset's `topology_params` (they belong to its own topology,
  a star's hub say).
* The whole configuration is then validated, so a combination that cannot run (migrants against
  the population, interval against generations, a torus's island count) is refused with the
  arithmetic in the message.
* Everything else (algorithm parameters, the instance, genome logging) is fixed by the preset. A
  different setting is a new preset; rebuild the Runner image. The service refuses to start if any
  preset is invalid.

| Preset | What it runs |
| --- | --- |
| `full_dataset` | **The reference dataset run**: GA on TSPLIB kroA100, 100,000 evaluations |
| `de_rastrigin` | DE/rand/1/bin on CEC-shifted Rastrigin 10D (official shift data, no rotation) |
| `pso_ackley` | PSO with constriction, ring swarm, `fanout: one_random`, Ackley 10D |
| `es_schwefel` | Self-adaptive (μ,λ)-ES, star topology, `diverse` → `similar`, Schwefel 10D |
| `ga_knapsack` | Binary GA on a real strongly correlated 0/1 knapsack (n = 100) |
| `ga_tsp` | Permutation GA (OX + inversion) on TSPLIB berlin52, its optimum as the target |
| `gp_nguyen7` | Koza GP on Nguyen-7, `genome_mode: full` |
| `gp_yacht` | Koza GP on the UCI Yacht data: no exact solution, so stagnation ends it |
| `docker_de_sphere` | DE on Sphere, 20,000 evaluations: the quick demo |

---

## Top level

| Field | Default | Notes |
| --- | --- | --- |
| `run_id` | required | From the request, as `run-<UTC timestamp>-<8 lowercase hex>`; never in a preset. A used id is refused |
| `description` | `""` | Free text, carried into `run_start` |
| `algorithm` | `de` | `ga` · `de` · `pso` · `gp` · `es` |
| `algorithm_params` | `{}` | [`ALGORITHMS.md`](ALGORITHMS.md); checked against the algorithm's signature |
| `benchmark` | `rastrigin` | [`BENCHMARKS.md`](BENCHMARKS.md) |
| `benchmark_params` | `{}` | Checked against the problem's signature |
| **`evaluation_budget`** | `100000` | **Total evaluations across the whole archipelago** |
| `target_generations` | `100` | The generation count the derived population aims for |
| `population_size` | `null` | Per island; leave null to derive it |
| `num_islands` | `5` | 4–6 |
| `seed` | `null` | Makes a run repeatable ([Randomness](#randomness)) |

Algorithm/benchmark compatibility is checked before anything starts (`de` on `tsp` fails at once,
naming the domains each supports).

### How a run is sized

```
budget_per_island = evaluation_budget / num_islands
population_size   = budget_per_island / ((target_generations + 1) × offspring_multiplier)
```

The `+ 1` is initialisation. `offspring_multiplier` is the ES's `offspring_per_parent` (default 7)
and 1 otherwise, so an ES lands on the same total as a DE. At the defaults: 20,000 per island over
~100 generations, a population of about 198. `run_start.config._runtime.budget_plan` records the
result.

**`target_generations` against the budget is the real decision:** more generations, smaller
population, deeper and narrower search. GP usually wants fewer, wider generations; DE and PSO
tolerate many narrow ones. An explicit `population_size` over-determines the run (the budget
becomes only a ceiling). The derived size is clamped to what the algorithm needs (DE: 4 for `/1`,
6 for `/2`; a tournament: its own size).

---

## `termination`

The budget is a ceiling; **stagnation normally ends a run**, which stops an island once it has found
what it will find instead of adding near-duplicate rows.

| Field | Default | Notes |
| --- | --- | --- |
| `stagnation_generations` | `30` | Stop after this many generations without improvement; 0 disables |
| `stagnation_tolerance` | `1e-10` | Smaller improvements do not reset the counter. It compares against the last *counted* improvement, so floating-point creep still counts as stagnant |
| `max_generations` | `5000` | Safety cap |
| `target_fitness` | `null` | Stop once the **raw objective** passes this (useful when the optimum is known: 0 for the continuous functions, published optima for TSPLIB) |

Expect runs to finish under budget; `run_end.total_evaluations` is exact. The run-wide time limit
is a service setting, `RUN_TIME_LIMIT_SECONDS`.

---

## `migration`

| Field | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | `false`: the no-migration control run |
| `topology` | `ring` | `ring` · `bidirectional_ring` · `fully_connected` · `star` · `random` · `torus` · `lattice` |
| `topology_params` | `{}` | e.g. `{hub: 0}` (star), `{degree: 2, seed: 0}` (random), `{rows: 2}` (torus) |
| `interval` | `10` | Generations between sends |
| `num_migrants` | `5` | Individuals per send |
| `selection` | `best` | `best` · `worst` · `random` · `tournament` · `roulette` · `diverse` |
| `replacement` | `worst` | `worst` · `random` · `similar` · `none` |
| `selection_params` | `{}` | e.g. `{tournament_size: 4}` |
| `replacement_params` | `{}` | e.g. `{candidate_pool: 10}` for `similar` |
| `fanout` | `all` | `all` · `one_random` · `round_robin` |
| `copy_migrants` | `true` | `false` removes emigrants from the source |
| `phase_jitter` | `0` | Per-island random offset, in generations |
| `poll_every_generation` | `true` | Check the inbox every generation, while sending on `interval` |
| `max_pending_per_island` | `1000` | Bound on undelivered migrants queued for one island |

**`torus` needs 6 islands:** with 4–5 (or 6 in one row) the wrapped grid is a plain ring, and
`run_start` would record a torus that never ran. `lattice` (no wrap) is distinct at every count.

**Choosing the knobs.**

* **`interval`** matters most: too short and the islands become one population, too long and they
  share nothing. Start near `target_generations / 10`.
* **`topology`** trades propagation speed against independence: `ring` mixes slowest,
  `fully_connected` fastest, `star` gives a strong source/sink asymmetry (the hardest test of
  whether migration edges reveal structure).
* **`fanout`**: under `all`, fully connected moves 4× a ring's migrants, confounding topology with
  volume; `one_random` or `round_robin` hold volume constant.
* **`selection` → `replacement`**: `best` → `worst` is elitist and converges fastest; `random` →
  `random` keeps diversity; `best` → `similar` is crowding (an immigrant displaces its nearest
  neighbour); `diverse` avoids shipping a clique of near-identical elites.
* **`phase_jitter`** breaks the lockstep of every island migrating on the same generation number.

---

## `logging`

| Field | Default | Notes |
| --- | --- | --- |
| `genome_mode` | `compact` | `compact` · `full`, both lossless; compact is about half the size. The hash is in both |
| `genome_precision_decimals` | `4` | Rounding for real genomes' location signature. **Changes what `genome_hash` means**: runs with different values are not comparable (it is on every row) |

Every record is validated against its model before it is written; there is no switch. It is the
only guard before the backend, and under Pydantic 2 the faster path (≈5 µs against ≈15 µs). A
rejected record stops the island and consumes no `seq` or `eval_index`.

Volume is linear in `evaluation_budget`: `full_dataset` appends ~100,000 records, 110 MB of record
data, 130 MB in Redis ([`MEMORY_MEASUREMENTS.md`](MEMORY_MEASUREMENTS.md)).

---

## Service settings

Environment variables, read by the service and passed to every master and by it to every island.
Each is a `ServiceSettings` field; an unset variable keeps its default, and a bad value stops the
service at startup.

| Variable | Default | Notes |
| --- | --- | --- |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | `redis://[user:password@]host:port/db`; redacted in `run_start` |
| `ISLAND_IMAGE` | `archipelago-island:latest` | Never built by the Runner |
| `MASTER_IMAGE` | `archipelago-runner:latest` | Every master runs the Runner's own image |
| `ISLAND_CPUS` | `1.0` | CPU quota per island |
| `ISLAND_MEMORY` | `2g` | Memory limit per island (islands use ~50 MB) |
| `MAX_CONCURRENT_RUNS` | `2` | The rest wait in arrival order |
| `MAX_CONTAINERS` | `15` | Masters and islands at once; a run that could never fit is refused (422) |
| `BROKER_CONNECT_TIMEOUT_SECONDS` | `10` | How long the master, then each island, tries to reach Redis at startup |
| `BROKER_STALL_SECONDS` | `120` | How long a writer retries with nothing acknowledged before the run fails. Each Redis call times out after `min(5, BROKER_STALL_SECONDS)` s |
| `BROKER_BUFFER_RECORDS` | `10000` | Records an island holds while Redis catches up; then it waits |
| `HEARTBEAT_SECONDS` | `2` | Islands' progress reports, which carry back the stop flag. Not a liveness probe |
| `BYE_TIMEOUT_SECONDS` | `20` | After being asked to stop, how long an island gets to finish its generation, flush and say goodbye before it is killed |
| `RUN_TIME_LIMIT_SECONDS` | `3600` | Always on, at most 86,400. A run still going is wound down and `failed`, ending with `run_end` (`time_limit`, with `error`). What ends a run with a hung island |

* **Nothing is dropped:** Redis must run `noeviction` and the stream must not be trimmed.
* **The password never reaches a record:** `run_start.config._runtime.settings` shows `***`. The
  containers get the real URL in their environment (readable with `docker inspect`, which needs the
  same Docker access that already means root).
* **One address for everything:** masters join the service's networks and islands the master's, so
  a compose name like `redis` resolves for all; a service on the host runs everything on the host's
  network.
* **Redis permissions:** `PING`, `XADD`, `XLEN`, `XRANGE`, `XREVRANGE`, `CLIENT ID`, `CLIENT KILL`
  (the last two make sure a batch whose reply was lost can never land twice).

---

## Randomness

Without a `seed`, each island draws OS entropy, so repeated runs are a sample, not a repetition.
With one, every random stream derives from it: **with migration off, the run repeats record for
record; with migration on, it does not**, since a migrant joins its destination on whatever
generation that island has reached. Seeded runs with migration are comparable, not identical.

What is always fixed is the **problem**: `benchmark_params.instance_file` names a committed
instance, `run_start.datasets` records its SHA-256, and there is no generator to fall back on.
