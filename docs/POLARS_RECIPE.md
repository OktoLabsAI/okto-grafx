# Polars consumer recipe

Requires `[polars]` and an existing `Document(id INT64,title STRING)` table.
`PolarsFrame` carries required metadata explicitly; keep it with the eager frame.

```python
from okto_grafx import QueryResult
from okto_grafx.polars import to_polars, import_polars

frame = to_polars(QueryResult(columns=("id", "title"), rows=((5, "Polars"),)),
                   types=("INT64", "STRING"))
with db.begin() as tx:
    polars_report = import_polars(tx, "CREATE (:Document {id:$id,title:$title})",
                                 frame, types=("INT64", "STRING"))
assert polars_report.statements == 1
```

[Types, options, limits and ownership](TABULAR_AND_PARQUET.md) · [API reference](API_REFERENCE.md)
