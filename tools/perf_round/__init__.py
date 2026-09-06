"""Versioned instruments of performance round 0.0.2 (GRAFX_PERFORMANCE_ROUND_FINAL.md).

The modules share one receipt format and one set of fail-closed guards:

* ``receipt``       -- provenance receipt, inventory hashes, data-home and declared-copy guards;
* ``board_copy``    -- copy a board directory and PROVE source and copy byte-identical;
* ``baseline_runs`` -- N >= 3 independent runs of an existing instrument, per mode, with dispersion.
* ``profile_pulse_card`` -- one external py-spy profile of a spawned disposable Pulse replay.
* ``pulse_graph_census_once`` -- one authenticated, untimed census on a disposable clone.

Nothing here is a gate, nothing here opens the default data home, and no raw output produced here
is an official result: every receipt says ``official: false`` and carries the machine sample
that lets a reader judge whether the box was loaded. The P0.3 instruments are ready, but their
post-drain execution and the P0.4 same-code series remain separate work.
"""

from __future__ import annotations

PACKAGE_VERSION = "0.0.2-p0.3"
