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

**The corpus proof of a closed-list provider is memoized per process (D-29).** Every
``connect()`` builds a fresh :class:`NativeCrc32c`, and proving the whole corpus against the
pure reference costs tens of milliseconds each time -- paid for the same ``google_crc32c``
function object over and over. The memo is keyed on the provider's STRONG identity (module
name, attribute, the file the module was loaded from, the version it reports and the exact
function object), never on the adapted callable, which ``load_provider`` creates anew per
call. Only a successful proof enters; a refusal leaves nothing behind. An injected provider
and an explicit ``verify_runtime=True`` keep their per-construction and per-call semantics
untouched. The domain's installer door keeps its own, independent proof: the adapter hands it
the same strong identity explicitly, and it too skips the replay only for the exact wrapper
it already proved -- so a second ``connect()`` over the same provider costs no corpus in
either door, while a replaced function, an injected callable or a refusal proves again. Both
memos hold at most one current identity per closed module/attribute slot. A process-local
adapter guard serializes lookup, proof and publication; the pure domain imports no mechanism.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from threading import RLock

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxError
from okto_grafx.domain.page.checksum import (
    CRC32C_ACCEPTANCE_CORPUS,
    CRC32C_INITIAL,
    CRC32C_KNOWN_ANSWERS,
    _forget_closed_proof,
    _install_validated_crc32c,
    _require_crc32c_agreement,
    _require_crc32c_answer,
    crc32c_reference,
    install_crc32c,
)

__all__ = [
    "NATIVE_ADAPTER_NAME",
    "CRC32C_PROVIDERS",
    "NativeCrc32c",
    "load_provider",
    "validated_closed_providers",
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


def _builtin_type_name(value: object) -> str:
    """Name a provider value without executing a metaclass descriptor."""
    value_type = type(value)
    declared = type.__dict__["__name__"].__get__(value_type, type(value_type))
    return str.__str__(declared)


def _crc_first(module_name: str) -> bool:
    """Return whether this provider takes the running CRC before the data."""
    return module_name == "google_crc32c"


def _adapt(
    module_name: str, attribute: str, provider: object
) -> Callable[[bytes, int], int]:
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
    if _crc_first(module_name):
        return lambda data, crc=CRC32C_INITIAL: provider(crc, data)
    return lambda data, crc=CRC32C_INITIAL: provider(data, crc)


def _closed_provider_identity(
    module_name: str, attribute: str, module: object, function: object
) -> tuple[str, str, str | None, str | None, object]:
    """Return the strong identity the corpus memo is keyed on.

    The function OBJECT is part of the key and is held strongly by the memo, so its id can
    never be recycled onto something else while the entry lives. The module origin and
    version separate two builds that expose an equally named function. Anything that is not
    a plain string is recorded as absent rather than guessed at.
    """
    spec = getattr(module, "__spec__", None)
    origin = getattr(spec, "origin", None)
    version = getattr(module, "__version__", None)
    return (
        module_name,
        attribute,
        origin if type(origin) is str else None,
        version if type(version) is str else None,
        function,
    )


_ClosedProviderIdentity = tuple[str, str, str | None, str | None, object]
_ClosedProviderSlot = tuple[str, str]


def _closed_slot(identity: _ClosedProviderIdentity) -> _ClosedProviderSlot:
    """Return the fixed module/attribute slot occupied by a closed provider."""
    return identity[0], identity[1]


def _same_closed_identity(
    left: _ClosedProviderIdentity, right: _ClosedProviderIdentity
) -> bool:
    """Compare metadata by value and the raw provider strictly by object identity."""
    return left[:4] == right[:4] and left[4] is right[4]


class _ClosedProvider:
    """A closed-list provider adapted to ``(data, crc) -> int`` that knows its identity.

    ``load_provider`` used to hand back a bare lambda, a new object on every call, so nothing
    about it could key a memo. This carries the strong identity of the underlying function
    alongside the same call semantics; an injected callable never gets one, which is exactly
    what keeps injected providers on the per-construction, per-call path.
    """

    __slots__ = ("_crc_first", "_function", "identity", "module_name")

    def __init__(
        self, module_name: str, attribute: str, module: object, function: object
    ) -> None:
        if not callable(function):
            raise ImportError(
                f"{module_name}.{attribute} is not callable, so it cannot compute a checksum."
            )
        self.module_name: str = module_name
        self._function: Callable[..., object] = function
        self._crc_first: bool = _crc_first(module_name)
        self.identity = _closed_provider_identity(
            module_name, attribute, module, function
        )

    def __call__(self, data: bytes, crc: int = CRC32C_INITIAL) -> object:
        if self._crc_first:
            return self._function(crc, data)
        return self._function(data, crc)

    def __repr__(self) -> str:
        return f"_ClosedProvider(module_name={self.module_name!r})"


_closed_provider_lock = RLock()
"""Serializes closed-provider lookup, both corpus proofs and their bounded publication.

The mechanism lives in the adapter, not the pure domain (G2). The private domain memo door is
entered only while this guard is held, so a concurrent first construction/installation has one
loader and one successful proof in each layer.
"""

_closed_providers: dict[_ClosedProviderSlot, _ClosedProvider] = {}
"""The current adapted wrapper for each closed-list module/attribute slot.

``load_provider`` hands the cached wrapper back while module, attribute, origin, version and
the raw function object are all unchanged; any of them changing REPLACES the slot and evicts
both obsolete proofs. Its size is therefore bounded by ``len(CRC32C_PROVIDERS)`` even across
arbitrarily many module reloads.
"""

_validated_closed_providers: dict[_ClosedProviderSlot, _ClosedProvider] = {}
"""Current closed-list wrappers this adapter has already proved against the corpus.

Only a SUCCESSFUL proof is recorded, only for a provider ``load_provider`` resolved (never an
injected callable) and only when runtime verification resolved off. A refusal records
nothing, so the next construction proves again. One entry per fixed provider slot is a hard
bound; raw callable identity is tested with ``is`` and never delegated to its equality/hash.
"""


def validated_closed_providers() -> tuple[str, ...]:
    """Return the closed-list providers this process has memoized, in first-proved order."""
    with _closed_provider_lock:
        return tuple(
            dict.fromkeys(
                provider.module_name
                for provider in _validated_closed_providers.values()
            )
        )


def _forget_validated_closed_providers() -> None:
    """Drop every memoized proof and cached wrapper so the corpus is replayed (tests)."""
    from okto_grafx.domain.page.checksum import _forget_closed_proofs

    with _closed_provider_lock:
        _validated_closed_providers.clear()
        _closed_providers.clear()
        _forget_closed_proofs()


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
        identity = _closed_provider_identity(module_name, attribute, module, function)
        slot = _closed_slot(identity)
        with _closed_provider_lock:
            cached = _closed_providers.get(slot)
            if cached is None or not _same_closed_identity(cached.identity, identity):
                if cached is not None:
                    _validated_closed_providers.pop(slot, None)
                    _forget_closed_proof(slot)
                cached = _ClosedProvider(module_name, attribute, module, function)
                _closed_providers[slot] = cached
            return module_name, cached
    wanted = ", ".join(name for name, _attribute in CRC32C_PROVIDERS)
    raise ImportError(
        f"No native CRC-32C provider is installed. Install the [accel] extra, which brings one "
        f"of: {wanted}."
    )


class NativeCrc32c:
    """A native CRC-32C, proved byte-identical to the pure reference before it is usable."""

    __slots__ = ("_memo_identity", "_provider", "_provider_name", "_verify_runtime")

    def __init__(
        self,
        provider: Callable[[bytes, int], int] | None = None,
        *,
        provider_name: str | None = None,
        verify_runtime: bool | None = None,
    ) -> None:
        """Adopt a provider, or find one, and refuse it unless it reproduces the reference.

        ``provider`` is a declared seam and not test scaffolding smuggled into production: a
        composition that already holds a native CRC-32C -- from a host application, from a
        vendored extension -- passes it here rather than being told to install a package it
        already has. The default path takes it from :func:`load_provider`, so production
        behaviour with no argument is exactly what it would be without the parameter (A81).

        ``verify_runtime=None`` chooses the safe useful default: closed-list packages are
        corpus-validated and run fast, while an injected callable is checked against the oracle
        on every call. An explicit bool always wins. A host can set ``False`` for a vendored
        extension it trusts in-process, or ``True`` to put even a closed-list package behind the
        per-call oracle; the latter trades away acceleration deliberately.

        The corpus proof of a closed-list provider is memoized per process under the provider's
        strong identity (D-29): a second construction over the very same function object skips
        the replay, a refusal never enters the memo, and neither an injected provider nor an
        explicit ``verify_runtime=True`` ever reads it.

        The check runs HERE as well as inside :func:`install_crc32c`, and that is not redundant
        defence: this one makes a wrong provider fail where it is CONSTRUCTED, naming the
        provider, while the installer's protects the domain from any candidate at all, including
        one nobody built through this class. Break either and the other still refuses, which is
        the case A67 asks about -- and for a value that silently converts healthy pages into
        quarantined ones, that is the side to err on (LESSONS L17).
        """
        if verify_runtime is not None and type(verify_runtime) is not bool:
            raise GrafxConfigurationError(
                "verify_runtime must be a bool or None.",
                field="verify_runtime",
                value=_builtin_type_name(verify_runtime),
            )
        verify_answers = (
            provider is not None if verify_runtime is None else verify_runtime
        )
        identity: _ClosedProviderIdentity | None = None
        if provider is None and not verify_answers:
            # Keep lookup, proof decision, full proof and publication in one critical section.
            # ``load_provider`` re-enters this RLock. Without the outer guard, a concurrent
            # reload could replace the slot between lookup and proof publication, letting an
            # obsolete identity displace the current one.
            with _closed_provider_lock:
                resolved_name, resolved = load_provider()
                if isinstance(resolved, _ClosedProvider):
                    identity = resolved.identity
                    self._provider = resolved
                    self._provider_name = resolved_name
                    self._verify_runtime = False
                    self._memo_identity = identity
                    slot = _closed_slot(identity)
                    if _validated_closed_providers.get(slot) is resolved:
                        return
                    self._require_agreement()
                    _validated_closed_providers[slot] = resolved
                    return
        elif provider is None:
            resolved_name, resolved = load_provider()
        else:
            if not callable(provider):
                observed = _builtin_type_name(provider)
                raise GrafxConfigurationError(
                    f"A CRC-32C provider must be callable; got {observed}.",
                    field="provider",
                    value=observed,
                )
            if provider_name is None:
                resolved_name = "supplied"
            elif not issubclass(type(provider_name), str):
                raise GrafxConfigurationError(
                    "A CRC-32C provider name must be a string.",
                    field="provider_name",
                    value=_builtin_type_name(provider_name),
                )
            else:
                resolved_name = str.__str__(provider_name)
                if not resolved_name:
                    raise GrafxConfigurationError(
                        "A CRC-32C provider name must not be empty.",
                        field="provider_name",
                        value="",
                    )
            resolved = provider
        self._provider: Callable[[bytes, int], int] = resolved
        self._provider_name: str = resolved_name
        self._verify_runtime: bool = verify_answers
        self._memo_identity = identity if not verify_answers else None
        self._require_agreement()

    def _raw_answer(self, data: bytes, crc: int) -> int:
        """Call the provider and return an exact bounded checksum or a typed refusal."""
        try:
            observed = self._provider(data, crc)
        except GrafxError:
            raise
        except Exception as failure:
            cause = _builtin_type_name(failure)
            raise GrafxConfigurationError(
                f"The native CRC-32C provider {self._provider_name!r} raised {cause}.",
                field="provider",
                value=self._provider_name,
                cause=cause,
                length=len(data),
                seed=crc,
            ) from failure
        return _require_crc32c_answer(
            observed,
            field="provider",
            name=self._provider_name,
            length=len(data),
            seed=crc,
        )

    def _answer(self, data: bytes, crc: int) -> int:
        """Return the provider answer only after the reference verifies these exact bytes."""
        produced = self._raw_answer(data, crc)
        if not self._verify_runtime:
            return produced
        return _require_crc32c_agreement(
            produced,
            data,
            crc,
            name=self._provider_name,
            field="provider",
        )

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
            produced = self._raw_answer(payload, CRC32C_INITIAL)
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
            produced = self._raw_answer(payload, seed)
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
        return self._answer(data, crc)

    def install(self) -> str:
        """Make this the implementation :func:`crc32c` calls, and say which one it replaced.

        The full acceptance corpus runs inside :func:`install_crc32c`. It is the last thing that
        happens before any real page is checksummed by this provider, and a provider that fails
        it anywhere is not installed at all. For a closed-list provider on the default path the
        strong identity travels with the callable, and the door skips the replay only when this
        exact wrapper already passed under it (D-29); an injected provider never carries one.
        """
        if not self._verify_runtime:
            if self._memo_identity is not None:
                with _closed_provider_lock:
                    slot = _closed_slot(self._memo_identity)
                    current = _closed_providers.get(slot)
                    # A previously constructed adapter remains safe to install after a module
                    # reload, but it must prove again without displacing the proof for the new
                    # current identity in this slot.
                    memo_identity = (
                        self._memo_identity if current is self._provider else None
                    )
                    return _install_validated_crc32c(
                        self._provider,
                        name=NATIVE_ADAPTER_NAME,
                        memo_identity=memo_identity,
                    )
            return _install_validated_crc32c(
                self._provider,
                name=NATIVE_ADAPTER_NAME,
                memo_identity=None,
            )
        return install_crc32c(self._provider, name=NATIVE_ADAPTER_NAME)

    def __repr__(self) -> str:
        return f"NativeCrc32c(name={NATIVE_ADAPTER_NAME!r}, provider={self._provider_name!r})"
