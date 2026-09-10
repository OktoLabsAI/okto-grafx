"""Pure optional temporal-tree image plans bound into native history batches."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from collections.abc import Callable

from okto_grafx.engine.system_history_index import HistoryAccessTree, _EMPTY
from okto_grafx.engine.system_history_store import HistoryChange, HistoryPageImage, _image, _corrupt

__all__ = ["PreparedHistoryIndex"]

_EXT = struct.Struct("<8sI32s")
_EXT_MAGIC = b"GXHYIR01"
_TRAILER = struct.Struct("<8s16sQI32sI32sI")
_TRAILER_MAGIC = b"GXHYIT01"
_INDEX_BLOCK_MAGIC = b"GXHYBL02"


def head_root(raw: bytes, base_size: int) -> tuple[int, bytes] | None:
    """Decode only the explicitly versioned optional head extension."""
    if len(raw) == base_size:
        return None
    if len(raw) != base_size + _EXT.size:
        raise _corrupt("head_length")
    magic, number, digest = _EXT.unpack_from(raw, base_size)
    if magic != _EXT_MAGIC or number < 1 or digest == bytes(32):
        raise _corrupt("index_root")
    return number, digest


def trailer(raw: bytes, identity: bytes, sequence: int) -> tuple[tuple[int, bytes], tuple[int, bytes], int]:
    """Validate an index transition descriptor; this does not prove its publication."""
    if len(raw) != _TRAILER.size:
        raise _corrupt("index_transition_length")
    magic, uuid, csn, old, old_hash, new, new_hash, count = _TRAILER.unpack(raw)
    if (magic != _TRAILER_MAGIC or uuid != identity or csn != sequence or new < 1
            or not 0 <= count < 2**32 or (old == 0) != (old_hash == bytes(32))):
        raise _corrupt("index_transition")
    return (old, old_hash), (new, new_hash), count


@dataclass(frozen=True, slots=True)
class PreparedHistoryIndex:
    """Captured predecessor pages and optional activation baseline; no persistent authority."""

    read: Callable[[str, int], bytes]
    previous_root: tuple[int, bytes] = _EMPTY
    baseline: tuple[tuple[int, tuple[HistoryChange, ...]], ...] = ()

    def bind(self, *, database_uuid: bytes, page_size: int, sequence: int,
             first_page: int, changes: tuple[HistoryChange, ...]) -> tuple[tuple[int, bytes], bytes, tuple[HistoryPageImage, ...]]:
        """Produce a canonical immutable index transition at the final native COMMIT."""
        tree = HistoryAccessTree(self.read, database_uuid=database_uuid, page_size=page_size,
                                 sequence=sequence, root=self.previous_root, first_page=first_page + 1,
                                 read_extent=first_page)
        if self.baseline:
            tree.bulk(tuple(((change.table.table_id, change.record_id, csn), change.encode())
                for csn, batch in (*self.baseline, (sequence, changes)) for change in batch))
        else:
            for change in changes:
                tree.insert((change.table.table_id, change.record_id, sequence), change.encode())
        # An empty writing COMMIT still carries its own transition descriptor;
        # activation always contains at least one historical schema event.
        marker = _TRAILER.pack(_TRAILER_MAGIC, database_uuid, sequence, *self.previous_root,
                               *tree.root, len(tree.images))
        images = (_image(page_size, first_page, sequence, marker), *(
            HistoryPageImage("system-history.dat", number, raw) for number, raw in tree.images.items()))
        return tree.root, hashlib.sha256(marker).digest(), images
