"""Run published consumer examples against an isolated installed wheel, not src/."""

from __future__ import annotations

from pathlib import Path
import re
import zipfile
import okto_grafx


def main() -> None:
    """Prove installed origins, package/source parity and executable documentation."""
    root = Path(__file__).resolve().parents[1]
    package = Path(okto_grafx.__file__).resolve().parent
    assert "site-packages" in package.parts and package != root / "src" / "okto_grafx"
    assert okto_grafx.__version__ == "0.0.5"
    wheel = root / ".grafx-tmp/search-resume-dist/okto_grafx-0.0.5-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        files = [
            n
            for n in archive.namelist()
            if n.startswith("okto_grafx/") and n.endswith(".py")
        ]
        for name in files:
            relative = name.removeprefix("okto_grafx/")
            assert archive.read(name) == (root / "src" / name).read_bytes()
            assert archive.read(name) == (package / relative).read_bytes()
    examples = 0
    for guide in ("FULL_TEXT_SEARCH.md", "HYBRID_SEARCH.md", "LOGICAL_TRANSFER.md", "SCHEMA_MIGRATIONS.md"):
        content = (root / "docs" / guide).read_text(encoding="utf-8")
        blocks = re.findall(r"```python\n(.*?)```", content, re.S)
        assert blocks
        namespace = {"__name__": "__installed_example__"}
        for block in blocks:
            exec(compile(block, guide, "exec"), namespace)
            examples += 1
    print(
        f"PASS: {len(files)} package files match source/wheel/install; {examples} installed documentation examples; origin={package}"
    )


if __name__ == "__main__":
    main()
