"""Every metric C6 emits comes from the frozen catalogue of CONTRACT.md section 9 (G7, TR-7)."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.engine.ledger_store import LEDGER_METRICS
from okto_grafx.engine.metrics_catalog import metric, metric_names
from okto_grafx.engine.quarantine import QUARANTINE_METRICS
from okto_grafx.engine.recovery_manager import RECOVERY_METRICS
from okto_grafx.engine.verifier import VERIFIER_METRICS

from .conftest import RecordingMetricsSink, Stack

EMITTED: tuple[str, ...] = tuple(
    descriptor.name
    for group in (LEDGER_METRICS, QUARANTINE_METRICS, RECOVERY_METRICS, VERIFIER_METRICS)
    for descriptor in group
)


def test_every_metric_this_component_emits_is_in_the_frozen_catalogue() -> None:
    catalogue = metric_names()
    for name in EMITTED:
        assert name in catalogue, name


def test_the_descriptors_are_the_catalogue_ones_and_not_copies() -> None:
    for group in (LEDGER_METRICS, QUARANTINE_METRICS, RECOVERY_METRICS, VERIFIER_METRICS):
        for descriptor in group:
            assert descriptor == metric(descriptor.name)


def test_a_name_outside_the_catalogue_cannot_be_looked_up() -> None:
    with pytest.raises(GrafxConfigurationError):
        metric("oktografx_recovery_feelings_total")


def test_the_component_registers_what_it_emits(stack: Stack) -> None:
    stack.recovery()
    stack.verifier()
    registered = {descriptor.name for descriptor in stack.metrics.registered}
    for name in EMITTED:
        assert name in registered, name


def test_a_disabled_sink_is_never_asked_to_record_anything(memory_device: object) -> None:
    from .conftest import build_stack

    quiet = RecordingMetricsSink(enabled=False)
    stack = build_stack(memory_device, metrics=quiet)
    stack.recovery().run()
    stack.verifier().verify("all")
    assert quiet.counters == {} and quiet.gauges == {}
    assert quiet.registered == []


def test_every_label_value_this_component_uses_is_one_the_descriptor_allows() -> None:
    from okto_grafx.engine.quarantine import QUARANTINE_ENTRIES
    from okto_grafx.engine.recovery_manager import (
        RECOVERIES_TOTAL,
        RECOVERY_DISCARDED_RECORDS_TOTAL,
    )
    from okto_grafx.engine.verifier import PAGE_KIND_LABELS

    outcomes = metric(RECOVERIES_TOTAL).labels[0]
    assert outcomes.allowed_values is not None
    from okto_grafx.domain.recovery.report import RECOVERY_OUTCOMES

    assert set(RECOVERY_OUTCOMES) == set(outcomes.allowed_values)

    origins = metric(RECOVERY_DISCARDED_RECORDS_TOTAL).labels[0]
    assert origins.allowed_values == frozenset({"reapplicable", "forensic"})

    kinds = metric("oktografx_checksum_verifications_total").labels[0]
    assert kinds.allowed_values is not None
    assert PAGE_KIND_LABELS["kind"] in kinds.allowed_values

    assert metric(QUARANTINE_ENTRIES).labels == ()
