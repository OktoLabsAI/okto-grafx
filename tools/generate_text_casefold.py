"""Freeze Unicode 15.1 case folding for FTS-v1; never generate it during installation."""

from pathlib import Path
import unicodedata

assert unicodedata.unidata_version == "15.1.0", (
    "Use Python's Unicode 15.1 build for this frozen format."
)
target = (
    Path(__file__).resolve().parents[1] / "src/okto_grafx/domain/index/text_casefold.py"
)
rows = [
    '"""Generated Unicode 15.1 full case folding; see tools/generate_text_casefold.py."""',
    "from __future__ import annotations",
    "from types import MappingProxyType",
    '__all__ = ["CASEFOLD_15_1"]',
    "CASEFOLD_15_1 = MappingProxyType({",
]
for codepoint in range(0x110000):
    char = chr(codepoint)
    if char.casefold() != char:
        rows.append(f"    {codepoint}: {ascii(char.casefold())},")
rows.extend(["})", ""])
target.write_text("\n".join(rows), encoding="utf-8")
