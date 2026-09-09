"""Bounded corpus scope indexing preserves the original nested-parameter rule."""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import pulse_query_corpus as freezer  # noqa: E402


def canonical(tree, target):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= getattr(target, "lineno", -1) <= node.end_lineno:
            args = node.args
            names.update(arg.arg for group in (args.posonlyargs, args.args, args.kwonlyargs) for arg in group)
            if args.vararg:
                names.add(args.vararg.arg)
            if args.kwarg:
                names.add(args.kwarg.arg)
    return names


def test_indexed_scope_names_equal_full_walk_for_every_ast_node(monkeypatch):
    tree = ast.parse('''
def outer(a, /, b=1, *rest, flag=True, **kw):
    async def inner(c, *, option=None):
        return call(a, c, option)
    class Nested:
        def method(self, d):
            return call(d, b)
    return call(a)
call(1)
''')
    nodes = tuple(ast.walk(tree))
    expected = [canonical(tree, node) for node in nodes]
    freezer._reset_caches()
    calls = []
    original = ast.walk

    def counted(root):
        calls.append(root)
        return original(root)

    monkeypatch.setattr(ast, "walk", counted)
    assert [freezer._parameter_names(tree, node) for node in nodes] == expected
    assert calls == [tree]
    freezer._reset_caches()
    assert [freezer._parameter_names(tree, node) for node in nodes] == expected
    assert calls == [tree, tree]


def test_scope_inventory_has_bounded_retention_and_reset():
    freezer._reset_caches()
    for number in range(100):
        tree = ast.parse(f"def f{number}(a):\n    return a")
        freezer._parameter_names(tree, tree.body[0])
    assert len(freezer._FUNCTION_SCOPE_CACHE) <= 64
    freezer._reset_caches()
    assert not freezer._FUNCTION_SCOPE_CACHE
