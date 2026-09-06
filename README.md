# Archipelago — Baseline dEA Integration

## Quick start

Everything runs in Docker by default: the master in one container, each island in
its own. All you need is a Docker daemon — the benchmark instances are committed
to the repository, so nothing is downloaded.

```bash
docker build --network=host -f docker/Dockerfile.master -t archipelago-master:latest .

# The reference dataset run: 100,000 evaluations on a real TSPLIB instance
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/runs:/archipelago/runs" \
  -e HOST_RUNS_DIR="$PWD/runs" \
  archipelago-master:latest run --config config/full_dataset.yaml
```