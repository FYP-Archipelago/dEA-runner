"""Benchmark problems, config-selectable by name."""

from .base import (
    DATA_ROOT,
    BinaryProblem,
    ContinuousProblem,
    Domain,
    PermutationProblem,
    Problem,
    ProblemEvaluation,
    RegressionProblem,
    resolve_instance_path,
)
from .continuous import (
    CONTINUOUS_FUNCTIONS,
    Ackley,
    Griewank,
    Rastrigin,
    Rosenbrock,
    ShiftedRotated,
    Schwefel,
    Sphere,
)
from .knapsack import Knapsack, KnapsackInstance, load_knapsack_file
from .registry import (
    BenchmarkNotFound,
    available_benchmarks,
    benchmark_domain,
    create_problem,
    register,
)
from .symbolic import SYMBOLIC_BENCHMARKS, SymbolicRegression, load_regression_csv
from .tsp import TSP, TSPInstance, load_tsplib_file

__all__ = [
    "Domain",
    "DATA_ROOT",
    "resolve_instance_path",
    "Problem",
    "ProblemEvaluation",
    "ContinuousProblem",
    "BinaryProblem",
    "PermutationProblem",
    "RegressionProblem",
    "Sphere",
    "Rastrigin",
    "Ackley",
    "Rosenbrock",
    "Griewank",
    "Schwefel",
    "ShiftedRotated",
    "CONTINUOUS_FUNCTIONS",
    "Knapsack",
    "KnapsackInstance",
    "load_knapsack_file",
    "TSP",
    "TSPInstance",
    "load_tsplib_file",
    "SymbolicRegression",
    "SYMBOLIC_BENCHMARKS",
    "load_regression_csv",
    "create_problem",
    "available_benchmarks",
    "benchmark_domain",
    "register",
    "BenchmarkNotFound",
]
