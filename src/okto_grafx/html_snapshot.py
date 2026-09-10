"""Offline, script-free graph pictures; no live database or browser authority."""

from dataclasses import dataclass
from html import escape
import math

from okto_grafx.domain.query.control import CancellationToken
from okto_grafx.engine.public_views import CatalogView
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded
from okto_grafx.projections import GraphProjection, _Work

__all__ = ["HtmlSnapshotLimits", "render_html_snapshot"]


@dataclass(frozen=True, slots=True)
class HtmlSnapshotLimits:
    """Display limits; source capture uses independent ProjectionLimits."""

    max_nodes: int = 200
    max_edges: int = 500
    max_bytes: int = 2 * 1024 * 1024
    max_work: int = 1_000_000

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            if (
                type(getattr(self, name)) is not int
                or not 0 < getattr(self, name) <= 2**31
            ):
                raise GrafxConfigurationError(
                    "Invalid HTML snapshot limit.", field=name
                )


def render_html_snapshot(
    graph: GraphProjection,
    *,
    catalog: CatalogView | None = None,
    limits: HtmlSnapshotLimits = HtmlSnapshotLimits(),
    cancellation: CancellationToken | None = None,
) -> str:
    """Render detached identities/edges and optional separately captured schema; never write files."""
    if type(graph) is not GraphProjection or type(limits) is not HtmlSnapshotLimits:
        raise GrafxConfigurationError(
            "Expected GraphProjection and HtmlSnapshotLimits."
        )
    if catalog is not None and type(catalog) is not CatalogView:
        raise GrafxConfigurationError(
            "Expected immutable CatalogView.", field="catalog"
        )
    work = _Work(limits.max_work, cancellation)
    parts, size = [], 0

    def append(text):
        """Charge bounded encoded HTML before retaining the fragment."""
        nonlocal size
        work.step()
        size += len(text.encode("utf-8"))
        if size > limits.max_bytes:
            raise GrafxQueryBudgetExceeded(
                "HTML output bound exceeded.", resource="html_bytes"
            )
        parts.append(text)

    def label(text):
        """Bound and escape a text label for safe HTML consumption."""
        # Check before escaping to avoid an unbounded temporary expansion.
        if len(text) > limits.max_bytes // 6:
            raise GrafxQueryBudgetExceeded(
                "HTML label bound exceeded.", resource="html_bytes"
            )
        return escape(text, quote=True)

    nodes = graph.nodes[: limits.max_nodes]
    append(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; '
        'style-src &#39;unsafe-inline&#39;">'
        "<title>Okto Grafx snapshot</title><body><h1>Okto Grafx snapshot</h1>"
    )
    append(
        f"<p>Database {graph.database_uuid.hex()} &mdash; snapshot LSN {graph.snapshot_lsn}. "
        "Detached, read-only picture. No live updates.</p>"
    )
    append(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000" '
        'role="img" aria-label="Directed graph" style="max-width:900px">'
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" '
        'refX="14" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="#777"/></marker></defs>'
    )

    def position(i):
        """Place a detached node on the bounded static circular layout."""
        return (
            500 + 400 * math.cos(i * 2 * math.pi / max(1, len(nodes))),
            500 + 400 * math.sin(i * 2 * math.pi / max(1, len(nodes))),
        )

    shown = 0
    for edge in graph.edges:
        work.step()
        if shown >= limits.max_edges:
            break
        if not (
            0 <= edge.source < len(graph.nodes) and 0 <= edge.target < len(graph.nodes)
        ):
            raise GrafxConfigurationError("Invalid projection endpoint.", field="edges")
        if edge.source >= len(nodes) or edge.target >= len(nodes):
            continue
        x1, y1 = position(edge.source)
        x2, y2 = position(edge.target)
        # Parallel physical edges retain separate titles/counts, even when paths overlap.
        curve = (
            f"C{x1 - 40:.2f},{y1 - 60:.2f} {x1 + 40:.2f},{y1 - 60:.2f}"
            if edge.source == edge.target
            else f"Q{(x1 + x2) / 2 + 25:.2f},{(y1 + y2) / 2 - 25:.2f}"
        )
        append(
            f'<path d="M{x1:.2f},{y1:.2f} {curve} '
            f'{x2:.2f},{y2:.2f}" fill="none" stroke="#777" marker-end="url(#arrow)">'
            f"<title>{label(edge.table)} #{edge.record_id}</title></path>"
        )
        shown += 1
    for i, node in enumerate(nodes):
        x, y = position(i)
        append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="6" fill="#2369c5"/>'
            f'<text x="{x + 9:.2f}" y="{y:.2f}" font-size="12">'
            f"{label(node.table)} #{node.record_id}</text>"
        )
    truncated = len(nodes) < len(graph.nodes) or shown < len(graph.edges)
    append(
        f"</svg><p>Nodes {len(nodes)}/{len(graph.nodes)}; edges {shown}/{len(graph.edges)}; "
        f"truncated: {str(truncated).lower()}.</p>"
    )
    if catalog is not None:
        append("<h2>Schema (separately captured metadata)</h2><ul>")
        for table in catalog.tables():
            append(f"<li>{label(table.name)} ({label(table.kind)})<ul>")
            for column in table.columns:
                append(
                    f"<li>{label(column.name)}: {column.type.name}; nullable={column.nullable}</li>"
                )
            append("</ul></li>")
        append("</ul>")
    append("</body></html>")
    return "".join(parts)
