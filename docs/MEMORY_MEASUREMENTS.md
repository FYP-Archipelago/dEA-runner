# Memory measurements

Two sets of measurements, both on the owner's laptop (Intel Core i3-1115G4, 2 cores / 4 threads,
15 GiB RAM, Ubuntu 24.04.5, Docker Engine 29.8.1, Redis 7.4.11):

1. **Redis per run** (§1, 2026-10-27, M4.8): what each preset's stream costs. It set the
   `evaluation_budget` cap at 500,000; §3 lowered it to **250,000** (2026-11-01).
2. **The whole platform** (§2–3, 2026-11-01): every container of the Runner and of the
   Archipelago Backend, while runs went through the backend's API.

---

## 1. Redis per run

**Method:** `scripts/measure_memory.py` on the compose stack (Redis with `noeviction` and no
persistence; the dummy worker reading every stream). It samples `INFO memory` every 100 ms and
reads each stream's `MEMORY USAGE`. *Record bytes* are the `kind` and `data` fields of every
entry; *overhead* is `MEMORY USAGE` ÷ record bytes.

### Every preset at its default budget

| Preset | Genomes | Evaluations | Record bytes | `MEMORY USAGE` | Overhead | Bytes / evaluation | At 500,000 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `docker_de_sphere` | compact | 19,950 | 15.4 MB | 17.8 MB | ×1.16 | 893 | 446 MB |
| `de_rastrigin` | compact | 91,105 | 69.6 MB | 80.6 MB | ×1.16 | 885 | 442 MB |
| `es_schwefel` | compact | 96,107 | 81.9 MB | 94.3 MB | ×1.15 | 981 | 490 MB |
| `full_dataset` (TSP kroA100) | compact | 99,760 | 110.1 MB | 130.0 MB | ×1.18 | 1,303 | 651 MB |
| `ga_knapsack` | compact | 85,026 | 54.5 MB | 63.0 MB | ×1.16 | 741 | 370 MB |
| `ga_tsp` (berlin52) | compact | 97,086 | 82.5 MB | 95.3 MB | ×1.16 | 982 | 491 MB |
| `gp_nguyen7` | full | 99,220 | 65.8 MB | 76.4 MB | ×1.16 | 770 | 385 MB |
| `gp_yacht` | full | 99,420 | 86.2 MB | 100.6 MB | ×1.17 | 1,012 | 506 MB |
| `pso_ackley` | compact | 99,602 | 85.1 MB | 97.9 MB | ×1.15 | 983 | 492 MB |

"Bytes / evaluation" includes the run's other records; "At 500,000" multiplies it out.
`MEMORY USAGE` overstates the real cost by about 7% (the nine streams summed to 755.9 MB while
`used_memory` grew by 709 MB).

### At the cap

| Scenario | Evaluations | `MEMORY USAGE` | `used_memory` peak |
| --- | ---: | ---: | ---: |
| `full_dataset`, 500,000 | 499,270 | 643.4 MB (1,289 B / evaluation) | 603 MB |
| Two `full_dataset` at 500,000, concurrently | 2 × ~499,270 | 2 × 643.4 MB | 1,207.5 MB |

* **Linear in evaluations**, at 0.74–1.3 KB each depending on the preset; Redis adds 15–18 % to
  the record bytes. `full_dataset` (a 100-city permutation per record) is the heaviest.
* **Concurrency costs nothing extra** in the streams themselves: two runs are the sum of the two.
* **Streams stay until the backend expires them.** Under `noeviction` a full Redis fails the next
  `XADD`, and the run fails rather than lose records.

Not covered: overrides that grow records (more generations give GP trees more time to bloat), and
Valkey (it behaves identically in the tests; its memory was not measured).

**Repeating it:** with the compose stack up, `python scripts/measure_memory.py --runs 1 --json
cap.json --start "python scripts/dummy_backend.py run full_dataset --set evaluation_budget=500000"`.
`--start` may be repeated (concurrent runs) and may hold a loop over presets;
`docker compose restart redis dummy-worker` empties Redis between scenarios.

---

## 2. The whole platform

**Setup.** The Archipelago Backend at `f40542c` (2026-11-01; clustering module `5468c5b`), from its
own `compose.yaml`: API, two Worker Pool workers, PostgreSQL 16, Redis 7.4 with `--maxmemory 2gb`,
`noeviction` and `appendonly yes`. The Runner from `docker-compose.prod.yml` on the backend's
network. Every run was started with the backend's `POST /runs`, clustering `{"birch": true}`,
on `full_dataset`, the heaviest preset.

**Method.** Every container's memory as `docker stats` reports it (cgroup v2 `memory.current` less
`inactive_file`), sampled every 0.5 s. Each figure is the peak of that component's total across
the scenario. Redis's `INFO memory` and `INFO clients` were sampled the same way. The backend's API
and workers and the Runner service were restarted before each scenario, so each starts fresh.

**Two runs at the cap** were measured with one worker stopped: two workers clustering 500,000
evaluations each at once would have needed about 8 GB, close to all the memory free on the laptop.
The second run waited for the first's clustering to finish, so its stream sat in Redis until the
worker read it. Two workers is projected below, from the one-run figures.

### Peak memory by component, MB

| Component | Idle | 1 run, 100k, batch | 1 run, 100k, streaming | 1 run, 500k × 6 islands, batch | 1 run, 500k × 6, streaming | 2 runs, 500k × 6, batch (1 worker) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **Runner** service | 73 | 73 | 73 | 75 | 75 | 75 |
| **Runner** masters | — | 51 | 52 | 51 | 51 | 102 (2 × 51) |
| **Runner** islands | — | 217 (5 × ≤ 44) | 216 | 301 (6 × ≤ 51) | 307 (6 × ≤ 52) | 600 (12 × ≤ 51) |
| *Runner, total* | *73* | *341* | *341* | *427* | *433* | *777* |
| **Redis** container | 9 | 145 | 185 | 636 | 818 | 1,755 |
| Redis `used_memory` | 2 | 127 | 162 | 630 | 878 | **1,809** of 2,048 |
| **Backend** workers (both) | 193 | 1,093 | 1,048 | 3,934 | 3,590 | 3,937 (one) |
| Backend worker (the busy one) | 96 | 997 | 952 | **3,838** | 3,489 | **3,937** |
| **Backend** API | 119 | 132 | 124 | 194 | 126 | 218 |
| **PostgreSQL** | 45 | 51 | 53 | 72 | 60 | 88 |
| *Everything at once* | *438* | *1,450* | *1,412* | *4,777* | *4,447* | *5,334* |

Components peak at different moments (the Runner's while the runs run, the workers' while they
cluster afterwards), so "everything at once" is less than the column's sum. Projected with both
workers clustering two runs at the cap: another ~3.9 GB, about **9.3 GB** in all.

### What each scenario produced

| Scenario | Runner done | Result saved | `runner_logs` | `clustered` | Result JSON | In PostgreSQL (gzip) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 100k, batch | 31 s | 59 s | 130 MB | 3.7 MB | 3.5 MB | 1.2 MB |
| 100k, streaming | 33 s | 59 s | 130 MB | 2.3 MB | 0.9 MB | 0.3 MB |
| 500k × 6, batch | 113 s | 357 s | 644 MB | 25 MB | 21 MB | 7.3 MB |
| 500k × 6, streaming | 122 s | 317 s | 644 MB | 2.7 MB | 1.0 MB | 0.4 MB |
| 2 × 500k × 6, batch | 230 s | 464 s, 717 s | 2 × 644 MB | ~21 MB each | 19 MB, 20 MB | 13.6 MB both |

Every run clustered every evaluation it streamed. Times are from a 4-thread laptop: twelve islands
share four threads, so two concurrent runs take twice as long as one.

---

## 3. What the platform needs

**The Runner: small and bounded.** About 75 MB for the service, ~50 MB per master and 44–52 MB per
island, independent of the budget. At the limits (two runs of six islands) that is **under
0.8 GB**. `ISLAND_MEMORY` (2 GB) is a cap, never approached.

**Redis: the streams, plus what the workers' reads hold.** The streams cost what §1 says (2 ×
644 MB at the cap), but Redis peaked 520 MB higher, at 1.81 GB. The cause was a client output
buffer of 581 MB: the worker's `XREAD` has no `COUNT`, so when it falls behind, or picks up a run
whose stream is already complete, a single reply holds the whole backlog. With the backend's
`--maxmemory 2gb` that left ~240 MB of headroom: a little more and Redis refuses our `XADD`s, and
the runs fail at `BROKER_STALL_SECONDS`. After the streams expired `used_memory` fell back to 2 MB,
but the process kept 677 MB resident, and its append-only file reached 1.2 GB on disk.

**The Worker Pool: the dominant cost.** Batch clustering held ~7.5 KB per evaluation (3.8 GB for
500,000), streaming slightly less (3.5 GB). A worker does not give the memory back: after the cap
runs an idle worker still held 1.4 GB, and after about forty mixed runs 0.9 GB.

**The API and PostgreSQL** stay under 250 MB; serving a 21 MB result is the API's peak.

**Sizing.** At the measured limits (two concurrent runs, 500,000 evaluations each) with
two workers, the platform needs about **9–10 GB** at peak. Nearly all of it comes when both workers
cluster (~7.9 GB) while both streams are still in Redis (1.3–1.8 GB); the Runner's 0.8 GB peaks
earlier, while the runs run. The levers, all on the backend's side:

1. `XREAD … COUNT` (a few thousand entries) in the workers bounds the output buffer at a few MB.
   Then Redis needs only its streams, ~1.3 GB at the cap.
2. Redis `maxmemory` of 3 GB, if the reads stay unbounded.
3. A memory limit on each worker, so a large run kills the worker rather than something else on
   the host, and a fresh worker process per job, which hands the retained memory back.
4. A lower `evaluation_budget` cap: 250,000 halves every figure that grows with the budget.

**The cap is now 250,000** (owner, 2026-11-01): ~1.9 GB per worker, ~0.65 GB of streams for two
concurrent runs (~0.9 GB with the unbounded reads), ~5 GB for the platform at peak with two
workers. Halved from the 500,000 measurements, not measured again.
