"""Independent parser for upstream result literals, never the Grafx evaluator.

Implements the primitive/container/graph notation specified in tck/README.adoc.
Calls and executable expressions are not literals and cannot execute host code.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import math
import re


@dataclass(frozen=True)
class ReferenceNode:
    labels: frozenset[str]
    properties: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class ReferenceRelationship:
    type: str
    properties: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class ReferencePath:
    nodes: tuple[ReferenceNode, ...]
    relationships: tuple[ReferenceRelationship, ...]
    directions: tuple[str, ...]


_TOKEN = re.compile(
    r'''\s*(?:(?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")'''
    r"|(?P<quoted>`(?:``|[^`])*`)|(?P<number>[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z_0-9]*)|(?P<symbol><-|->|[()\[\]{}:,<>-]))"
)


class _Reader:
    def __init__(self, text: str):
        self.tokens = []
        offset = 0
        while offset < len(text):
            match = _TOKEN.match(text, offset)
            if match is None:
                if not text[offset:].strip():
                    break
                raise ValueError(f"Invalid reference literal at offset {offset}")
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
            offset = match.end()
        self.position = 0

    def peek(self):
        return self.tokens[self.position][1] if self.position < len(self.tokens) else None

    def take(self, expected=None):
        if self.position >= len(self.tokens):
            raise ValueError("Truncated reference literal")
        kind, value = self.tokens[self.position]
        if expected is not None and value != expected:
            raise ValueError(f"Expected {expected!r}, got {value!r}")
        self.position += 1
        return kind, value

    def name(self):
        kind, value = self.take()
        if kind == "quoted":
            return value[1:-1].replace("``", "`")
        if kind == "string":
            return ast.literal_eval(value)
        if kind == "name":
            return value
        raise ValueError("Expected reference key or label")

    def properties(self, depth):
        if self.peek() != "{":
            return ()
        return tuple(sorted(self.value(depth + 1).items()))

    def node(self, depth):
        self.take("(")
        labels = set()
        while self.peek() == ":":
            self.take(":")
            labels.add(self.name())
        properties = self.properties(depth)
        self.take(")")
        return ReferenceNode(frozenset(labels), properties)

    def relationship(self, depth):
        self.take("[")
        self.take(":")
        name = self.name()
        properties = self.properties(depth)
        self.take("]")
        return ReferenceRelationship(name, properties)

    def value(self, depth=0):
        if depth > 128:
            raise ValueError("Reference literal nesting exceeds 128")
        token = self.peek()
        if token == "(":
            return self.node(depth)
        if token == "<":
            self.take("<")
            nodes, relationships, directions = [self.node(depth)], [], []
            while self.peek() != ">":
                direction = self.take()[1]
                if direction not in {"-", "<-"}:
                    raise ValueError("Expected directed reference path")
                relationships.append(self.relationship(depth))
                self.take("->" if direction == "-" else "-")
                directions.append("outgoing" if direction == "-" else "incoming")
                nodes.append(self.node(depth))
            self.take(">")
            return ReferencePath(tuple(nodes), tuple(relationships), tuple(directions))
        if token == "[":
            if self.position + 1 < len(self.tokens) and self.tokens[self.position + 1][1] == ":":
                return self.relationship(depth)
            self.take("[")
            result = []
            while self.peek() != "]":
                result.append(self.value(depth + 1))
                if self.peek() != ",":
                    break
                self.take(",")
            self.take("]")
            return result
        if token == "{":
            self.take("{")
            result = {}
            while self.peek() != "}":
                key = self.name()
                if key in result:
                    raise ValueError("Duplicate reference map key")
                self.take(":")
                result[key] = self.value(depth + 1)
                if self.peek() != ",":
                    break
                self.take(",")
            self.take("}")
            return result
        kind, token = self.take()
        if kind == "string":
            return ast.literal_eval(token)
        if kind == "number":
            return float(token) if any(c in token for c in ".eE") else int(token)
        if token == "-" and self.peek() == "Inf":
            self.take("Inf")
            return -math.inf
        if token in {"null", "true", "false", "NaN", "Inf"}:
            return {"null": None, "true": True, "false": False, "NaN": math.nan, "Inf": math.inf}[token]
        raise ValueError(f"Unsupported reference literal: {token!r}")


def reference_value(text: str) -> object:
    """Decode one complete expected value; trailing syntax refuses admission."""
    reader = _Reader(text)
    value = reader.value()
    if reader.peek() is not None:
        raise ValueError("Trailing reference literal syntax")
    return value


def reference_key(value: object, *, unordered_lists=False) -> object:
    """Hashable independent oracle keys, preserving type and duplicate multiplicity."""
    from collections import Counter

    if isinstance(value, ReferenceNode):
        return ("node", value.labels, frozenset((key, reference_key(item, unordered_lists=unordered_lists))
                                                for key, item in value.properties))
    if isinstance(value, ReferenceRelationship):
        return ("relationship", value.type, frozenset((key, reference_key(item, unordered_lists=unordered_lists))
                                                     for key, item in value.properties))
    if isinstance(value, ReferencePath):
        return ("path", tuple(reference_key(n, unordered_lists=unordered_lists) for n in value.nodes),
                tuple(reference_key(r, unordered_lists=unordered_lists) for r in value.relationships), value.directions)
    if isinstance(value, (tuple, list)):
        items = [reference_key(item, unordered_lists=unordered_lists) for item in value]
        return ("list-bag", frozenset(Counter(items).items())) if unordered_lists else ("list", tuple(items))
    if isinstance(value, dict):
        return ("map", frozenset((key, reference_key(item, unordered_lists=unordered_lists)) for key, item in value.items()))
    if isinstance(value, float) and math.isnan(value):
        return ("float", "NaN")
    return (type(value).__name__, value)
