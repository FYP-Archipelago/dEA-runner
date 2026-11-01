# The Five Algorithms

Five genome structures rather than five variations on bitstring-plus-one-point-crossover, so the
records exercise everything the clustering pipeline must handle: fixed-length discrete,
fixed-length continuous, continuous with carried state, and variable-length structured.

| Name | Genome | Encoding in the log | Domain |
| --- | --- | --- | --- |
| `ga` | bitstring **or** permutation | `bitstring` / `permutation` | 0/1 knapsack, TSP |
| `de` | real vector | `real_vector` | continuous |
| `pso` | real vector **+ velocity** | `real_vector_velocity` | continuous |
| `gp` | variable-length **tree** | `expression_tree` | symbolic regression |
| `es` | real vector **+ self-adaptive step sizes** | `real_vector_sigma` | continuous |

Every operator implements a standard, citable formulation, cited on the function that implements
it. This code may be described in the paper's methodology, so the goal is literature fidelity, not
cleverness: a well-known variant is implemented as published, including choices that look
improvable.

---

## Common structure

An algorithm implements two methods and three hooks:

```python
class Algorithm:
    def initialize(self, population_size) -> list[Individual]: ...
    def step(self, population) -> list[Individual]: ...          # one generation

    def genome_for_log(self, individual): ...       # what the location signature covers
    def diversity(self, population): ...            # (value, metric_name, sample_size)
    def serialize_migrant(self, individual): ...    # JSON-safe wire payload
    def deserialize_migrant(self, payload): ...
```

Whether `step` is generational, steady-state, (μ,λ) or a swarm update is entirely
the algorithm's business. **Migration is applied between steps by the island
runner, never inside an algorithm**, which keeps the migration mechanism
identical across all five and therefore comparable.

An algorithm has no access to the logger; see
[`ARCHITECTURE.md` §2](ARCHITECTURE.md).

---

## `ga` — Genetic Algorithm

Generational replacement with elitism (De Jong 1975), over bitstrings or
permutations. `representation: auto` picks from the benchmark's domain, which is
the normal case.

**References.** J. H. Holland, *Adaptation in Natural and Artificial Systems*,
1975; D. E. Goldberg, *Genetic Algorithms in Search, Optimization, and Machine
Learning*, 1989.

### Selection

| Value | Formulation |
| --- | --- |
| `tournament` (default) | Sample *k*, take the best. Goldberg & Deb, FOGA 1, 1991. |
| `rank` | Linear ranking, *p_i* = (2−s)/μ + 2i(s−1)/(μ(μ−1)). Baker, ICGA 1985. |

Both depend only on *order*, not on fitness magnitude. That is why neither is
fitness-proportionate: roulette needs an arbitrary transformation for a
minimisation objective like a TSP tour length, and the transformation — not the
algorithm — would end up setting the selection pressure.

### Binary operators

| Value | Formulation |
| --- | --- |
| `uniform` (default) | Per-locus swap. Syswerda, ICGA 1989. Maximal linkage disruption, which is right for the knapsack where adjacent item indices mean nothing. |
| `one_point` | Holland 1975. Cut drawn from 1…L−1, so at least one bit is always exchanged. |
| `two_point` | De Jong 1975. Reduces one-point's positional bias. |

Mutation is independent per-bit flip. `mutation_rate: null` resolves to **1/L**,
one expected flip per genome (Mühlenbein, PPSN II, 1992).

### Permutation operators

| Crossover | Preserves |
| --- | --- |
| `order` / `ox` (default) | *Relative* order — the structure that matters for a tour. Davis, IJCAI 1985. |
| `pmx` | Absolute position more strongly. Goldberg & Lingle, ICGA 1985. |
| `cycle` / `cx` | Absolute position completely: every allele keeps a position it held in one parent. Oliver, Smith & Holland, ICGA 1987. |

| Mutation | Notes |
| --- | --- |
| `inversion` (default) | Reverse a subsequence — Holland's inversion operator, and exactly a **2-opt move** on a symmetric tour, changing only two edges. That locality is why it beats swap on TSP. |
| `swap` | Exchange two positions. The minimal permutation move. |
| `scramble` | Shuffle a subsequence. Syswerda 1991. |
| `insertion` | Remove one element and reinsert it. Fogel 1988. |

### Provenance

Offspring are labelled by what actually fired: `crossover`, `mutation`,
`crossover_mutation`, or `clone` when neither did. A clone is a real offspring
that consumed an evaluation, and the STN self-loop it produces is a true
statement — the search stayed put.

**Elites keep their identity and are not re-logged.** An individual surviving *k*
generations appears once, not *k* times; re-logging would fabricate a self-loop
reflecting bookkeeping rather than search. Set `log_elites: true` to change this.

---

## `de` — Differential Evolution

**Reference.** R. Storn & K. Price, *Journal of Global Optimization* 11(4), 1997;
variants and bound handling from Price, Storn & Lampinen, *Differential
Evolution: A Practical Approach*, 2005.

One generation, per target vector *x_i*:

1. **Mutation** — donor *v* = *x_r1* + F(*x_r2* − *x_r3*), indices distinct and ≠ *i*.
2. **Crossover** — trial *u* takes component *j* from the donor when
   rand_j ≤ CR **or** *j* = *j*_rand. The forced index guarantees the trial
   differs from its target, so no evaluation is spent on an exact duplicate.
3. **Selection** — greedy and one-to-one; *u* replaces *x_i* only if at least as
   good. Ties go to the trial, which lets the population drift across neutral
   plateaus instead of stalling on them.

| Parameter | Default | Notes |
| --- | --- | --- |
| `strategy` | `rand/1` | `rand/1` · `rand/2` · `best/1` · `best/2` · `current-to-best/1` · `current-to-rand/1` |
| `f` | `0.5` | Differential weight; [0.4, 1.0] suggested. |
| `cr` | `0.9` | 0.9 for non-separable problems, ~0.1 for separable. |
| `crossover` | `bin` | `bin` (binomial) or `exp` (one contiguous circular block). |
| `dither` | `false` | Redraw F per trial from `dither_range`. |
| `bound_handling` | `midpoint` | `clip` · `reflect` · `reinitialize` · `midpoint` |

`rand/1` is the default because it is the most explorative, which is what islands
that are supposed to search *different* regions want.

**Bound handling matters more than it looks.** `clip` piles probability mass
exactly on the boundary — which helps on Schwefel, whose optimum sits near a
bound, and wastes trials on problems whose optimum is interior. `midpoint` resets
a violating component to the midpoint between the target and the violated bound,
keeping the trial near its parent.

**Provenance.** A trial's `parent_ids` are `[target, r1, r2, r3]`. DE's step is
genuinely four-parent, and recording only the target would erase the differential
structure that distinguishes a DE trajectory from a mutation-only one. A
surviving target is *not* re-logged: it is the same individual at the same point.

---

## `pso` — Particle Swarm Optimization

**References.** Kennedy & Eberhart, ICNN 1995; Shi & Eberhart, CEC 1998/1999
(inertia weight); Clerc & Kennedy, IEEE TEC 6(1), 2002 (constriction);
Kennedy & Mendes, CEC 2002 (ring neighbourhood).

```
v ← w·v + c₁r₁(p − x) + c₂r₂(g − x)
x ← x + v
```

with **r₁, r₂ drawn per component** — not per particle, a common and consequential
implementation error that turns the stochastic per-dimension search into a scaled
line search. There is a test for it.

| Parameter | Default | Notes |
| --- | --- | --- |
| `topology` | `gbest` | `gbest` (fully connected) or `ring` (k neighbours by index). |
| `neighbourhood_size` | `1` | Neighbours per side for `ring`. |
| `inertia` / `inertia_final` | `0.7298` | Decreased linearly across the run when they differ. |
| `cognitive`, `social` | `1.49445` | The standard parameter set (Bratton & Kennedy, SIS 2007). |
| `constriction` | `false` | Clerc's χ instead of an inertia weight; requires c₁+c₂ > 4. |
| `velocity_clamp` | `0.5` | Fraction of each dimension's range bounding \|v\|. |
| `bound_handling` | `clip_zero_velocity` | Absorbing wall (Robinson & Rahmat-Samii 2004). |

Velocities are initialised uniformly in ±v_max rather than to zero: a
zero-velocity swarm takes its first step purely along the cognitive/social axes,
and at generation 0 every particle *is* its own personal best, so it would not
move at all.

**Why PSO earns its place.** Its genome is structurally distinct — a point *plus
momentum*. The location signature covers **position only**: two particles at the
same point are the same STN node whatever their velocity. `full` genome mode
still records velocity, because the next move cannot be explained without it.

**Provenance.** `parent_ids` are `[previous position, informant]`. PSO has no
genetic parents but it does have *influences*, and the social link is exactly
what distinguishes a swarm's trajectory network from a population of independent
hill climbers.

**Migration transfers position, velocity and personal best.** A particle *is*
that triple; a migrant stripped of its momentum would be reset to a random walk
on arrival, making migration look far more disruptive than the policy intends.

---

## `gp` — Genetic Programming

**Reference.** J. R. Koza, *Genetic Programming*, MIT Press, 1992 — including the
specific probabilities, notably the **90/10 internal/terminal bias** in
crossover-point selection (§6.4). Without it, a random tree's leaf majority means
most crossovers swap a single terminal, which is a mutation in all but name.

| Parameter | Default | Notes |
| --- | --- | --- |
| `function_set` | `koza` | `arithmetic` · `koza` · `extended`, or an explicit list. |
| `min_init_depth` / `max_init_depth` | `2` / `6` | Ramped half-and-half range. |
| `max_depth` | `17` | Koza's limit. Over-deep offspring are **discarded**, not repaired. |
| `tournament_size` | `7` | Koza's value for symbolic regression. |
| `crossover_rate` | `0.9` | Subtree crossover. |
| `subtree_mutation_rate` | `0.05` | |
| `point_mutation_rate` | `0.03` | Structure-preserving: same shape, different symbol. |
| `hoist_mutation_rate` | `0.02` | Bloat control (Kinnear, CEC 1994) — can only shrink. |
| `internal_node_bias` | `0.9` | Koza's 90/10 rule. |
| `parsimony_coefficient` | `0.0` | Affects **selection only**, never the logged fitness. |

The four operator probabilities are applied as a mutually exclusive choice per
offspring; any remaining mass reproduces the parent unchanged.

**Protected primitives.** Koza's closure requirement: division by near-zero
returns 1.0, `log`/`sqrt` take the absolute value, `exp` is clipped to avoid
overflowing to `inf`. These are Koza's own definitions, kept rather than
"improved" — a different protection scheme changes the fitness landscape and
would make results incomparable with the GP literature. (The `exp` clip is the
one addition, and it is a ranking concern, not a correctness one: an `inf`
collapses a whole region of the search space onto the same invalid fitness,
destroying the gradient selection needs.)

**Parsimony pressure never touches the logged fitness.** A log whose `fitness`
silently included a size term would not be comparable with published RMSE figures
for the same benchmark.

**Why GP earns its place.** It is the only variable-length, structured genome —
exactly the case a fixed-width clustering pipeline cannot assume away. Its
location signature is the canonical prefix string, so downstream gets a
well-defined notion of "the same program" without knowing anything about trees.
`diversity` is the ratio of distinct canonical expressions, which catches the GP
failure mode that matters (the population collapsing onto cloned subtrees) at
O(n) rather than the O(n²) a tree-edit-distance measure would cost.

---

## `es` — Evolution Strategy

**References.** Rechenberg 1973; Schwefel 1981/1995; Bäck & Schwefel,
*Evolutionary Computation* 1(1), 1993.

Self-adaptive mutation, *n*-σ scheme:

```
σ'ᵢ = σᵢ · exp(τ'·N(0,1) + τ·Nᵢ(0,1))       τ' = 1/√(2n),  τ = 1/√(2√n)
x'ᵢ = xᵢ + σ'ᵢ · Nᵢ(0,1)
```

The first normal deviate is drawn once per individual (a global scale change),
the second once per component (differential scaling).

> **Order is load-bearing.** Step sizes are mutated *first*, and the object
> variables are perturbed with the **new** σ, so selection judges a step size by
> the offspring it actually produced. Reversing the order decouples the strategy
> parameter from the fitness that is supposed to select it, and self-adaptation
> silently stops working. There is a test for it.

| Parameter | Default | Notes |
| --- | --- | --- |
| `selection` | `comma` | `comma` (μ,λ) or `plus` (μ+λ). |
| `offspring_per_parent` | `7` | λ/μ. Schwefel's ratio. |
| `sigma_mode` | `n_sigma` | One σ per component, or `single` per individual. |
| `recombination` | `discrete` | Object variables: `discrete` · `intermediate` · `none`. |
| `strategy_recombination` | `intermediate` | Step sizes. Schwefel's recommended pairing. |
| `rho` | `2` | Parents per offspring; set to μ for global recombination. |
| `initial_sigma_ratio` | `0.1` | Initial σ as a fraction of each dimension's range. |

Comma selection discards parents every generation, which is what lets a
self-adaptive ES escape a step size that has become too small — the reason
Schwefel prefers it for multimodal problems. A σ floor of 1e-12 prevents
self-adaptation driving a lineage to a standstill.

**Why ES earns its place.** The search distribution is *part of the individual*,
inherited and selected along with it. The location signature covers the object
variables only; `full` mode records the σ vector, which is what makes step-size
adaptation visible downstream. Migration transfers σ too — resetting it on
arrival would force the migrant to re-learn its step size and confound any
measurement of migration's effect.

> **CMA-ES** is the alternative the specification allows. It is not implemented
> here because its strategy parameters (C, p_c, p_σ) are *population*-level, not
> per-individual, which fits the "genome carries strategy parameters" distinction
> less well — and that distinction is the reason ES is in the set of five.

---

## Adding a sixth algorithm

1. Subclass `Algorithm` in `algorithms/`, declaring `name`,
   `supported_domains` and `genome_encoding`.
2. Implement `initialize`, `step`, `serialize_migrant`, `deserialize_migrant`;
   override `genome_for_log` and `diversity` if the representation needs it.
3. `register(YourAlgorithm)` in `algorithms/registry.py`.

No logging code, and no orchestrator change. A new structural family of genome also needs a
`GenomeEncoding` member, its signature rule and compact codec in `archipelago_logging/genome.py`,
and the backend's clustering module taught to decode it: that changes the record format, so agree
it with the backend first.
