"""Strict local text codecs, bounded iteration, rollback and source lifetime."""

from dataclasses import replace
import json

import pytest

from okto_grafx import connect, CancellationToken, Timestamp
from okto_grafx.domain.model.value import Uuid
from okto_grafx.errors import (
    GrafxError,
    GrafxQueryBudgetExceeded,
    GrafxQueryCancelled,
    GrafxUnsupportedOperation,
)
from okto_grafx.text_import import (
    TextImportLimits,
    read_csv_batches,
    read_jsonl_batches,
    import_csv,
    import_jsonl,
)


@pytest.mark.parametrize("kind", ["csv", "jsonl"])
def test_scalar_codecs_and_native_import_reopen(tmp_path, kind):
    columns = ("id", "flag", "num", "text", "blob", "uuid", "time")
    types = ("INT64", "BOOL", "DOUBLE", "STRING", "BYTES", "UUID", "TIMESTAMP")
    key = "00112233-4455-6677-8899-aabbccddeeff"
    path = tmp_path / f"input.{kind}"
    if kind == "csv":
        path.write_text(
            ",".join(columns)
            + f'\n1,true,2.5,"a,\nb",YQ==,{key},-1\n2,\\N,\\N,,\\N,\\N,\\N\n',
            encoding="utf-8",
            newline="\n",
        )
    else:
        rows = [
            (1, True, 2.5, "a,\nb", "YQ==", key, -1),
            (2, None, None, "", None, None, None),
        ]
        path.write_text(
            "\n".join(json.dumps(dict(zip(columns, row))) for row in rows),
            encoding="utf-8",
        )
    read, load = (
        (read_csv_batches, import_csv)
        if kind == "csv"
        else (read_jsonl_batches, import_jsonl)
    )
    batches = list(
        read(
            path,
            allowed_root=tmp_path,
            columns=columns,
            types=types,
            limits=TextImportLimits(batch_rows=1),
        )
    )
    assert len(batches) == 2
    assert batches[0][0]["blob"] == b"a"
    assert batches[0][0]["time"] == Timestamp(-1)
    assert batches[0][0]["uuid"] == Uuid(bytes.fromhex(key.replace("-", "")))
    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute(
                "CREATE NODE TABLE N(id INT64,flag BOOL,num DOUBLE,text STRING,blob BLOB,uuid UUID,time TIMESTAMP,PRIMARY KEY(id))"
            )
        with db.begin() as tx:
            assert (
                load(
                    tx,
                    "CREATE (:N {id:$id,flag:$flag,num:$num,text:$text,blob:$blob,uuid:$uuid,time:$time})",
                    path,
                    allowed_root=tmp_path,
                    columns=columns,
                    types=types,
                ).statements
                == 2
            )
    with connect(tmp_path / "db") as db:
        assert db.execute("MATCH (n:N) RETURN n.id,n.text ORDER BY n.id").rows == (
            (1, "a,\nb"),
            (2, ""),
        )


@pytest.mark.parametrize("kind", ["csv", "jsonl"])
@pytest.mark.parametrize("failure", ["duplicate", "malformed", "bound", "cancel"])
def test_late_failure_rolls_back_call_preserves_prior_staging(tmp_path, kind, failure):
    path = tmp_path / "input"
    tail = "99" if failure == "duplicate" else "2"
    if kind == "csv":
        content = f"id\n1\n{tail}\n" if failure != "malformed" else "id\n1\nbad\n"
    else:
        content = (
            f'{{"id":1}}\n{{"id":{tail}}}\n'
            if failure != "malformed"
            else '{"id":1}\n{"id":2,"id":3}\n'
        )
    path.write_text(content, encoding="utf-8")
    token = CancellationToken()
    if failure == "cancel":
        token.cancel()
    load = import_csv if kind == "csv" else import_jsonl
    with connect(":memory:") as db, db.begin() as tx:
        tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
        tx.execute("CREATE (:N {id:99})")
        with pytest.raises(GrafxError):
            load(
                tx,
                "CREATE (:N {id:$id})",
                path,
                allowed_root=tmp_path,
                columns=("id",),
                types=("INT64",),
                limits=TextImportLimits(
                    batch_rows=1, max_rows=1 if failure == "bound" else 10
                ),
                cancellation=token,
            )
        assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((99,),)


@pytest.mark.parametrize(
    "content",
    [
        '{"x":1,"x":2}',
        '{"y":1}',
        "{}",
        "[]",
        '{"x":[]}',
        '{"x":{}}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":1e999}',
        '{"x":true}',
        '{"x":9007199254740993}',
        "",
        "\n",
    ],
)
def test_jsonl_rejects_ambiguous_or_lossy_numbers_and_shapes(tmp_path, content):
    path = tmp_path / "input"
    path.write_text(content, encoding="utf-8")
    if content == "":
        assert (
            list(
                read_jsonl_batches(
                    path, allowed_root=tmp_path, columns=("x",), types=("DOUBLE",)
                )
            )
            == []
        )
    else:
        with pytest.raises(GrafxUnsupportedOperation):
            list(
                read_jsonl_batches(
                    path, allowed_root=tmp_path, columns=("x",), types=("DOUBLE",)
                )
            )


@pytest.mark.parametrize(
    "option,value",
    [
        ("max_rows", 1),
        ("max_batches", 1),
        ("max_file_bytes", 1),
        ("max_record_bytes", 2),
        ("max_field_bytes", 1),
        ("max_batch_bytes", 1),
        ("max_work", 1),
    ],
)
def test_csv_all_limits(tmp_path, option, value):
    path = tmp_path / "input"
    path.write_text("x\nhello\nworld\n", encoding="utf-8")
    limits = replace(TextImportLimits(batch_rows=1), **{option: value})
    with pytest.raises(GrafxQueryBudgetExceeded):
        list(
            read_csv_batches(
                path,
                allowed_root=tmp_path,
                columns=("x",),
                types=("STRING",),
                limits=limits,
                null_token="-",
            )
        )


def test_multiline_bound_and_early_close_and_mutation(tmp_path):
    path = tmp_path / "input"
    path.write_text('x\n"abc\ndef\nghi"\n', encoding="utf-8")
    with pytest.raises(GrafxQueryBudgetExceeded):
        list(
            read_csv_batches(
                path,
                allowed_root=tmp_path,
                columns=("x",),
                types=("STRING",),
                limits=TextImportLimits(max_record_bytes=9),
            )
        )
    path.write_text("x\n1\n2\n", encoding="utf-8")
    reader = read_csv_batches(
        path,
        allowed_root=tmp_path,
        columns=("x",),
        types=("INT64",),
        limits=TextImportLimits(batch_rows=1),
    )
    assert next(reader)[0]["x"] == 1
    reader.close()
    path.rename(tmp_path / "renamed")
    path = tmp_path / "renamed"
    reader = read_csv_batches(
        path,
        allowed_root=tmp_path,
        columns=("x",),
        types=("INT64",),
        limits=TextImportLimits(batch_rows=1),
    )
    next(reader)
    path.write_text("x\n1\n2\n3\n", encoding="utf-8")
    with pytest.raises(GrafxUnsupportedOperation):
        list(reader)


def test_path_policy_and_invalid_unicode(tmp_path):
    for name in ("../outside", "https://host/file", "//server/share/file", "file:ads"):
        with pytest.raises(GrafxUnsupportedOperation):
            list(
                read_csv_batches(
                    name, allowed_root=tmp_path, columns=("x",), types=("STRING",)
                )
            )
    path = tmp_path / "input"
    path.write_bytes(b"x\n\xff")
    with pytest.raises(GrafxUnsupportedOperation):
        list(
            read_csv_batches(
                path, allowed_root=tmp_path, columns=("x",), types=("STRING",)
            )
        )
    token = CancellationToken()
    token.cancel()
    with pytest.raises(GrafxQueryCancelled):
        list(
            read_jsonl_batches(
                path,
                allowed_root=tmp_path,
                columns=("x",),
                types=("STRING",),
                cancellation=token,
            )
        )


@pytest.mark.parametrize(
    "value,kind",
    [
        ("+1", "INT64"),
        ("01", "INT64"),
        ("1.0", "INT64"),
        ("9223372036854775808", "INT64"),
        ("TRUE", "BOOL"),
        ("1", "BOOL"),
        ("NaN", "DOUBLE"),
        ("Infinity", "DOUBLE"),
        ("1e9999", "DOUBLE"),
        ("notbase64!", "BYTES"),
        ("bad", "UUID"),
        ("00112233-4455-6677-8899-AABBCCDDEEFF", "UUID"),
        ("2026-01-01", "TIMESTAMP"),
    ],
)
def test_csv_scalar_refusals_are_localized(tmp_path, value, kind):
    path = tmp_path / "input"
    path.write_text(f"x\n{value}\n", encoding="utf-8")
    with pytest.raises(GrafxUnsupportedOperation) as failure:
        list(
            read_csv_batches(path, allowed_root=tmp_path, columns=("x",), types=(kind,))
        )
    assert failure.value.details["row"] == 1
    assert failure.value.details["column"] == "x"


@pytest.mark.parametrize("kind", ["csv", "jsonl"])
def test_midstream_cancellation_rolls_back_import(tmp_path, monkeypatch, kind):
    import okto_grafx.text_import as module

    path = tmp_path / "input"
    path.write_text(
        "id\n1\n2\n" if kind == "csv" else '{"id":1}\n{"id":2}\n', encoding="utf-8"
    )
    token = CancellationToken()
    original = module._value

    def observed(value, native_kind, text, row, column, limits):
        result = original(value, native_kind, text, row, column, limits)
        if row == 2:
            token.cancel()
        return result

    monkeypatch.setattr(module, "_value", observed)
    with connect(":memory:") as db, db.begin() as tx:
        tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
        tx.execute("CREATE (:N {id:99})")
        load = import_csv if kind == "csv" else import_jsonl
        with pytest.raises(GrafxQueryCancelled):
            load(
                tx,
                "CREATE (:N {id:$id})",
                path,
                allowed_root=tmp_path,
                columns=("id",),
                types=("INT64",),
                cancellation=token,
                limits=TextImportLimits(batch_rows=1),
            )
        assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((99,),)


def test_csv_quotes_null_token_and_header_policies(tmp_path):
    path = tmp_path / "input"
    path.write_text('x\n"\\N"\n""\n"a""b"\n', encoding="utf-8")
    rows = list(
        read_csv_batches(path, allowed_root=tmp_path, columns=("x",), types=("STRING",))
    )
    assert rows == [({"x": None}, {"x": ""}, {"x": 'a"b'})]
    for content in ("", "y\n1", "x,x\n1,2", "\ufeffx\n1", 'x\n"unterminated', "x\n\n"):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(GrafxUnsupportedOperation):
            list(
                read_csv_batches(
                    path, allowed_root=tmp_path, columns=("x",), types=("STRING",)
                )
            )
