"""Malformed literals are diagnosed natively, before any statement effects."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.query.parser import parse
from okto_grafx.errors import GrafxParseError
from tools.tck_errors import compile_error


@pytest.mark.parametrize("expression,detail,field,reason", [
    ("9223372h54775808", "InvalidNumberLiteral", "number_literal", "invalid_numeric_literal"),
    ("-12abc", "InvalidNumberLiteral", "number_literal", "invalid_numeric_literal"),
    ("+1e", "InvalidNumberLiteral", "number_literal", "invalid_numeric_literal"),
    ("1.2e3xyz", "InvalidNumberLiteral", "number_literal", "invalid_numeric_literal"),
    ("9223372#54775808", "UnexpectedSyntax", "character", "unsupported_query_character"),
    ("{k1#k:1}", "UnexpectedSyntax", "character", "unsupported_query_character"),
])
def test_malformed_literal_refuses_before_write_and_keeps_transaction_usable(tmp_path, expression, detail, field, reason):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Before)")
            with pytest.raises(GrafxParseError) as failure:
                tx.execute("CREATE(:Never) RETURN " + expression)
            error = failure.value
            assert error.details["field"] == field
            assert error.details["reason"] == reason
            assert error.details["query_phase"] == "planning"
            assert (compile_error(error).type,compile_error(error).phase,compile_error(error).detail) == (
                "SyntaxError","compile time",detail)
            assert error.details["line"] == 1 and error.details["column"] > 1
            tx.execute("CREATE(:After)")
        assert db.execute("MATCH(n:Never) RETURN count(*)").rows == ((0,),)
        assert db.execute("MATCH(n) RETURN count(*)").rows == ((2,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("query", ["RETURN 12 abc", "RETURN 12/*gap*/abc", "RETURN 12 `abc`"])
def test_separated_name_is_not_reclassified_as_numeric_suffix(query):
    with pytest.raises(GrafxParseError) as failure:
        parse(query)
    assert compile_error(failure.value).detail != "InvalidNumberLiteral"


@pytest.mark.parametrize("query,expected", [
    ("RETURN 1AS a", ((1,),)),
    ("RETURN 1IN [1] AS a", ((True,),)),
    ("RETURN 1e3, -9223372036854775808, 0xFF,0o7", ((1000.0,-9223372036854775808,255,7),)),
    ("RETURN {`k1#k`:1}['k1#k']", ((1,),)),
])
def test_legitimate_numeric_and_quoted_key_forms_unchanged(query, expected):
    with connect(":memory:") as db:
        assert db.execute(query).rows == expected


@pytest.mark.parametrize("field,value,reason", [
    ("number_literal","12abc","invalid_numeric_literal"),
    ("character","#","unsupported_query_character"),
])
@pytest.mark.parametrize("change", ["missing_phase","execution_phase","wrong_field","wrong_reason"])
def test_mapping_requires_complete_native_evidence(field, value, reason, change):
    details = dict(field=field,value=value,reason=reason,query_phase="planning")
    if change == "missing_phase":
        details.pop("query_phase")
    elif change == "execution_phase":
        details["query_phase"] = "execution"
    elif change == "wrong_field":
        details["field"] = "unrelated"
    else:
        details["reason"] = "unrelated"
    assert compile_error(GrafxParseError("Invalid number or character", **details)).detail == "parse_error"
