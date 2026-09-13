"""Public DELETE plans expose owned canonical expressions, not arbitrary objects."""

from dataclasses import replace

import pytest

from okto_grafx.domain.query.ast import ListExpression, Literal, Subscript, Variable
from okto_grafx.domain.query.plan import DeleteEntities, SingleRow
from okto_grafx.engine.public_views import _query_plan_rebuild
from okto_grafx.errors import GrafxPlanError


def test_delete_target_snapshot_is_independent_and_rejects_forged_expressions():
    target = Subscript(ListExpression((Variable("n"),)),Literal(0))
    raw = DeleteEntities(child=SingleRow(),targets=(target,),detach=True)
    owned = _query_plan_rebuild(raw)
    assert owned == raw and owned is not raw
    assert owned.targets[0] is not target
    assert owned.targets[0].subject is not target.subject
    assert owned.details() == {"targets":"[n][0]","detach":True}
    with pytest.raises(GrafxPlanError):
        _query_plan_rebuild(replace(raw,targets=(object(),)))
