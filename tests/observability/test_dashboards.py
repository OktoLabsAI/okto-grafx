"""The dashboard coverage gate (SPEC-M1 OR-5, AC-14; SPEC-VEC OR-3, AC-10).

The board guideline behind OR-5 is blunt: a metric delivered without a panel is blocking debt.
That is only true if something checks it, so this file parses the two versioned Grafana
dashboards, pulls every metric name out of every PromQL expression and compares the result with
the catalog. The comparison is an equality, not a containment, because the failure of a panel
that queries a metric nobody declares is just as real as the failure of a metric nobody plots.

Histogram series carry a suffix a scraper adds, never the catalog: an expression naming
``..._bucket``, ``..._sum`` or ``..._count`` is normalised back to the metric it belongs to. The
order of that normalisation matters, because ``oktografx_query_rows_returned_count`` is itself a
catalog name (amendment A1), so a token is only stripped when it is not already a metric.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from okto_grafx.engine.metrics_catalog import metric, metric_names

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DASHBOARD_DIR: Path = PROJECT_ROOT / "dashboards"
DASHBOARDS: dict[str, Path] = {
    "oktografx-m1": DASHBOARD_DIR / "oktografx-m1.json",
    "oktografx-vector": DASHBOARD_DIR / "oktografx-vector.json",
}

METRIC_TOKEN = re.compile(r"oktografx_[a-z0-9_]+")
SERIES_SUFFIXES: tuple[str, ...] = ("_bucket", "_sum", "_count")
PROMQL_COMMENT = re.compile(r"#[^\n]*")


def _strip_comments(expression: str) -> str:
    """Remove PromQL comments, so a metric named in one is not counted as covered.

    A comment is not a query. Counting it would let a panel claim coverage of a metric it never
    plots, which is precisely the debt OR-5 exists to make visible.
    """
    return PROMQL_COMMENT.sub("", expression)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every panel, including the ones nested inside a collapsed row."""
    found: list[dict[str, Any]] = []
    for panel in dashboard.get("panels", []):
        found.append(panel)
        found.extend(panel.get("panels", []))
    return found


def _expressions(dashboard: dict[str, Any]) -> list[tuple[str, str]]:
    """Return every PromQL expression with the title of the panel it belongs to."""
    return [
        (panel.get("title", "<untitled>"), _strip_comments(target["expr"]))
        for panel in _panels(dashboard)
        for target in panel.get("targets", [])
        if isinstance(target.get("expr"), str)
    ]


def _normalise(token: str, known: frozenset[str]) -> str:
    """Map a series name back to the catalog metric it belongs to."""
    if token in known:
        return token
    for suffix in SERIES_SUFFIXES:
        if token.endswith(suffix) and token[: -len(suffix)] in known:
            return token[: -len(suffix)]
    return token


def _referenced(dashboard: dict[str, Any], known: frozenset[str]) -> set[str]:
    return {
        _normalise(token, known)
        for _, expression in _expressions(dashboard)
        for token in METRIC_TOKEN.findall(expression)
    }


ALL_DASHBOARDS: dict[str, dict[str, Any]] = {
    name: _load(path) for name, path in DASHBOARDS.items() if path.is_file()
}


def test_both_dashboards_are_versioned_in_the_repository() -> None:
    missing = [str(path) for path in DASHBOARDS.values() if not path.is_file()]
    assert missing == [], f"missing dashboard file(s): {missing}"


@pytest.mark.parametrize("name", sorted(DASHBOARDS))
def test_a_dashboard_is_importable_grafana_json(name: str) -> None:
    dashboard = ALL_DASHBOARDS[name]
    assert dashboard["uid"] == name
    assert isinstance(dashboard["schemaVersion"], int) and dashboard["schemaVersion"] >= 36
    assert dashboard["title"].startswith("Okto Grafx")
    assert dashboard["description"]
    assert "oktografx" in dashboard["tags"]
    assert dashboard["templating"] == {"list": []}
    assert dashboard["__inputs"][0]["pluginId"] == "prometheus"
    assert dashboard["panels"]


@pytest.mark.parametrize("name", sorted(DASHBOARDS))
def test_every_panel_is_titled_described_and_placed(name: str) -> None:
    identifiers: list[int] = []
    for panel in _panels(ALL_DASHBOARDS[name]):
        identifiers.append(panel["id"])
        assert panel["title"], f"{name} has a panel without a title"
        assert panel["gridPos"]["w"] > 0 and panel["gridPos"]["h"] > 0
        if panel["type"] == "row":
            continue
        assert panel["description"], f"{name}: panel {panel['title']!r} has no description"
        assert panel["targets"], f"{name}: panel {panel['title']!r} has no target"
        for target in panel["targets"]:
            assert target["expr"].strip()
            assert target["datasource"]["type"] == "prometheus"
            assert target["refId"]
    assert len(identifiers) == len(set(identifiers)), f"{name} reuses a panel id"


@pytest.mark.parametrize("name", sorted(DASHBOARDS))
def test_no_panel_overlaps_another(name: str) -> None:
    occupied: set[tuple[int, int]] = set()
    for panel in _panels(ALL_DASHBOARDS[name]):
        position = panel["gridPos"]
        for x in range(position["x"], position["x"] + position["w"]):
            for y in range(position["y"], position["y"] + position["h"]):
                assert (x, y) not in occupied, f"{name}: {panel['title']!r} overlaps another panel"
                occupied.add((x, y))
        assert position["x"] + position["w"] <= 24


def test_the_dashboards_reference_every_metric_of_the_catalog_and_nothing_else() -> None:
    known = metric_names()
    referenced: set[str] = set()
    for dashboard in ALL_DASHBOARDS.values():
        referenced |= _referenced(dashboard, known)
    missing = sorted(known - referenced)
    unknown = sorted(referenced - known)
    assert missing == [], (
        f"{len(missing)} metric(s) have no panel, which OR-5 makes blocking debt: {missing}"
    )
    assert unknown == [], (
        f"{len(unknown)} panel expression(s) query a metric that is not in the catalog: {unknown}"
    )


def test_the_m1_dashboard_covers_the_engine_metrics() -> None:
    known = metric_names()
    referenced = _referenced(ALL_DASHBOARDS["oktografx-m1"], known)
    expected = {name for name in known if not name.startswith("oktografx_vector_")}
    assert expected <= referenced, sorted(expected - referenced)


def test_the_vector_dashboard_covers_every_vector_metric() -> None:
    known = metric_names()
    referenced = _referenced(ALL_DASHBOARDS["oktografx-vector"], known)
    expected = {name for name in known if name.startswith("oktografx_vector_")}
    assert expected <= referenced, sorted(expected - referenced)


def test_a_histogram_is_queried_through_a_series_a_scraper_actually_exposes() -> None:
    known = metric_names()
    for name, dashboard in ALL_DASHBOARDS.items():
        for title, expression in _expressions(dashboard):
            for token in METRIC_TOKEN.findall(expression):
                base = _normalise(token, known)
                if metric(base).kind.value != "histogram":
                    continue
                assert token != base, (
                    f"{name}: panel {title!r} queries the histogram {base!r} directly; a "
                    f"histogram is exposed as _bucket, _sum and _count"
                )


def test_the_normalisation_does_not_eat_a_metric_whose_own_name_ends_in_count() -> None:
    # Amendment A1 froze oktografx_query_rows_returned_count, so the suffix stripping must
    # recognise the catalog name before it tries to strip anything.
    known = metric_names()
    assert _normalise("oktografx_query_rows_returned_count", known) == (
        "oktografx_query_rows_returned_count"
    )
    assert _normalise("oktografx_query_rows_returned_count_bucket", known) == (
        "oktografx_query_rows_returned_count"
    )
    assert _normalise("oktografx_query_rows_returned_count_count", known) == (
        "oktografx_query_rows_returned_count"
    )
    assert _normalise("oktografx_not_a_metric_total", known) == "oktografx_not_a_metric_total"


def test_the_gate_would_notice_a_metric_without_a_panel() -> None:
    # A coverage test that cannot fail proves nothing.
    known = metric_names()
    referenced = set()
    for dashboard in ALL_DASHBOARDS.values():
        referenced |= _referenced(dashboard, known)
    pretended = known | {"oktografx_future_metric_total"}
    assert sorted(pretended - referenced) == ["oktografx_future_metric_total"]


def test_a_metric_named_only_in_a_comment_does_not_count_as_covered() -> None:
    # A comment is not a query: counting it would let a panel claim coverage it does not have.
    known = metric_names()
    commented = {
        "title": "probe",
        "panels": [
            {
                "title": "probe",
                "type": "timeseries",
                "targets": [
                    {
                        "expr": (
                            "sum(rate(oktografx_write_conflicts_total[5m]))"
                            "\n# oktografx_brand_new_total is not plotted here"
                        )
                    }
                ],
            }
        ],
    }
    assert _referenced(commented, known) == {"oktografx_write_conflicts_total"}
    assert _strip_comments("a # b\nc") == "a \nc"


def test_no_panel_expression_of_the_shipped_dashboards_hides_behind_a_comment() -> None:
    for name, dashboard in ALL_DASHBOARDS.items():
        for panel in _panels(dashboard):
            for target in panel.get("targets", []):
                expression = target.get("expr")
                if isinstance(expression, str):
                    assert "#" not in expression, f"{name}: {panel['title']!r} comments its query"
