"""Execute consumer examples as published, including their assertions."""

from pathlib import Path
import re

import pytest


@pytest.mark.parametrize("guide", ["FULL_TEXT_SEARCH.md", "LOGICAL_TRANSFER.md"])
def test_documented_python_workflow(guide):
    source = (Path(__file__).resolve().parents[2] / "docs" / guide).read_text(
        encoding="utf-8"
    )
    examples = re.findall(r"```python\n(.*?)```", source, re.DOTALL)
    assert examples
    namespace = {"__name__": "__documented_example__"}
    for example in examples:
        exec(compile(example, guide, "exec"), namespace)
