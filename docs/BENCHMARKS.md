# Benchmarks

All well-known problems from the optimisation and GP literature, with the source
cited on each class, and all selectable by name from a preset (`config/*.yaml`). The point of
using standard benchmarks is comparability with published results, which is worth
nothing if the implementation is subtly wrong — so every function is tested at
its documented global optimum, and every parser is tested against the real
instance files rather than a synthetic fixture.

```bash
python -c "from benchmarks.registry import available_benchmarks as b; print(*b().items(), sep='\n')"   # names and domains
```

## Real instances

14 files are committed under `benchmarks/data/`, with the source URL, size and
SHA-256 of each recorded in `MANIFEST.json`.

| Group | Files | Used by |
| --- | --- | --- |
| `tsp/` | TSPLIB eil51, berlin52, att48, kroA100 — with published optima | `ga_tsp.yaml`, `full_dataset.yaml` |
| `knapsack/` | Pisinger uncorrelated / weakly / strongly / inverse-strongly correlated, n=100 and 200 | `ga_knapsack.yaml` |
| `regression/` | UCI Yacht (308×6), UCI Airfoil (1503×5) | `gp_yacht.yaml` |
| `cec/` | CEC2017 shift vectors | `de_rastrigin.yaml` |

The instances are **committed to the repository**, so a clone has byte-identical
data with no network access, and `run_start.datasets` records the SHA-256 of the
file a run actually read. `benchmarks/data/README.md` has the details, including
why the CEC rotation matrices are **not** among them.

**There is no instance generator, and no generated fallback.** A benchmark that
takes an instance file requires one and fails loudly without it. A search is
only repeatable with a `seed` and without migration, so the *problem* is the one
thing that must be fixed between every pair of runs that are meant to be compared,
and a generated stand-in quietly substituted for a missing file would take that
away while still looking like the named benchmark. Relative paths resolve against the
repository root, so the same config means the same file whether it is read by the
master on the host or by an island inside a container.

```yaml
benchmark: tsp
benchmark_params:
  instance_file: benchmarks/data/tsp/berlin52.tsp
```

`benchmark_params` is validated against the problem's constructor: a typo is an
error at config time, not a silent default.

---

## Continuous functions

Domains are the ones conventionally used with each function, so a convergence
curve produced here is directly comparable with published curves.

| Name | Formula | Domain | Optimum | Character |
| --- | --- | --- | --- | --- |
| `sphere` | Σxᵢ² | [−5.12, 5.12]ⁿ | 0 at **0** | unimodal, separable, convex |
| `rastrigin` | 10n + Σ(xᵢ² − 10cos 2πxᵢ) | [−5.12, 5.12]ⁿ | 0 at **0** | highly multimodal, separable — the standard premature-convergence test |
| `ackley` | −20e^(−0.2√(Σxᵢ²/n)) − e^(Σcos2πxᵢ/n) + 20 + e | [−32.768, 32.768]ⁿ | 0 at **0** | flat outer region, deep central basin — the difficulty is the plateau |
| `rosenbrock` | Σ[100(xᵢ₊₁−xᵢ²)² + (xᵢ−1)²] | [−5, 10]ⁿ | 0 at **1** | unimodal, non-separable, narrow curved valley |
| `griewank` | 1 + Σxᵢ²/4000 − Πcos(xᵢ/√i) | [−600, 600]ⁿ | 0 at **0** | gets *easier* with dimension |
| `schwefel` | 418.9829n − Σxᵢ sin√\|xᵢ\| | [−500, 500]ⁿ | 0 at **420.9687** | deceptive: optimum near a bound, far from the second-best basin |

**References.** De Jong 1975 (Sphere, and F2 for Rosenbrock); Rastrigin 1974 with
Mühlenbein et al. 1991's n-dimensional generalisation; Ackley 1987 with Bäck
1996's parameterisation; Rosenbrock 1960; Griewank 1981; Schwefel 1981.

### Shifted and rotated variants

Raw benchmark functions have their optimum at the origin and are separable, which
flatters algorithms that happen to be origin-biased or that vary one coordinate
at a time. The CEC suites address this by composing each base function with a
shift and a rotation:

```
g(x) = f(M(x − o))
```

`shifted_rotated` implements exactly that, taking **o** and **M** from the
official CEC data files:

```yaml
benchmark: shifted_rotated
benchmark_params:
  function: rastrigin
  dimension: 10
  shift_file: benchmarks/data/cec/shift_data_3.txt
  # rotation_file: ...   # no rotation matrix ships with this repo
```

Both files are **validated on load**: the shift must lie inside the function's
domain, and the matrix must actually be orthogonal — a non-orthogonal matrix is
not a rotation, it rescales and shears the space, producing something that
resembles the named benchmark without being it.

Supplying a shift file also switches the search box to the CEC domain,
`[-100, 100]^D`, which is what the suite is defined on and what its shift vectors
are drawn for.

**Omitting a file is a no-op, never a substitute.** No `shift_file` means the
optimum stays at the origin; no `rotation_file` means the identity matrix. The
problem then simply *is* the plain base function, and `describe()` reports
`shifted: false` / `rotated: false` so the log says which it was. Generating a
shift from a seed instead would produce a problem that is not the CEC one, cannot
be compared with published results, and is indistinguishable in the log from one
that is.

**References.** Liang, Qu & Suganthan, CEC 2014/2017 problem definitions; the
rotation device is Salomon, *BioSystems* 39, 1996.

```yaml
benchmark: shifted_rotated
benchmark_params:
  function: rastrigin
  dimension: 10
  shift_file: benchmarks/data/cec/shift_data_3.txt
```

See `benchmarks/data/README.md` for why the rotation matrices from the usual
public mirror are not shipped.

---

## `knapsack` — 0/1 Knapsack

Binary encoding, maximisation.

```yaml
benchmark: knapsack
benchmark_params:
  instance_file: benchmarks/data/knapsack/strongly_correlated_n100_R1000.kp
```

`instance_file` is the only parameter, and it is required. The shipped instances
come from Pisinger's families, named by the family they belong to:

| Family | Values | Hardness |
| --- | --- | --- |
| `uncorrelated` | v ~ U[1,R], independent of w | easiest — greedy density ordering is near-optimal |
| `weakly_correlated` | v = w + U[−R/10, R/10] | realistic, moderate |
| `strongly_correlated` | v = w + R/10 | hard — near-identical value density everywhere |
| `inverse_strongly_correlated` | w = v + R/10 | hard, mirrored |

The loader accepts the kplib layout only. kplib files carry no declared optimum,
so `known_optimum` is `None` for every shipped instance and results are reported
as raw values rather than as an optimality gap.

**Reference.** D. Pisinger, "Where are the hard knapsack problems?", *Computers &
Operations Research* 32(9), 2005.

### Infeasibility

Repair operators are out of scope for the project, so an overweight solution is
handled by graded penalty:

```
f(x) = Σvᵢxᵢ − ρ·max(0, Σwᵢxᵢ − c)     with     ρ = maxᵢ(vᵢ/wᵢ)
```

With that multiplier, dropping any single overweight item is never worse than
keeping it, so no infeasible solution can outscore the best feasible one and the
penalised landscape keeps its optimum where the true one is. **`feasible` and
`constraint_violation` are logged separately from `fitness`**, so downstream can
always distinguish a genuinely good solution from a well-penalised bad one, and
`objective` carries the unpenalised value.

**Reference.** Michalewicz, *Genetic Algorithms + Data Structures = Evolution
Programs*, 3rd ed., 1996, §15; Gottlieb, *Artificial Evolution*, 1999.

### Real instances

```yaml
benchmark_params:
  instance_file: benchmarks/data/knapsack/strongly_correlated_n100_R1000.kp
```

One layout is parsed — **kplib** (`.kp`, what the downloaded instances use):
count and capacity on their own lines, blank-separated, then `value weight`
pairs:

```
100
29017

945 845
858 758
```


Pisinger's own layout (a `n`/`c`/`z` header, then `index value weight`) is
**detected and rejected by name**, rather than misparsed as kplib:

```
knapPI_1_100_1000_1
n 100
c 995
z 9147
1 94 485
```

That file carries `z`, the known optimum, which kplib's does not. Switching the
shipped instances to Pisinger's distribution is what would make optimality-gap
reporting possible; it would need this parser restored.

---

## `tsp` — Travelling Salesman Problem

Permutation encoding, minimisation. A genome is a closed tour, so the length
includes the return edge. **Every permutation is feasible**, which is why this
problem needs no penalty and no repair — the burden falls on the algorithm to use
permutation-preserving operators.

```yaml
benchmark: tsp
benchmark_params: {instance_file: benchmarks/data/tsp/berlin52.tsp}
```

| Instance | Cities | Known optimum |
| --- | --- | --- |
| eil51 | 51 | 426 |
| berlin52 | 52 | 7542 |
| att48 | 48 | 10628 |
| kroA100 | 100 | 21282 |

Set `termination.target_fitness` to one of these to stop as soon as an island
solves the instance.

### TSPLIB support

`load_tsplib_file` implements the format directly (Reinelt, *ORSA Journal on
Computing* 3(4), 1991):

* `NODE_COORD_SECTION` with `EDGE_WEIGHT_TYPE` ∈ {`EUC_2D`, `CEIL_2D`, `MAN_2D`,
  `MAX_2D`, `ATT`, `GEO`}
* `EDGE_WEIGHT_SECTION` with `EDGE_WEIGHT_FORMAT` ∈ {`FULL_MATRIX`, `UPPER_ROW`,
  `LOWER_ROW`, `UPPER_DIAG_ROW`, `LOWER_DIAG_ROW`}

**TSPLIB's integer rounding is reproduced exactly** — `EUC_2D` rounds to the
nearest integer, `ATT` uses its own pseudo-Euclidean rule, `GEO` uses `DDD.MM`
coordinates on a 6378.388 km sphere. This matters: published tour lengths for
TSPLIB instances are integers computed this way, and a plain floating-point
Euclidean distance would produce numbers that look right but match no published
optimum.

Without a file, instances are uniform random Euclidean points with `EUC_2D`
rounding — the standard random-TSP model (Beardwood, Halton & Hammersley 1959).

---

## Symbolic regression

For GP. The benchmark supplies the data and the error metric; the **function set
and tree encoding belong to the algorithm**, because those are properties of the
algorithm's configuration, not of the problem.

| Name | Target | Sampling |
| --- | --- | --- |
| `nguyen1` … `nguyen4` | x³+x²+x through x⁶+…+x | 20 pts, U[−1,1] |
| `nguyen5` | sin(x²)cos(x) − 1 | 20 pts, U[−1,1] |
| `nguyen6` | sin(x) + sin(x+x²) | 20 pts, U[−1,1] |
| `nguyen7` | ln(x+1) + ln(x²+1) | 20 pts, U[0,2] |
| `nguyen8` | √x | 20 pts, U[0,4] |
| `nguyen9` | sin(x) + sin(y²) | 100 pts, U[0,1]² |
| `nguyen10` | 2·sin(x)·cos(y) | 100 pts, U[0,1]² |
| `pagie1` | 1/(1+x⁻⁴) + 1/(1+y⁻⁴) | fixed 26×26 grid on [−5,5]² |
| `keijzer6` | Σᵢ₌₁ˣ 1/i | train x ∈ 1…50, **test x ∈ 1…120** |
| `korns1` | 1.57 + 24.3·x₃ | 1000 pts, U[−50,50]⁵ — only x₃ is relevant |

**References.** McDermott et al., "Genetic programming needs better benchmarks",
GECCO 2012; Uy et al., *GPEM* 12(2), 2011 (Nguyen); Pagie & Hogeweg,
*Evolutionary Computation* 5(4), 1997; Keijzer, EuroGP 2003; Korns, GPTP IX, 2011.

**Keijzer-6's test range deliberately leaves the training domain** — it measures
extrapolation, not interpolation. Held-out error is reported in
`island_end.algorithm_summary.test_error`, because a GP run's training error
alone cannot distinguish a solved problem from an overfitted one.

`metric` is `rmse` (default), `mse` or `mae`. RMSE is what the benchmark papers
report, so it is what keeps results comparable with them.

**Invalid expressions** — protected division near zero, overflow — produce
`INVALID_FITNESS` (1e12), large but **finite**. An actual `inf` would make the
island's mean fitness `inf` and break selection arithmetic for everyone.

### Real data

Two real datasets are selectable by name like any other benchmark:

| Name | Rows | Features | Target |
| --- | --- | --- | --- |
| `yacht` | 308 | 6 | residuary resistance |
| `airfoil` | 1503 | 5 | scaled sound pressure level |

```yaml
benchmark: yacht
benchmark_params:
  metric: rmse
```

Columns are named (`froude_number`, `angle_of_attack`, …) rather than `x0..xn`,
so an evolved expression is readable. Row count drives GP evaluation cost — the
tree runs over every row for every individual — which is why `gp_yacht.yaml` uses
the smaller set.

Real data changes the problem in ways worth having in the dataset: there is no
exact expression to find, so fitness plateaus at a non-zero error and the
stagnation rule, rather than a target fitness, is what ends the run.

Any other delimited table works too:

```yaml
benchmark: symbolic
benchmark_params:
  benchmark: csv
  csv_path: data/my_data.csv
  csv_target_column: y
```

The loader sniffs comma / tab / whitespace delimiters, CRLF line endings, and
whether there is a header — the three shapes the real UCI files actually ship in.
A plain loader rather than a scikit-learn dependency, which would otherwise be
pulled into the island image for one `load_*` helper.

---

## Instance files

```bash
pytest tests/test_datasets.py   # check every file against MANIFEST.json
```

Files live under `benchmarks/data/` and are referenced by path from a config.
`MANIFEST.json` records provenance; `run_start.datasets` records the SHA-256 of
whichever instance a run used. That pins the problem for every run, seeded or
not.

---

## Adding a benchmark

1. Subclass `ContinuousProblem`, `BinaryProblem`, `PermutationProblem` or
   `RegressionProblem` in `benchmarks/`, implementing `evaluate` and
   `random_solution`, and citing the source in the docstring.
2. `register("name", YourProblem, Domain.X)` in `benchmarks/registry.py`.

No orchestrator change. `describe()` must return everything needed to reconstruct
the instance, because the log is required to be sufficient on its own to
reproduce a run.
