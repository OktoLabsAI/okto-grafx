"""The optional accelerated CRC-32C adapter (D2, and the commit profile that motivated it).

A profile of the durable commit put 59.4% of the whole commit in the pure-Python checksum: a
byte-at-a-time table reduction runs at about 1.3 MiB/s, and a native CRC-32C does the same work
roughly 184 times faster. D2 allows exactly this -- the core stays pure Python, the native
implementation lives behind the port -- and G3 already sanctions the shape: a third-party
accelerator lives in the ``[accel]`` extra and is imported in its adapter and nowhere else,
which is what :mod:`okto_grafx.adapters.vectormath_numpy` does for numpy.

Importing this module without a provider raises ``ImportError``. That is the intended shape:
the engine runs the pure reference when the accelerator is absent, and the whole suite passes
with nothing installed at all.

**This adapter is never the authority.** ``zlib.crc32`` is NOT a substitute -- it is CRC-32
(IEEE), a different polynomial, and it was used in the profile only as a cost-shape proxy. A
provider that computes anything other than Castagnoli CRC-32C does not fail loudly when it is
installed: it reports corruption on healthy pages, and the caller is then told to quarantine
data that was never damaged. So this adapter proves byte-identity against
:func:`okto_grafx.domain.page.checksum.crc32c_reference` over the full acceptance corpus BEFORE
it can be installed, and :func:`install_crc32c` proves it again at the door. A provider that
disagrees anywhere is refused, and refusing is total.

Two providers are accepted, in this order, because both ship real CRC-32C and neither is
universally available:

* ``google_crc32c`` -- ``extend(crc, data)``, whose ``crc`` is the previous RESULT, which is this
  build's chaining convention;
* ``crc32c`` -- ``crc32c(data, value)``, same convention with the arguments the other way round.

Both are adapted to ``(data, crc) -> int`` here, which is the only signature the domain knows.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.page.checksum import (
    CRC32C_ACCEPTANCE_CORPUS,
    CRC32C_INITIAL,
    CRC32C_KNOWN_ANSWERS,
    crc32c_reference,
    install_crc32c,
)

__all__ = [
    "NATIVE_ADAPTER_NAME",
    "CRC32C_PROVIDERS",
    "NativeCrc32c",
    "load_provider",
]

NATIVE_ADAPTER_NAME: str = "native"
"""The bounded label this adapter reports, matching the ``native`` selector of DatabaseConfig."""

CRC32C_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("google_crc32c", "extend"),
    ("crc32c", "crc32c"),
)
"""The provider modules this adapter accepts, and the callable it takes from each, in order.

Declared as data so the list is inspectable and so a test can say which provider it measured.
It is a closed list on purpose (A54.1): "whatever module happens to be importable" is an
open-world question whose answer the author controls by choosing the name, and a checksum is not
somewhere to be clever about provenance.
"""


def _adapt(module_name: str, attribute: str, provider: object) -> Callable[[bytes, int], int]:
    """Return the provider as ``(data, crc) -> int``, whatever argument order it was written in.

    The adaptation is per provider and explicit. Guessing the order from a trial call would
    "work" for the seed 0 that a page checksum of one contiguous range always uses, and be
    silently wrong for every chained computation -- the ledger, the catalog and the commit state
    all chain.
    """
    if not callable(provider):
        raise ImportError(
            f"{module_name}.{attribute} is not callable, so it cannot compute a checksum."
        )
    if module_name == "google_crc32c":
        return lambda data, crc=CRC32C_INITIAL: int(provider(crc, data))
    return lambda data, crc=CRC32C_INITIAL: int(provider(data, crc))


def load_provider() -> tuple[str, Callable[[bytes, int], int]]:
    """Return the name and the adapted callable of the first available provider.

    Raises ``ImportError`` naming every provider it looked for when none is installed, so the
    remedy is in the failure rather than in documentation somebody has to find.
    """
    for module_name, attribute in CRC32C_PROVIDERS:
        try:
            module = import_module(module_name)
        except ImportError:
            continue
        function = getattr(module, attribute, None)
        if function is None:
            continue
        return module_name, _adapt(module_name, attribute, function)
    wanted = ", ".join(name for name, _attribute in CRC32C_PROVIDERS)
    raise ImportError(
        f"No native CRC-32C provider is installed. Install the [accel] extra, which brings one "
        f"of: {wanted}."
    )


class NativeCrc32c:
    """A native CRC-32C, proved byte-identical to the pure reference before it is usable."""

    __slots__ = ("_provider", "_provider_name")

    def __init__(
        self,
        provider: Callable[[bytes, int], int] | None = None,
        *,
        provider_name: str | None = None,
    ) -> None:
        """Adopt a provider, or find one, and refuse it unless it reproduces the reference.

        ``provider`` is a declared seam and not test scaffolding smuggled into production: a
        composition that already holds a native CRC-32C -- from a host application, from a
        vendored extension -- passes it here rather than being told to install a package it
        already has. The default path takes it from :func:`load_provider`, so production
        behaviour with no argument is exactly what it would be without the parameter (A81).

        The check runs HERE as well as inside :func:`install_crc32c`, and that is not redundant
        defence: this one makes a wrong provider fail where it is CONSTRUCTED, naming the
        provider, while the installer's protects the domain from any candidate at all, including
        one nobody built through this class. Break either and the other still refuses, which is
        the case A67 asks about -- and for a value that silently converts healthy pages into
        quarantined ones, that is the side to err on (LESSONS L17).
        """
        if provider is None:
            resolved_name, resolved = load_provider()
        else:
            if not callable(provider):
                raise GrafxConfigurationError(
                    f"A CRC-32C provider must be callable; got {type(provider).__name__}.",
                    field="provider",
                    value=type(provider).__name__,
                )
            resolved_name, resolved = provider_name or "supplied", provider
        self._provider: Callable[[bytes, int], int] = resolved
        self._provider_name: str = resolved_name
        self._require_agreement()

    def _require_agreement(self) -> None:
        """Refuse a provider that is not computing Castagnoli CRC-32C, before anything uses it.

        The published vectors come first, so a wrong POLYNOMIAL is named as such rather than
        reported as a disagreement with our own reference -- a provider computing CRC-32 (IEEE)
        is not a slightly different CRC-32C, and the message should not suggest that it is.

        Then the WHOLE acceptance corpus, not a handful of short vectors. An earlier version of
        this checked three: the empty string, the published nine bytes, and thirty-two zeros. A
        provider wrong only on tail lengths congruent to five modulo eight passed all three and
        was caught one door later by the installer -- which is precisely the half-acceptance
        this class exists to prevent, arriving inside the class itself. The inputs a short check
        uses are the inputs every implementation already gets right.
        """
        for payload, answer in CRC32C_KNOWN_ANSWERS:
            produced = self._provider(payload, CRC32C_INITIAL)
            if produced != answer:
                raise GrafxConfigurationError(
                    f"The native CRC-32C provider {self._provider_name!r} answered "
                    f"{produced:#010x} for a published vector whose CRC-32C is {answer:#010x}; "
                    f"it is computing a different checksum, not a different implementation of "
                    f"this one.",
                    field="provider",
                    value=self._provider_name,
                    length=len(payload),
                    expected=answer,
                    produced=produced,
                )
        for payload, seed in CRC32C_ACCEPTANCE_CORPUS:
            expected = crc32c_reference(payload, seed)
            produced = self._provider(payload, seed)
            if produced != expected:
                raise GrafxConfigurationError(
                    f"The native CRC-32C provider {self._provider_name!r} answered "
                    f"{produced:#010x} where CRC-32C is {expected:#010x}; it is not computing "
                    f"Castagnoli CRC-32C, and installing it would report corruption on healthy "
                    f"pages.",
                    field="provider",
                    value=self._provider_name,
                    length=len(payload),
                    expected=expected,
                    produced=produced,
                )

    @property
    def name(self) -> str:
        """Return the bounded label this adapter reports."""
        return NATIVE_ADAPTER_NAME

    @property
    def provider(self) -> str:
        """Return the provider this adapter is computing through, so a run can be reproduced."""
        return self._provider_name

    def checksum(self, data: bytes, crc: int = CRC32C_INITIAL) -> int:
        """Return the CRC-32C of the data, continuing a previous result when one is given."""
        return self._provider(data, crc)

    def install(self) -> str:
        """Make this the implementation :func:`crc32c` calls, and say which one it replaced.

        The full acceptance corpus runs inside :func:`install_crc32c`. It is the last thing that
        happens before any real page is checksummed by this provider, and a provider that fails
        it anywhere is not installed at all.
        """
        return install_crc32c(self._provider, name=NATIVE_ADAPTER_NAME)

    def __repr__(self) -> str:
        return f"NativeCrc32c(name={NATIVE_ADAPTER_NAME!r}, provider={self._provider_name!r})"
