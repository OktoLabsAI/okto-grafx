"""ST-2 descriptor identity policies for the local filesystem adapter."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.domain.control_record import (
    ControlRecordKind,
    TwoSlotControlRecordStore,
)
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxStorageError,
    GrafxUnsupportedOperation,
)


PAGE_SIZE: int = 512
HEAP: str = "heap.dat"
CATALOG: str = "catalog.dat"
CONTROL: str = "control/commit.state"
SEGMENT: str = "wal/000000000001.wal"
INDEX: str = "index/person_name.idx"
CONTROL_TEMP: str = "control/commit.state.test.tmp"
DATABASE_UUID: bytes = bytes.fromhex("00112233445566778899aabbccddeeff")


def _seed(root: Path, *files: tuple[str, bytes]) -> None:
    """Create complete file images through a separate, closed device."""
    with LocalStorageDevice(root, page_size=PAGE_SIZE) as writer:
        for name, payload in files:
            writer.create(name)
            writer.append_log(name, payload)
            writer.durable_barrier(name)


def _count_warm_identity_proofs(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Record each invocation of the warm-descriptor identity proof."""
    calls: list[str] = []
    original = LocalStorageDevice._still_names

    def counted(self: LocalStorageDevice, name: str, descriptor: int) -> bool:
        calls.append(name)
        return original(self, name, descriptor)

    monkeypatch.setattr(LocalStorageDevice, "_still_names", counted)
    return calls


def _count_exact_identity_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Record exact-case namespace walks independently from warm handle comparisons."""
    calls: list[str] = []
    original = LocalStorageDevice._resolve_identity

    def counted(
        self: LocalStorageDevice, name: str
    ) -> os.stat_result | None:
        calls.append(name)
        return original(self, name)

    monkeypatch.setattr(LocalStorageDevice, "_resolve_identity", counted)
    return calls


@pytest.mark.parametrize("policy", ["eager", "GENERATION", None, 7])
def test_the_local_constructor_refuses_an_unknown_descriptor_policy_before_creating_a_root(
    tmp_path: Path, policy: object
) -> None:
    root = tmp_path / "database"

    with pytest.raises(GrafxConfigurationError) as refused:
        LocalStorageDevice(
            root,
            page_size=PAGE_SIZE,
            descriptor_revalidation=policy,  # type: ignore[arg-type]
        )

    assert refused.value.details["field"] == "descriptor_revalidation"
    assert not root.exists()


def test_strict_revalidates_every_warm_descriptor_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "database"
    _seed(root, (HEAP, b"heap"))
    with LocalStorageDevice(
        root, page_size=PAGE_SIZE, descriptor_revalidation="strict"
    ) as reader:
        assert reader.read_log(HEAP, 0, 4) == b"heap"  # cold admission proves the path
        calls = _count_warm_identity_proofs(monkeypatch)

        for _ in range(3):
            assert reader.read_log(HEAP, 0, 4) == b"heap"

        assert calls == [HEAP, HEAP, HEAP]


def test_generation_classification_is_closed_and_canonical(tmp_path: Path) -> None:
    with LocalStorageDevice(
        tmp_path / "database",
        page_size=PAGE_SIZE,
        descriptor_revalidation="generation",
    ) as device:
        amortized = {
            HEAP,
            CATALOG,
            INDEX,
            "index/_internal_7.idx",
            SEGMENT,
            "wal/999999999999.wal",
        }
        strict = {
            CONTROL,
            "control/writer.lease",
            "ledger/0001.entry",
            "bootstrap/heap.dat",
            "grafx.meta",
            "quarantine/heap.dat",
            "index_orphan/person_name.idx",
            "index/person-name.idx",
            "index/child/person_name.idx",
            "index/.idx",
            "wal/000000000000.wal",
            "wal/1.wal",
            "wal/000000000001.wal.tmp",
            "wal/child/000000000001.wal",
            "data/heap.dat",
            "future/file.bin",
        }

        assert all(device._uses_generation_revalidation(name) for name in amortized)
        assert not any(device._uses_generation_revalidation(name) for name in strict)


def test_generation_revalidates_one_warm_hit_after_each_global_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "database"
    _seed(root, (HEAP, b"heap"))
    with LocalStorageDevice(
        root, page_size=PAGE_SIZE, descriptor_revalidation="generation"
    ) as reader:
        assert reader.read_log(HEAP, 0, 4) == b"heap"
        calls = _count_warm_identity_proofs(monkeypatch)

        assert reader.read_log(HEAP, 0, 4) == b"heap"
        assert calls == []

        reader.invalidate_descriptor_identity(None)
        assert reader.read_log(HEAP, 0, 4) == b"heap"
        assert reader.read_log(HEAP, 0, 4) == b"heap"
        assert calls == [HEAP]

        reader.invalidate_descriptor_identity(None)
        assert reader.read_log(HEAP, 0, 4) == b"heap"
        assert reader.read_log(HEAP, 0, 4) == b"heap"
        assert calls == [HEAP, HEAP]


def test_generation_keeps_a_paged_descriptor_until_a_global_boundary(
    tmp_path: Path,
) -> None:
    """The opt-in tradeoff is observable: external replacement needs a Grafx boundary."""
    root = tmp_path / "database"
    _seed(root, (HEAP, b"old-heap"))
    with LocalStorageDevice(
        root, page_size=PAGE_SIZE, descriptor_revalidation="generation"
    ) as reader:
        assert reader.read_log(HEAP, 0, 8) == b"old-heap"
        with LocalStorageDevice(root, page_size=PAGE_SIZE) as publisher:
            publisher.create("heap.next")
            publisher.append_log("heap.next", b"new-heap")
            publisher.atomic_replace("heap.next", HEAP)

        # Grafx never republishes heap.dat this way while participants are live.  An external
        # tool can, and generation mode deliberately retains the old inode until a certified
        # global or directed refresh says the logical name may have moved.
        assert reader.read_log(HEAP, 0, 8) == b"old-heap"

        reader.invalidate_descriptor_identity(None)
        assert reader.read_log(HEAP, 0, 8) == b"new-heap"


def test_strict_observes_an_external_paged_replacement_on_the_next_hit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    _seed(root, (HEAP, b"old-heap"))
    with LocalStorageDevice(
        root, page_size=PAGE_SIZE, descriptor_revalidation="strict"
    ) as reader:
        assert reader.read_log(HEAP, 0, 8) == b"old-heap"
        with LocalStorageDevice(root, page_size=PAGE_SIZE) as publisher:
            publisher.create("heap.next")
            publisher.append_log("heap.next", b"new-heap")
            publisher.atomic_replace("heap.next", HEAP)

        assert reader.read_log(HEAP, 0, 8) == b"new-heap"


def test_generation_revalidates_only_the_name_at_a_targeted_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "database"
    _seed(root, (HEAP, b"heap"), (CATALOG, b"catalog"))
    with LocalStorageDevice(
        root, page_size=PAGE_SIZE, descriptor_revalidation="generation"
    ) as reader:
        assert reader.read_log(HEAP, 0, 4) == b"heap"
        assert reader.read_log(CATALOG, 0, 7) == b"catalog"
        calls = _count_warm_identity_proofs(monkeypatch)

        reader.invalidate_descriptor_identity(HEAP)
        assert reader.read_log(CATALOG, 0, 7) == b"catalog"
        assert reader.read_log(HEAP, 0, 4) == b"heap"

        assert calls == [HEAP]


def test_releasing_a_descriptor_forgets_its_generation_stamp(tmp_path: Path) -> None:
    root = tmp_path / "database"
    _seed(root, (HEAP, b"heap"))
    with LocalStorageDevice(
        root, page_size=PAGE_SIZE, descriptor_revalidation="generation"
    ) as device:
        assert device.read_log(HEAP, 0, 4) == b"heap"
        assert HEAP in device._descriptor_identity_generation

        device._release(HEAP)

        assert HEAP not in device._handles
        assert HEAP not in device._descriptor_identity_generation
        assert device.read_log(HEAP, 0, 4) == b"heap"
        assert HEAP in device._descriptor_identity_generation


def test_replacing_paged_names_forgets_both_generation_stamps(tmp_path: Path) -> None:
    root = tmp_path / "database"
    _seed(root, (HEAP, b"old!"), (CATALOG, b"new!"))
    with LocalStorageDevice(
        root, page_size=PAGE_SIZE, descriptor_revalidation="generation"
    ) as device:
        assert device.read_log(HEAP, 0, 4) == b"old!"
        assert device.read_log(CATALOG, 0, 4) == b"new!"
        assert set(device._descriptor_identity_generation) == {HEAP, CATALOG}

        device.atomic_replace(CATALOG, HEAP)

        assert CATALOG not in device._descriptor_identity_generation
        assert HEAP not in device._descriptor_identity_generation
        assert device.read_log(HEAP, 0, 4) == b"new!"


def test_evicting_a_descriptor_forgets_only_its_generation_stamp(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    _seed(root, (HEAP, b"heap"), (CATALOG, b"catalog"))
    with LocalStorageDevice(
        root,
        page_size=PAGE_SIZE,
        max_open_files=1,
        descriptor_revalidation="generation",
    ) as device:
        assert device.read_log(HEAP, 0, 4) == b"heap"
        assert set(device._descriptor_identity_generation) == {HEAP}

        assert device.read_log(CATALOG, 0, 7) == b"catalog"

        assert HEAP not in device._handles
        assert HEAP not in device._descriptor_identity_generation
        assert set(device._descriptor_identity_generation) == {CATALOG}


def test_generation_mode_still_observes_a_control_replacement_on_the_next_hit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    _seed(root, (CONTROL, b"old-control"))
    with LocalStorageDevice(
        root, page_size=PAGE_SIZE, descriptor_revalidation="generation"
    ) as reader:
        assert reader.read_log(CONTROL, 0, 11) == b"old-control"
        assert CONTROL not in reader._descriptor_identity_generation
        with LocalStorageDevice(root, page_size=PAGE_SIZE) as publisher:
            publisher.create("control/commit.state.next")
            publisher.append_log("control/commit.state.next", b"new-control")
            publisher.atomic_replace("control/commit.state.next", CONTROL)

        assert reader.read_log(CONTROL, 0, 11) == b"new-control"


@pytest.mark.parametrize("policy", ["strict", "generation"])
def test_two_slot_control_io_keeps_one_strict_proof_per_actual_descriptor_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    root = tmp_path / "database"
    with LocalStorageDevice(
        root,
        page_size=PAGE_SIZE,
        descriptor_revalidation=policy,  # type: ignore[arg-type]
    ) as device:
        store = TwoSlotControlRecordStore(
            device,
            file=CONTROL,
            record_kind=ControlRecordKind.COMMIT_STATE,
            database_uuid=DATABASE_UUID,
            file_nonce=17,
            temporary=CONTROL_TEMP,
        )
        assert store.publish(b"one") == 1
        assert store.read() is not None  # cold descriptor admission
        calls = _count_warm_identity_proofs(monkeypatch)
        observations = _count_exact_identity_observations(monkeypatch)

        observed = store.read()

        assert observed is not None
        assert observed.payload == b"one"
        assert observations == [CONTROL]
        assert calls == []

        calls.clear()
        observations.clear()
        assert store.publish(b"two") == 2
        assert observations == [CONTROL]
        assert calls == [CONTROL, CONTROL]


@pytest.mark.parametrize("policy", ["strict", "generation"])
def test_fused_control_read_never_calls_the_independent_exists_door(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    root = tmp_path / "database"
    _seed(root, (CONTROL, b"control"))
    with LocalStorageDevice(
        root,
        page_size=PAGE_SIZE,
        descriptor_revalidation=policy,  # type: ignore[arg-type]
    ) as device:
        assert device.read_log_if_exists(CONTROL, 0, 7) == b"control"

        def independent_exists_must_not_run(
            _self: LocalStorageDevice, _file: str
        ) -> bool:
            raise AssertionError("fused read regressed to exists + read_log")

        monkeypatch.setattr(
            LocalStorageDevice, "exists", independent_exists_must_not_run
        )
        assert device.read_log_if_exists(CONTROL, 0, 7) == b"control"


def test_fused_read_distinguishes_an_absent_name_from_a_present_empty_file(
    tmp_path: Path,
) -> None:
    with LocalStorageDevice(tmp_path / "database", page_size=PAGE_SIZE) as device:
        assert device.read_log_if_exists("control/absent.state", 0, 1) is None
        device.create("control/empty.state")
        assert device.read_log_if_exists("control/empty.state", 0, 1) == b""


def test_fused_read_refuses_a_case_only_alias(tmp_path: Path) -> None:
    root = tmp_path / "database"
    _seed(root, (CONTROL, b"control"))
    with LocalStorageDevice(root, page_size=PAGE_SIZE) as device:
        with pytest.raises(GrafxUnsupportedOperation) as refused:
            device.read_log_if_exists("CONTROL/commit.state", 0, 7)

    assert refused.value.details["reason"] == "case_collision"


@pytest.mark.parametrize("policy", ["strict", "generation"])
def test_fused_control_read_observes_an_atomic_replacement_on_the_next_call(
    tmp_path: Path,
    policy: str,
) -> None:
    root = tmp_path / "database"
    _seed(root, (CONTROL, b"old"))
    with LocalStorageDevice(
        root,
        page_size=PAGE_SIZE,
        descriptor_revalidation=policy,  # type: ignore[arg-type]
    ) as reader:
        assert reader.read_log_if_exists(CONTROL, 0, 3) == b"old"
        with LocalStorageDevice(root, page_size=PAGE_SIZE) as publisher:
            publisher.create("control/commit.state.next")
            publisher.append_log("control/commit.state.next", b"new")
            publisher.atomic_replace("control/commit.state.next", CONTROL)

        assert reader.read_log_if_exists(CONTROL, 0, 3) == b"new"


def test_fused_cold_open_fails_closed_when_the_observed_name_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "database"
    _seed(root, (CONTROL, b"control"))
    original = LocalStorageDevice._open_named_descriptor

    def remove_before_open(
        self: LocalStorageDevice,
        name: str,
        path: str,
        *,
        create_new: bool,
    ) -> int:
        Path(path).unlink()
        return original(self, name, path, create_new=create_new)

    with LocalStorageDevice(root, page_size=PAGE_SIZE) as reader:
        monkeypatch.setattr(
            LocalStorageDevice, "_open_named_descriptor", remove_before_open
        )
        with pytest.raises(GrafxStorageError):
            reader.read_log_if_exists(CONTROL, 0, 7)
