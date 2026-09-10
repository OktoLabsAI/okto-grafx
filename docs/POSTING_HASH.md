# Repeated-key posting indexes (0.0.6 development)

An explicit equality index can store repeated keys once per page:

```python
db.create_index("status_postings", "Task", ("status",),
                layout="posting_hash", bucket_count=64)
distribution = db.index_distribution("status_postings")
```

`create_index` has the same arguments, transaction/generation publication and
return contract as other property indexes. This is an additional `layout` value,
not a new connection setting. Node and relationship property columns are eligible;
automatic identity, ordered, proximity/vector and full-text indexes are not.
Queries select the index through the existing planner; no query syntax changes.

Use it for equality workloads with frequent repeated, especially long, keys.
The deterministic 512-byte-page example stores 60 repetitions of an 80-character
string in **4 bucket pages**. This is physical work evidence, not a wall-clock
benchmark. Full enumeration still costs O(output + bucket pages). Unique keys can
cost more because their dictionary entries are not shared. Do not choose this as
a universal replacement for hash, ordered or full-text search.
The store now memoizes complete successful decodes keyed by **identical complete
canonical page bytes**, not page address or LSN alone. The LRU retains at most
64 images and a conservative 1 MiB accounting per posting-index handle; oversized
pages decode normally without caching. These fixed internal limits add no
connection options. Outputs are immutable, failures are not cached, and changes
at the same LSN, WAL application, slot reuse and page reload cannot reuse a stale
decode. Native CRC admission, generation/certificate checks and heap visibility
remain independent and mandatory. This is decoded structure, not authority.
Tests count one decode for repeated equal bytes and new validation after same-LSN
mutation; this is work reduction, not a claimed end-to-end latency ratio.

Reference postings are 19 bytes plus the native slot directory; each distinct key
also needs a dictionary slot per page. Native heap validation remains mandatory
for every candidate, including old snapshots, updates and deletes. The layout
does not confer read authority, retain MVCC forever or alter either OCC check.

The live same-key INSERT batch path validates membership once, for 2–4,096
effects and up to 512 bucket pages. Other batches, rebuild and recovery retain
the scalar protocol. These internal bounds are not configurable and exceeding
them selects the correct fallback, not an incomplete result. No general write
speedup is promised. Existing transaction, query and distribution budgets apply.

Rebuild/rehash, verification, checkpoint, physical backup/restore and logical
transfer use their existing APIs. Logical transfer preserves the declared layout
and rebuilds physical postings. Existing indexes are never converted silently.
Dropping an index does not remove the required catalog capability. Old builds
without bit 14 refuse the store; there is no in-place downgrade.

Invalid configuration uses `GrafxIndexError`; damaged dictionary/posting framing
or references uses `GrafxCorruptionDetected`; unknown required formats use
`GrafxSchemaVersionMismatch`. Existing conflict, budget, stale-index and uncertain
durable-outcome errors retain their contracts; callers must not blindly retry an
uncertain commit.

See [format and recovery contract](specs/POSTING_HASH_V1.md),
[index operations](INDEXES_AND_VECTORS.md) and [roadmap](../ROADMAP.md).
