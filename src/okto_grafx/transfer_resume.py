"""Operator-owned resumable import workspace, never a partially published destination."""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.api import connect
from okto_grafx.transfer import (
    TransferLimits,
    TransferReport,
    RecordIdMapping,
    _manifest,
    _rows,
    _schema,
    _refuse,
    _remap,
    _import_into,
    _report,
    _json,
    _promote,
)
from okto_grafx.domain.model.schema import TableDef, EmbeddingSpaceDef
from okto_grafx.domain.model.value import encode_values
from okto_grafx.engine.database import Database

__all__: list[str] = []


def validated_inventory(
    db: Database,
    storage: LocalStorageDevice,
    manifest: dict,
    tables: tuple[TableDef, ...],
    spaces: tuple[EmbeddingSpaceDef, ...],
    limits: TransferLimits,
) -> dict[str, tuple[int, ...]]:
    """Prove every existing row is an exact committed source prefix before appending."""
    catalog = db._catalog.catalog
    actual = _schema(catalog)
    expected = manifest["schema"]

    def logical_table(item: dict) -> dict:
        """Physical table IDs are intentionally remapped."""
        return {k: v for k, v in item.items() if k != "table_id"}

    if sorted(map(logical_table, actual["tables"]), key=lambda x: x["name"]) != sorted(
        map(logical_table, expected["tables"]), key=lambda x: x["name"]
    ):
        raise _refuse("resume_schema_mismatch")
    if {s.name for s in spaces} != {s.name for s in catalog.spaces()}:
        raise _refuse("resume_spaces_mismatch")
    for source in spaces:
        target = catalog.space(source.name)
        if source.is_active and not target.is_active:
            raise _refuse("resume_spaces_mismatch")
        if any(
            getattr(source, k) != getattr(target, k)
            for k in ("dimension", "metric", "normalized", "storage_dtype")
        ):
            raise _refuse("resume_spaces_mismatch")
    expected_indexes = {i["name"]: i for i in expected["indexes"]}
    for item in actual["indexes"]:
        # Cardinality hints are informational; import restores explicit bucket counts.
        wanted = expected_indexes.get(item["name"])
        if wanted is None or _json(
            {k: v for k, v in item.items() if k != "expected_cardinality"}
        ) != _json({k: v for k, v in wanted.items() if k != "expected_cardinality"}):
            raise _refuse("resume_index_mismatch")
    inventory = {}
    observed = {}
    count = 0
    with db.begin("read") as reader:
        for table in tables:
            rows = []
            cursor = None
            while True:
                page = reader.scan_rows_v1(
                    table.name, limit=limits.batch_rows, cursor=cursor
                )
                for row in page.rows:
                    count += 1
                    if count > limits.max_rows:
                        raise _refuse("resume_row_budget")
                    rows.append(row.record_id)
                    observed[table.name, row.record_id] = hashlib.sha256(
                        encode_values(row.values)
                    ).digest()
                cursor = page.next_cursor
                if cursor is None:
                    break
            inventory[table.name] = tuple(sorted(rows))
    identity = {}
    space_map = {s.space_id: catalog.space(s.name).space_id for s in spaces}
    objects = {t.name: o for t, o in zip(tables, manifest["objects"], strict=True)}
    for table in sorted(tables, key=lambda t: t.kind != "node"):
        prior = inventory[table.name]
        source_count = 0
        for ordinal, (rid, values) in enumerate(
            _rows(storage, objects[table.name], table, limits)
        ):
            source_count += 1
            if ordinal >= len(prior):
                continue
            target_id = prior[ordinal]
            identity[table.name, rid] = target_id
            values = _remap(values, space_map)
            if table.kind == "rel":
                try:
                    values = (
                        identity[table.from_table, values[0]],
                        identity[table.to_table, values[1]],
                        *values[2:],
                    )
                except KeyError as error:
                    raise _refuse("resume_endpoint_prefix_missing") from error
            if (
                observed.pop((table.name, target_id))
                != hashlib.sha256(encode_values(values)).digest()
            ):
                raise _refuse("resume_row_mismatch")
        if len(prior) > source_count:
            raise _refuse("resume_extra_rows")
    if observed:
        raise _refuse("resume_extra_rows")
    return inventory


def _save(storage: LocalStorageDevice, state: dict) -> None:
    """Barrier private metadata then atomically replace its complete small manifest."""
    name = "resume.next"
    storage.create(name, exclusive=False)
    # Truncate only this owned scratch record via the storage adapter.
    storage.truncate_log(name, 0)
    storage.append_log(name, _json(state))
    storage.durable_barrier(name)
    storage.atomic_replace(name, "resume.json")
    storage.durable_barrier("resume.json")


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    """Refuse ambiguous JSON fields in private resume metadata."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise _refuse("resume_duplicate_field")
        result[key] = value
    return result


def resume_import(
    root: Path,
    destination: str | os.PathLike[str],
    directory: str | os.PathLike[str],
    limits: TransferLimits,
) -> TransferReport:
    """Resume only the same manifest/destination under a separate workspace lock."""
    raw_work = Path(directory).absolute()
    raw_target = Path(destination).absolute()
    if raw_work.is_symlink() or raw_target.is_symlink():
        raise _refuse("resume_symlink")
    work = raw_work.parent.resolve(strict=True) / raw_work.name
    target = raw_target.parent.resolve(strict=True) / raw_target.name
    paths = (root, work, target)
    if any(
        a == b or a in b.parents or b in a.parents
        for i, a in enumerate(paths)
        for b in paths[i + 1 :]
    ):
        raise _refuse("resume_overlapping_paths")
    if work.drive.lower() != target.drive.lower():
        raise _refuse("resume_cross_volume")
    with LocalStorageDevice(root, create_root=False) as storage:
        manifest, raw, tables, spaces = _manifest(storage, limits)
        for item, table in zip(manifest["objects"], tables, strict=True):
            for _ in _rows(storage, item, table, limits):
                pass
        expected = dict(
            format="grafx-resume-1",
            artifact=hashlib.sha256(raw).hexdigest(),
            destination=str(target),
            source_uuid=manifest["source_uuid"],
        )
        if not work.exists():
            work.mkdir()
        if any(
            p.name not in ("control", "database", "resume.json", "resume.next")
            or p.is_symlink()
            for p in work.iterdir()
        ):
            raise _refuse("resume_unowned_workspace")
        with LocalStorageDevice(work, create_root=False) as state_storage:
            lock = LocalProcessCoordinator(
                state_storage,
                SystemClock(),
                lock_directory=str(work / "control"),
                namespace=str(work),
            )
            with lock.exclusive("logical_import", timeout=0):
                staging = work / "database"
                if any(
                    p.name not in ("control", "database", "resume.json", "resume.next")
                    or p.is_symlink()
                    for p in work.iterdir()
                ):
                    raise _refuse("resume_unowned_workspace")
                if state_storage.exists("resume.json"):
                    size = state_storage.file_size("resume.json")
                    if not 0 < size <= 16384:
                        raise _refuse("resume_manifest_size")
                    try:
                        state = json.loads(
                            state_storage.read_log("resume.json", 0, size),
                            object_pairs_hook=_unique_fields,
                        )
                    except (ValueError, UnicodeError) as error:
                        raise _refuse("resume_manifest_json") from error
                    if (
                        type(state) is not dict
                        or set(state) != set(expected) | {"uuid", "phase", "lsn"}
                        or any(state.get(k) != v for k, v in expected.items())
                    ):
                        raise _refuse("resume_manifest_mismatch")
                    if state["phase"] not in ("loading", "ready"):
                        raise _refuse("resume_phase")
                    if (
                        type(state["uuid"]) is not str
                        or len(state["uuid"]) != 32
                        or any(c not in "0123456789abcdef" for c in state["uuid"])
                        or type(state["lsn"]) is not int
                        or not 0 <= state["lsn"] < 2**64 - 1
                    ):
                        raise _refuse("resume_identity_metadata")
                else:
                    if (
                        staging.exists()
                        or target.exists()
                        or state_storage.exists("resume.next")
                    ):
                        raise _refuse("resume_unowned_destination")
                    # A crash before this metadata exists leaves only a fresh empty
                    # private store; refusal on retry preserves that ambiguous workspace.
                    with connect(staging) as db:
                        state = {
                            **expected,
                            "uuid": db.identity.database_uuid.hex(),
                            "phase": "loading",
                            "lsn": 0,
                        }
                        db.checkpoint()
                    _save(state_storage, state)
                if target.exists():
                    if state["phase"] != "ready" or staging.exists():
                        raise _refuse("destination_exists")
                    path = target
                    readonly = True
                else:
                    if not staging.is_dir() or staging.is_symlink():
                        raise _refuse("resume_missing_database")
                    path = staging
                    readonly = False
                with connect(path, read_only=readonly) as db:
                    if (
                        db.identity.database_uuid.hex() != state["uuid"]
                        or state["uuid"] == manifest["source_uuid"]
                    ):
                        raise _refuse("resume_identity_mismatch")
                    if readonly:
                        if (
                            db._transactions.published_state().last_committed_lsn
                            != state["lsn"]
                        ):
                            raise _refuse("resume_published_target_changed")
                        inventory = validated_inventory(
                            db, storage, manifest, tables, spaces, limits
                        )
                        mapping = []
                        objects = {
                            t.name: o
                            for t, o in zip(tables, manifest["objects"], strict=True)
                        }
                        for t in sorted(tables, key=lambda t: t.kind != "node"):
                            pairs = _rows(storage, objects[t.name], t, limits)
                            if objects[t.name]["rows"] != len(inventory[t.name]):
                                raise _refuse("resume_incomplete_publication")
                            mapping.extend(
                                RecordIdMapping(t.name, rid, target_id)
                                for (rid, _), target_id in zip(
                                    pairs, inventory[t.name], strict=True
                                )
                            )
                        if db.verify("all").findings:
                            raise _refuse("resume_verification_failed")
                        return _report(
                            target, manifest, raw, state["uuid"], tuple(mapping)
                        )
                    mapping = _import_into(
                        db, storage, manifest, tables, spaces, limits, resume=True
                    )
                    db.checkpoint()
                    state["phase"] = "ready"
                    state["lsn"] = db._transactions.published_state().last_committed_lsn
                with connect(staging, read_only=True) as db:
                    if (
                        db.identity.database_uuid.hex() != state["uuid"]
                        or db.verify("all").findings
                    ):
                        raise _refuse("resume_reopen_failed")
                _save(state_storage, state)
                _promote(staging, target)
                return _report(target, manifest, raw, state["uuid"], mapping)
