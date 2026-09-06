#!/usr/bin/env python3
"""Download the real benchmark instances into ``benchmarks/data/``."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = REPOSITORY_ROOT / "benchmarks" / "data"
MANIFEST = DATA_ROOT / "MANIFEST.json"

TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Dataset:
    """One file to fetch."""

    group: str
    target: str
    url: str
    note: str


TSPLIB_MIRROR = "https://raw.githubusercontent.com/mastqe/tsplib/master/"
KPLIB_MIRROR = "https://raw.githubusercontent.com/likr/kplib/master/"
CEC2017_MIRROR = "https://raw.githubusercontent.com/lacerdamarcelo/cec17_python/master/input_data/"
UCI_MIRROR = "https://archive.ics.uci.edu/ml/machine-learning-databases/"

DATASETS: list[Dataset] = [
    Dataset("tsp", "tsp/eil51.tsp", TSPLIB_MIRROR + "eil51.tsp",
            "51-city Christofides/Eilon instance; known optimum 426"),
    Dataset("tsp", "tsp/berlin52.tsp", TSPLIB_MIRROR + "berlin52.tsp",
            "52 locations in Berlin (Groetschel); known optimum 7542"),
    Dataset("tsp", "tsp/att48.tsp", TSPLIB_MIRROR + "att48.tsp",
            "48 US state capitals (Padberg/Rinaldi), ATT pseudo-Euclidean; known optimum 10628"),
    Dataset("tsp", "tsp/kroA100.tsp", TSPLIB_MIRROR + "kroA100.tsp",
            "100-city Krolak/Felts/Nelson instance A; known optimum 21282"),

    Dataset("knapsack", "knapsack/uncorrelated_n100_R1000.kp",
            KPLIB_MIRROR + "00Uncorrelated/n00100/R01000/s000.kp",
            "Pisinger uncorrelated, n=100, R=1000 (easiest family)"),
    Dataset("knapsack", "knapsack/weakly_correlated_n100_R1000.kp",
            KPLIB_MIRROR + "01WeaklyCorrelated/n00100/R01000/s000.kp",
            "Pisinger weakly correlated, n=100, R=1000"),
    Dataset("knapsack", "knapsack/strongly_correlated_n100_R1000.kp",
            KPLIB_MIRROR + "02StronglyCorrelated/n00100/R01000/s000.kp",
            "Pisinger strongly correlated, n=100, R=1000 (hard: uniform value density)"),
    Dataset("knapsack", "knapsack/inverse_strongly_correlated_n100_R1000.kp",
            KPLIB_MIRROR + "03InverseStronglyCorrelated/n00100/R01000/s000.kp",
            "Pisinger inverse strongly correlated, n=100, R=1000"),
    Dataset("knapsack", "knapsack/uncorrelated_n200_R1000.kp",
            KPLIB_MIRROR + "00Uncorrelated/n00200/R01000/s000.kp",
            "Pisinger uncorrelated, n=200, R=1000"),
    Dataset("knapsack", "knapsack/strongly_correlated_n200_R1000.kp",
            KPLIB_MIRROR + "02StronglyCorrelated/n00200/R01000/s000.kp",
            "Pisinger strongly correlated, n=200, R=1000"),

    Dataset("cec", "cec/shift_data_1.txt", CEC2017_MIRROR + "shift_data_1.txt",
            "CEC2017 shift vector, function 1"),
    Dataset("cec", "cec/shift_data_3.txt", CEC2017_MIRROR + "shift_data_3.txt",
            "CEC2017 shift vector, function 3"),
    Dataset("regression", "regression/yacht_hydrodynamics.data",
            UCI_MIRROR + "00243/yacht_hydrodynamics.data",
            "UCI Yacht Hydrodynamics: 308 rows, 6 features, target = residuary resistance"),
    Dataset("regression", "regression/airfoil_self_noise.dat",
            UCI_MIRROR + "00291/airfoil_self_noise.dat",
            "UCI Airfoil Self-Noise: 1503 rows, 5 features, target = sound pressure level"),
]

GROUPS = sorted({dataset.group for dataset in DATASETS})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"files": {}}


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "archipelago-baseline-dea/0.2 (+research use)"},
    )
    temporary = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        temporary.write_bytes(response.read())
    temporary.replace(destination)


def fetch(groups: list[str] | None, force: bool, verify_only: bool) -> int:
    """Download one group of instances and verify their checksums."""
    socket.setdefaulttimeout(TIMEOUT_SECONDS)
    manifest = _load_manifest()
    files = manifest.setdefault("files", {})

    selected = [d for d in DATASETS if not groups or d.group in groups]
    failures: list[tuple[str, str]] = []
    fetched = skipped = verified = 0

    for dataset in selected:
        destination = DATA_ROOT / dataset.target
        recorded = files.get(dataset.target)

        if destination.exists() and not force:
            digest = _sha256(destination)
            if verify_only:
                if recorded and recorded.get("sha256") != digest:
                    failures.append((dataset.target, "checksum does not match the manifest"))
                else:
                    verified += 1
                continue
            if recorded and recorded.get("sha256") == digest:
                skipped += 1
                continue

        if verify_only:
            failures.append((dataset.target, "missing"))
            continue

        try:
            _download(dataset.url, destination)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            failures.append((dataset.target, f"{type(exc).__name__}: {exc}"))
            continue

        files[dataset.target] = {
            "group": dataset.group,
            "url": dataset.url,
            "note": dataset.note,
            "bytes": destination.stat().st_size,
            "sha256": _sha256(destination),
        }
        fetched += 1
        print(f"  fetched  {dataset.target}  ({destination.stat().st_size:,} bytes)")

    if not verify_only:
        manifest["schema"] = 1
        manifest["note"] = (
            "Provenance for the third-party benchmark instances under benchmarks/data/. "
            "Regenerate with scripts/fetch_datasets.py; verify with --verify."
        )
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if verify_only:
        print(f"verified {verified} file(s)")
    else:
        print(f"\nfetched {fetched}, already present {skipped}, failed {len(failures)}")
        print(f"manifest: {MANIFEST}")

    for target, reason in failures:
        print(f"  FAILED   {target}: {reason}", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    """Parse arguments and fetch or verify the requested groups."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        action="append",
        choices=GROUPS,
        help=f"Fetch only these groups. Repeatable. Available: {', '.join(GROUPS)}",
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if present and valid.")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Check existing files against the manifest and download nothing.",
    )
    args = parser.parse_args()
    return fetch(args.only, args.force, args.verify)


if __name__ == "__main__":
    sys.exit(main())
