"""Static properties every module of the package must keep (CONTRACT.md section 11, A6, A24).

Item 3 of the definition of done asks for type annotations on every public symbol and postponed
annotation evaluation everywhere; item 8 forbids leftover stubs; A6 asks for ``__all__``; A24
gives the shared page bounds exactly one definition. All four are cheap to state as a gate over
the source tree, and stating them here keeps them true for every later wave.

Every rule in this file carries an anti-vacuity suite: synthetic modules containing the spelling
the rule is meant to catch, asserting the detector flags each one, plus the legitimate spellings
asserting it stays quiet. A gate without a "this checker can fail" test is a gate on trust, and
two gates in this project have already been found blind to a spelling nobody wrote down.

Declared limits of the A24 rule. It reads bindings that are visible in the syntax tree:
assignment, annotated assignment, augmented assignment, destructuring, the walrus, ``for`` /
``with`` / ``except`` targets, comprehension targets, ``def`` / ``async def`` / ``class``, a PEP
695 ``type`` alias, ``globals()[...]`` and ``vars()[...]`` stores, an attribute store such as
``sys.modules[__name__].MIN_PAGE_SIZE = ...``, ``setattr`` with a literal name,
``vars(module).update(...)``, and -- the form an audit found the first version blind to -- an
**import alias**: ``from settings import floor as MIN_PAGE_SIZE`` binds the name as surely as an
assignment does, as does importing an A24 name from anywhere but its owning module.

What it cannot see is a name produced at runtime: a PEP 562 module ``__getattr__``, a value
passed through ``exec`` or ``importlib``, or a ``setattr`` whose name is computed. Those are not
spellings an honest builder writes for a constant, and the import-boundary gate already refuses
the machinery most of them need inside the pure core -- but the rule is a floor, not a proof,
and saying so here is better than leaving the claim unqualified.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
PACKAGE_ROOT: Path = PROJECT_ROOT / "src" / "okto_grafx"
SOURCE_FILES: list[Path] = sorted(PACKAGE_ROOT.rglob("*.py"))

UNFINISHED_MARKERS: tuple[tuple[str, str], ...] = (
    ("TODO", r"\bTODO\b|(?i:\btodo\s*:)"),
    ("FIXME", r"(?i:\bfixme\b)"),
    ("XXX", r"(?i:\bxxx\b)"),
    ("HACK", r"\bHACK\b|(?i:\bhack\s*:)"),
    ("pass # stub", r"(?i:\bpass\b[ \t]*#[ \t]*stub)"),
)
"""Markers that say the code is unfinished, matched case-insensitively on word boundaries.

CONTRACT.md section 11 item 8 quotes ``pass  # stub`` verbatim, so it is matched as written and
with any amount of whitespace around the comment. Word boundaries keep ``todos`` and ``hacking``
from tripping the rule while ``# ToDo`` and ``# fixme`` do.

Raising NotImplemented or NotImplementedError is caught separately, by shape rather than by
text, because ``return NotImplemented`` is the correct answer of a rich comparison that does not
apply to the other operand -- the storage core uses it properly and must not be punished for it.
"""

UNIMPLEMENTED_EXCEPTIONS: frozenset[str] = frozenset({"NotImplemented", "NotImplementedError"})
"""Raising either of these is an unimplemented body, whatever the docstring above it says."""

STUB_EXEMPT_DECORATORS: frozenset[str] = frozenset(
    {"overload", "abstractmethod", "abstractproperty"}
)
"""Decorators whose body is a declaration, so an ellipsis body is the correct spelling."""

DECLARATION_BASES: frozenset[str] = frozenset({"Protocol", "ABC"})
"""Base classes whose methods are declarations rather than implementations."""

SINGLE_DEFINITION_SYMBOLS: frozenset[str] = frozenset(
    {"MIN_PAGE_SIZE", "MAX_PAGE_SIZE", "validate_page_size"}
)
"""Symbols amendment A24 gives exactly one definition, owned by C1 in domain/page."""

_TYPE_ALIAS = getattr(ast, "TypeAlias", None)
"""PEP 695 type aliases exist from 3.12; on 3.11 the node is simply absent (A38)."""

SINGLE_DEFINITION_OWNER: Path = PACKAGE_ROOT / "domain" / "page"
"""The one package allowed to define them. Everyone else imports."""

OWNER_MODULES: frozenset[str] = frozenset(
    {"okto_grafx.domain.page", "okto_grafx.domain.page.layout"}
)
"""The module paths an A24 symbol may legitimately be imported from."""


def _relative(path: Path) -> str:
    """Return a project-relative label, or the plain path for a synthetic probe module."""
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _is_public(name: str) -> bool:
    """Return True for a name that belongs to the public surface, dunders included."""
    if name.startswith("__") and name.endswith("__"):
        return True
    return not name.startswith("_")


def _dotted(node: ast.expr) -> str:
    """Return the dotted spelling of a name or attribute chain, or an empty string."""
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
        return ".".join(reversed(parts))
    return ""


# --- annotations and postponed evaluation -----------------------------------------------------


def test_the_gate_sees_the_whole_package() -> None:
    assert len(SOURCE_FILES) >= 15


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_every_public_function_is_fully_annotated(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_public(node.name):
            continue
        location = f"{_relative(path)}:{node.lineno} {node.name}"
        arguments = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            if argument.arg in {"self", "cls"}:
                continue
            assert argument.annotation is not None, f"{location} parameter {argument.arg}"
        assert node.returns is not None, f"{location} return value"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_every_module_postpones_annotation_evaluation(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert "from __future__ import annotations" in source


# --- nothing unfinished is left behind (section 11 item 8) ------------------------------------


def _unfinished_markers(source: str) -> list[str]:
    """Return the unfinished-work markers found in the text of a module."""
    # No global IGNORECASE: each pattern carries its own case discipline, because TODO and
    # HACK are ordinary English words while FIXME and XXX are not.
    return [label for label, pattern in UNFINISHED_MARKERS if re.search(pattern, source)]


def _unimplemented_raises(source: str) -> list[str]:
    """Return the places that raise NotImplemented or NotImplementedError.

    Matched by shape, not by text: ``return NotImplemented`` is how a rich comparison declines an
    operand it does not know, and the storage core is right to use it.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        name = _dotted(node.exc)
        if name.split(".")[-1] in UNIMPLEMENTED_EXCEPTIONS:
            found.append(f"raise {name} (line {node.lineno})")
    return found


def _ellipsis_stubs(source: str) -> list[str]:
    """Return the concrete functions whose entire body is an ellipsis.

    A Protocol body, an ``@overload`` and an ``@abstractmethod`` are declarations, so an ellipsis
    is the correct spelling there. Anywhere else it is an unimplemented function that quietly
    returns None to whoever calls it.
    """
    tree = ast.parse(source)
    declarations: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(_dotted(base).split(".")[-1] in DECLARATION_BASES for base in node.bases):
            continue
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                declarations.add(id(child))

    stubs: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if id(node) in declarations:
            continue
        if any(
            _dotted(decorator).split(".")[-1] in STUB_EXEMPT_DECORATORS
            for decorator in node.decorator_list
        ):
            continue
        body = list(node.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        if len(body) != 1:
            continue
        statement = body[0]
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is Ellipsis
        ):
            stubs.append(f"{node.name} (line {node.lineno})")
    return stubs


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_no_module_carries_an_unfinished_marker(path: Path) -> None:
    found = _unfinished_markers(path.read_text(encoding="utf-8"))
    assert found == [], f"{_relative(path)} still carries {', '.join(found)}"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_no_module_raises_an_unimplemented_marker(path: Path) -> None:
    found = _unimplemented_raises(path.read_text(encoding="utf-8"))
    assert found == [], f"{_relative(path)} leaves {', '.join(found)}"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_no_concrete_function_has_an_ellipsis_body(path: Path) -> None:
    stubs = _ellipsis_stubs(path.read_text(encoding="utf-8"))
    assert stubs == [], f"{_relative(path)} leaves unimplemented bodies: {', '.join(stubs)}"


UNFINISHED_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("upper case todo", "# TODO: finish this"),
    ("lower case todo", "# todo: finish this"),
    ("mixed case todo", "# ToDo: finish this"),
    ("todo in a docstring", '"""Work in progress. TODO: finish."""'),
    ("todo with no separator", "# TODO finish this"),
    ("upper case fixme", "# FIXME: broken"),
    ("lower case fixme", "value = 1  # fixme"),
    ("mixed case fixme", "value = 1  # FixMe"),
    ("xxx marker", "# XXX: careful"),
    ("lower case xxx", "# xxx careful"),
    ("hack marker", "# HACK: works by accident"),
    ("pass stub verbatim", "def f() -> None:\n    pass  # stub"),
    ("pass stub tight", "def f() -> None:\n    pass # stub"),
    ("pass stub upper", "def f() -> None:\n    pass  # STUB"),
    ("pass stub with a tab", "def f() -> None:\n    pass\t#\tstub"),
)


@pytest.mark.parametrize(
    ("label", "source"), UNFINISHED_SPELLINGS, ids=[row[0] for row in UNFINISHED_SPELLINGS]
)
def test_the_marker_detector_flags_every_spelling(label: str, source: str) -> None:
    assert _unfinished_markers(source), label


FINISHED_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("todos is not todo", "todos = []"),
    ("an ordinary english todo", "# The remaining work is on the todo list."),
    ("a hack as a noun", '"""No hack was needed here."""'),
    ("hacking is not hack", '"""No hacking around here."""'),
    ("a plain pass", "def f() -> None:\n    pass"),
    ("a comment about a stub adapter", "# The stub adapter lives in the tests."),
    ("the comparison sentinel", "def __eq__(self, other: object) -> bool:\n    return NotImplemented"),
    ("ordinary code", "def f(value: int) -> int:\n    return value + 1"),
)


@pytest.mark.parametrize(
    ("label", "source"), FINISHED_SPELLINGS, ids=[row[0] for row in FINISHED_SPELLINGS]
)
def test_the_marker_detector_does_not_cry_wolf(label: str, source: str) -> None:
    assert _unfinished_markers(source) == [], label


RAISED_STUBS: tuple[tuple[str, str], ...] = (
    ("bare not implemented error", "def f() -> None:\n    raise NotImplementedError"),
    ("called not implemented error", "def f() -> None:\n    raise NotImplementedError('later')"),
    ("bare not implemented", "def f() -> None:\n    raise NotImplemented"),
    ("qualified", "import builtins\n\n\ndef f() -> None:\n    raise builtins.NotImplementedError"),
    ("inside a method", "class C:\n    def m(self) -> None:\n        raise NotImplementedError"),
)


@pytest.mark.parametrize(
    ("label", "source"), RAISED_STUBS, ids=[row[0] for row in RAISED_STUBS]
)
def test_the_raise_detector_flags_an_unimplemented_body(label: str, source: str) -> None:
    assert _unimplemented_raises(source), label


LEGITIMATE_RAISES: tuple[tuple[str, str], ...] = (
    ("comparison sentinel", "def __eq__(self, other: object) -> bool:\n    return NotImplemented"),
    ("a real error", "def f() -> None:\n    raise ValueError('no')"),
    ("a grafx error", "def f() -> None:\n    raise GrafxConfigurationError('no')"),
    ("a bare re-raise", "def f() -> None:\n    try:\n        g()\n    except ValueError:\n        raise"),
    ("the sentinel as a value", "value = NotImplemented"),
)


@pytest.mark.parametrize(
    ("label", "source"), LEGITIMATE_RAISES, ids=[row[0] for row in LEGITIMATE_RAISES]
)
def test_the_raise_detector_spares_a_legitimate_raise(label: str, source: str) -> None:
    assert _unimplemented_raises(source) == [], label


ELLIPSIS_STUBS: tuple[tuple[str, str], ...] = (
    ("bare function", "def f() -> None:\n    ..."),
    ("documented function", 'def f() -> None:\n    """Do the thing."""\n    ...'),
    ("method", "class C:\n    def m(self) -> None:\n        ..."),
    ("async function", "async def f() -> None:\n    ..."),
    ("class that is not a protocol", "class C(object):\n    def m(self) -> None:\n        ..."),
)


@pytest.mark.parametrize(
    ("label", "source"), ELLIPSIS_STUBS, ids=[row[0] for row in ELLIPSIS_STUBS]
)
def test_the_ellipsis_detector_flags_a_concrete_stub(label: str, source: str) -> None:
    assert _ellipsis_stubs(source), label


ELLIPSIS_DECLARATIONS: tuple[tuple[str, str], ...] = (
    (
        "protocol method",
        "from typing import Protocol\n\n\nclass P(Protocol):\n"
        '    def m(self) -> None:\n        """Do the thing."""\n        ...',
    ),
    (
        "protocol property",
        "from typing import Protocol\n\n\nclass P(Protocol):\n"
        "    @property\n    def name(self) -> str:\n        ...",
    ),
    ("overload", "from typing import overload\n\n\n@overload\ndef f(value: int) -> int:\n    ..."),
    (
        "abstract method",
        "from abc import ABC, abstractmethod\n\n\nclass C(ABC):\n"
        "    @abstractmethod\n    def m(self) -> None:\n        ...",
    ),
    ("a real body", "def f(value: int) -> int:\n    return value"),
)


@pytest.mark.parametrize(
    ("label", "source"), ELLIPSIS_DECLARATIONS, ids=[row[0] for row in ELLIPSIS_DECLARATIONS]
)
def test_the_ellipsis_detector_spares_a_declaration(label: str, source: str) -> None:
    assert _ellipsis_stubs(source) == [], label


# --- every module declares what it exports (A6) -----------------------------------------------


def _module_level_names(tree: ast.Module) -> set[str]:
    """Return every name bound by a module-level assignment."""
    return {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }


def _declared_exports(tree: ast.Module) -> list[str]:
    """Return every string named in ``__all__``, whatever expression shape holds it.

    A list, a tuple, a concatenation, a starred unpacking, a call: the strings are collected from
    the whole expression, so no spelling of ``__all__`` escapes the check that its entries exist.
    The previous rule looked only at a plain list or tuple and silently skipped everything else.
    """
    exports: list[str] = []

    def strings_of(value: ast.AST) -> list[str]:
        return [
            element.value
            for element in ast.walk(value)
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )
            if not any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in targets
            ):
                continue
            if node.value is not None:
                exports.extend(strings_of(node.value))
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # __all__.append("X") and __all__.extend([...]) advertise just as loudly.
            owner = node.func.value
            if (
                isinstance(owner, ast.Name)
                and owner.id == "__all__"
                and node.func.attr in {"append", "extend", "insert", "__iadd__"}
            ):
                for argument in node.args:
                    exports.extend(strings_of(argument))
    return exports


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_every_module_declares_what_it_exports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert "__all__" in _module_level_names(tree), f"{_relative(path)} does not declare __all__"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_every_declared_export_is_defined_or_imported_by_its_module(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    defined |= _module_level_names(tree)
    imported = {
        (alias.asname or alias.name).split(".")[0]
        for node in ast.walk(tree)
        for alias in (node.names if isinstance(node, (ast.Import, ast.ImportFrom)) else [])
    }
    for name in _declared_exports(tree):
        assert name in defined or name in imported, (
            f"{_relative(path)} exports {name!r}, which it does not define or import"
        )


def test_the_export_reader_sees_through_any_container_shape() -> None:
    # The hole this closes: a value that was not a plain list or tuple used to be skipped, so
    # everything it advertised went unchecked.
    def advertised(source: str) -> set[str]:
        return set(_declared_exports(ast.parse(source)))

    assert advertised('__all__ = ["A", "B"]') == {"A", "B"}
    assert advertised('__all__ = ("A",)') == {"A"}
    assert advertised('__all__ = ["A"] + ["B"]') == {"A", "B"}
    assert advertised('__all__ = [*["A"], "B"]') == {"A", "B"}
    assert advertised('__all__: list[str] = ["A"]') == {"A"}
    assert advertised('__all__ = list(["A"])') == {"A"}
    assert advertised('__all__ = ["A"] if True else ["B"]') == {"A", "B"}
    assert advertised('__all__ = sorted(["B", "A"])') == {"A", "B"}


def test_the_export_rule_catches_an_advertised_name_that_does_not_exist(tmp_path: Path) -> None:
    module = tmp_path / "impostor.py"
    module.write_text('__all__ = ["Nonexistent"] + ["AlsoMissing"]\n', encoding="utf-8")
    with pytest.raises(AssertionError, match="Nonexistent"):
        test_every_declared_export_is_defined_or_imported_by_its_module(module)


# --- one definition per shared bound (amendment A24) -------------------------------------------


def _bound_symbols(target: ast.expr, kind: str, line: int) -> list[str]:
    """Return the A24 symbols a binding target names, whatever its shape."""
    return [
        f"{element.id} ({kind} at line {line or element.lineno})"
        for element in ast.walk(target)
        if isinstance(element, ast.Name) and element.id in SINGLE_DEFINITION_SYMBOLS
    ]


def _dynamic_binding(node: ast.Call) -> list[str]:
    """Return the A24 symbols a call binds by name, such as setattr or a namespace update."""
    func = node.func
    if isinstance(func, ast.Attribute):
        callee = func.attr
    elif isinstance(func, ast.Name):
        callee = func.id
    else:
        callee = ""
    if callee == "setattr" and len(node.args) >= 2:
        name = node.args[1]
        if isinstance(name, ast.Constant) and name.value in SINGLE_DEFINITION_SYMBOLS:
            return [f"{name.value} (setattr at line {node.lineno})"]
        return []
    if callee == "update":
        for argument in list(node.args) + [keyword.value for keyword in node.keywords]:
            for element in ast.walk(argument):
                if (
                    isinstance(element, ast.Constant)
                    and element.value in SINGLE_DEFINITION_SYMBOLS
                ):
                    return [f"{element.value} (namespace update at line {node.lineno})"]
        for keyword in node.keywords:
            if keyword.arg in SINGLE_DEFINITION_SYMBOLS:
                return [f"{keyword.arg} (namespace update at line {node.lineno})"]
    return []


def _a24_offences(source: str) -> list[str]:
    """Return the A24 symbols this source binds to anything other than the imported original.

    The rule is inverted on purpose: an imported reference is the only accepted value. Anything
    else -- a literal, an arithmetic expression, a call, a lambda, a locally defined function --
    is a second definition wearing the name of the first, and the audit that prompted A24 walked
    straight past every one of those spellings.
    """
    tree = ast.parse(source)

    imported_origin: dict[str, str] = {}
    module_aliases: set[str] = set()
    offences: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound in SINGLE_DEFINITION_SYMBOLS and (
                    module not in OWNER_MODULES or alias.name != bound
                ):
                    # Binding an A24 name to anything but the same name from the owning module
                    # is a second definition: the alias is the binding.
                    offences.append(
                        f"{bound} (import alias of {module or '.'}.{alias.name} "
                        f"at line {node.lineno})"
                    )
            if module in OWNER_MODULES:
                for alias in node.names:
                    imported_origin[alias.asname or alias.name] = alias.name
            elif module in {"okto_grafx.domain", "okto_grafx"}:
                for alias in node.names:
                    if alias.name == "page":
                        module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in SINGLE_DEFINITION_SYMBOLS:
                    offences.append(
                        f"{bound} (module bound to the name at line {node.lineno})"
                    )
                if alias.name in OWNER_MODULES:
                    module_aliases.add(alias.asname or alias.name.split(".")[0])

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in SINGLE_DEFINITION_SYMBOLS:
                offences.append(f"{node.name} (redefined as a function at line {node.lineno})")
            continue
        if isinstance(node, ast.ClassDef) and node.name in SINGLE_DEFINITION_SYMBOLS:
            offences.append(f"{node.name} (redefined as a class at line {node.lineno})")
            continue
        if isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id in SINGLE_DEFINITION_SYMBOLS:
                offences.append(
                    f"{node.target.id} (augmented assignment at line {node.lineno})"
                )
            continue
        if isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name) and node.target.id in SINGLE_DEFINITION_SYMBOLS:
                offences.append(f"{node.target.id} (walrus at line {node.lineno})")
            continue
        if _TYPE_ALIAS is not None and isinstance(node, _TYPE_ALIAS):
            name = getattr(node, "name", None)
            if isinstance(name, ast.Name) and name.id in SINGLE_DEFINITION_SYMBOLS:
                offences.append(f"{name.id} (type alias at line {node.lineno})")
            continue
        if isinstance(node, (ast.For, ast.AsyncFor)):
            offences.extend(_bound_symbols(node.target, "loop target", node.lineno))
            continue
        if isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                offences.extend(
                    _bound_symbols(node.optional_vars, "with target", node.optional_vars.lineno)
                )
            continue
        if isinstance(node, ast.ExceptHandler):
            if node.name in SINGLE_DEFINITION_SYMBOLS:
                offences.append(f"{node.name} (except target at line {node.lineno})")
            continue
        if isinstance(node, (ast.comprehension,)):
            offences.extend(_bound_symbols(node.target, "comprehension target", 0))
            continue
        if isinstance(node, ast.Call):
            offences.extend(_dynamic_binding(node))
            continue
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            if node.attr in SINGLE_DEFINITION_SYMBOLS:
                offences.append(f"{node.attr} (attribute assignment at line {node.lineno})")
            continue
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            root = node.value
            if isinstance(root, ast.Call) and _dotted(root.func).rsplit(".", 1)[-1] in {
                "globals",
                "vars",
                "locals",
            }:
                index = node.slice
                if (
                    isinstance(index, ast.Constant)
                    and index.value in SINGLE_DEFINITION_SYMBOLS
                ):
                    offences.append(
                        f"{index.value} (namespace assignment at line {node.lineno})"
                    )
            continue
        if isinstance(node, ast.AnnAssign):
            targets: list[ast.expr] = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                # A destructuring target cannot bind the imported original, so any A24 name
                # inside one is a second definition however the right-hand side is spelled.
                for element in ast.walk(target):
                    if (
                        isinstance(element, ast.Name)
                        and element.id in SINGLE_DEFINITION_SYMBOLS
                    ):
                        offences.append(
                            f"{element.id} (destructured at line {node.lineno})"
                        )
                continue
            if target.id not in SINGLE_DEFINITION_SYMBOLS:
                continue
            value = node.value
            if value is None:
                offences.append(f"{target.id} (declared without a value at line {node.lineno})")
                continue
            if isinstance(value, ast.Name) and imported_origin.get(value.id) == target.id:
                continue
            if (
                isinstance(value, ast.Attribute)
                and value.attr == target.id
                and isinstance(value.value, ast.Name)
                and value.value.id in module_aliases
            ):
                continue
            offences.append(f"{target.id} ({type(value).__name__} at line {node.lineno})")
    return offences


def _a24_offences_of(path: Path) -> list[str]:
    """Return the A24 offences of one module."""
    return _a24_offences(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_only_the_storage_core_defines_the_shared_page_bounds(path: Path) -> None:
    # A24 exists because this duplication reappeared in a third component after being fixed in
    # the first two: two same-named constants that agree today are a divergence waiting for
    # someone to edit one of them.
    if SINGLE_DEFINITION_OWNER in path.parents:
        return
    offences = _a24_offences_of(path)
    assert offences == [], (
        f"{_relative(path)} declares {', '.join(offences)}; import them from "
        f"okto_grafx.domain.page instead (amendment A24)"
    )


def test_the_storage_core_really_owns_the_definitions() -> None:
    # The rule is only meaningful if the owner does define them.
    owned: set[str] = set()
    for path in SOURCE_FILES:
        if SINGLE_DEFINITION_OWNER not in path.parents:
            continue
        owned.update(offence.split(" ", 1)[0] for offence in _a24_offences_of(path))
    assert owned == SINGLE_DEFINITION_SYMBOLS


A24_REDECLARATIONS: tuple[tuple[str, str], ...] = (
    ("plain literal", "MIN_PAGE_SIZE = 512"),
    ("annotated literal", "MAX_PAGE_SIZE: int = 32768"),
    ("power expression", "MIN_PAGE_SIZE = 2 ** 8"),
    ("product expression", "MAX_PAGE_SIZE = 64 * 1024"),
    ("unary plus", "MIN_PAGE_SIZE = +256"),
    ("call", "MIN_PAGE_SIZE = int('256')"),
    ("lambda validator", "validate_page_size = lambda page_size: page_size"),
    (
        "local function bound to the name",
        "def _looser(page_size):\n    return page_size\n\n\nvalidate_page_size = _looser",
    ),
    ("redefined function", "def validate_page_size(page_size):\n    return page_size"),
    ("redefined async function", "async def validate_page_size(page_size):\n    return page_size"),
    ("declared without a value", "MIN_PAGE_SIZE: int"),
    ("conditional expression", "MIN_PAGE_SIZE = 512 if True else 256"),
    ("tuple unpacking", "MIN_PAGE_SIZE, MAX_PAGE_SIZE = 512, 32768"),
    (
        "imported from somewhere else",
        "from settings import MIN_PAGE_SIZE as CORE\nMIN_PAGE_SIZE = CORE",
    ),
    (
        "swapped alias",
        "from okto_grafx.domain.page import MAX_PAGE_SIZE as CORE_MAX\nMIN_PAGE_SIZE = CORE_MAX",
    ),
    ("attribute of the wrong module", "import settings\nMIN_PAGE_SIZE = settings.MIN_PAGE_SIZE"),
)


@pytest.mark.parametrize(
    ("label", "source"), A24_REDECLARATIONS, ids=[row[0] for row in A24_REDECLARATIONS]
)
def test_the_a24_detector_flags_every_redeclaration(label: str, source: str) -> None:
    assert _a24_offences(source), label


A24_ACCEPTED: tuple[tuple[str, str], ...] = (
    (
        "aliased import bound through",
        "from okto_grafx.domain.page import MIN_PAGE_SIZE as CORE_MIN_PAGE_SIZE\n"
        "MIN_PAGE_SIZE: int = CORE_MIN_PAGE_SIZE",
    ),
    ("plain import re-exported", "from okto_grafx.domain.page import MAX_PAGE_SIZE"),
    (
        "module attribute",
        "from okto_grafx.domain import page\nMAX_PAGE_SIZE = page.MAX_PAGE_SIZE",
    ),
    (
        "imported from the layout module",
        "from okto_grafx.domain.page.layout import MIN_PAGE_SIZE as FLOOR\nMIN_PAGE_SIZE = FLOOR",
    ),
    ("an unrelated constant", "PAGE_HEADER_SIZE = 32"),
    (
        "a lowercase local",
        "def f(page_size: int) -> int:\n    max_page_size = 32768\n    return max_page_size",
    ),
    (
        "the validator imported and used",
        "from okto_grafx.domain.page import validate_page_size\n\n\n"
        "def f(size: int) -> int:\n    return validate_page_size(size)",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), A24_ACCEPTED, ids=[row[0] for row in A24_ACCEPTED]
)
def test_the_a24_detector_accepts_an_imported_reference(label: str, source: str) -> None:
    assert _a24_offences(source) == [], label


def test_the_impostor_module_from_the_audit_is_caught(tmp_path: Path) -> None:
    # The exact source the audit appended to src/okto_grafx/errors.py, which the whole suite
    # missed: two bounds and a validator, none of them literals.
    impostor = (
        "def _looser(page_size: int) -> int:\n    return page_size\n\n\n"
        "MIN_PAGE_SIZE = 2 ** 8\n"
        "MAX_PAGE_SIZE = 64 * 1024\n"
        "validate_page_size = _looser\n"
    )
    offences = _a24_offences(impostor)
    assert len(offences) == 3
    assert {offence.split(" ", 1)[0] for offence in offences} == SINGLE_DEFINITION_SYMBOLS

    module = tmp_path / "impostor.py"
    module.write_text(impostor, encoding="utf-8")
    with pytest.raises(AssertionError, match="amendment A24"):
        test_only_the_storage_core_defines_the_shared_page_bounds(module)


def test_the_configuration_derives_both_bounds() -> None:
    # The live assertion for the module that got this wrong twice.
    assert _a24_offences_of(PACKAGE_ROOT / "runtime" / "config.py") == []


A24_DYNAMIC_REDECLARATIONS: tuple[tuple[str, str], ...] = (
    ("walrus at module level", "if (MIN_PAGE_SIZE := 512):\n    pass"),
    (
        "walrus in a comprehension",
        "sizes = [MAX_PAGE_SIZE := size for size in (512, 1024)]",
    ),
    ("augmented assignment", "MIN_PAGE_SIZE += 1"),
    ("globals assignment", "globals()['MIN_PAGE_SIZE'] = 256"),
    ("vars assignment", "vars()['MAX_PAGE_SIZE'] = 65536"),
    ("setattr on a module", "import settings\nsetattr(settings, 'MIN_PAGE_SIZE', 256)"),
    (
        "namespace update",
        "import settings\nvars(settings).update({'MAX_PAGE_SIZE': 65536})",
    ),
    ("namespace update by keyword", "import settings\nvars(settings).update(MAX_PAGE_SIZE=65536)"),
    ("for loop target", "for MIN_PAGE_SIZE in (256, 512):\n    pass"),
    ("with target", "with open_thing() as MAX_PAGE_SIZE:\n    pass"),
    ("except target", "try:\n    pass\nexcept ValueError as MIN_PAGE_SIZE:\n    pass"),
    ("comprehension target", "sizes = [MIN_PAGE_SIZE for MIN_PAGE_SIZE in (256,)]"),
    ("class of the same name", "class validate_page_size:\n    pass"),
)


@pytest.mark.parametrize(
    ("label", "source"),
    A24_DYNAMIC_REDECLARATIONS,
    ids=[row[0] for row in A24_DYNAMIC_REDECLARATIONS],
)
def test_the_a24_detector_flags_a_dynamic_redeclaration(label: str, source: str) -> None:
    assert _a24_offences(source), label


A24_DYNAMIC_ACCEPTED: tuple[tuple[str, str], ...] = (
    ("an unrelated walrus", "if (size := 512):\n    pass"),
    ("an unrelated loop target", "for size in (256, 512):\n    pass"),
    ("an unrelated setattr", "import settings\nsetattr(settings, 'PAGE_HEADER_SIZE', 32)"),
    ("an unrelated update", "import settings\nvars(settings).update({'DEFAULT': 1})"),
    (
        "reading the imported bound in a loop",
        "from okto_grafx.domain.page import MAX_PAGE_SIZE\nfor size in (512, MAX_PAGE_SIZE):\n    pass",
    ),
    (
        "calling the imported validator",
        "from okto_grafx.domain.page import validate_page_size\nsize = validate_page_size(512)",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"),
    A24_DYNAMIC_ACCEPTED,
    ids=[row[0] for row in A24_DYNAMIC_ACCEPTED],
)
def test_the_a24_detector_spares_an_unrelated_dynamic_binding(label: str, source: str) -> None:
    assert _a24_offences(source) == [], label


A24_IMPORT_ALIASES: tuple[tuple[str, str], ...] = (
    ("aliased from a foreign module", "from settings import floor as MIN_PAGE_SIZE"),
    (
        "aliased validator from a foreign module",
        "from settings import loose_check as validate_page_size",
    ),
    (
        "imported from a re-exporter",
        "from okto_grafx.runtime.config import MAX_PAGE_SIZE",
    ),
    (
        "aliased from the owner under the wrong name",
        "from okto_grafx.domain.page import validate_page_size as MIN_PAGE_SIZE",
    ),
    ("module bound to the name", "import settings as MIN_PAGE_SIZE"),
    ("relative import alias", "from .settings import floor as MAX_PAGE_SIZE"),
    (
        "attribute assignment into a module",
        "import sys\nsys.modules[__name__].MIN_PAGE_SIZE = 256",
    ),
    (
        "all three at once",
        "from settings import floor as MIN_PAGE_SIZE\n"
        "from settings import ceiling as MAX_PAGE_SIZE\n"
        "from settings import loose_check as validate_page_size",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), A24_IMPORT_ALIASES, ids=[row[0] for row in A24_IMPORT_ALIASES]
)
def test_the_a24_detector_flags_an_import_alias(label: str, source: str) -> None:
    # An import alias is a binding in the syntax tree, which is exactly what the rule claims to
    # read. Deleting one line of the two-line form used to defeat the whole detector.
    assert _a24_offences(source), label


def test_the_a24_detector_catches_all_three_aliases_at_once() -> None:
    source = (
        "from settings import floor as MIN_PAGE_SIZE\n"
        "from settings import ceiling as MAX_PAGE_SIZE\n"
        "from settings import loose_check as validate_page_size\n"
    )
    offences = _a24_offences(source)
    assert {offence.split(" ", 1)[0] for offence in offences} == SINGLE_DEFINITION_SYMBOLS


A24_IMPORT_ACCEPTED: tuple[tuple[str, str], ...] = (
    ("same name from the owner", "from okto_grafx.domain.page import MIN_PAGE_SIZE"),
    (
        "same name from the layout module",
        "from okto_grafx.domain.page.layout import MAX_PAGE_SIZE",
    ),
    (
        "aliased to a local name",
        "from okto_grafx.domain.page import MIN_PAGE_SIZE as CORE_MIN_PAGE_SIZE",
    ),
    ("the validator from the owner", "from okto_grafx.domain.page import validate_page_size"),
    ("an unrelated import", "from settings import PAGE_HEADER_SIZE"),
    ("an unrelated module alias", "import settings as configuration"),
)


@pytest.mark.parametrize(
    ("label", "source"), A24_IMPORT_ACCEPTED, ids=[row[0] for row in A24_IMPORT_ACCEPTED]
)
def test_the_a24_detector_accepts_an_honest_import(label: str, source: str) -> None:
    assert _a24_offences(source) == [], label


def test_a_type_alias_redeclaration_is_flagged_where_the_syntax_exists() -> None:
    """The PEP 695 branch of the A24 rule, checked on every interpreter that can express it.

    The probe INPUT is parsed too, so it cannot be a bare parametrize entry: on 3.11 the string
    is a SyntaxError and the case dies before the rule is ever consulted (A38). It does not
    follow that the test should be skipped there -- it should assert what is true there. On 3.11
    the truth is that the syntax does not exist and the rule correctly has nothing to match; on
    3.12 and later the truth is that the redeclaration is flagged. Both run, neither is skipped,
    and no marker is needed.

    Recorded because it was got wrong: this test briefly carried @pytest.mark.platform_specific,
    which is not a platform question at all. A marker with no family and no condition names
    nothing, demands no counterpart and costs nothing -- the password A54 exists to withdraw,
    reached for by the component that owns the gate. The parity gate caught it.
    """
    if _TYPE_ALIAS is None:
        assert not hasattr(ast, "TypeAlias")
        assert _a24_offences("MIN_PAGE_SIZE = 512")
        return
    assert _a24_offences("type MIN_PAGE_SIZE = int")


def test_the_a24_rule_runs_on_the_minimum_interpreter() -> None:
    # A38: ast.TypeAlias does not exist on 3.11, which pyproject declares as the minimum. The
    # node is looked up rather than referenced, so the rule runs there instead of raising.
    assert _TYPE_ALIAS is None or _TYPE_ALIAS is ast.TypeAlias
    for path in SOURCE_FILES[:3]:
        _a24_offences_of(path)
    if _TYPE_ALIAS is not None:
        assert _a24_offences("type MIN_PAGE_SIZE = int")


ALL_MUTATIONS: tuple[tuple[str, str], ...] = (
    ("augmented assignment", '__all__ = ["A"]\n__all__ += ["Nonexistent"]'),
    ("append", '__all__ = ["A"]\n__all__.append("Nonexistent")'),
    ("extend", '__all__ = ["A"]\n__all__.extend(["Nonexistent"])'),
    ("insert", '__all__ = ["A"]\n__all__.insert(0, "Nonexistent")'),
)


@pytest.mark.parametrize(
    ("label", "source"), ALL_MUTATIONS, ids=[row[0] for row in ALL_MUTATIONS]
)
def test_the_export_reader_follows_a_mutated_all(label: str, source: str) -> None:
    assert "Nonexistent" in _declared_exports(ast.parse(source)), label


def test_a_mutated_all_advertising_a_phantom_fails_the_rule(tmp_path: Path) -> None:
    module = tmp_path / "impostor.py"
    module.write_text('__all__ = []\n__all__.append("Nonexistent")\n', encoding="utf-8")
    with pytest.raises(AssertionError, match="Nonexistent"):
        test_every_declared_export_is_defined_or_imported_by_its_module(module)
