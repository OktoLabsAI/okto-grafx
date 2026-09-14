"""Fixtures shared by the public-API suite.

The one fixture here exists because W-01 gave the concrete coordinator a second, faster way to
serialise ONE section -- the participant section, whose name carries that coordinator instance's
own ``owner_id`` digest and which therefore excludes nobody outside this process. Tests whose
subject is the operating-system lock file of that section (how often it is opened, locked and
released, and the descriptor scope TXN-1 parks on it) need a coordinator that declines the
declaration, which is exactly what every coordinator without the private capability does.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from okto_grafx.adapters import coordination_local


@pytest.fixture
def undeclared_participant_section(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Make the coordinator decline W-01, so the participant section keeps its file lock.

    This is the shape every coordinator had before W-01 and the shape any coordinator that does
    not implement the private-section capability still has. The engine offers the name and takes
    no for an answer, so this is a production code path, not a test-only branch.
    """
    monkeypatch.setattr(
        coordination_local.LocalProcessCoordinator,
        "_declare_private_section",
        lambda self, name: False,
    )
    yield
