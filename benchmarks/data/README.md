# Benchmark instance files

Real third-party instances, fetched by `scripts/fetch_datasets.py`:

```bash
python scripts/fetch_datasets.py                # everything
python scripts/fetch_datasets.py --only tsp     # one group
python scripts/fetch_datasets.py --verify       # check checksums, download nothing
```

`MANIFEST.json` records the source URL, byte size and SHA-256 of every file.
**That manifest is the point.** Search randomness is deliberately not
reproducible in this harness; what must be identical between runs is the
*dataset*, and a checksum is how that is verifiable after the fact rather than
assumed. `run_start.datasets` carries the SHA-256 of whichever instance a run
actually used.

These files are **committed to the repository**, so a clone runs offline with
byte-identical data and nothing to download. They are also required: a benchmark
that takes an instance file will not invent one if it is missing, because a
generated stand-in would answer to the benchmark's name while being a different
problem, and the log could not tell the two apart.

---

## `tsp/` — TSPLIB

| File | Cities | Weight type | Known optimum |
| --- | --- | --- | --- |
| `eil51.tsp` | 51 | EUC_2D | 426 |
| `berlin52.tsp` | 52 | EUC_2D | 7542 |
| `att48.tsp` | 48 | ATT | 10628 |
| `kroA100.tsp` | 100 | EUC_2D | 21282 |

```yaml
benchmark: tsp
benchmark_params:
  instance_file: benchmarks/data/tsp/berlin52.tsp
```

TSPLIB's integer rounding is reproduced exactly — `EUC_2D` rounds to the nearest
integer, `ATT` uses its own pseudo-Euclidean rule — so a tour length from this
harness is directly comparable with the published optima above. Set
`termination.target_fitness` to one of them to stop as soon as an island solves
the instance.

Source: G. Reinelt, *TSPLIB*, ORSA Journal on Computing 3(4), 1991. Fetched from
the `mastqe/tsplib` mirror. Asymmetric TSP (`TYPE: ATSP`) is **not** supported
and is rejected with a clear message.

## `knapsack/` — Pisinger's families

| File | Items | Family |
| --- | --- | --- |
| `uncorrelated_n100_R1000.kp` | 100 | uncorrelated — easiest |
| `weakly_correlated_n100_R1000.kp` | 100 | weakly correlated |
| `strongly_correlated_n100_R1000.kp` | 100 | strongly correlated — hard |
| `inverse_strongly_correlated_n100_R1000.kp` | 100 | inverse strongly correlated |
| `uncorrelated_n200_R1000.kp` | 200 | uncorrelated |
| `strongly_correlated_n200_R1000.kp` | 200 | strongly correlated |

```yaml
benchmark: knapsack
benchmark_params:
  instance_file: benchmarks/data/knapsack/strongly_correlated_n100_R1000.kp
```

One layout parses: **kplib** (`.kp`, the item count and the capacity on separate
lines, then one `value weight` pair per line) — what these files use. A file in
Pisinger's own layout (`n`/`c`/`z` header, then `index value weight`) is detected
and rejected by name rather than misparsed.

Note that kplib files carry no optimum, so `known_optimum` is `None` for every
instance here and results cannot be reported as an optimality gap. Pisinger's own
distribution does carry it, in the `z` header — switching source would be the way
to recover that.

Source: the Knapsack Problem Library (`likr/kplib`), generated per
D. Pisinger, "Where are the hard knapsack problems?", *Computers & Operations
Research* 32(9), 2005.

## `regression/` — real data for GP

| File | Rows | Features | Target |
| --- | --- | --- | --- |
| `yacht_hydrodynamics.data` | 308 | 6 | residuary resistance |
| `airfoil_self_noise.dat` | 1503 | 5 | scaled sound pressure level |

```yaml
benchmark: yacht      # or: airfoil
benchmark_params:
  metric: rmse
```

Selected by name like any other benchmark. Both are standard symbolic-regression
sets and both are small enough that a tree evaluation over the whole dataset
stays cheap — which matters, because GP evaluates every row for every individual.
Yacht is the cheaper of the two and is what `config/gp_yacht.yaml` uses.

Columns are named (`froude_number`, `angle_of_attack`, …) rather than `x0..xn`,
so an evolved expression is readable.

Sources: UCI Machine Learning Repository — Gerritsma, Onnink & Versluis (Yacht
Hydrodynamics); Brooks, Pope & Marcolini, NASA RP-1218, 1989 (Airfoil
Self-Noise).

## `cec/` — CEC2017 shift vectors

| File | Contents |
| --- | --- |
| `shift_data_1.txt` | shift vector, function 1, 100 values |
| `shift_data_3.txt` | shift vector, function 3, 100 values |

```yaml
benchmark: shifted_rotated
benchmark_params:
  function: rastrigin
  dimension: 10
  shift_file: benchmarks/data/cec/shift_data_3.txt
```

Supplying a shift file **also switches the search box to the CEC domain**,
`[-100, 100]^D`, which is what the whole suite is defined on and what its shift
vectors are drawn for. Pairing CEC data with a base function's classical domain
(Rastrigin's `[-5.12, 5.12]`) would put the optimum outside the box, and is
rejected.

### Why there are no rotation matrices here

`ShiftedRotated` accepts a `rotation_file` and **verifies that the matrix is
orthogonal**, because a matrix that is not is not a rotation: it rescales and
shears the search space, silently changing the function's conditioning while
still looking like the named benchmark.

The `M_*.txt` files on the mirror these shift vectors came from fail that check —
row norms up to 1.9, determinant −3.5 — so they are not fetched. Without a
rotation file the problem is simply **not rotated**: the identity matrix, and
`describe()` reports `rotated: false`. Nothing is generated to stand in for the
official matrix, because a Haar-random one would be a different problem wearing
the same name and the log could not tell the two apart.

Genuine CEC matrices from the official distribution can be dropped in here and
will be accepted; a bad set cannot slip through.

## Adding your own

Any file placed here can be referenced by path from a config. Re-run
`scripts/fetch_datasets.py --verify` after changing anything, and add new
downloads to the `DATASETS` list in that script so their provenance is recorded
rather than lost.
