"""Expression trees for Genetic Programming: primitives, evaluation, parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

import numpy as np

__all__ = [
    "Primitive",
    "Node",
    "FUNCTION_SETS",
    "PRIMITIVES",
    "canonical",
    "parse",
    "evaluate",
    "iter_nodes",
    "replace_at",
    "depth",
    "size",
]

CONSTANT_PRECISION = 6


@dataclass(frozen=True)
class Primitive:
    """A function-set member: a name, an arity, and a vectorised implementation."""

    name: str
    arity: int
    call: Callable[..., np.ndarray]
    infix: str | None = None


def _protected_division(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Koza's protected division: ``a / b``, or 1.0 where ``|b|`` is negligible."""
    return np.where(np.abs(b) < 1e-12, 1.0, a / np.where(np.abs(b) < 1e-12, 1.0, b))


def _protected_log(a: np.ndarray) -> np.ndarray:
    """``log(|a|)``, and 0.0 at zero."""
    magnitude = np.abs(a)
    return np.where(magnitude < 1e-12, 0.0, np.log(np.where(magnitude < 1e-12, 1.0, magnitude)))


def _protected_sqrt(a: np.ndarray) -> np.ndarray:
    """``sqrt(|a|)``."""
    return np.sqrt(np.abs(a))


def _protected_exp(a: np.ndarray) -> np.ndarray:
    """``exp(a)`` with the argument clipped to avoid overflow to ``inf``."""
    return np.exp(np.clip(a, -60.0, 60.0))


PRIMITIVES: dict[str, Primitive] = {
    "add": Primitive("add", 2, lambda a, b: a + b, "+"),
    "sub": Primitive("sub", 2, lambda a, b: a - b, "-"),
    "mul": Primitive("mul", 2, lambda a, b: a * b, "*"),
    "div": Primitive("div", 2, _protected_division, "/"),
    "neg": Primitive("neg", 1, lambda a: -a),
    "sin": Primitive("sin", 1, np.sin),
    "cos": Primitive("cos", 1, np.cos),
    "exp": Primitive("exp", 1, _protected_exp),
    "log": Primitive("log", 1, _protected_log),
    "sqrt": Primitive("sqrt", 1, _protected_sqrt),
    "square": Primitive("square", 1, lambda a: a * a),
    "abs": Primitive("abs", 1, np.abs),
    "min": Primitive("min", 2, np.minimum),
    "max": Primitive("max", 2, np.maximum),
}

FUNCTION_SETS: dict[str, tuple[str, ...]] = {
    "arithmetic": ("add", "sub", "mul", "div"),
    "koza": ("add", "sub", "mul", "div", "sin", "cos", "exp", "log"),
    "extended": tuple(PRIMITIVES),
}


@dataclass(frozen=True)
class Node:
    """One node of an expression tree."""

    symbol: str
    children: tuple["Node", ...] = ()
    value: float | None = None

    @property
    def is_terminal(self) -> bool:
        """True for a leaf: a variable or a constant."""
        return not self.children

    @property
    def is_constant(self) -> bool:
        """True for a constant leaf."""
        return self.symbol == "const"

    def __call__(self, x: np.ndarray, variables: Sequence[str]) -> np.ndarray:
        """Evaluate over a design matrix of shape ``(n_samples, n_variables)``."""
        return evaluate(self, x, variables)


def evaluate(node: Node, x: np.ndarray, variables: Sequence[str]) -> np.ndarray:
    """Vectorised recursive evaluation of a tree over all samples at once."""
    rows = x.shape[0]
    if node.is_constant:
        return np.full(rows, float(node.value))
    if node.is_terminal:
        try:
            column = variables.index(node.symbol)
        except ValueError as exc:
            raise ValueError(f"unknown variable {node.symbol!r}; known: {list(variables)}") from exc
        return x[:, column]

    primitive = PRIMITIVES[node.symbol]
    arguments = [evaluate(child, x, variables) for child in node.children]
    return primitive.call(*arguments)


def canonical(node: Node) -> str:
    """Parenthesised prefix rendering; the tree's location signature."""
    if node.is_constant:
        return f"{float(node.value):.{CONSTANT_PRECISION}g}"
    if node.is_terminal:
        return node.symbol
    return f"{node.symbol}({','.join(canonical(child) for child in node.children)})"


def parse(text: str) -> Node:
    """Inverse of :func:`canonical`. Used to rebuild a migrated tree."""
    position = 0

    def parse_node() -> Node:
        """Consume one node and its arguments from the token stream."""
        nonlocal position
        start = position
        while position < len(text) and text[position] not in "(),":
            position += 1
        token = text[start:position].strip()
        if not token:
            raise ValueError(f"empty token at offset {start} in {text!r}")

        if position < len(text) and text[position] == "(":
            if token not in PRIMITIVES:
                raise ValueError(f"unknown primitive {token!r} in {text!r}")
            position += 1
            children: list[Node] = [parse_node()]
            while position < len(text) and text[position] == ",":
                position += 1
                children.append(parse_node())
            if position >= len(text) or text[position] != ")":
                raise ValueError(f"unbalanced parentheses in {text!r}")
            position += 1
            expected = PRIMITIVES[token].arity
            if len(children) != expected:
                raise ValueError(
                    f"primitive {token!r} takes {expected} arguments, got {len(children)} in {text!r}"
                )
            return Node(token, tuple(children))

        try:
            return Node("const", (), float(token))
        except ValueError:
            return Node(token)

    root = parse_node()
    if position != len(text):
        raise ValueError(f"trailing characters at offset {position} in {text!r}")
    return root


def iter_nodes(node: Node) -> Iterator[Node]:
    """Preorder traversal. Index *i* of this sequence addresses a subtree."""
    yield node
    for child in node.children:
        yield from iter_nodes(child)


def size(node: Node) -> int:
    """Number of nodes."""
    return 1 + sum(size(child) for child in node.children)


def depth(node: Node) -> int:
    """Depth in edges: a lone terminal has depth 0."""
    return 0 if node.is_terminal else 1 + max(depth(child) for child in node.children)


def replace_at(node: Node, index: int, replacement: Node) -> Node:
    """Return a copy of ``node`` with the preorder-``index`` subtree replaced."""
    counter = 0

    def rebuild(current: Node) -> Node:
        """Copy the tree, swapping in the replacement at the target index."""
        nonlocal counter
        here = counter
        counter += 1
        if here == index:
            counter += size(current) - 1
            return replacement
        if current.is_terminal:
            return current
        return Node(current.symbol, tuple(rebuild(child) for child in current.children), current.value)

    return rebuild(node)
