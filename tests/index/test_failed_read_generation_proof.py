"""An operation failure is authoritative only inside a proved stable index view."""
import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError, GrafxUnsupportedOperation
from okto_grafx.engine.index_manager import INDEX_READ_RETRY_BUDGET
from .conftest import build_database, cold_view


@pytest.mark.parametrize('kind', [GrafxUnsupportedOperation, GrafxCorruptionDetected])
def test_stable_failure_keeps_original_exception(kind):
    db = build_database()
    failure = kind('original stable refusal', field='injected')
    calls = []

    def fail(certificate):
        calls.append(certificate)
        raise failure

    with pytest.raises(kind) as caught:
        db.exact._stable_view(0, fail)
    assert caught.value is failure
    assert len(calls) == 1


def test_foreign_change_during_failed_attempt_retries_fresh():
    writer = build_database()
    reader = cold_view(writer)
    reader.exact._stable_view(0, lambda cert: cert)
    calls = []
    marker = object()

    def read(certificate):
        calls.append(certificate)
        if len(calls) == 1:
            writer.exact.advance_built_through(100)
            raise GrafxUnsupportedOperation('mixed-generation slot', field='slot')
        return marker

    assert reader.exact._stable_view(0, read) is marker
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert calls[1].header.built_through_lsn == 100


def test_repeated_failed_generation_changes_keep_existing_retry_bound():
    writer = build_database()
    reader = cold_view(writer)
    calls = []

    def read(certificate):
        calls.append(certificate)
        writer.exact.advance_built_through(100 + len(calls))
        raise GrafxUnsupportedOperation('mixed view', field='slot')

    with pytest.raises(GrafxIndexError) as caught:
        reader.exact._stable_view(0, read)
    assert caught.value.details['field'] == 'index_view_changed'
    assert caught.value.details['attempts'] == INDEX_READ_RETRY_BUDGET + 1
    assert caught.value.retryable
    assert len(calls) == INDEX_READ_RETRY_BUDGET + 1


@pytest.mark.parametrize('failure', [RuntimeError('host'), KeyboardInterrupt(), SystemExit(1)])
def test_host_failure_and_process_control_are_not_retried(failure):
    writer = build_database()
    reader = cold_view(writer)
    calls = []

    def read(certificate):
        calls.append(certificate)
        writer.exact.advance_built_through(100)
        raise failure

    with pytest.raises(type(failure)) as caught:
        reader.exact._stable_view(0, read)
    assert caught.value is failure
    assert len(calls) == 1
