# RF01 structural qualification profile

`tests/pulse/rf01_structural_evidence.py` is the immutable Pulse unittest entry
for RF01's revised, non-temporal acceptance scenario. Register the profile for
that scenario and its existing Grafx source reference, then execute it against
the complete Git revision. The useful selectors are:

```text
tests/pulse/rf01_structural_evidence.py::RF01StructuralEvidence.test_structural_qualification
tests/pulse/rf01_structural_evidence.py::RF01StructuralEvidence.test_memory_retention_and_spill_cleanup
tests/bench/test_rf01_evidence.py::RF01Evidence.test_compatibility_functional
tests/bench/test_rf01_evidence.py::RF01Evidence.test_physical_scan
tests/bench/test_rf01_evidence.py::RF01Evidence.test_fallback_regimes
```

The first selector checks seven real product controls. It compares exact result
hashes with independent fixture oracles and requires actual physical node/edge
work, index probes, certificate attempts and landing batches. Landing runs
without a query quota to exercise the batch path. A separate positive-quota
landing checks the scalar memory guard, memory peak and retained baseline. A
write control requires a successful commit ACK, fresh recovery, read-only reopen
and a clean integrity verification. The second selector reruns all four
termination cases from the committed quota/retention tests: exhaustion, early
close, cancellation and budget error, with no retained logical allocation or
spill files. The remaining selectors cover metrics sink equivalence, injected
recording failures, physical accounting and index fallback.

The profile may use a 120-second Pulse timeout; the complete selector set is
expected to finish well within it on the current host, but the host registration
is authoritative. The local structured report can be produced with
`python -m bench.harness.structural_qualification` from this checkout. It emits
only result hashes, counters and logical-memory observations, and explicitly
sets `latency` to `unqualified`. It does not compare elapsed times, establish a
percentage improvement, or certify the historical temporal campaign.
