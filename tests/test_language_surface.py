"""The product surface is en-US (CONTRACT.md G1, SPEC-M1 preamble).

Class names, error codes, messages and docstrings are the product; the specs that describe them
are written in pt-BR, and this gate is what keeps the two apart. It checks three things:

1. every public symbol of ``okto_grafx.errors`` carries a documented, sentence-shaped docstring;
2. every error code is a unique snake_case token;
3. no source file under ``src/`` carries Portuguese in a docstring or in a string literal.

Heuristic for item 3. Portuguese written correctly uses characters outside ASCII, so the first
rule is simply that source files are pure ASCII: that alone catches "nao", "esta", "sao" and any
word with a cedilla or a tilde. Accent-stripped Portuguese survives that rule, so a second pass
matches a short list of function words that are unambiguous once they are surrounded by spaces
(" nao ", " uma ", " para o ", ...) plus two suffix fragments ("cao ", "coes "). Every marker was
chosen because no English word contains it as a whole token, which keeps the false-positive rate
at zero on the current tree; the assertion message names the marker so a future en-US sentence
that trips it can be re-checked deliberately.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from okto_grafx import errors as public_errors
from okto_grafx.domain.errors import GrafxError
from okto_grafx.domain.ports.metrics import NON_EN_US_MARKERS

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
PACKAGE_ROOT: Path = PROJECT_ROOT / "src" / "okto_grafx"
SOURCE_FILES: list[Path] = sorted(PACKAGE_ROOT.rglob("*.py"))

PORTUGUESE_MARKERS: tuple[str, ...] = NON_EN_US_MARKERS
"""The marker list is owned by ``domain/ports/metrics.py`` so the registration-time check
on a metric description and this source-tree gate can never drift apart."""

MARKER_DEFINITION_NAME: str = "NON_EN_US_MARKERS"
"""The one assignment whose own string literals are the markers, and so are not evidence."""

MARKER_DEFINITION_MODULE: Path = PACKAGE_ROOT / "domain" / "ports" / "metrics.py"
"""The single module that owns the marker list. The exemption is granted nowhere else, so no
other module can smuggle a pt-BR message past this gate by naming a constant NON_EN_US_MARKERS."""


PORTUGUESE_WORDS: frozenset[str] = frozenset(
    {
        "ainda", "antes", "apenas", "aberto", "aqui", "arquivo", "atualizar", "banco", "buscar",
        "cabecalho", "cada", "campo", "chave", "cheio", "coluna", "consulta", "contagem",
        "criar", "dados", "depois", "deve", "devem", "entao", "entrada", "entre", "erro",
        "escrita", "espaco", "esta", "estao", "excluir", "falso", "fechado", "gravado",
        "gravar", "indice", "inicio", "leitura", "linha", "lista", "memoria", "mesmo", "muito",
        "nao", "nome", "novo", "nunca", "onde", "pagina", "pela", "pelo", "porque", "pouco",
        "primeiro", "processo", "quando", "quantidade", "recuperacao", "registro", "resultado",
        "retorno", "saida", "salvar", "sao", "sempre", "sobre", "tabela", "tamanho", "tipo",
        "todas", "todos", "transacao", "ultimo", "uma", "umas", "usuario", "validacao", "valor",
        "vazio", "verdadeiro",
    }
)
"""Curated pt-BR words used to keep identifiers en-US (guideline G1, first surface named).

Heuristic, and its limits. Identifiers are split on underscores and then on CamelCase
boundaries, lowercased, and each token is looked up in this list; a hit fails the gate. The list
is deliberately small and curated: every word was checked against the complete identifier
vocabulary of ``src/`` (716 distinct tokens at the time of writing, across every delivered
component) and against ordinary English, so a hit means Portuguese rather than a coincidence.
Words of one or two letters are excluded entirely, and the only three-letter words kept are
those with no plausible reading as an English abbreviation ("nao", "sao", "uma"); words such as
"fim", "uso" and "dos" are left out for that reason, as is "tempo", which is also English.

What it does not catch: a Portuguese word absent from the list, a single-token identifier whose
spelling is also English, and local variable names, which are not a product surface. The gate is
a floor, not a proof; code review remains the ceiling.
"""

IDENTIFIER_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+")
"""Splits CamelCase and acronyms, so a class name is examined word by word like a snake_case one."""


def _identifier_tokens(name: str) -> list[str]:
    """Return the lowercase words of an identifier, split on underscores and CamelCase."""
    tokens: list[str] = []
    for part in name.split("_"):
        tokens.extend(word.lower() for word in IDENTIFIER_WORD.findall(part))
    return tokens


def _portuguese_tokens(name: str) -> list[str]:
    """Return the pt-BR words found inside one identifier."""
    return [token for token in _identifier_tokens(name) if token in PORTUGUESE_WORDS]


def _surface_identifiers(tree: ast.Module) -> list[tuple[str, str, int]]:
    """Return (kind, name, line) for every identifier that belongs to the product surface.

    Functions, classes and their parameters, plus module-level and class-level assignment
    targets: the last of these is where dataclass fields live, and a field name is as public as
    a method name. Local variables are out of scope on purpose (CONTRACT.md G1 names API,
    exception, metric and CLI surfaces).
    """
    surfaces: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            surfaces.append(("function", node.name, node.lineno))
            arguments = [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]
            if node.args.vararg is not None:
                arguments.append(node.args.vararg)
            if node.args.kwarg is not None:
                arguments.append(node.args.kwarg)
            surfaces.extend(("parameter", argument.arg, argument.lineno) for argument in arguments)
        elif isinstance(node, ast.ClassDef):
            surfaces.append(("class", node.name, node.lineno))

    bodies: list[list[ast.stmt]] = [tree.body]
    bodies.extend(node.body for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    for body in bodies:
        for statement in body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                surfaces.append(("attribute", statement.target.id, statement.lineno))
            elif isinstance(statement, ast.Assign):
                surfaces.extend(
                    ("attribute", target.id, statement.lineno)
                    for target in statement.targets
                    if isinstance(target, ast.Name)
                )
    return surfaces


def _relative(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")


def _string_constants(tree: ast.AST, *, exclude: set[int] | None = None) -> list[str]:
    """Return every string literal of a module, which includes all of its docstrings."""
    skipped = exclude or set()
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skipped
    ]


def _public_error_symbols() -> list[tuple[str, type]]:
    return [(name, getattr(public_errors, name)) for name in public_errors.__all__]


PUBLIC_ERROR_SYMBOLS: list[tuple[str, type]] = _public_error_symbols()


def test_the_gate_sees_the_whole_package() -> None:
    assert len(SOURCE_FILES) >= 15
    assert PUBLIC_ERROR_SYMBOLS


@pytest.mark.parametrize(
    ("name", "symbol"), PUBLIC_ERROR_SYMBOLS, ids=[name for name, _ in PUBLIC_ERROR_SYMBOLS]
)
def test_every_public_error_has_an_en_us_docstring(name: str, symbol: type) -> None:
    documentation = symbol.__doc__
    assert documentation, f"{name} has no docstring"
    text = documentation.strip()
    assert text.isascii(), f"{name} has a docstring outside ASCII"
    assert text[0].isupper(), f"{name} has a docstring that does not start with a capital"
    assert text.endswith((".", "!")), f"{name} has a docstring that is not a sentence"
    assert len(text.split()) >= 5, f"{name} has a docstring too short to say anything"


@pytest.mark.parametrize(
    ("name", "symbol"), PUBLIC_ERROR_SYMBOLS, ids=[name for name, _ in PUBLIC_ERROR_SYMBOLS]
)
def test_every_public_error_docstring_is_free_of_portuguese(name: str, symbol: type) -> None:
    text = f" {(symbol.__doc__ or '').lower()} "
    for marker in PORTUGUESE_MARKERS:
        assert marker not in text, f"{name} docstring matches the marker {marker!r}"


def test_the_public_module_and_its_symbols_are_documented() -> None:
    assert public_errors.__doc__
    assert public_errors.__doc__.isascii()


def test_error_codes_are_unique_snake_case() -> None:
    codes: dict[str, str] = {}
    for name, symbol in PUBLIC_ERROR_SYMBOLS:
        assert issubclass(symbol, GrafxError)
        code = symbol.code
        assert isinstance(code, str) and code, f"{name} has no code"
        assert code.isascii(), f"{name} has a code outside ASCII"
        assert code == code.lower(), f"{name} has a code that is not lowercase: {code!r}"
        assert " " not in code and "-" not in code, f"{name} has a code that is not snake_case"
        assert not code.startswith("_") and not code.endswith("_"), f"{name} has a padded code"
        assert "__" not in code, f"{name} has a code with a double underscore"
        for character in code:
            assert character.islower() or character.isdigit() or character == "_", (
                f"{name} has a code with an unexpected character: {character!r}"
            )
        assert code not in codes, f"{name} reuses the code of {codes.get(code)}"
        codes[code] = name
    assert len(codes) == len(PUBLIC_ERROR_SYMBOLS)


def test_the_base_error_code_is_not_shadowed_by_a_subclass() -> None:
    assert GrafxError.code == "grafx_error"
    assert "grafx_error" not in {symbol.code for _, symbol in PUBLIC_ERROR_SYMBOLS if symbol is not GrafxError}


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_every_source_file_is_pure_ascii(path: Path) -> None:
    raw = path.read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as failure:  # pragma: no cover - only on a real violation
        pytest.fail(f"{_relative(path)} is not ASCII at byte {failure.start}")


def _marker_definition_literals(tree: ast.AST, path: Path | None = None) -> set[int]:
    """Return the identities of the string literals that define the marker list itself.

    Only the owning module is granted the exemption; ``path`` of None means the caller is a
    self-test working on a synthetic tree and wants the rule exercised directly.
    """
    if path is not None and path.resolve() != MARKER_DEFINITION_MODULE.resolve():
        return set()
    excluded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == MARKER_DEFINITION_NAME
            for target in targets
        ):
            continue
        if node.value is None:
            continue
        for descendant in ast.walk(node.value):
            if isinstance(descendant, ast.Constant) and isinstance(descendant.value, str):
                excluded.add(id(descendant))
    return excluded


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_no_string_literal_in_the_source_reads_as_portuguese(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for literal in _string_constants(tree, exclude=_marker_definition_literals(tree, path)):
        haystack = f" {literal.lower()} "
        for marker in PORTUGUESE_MARKERS:
            assert marker not in haystack, (
                f"{_relative(path)} has a string matching the marker {marker!r}: {literal[:80]!r}"
            )


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_every_module_and_public_definition_is_documented(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert ast.get_docstring(tree), f"{_relative(path)} has no module docstring"
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and not node.name.startswith("_"):
            assert ast.get_docstring(node), (
                f"{_relative(path)} has no docstring for {node.name} at line {node.lineno}"
            )


def test_the_markers_detect_portuguese_and_spare_english() -> None:
    # The heuristic is only worth something if it fires on real Portuguese and stays silent on
    # the kind of English these docstrings actually contain.
    portuguese = [
        "o commit nao retorna antes da barreira.",
        "a entrada esta no ledger.",
        "os registros sao descartados.",
        "uma pagina por transacao.",
        "a validacao ocorre para o commit.",
        "reconciliacao de tombstones e as excecoes.",
    ]
    english = [
        "Every byte the engine persists goes through here.",
        "Return True when the space is reclaimed immediately.",
        "The commit does not return before the durability barrier completes.",
        "A partition key is a 64-bit value built from the table id.",
        "Location, vacation and the escalation of a cascading failure.",
        "This measurement is taken with the observer's own monotonic clock.",
    ]
    for sentence in portuguese:
        haystack = f" {sentence.lower()} "
        assert any(marker in haystack for marker in PORTUGUESE_MARKERS), sentence
    for sentence in english:
        haystack = f" {sentence.lower()} "
        matched = [marker for marker in PORTUGUESE_MARKERS if marker in haystack]
        assert matched == [], f"{sentence!r} falsely matched {matched}"


def test_the_marker_exclusion_only_spares_the_definition_itself() -> None:
    # The module that owns the marker list is still scanned; only the literals that ARE the
    # markers are exempt, so pt-BR anywhere else in that file is still a failure.
    source = 'NON_EN_US_MARKERS = (" nao ", "cao ")\nOTHER = "a validacao nao ocorre."\n'
    tree = ast.parse(source)
    literals = _string_constants(tree, exclude=_marker_definition_literals(tree))
    assert " nao " not in literals
    assert "a validacao nao ocorre." in literals


def test_the_registration_check_and_this_gate_share_one_marker_list() -> None:
    assert PORTUGUESE_MARKERS is NON_EN_US_MARKERS
    assert len(NON_EN_US_MARKERS) == len(set(NON_EN_US_MARKERS))
    for marker in NON_EN_US_MARKERS:
        assert marker.isascii() and marker == marker.lower()


def test_the_marker_exemption_is_granted_to_one_module_only() -> None:
    # The same source, in the owning module and anywhere else: only the owner is spared.
    source = 'NON_EN_US_MARKERS = (" nao ", "cao ")\n'
    tree = ast.parse(source)
    assert _marker_definition_literals(tree, MARKER_DEFINITION_MODULE) != set()
    assert _marker_definition_literals(tree, PACKAGE_ROOT / "domain" / "errors.py") == set()
    assert _marker_definition_literals(tree, PACKAGE_ROOT / "runtime" / "config.py") == set()


def test_a_module_cannot_borrow_the_exemption_by_reusing_the_name() -> None:
    # An impostor module defining a constant with the owning name is still fully scanned.
    impostor = PACKAGE_ROOT / "domain" / "errors.py"
    source = 'NON_EN_US_MARKERS = ("a validacao nao ocorre.",)\n'
    tree = ast.parse(source)
    literals = _string_constants(tree, exclude=_marker_definition_literals(tree, impostor))
    assert "a validacao nao ocorre." in literals


def test_the_owning_module_is_still_scanned_outside_its_marker_assignment() -> None:
    source = 'NON_EN_US_MARKERS = (" nao ",)\nOTHER = "a validacao nao ocorre."\n'
    tree = ast.parse(source)
    literals = _string_constants(
        tree, exclude=_marker_definition_literals(tree, MARKER_DEFINITION_MODULE)
    )
    assert " nao " not in literals
    assert "a validacao nao ocorre." in literals


# --- identifiers are en-US too (G1 names them first) ------------------------------------------


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_no_identifier_in_the_source_reads_as_portuguese(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for kind, name, line in _surface_identifiers(tree):
        found = _portuguese_tokens(name)
        assert not found, f"{_relative(path)}:{line} {kind} {name!r} contains pt-BR {found}"


def test_the_gate_examines_a_real_number_of_identifiers() -> None:
    # A scan that finds nothing to look at passes for the wrong reason.
    total = 0
    for path in SOURCE_FILES:
        total += len(_surface_identifiers(ast.parse(path.read_text(encoding="utf-8"))))
    assert total >= 200


@pytest.mark.parametrize(
    "name",
    [
        "validacao_do_commit",
        "escrita_nao_permitida",
        "ValidacaoDeCommit",
        "PaginaDeDados",
        "tamanho_maximo",
        "ler_registro",
        "indice_secundario",
        "espaco_de_embedding",
        "TabelaDeSimbolos",
        "valor_atualizar",
    ],
)
def test_the_identifier_heuristic_flags_a_portuguese_name(name: str) -> None:
    assert _portuguese_tokens(name)


@pytest.mark.parametrize(
    "name",
    [
        "durable_barrier",
        "GrafxWriteConflict",
        "read_page",
        "partitions_per_table",
        "granularity_descriptor",
        "MetricDescriptor",
        "reader_horizon",
        "vector_exact_scan_threshold",
        "NO_PAGE",
        "RecordRef",
        "to_dict",
        "page_size",
        "checksum",
        "SplitMix64",
        "encode_page",
        "list_files",
        "atomic_replace",
        "snapshot_lsn",
    ],
)
def test_the_identifier_heuristic_spares_the_real_vocabulary(name: str) -> None:
    assert _portuguese_tokens(name) == []


def test_a_planted_portuguese_definition_is_caught() -> None:
    # The gate must fail on the shape of the hole it exists to close.
    source = (
        "def validacao_do_commit(pagina):\n"
        "    return pagina\n\n\n"
        "class RegistroDeEscrita:\n"
        "    tamanho: int = 0\n"
    )
    tree = ast.parse(source)
    flagged = [name for _, name, _ in _surface_identifiers(tree) if _portuguese_tokens(name)]
    assert set(flagged) == {"validacao_do_commit", "pagina", "RegistroDeEscrita", "tamanho"}


def test_the_word_list_carries_no_english_word_of_this_codebase() -> None:
    vocabulary: set[str] = set()
    for path in SOURCE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for _, name, _ in _surface_identifiers(tree):
            vocabulary.update(_identifier_tokens(name))
    assert vocabulary & PORTUGUESE_WORDS == set()
    assert all(word.isascii() and word.islower() for word in PORTUGUESE_WORDS)
    assert all(len(word) >= 3 for word in PORTUGUESE_WORDS)
