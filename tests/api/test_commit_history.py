"""Public opt-in provenance consumption, independent of physical journal APIs."""

import pytest

from okto_grafx import CommitId, CommitMetadata, connect
from okto_grafx.errors import GrafxConfigurationError, GrafxUnsupportedOperation


def setup(db):
    db.ensure_identity_indexes()
    db.enable_commit_history()
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")


def test_public_metadata_lookup_and_pagination_retains_snapshot(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        with pytest.raises(GrafxUnsupportedOperation):
            db.commit_history()
        setup(db)
        with db.transaction(metadata=CommitMetadata(actor="test", reason="create")) as tx:
            tx.execute("CREATE (:P {id:1})")
        identity = CommitId(db.identity.database_uuid, tx.report.csn)
        assert db.lookup_commit(identity).metadata.actor == "test"
        with db.begin("read") as reader:
            first = reader.commit_history(limit=1)
            assert first.has_more
            with connect(root, page_size=512) as writer:
                with writer.begin(metadata=CommitMetadata(actor="other")) as tx2:
                    tx2.execute("CREATE (:P {id:2})")
                future = CommitId(db.identity.database_uuid, tx2.report.csn)
            assert reader.lookup_commit(future) is None
            tail = reader.commit_history(after=first.entries[-1].identity)
            assert tail.read_sequence == first.read_sequence
            assert not tail.has_more
            assert tail.entries[-1].identity == identity
        assert db.lookup_commit(future).metadata.actor == "other"
        before = db.commit_history()
        db.enable_commit_history()
        assert db.commit_history().entries == before.entries
        db.checkpoint()
    with connect(root, page_size=512, read_only=True) as db:
        assert db.lookup_commit(identity).metadata.reason == "create"
        assert db.commit_history().entries == before.entries
        with pytest.raises(GrafxUnsupportedOperation):
            db.enable_commit_history()


@pytest.mark.parametrize("limit", [True, 0, -1, 1001, 1.5, "2"])
def test_public_limit_admission(tmp_path, limit):
    with connect(tmp_path / "db") as db:
        with pytest.raises(GrafxConfigurationError):
            db.commit_history(limit=limit)


def test_foreign_identity_and_read_metadata_refused(tmp_path):
    with connect(tmp_path / "db") as db:
        setup(db)
        with pytest.raises(GrafxConfigurationError):
            db.lookup_commit(CommitId(b"x" * 16, 1))
        with pytest.raises(GrafxConfigurationError):
            db.begin("read", metadata=CommitMetadata(actor="not a write"))
        page = db.commit_history()
        assert db.lookup_commit(CommitId(db.identity.database_uuid, page.activation_sequence)) is None


@pytest.mark.parametrize("enabled", [False, True])
def test_metrics_parity_and_full_verify(tmp_path, enabled):
    with connect(tmp_path / "db", page_size=512, metrics="json" if enabled else "noop",
                 metrics_destination=str(tmp_path / "metrics.json") if enabled else None) as db:
        setup(db)
        metadata = CommitMetadata(actor="do-not-emit", attributes={"private": "value"})
        with db.begin(metadata=metadata) as tx:
            tx.execute("CREATE (:P {id:1})")
        history = db.commit_history()
        report = db.verify()
        assert report.clean
        assert {"commits.dir", "commits.dat"} <= set(report.files_checked)
        assert report.records_checked >= len(history.entries)
        if enabled:
            snapshot = db.snapshot_metrics()
            assert snapshot["oktografx_commits_with_metadata_total"]["samples"][0]["value"] == 1
            assert snapshot["oktografx_commit_metadata_bytes_total"]["samples"][0]["value"] == len(metadata.canonical_bytes)
            assert "do-not-emit" not in repr(snapshot)
        assert history.entries[-1].metadata == metadata


def test_full_verify_refuses_damaged_older_record_not_just_head(tmp_path):
    from okto_grafx.domain.page import Page
    with connect(tmp_path / "db", page_size=512) as db:
        setup(db)
        for i in range(12):
            with db.begin(metadata=CommitMetadata(actor="x" * 200)) as tx:
                tx.execute("CREATE (:P {id:$id})", {"id": i})
        db.checkpoint()
        assert db.verify().clean
        # Damage an early record but retain a valid enclosing page CRC. Latest
        # head alone cannot certify history; the full verifier must find this.
        page = Page.from_bytes(db._storage.read_page("commits.dat", 1))
        payload = bytearray(page.read_slot(1))
        payload[40] ^= 1
        page.update_slot(1, bytes(payload))
        db._storage.write_page("commits.dat", 1, page.to_bytes())
        report = db.verify()
        assert not report.clean
        assert any(finding.location.file == "commits.dir" for finding in report.findings)


def test_native_clock_ties_regression_and_invalid_clock_refuse_before_commit(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        setup(db)
        original = db._transactions._clock

        class Clock:
            value = 0.0

            def wall(self):
                return self.value

            def monotonic(self):
                return original.monotonic()

        clock = Clock()
        db._transactions._clock = clock
        for i, stamp in enumerate((0.0, 0.0, -1.0)):
            clock.value = stamp
            with db.begin(metadata=CommitMetadata(actor="clock")) as tx:
                tx.execute("CREATE (:P {id:$id})", {"id": i})
        entries = db.commit_history().entries[-3:]
        assert [entry.timing.observed_at.micros for entry in entries] == [0, 0, -1000000]
        assert all(entry.timing.clock_adjusted for entry in entries)
        assert entries[1].timing.ordered_at.micros == entries[0].timing.ordered_at.micros + 1
        assert entries[2].timing.ordered_at.micros == entries[1].timing.ordered_at.micros + 1
        sequence = entries[-1].identity.sequence
        clock.value = float("inf")
        with pytest.raises(GrafxConfigurationError):
            with db.begin() as tx:
                tx.execute("CREATE (:P {id:99})")
        assert db.commit_history().entries[-1].identity.sequence == sequence
        db._transactions._clock = original
        assert db.execute("MATCH (p:P) RETURN count(p)").rows == ((3,),)


@pytest.mark.parametrize("compression", [False, True])
@pytest.mark.parametrize("segment_bytes", [4096, 65536])
def test_large_metadata_cross_page_roll_and_reopen(tmp_path, compression, segment_bytes):
    from okto_grafx import MetadataLimits
    root = tmp_path / "db"
    metadata = CommitMetadata(
        attributes={str(i): "é" * 2000 for i in range(12)},
        limits=MetadataLimits(max_bytes=65536),
    )
    with connect(root, page_size=512, wal_segment_bytes=segment_bytes) as db:
        db.ensure_identity_indexes()
        if compression:
            db.enable_wal_page_compression()
        db.enable_commit_history()
        with db.begin(metadata=metadata) as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        identity = CommitId(db.identity.database_uuid, tx.report.csn)
        assert db.lookup_commit(identity).metadata == metadata
        assert db.verify().clean
        db.checkpoint()
    with connect(root, page_size=512, wal_segment_bytes=segment_bytes) as db:
        assert db.lookup_commit(identity).metadata == metadata
        assert db.verify().clean


def test_metadata_metric_failure_cannot_turn_commit_into_retry(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        setup(db)
        original = db._transactions._metrics

        class Sink:
            enabled = True
            failed = 0

            def __getattr__(self, name):
                return getattr(original, name)

            def set_gauge(self, name, value, labels=None):
                if name == "oktografx_commit_id_high_watermark_count":
                    self.failed += 1
                    raise RuntimeError("sink failure after durable commit")
                return original.set_gauge(name, value, labels)

        sink = Sink()
        db._transactions._metrics = sink
        with db.begin(metadata=CommitMetadata(actor="safe")) as tx:
            tx.execute("CREATE (:P {id:1})")
        assert tx.report.durable and tx.report.wrote and sink.failed == 1
        db._transactions._metrics = original
        assert db.lookup_commit(CommitId(db.identity.database_uuid, tx.report.csn)).metadata.actor == "safe"


def test_public_invalid_inputs_refuse_before_opening_participant(tmp_path, monkeypatch):
    with connect(tmp_path / "db") as db:
        def forbidden(*args, **kwargs):
            pytest.fail("invalid public input reached participant IO")
        monkeypatch.setattr(type(db._transactions), "begin", forbidden)
        monkeypatch.setattr(type(db._transactions), "_participant_descriptor_scope", forbidden)
        with pytest.raises(GrafxConfigurationError):
            db.lookup_commit("not a CommitId")
        with pytest.raises(GrafxConfigurationError):
            db.commit_history(limit=True)
        with pytest.raises(GrafxConfigurationError):
            with db.transaction("read", metadata=CommitMetadata(actor="invalid mode")):
                pytest.fail("read metadata was accepted")
