"""Abrupt process cuts around one typed-collection DDL/DML commit."""

import os
import sys

from okto_grafx import connect, DateValue, DecimalValue
from okto_grafx.engine.database import Transaction
from okto_grafx.engine.txn_manager import TransactionManager

# Keep the crash process independent of pytest and the parent's tests package path.
DDL = "CREATE NODE TABLE N(id INT64, xs LIST<INT64 NOT NULL>, amounts MAP<DECIMAL(12,4)>, pair ARRAY<STRING,2>, info STRUCT<day:DATE NOT NULL,meta:MAP<ANY>>,PRIMARY KEY(id))"
CREATE = "CREATE(n:N {id:$id,xs:$xs,amounts:$amounts,pair:$pair,info:$info})"
INPUT = {"xs": [1, 2], "amounts": {"price": DecimalValue(125, 3, 2)}, "pair": ["a", None],
         "info": {"day": DateValue(2024, 2, 29), "meta": {"nested": [DecimalValue(1, 1, 0), True]}}}

path, codec, phase = sys.argv[1:]
with connect(path, codec=codec) as db:
    tx = db.begin()
    tx.execute(DDL)
    tx.execute(CREATE, {"id": 1, **INPUT})
    if phase == "before_commit":
        os._exit(71)
    assert phase == "before_apply"
    original = TransactionManager._apply_images

    def stop_apply(manager, images):
        if tx._context.state.value == "committed":
            os._exit(73)
        return original(manager, images)

    TransactionManager._apply_images = stop_apply
    Transaction.commit(tx)
raise AssertionError("Crash point not reached")
