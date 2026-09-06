"""The five island algorithms, spanning five distinct genome structures."""

from .base import Algorithm, ESIndividual, Individual, IslandContext, Particle
from .de import DifferentialEvolution
from .es import EvolutionStrategy
from .ga import GeneticAlgorithm
from .gp import GeneticProgramming, TreeIndividual
from .pso import ParticleSwarmOptimization
from .registry import (
    AlgorithmNotFound,
    IncompatibleAlgorithm,
    accepted_parameters,
    algorithm_class,
    available_algorithms,
    check_compatible,
    create_algorithm,
    register,
)

__all__ = [
    "Algorithm",
    "Individual",
    "Particle",
    "ESIndividual",
    "TreeIndividual",
    "IslandContext",
    "GeneticAlgorithm",
    "DifferentialEvolution",
    "ParticleSwarmOptimization",
    "GeneticProgramming",
    "EvolutionStrategy",
    "create_algorithm",
    "available_algorithms",
    "algorithm_class",
    "accepted_parameters",
    "check_compatible",
    "register",
    "AlgorithmNotFound",
    "IncompatibleAlgorithm",
]
