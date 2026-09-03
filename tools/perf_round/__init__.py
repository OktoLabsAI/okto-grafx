"""Versioned instruments of performance round 0.0.2 (GRAFX_PERFORMANCE_ROUND_FINAL.md, P0.2).

Three small modules that provide the initial P0.2 primitives, sharing one receipt format and one
set of fail-closed guards:

* ``receipt``       -- provenance receipt, inventory hashes, data-home and declared-copy guards;
* ``board_copy``    -- copy a board directory and PROVE source and copy byte-identical;
* ``baseline_runs`` -- N >= 3 independent runs of an existing instrument, per mode, with dispersion.

Nothing here is a gate, nothing here opens the default data home, and no raw output produced here
is an official result: every receipt says ``official: false`` and carries the machine sample
that lets a reader judge whether the box was loaded. P0.2 closes only when the concrete post-drain
driver used for the P0.3/P0.4 measurements is also versioned with its parameters and output schema.
"""

from __future__ import annotations

PACKAGE_VERSION = "0.0.2-p0.2"
