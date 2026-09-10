"""Execute temporal consumer examples as printed, including reopen and retention."""

from pathlib import Path
import re


def test_temporal_consumer_workflow(tmp_path, monkeypatch):
    guide = Path(__file__).resolve().parents[2] / "docs" / "SYSTEM_TIME_HISTORY.md"
    source = guide.read_text(encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    namespace = {"__name__": "__temporal_example__"}
    examples = re.findall(r"```python\n(.*?)```", source, re.DOTALL)
    assert len(examples) == 2
    for example in examples:
        exec(compile(example, guide.name, "exec"), namespace)
