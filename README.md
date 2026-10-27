# Archipelago — Baseline dEA Integration

## Quick start

Everything runs in Docker by default: the master in one container, each island in
its own, every record appended to a Redis stream. All you need is a Docker daemon —
the benchmark instances are committed to the repository, so nothing is downloaded.

```bash
docker build --network=host -f docker/Dockerfile.master -t archipelago-master:latest .
docker network create archipelago
docker run -d --name redis --network archipelago redis:7-alpine --maxmemory-policy noeviction

# The reference dataset run: 100,000 evaluations on a real TSPLIB instance
docker run --rm --network archipelago \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/runs:/archipelago/runs" \
  -e HOST_RUNS_DIR="$PWD/runs" \
  -e ARCHIPELAGO_BROKER__URL=redis://redis:6379/0 \
  archipelago-master:latest run --config config/full_dataset.yaml
```

The records land in `run:<run id>:runner_logs`.