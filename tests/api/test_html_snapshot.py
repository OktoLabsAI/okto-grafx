"""Offline renderer: bounded, escaped and detached from storage."""

from dataclasses import replace
from html.parser import HTMLParser
import pytest
from tests.api.test_projection_algorithms import picture
from okto_grafx import CancellationToken, connect
from okto_grafx.html_snapshot import render_html_snapshot, HtmlSnapshotLimits
from okto_grafx.projections import ProjectionNode, project_graph
from okto_grafx.errors import GrafxQueryBudgetExceeded, GrafxQueryCancelled


def test_truncation_and_escaping():
    graph = picture(3, [(0, 0), (0, 1), (1, 2)])
    graph = replace(
        graph,
        nodes=(ProjectionNode('<script>alert("x")</script>', 0), *graph.nodes[1:]),
    )
    html = render_html_snapshot(
        graph, limits=HtmlSnapshotLimits(max_nodes=2, max_edges=1)
    )
    assert "Nodes 2/3; edges 1/3; truncated: true" in html
    assert "&lt;script&gt;" in html and "<script>" not in html

    class Tags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            assert tag not in ("script", "iframe", "object", "embed")
            assert all(not key.startswith("on") for key, value in attrs)

    Tags().feed(html)


def test_native_capture_and_schema(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1})")
        graph = project_graph(db, node_tables=("N",))
        catalog = db.catalog.catalog
    html = render_html_snapshot(graph, catalog=catalog)
    assert "truncated: false" in html and "id: INT64" in html
    assert render_html_snapshot(graph, catalog=catalog) == html


def test_limits_empty_and_cancellation():
    graph = picture(3, [])
    assert "Nodes 0/0" in render_html_snapshot(picture(0, []))
    for limits in [HtmlSnapshotLimits(max_bytes=20), HtmlSnapshotLimits(max_work=1)]:
        with pytest.raises(GrafxQueryBudgetExceeded):
            render_html_snapshot(graph, limits=limits)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(GrafxQueryCancelled):
        render_html_snapshot(graph, cancellation=token)
