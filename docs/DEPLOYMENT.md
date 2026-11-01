# Deploying the Runner

The Runner is deployed as one container (`docker-compose.prod.yml`) on a Docker host. For every
run it starts a master container, and the master one container per island, through the host's
Docker socket. Redis is the Archipelago Backend's. Where the Runner and Redis will run is not yet
decided (`docs/HANDOFF.md` §8); this guide holds for any of the arrangements README "Production"
lists.

---

## What the host needs

| | |
| --- | --- |
| Docker | Engine and the Compose plugin (tested with Engine 29.8 and Compose 5.5). The Runner mounts the socket at `/var/run/docker.sock`. |
| Images | `archipelago-runner` (225 MB) and `archipelago-island` (215 MB), built on the host by `up --build`. Nothing is pulled from a registry. |
| CPU | One core per island keeps an island at full speed: two runs of 5–6 islands use 10–12. Fewer cores only make runs slower. `ISLAND_CPUS` (1.0) caps each island. |
| Memory | About 0.3 GB per running run on the Runner's side (an island uses ~45–50 MB, a master ~50 MB, the service ~75 MB); `ISLAND_MEMORY` (2 GB) is a cap per island, not a reservation. The whole platform's needs, backend included, are in [`MEMORY_MEASUREMENTS.md`](MEMORY_MEASUREMENTS.md) §3. |
| Network | The Runner reaches Redis (README "Reaching Redis"); the backend reaches the Runner's port, bound to `RUNNER_BIND`. |

**Redis** (the backend's) must have `maxmemory-policy noeviction`, no `MAXLEN` on the streams, a
password, and about 0.8 GB free for runner streams: two concurrent runs at the 250,000-evaluation
cap hold ~650 MB. Measured at the old cap of 500,000: 1.3 GB for the streams, and with the
backend's workers reading without `COUNT` Redis peaked at 1.81 GB of its 2 GB `maxmemory` ([`MEMORY_MEASUREMENTS.md`](MEMORY_MEASUREMENTS.md) §3). The backend expires
each stream once processed. A restricted ACL user needs `PING`, `XADD`, `XLEN`, `XRANGE`, `XREVRANGE`,
`CLIENT ID` and `CLIENT KILL`.

---

## Deploying

```bash
git clone git@github.com:FYP-Archipelago/baseline-dEA.git && cd baseline-dEA
git checkout <the commit or tag to deploy>
cp .env.prod.example .env.prod        # REDIS_URL, RUNNER_BIND; optional settings below them
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

Then check it, from a machine that can reach `RUNNER_BIND`:

```bash
curl -s http://<RUNNER_BIND>:<RUNNER_PORT>/health    # {"status":"ok"}: up, and Redis answers
```

And run one quick job end to end. The dummy backend needs this repository's Python dependencies
(`pip install -e .`); it starts the run, follows it, and checks its stream in Redis:

```bash
python scripts/dummy_backend.py --runner-url http://<RUNNER_BIND>:<RUNNER_PORT> --redis-url "<REDIS_URL>" run docker_de_sphere --set evaluation_budget=5000
```

It prints `finished` and `stream valid: …` and exits 0. Its stream stays in Redis until deleted;
the Swagger UI at `/docs` lists every endpoint.

---

## Operating it

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f runner    # requests, queue errors
docker ps --filter label=app=archipelago                  # every master and island now running
docker logs archipelago-<last 8 hex of the run id>-master # one run's master, while it runs
```

* **A run's state** is `GET /runs/{run_id}`. Its master and islands are removed when it ends; its
  records stay in Redis.
* **The Runner restarts itself** (`unless-stopped`), and a restarted Runner keeps the runs that were
  running. Runs still queued are lost; the backend sees 404 for them and fails them.
* **Logs** are capped at 5 × 10 MB for the Runner. Masters and islands are removed with their run,
  so their logs go with them; a failed island's last 40 lines are kept in the stream.
* **Orphans** appear only if a master is killed from outside (the Runner removes what a dead master
  leaves when it notices): `docker ps -a --filter label=archipelago.run_id=<run id>`.
* **Disk:** old images after upgrades, `docker image prune`.

---

## Upgrading and rolling back

```bash
git fetch && git checkout <new commit or tag>
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

This rebuilds both images and replaces the Runner's container. Runs already running carry on, on
the code they started with, and the new Runner adopts them; new runs use the new images. **Queued
runs are lost**, so upgrade when the backend has none waiting. Rolling back is the same with the
previous commit. The record format is not versioned: change it only in step with the backend.

---

## Stopping

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod down
```

This stops the Runner only. Running masters are independent containers: they finish their runs
and end their streams normally, and the next Runner to start finds whatever is still running.
While they run, `down` cannot remove the project's network (the masters are on it) and says so; it
goes with the next `down` once they have finished. To
stop the runs too, cancel each through the API first, which winds it down cleanly to `run_end`
(`aborted`). Removing their containers by force (`docker rm -f`) ends them without a `run_end`, and
the backend's `abort` then has to end each stream.

---

## Security

* **The Docker socket** gives the Runner, and every master, root on the host. Run nothing else on
  that host that you would not trust it with.
* **No authentication:** anyone who reaches the Runner's port can start and cancel runs. Bind it
  (`RUNNER_BIND`) to `127.0.0.1` or a private address; ufw does not filter ports Docker publishes.
* **The Redis password** is in `.env.prod` (gitignored) and in the environment of the Runner, the
  masters and the islands, readable with `docker inspect` by anyone with Docker access. It never
  reaches a record: `run_start` carries the settings with it redacted.
* **Islands run as an unprivileged user** (uid 1000) with no socket; the Runner and the masters run
  as root, to reach the socket.

---

## Before going live

- [ ] `.env.prod`: `REDIS_URL` with the backend's password; `RUNNER_BIND` a private address
- [ ] Redis: `noeviction`, ~1.5 GB free, the ACL commands above if it uses a restricted user
- [ ] From the Runner's host, Redis answers (`/health` is `ok`); from the backend's host, the
      Runner's port answers
- [ ] A quick run end to end with the dummy backend: `finished`, stream valid
- [ ] The backend has agreed the contract's open items (`cancelled`, the 503s, `run_end.error`, the
      bounds), reads `run:{run_id}:runner_logs`, and its clustering module no longer requires
      `schema_version` (the Runner's records carry none, so until then it refuses every run)
