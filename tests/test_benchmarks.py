"""Benchmark correctness: known optima, file loaders, registry wiring."""

from __future__ import annotations

import numpy as np
import pytest

from benchmarks import (
    Ackley,
    Griewank,
    Rastrigin,
    Rosenbrock,
    Schwefel,
    ShiftedRotated,
    Sphere,
    available_benchmarks,
    benchmark_domain,
    create_problem,
)
from benchmarks.knapsack import Knapsack, load_knapsack_file
from benchmarks.registry import BenchmarkNotFound
from benchmarks.symbolic import SYMBOLIC_BENCHMARKS, SymbolicRegression
from benchmarks.tsp import TSP, load_tsplib_file


@pytest.mark.parametrize(
    ("cls", "optimum_value"),
    [
        (Sphere, 0.0),
        (Rastrigin, 0.0),
        (Ackley, 0.0),
        (Griewank, 0.0),
    ],
)
def test_functions_are_zero_at_the_origin(cls, optimum_value):
    problem = cls(dimension=10)
    assert problem.evaluate(np.zeros(10)).fitness == pytest.approx(optimum_value, abs=1e-12)


def test_rosenbrock_optimum_is_at_the_all_ones_vector():
    problem = Rosenbrock(dimension=10)
    assert problem.evaluate(np.ones(10)).fitness == pytest.approx(0.0, abs=1e-12)
    assert problem.evaluate(np.zeros(10)).fitness > 0.0


def test_schwefel_optimum_is_at_420_9687():
    problem = Schwefel(dimension=10)
    assert problem.evaluate(np.full(10, 420.9687)).fitness == pytest.approx(0.0, abs=1e-4)


def test_rosenbrock_requires_at_least_two_dimensions():
    with pytest.raises(ValueError, match="dimension >= 2"):
        Rosenbrock(dimension=1)


def test_random_solutions_are_inside_the_box(rng):
    problem = Griewank(dimension=5)
    for _ in range(50):
        x = problem.random_solution(rng)
        assert np.all(x >= problem.lower) and np.all(x <= problem.upper)


def test_a_shift_file_moves_the_optimum_off_the_origin(tmp_path):
    shift = tmp_path / "shift.txt"
    shift.write_text(" ".join(str(1.0 + index) for index in range(8)))

    problem = ShiftedRotated("rastrigin", dimension=8, shift_file=str(shift))
    assert np.allclose(problem.shift, np.arange(1.0, 9.0))
    assert problem.evaluate(problem.shift).fitness == pytest.approx(0.0, abs=1e-9)
    assert problem.evaluate(np.zeros(8)).fitness > 1e-6


def test_a_rotation_file_must_actually_be_a_rotation(tmp_path):
    """A non-orthogonal matrix would shear the space and silently change the problem."""
    rotation = tmp_path / "rotation.txt"
    rotation.write_text("\n".join("2 0\n0 2".split("\n")))
    with pytest.raises(ValueError, match="not an orthogonal matrix"):
        ShiftedRotated("sphere", dimension=2, rotation_file=str(rotation))


def test_without_data_files_the_problem_is_an_honest_no_op():
    """No generated stand-in: no shift file means no shift, and it says so."""
    problem = ShiftedRotated("ackley", dimension=6)
    assert np.allclose(problem.shift, 0.0)
    assert np.allclose(problem.rotation, np.eye(6))
    assert problem.describe()["shifted"] is False
    assert problem.describe()["rotated"] is False
    assert problem.evaluate(np.zeros(6)).fitness == pytest.approx(
        Ackley(dimension=6).evaluate(np.zeros(6)).fitness
    )


def test_a_missing_instance_file_is_an_error_not_a_generated_substitute():
    with pytest.raises(FileNotFoundError, match="does not generate substitutes"):
        ShiftedRotated("sphere", dimension=4, shift_file="benchmarks/data/cec/nope.txt")


@pytest.fixture
def knapsack_file(tmp_path):
    """A small, hand-written kplib instance: 5 items, capacity 100, total weight 200."""
    path = tmp_path / "small.kp"
    path.write_text("5\n100\n\n94 20\n50 30\n30 40\n40 50\n60 60\n")
    return path


def test_knapsack_requires_an_instance_file():
    """No generator: an unpinned instance would make two runs incomparable."""
    with pytest.raises(TypeError):
        Knapsack()


def test_knapsack_is_a_maximisation_with_a_penalty_that_preserves_the_optimum(knapsack_file):
    problem = Knapsack(instance_file=str(knapsack_file))
    assert problem.maximising

    empty = problem.evaluate(np.zeros(problem.dimension, dtype=np.int8))
    assert empty.fitness == 0.0 and empty.feasible

    everything = problem.evaluate(np.ones(problem.dimension, dtype=np.int8))
    assert not everything.feasible
    assert everything.constraint_violation > 0
    assert everything.fitness < everything.objective


def test_knapsack_reports_objective_separately_from_penalised_fitness(knapsack_file):
    problem = Knapsack(instance_file=str(knapsack_file))
    result = problem.evaluate(np.ones(problem.dimension, dtype=np.int8))
    assert result.objective == pytest.approx(float(np.sum(problem.instance.values)))
    assert result.fitness != result.objective


def test_a_pisinger_format_file_is_rejected_by_name(tmp_path):
    """Only kplib is supported; say which format the file looks like, not just 'malformed'."""
    path = tmp_path / "knapPI.txt"
    path.write_text(
        "knapPI_1_5_1000_1\nn 5\nc 100\nz 300\n1 10 20\n2 20 30\n3 30 40\n4 40 50\n5 50 60\n"
    )
    with pytest.raises(ValueError, match="Pisinger-format"):
        load_knapsack_file(path)


def test_knapsack_kplib_file_format(tmp_path):
    """The layout the Knapsack Problem Library ships in: blank-separated, one value per line."""
    path = tmp_path / "instance.kp"
    path.write_text("\n5\n100\n\n94 20\n50 30\n30 40\n40 50\n60 60\n")
    instance = load_knapsack_file(path)
    assert instance.n == 5 and instance.capacity == 100
    assert instance.values.tolist() == [94, 50, 30, 40, 60]
    assert instance.weights.tolist() == [20, 30, 40, 50, 60]


def test_knapsack_file_with_a_bad_header_is_rejected(tmp_path):
    path = tmp_path / "bad.kp"
    path.write_text("not-a-number\n100\n94 20\n")
    with pytest.raises(ValueError, match="item count and the capacity"):
        load_knapsack_file(path)


def test_knapsack_file_with_too_few_item_lines_is_rejected(tmp_path):
    path = tmp_path / "short.kp"
    path.write_text("5\n100\n94 20\n50 30\n")
    with pytest.raises(ValueError, match="item lines"):
        load_knapsack_file(path)


def test_tsp_tour_length_is_the_closed_tour(tmp_path):
    path = tmp_path / "square.tsp"
    path.write_text(
        "NAME : square\nTYPE : TSP\nDIMENSION : 4\nEDGE_WEIGHT_TYPE : EUC_2D\n"
        "NODE_COORD_SECTION\n1 0 0\n2 0 10\n3 10 10\n4 10 0\nEOF\n"
    )
    problem = TSP(instance_file=str(path))
    assert problem.evaluate([0, 1, 2, 3]).fitness == 40.0


def test_tsplib_euc_2d_rounds_to_the_nearest_integer(tmp_path):
    """TSPLIB's integer rounding is what makes published optima reproducible."""
    path = tmp_path / "d.tsp"
    path.write_text(
        "NAME : d\nTYPE : TSP\nDIMENSION : 2\nEDGE_WEIGHT_TYPE : EUC_2D\n"
        "NODE_COORD_SECTION\n1 0 0\n2 10 10\nEOF\n"
    )
    instance = load_tsplib_file(path)
    assert instance.distances[0, 1] == 14.0


def test_tsplib_explicit_lower_diag_row(tmp_path):
    path = tmp_path / "m.tsp"
    path.write_text(
        "NAME : m\nTYPE : TSP\nDIMENSION : 3\nEDGE_WEIGHT_TYPE : EXPLICIT\n"
        "EDGE_WEIGHT_FORMAT : LOWER_DIAG_ROW\nEDGE_WEIGHT_SECTION\n0\n1 0\n2 3 0\nEOF\n"
    )
    instance = load_tsplib_file(path)
    assert instance.distances.tolist() == [[0, 1, 2], [1, 0, 3], [2, 3, 0]]


def test_tsp_is_symmetric_under_tour_reversal(rng, tmp_path):
    path = tmp_path / "pentagon.tsp"
    path.write_text(
        "NAME : pentagon\nTYPE : TSP\nDIMENSION : 5\nEDGE_WEIGHT_TYPE : EUC_2D\n"
        "NODE_COORD_SECTION\n1 0 0\n2 10 3\n3 7 12\n4 2 14\n5 -4 6\nEOF\n"
    )
    problem = TSP(instance_file=str(path))
    tour = problem.random_solution(rng)
    assert problem.evaluate(tour).fitness == pytest.approx(problem.evaluate(tour[::-1]).fitness)


def test_tsp_requires_an_instance_file():
    """No generator: a run must be checkable against a published optimum."""
    with pytest.raises(TypeError):
        TSP()


def test_a_relative_instance_path_resolves_against_the_repository_not_the_cwd(monkeypatch, tmp_path):
    """Islands run with a different cwd from the master; both must find the same file."""
    monkeypatch.chdir(tmp_path)
    problem = TSP(instance_file="benchmarks/data/tsp/berlin52.tsp")
    assert problem.dimension == 52


def test_tsp_rejects_an_unsupported_edge_weight_type(tmp_path):
    path = tmp_path / "bad.tsp"
    path.write_text(
        "NAME : b\nTYPE : TSP\nDIMENSION : 2\nEDGE_WEIGHT_TYPE : XRAY1\n"
        "NODE_COORD_SECTION\n1 0 0\n2 1 1\nEOF\n"
    )
    with pytest.raises(ValueError, match="unsupported EDGE_WEIGHT_TYPE"):
        load_tsplib_file(path)


@pytest.mark.parametrize("name", sorted(SYMBOLIC_BENCHMARKS))
def test_the_target_function_scores_zero_error(name):
    """If the perfect model does not score zero, the target and the data disagree."""
    problem = SymbolicRegression(benchmark=name, instance_seed=4)
    target = SYMBOLIC_BENCHMARKS[name]["target"]
    assert problem.evaluate(lambda x: target(x)).fitness == pytest.approx(0.0, abs=1e-9)


def test_non_finite_predictions_score_a_large_finite_value():
    """An inf would make the island's mean fitness inf and break selection arithmetic."""
    problem = SymbolicRegression(benchmark="nguyen7")
    result = problem.evaluate(lambda x: np.full(x.shape[0], np.inf))
    assert np.isfinite(result.fitness) and result.fitness > 1e9
    assert result.feasible is False


def test_a_raising_predictor_is_scored_not_propagated():
    problem = SymbolicRegression(benchmark="nguyen7")
    assert np.isfinite(problem.evaluate(lambda x: 1 / 0).fitness)


def test_a_constant_predictor_is_broadcast():
    """Many initial random GP trees are constants; a scalar must not be an error."""
    problem = SymbolicRegression(benchmark="nguyen7")
    assert np.isfinite(problem.evaluate(lambda x: 0.0).fitness)


def test_keijzer6_test_range_extends_beyond_training():
    """This benchmark exists to measure extrapolation."""
    problem = SymbolicRegression(benchmark="keijzer6")
    assert problem.x.max() == 50
    assert problem.test_x is not None and problem.test_x.max() == 120


def test_csv_loading_with_a_header(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("a,b,y\n1,2,3\n4,5,9\n")
    problem = SymbolicRegression(benchmark="csv", csv_path=str(path), csv_target_column="y")
    assert problem.variable_names == ["a", "b"]
    assert problem.y.tolist() == [3.0, 9.0]


@pytest.mark.parametrize(
    ("content", "expected_columns"),
    [
        ("1\t2\t3\r\n4\t5\t9\r\n", 2),
        ("1 2 3\n4 5 9\n", 2),
        ("1,2,3\n4,5,9\n", 2),
    ],
)
def test_regression_loader_sniffs_the_real_file_shapes(tmp_path, content, expected_columns):
    """The three layouts the downloaded datasets actually ship in."""
    from benchmarks.symbolic import load_regression_table

    path = tmp_path / "data.txt"
    path.write_text(content)
    x, y, names = load_regression_table(path)
    assert x.shape == (2, expected_columns)
    assert y.tolist() == [3.0, 9.0]
    assert names == ["x0", "x1"]


def test_regression_loader_rejects_ragged_rows(tmp_path):
    from benchmarks.symbolic import load_regression_table

    path = tmp_path / "ragged.csv"
    path.write_text("1,2,3\n4,5\n")
    with pytest.raises(ValueError, match="inconsistent widths"):
        load_regression_table(path)


def test_regression_loader_reports_the_offending_line(tmp_path):
    from benchmarks.symbolic import load_regression_table

    path = tmp_path / "bad.csv"
    path.write_text("1,2,3\n4,oops,9\n")
    with pytest.raises(ValueError, match="non-numeric"):
        load_regression_table(path)


REQUIRED_INSTANCES = {
    "knapsack": {"instance_file": "benchmarks/data/knapsack/uncorrelated_n100_R1000.kp"},
    "tsp": {"instance_file": "benchmarks/data/tsp/berlin52.tsp"},
}


def test_every_registered_benchmark_instantiates():
    for name in available_benchmarks():
        problem = create_problem(name, REQUIRED_INSTANCES.get(name, {}))
        assert problem.domain is benchmark_domain(name)


def test_unknown_benchmark_names_the_alternatives():
    with pytest.raises(BenchmarkNotFound, match="available"):
        create_problem("not_a_benchmark")


def test_a_typo_in_benchmark_params_is_an_error_not_a_silent_default():
    """A silently ignored parameter runs an entire sweep against the wrong instance."""
    with pytest.raises(TypeError, match="invalid parameters"):
        create_problem("sphere", {"dimensionn": 10})


def test_describe_is_json_serialisable_and_elides_large_arrays():
    import json

    problem = ShiftedRotated("sphere", dimension=30)
    payload = problem.describe()
    json.dumps(payload)
    assert payload["shifted"] is False and payload["rotated"] is False
