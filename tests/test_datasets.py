"""The real benchmark instances committed under ``benchmarks/data/``."""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from benchmarks.knapsack import load_knapsack_file
from benchmarks.registry import create_problem
from benchmarks.symbolic import REAL_REGRESSION_DATASETS
from benchmarks.tsp import load_tsplib_file


def _data_root(repository_root):
    return repository_root / "benchmarks" / "data"


def _manifest(repository_root) -> dict:
    path = _data_root(repository_root) / "MANIFEST.json"
    if not path.exists():
        pytest.fail("benchmarks/data/MANIFEST.json is missing; restore it from git")
    return json.loads(path.read_text())


def _require(repository_root, relative: str):
    path = _data_root(repository_root) / relative
    if not path.exists():
        pytest.fail(f"{relative} is missing from benchmarks/data/; restore it from git")
    return path


def test_manifest_checksums_match_the_files_on_disk(repository_root):
    """The dataset identity check. A silent change here would invalidate every run."""
    manifest = _manifest(repository_root)
    assert manifest["files"], "manifest is empty"

    for relative, entry in manifest["files"].items():
        path = _data_root(repository_root) / relative
        assert path.exists(), f"{relative} is in the manifest but missing on disk"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == entry["sha256"], f"{relative} does not match its recorded checksum"
        assert path.stat().st_size == entry["bytes"]


def test_manifest_records_provenance_for_every_file(repository_root):
    """A dataset without a source URL cannot be re-obtained or cited."""
    for relative, entry in _manifest(repository_root)["files"].items():
        assert entry["url"].startswith("http"), relative
        assert entry["note"], relative
        assert entry["group"] in {"tsp", "knapsack", "cec", "regression"}, relative


@pytest.mark.parametrize(
    ("name", "cities", "edge_weight_type"),
    [
        ("eil51.tsp", 51, "EUC_2D"),
        ("berlin52.tsp", 52, "EUC_2D"),
        ("att48.tsp", 48, "ATT"),
        ("kroA100.tsp", 100, "EUC_2D"),
    ],
)
def test_real_tsplib_instances_parse(repository_root, name, cities, edge_weight_type):
    instance = load_tsplib_file(_require(repository_root, f"tsp/{name}"))
    assert instance.n == cities
    assert instance.edge_weight_type == edge_weight_type
    assert instance.distances.shape == (cities, cities)
    assert np.allclose(instance.distances, instance.distances.T), "symmetric TSP"
    assert np.all(np.diag(instance.distances) == 0)
    assert np.all(instance.distances >= 0)


def test_a_real_instance_produces_tour_lengths_near_the_known_optimum(repository_root):
    """berlin52's optimum is 7542; a random tour must be worse, and finitely so."""
    problem = create_problem(
        "tsp", {"instance_file": str(_require(repository_root, "tsp/berlin52.tsp"))}
    )
    rng = np.random.default_rng(0)
    lengths = [problem.evaluate(problem.random_solution(rng)).fitness for _ in range(20)]
    assert all(length > 7542 for length in lengths)
    assert min(lengths) < 100_000


def test_att48_uses_the_pseudo_euclidean_rule(repository_root):
    """ATT is not Euclidean; parsing it as EUC_2D would give plausible wrong numbers."""
    instance = load_tsplib_file(_require(repository_root, "tsp/att48.tsp"))
    assert instance.coordinates is not None
    a, b = instance.coordinates[0], instance.coordinates[1]
    euclidean = float(np.hypot(*(a - b)))
    assert instance.distances[0, 1] < euclidean, "ATT distances are scaled down by sqrt(10)"


@pytest.mark.parametrize(
    ("name", "items"),
    [
        ("uncorrelated_n100_R1000.kp", 100),
        ("weakly_correlated_n100_R1000.kp", 100),
        ("strongly_correlated_n100_R1000.kp", 100),
        ("inverse_strongly_correlated_n100_R1000.kp", 100),
        ("uncorrelated_n200_R1000.kp", 200),
        ("strongly_correlated_n200_R1000.kp", 200),
    ],
)
def test_real_knapsack_instances_parse(repository_root, name, items):
    instance = load_knapsack_file(_require(repository_root, f"knapsack/{name}"))
    assert instance.n == items
    assert np.all(instance.weights > 0)
    assert np.all(instance.values > 0)
    ratio = instance.capacity / instance.weights.sum()
    assert 0.3 < ratio < 0.7


def test_the_strongly_correlated_family_really_is_correlated(repository_root):
    """If the parse silently swapped value and weight, this is what would catch it."""
    strong = load_knapsack_file(
        _require(repository_root, "knapsack/strongly_correlated_n100_R1000.kp")
    )
    uncorrelated = load_knapsack_file(
        _require(repository_root, "knapsack/uncorrelated_n100_R1000.kp")
    )
    strong_correlation = float(np.corrcoef(strong.values, strong.weights)[0, 1])
    weak_correlation = float(np.corrcoef(uncorrelated.values, uncorrelated.weights)[0, 1])
    assert strong_correlation > 0.99
    assert abs(weak_correlation) < 0.3


def test_a_real_knapsack_instance_drives_a_problem(repository_root):
    path = _require(repository_root, "knapsack/strongly_correlated_n100_R1000.kp")
    problem = create_problem("knapsack", {"instance_file": str(path)})
    assert problem.maximising
    assert problem.dimension == 100

    empty = problem.evaluate(np.zeros(100, dtype=np.int8))
    assert empty.fitness == 0.0 and empty.feasible

    everything = problem.evaluate(np.ones(100, dtype=np.int8))
    assert not everything.feasible
    assert everything.objective > everything.fitness


@pytest.mark.parametrize("name", sorted(REAL_REGRESSION_DATASETS))
def test_real_regression_datasets_load(repository_root, name):
    spec = REAL_REGRESSION_DATASETS[name]
    _require(repository_root, spec["path"].split("benchmarks/data/", 1)[1])

    problem = create_problem(name, {})
    assert problem.x.ndim == 2
    assert problem.x.shape[0] == problem.y.shape[0] > 100
    assert problem.x.shape[1] == len(problem.variable_names)
    assert np.all(np.isfinite(problem.x)) and np.all(np.isfinite(problem.y))
    assert not problem.maximising


def test_regression_variable_names_are_meaningful_not_positional(repository_root):
    """A GP expression naming `froude_number` is readable; one naming `x5` is not."""
    _require(repository_root, "regression/yacht_hydrodynamics.data")
    problem = create_problem("yacht", {})
    assert "froude_number" in problem.variable_names
    assert not any(name.startswith("x") and name[1:].isdigit() for name in problem.variable_names)


def test_a_constant_predictor_scores_the_target_standard_deviation(repository_root):
    """Sanity check on the RMSE wiring against a value computable by hand."""
    _require(repository_root, "regression/yacht_hydrodynamics.data")
    problem = create_problem("yacht", {"metric": "rmse"})
    mean = float(problem.y.mean())
    result = problem.evaluate(lambda x: np.full(x.shape[0], mean))
    assert result.fitness == pytest.approx(float(problem.y.std()), rel=1e-9)


def test_the_two_regression_sets_differ_in_cost(repository_root):
    """Row count drives GP evaluation cost, which is why the config picks yacht."""
    for name in ("yacht", "airfoil"):
        _require(repository_root, REAL_REGRESSION_DATASETS[name]["path"].split("benchmarks/data/", 1)[1])
    assert create_problem("airfoil", {}).x.shape[0] > 4 * create_problem("yacht", {}).x.shape[0]


def test_official_cec_shift_data_is_usable(repository_root):
    """The official shift vectors are genuine and switch the problem to the CEC domain."""
    shift = _require(repository_root, "cec/shift_data_3.txt")
    problem = create_problem(
        "shifted_rotated",
        {"function": "rastrigin", "dimension": 10, "shift_file": str(shift)},
    )
    assert problem.lower[0] == -100.0 and problem.upper[0] == 100.0
    assert problem.evaluate(problem.shift).fitness == pytest.approx(0.0, abs=1e-8)
    assert problem.evaluate(np.zeros(10)).fitness > 1.0, "the optimum is no longer at the origin"


def test_a_shift_file_for_the_wrong_domain_is_rejected(repository_root):
    shift = _require(repository_root, "cec/shift_data_3.txt")
    with pytest.raises(ValueError, match="outside the domain"):
        create_problem(
            "shifted_rotated",
            {
                "function": "rastrigin",
                "dimension": 10,
                "shift_file": str(shift),
                "lower": -5.12,
                "upper": 5.12,
            },
        )


def test_a_rotation_file_that_is_not_orthogonal_is_rejected(tmp_path):
    """Verified rather than trusted: a non-rotation silently changes the problem."""
    path = tmp_path / "not_a_rotation.txt"
    matrix = np.eye(4) * 2.0
    np.savetxt(path, matrix)
    with pytest.raises(ValueError, match="not an orthogonal matrix"):
        create_problem(
            "shifted_rotated",
            {"function": "sphere", "dimension": 4, "rotation_file": str(path)},
        )


def test_a_genuine_rotation_file_is_accepted(tmp_path):
    path = tmp_path / "rotation.txt"
    rng = np.random.default_rng(0)
    q, r = np.linalg.qr(rng.standard_normal((6, 6)))
    np.savetxt(path, q * np.sign(np.diag(r)))
    problem = create_problem(
        "shifted_rotated",
        {"function": "sphere", "dimension": 6, "rotation_file": str(path)},
    )
    assert np.allclose(problem.rotation @ problem.rotation.T, np.eye(6), atol=1e-9)


def test_the_official_shift_is_used_and_nothing_stands_in_for_it(repository_root):
    """With the file, the CEC shift; without it, no shift at all -- never a stand-in."""
    shift = _require(repository_root, "cec/shift_data_3.txt")
    official = create_problem(
        "shifted_rotated",
        {"function": "rastrigin", "dimension": 10, "shift_file": str(shift)},
    )
    expected = np.loadtxt(shift).ravel()[:10]
    assert np.allclose(official.shift, expected)
    assert official.describe()["shifted"] is True

    unshifted = create_problem("shifted_rotated", {"function": "rastrigin", "dimension": 10})
    assert np.allclose(unshifted.shift, 0.0)
    assert unshifted.describe()["shifted"] is False
