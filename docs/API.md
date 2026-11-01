# The Runner's API

The Archipelago Backend starts, follows and cancels runs over five HTTP endpoints; every run's
records go to the Redis stream `run:{run_id}:runner_logs` ([`LOGGING_FORMAT.md`](LOGGING_FORMAT.md)).
The Swagger UI at `/docs` shows the same document and sends real requests; the OpenAPI document is
`/openapi.json`. The backend-facing version is the "Runner API Contract" doc (`docs/HANDOFF.md`
§9).

---

## Conventions

* Every body is JSON. The Runner has no authentication: it must be reachable only from the
  backend's network (README, "Production").
* **`run_id`** is minted by the backend as `run-YYYYMMDDTHHMMSSZ-xxxxxxxx` (UTC timestamp, 8
  lowercase hex digits) and used verbatim in every record, container name and the stream key.
* **The run object** answers every request about a run: `{"run_id": …, "state": …, "error": …}`.
* **Every error response**, whatever its status, is `{"error": "<readable message>"}`, fit to show
  a user.

| `state` | Meaning | Terminal |
| --- | --- | --- |
| `queued` | Waiting for a slot: at most `MAX_CONCURRENT_RUNS` (2) run at once; the rest wait in arrival order | no |
| `running` | The run's containers are up and its records are streaming | no |
| `finished` | Ended normally: `run_end.termination_reason` is `completed` | yes |
| `failed` | Ended with an error (`error`), or stopped by the time limit (`time_limit`); `error` says why, as `run_end.error` does | yes |
| `cancelled` | Cancelled: `run_end.termination_reason` is `aborted`, or there is no stream if it was still queued | yes |

`error` is a string only when `state` is `failed`. A terminal state never changes.

```
POST /runs ──► queued ──(a slot frees)──► running ──► finished
                 │                           ├──────► failed      (error, or the time limit)
                 └──(cancel)──► cancelled ◄──┘(cancel, after the islands wind down)
```

---

## Behaviour

**Starting.** `POST /runs` answers at once: `queued`, or `running` when a slot was free. A run is a
preset plus at most the published overrides (bounds below); everything else comes from the preset.
`migration.enabled: false` runs a baseline without migration (the other migration keys then do not
matter), which with a `seed` repeats record for record. Checked in order: the body's shape,
`run_id`, the preset, each override's key, type and bounds, the whole configuration (`num_migrants`
below the per-island population, `interval` ≤ `target_generations`, a torus only with 6 islands),
then the container limit. Each failure is a 422 naming the problem and its numbers.

**Idempotency.** A repeat of a request — same `run_id`, same preset and overrides as sent, key
order ignored — answers the run's current state and starts nothing. The same `run_id` with
anything else is a 409. So is a `run_id` whose stream already exists in Redis when the Runner has no
record of it (it restarted): a retried request can never start a second run into an old stream.

**Following.** Poll `GET /runs/{run_id}` every 2 s. A 404 means the Runner knows no such run;
treat it as failed. A 503 means Redis is unreachable, so a run the Runner no longer remembers
cannot be looked up: retry, never treat it as failed.

**Cancelling.** `POST /runs/{run_id}/cancel` takes no body and is idempotent. A queued run is
cancelled at once (200) and never writes a stream. A running run winds down (202, `state` still
`running`): each island finishes its generation, then the stream ends with `run_end`
(`termination_reason: aborted`) and the state becomes `cancelled`, usually within seconds. A
terminal run is left as it is (200).

**The time limit.** Always on (`RUN_TIME_LIMIT_SECONDS`, an hour by default, at most a day): a run
still going is wound down the same way and ends with `run_end.termination_reason: time_limit` and
an `error` saying so. The backend treats that as a failure, and so does the API: `failed`, also
after a Runner restart. A hung island never says goodbye; the limit is what ends its run.

**Restarts.** Each run's master is its own container, so running runs survive a Runner restart:
the new Runner finds their masters by label and reports them `running` until they end, and an
exact repeat of their request still answers 202. Queued runs are lost: `GET` answers 404. A run
the Runner no longer remembers is answered from its stream's `run_end`.

**The stream.** `run_start` first, `run_end` last, nothing after it; each island's records in
`seq` order. `run_end.error` repeats why a run failed. The backend writes `abort` when `GET`
answers 404, or `failed` while the stream has no `run_end` (a Runner that lost Redis cannot write
one); the first terminator wins. The Runner looks past entries it did not write.

---

## Reference

<!-- BEGIN GENERATED API REFERENCE -->
Generated from the service (version 0.5.0) by `scripts/generate_api_docs.py`; the Swagger UI at `/docs` shows the same document.

### Endpoints

### `GET /presets`

**List the presets.** The presets the frontend can offer, each with its default for every override key, and the overrides a run may set with their bounds.

| Status | Body | When |
| --- | --- | --- |
| 200 | `PresetsView` | The presets and the override bounds. |

### `POST /runs`

**Start a run.** Starts the run and answers at once; the run itself is asynchronous.

Body: `RunRequest` (below).

| Status | Body | When |
| --- | --- | --- |
| 202 | `RunView` | Accepted: queued, or running if a slot was free. An exact repeat of an earlier request with the same run_id returns that run's current state and starts nothing. |
| 422 | `ErrorBody` | The body is not valid JSON or not this shape; run_id is malformed; unknown preset; unknown override key; a value of the wrong type or out of bounds; a combination that cannot run (num_migrants not below the per-island population, interval above target_generations, a torus with fewer than 6 islands); or more containers than the Runner's limit. |
| 409 | `ErrorBody` | The run_id was already used with a different preset or overrides, or its stream already exists but the Runner has no record of the run (it restarted). |
| 503 | `ErrorBody` | Redis is unreachable. Nothing was started, so retrying with the same run_id is safe. |

### `GET /runs/{run_id}`

**Get a run's state.** Poll every 2 s. A run this Runner no longer remembers (it restarted) is answered from its stream's run_end.

Path parameter `run_id`: The run's id, as given to POST /runs.

| Status | Body | When |
| --- | --- | --- |
| 200 | `RunView` | The run. |
| 404 | `ErrorBody` | No such run; treat it as failed. |
| 503 | `ErrorBody` | Redis is unreachable, so a run this Runner no longer remembers cannot be looked up in its stream. Retry; never treat it as failed. |

### `POST /runs/{run_id}/cancel`

**Cancel a run.** No body. Idempotent: repeating it changes nothing.

Path parameter `run_id`: The run's id, as given to POST /runs.

| Status | Body | When |
| --- | --- | --- |
| 200 | `RunView` | The run is now terminal: a queued run is cancelled at once and never writes a stream; a run already finished, failed or cancelled is left as it is. |
| 202 | `RunView` | The run was running and is winding down: each island finishes its current generation, then the stream ends with run_end (termination_reason aborted). state stays running until then, then becomes cancelled. |
| 404 | `ErrorBody` | No such run. |
| 503 | `ErrorBody` | Redis is unreachable, so a run this Runner no longer remembers cannot be looked up in its stream. Retry; never treat it as failed. |

### `GET /health`

**Check the Runner can serve runs.** For the container healthcheck: 200 when the Runner is up and can reach Redis.

| Status | Body | When |
| --- | --- | --- |
| 200 | `HealthView` | The Runner can reach Redis: `{"status": "ok"}`. |
| 503 | `HealthView` | Redis is unreachable. |

### Schemas

#### `ErrorBody`

Every error response, whatever its status.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `error` | `string` | yes | What went wrong, in words fit to show a user. |

#### `HealthView`

Whether the Runner can serve runs.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `status` | `"ok" \| "unavailable"` | yes | ok, or unavailable when Redis cannot be reached. |
| `error` | `string \| null` | no | Why not; present only when unavailable. |

#### `PresetView`

One preset, with its value for every override key.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `preset` | `string` | yes | The name to send as `preset` in POST /runs. |
| `algorithm` | `string` | yes | ga, de, pso, gp or es. |
| `benchmark` | `string` | yes | The problem it solves. |
| `description` | `string` | yes | What the preset runs, in a sentence. |
| `defaults` | `object` | yes | The preset's value for every override key that has a default (all but seed). |

#### `PresetsView`

The presets, and every key a request may override with its bounds.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `overrides` | `object of object` | yes | Each override key's type (int, bool or choice) and its bounds: min, max, choices; a bound with no limit is left out, and optional: true marks a key with no default. |
| `presets` | `PresetView[]` | yes | Every preset, in name order. |

#### `RunRequest`

The body of `POST /runs`: a preset, and at most the published overrides.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `run_id` | `string` | yes | Minted by the backend as run-YYYYMMDDTHHMMSSZ-xxxxxxxx (UTC timestamp, 8 lowercase hex digits) and used verbatim in every record, container and the stream key. |
| `preset` | `string` | yes | A preset name from GET /presets. |
| `overrides` | `object` | no | Optional. Dotted keys from GET /presets `overrides`, each within its bounds and of the exact JSON type; a key left out takes the preset's default. |

#### `RunView`

A run, as every endpoint about one answers.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `run_id` | `string` | yes | The run's id, as given to POST /runs. |
| `state` | `"queued" \| "running" \| "finished" \| "failed" \| "cancelled"` | yes | queued: waiting for a slot. running: its containers are up and its records are streaming. finished: ended normally. failed: ended with an error, or stopped by the Runner's time limit. cancelled: cancelled through POST /runs/{run_id}/cancel. |
| `error` | `string \| null` | yes | Why the run failed, in words fit to show a user; null unless state is failed. The same text as the stream's run_end.error. |

### Override bounds

| Key | Type | Bounds |
| --- | --- | --- |
| `num_islands` | int | ≥ 4, ≤ 6 |
| `evaluation_budget` | int | ≥ 1,000, ≤ 250,000 |
| `target_generations` | int | ≥ 2 |
| `migration.enabled` | bool | `true` · `false` |
| `migration.topology` | choice | `bidirectional_ring` · `fully_connected` · `lattice` · `random` · `ring` · `star` · `torus` |
| `migration.interval` | int | ≥ 1 |
| `migration.num_migrants` | int | ≥ 1 |
| `seed` | int | ≥ 0, ≤ 4,294,967,295, optional, no default |

### Presets and their defaults

| Preset | Algorithm | Benchmark | `num_islands` | `evaluation_budget` | `target_generations` | `migration.enabled` | `migration.topology` | `migration.interval` | `migration.num_migrants` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `de_rastrigin` | de | shifted_rotated | 5 | 100,000 | 200 | true | ring | 20 | 5 |
| `docker_de_sphere` | de | sphere | 5 | 20,000 | 60 | true | ring | 5 | 3 |
| `es_schwefel` | es | schwefel | 5 | 100,000 | 200 | true | star | 15 | 3 |
| `full_dataset` | ga | tsp | 5 | 100,000 | 250 | true | ring | 20 | 5 |
| `ga_knapsack` | ga | knapsack | 5 | 100,000 | 200 | true | fully_connected | 25 | 2 |
| `ga_tsp` | ga | tsp | 5 | 100,000 | 300 | true | lattice | 30 | 4 |
| `gp_nguyen7` | gp | nguyen7 | 5 | 100,000 | 100 | true | ring | 10 | 5 |
| `gp_yacht` | gp | yacht | 5 | 100,000 | 80 | true | ring | 10 | 5 |
| `pso_ackley` | pso | ackley | 5 | 100,000 | 200 | true | bidirectional_ring | 25 | 3 |
<!-- END GENERATED API REFERENCE -->
