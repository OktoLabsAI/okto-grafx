"""Decimal parameter keys are opaque strings, never coerced to integers."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


def test_numeric_parameters_keep_distinct_names_and_bind_in_native_writes(tmp_path):
    with connect(tmp_path / "db") as db:
        assert db.execute("RETURN $1, $01, $0, $`1name`", {"1":2, "01":3, "0":4, "1name":5}).rows == ((2,3,4,5),)
        with db.begin("write") as tx:
            tx.execute("CREATE(a {v:$1})-[:R {v:$2}]->(b {v:$3})", {"1":"start", "2":2, "3":"end"})
        assert db.execute("MATCH(a)-[r:R]->(b) WHERE a.v=$1 AND r.v=$2 RETURN b.v", {"1":"start", "2":2}).rows == (("end",),)
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("CREATE(a {v:$1})-[:R {v:$2}]->(b)", {"1":"bad"})
        assert db.execute("MATCH(n) RETURN count(n)").rows == ((2,),)
        assert db.verify("all").findings == ()
