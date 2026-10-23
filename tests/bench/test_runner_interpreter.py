"""Real subprocess dependency/provenance checks, with no timed campaign.

RF01_TEST_PYTHON / RF01_TEST_SOURCE optionally select the native Pulse interpreter
and an existing historical checkout. These inputs affect only this smoke test.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

from bench.harness.campaign_schema import RUNNER_PYTHON_FLAGS


def test_real_runner_imports_selected_source_and_default_codec(tmp_path):
    python = os.environ.get("RF01_TEST_PYTHON", sys.executable)
    source = Path(os.environ.get("RF01_TEST_SOURCE", Path(__file__).resolve().parents[2])).resolve()
    poison = tmp_path / "okto_grafx"
    poison.mkdir()
    (poison / "__init__.py").write_text("raise RuntimeError('unsafe cwd/PYTHONPATH imported')")
    script = """
import json, pathlib, site, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / 'src'))
import numpy, psutil, okto_grafx
from okto_grafx import connect
root = pathlib.Path(sys.argv[1]).resolve()
assert pathlib.Path(okto_grafx.__file__).resolve().is_relative_to(root / 'src')
assert sys.flags.ignore_environment and sys.flags.safe_path and sys.flags.dont_write_bytecode
with connect(sys.argv[2], query_memory_budget_bytes=4096) as db:
    with db.begin('write') as tx:
        tx.execute('CREATE NODE TABLE N(id INT64,value INT64,PRIMARY KEY(id))')
    with db.begin('write') as tx:
        tx.execute('CREATE (:N {id:7,value:21})')
    result = db.execute('MATCH (n:N {id:$id}) RETURN n.value', {'id':7})
    assert result.rows == ((21,),)
    plan = [type(p).__name__ for p in db.explain('MATCH (n:N {id:7}) RETURN n.value').walk()]
    assert any('Index' in p or 'Seek' in p for p in plan), plan
print(json.dumps({'package_file':okto_grafx.__file__, 'numpy':numpy.__version__,
                  'psutil':psutil.__version__, 'user_site_enabled':site.ENABLE_USER_SITE,
                  'rows':result.rows}))
"""
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    output = subprocess.check_output([python, *RUNNER_PYTHON_FLAGS, "-c", script,
                                      str(source), str(tmp_path / "db")],
                                     cwd=tmp_path, env=env, text=True, timeout=30)
    receipt = json.loads(output)
    assert Path(receipt["package_file"]).resolve().is_relative_to(source / "src")
    assert receipt["rows"] == [[21]]
    assert receipt["numpy"] and receipt["psutil"]
