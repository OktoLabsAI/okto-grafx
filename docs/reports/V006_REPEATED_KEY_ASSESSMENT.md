# Conditional repeated-key layout assessment

Date: September 10, 2026. `feature/v0.0.6`, working tree after base `4ee4d2e`.
This is item 7's bounded operation-count assessment, **not an implemented new
posting format or a claim that the performance issue is solved**.

Subsequent implementation: explicit [posting hash](../POSTING_HASH.md) now shares
physical keys per page. The observations below remain the original HASH baseline;
they are not the measurements of the new layout. See the current roadmap for
implementation and regression status.

## Measured current behavior

`tests/api/test_repeated_key_evidence.py` creates two temporary graphs with 512-byte
pages and an exact index containing one repeated key. It verifies complete candidate
sets, cold/warm decoding and native query counts. No timing ratio is an acceptance gate.

| Stored occurrences | Cold entry decoder calls | Warm entry decoder calls | Bucket pages | Candidates preserved |
| ---: | ---: | ---: | ---: | ---: |
| 60 | 60 | 0 | 5 | 60 |
| 240 | 240 | 0 | 20 | 240 |

Both report dominant-key fraction 1.0 and `inspect_key_skew`. Assisted rehash correctly
does not manufacture a growth operation: one identical key still hashes to one
bucket regardless of directory size. The existing hot-key and distribution tests
also cover foreign writes, snapshot visibility, changed-page refusal and budgets.

## Interpretation and remaining implementation

- A long repeated-key chain is real: physical page work grows with its occurrences.
  Increasing bucket count cannot divide identical keys.
- Existing exact-image key-page memoization already eliminates repeated entry
  decoding in the warm sample. Replacing it with another authority cache would not
  address the measured residual page traversal.
- Returning all matching occurrences requires output proportional to their count.
  A compressed/grouped posting representation could reduce storage/page/decoding
  overhead, but cannot honestly promise constant-time complete enumeration.
- The native commit/replay path already has bounded hot-bucket preparation and
  target-location reuse. Do not claim a new elimination of quadratic work that this
  path has already eliminated within its supported bounds.

The new physical posting layout remains **unimplemented**. These samples isolate
the residual and establish a reproducible baseline; they do not select a durable
representation or quantify end-to-end write/read gains for it. The next item-7 work
is the representation plus native recovery/verification contract, not a new
authorization request or a marginal performance threshold. The full continuation
still includes item 8's separate durable temporal-history implementation.

Source anchors: `IndexStore._scan_bucket`, `_stable_candidates` and
`_prepare_common_replay_hot_bucket` in `engine/index_manager.py`;
`engine/index_distribution.py`; `tests/api/test_hot_key_pages.py`;
`tests/api/test_large_hash_distribution.py`.
