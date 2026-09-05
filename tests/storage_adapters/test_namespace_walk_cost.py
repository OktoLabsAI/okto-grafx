"""What a prefixed listing and a warm descriptor hit may cost, and what they must still refuse.

Every test here counts system calls around ONE device operation on a small board that has the
shape of the acceptance board: a handful of WAL segments, many index files, a control file and
a nested quarantine. The counts are the assertions. A result-only test would pass both before
and after the change, and so would prove nothing about the cost this suite exists to pin.

The safety half is written as ordinary behaviour: a redirected component on the way to the
prefix is refused exactly as before, a redirect elsewhere is still refused by the full walk, and
a control file published by another participant is still seen on a warm hit. Those tests pass
before and after -- they are the pins that the cheaper walk and the cheaper revalidation are not
allowed to break, and each names the mutant that would break it.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from okto_grafx.adapters import storage_local
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.domain.errors import GrafxUnsupportedOperation

PAGE_SIZE: int = 512

WAL_SEGMENTS: tuple[str, ...] = ("wal/000000000001.wal", "wal/000000000002.wal")
INDEX_FILES: tuple[str, ...] = tuple(f"index/i{number:03d}.idx" for number in range(60))
CONTROL: str = "control/commit.state"
NESTED: str = "quarantine/a/b/c.dat"
HEAP: str = "heap.dat"


@contextlib.contextmanager
def _counting_syscalls() -> Iterator[Counter[str]]:
    """Count the namespace system calls the adapter makes inside the block, then restore them."""
    counts: Counter[str] = Counter()
    originals = {
        "scandir": os.scandir,
        "lstat": os.lstat,
        "stat": os.stat,
        "fstat": os.fstat,
        "realpath": os.path.realpath,
    }

    def _counting(name: str):  # type: ignore[no-untyped-def]
        original = originals[name]

        def call(*args, **kwargs):  # type: ignore[no-untyped-def]
            counts[name] += 1
            return original(*args, **kwargs)

        return call

    os.scandir = _counting("scandir")  # type: ignore[assignment]
    os.lstat = _counting("lstat")  # type: ignore[assignment]
    os.stat = _counting("stat")  # type: ignore[assignment]
    os.fstat = _counting("fstat")  # type: ignore[assignment]
    os.path.realpath = _counting("realpath")  # type: ignore[assignment]
    try:
        yield counts
    finally:
        os.scandir = originals["scandir"]  # type: ignore[assignment]
        os.lstat = originals["lstat"]  # type: ignore[assignment]
        os.stat = originals["stat"]  # type: ignore[assignment]
        os.fstat = originals["fstat"]  # type: ignore[assignment]
        os.path.realpath = originals["realpath"]  # type: ignore[assignment]


@pytest.fixture
def board(tmp_path: Path) -> Iterator[tuple[LocalStorageDevice, Path]]:
    """A device over a board shaped like the acceptance board, small enough to count."""
    root = tmp_path / "database"
    device = LocalStorageDevice(root, page_size=PAGE_SIZE)
    for name in (*WAL_SEGMENTS, *INDEX_FILES, CONTROL, NESTED, HEAP):
        device.create(name)
    device.allocate(HEAP, 2)
    device.write_page(HEAP, 0, bytes([1]) * PAGE_SIZE)
    device.append_log(CONTROL, b"published-1")
    try:
        yield device, root
    finally:
        device.close()


def _directory_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (NotImplementedError, OSError) as failure:
        pytest.skip(f"directory symlink/reparse creation is unavailable: {failure}")


# --- B1: a prefixed listing walks the prefix, not the board -------------------------------


def test_a_prefixed_listing_resolves_at_most_one_real_path_per_directory_level(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, _root = board
    with _counting_syscalls() as counts:
        listed = device.list_files("wal/")
    assert listed == WAL_SEGMENTS
    # Before: the root plus every one of the 60+ entries of the board went through realpath.
    # After: the root and the ``wal`` directory; entries inherit containment from their parent.
    assert counts["realpath"] <= 2, dict(counts)


def test_a_directory_prefix_does_not_enumerate_its_siblings(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, _root = board
    with _counting_syscalls() as counts:
        listed = device.list_files("wal/")
    assert listed == WAL_SEGMENTS
    # One scandir: the ``wal`` directory itself. Neither the root nor ``index/`` is opened.
    # Mutant m1 -- a walk that starts at the root and filters -- opens five directories here.
    assert counts["scandir"] == 1, dict(counts)
    # Root identity, the ``wal`` segment, and whatever the platform's junction probe adds.
    assert counts["lstat"] <= 4, dict(counts)


def test_a_file_prefix_scans_only_the_parent_directory(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, _root = board
    with _counting_syscalls() as counts:
        listed = device.list_files("control/commit.")
    assert listed == (CONTROL,)
    assert counts["scandir"] == 1, dict(counts)


def test_a_missing_prefix_directory_lists_nothing_without_scanning(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, _root = board
    with _counting_syscalls() as counts:
        assert device.list_files("nothing/") == ()
    assert counts["scandir"] == 0, dict(counts)


def test_a_stored_file_used_as_a_prefix_directory_lists_nothing(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, _root = board
    assert device.list_files(f"{HEAP}/") == ()


def test_nested_names_below_the_prefix_directory_are_returned(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, _root = board
    # Mutant m3 -- one listdir level instead of a walk -- returns nothing for both.
    assert device.list_files("quarantine/") == (NESTED,)
    assert device.list_files("quarantine/a/") == (NESTED,)


def test_a_prefix_spelt_in_another_case_lists_nothing(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, _root = board
    # Logical names are case sensitive whatever the volume does. On a case-insensitive volume
    # a walk that descended by the caller's spelling would open the stored ``wal`` for ``Wal/``
    # and yield fabricated ``Wal/...`` names; on a case-sensitive one this holds trivially.
    assert device.list_files("Wal/") == ()
    assert device.list_files("WAL/000000000001.wal") == ()
    assert device.list_files("wal/") == WAL_SEGMENTS


def test_a_prefix_without_a_directory_is_still_a_full_listing(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, _root = board
    everything = device.list_files()
    for prefix in ("wal", "i"):
        expected = tuple(name for name in everything if name.startswith(prefix))
        assert device.list_files(prefix) == expected


def test_a_redirected_prefix_directory_is_still_refused(
    board: tuple[LocalStorageDevice, Path], tmp_path: Path
) -> None:
    device, root = board
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "000000000009.log").write_bytes(b"victim bytes")
    # A directory the device never opened a file in: Windows refuses to rename a directory
    # that holds an open handle, and the fixture keeps ``wal/`` open.
    redirected = root / "spool"
    try:
        _directory_symlink_or_skip(redirected, victim)
        before = {entry.name: entry.read_bytes() for entry in victim.iterdir()}
        # Mutant m2 -- entering the prefix directory without inspecting it -- lists the victim.
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.list_files("spool/")
        assert raised.value.details["reason"] == "redirected_path"
        assert raised.value.details["file"] == "spool"
        assert {entry.name: entry.read_bytes() for entry in victim.iterdir()} == before
    finally:
        if redirected.is_symlink():
            redirected.unlink()


def test_a_redirect_outside_the_prefix_is_the_full_walk_s_business(
    board: tuple[LocalStorageDevice, Path], tmp_path: Path
) -> None:
    device, root = board
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "planted.idx").write_bytes(b"victim bytes")
    planted = root / "index" / "evil"
    try:
        _directory_symlink_or_skip(planted, victim)
        # The prefixed listing never reaches ``index/``: it has no opinion about it.
        assert device.list_files("wal/") == WAL_SEGMENTS
        # The full listing still meets the redirect and still refuses it, at any depth.
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.list_files()
        assert raised.value.details["reason"] == "redirected_path"
        assert raised.value.details["file"] == "index/evil"
    finally:
        if planted.is_symlink():
            planted.unlink()


def test_entries_listed_outside_the_proved_directory_are_refused(
    board: tuple[LocalStorageDevice, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    device, root = board
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "foreign.wal").write_bytes(b"victim segment")
    real_scandir = os.scandir
    target = os.path.normcase(str(root / "wal"))

    def foreign_scandir(path=".", *args, **kwargs):  # type: ignore[no-untyped-def]
        # Model a listing that came from somewhere else: the entries of the victim, delivered
        # for the proved directory. The base resolved every entry's real path and refused
        # them as an escape; the prefix walk must refuse them without that resolution.
        if os.path.normcase(os.fspath(path)) == target:
            return real_scandir(str(victim))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", foreign_scandir)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        device.list_files("wal/")
    assert raised.value.details["reason"] == "path_escape"
    assert raised.value.details["file"] == "wal/foreign.wal"


def _windows_junction_or_skip(link: Path, target: Path) -> None:
    """Create an NTFS junction (no privilege needed), or declare the host cannot."""
    try:
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as failure:
        pytest.skip(f"NTFS junction creation is unavailable: {failure}")
    if created.returncode != 0:
        pytest.skip(
            "NTFS junction creation is unavailable: "
            f"{created.stderr.strip() or created.stdout.strip()}"
        )


def _exchange_right_before_listing(
    device: LocalStorageDevice,
    root: Path,
    victim: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_redirect: Callable[[Path, Path], None],
) -> None:
    """Exchange a proved directory for a redirect between its proof and its listing.

    The listing then follows the redirect and returns the victim's entries under the stored
    path, so only re-proving the directory AFTER the listing can refuse this. The directory is
    one the device never opened a file in: Windows refuses to rename one holding a handle.
    """
    (victim / "planted.log").write_bytes(b"victim bytes")
    spool = root / "spool"
    spool.mkdir()
    (spool / "000000000001.log").write_bytes(b"stored")
    original = root / "spool-original"
    real_scandir = os.scandir
    target = os.path.normcase(str(spool))
    swapped = False

    def swapping_scandir(path=".", *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if os.path.normcase(os.fspath(path)) == target and not swapped:
            spool.rename(original)
            make_redirect(spool, victim)
            swapped = True
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", swapping_scandir)
    try:
        before = {entry.name: entry.read_bytes() for entry in victim.iterdir()}
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.list_files("spool/")
        assert raised.value.details["reason"] == "redirected_path"
        assert raised.value.details["file"] == "spool"
        assert swapped, "the exchange never happened, so nothing was proved"
        assert {entry.name: entry.read_bytes() for entry in victim.iterdir()} == before
    finally:
        monkeypatch.setattr(os, "scandir", real_scandir)
        if spool.is_symlink():
            spool.unlink()
        elif swapped and spool.exists():
            os.rmdir(spool)  # an NTFS junction is removed as an entry, never followed
        if original.exists():
            original.rename(spool)


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name != "nt", reason="An NTFS junction exchange requires Windows.")
def test_a_directory_exchanged_for_a_junction_right_before_its_listing_is_refused(
    board: tuple[LocalStorageDevice, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    device, root = board
    victim = tmp_path / "victim"
    victim.mkdir()
    _exchange_right_before_listing(device, root, victim, monkeypatch, _windows_junction_or_skip)


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name == "nt", reason="The POSIX half of the pair uses a symlink.")
def test_a_directory_exchanged_for_a_symlink_right_before_its_listing_is_refused(
    board: tuple[LocalStorageDevice, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    device, root = board
    victim = tmp_path / "victim"
    victim.mkdir()
    _exchange_right_before_listing(device, root, victim, monkeypatch, _directory_symlink_or_skip)


# --- B2: a warm descriptor hit checks identity, not the whole chain again ------------------


def test_a_warm_descriptor_hit_does_not_re_prove_the_path_chain(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, _root = board
    device.read_page(HEAP, 0)  # admit the descriptor: the chain is proved here, once
    with _counting_syscalls() as counts:
        assert device.read_page(HEAP, 0) == bytes([1]) * PAGE_SIZE
    # Identity reuses the final lstat of the required no-follow chain and compares it with one
    # fstat of the held descriptor (plus the fstat that sizes the file). Before, the same hit
    # followed the final path once more with stat. The guard that stays -- one lstat of the root
    # and one of each existing component, followed nowhere -- is REQUIRED here: a hit that
    # skipped it would not refuse a root or a directory exchanged for a redirect after admission.
    assert counts["realpath"] == 0, dict(counts)
    assert 2 <= counts["lstat"] <= 4, dict(counts)
    assert counts["stat"] == 0, dict(counts)
    assert counts["fstat"] <= 2, dict(counts)


# --- QW-3: cold identity resolution inherits containment from proved parents ----------------


def test_identity_namespace_doors_do_not_rederive_real_paths(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, _root = board

    with _counting_syscalls() as exists_calls:
        assert device.exists(CONTROL)
    assert exists_calls["realpath"] == 0, dict(exists_calls)

    staging = "control/qw3.next"
    target = "control/qw3.state"
    with _counting_syscalls() as create_calls:
        device.create(staging)
    assert create_calls["realpath"] == 0, dict(create_calls)
    device.append_log(staging, b"state")

    with _counting_syscalls() as replace_calls:
        device.atomic_replace(staging, target)
    assert replace_calls["realpath"] == 0, dict(replace_calls)

    with _counting_syscalls() as remove_calls:
        device.remove(target)
    assert remove_calls["realpath"] == 0, dict(remove_calls)


def test_a_cold_descriptor_miss_does_not_rederive_real_paths(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    _device, root = board
    cold = LocalStorageDevice(root, page_size=PAGE_SIZE)
    try:
        with _counting_syscalls() as counts:
            assert cold.read_page(HEAP, 0) == bytes([1]) * PAGE_SIZE
        assert counts["realpath"] == 0, dict(counts)
    finally:
        cold.close()


def _exchange_parent_after_its_entries_were_listed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_redirect: Callable[[Path, Path], None],
) -> None:
    """A child resolved from entries of an exchanged parent is never accepted."""
    root = tmp_path / "database"
    spool = root / "spool"
    spool.mkdir(parents=True)
    (spool / "state.bin").write_bytes(b"stored")
    victim = tmp_path / "victim"
    victim.mkdir()
    protected = victim / "state.bin"
    protected.write_bytes(b"victim")
    original = root / "spool-original"
    device = LocalStorageDevice(root, page_size=PAGE_SIZE, create_root=False)
    real_listdir = storage_local.os.listdir
    target = os.path.normcase(str(spool))
    swapped = False

    def swapping_listdir(path: str) -> list[str]:
        nonlocal swapped
        entries = real_listdir(path)
        if os.path.normcase(os.fspath(path)) == target and not swapped:
            spool.rename(original)
            make_redirect(spool, victim)
            swapped = True
        return entries

    monkeypatch.setattr(storage_local.os, "listdir", swapping_listdir)
    try:
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.exists("spool/state.bin")
        assert raised.value.details["reason"] == "redirected_path"
        assert swapped, "the parent was never exchanged after its listing"
        assert protected.read_bytes() == b"victim"
    finally:
        device.close()
        monkeypatch.setattr(storage_local.os, "listdir", real_listdir)
        if spool.is_symlink():
            spool.unlink()
        elif swapped and spool.exists():
            os.rmdir(spool)
        if original.exists():
            original.rename(spool)


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name != "nt", reason="An NTFS junction exchange requires Windows.")
def test_a_parent_exchanged_for_a_junction_after_listing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exchange_parent_after_its_entries_were_listed(
        tmp_path, monkeypatch, _windows_junction_or_skip
    )


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name == "nt", reason="The POSIX half of the pair uses a symlink.")
def test_a_parent_exchanged_for_a_symlink_after_listing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exchange_parent_after_its_entries_were_listed(
        tmp_path, monkeypatch, _directory_symlink_or_skip
    )


def _exchange_parent_right_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_redirect: Callable[[Path, Path], None],
) -> None:
    """A parent exchanged after lstat but before open must never admit the foreign handle."""
    root = tmp_path / "database"
    spool = root / "spool"
    spool.mkdir(parents=True)
    (spool / "state.bin").write_bytes(b"stored")
    victim = tmp_path / "victim"
    victim.mkdir()
    protected = victim / "state.bin"
    protected.write_bytes(b"victim")
    original = root / "spool-original"
    device = LocalStorageDevice(root, page_size=PAGE_SIZE, create_root=False)
    real_open = storage_local._open_descriptor
    target = os.path.normcase(str(spool / "state.bin"))
    swapped = False

    def swapping_open(path: str, *, create_new: bool) -> int:
        nonlocal swapped
        if os.path.normcase(path) == target and not swapped:
            spool.rename(original)
            make_redirect(spool, victim)
            swapped = True
        return real_open(path, create_new=create_new)

    monkeypatch.setattr(storage_local, "_open_descriptor", swapping_open)
    try:
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.read_log("spool/state.bin", 0, 6)
        assert raised.value.details["reason"] == "redirected_path"
        assert swapped, "the parent was never exchanged at the open boundary"
        assert protected.read_bytes() == b"victim"
    finally:
        device.close()
        monkeypatch.setattr(storage_local, "_open_descriptor", real_open)
        if spool.is_symlink():
            spool.unlink()
        elif swapped and spool.exists():
            os.rmdir(spool)
        if original.exists():
            original.rename(spool)


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name != "nt", reason="An NTFS junction exchange requires Windows.")
def test_a_parent_exchanged_for_a_junction_between_resolution_and_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exchange_parent_right_before_open(
        tmp_path, monkeypatch, _windows_junction_or_skip
    )


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name == "nt", reason="The POSIX half of the pair uses a symlink.")
def test_a_parent_exchanged_for_a_symlink_between_resolution_and_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exchange_parent_right_before_open(
        tmp_path, monkeypatch, _directory_symlink_or_skip
    )


def _exchange_root_right_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_redirect: Callable[[Path, Path], None],
) -> None:
    """The opened root identity is rechecked after a cold descriptor has been obtained."""
    root = tmp_path / "database"
    root.mkdir()
    (root / "state.bin").write_bytes(b"stored")
    victim = tmp_path / "victim"
    victim.mkdir()
    protected = victim / "state.bin"
    protected.write_bytes(b"victim")
    original = tmp_path / "database-original"
    device = LocalStorageDevice(root, page_size=PAGE_SIZE, create_root=False)
    real_open = storage_local._open_descriptor
    target = os.path.normcase(str(root / "state.bin"))
    swapped = False

    def swapping_open(path: str, *, create_new: bool) -> int:
        nonlocal swapped
        if os.path.normcase(path) == target and not swapped:
            root.rename(original)
            make_redirect(root, victim)
            swapped = True
        return real_open(path, create_new=create_new)

    monkeypatch.setattr(storage_local, "_open_descriptor", swapping_open)
    try:
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.read_log("state.bin", 0, 6)
        assert raised.value.details["reason"] == "redirected_root"
        assert swapped, "the root was never exchanged at the open boundary"
        assert protected.read_bytes() == b"victim"
    finally:
        device.close()
        monkeypatch.setattr(storage_local, "_open_descriptor", real_open)
        if root.is_symlink():
            root.unlink()
        elif swapped and root.exists():
            os.rmdir(root)
        if original.exists():
            original.rename(root)


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name != "nt", reason="An NTFS junction exchange requires Windows.")
def test_a_root_exchanged_for_a_junction_between_resolution_and_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exchange_root_right_before_open(tmp_path, monkeypatch, _windows_junction_or_skip)


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name == "nt", reason="The POSIX half of the pair uses a symlink.")
def test_a_root_exchanged_for_a_symlink_between_resolution_and_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exchange_root_right_before_open(tmp_path, monkeypatch, _directory_symlink_or_skip)


def test_a_control_file_published_by_another_participant_is_seen_on_a_warm_hit(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, root = board
    assert device.read_log(CONTROL, 0, 11) == b"published-1"
    other = LocalStorageDevice(root, page_size=PAGE_SIZE)
    try:
        other.create("control/commit.state.tmp")
        other.append_log("control/commit.state.tmp", b"published-2")
        other.atomic_replace("control/commit.state.tmp", CONTROL)
    finally:
        other.close()
    # Mutant m4 -- a revalidation that stops looking at the directory entry -- answers the old
    # bytes here forever (the CF-12 defect the identity check exists to prevent).
    assert device.read_log(CONTROL, 0, 11) == b"published-2"


# The three redirect-under-a-cached-descriptor tests run on POSIX only: Windows refuses to
# rename a directory that holds an open handle, so the redirect cannot appear under a cached
# descriptor there. Their Windows counterpart is the counting test above, which REQUIRES the
# guard's lstat calls on every warm hit (G4: the skip is declared per test, marker and family).
REDIRECT_UNDER_HANDLE_NEEDS_POSIX: str = (
    "Windows refuses to rename a directory that holds an open handle, so a redirect cannot "
    "appear under a cached descriptor there; on POSIX it can, and the warm hit must catch it."
)


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name == "nt", reason=REDIRECT_UNDER_HANDLE_NEEDS_POSIX)
def test_a_directory_redirected_to_its_own_original_is_still_refused_on_a_warm_hit(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, root = board
    assert device.read_log(CONTROL, 0, 11) == b"published-1"  # admitted and cached
    original = root / "control-original"
    redirected = root / "control"
    redirected.rename(original)
    try:
        _directory_symlink_or_skip(redirected, original)
        # The redirect leads back to the very same file, so the identity of the entry still
        # agrees with the held descriptor: only inspecting the component itself, without
        # following it, can refuse this. A hit that compared identities alone would answer.
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.read_log(CONTROL, 0, 11)
        assert raised.value.details["reason"] == "redirected_path"
    finally:
        if redirected.is_symlink():
            redirected.unlink()
        if original.exists():
            original.rename(redirected)


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name == "nt", reason=REDIRECT_UNDER_HANDLE_NEEDS_POSIX)
def test_a_root_redirected_to_its_own_original_is_still_refused_on_a_warm_hit(
    board: tuple[LocalStorageDevice, Path],
) -> None:
    device, root = board
    assert device.read_page(HEAP, 0) == bytes([1]) * PAGE_SIZE  # admitted and cached
    original = root.parent / "database-original"
    root.rename(original)
    try:
        _directory_symlink_or_skip(root, original)
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.read_page(HEAP, 0)
        assert raised.value.details["reason"] == "redirected_root"
    finally:
        if root.is_symlink():
            root.unlink()
        if original.exists():
            original.rename(root)


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name == "nt", reason=REDIRECT_UNDER_HANDLE_NEEDS_POSIX)
def test_a_component_redirected_after_admission_is_refused_on_the_next_access(
    board: tuple[LocalStorageDevice, Path], tmp_path: Path
) -> None:
    device, root = board
    # The descriptor is admitted and cached here, with its chain proved once.
    assert device.read_log(CONTROL, 0, 11) == b"published-1"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "commit.state").write_bytes(b"victim authority")
    original = root / "control-original"
    redirected = root / "control"
    redirected.rename(original)
    try:
        _directory_symlink_or_skip(redirected, victim)
        before = {entry.name: entry.read_bytes() for entry in victim.iterdir()}
        # The cached path now leads through the redirect to a different file: identity
        # disagrees, the name is re-resolved from scratch, and that resolution refuses it.
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            device.read_log(CONTROL, 0, 11)
        assert raised.value.details["reason"] == "redirected_path"
        assert {entry.name: entry.read_bytes() for entry in victim.iterdir()} == before
    finally:
        if redirected.is_symlink():
            redirected.unlink()
        if original.exists():
            original.rename(redirected)
