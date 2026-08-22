"""On-demand verification: the shapes a walk reports in (SPEC-M1 FR-11, AC-12).

A verifier that certifies damaged state is worse than no verifier, so everything here is written
to keep "checked and clean" distinguishable from "never looked".
"""

from __future__ import annotations

from okto_grafx.domain.verify.findings import (
    NOT_APPLICABLE,
    SCOPE_ALL,
    SCOPE_INDEXES,
    SCOPE_PAGES,
    SCOPE_RECORDS,
    VERIFICATION_SCOPES,
    FindingKind,
    FindingLocation,
    VerificationFinding,
    VerificationReport,
    scope_covers,
)
from okto_grafx.domain.verify.routing import (
    PAGE_REFUSAL_ROUTES,
    REDO_ANSWERS_IT,
    UNCLASSIFIED,
    route_page_refusal,
)

__all__ = [
    "NOT_APPLICABLE",
    "PAGE_REFUSAL_ROUTES",
    "REDO_ANSWERS_IT",
    "UNCLASSIFIED",
    "SCOPE_ALL",
    "SCOPE_INDEXES",
    "SCOPE_PAGES",
    "SCOPE_RECORDS",
    "VERIFICATION_SCOPES",
    "FindingKind",
    "FindingLocation",
    "VerificationFinding",
    "VerificationReport",
    "route_page_refusal",
    "scope_covers",
]
