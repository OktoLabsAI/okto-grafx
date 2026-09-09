# Bounded prefix postings — continuation after 69ed311

Approved item 6. Explicit `TextIndexOptions(prefix_max_characters=N)`, N=1..32,
adds distinct prefix postings for every analyzed term, at character boundaries
1..min(N, len(term)); zero remains byte-identical legacy behavior. Prefix bytes
are the frozen analyzer's UTF-8 tokens, not Python locale/casefold behavior.

Persistent derivation `fulltext_v4_` contains the existing analyzer header and
field-weight bytes followed by three u8 values: statistics mode (0=wal,1=durable),
history capacity (0..32 with existing rules), prefix character ceiling (1..32).
Required capability `fulltext_prefixes_v1` is bit 11, depending on full-text bit 5.
Statistics bits 6/8 remain required when applicable. Default v1/v2/v3 identities
are unchanged. Unsupported readers must refuse before use.

Keys 0x00 (length summary) and 0x01 (whole term) keep their meaning. A 0x02 key
contains one distinct UTF-8 prefix per row version. It uses the same logical index
WAL entries, quota accounting, COMMIT reducer, immutable generation, verification,
backup and logical-transfer contracts as whole terms. Writes can cost more;
existing transaction/index-build limits still refuse rather than truncate.
At most 65,536 distinct prefixes per document are admitted, checked before adding
the next prefix. Overflow is a typed query-budget refusal, never partial indexing.

`search_text(prefix=True)` applies prefix semantics to every analyzed query token
(OR, as ordinary lexical search). A query token longer than the configured prefix
ceiling refuses; no heap scan is substituted. Prefix postings yield snapshot-
visible candidate terms, independently checked against heap fields. At most
`TextSearchLimits.max_expanded_terms` distinct terms (default 128) are expanded.
Each complete term uses ordinary exact postings/BM25 document frequency; returned
matched_terms are actual whole terms. Work/memory/deadline/cancellation limits and
complete pre/post certification cover expansion and ranking together. No partial
answer, wildcard grammar, phrase search, autocomplete ordering or fuzzy matching.

Required evidence: exact-term oracle parity, expansion refusal, old/current reader
isolation, rollback/update/delete/rebuild, malformed/capability refusal and native
crash/replay plus transfer/backup. Implementation acceptance is recorded in the
[round receipt](../reports/V005_AFTER_69ED311.md), not inferred from this spec.
