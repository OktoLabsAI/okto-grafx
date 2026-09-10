# Offline HTML graph viewer (0.0.6)

[Documentation index](README.md) · [Graph capture](GRAPH_PROJECTIONS.md)

`render_html_snapshot` returns a standalone UTF-8-compatible HTML string containing
a directed SVG graph, identities, counts, provenance and optional table schema.
There are no scripts, executable property values, external fonts, CDN requests or
database connections. It is a static inspection picture, not a query console,
live health dashboard, authorization layer or editing interface.

```python
from okto_grafx import connect
from okto_grafx.projections import project_graph, ProjectionLimits
from okto_grafx.html_snapshot import render_html_snapshot, HtmlSnapshotLimits

with connect('./graph', read_only=True) as db:
    graph = project_graph(db, node_tables=('Item',), relationship_tables=(),
                          limits=ProjectionLimits(max_nodes=10000))
    schema = db.catalog.catalog

html = render_html_snapshot(graph, catalog=schema,
                            limits=HtmlSnapshotLimits(max_nodes=200, max_edges=500))
with open('snapshot.html', 'x', encoding='utf-8') as output:
    output.write(html)
```

The renderer writes no files; the example uses exclusive creation to avoid
overwriting an existing report. `catalog` is optional, an immutable public
`CatalogView`; it is labelled **separately captured metadata**, not asserted to be
the same instant as the graph. Tables/columns are HTML-escaped; source row property
payloads are not exported. Anyone with the file can see its identifiers/schema,
so treat it as exported data, not a security boundary.

A complete synthetic example is [examples/html_snapshot.py](../examples/html_snapshot.py):
run `python examples/html_snapshot.py new-snapshot.html` after installing this source
revision. It uses an in-memory graph and refuses to overwrite the output. Browser
acceptance is available in `tools/check_html_snapshot_browser.mjs` with externally
provided Playwright and Chrome; neither becomes a Grafx runtime dependency.

`HtmlSnapshotLimits`: `max_nodes=200`, `max_edges=500`, `max_bytes=2097152`,
`max_work=1000000`, positive integers <=2^31. Graph capture has independent
`ProjectionLimits` and never silently truncates. Rendering selects the first nodes
in capture order and up to the edge cap whose endpoints are shown. The output
explicitly states shown/total node and edge counts and `truncated: true/false`.
Byte/work exhaustion returns a typed failure, not half a successful HTML document.
Optional `cancellation` is checked cooperatively; no durable operation is involved.

Layout is deterministic circular placement, not a costly force simulation. Arrows
show direction and self-loops have curved paths. Physical parallel edges remain
distinct elements/titles/counts, although paths can overlap visually. Empty graphs
are valid. The picture can outlive the source handle and never refreshes itself.
