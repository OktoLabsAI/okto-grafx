# Grafx 0.0.5 PyPI publication

Published with explicit user authorization on September 10, 2026:
[okto-grafx 0.0.5](https://pypi.org/project/okto-grafx/0.0.5/).

- Artifact: `okto_grafx-0.0.5-py3-none-any.whl` (universal wheel only).
- SHA256: `4f09d7e3ba1b8c7b716aa0b278bea2f39428db68fea71b27f56521f3b4bffcc5`.
- Python requirement: `>=3.11`.
- Base dependencies: `numpy>=1.24`, `google-crc32c>=1.5`.
- `[accel]` remains a compatibility alias; default codec/vector math are NumPy.
- The upload contains Grafx only, not Pulse.

The artifact is exactly the wheel already validated and installed in both local
Pulse 0.3.3 environments. Immediately before upload, all 186 package files matched
the current source, `twine check` passed, and PyPI confirmed that 0.0.5 did not yet
exist. After upload, the version-specific PyPI JSON endpoint confirmed the exact
filename, SHA256, Python requirement and base dependencies.

Previous verification includes targeted regression, pure/NumPy page interchange
and snapshot isolation, staged application/Settings composition, durable writes,
recovery, generation promotion, vector search, idempotency and reopen. See the
[compatibility matrix](../V005_COMPATIBILITY.md) and local artifacts under
`.grafx-tmp/accel-default/`. This publication is not a claim of additional full-suite
or cross-platform tests in the publication turn.

No Git commit, push, merge or tag was performed as part of this request. The wheel
was frozen before publication: its README metadata retains the pre-publication
source wording; the repository README and this receipt now record publication.
No token was saved in repository files or package artifacts.
