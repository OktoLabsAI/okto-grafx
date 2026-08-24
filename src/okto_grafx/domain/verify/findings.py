"""What a verification walk found, and where (CONTRACT.md section 8.6, SPEC-M1 FR-11, AC-12).

A finding is a statement about the database with a location precise enough to act on: which file,
which page, which slot, which sequence number, which index. FR-11 asks for a machine-readable
report an operator or a CI job can read, and section 8.6 fixes the three parts of a finding --
``kind``, ``location`` and an en-US ``detail``.

The report carries counts as well as findings, and they are load-bearing rather than decorative.
"No findings" and "nothing was checked" are the same empty tuple, and amendment A75.2 is about
exactly that confusion: a count of zero and a failure to count are the same value in most
encodings and opposite facts. A clean database returns an empty ``findings`` AND non-zero counts,
so a caller can tell a verification that passed from one that never ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ids import NO_LSN, Lsn

__all__ = [
    "VERIFICATION_FINDING_KINDS",
    "VERIFICATION_SCOPES",
    "SCOPE_ALL",
    "SCOPE_INDEXES",
    "SCOPE_PAGES",
    "SCOPE_RECORDS",
    "NOT_APPLICABLE",
    "FindingKind",
    "FindingLocation",
    "VerificationFinding",
    "VerificationReport",
    "scope_covers",
]

SCOPE_PAGES: str = "pages"
"""Page headers and page checksums, read straight off the device."""

SCOPE_RECORDS: str = "records"
"""Heap records, their version chains, and the table structure that reaches them."""

SCOPE_INDEXES: str = "indexes"
"""Secondary index entries against the heap they point at."""

SCOPE_ALL: str = "all"
"""Every scope above, in one walk."""

VERIFICATION_SCOPES: tuple[str, ...] = (
    SCOPE_PAGES,
    SCOPE_RECORDS,
    SCOPE_INDEXES,
    SCOPE_ALL,
)
"""The closed set CONTRACT.md section 8.6 gives ``Verifier.verify``."""

NOT_APPLICABLE: int = -1
"""What a location field holds when the finding is not about a page or a slot."""


class FindingKind:
    """The vocabulary of verification findings. A closed set, so a caller can switch on it."""

    PAGE_CHECKSUM: str = "page_checksum"
    PAGE_TYPE: str = "page_type"
    PAGE_TORN: str = "page_torn"
    PAGE_UNWRITTEN: str = "page_unwritten"
    PAGE_DESCRIPTOR_MISSING: str = "page_descriptor_missing"
    FILE_HEADER: str = "file_header"
    FILE_UNREADABLE: str = "file_unreadable"
    RECORD_HEADER: str = "record_header"
    RECORD_LENGTH: str = "record_length"
    RECORD_LIFETIME: str = "record_lifetime"
    VERSION_CHAIN: str = "version_chain"
    ORPHAN_PAGE: str = "orphan_page"
    EXTENT_DRIFT: str = "extent_drift"
    TABLE_UNREADABLE: str = "table_unreadable"
    CATALOG_UNREADABLE: str = "catalog_unreadable"
    INDEX_UNREADABLE: str = "index_unreadable"
    INDEX_ENTRY_MALFORMED: str = "index_entry_malformed"
    INDEX_ENTRY_UNRESOLVED: str = "index_entry_unresolved"
    INDEX_KEY_MISMATCH: str = "index_key_mismatch"
    INDEX_ENTRY_MISSING: str = "index_entry_missing"
    LOG_DAMAGE: str = "log_damage"


VERIFICATION_FINDING_KINDS: frozenset[str] = frozenset(
    (
        FindingKind.PAGE_CHECKSUM,
        FindingKind.PAGE_TYPE,
        FindingKind.PAGE_TORN,
        FindingKind.PAGE_UNWRITTEN,
        FindingKind.PAGE_DESCRIPTOR_MISSING,
        FindingKind.FILE_HEADER,
        FindingKind.FILE_UNREADABLE,
        FindingKind.RECORD_HEADER,
        FindingKind.RECORD_LENGTH,
        FindingKind.RECORD_LIFETIME,
        FindingKind.VERSION_CHAIN,
        FindingKind.ORPHAN_PAGE,
        FindingKind.EXTENT_DRIFT,
        FindingKind.TABLE_UNREADABLE,
        FindingKind.CATALOG_UNREADABLE,
        FindingKind.INDEX_UNREADABLE,
        FindingKind.INDEX_ENTRY_MALFORMED,
        FindingKind.INDEX_ENTRY_UNRESOLVED,
        FindingKind.INDEX_KEY_MISMATCH,
        FindingKind.INDEX_ENTRY_MISSING,
        FindingKind.LOG_DAMAGE,
    )
)
"""The exact machine-readable finding vocabulary accepted by verification reports."""


@dataclass(frozen=True, slots=True)
class FindingLocation:
    """Where a finding is, with the five coordinates section 8.6 names."""

    file: str = ""
    page: int = NOT_APPLICABLE
    slot: int = NOT_APPLICABLE
    lsn: Lsn = NO_LSN
    index: str = ""

    def __post_init__(self) -> None:
        """Refuse a location whose fields are not the shapes a report can print."""
        for name, value in (("file", self.file), ("index", self.index)):
            if not isinstance(value, str):
                raise GrafxConfigurationError(
                    f"A finding location {name} must be a string; got {type(value).__name__}.",
                    field=name,
                    value=type(value).__name__,
                )
        for name, value in (
            ("page", self.page),
            ("slot", self.slot),
            ("lsn", self.lsn),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise GrafxConfigurationError(
                    f"A finding location {name} must be an integer; got {type(value).__name__}.",
                    field=name,
                    value=repr(value),
                )

    def describe(self) -> str:
        """Return the location as one en-US phrase, for a message that has to read well."""
        parts: list[str] = []
        if self.file:
            parts.append(f"file {self.file!r}")
        if self.index:
            parts.append(f"index {self.index!r}")
        if self.page != NOT_APPLICABLE:
            parts.append(f"page {self.page}")
        if self.slot != NOT_APPLICABLE:
            parts.append(f"slot {self.slot}")
        if self.lsn != NO_LSN:
            parts.append(f"sequence number {self.lsn}")
        return ", ".join(parts) if parts else "the database"


@dataclass(frozen=True, slots=True)
class VerificationFinding:
    """One disagreement the walk met, with its location and an en-US explanation."""

    kind: str
    location: FindingLocation
    detail: str

    def __post_init__(self) -> None:
        """Refuse a finding that names nothing, since a finding exists to be read."""
        if not isinstance(self.kind, str) or not self.kind:
            raise GrafxConfigurationError(
                "A verification finding must carry a kind.",
                field="kind",
                value=repr(self.kind),
            )
        if not isinstance(self.location, FindingLocation):
            raise GrafxConfigurationError(
                f"A verification finding needs a FindingLocation; got "
                f"{type(self.location).__name__}.",
                field="location",
                value=type(self.location).__name__,
            )
        if not isinstance(self.detail, str) or not self.detail:
            raise GrafxConfigurationError(
                f"The {self.kind!r} finding must carry a detail a reader can act on.",
                field="detail",
                value=repr(self.detail),
            )


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """The result of one verification walk: what was checked, and what disagreed."""

    scope: str
    findings: tuple[VerificationFinding, ...] = field(default_factory=tuple)
    pages_checked: int = 0
    records_checked: int = 0
    index_entries_checked: int = 0
    files_checked: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Refuse a report whose scope is not one of the four words section 8.6 freezes."""
        if self.scope not in VERIFICATION_SCOPES:
            raise GrafxConfigurationError(
                f"A verification scope is one of {VERIFICATION_SCOPES}; got {self.scope!r}.",
                field="scope",
                value=repr(self.scope),
            )
        for name, value in (
            ("pages_checked", self.pages_checked),
            ("records_checked", self.records_checked),
            ("index_entries_checked", self.index_entries_checked),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GrafxConfigurationError(
                    f"The {name} of a verification report must be a non-negative integer; "
                    f"got {value!r}.",
                    field=name,
                    value=repr(value),
                )

    @property
    def clean(self) -> bool:
        """Return True when the walk ran and found nothing to report.

        Both halves are asserted, and the second is the one that matters: a walk that checked
        nothing has an empty findings tuple too, and calling that clean is how a verifier comes to
        certify a database it never looked at (A75.2).
        """
        if self.findings:
            return False
        return bool(
            self.pages_checked or self.records_checked or self.index_entries_checked
        )

    def findings_at(
        self, kind: str, file: str, page: int
    ) -> tuple[VerificationFinding, ...]:
        """Return every finding of one kind about one page of one file.

        A caller checking that a page was reported ONCE needs to ask about that page rather than
        about the whole walk, because two damaged pages and one page reported twice are the same
        number under ``findings_of``.
        """
        return tuple(
            finding
            for finding in self.findings
            if finding.kind == kind
            and finding.location.file == file
            and finding.location.page == page
        )

    def findings_of(self, kind: str) -> tuple[VerificationFinding, ...]:
        """Return every finding of one kind, so a caller need not filter by hand."""
        return tuple(finding for finding in self.findings if finding.kind == kind)


def scope_covers(scope: str, wanted: str) -> bool:
    """Return whether a requested scope includes one particular walk."""
    if scope not in VERIFICATION_SCOPES:
        raise GrafxConfigurationError(
            f"A verification scope is one of {VERIFICATION_SCOPES}; got {scope!r}.",
            field="scope",
            value=repr(scope),
        )
    return scope == SCOPE_ALL or scope == wanted
