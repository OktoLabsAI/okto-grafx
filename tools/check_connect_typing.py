"""Opt-in static consumer check; requires mypy, never a runtime dependency."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "MYPYPATH": str(root / "src")}
    positive = """
from okto_grafx import ConnectOptions, PortRegistry, connect
options: ConnectOptions = {"page_size": 8192, "descriptor_revalidation": "generation"}
db = connect(":memory:", **options)
def custom(registry: PortRegistry) -> None:
    connect(":memory:", registry=registry, read_only=False, max_result_rows=None)
"""
    negative = """
from okto_grafx import connect
connect(":memory:", typo_page_size=8192)
connect(":memory:", page_size="8192")
connect(":memory:", descriptor_revalidation="unsafe")
"""
    for source, expected in ((positive, 0), (negative, 3)):
        result = subprocess.run([sys.executable, "-m", "mypy", "--python-version=3.11",
                                 "--follow-imports=silent", "--ignore-missing-imports", "-c", source],
                                cwd=root, env=env, text=True, capture_output=True, timeout=60)
        print(result.stdout, end="")
        assert result.returncode == (1 if expected else 0), result.stderr
        assert result.stdout.count("error:") == expected, result.stdout
    print("ConnectOptions static consumer contract PASS")


if __name__ == "__main__":
    main()
