"""Optional interop imports must not contaminate a dependency-free consumer."""

from pathlib import Path
import subprocess
import sys


def test_bare_source_typed_optional_refusals():
    source = str(Path(__file__).resolve().parents[2] / "src")
    code = """
import sys
sys.path.insert(0, SOURCE)
from okto_grafx import QueryResult, connect
from okto_grafx.projections import project_graph
from okto_grafx.tabular import to_pandas
from okto_grafx.parquet import write_parquet
from okto_grafx.errors import GrafxUnsupportedOperation
assert not any(n in sys.modules for n in ('numpy', 'pandas', 'pyarrow'))
with connect(':memory:', codec='pure', vector_math='pure', checksum='pure') as db:
    with db.begin() as tx:
        tx.execute('CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))')
        tx.execute('CREATE (:N {id:1})')
    graph = project_graph(db, node_tables=('N',))
    assert graph.pagerank().scores == (1.0,)
    for operation in (
        lambda: graph.pagerank(backend='numpy'),
        lambda: to_pandas(QueryResult(columns=('id',)), types=('INT64',)),
        lambda: write_parquet(QueryResult(columns=('id',)), 'unused.parquet', allowed_root='.', types=('INT64',)),
    ):
        try:
            operation()
        except GrafxUnsupportedOperation:
            pass
        else:
            raise AssertionError('selected absent dependency was not refused')
assert not any(n in sys.modules for n in ('numpy', 'pandas', 'pyarrow'))
""".replace("SOURCE", repr(source))
    completed = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
