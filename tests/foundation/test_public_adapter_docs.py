"""Executable public examples and the trust boundary for caller-supplied adapters."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from okto_grafx import DatabaseConfig, connect
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports

ROOT = Path(__file__).resolve().parents[2]
DOC_TEST_MARKER = "<!-- okto-grafx-doc-test -->"
MARKED_PYTHON = re.compile(
    rf"{re.escape(DOC_TEST_MARKER)}\s*```python\r?\n(?P<source>.*?)\r?\n```",
    re.DOTALL,
)
PUBLIC_EXAMPLES = (
    pytest.param(ROOT / "docs" / "INTEGRATION.md", 1, id="integration"),
    pytest.param(ROOT / "docs" / "PORTS.md", 4, id="ports"),
)


@pytest.mark.parametrize(("document", "expected_count"), PUBLIC_EXAMPLES)
def test_the_marked_public_adapter_examples_execute_in_order(
    document: Path, expected_count: int
) -> None:
    """Keep public extension examples coupled to the API they teach."""
    source = document.read_text(encoding="utf-8")
    snippets = [match.group("source") for match in MARKED_PYTHON.finditer(source)]
    assert len(snippets) == expected_count

    namespace: dict[str, object] = {
        "__name__": f"_okto_grafx_doc_test_{document.stem.lower()}"
    }
    for index, snippet in enumerate(snippets, start=1):
        filename = f"{document.as_posix()}#okto-grafx-doc-test-{index}"
        exec(compile(snippet, filename, "exec"), namespace)


def test_a_custom_adapter_exception_propagates_without_reclassification() -> None:
    """Custom adapters are trusted host code, not a second Grafx error boundary."""
    failure = RuntimeError("trusted custom clock failure")

    class BrokenClock:
        def monotonic(self) -> float:
            raise failure

        def wall(self) -> float:
            return 0.0

    config = DatabaseConfig(path=":memory:")
    registry = build_default_registry(config)
    registry.bind("clock", BrokenClock())
    try:
        with pytest.raises(RuntimeError, match="trusted custom clock failure") as raised:
            database = connect(":memory:", registry=registry)
            try:
                with database.begin("read"):
                    pass
            finally:
                database.close()
        assert raised.value is failure
    finally:
        release_ports(registry)
