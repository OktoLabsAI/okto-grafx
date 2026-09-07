# Performance round 0.0.4 — finite execution plan

Latest native read-path follow-up (2026-09-07): the existing unused-vector
opportunity now covers the orphan audit's OPTIONAL degree pipeline when no
expression consumes its target bindings. Full validation/certificates remain.
The isolated 953-row query preserved its ordered digest; samples were
2.40–2.54 s full versus 2.22–2.30 s candidate, not a whole-UI speedup claim.
Affected/adjacent slices passed 107 and 87 tests; the final overlapping 14-test
slice also proves lower metered retention. Details and deployment boundary:
`docs/OPTIONAL_DEGREE_VECTOR_LANDINGS_0_0_4.md`. This does not reopen the rejected
aggregate node projection, remove Health checks or add an authority cache.

This document is the execution authority for the Grafx 0.0.4 performance round. It consolidates
Claude's measured surveys, Codex's adversarial review, and direct measurements of the current
Okto Pulse workload. It is deliberately finite: later profiling may reorder or reject an item,
but it does not add work to the round unless it exposes a correctness defect or an unbounded
complexity cliff on an already selected path.

Branch: `feature/v0.0.4`

Base: `ea4b5ff` (`0.0.3`)

Package version: `0.0.4`

## Non-negotiable invariants

Every optimization must preserve:

- independent multi-process and multi-thread readers and writers;
- snapshot isolation, both OCC validations, and writer fencing;
- WAL order, acknowledged durability, recovery and restore;
- catalog, heap and index consistency, including fail-closed corruption checks;
- the public result, error, pagination, ordering and budget semantics;
- bounded memory with deterministic refusal or canonical fallback.

Performance evidence never authorizes weakening one of these properties. Grafx-specific behavior
in Okto Pulse belongs to Community adapters and composition roots; Pulse Core remains backend
agnostic.

## Evidence used for selection

The evidence set is:

- `FABLE_PERFORMANCE_GRAFX.md`;
- `.grafx-tmp/levantamento2/LEVANTAMENTO_5.md`;
- `.grafx-tmp/levantamento2/L5_ADENDO_MEMORIA_QUENTE.md`;
- `.grafx-tmp/levantamento2/LEVANTAMENTO_6.md`;
- `.grafx-tmp/levantamento2/LEVANTAMENTO_6_TECNICAS.md`;
- the previous execution record in `docs/PERFORMANCE_ROUND_0_0_3.md`;
- direct Grafx and installed-Pulse measurements below.

On the real board database, with Pulse stopped, a read-only Grafx handle measured:

| Operation | Observed time/result |
|---|---:|
| Read-only open | 0.925 s |
| First 500-node page | 0.667 s; 2,069 rows scanned |
| Second 500-node cursor page | 0.385 s; 2,069 rows scanned |
| 66 relationship statements, 770 rows, cold | 1.833 s |
| Same relationship batch, warm | 1.100 s |
| Two 500-node reads sequentially | 1.603 s |
| Two 500-node reads on independent handles | 1.492 s |
| Read during a public writer transaction | 0.671 s |

After the Community integration stopped executing blocking graph work on the async event loop,
two concurrent graph REST requests took 4.32 s and 7.92 s while `/boards` still completed in
0.22 s. The event-loop serialization is closed; the remaining overlap is engine CPU/GIL and
per-statement work.

## Adversarial corrections to the surveys

1. The original KGRUN-2 estimate does not apply to the production predicate. Seven repeated
   `coalesce` subexpressions fall back to the oracle, and a naive prototype ignored
   `row.computed`. The selected design is closure-based evaluation plus a `coalesce` leaf and
   lazy per-row common-subexpression elimination (EXEC-CSE), with an explicit computed-row
   guard. Measured opportunity: 19.6–20.3% of the KG page and 17.3% of vector search.
2. A 64 MiB buffer pool cannot hold the eager directory of the Pulse schema's indexes:
   162 indexes × 65 pages = 10,530 pages, versus 8,192 frames. A 256 MiB budget is therefore a
   consumer configuration candidate, not a new engine default.
3. CONCUR-2 has the largest measured upside, but also changes the granularity of physical
   identity proof. It is not Wave 1 work. It requires a formal equivalence proof and hostile
   multi-process tests before promotion.
4. `vector_math="auto"` currently selects the pure implementation. NumPy measured 9.2% faster
   for the vector workload, but changing the default is a determinism policy decision, not a
   transparent code optimization.
5. KGRUN-6 has zero value on the measured denominator because the accelerated port already runs.
   The useful decode target is skipping the 39 columns the consumer did not request.
6. Building indexes at the end, operator batching that changes error timing, larger pages,
   Bloom filters, quantization and PyPy were measured as neutral, slower or semantically unsafe;
   they are not implementation targets for this round.

## Newly identified opportunities

### CURSOR-1 — ordered cursor access

Each 500-node page scanned all 2,069 visible rows. Walking all pages is therefore `O(P*N)`, even
though a cursor is supplied. An ordered `(created_at, id)` access path, or a generation-fenced
cursor materialization, could make continuation proportional to the remaining page. This is a
structural candidate: record and design after Wave 2, do not improvise a format change in Wave 1.

### BATCH-REL-1 — shared preparation for relationship-table batches

The warm relationship batch still costs about 1.10 s for only 771 scanned rows across 66
statements. The transactional primary-key memo may remove much of that fixed work. Re-profile
after STO-M1; only then consider a batch API that shares a snapshot and statement preparation
while preserving each statement's independent result and error boundary.

### CONC-CPU-1 — CPU/GIL after legitimate I/O concurrency

Independent Grafx handles no longer block unrelated Pulse HTTP work, but two heavy graph reads do
not scale linearly. EXEC-CSE and decode/projection work therefore precede adding more application
parallelism. Process pools or a native accelerator are not selected in Wave 1.

## Fixed implementation order

### Wave 0 — release line and existing transaction-local PK work

Status: **implemented and pushed** (`10b7520`).

- keep the existing dirty-table automatic primary-key index overlay;
- prove insert/update/key-change/delete and pending-reference containment;
- prove two endpoint seeks do not fall back to a node-table scan;
- bump package and README versions to `0.0.4`;
- do not overwrite unrelated pre-existing worktree changes.

The focused baseline passes 65 primary-key/dirty-table tests. The full query slice reached 100%;
one instrumentation-only expectation was updated from one unrelated landing decode to zero after
the new exact dirty-table seek, and its focused rerun passed. Ruff and `git diff --check` pass.

### Wave 1 — highest return without protocol changes

Engine/query lane:

1. KGRUN-M4: fix landing-memo charging so the cache does not fall from a performance cliff;
   enforce a deterministic memory cap and canonical fallback. **Implemented and integrated** in
   `cd52c33` (with the call-compatibility repair `90f4717`). The bounded LRU charges by landing
   shape, and exhaustion still takes the canonical uncached path.
2. KGRUN-M3: choose scan versus seek before encoding up to two 500-key sets. **Implemented and
   integrated** in `0e30b20`; `05acb7f` closes an adversarially found parity gap by restricting
   the fast string frontier to ASCII and returning surrogates/non-ASCII probes to the canonical
   encoder.
3. RELSEEK-M4: avoid vector materialization only for an internal endpoint landing that does not
   project the vector; all validation and refusal paths remain. **Implemented and integrated**
   in `88fedc9`; `69b2a51` makes the optimized identity landing an optional collaborator
   capability and preserves the prior `validated_versions` fallback for custom index managers.
   On the production-board relationship fan-out, the accumulated M3/M4/RELSEEK batch measured
   1.79x median speedup (round ratios 1.96x/2.11x/1.62x/1.45x), with an identical digest across
   1,375 edges. The synthetic capacity-cliff workload improved 1.33x minimum/1.37x median,
   removed the observed capacity refusal (1 to 0), and retained an identical digest.
4. EXEC-CSE, including KGRUN-M1. **Implemented and integrated** in `aee6002`; the adversarial
   repairs in `01f3438` and `8599f15` keep mapping subjects single-evaluation, forbid CSE across
   observable mapping/fallback reads, compile only on the first real non-computed row, and use a
   closed literal-key domain rather than arbitrary `repr`/hash authority. Cache mutation is
   locked while execution remains outside the lock; zero-row queries preserve their previous
   behavior. `3c71cf1` also removes a flaky concurrent landing assertion that confused unequal
   identifier sizes with unequal cache charges. The production-shaped 500-node page improved
   1.37x median across six alternating rounds (all six faster; 1.15x--2.12x band), with identical
   result digests. The 69-row `IN` fan-out remained effectively neutral at 1.05x, as expected for
   its canonical fallback, and the vector sample showed no regression claim. Ten focal tests,
   108 proportional tests, seven killed mutants and the accumulated full `tests/query` slice
   pass. No `exec` code generation was introduced.
5. KGRUN-M2(a): stateless pure cosine scorer preparation; no authority-bearing memo outside the
   HNSW generation fence. **Implemented and pushed** in `71b4110`: `PureVectorMath` now exposes
   the existing exact prepared-norm capability while retaining no adapter state; HNSW publishes a candidate
   norm only after a successful score and binds it to the immutable backing object of that node
   generation. A paired 384-dimensional/300-candidate scorer microbenchmark measured 2.00x
   (`0.1544 s` to `0.0771 s`, 50.1% less scorer time); the workload-level estimate remains ~8%.
6. VECTOR-6: test the identity-index gate before the O(N) proof. **Already present in the 0.0.3
   base** (`c77d414`): `_seal_materialized_candidates` validates the space/index/cost gate before
   it even obtains the witness iterator, and the query layer supplies a lazy generator. No new
   implementation is due in this round.
7. LV-3: resolve active indexes once per row in commit accounting/staging. **Implemented and
   pushed** in `b3d2ae3`: the canonical manager now carries one immutable table-local projection
   from WAL quota prediction into delete/insert staging, while custom managers and overridden hooks keep
   the observable legacy path. The produced-versus-expected WAL invariant remains mandatory.

Storage/identity lane:

8. STORID-1 + STORID-M2: remove the redundant existence probe before page count and enumerate
   once, preserving exact missing/case/symlink error ordering and types. **Implemented locally:**
   established files now take one page-count proof; only the ambiguous zero/missing boundary of a
   narrow storage collaborator pays `exists`; legacy nonce inventory performs one directory list.
   The complete index plus storage-device/fault-adapter regression slice passes. Pushed in
   `cbab991`; `abe068c` additionally preserves adapters that report exact absence as Python's
   `FileNotFoundError`, without translating permission, case, link or corruption failures.

Pulse Community lane, maintained in the Pulse repository rather than Grafx:

9. make the Grafx handle/pool budget configurable after measuring 128/256 MiB. **Decision
   revised by the real-board checkpoint on 2026-09-07:** retain 64 MiB by default. All three
   budgets produced identical ordered/canonical digests for two 500-row pages. Single-pair
   observations at 128 MiB were 0.261/0.255 s and at 256 MiB 0.315/0.283 s; these noisy samples
   establish no benefit from automatically allocating 256 MiB. Community configuration is
   implemented as `KG_GRAFX_BUFFER_POOL_MB` / `kg_grafx_buffer_pool_mb` (Community only), with
   exact positive-integer validation and rejection of mismatched shared pools. The accumulated
   settings/pool/composition checkpoints passed 129 distinct tests, including effective native budgets
   and durable reads at 64/128/256 MiB. The option is startup-only, documented in Community
   `docs/GRAFX_BUFFER_POOL_BUDGET.md`; no live budget was raised. Remember that one writer plus
   two reader lanes multiplies the
   nominal buffer envelope by three (192/384/768 MiB per fully opened board, excluding other
   engine/process memory). This does not change the native default;
10. deliver vector components through the Community sink so Grafx remains the validator.
    **Already implemented:** `grafx_logical_sink.py` delivers `LogicalVector.components`,
    `space_id` and `dtype` directly to native `VectorValue`, without a second normalization;
11. batch safe `IN` lookups, reduce sink commits only across explicitly recoverable units, and
    collapse the Grafx-specific two-hop fan-out without changing Core abstractions.
    **Partially implemented:** endpoint projection groups typed `IN` lists and executes the
    validated read batch under one snapshot (`kg_routes.py`, `grafx_cypher_executor.py`). The
    logical sink commits one recoverable batch (default maximum 500), checkpoints the exclusively
    owned unbound candidate at completion and cold-verifies its fingerprint before publication.
    **Remaining:** `find_by_artifact` memoizes adjacency per node, but still queries each incident
    layout/direction. The September 7 native vector-free landing candidate removes unused
    vector materialization from its existing closed scalar single-hop queries, without changing
    the frontier. Focused evidence and the accumulated checkpoint status are recorded in
    `docs/VECTOR_FREE_TRAVERSAL_0_0_4.md`; this does not claim that layout fan-out is eliminated.
    The subsequent scalar-anchor extension reuses the existing bounded PK memo in native read
    transactions: 36 repeated central-node bucket probes become 1, with an independent stable
    index view per statement, identical 105-neighbour results and a 150.12 → 131.10 ms warm
    median on the isolated real-board restore. Evidence and exact fallback boundaries:
    `docs/SCALAR_PRIMARY_KEY_MEMO_0_0_4.md`. This is not CONCUR-2 or query fan-out collapse.
    **Consumer correctness follow-up (`Community@440070b`):** the routed facade and Grafx
    store now expose the existing Core filtered-context capability. Hop1 direction/type filters
    select only their layouts and depth 1 performs no hop2 queries, rather than silently using
    the default two-hop path. The result limit no longer truncates candidate centers before
    expansion. Final real-Grafx/Community slice: 53 tests; existing Core contract: 8 tests.
    Details in Community `docs/GRAFX_RELATED_CONTEXT_FILTERS.md`. This removes unrequested work
    from filtered calls but does not claim the unrestricted layout fan-out is collapsed.
    Eager cross-node batching is not accepted yet: it can visit a center that
    the existing `max_rows` frontier never reaches, changing error/admission behavior. Keep the
    current exact result order, parallel-edge multiplicity, visibility filters and null hop2
    extension as the oracle for this already selected residual, not as a new feature target.

### Wave 2 — medium changes after Wave 1 re-profile

1. WRITE-1, then NATVER-2, with peak-memory and retained-lifetime measurements. **WRITE-1 is
   implemented and integrated** in `5904821`: query materialization may carry the exact encoded
   payload through a private, revocable proof bound to the same table and values objects. Copied
   or replaced tuples, pending endpoints, custom heap doors and every unproved value fall back to
   the canonical encoder. Nested mutable values (`list`, `dict`, `bytearray`) never receive an
   identity-only proof. Optional retained payload is capped at 8 MiB per transaction; exhaustion
   costs another canonical encode rather than changing admission or correctness. In six paired
   300-row wide-write rounds, median commit time fell from about 629 ms to 452 ms (`1.39x`,
   `-28%`) and total time fell about 11.9%; all twelve runs produced the same 499,712-byte
   `heap.dat` SHA-256. The measured pre-commit memory delta was about 478 KiB for 399 KiB of
   proved payload, and proof authority is revoked at discard, commit and abort. Five initial plus
   one budget-fallback adversarial tests and the 16-file proportional write/transaction/heap
   slice pass; Ruff, compileall and `git diff --check` are clean. **NATVER-2 is also implemented
   and integrated** in `bd02990`: the same canonical encoder now appends scalar and compound
   values into one private byte buffer, reusing the column's already-classified type and keeping
   vector validation in the existing oracle. A hostile 10,000-case differential produced the
   same 5,200 acceptances, 4,800 refusals and digest of bytes plus error details. The measured
   3,142-byte Pulse-shaped row improved about `1.17x`; the end-to-end post-WRITE-1 write signal
   was neutral within noise, so no larger claim is made. The full `tests/storage_core` slice plus
   WRITE-1 tests passes.
2. NATVER-3 only after a discriminating CRC microbenchmark. **Implemented and integrated** in
   `38d2785` after the discriminant: only the already corpus-proved `google_crc32c.extend` and
   `crc32c.crc32c` identities receive a collapsed adapter/call/result-check frame. Injected,
   stale and runtime-verified providers retain the original path; late bool, oversized-int and
   provider-exception mutants retain their typed refusals. The implemented path reproduced the
   same answers and measured `1.35x`, `1.71x`, `1.73x` and `1.85x` per call at 32 B, 256 B,
   4 KiB and 16,380 B. The corrected transfer estimate remains about 0.2%, so this is explicitly
   not reported as a material endpoint gain. The checksum, page-codec, WAL-format, ledger and
   bootstrap slice passes.
3. CKPTCERT-1 with heap and index corruption injected independently. **Implemented and
   integrated** in `577bdb0`: the one canonical, call-local table scan now seeds exact resolved
   `RecordRef` identities for the index-entry pass only when the built-in heap's catalog agrees
   structurally with the same table definition. A scan failure seeds nothing and remains reported
   once per affected index while entry failures are still resolved and reported independently;
   custom collaborators keep their former protocol. The adversarial integration review also
   delays publication of the seeded marker until a valid covering index and successful scan are
   proved, so an earlier malformed/non-covering index cannot suppress the optimization. On 4,600
   rows and 28 indexes, six paired rounds improved `verify(all)` from about 0.98 s to 0.575 s
   (`1.71x`, all rounds `1.70x`--`1.87x`) with identical reports. Heap-only, index-only and combined
   corruption retained their classifications/digests; 80 focal and 547 proportional tests pass.
4. One planned decode path for KGRUN-1/LADYBUG-M1/NATVER-1. **Closed with split outcomes** in
   `055d8ee`: the exact built-in heap can retain only the positional columns proved by a closed
   node-scan pipeline while still parsing and validating the complete durable tuple. Omitted
   positions carry a private sentinel and any unproved access refuses fail-closed; dirty
   transaction rows replace the physical projection with their complete pending values. A
   hostile 10,000-payload differential matched the canonical decoder's acceptance/refusal and
   exact exception class, `field` and `offset`. On a same-process alternating KG-shaped A/B
   (800 wide rows, 32 extra strings, 384-dimensional vector, seven queries per round), median
   wall time fell from 0.921 s to 0.772 s (`1.19x`, `-16.2%`) with an identical digest. The
   proposed full scalar inlining was rejected after measuring about 9% regression, and the
   projected vector-prefilter wiring was removed after an 11--13% end-to-end regression: its
   scorer still required the vector and skipped strings still had to validate UTF-8. Those two
   negative variants are not carried forward as moving targets.
5. KGRUN-3: **implemented and integrated** in `9e2d692` and `86f72f0`, with the adversarial
   correction `d1b775f`. A bounded `ORDER BY`/`LIMIT` now evaluates only potentially refusing
   projections and sort aliases on every source row; literals, already-bound parameters,
   declared/polymorphic properties, `label()` and non-DOUBLE `coalesce` are evaluated only for
   retained rows. The proof distinguishes typed from polymorphic bindings, so a cached nullability
   proof cannot hide a missing-column refusal. Temporary sort/distinct spill preserves the
   private unmaterialized-column marker rather than publishing or coercing it. The KG-page
   structural count fell from about 56,000 to 20,000 property reads (`-64%`); its isolated wall
   signal was a noisy `1.07x`, so the structural reduction is accepted without a larger timing
   claim. Eight initial query shapes, the two-mode adversarial cache sequence, four killed
   mutants and the accumulated full `tests/query` regression pass.
6. LADYBUG-M4: **closed as NO-GO under the current fail-closed contract**. The retained DETACH
   guarantee requires a corrupt *non-incident* relationship row to refuse the statement before
   any row is ended. Such corruption can be a property tag inside a checksum-valid page, so it
   is observable only by decoding every visible relationship row; an endpoint index cannot
   validate what it does not read. Claude built the indexed variant outside the branch and
   proved the trade-off: on 14,044 visible relationship rows it reduced three scans to four
   probes and improved the operation from median 226 ms to 12 ms (`19.7x`), with the same
   successful-state digest, but failed exactly the two pinned non-incident-corruption cases.
   No semantic code or weakened test was integrated. Handoff
   `hof_87ab57fb01084030bea3fae7be43c239` records the evidence. Reopening requires an explicit
   policy change; cheaper full-payload structural validation may be considered only as a codec
   optimization that retains the same refusal.
7. STO-M1: **implemented and integrated** in `04aa244`. Node primary-key groups repeatedly
   resolved by the relationship layouts of one transaction are retained only for the exact
   transaction object, intent list, snapshot object, native manager/store identity, registry
   revision, heap derived epoch and private durable index generation. Every cache hit still
   opens the normal pre/post page-0 certificate and companion heap view; a generation change
   revalidates every requested key, and a heap/registry transition during a cached read forces
   one uncached read. Dirty tables, custom/overridden managers and any incomplete authority keep
   the canonical path. The LRU is capped at 4,096 entries per transaction inside the existing
   32 MiB decoded-landing budget; admission precedes mutation, capacity changes cost only, and
   commit/rollback/transaction-id replacement returns every charge. In an alternating
   ten-layout workload, PK decodes fell from 1,200 to 120 (`-90%`) and median fan-out time from
   229 ms to 192 ms (`1.19x`), with identical result digests. The focused/proportional 124-test
   slice, Ruff, compileall and diff-check pass.

### Wave 3 — small residuals and one re-profile

1. WRITE-4/M1/M2 with a crash matrix at every segment boundary — **implementado e integrado** em
   `b444d09`, `74514e2` e `f0fc97d`; handoff Nexus
   `hof_ab1156bc405346f3b767bbef80a0eeb3` concluído e verificado. Registros já canônicos não são
   reconstruídos, o descritor é aplicado uma única vez e o resultado intrínseco do preview é
   reutilizado somente para os mesmos objetos exatos. Cauda, época, rotação, teto do segmento e
   LSN terminal continuam recalculados imediatamente antes do append. A primeira revisão foi
   recusada porque o memo sobrevivia a portas falíveis, podia ser publicado antes do plano inteiro
   e retinha as fontes em duplicidade; `f0fc97d` passou a consumi-lo antes de qualquer porta,
   publicá-lo só após o último check e manter uma única retenção. O corpus diferencial de 160
   lotes/73 rotações permaneceu byte a byte idêntico; 328 testes WAL e oito mutantes passaram. No
   benchmark pareado, preview e append melhoraram `1,42x` e `1,73x`, respectivamente.
2. WRITE-M3 and WRITE-5 — **implementados em `b8906f6`**. O snapshot de parâmetros usa um
   caminho linear especializado somente para o `dict` embutido com valores escalares imutáveis
   exatos; subclasses, mappings customizados, compostos e valores hostis continuam no copiador
   canônico e nos mesmos limites/erros. A resolução de coluna usa a autoridade
   `TableDefinition.column_positions`, eliminando a busca linear sem mudar o erro de ausência.
   Os microbenchmarks isolados mediram `2,80x` e `3,01x`, respectivamente; 133 testes públicos de
   query/escrita mais a regressão completa do query engine passaram.
3. STORID-M1 — **implementado em `fd111cd`**. No Windows, a prova de nome exato usa
   `FindFirstFileExW` sobre caminho estendido e compara o nome armazenado, sem enumerar irmãos;
   ausência continua ausência, colisão de caixa continua tipada e todo outro erro de SO continua
   traduzido/fail-closed. A identidade do diretório permanece cercada antes/depois, e `lstat`
   mais a recusa de reparse point continuam obrigatórios. Hosts sem a API e todas as demais
   plataformas retêm a caminhada portátil. O teste nativo cobre raiz física Unicode, 688 irmãos,
   acerto, ausência e colisão sem permitir `listdir`; a matriz proporcional cobre também troca
   por junction/symlink, descritores e durabilidade. Nesse diretório, 300 resoluções do último
   filho caíram de mediana 0,411 s para 0,185 s (`2,22x`).
   O checkpoint também executou os dois testes de recovery que já falhavam na base. Um fixture
   chamava de válida uma página HEAP sem o descritor estrutural hoje verificado pela pré-validação
   e foi corrigido para exercitar a imagem inválida posterior pretendida. O segundo caso expôs
   um defeito real: o diretório de replay quente podia ignorar hooks físicos especializados ou
   injetados. `a082ff8` agora recusa apenas esse diretório quando qualquer hook substituído não é
   o canônico, preservando o replay escalar e a falha fail-closed. Os 27 testes do batch comum e
   a regressão combinada de replay/recovery ficaram verdes.
4. Re-run the direct KG page, relationship fan-out, vector, transfer, open, recovery and
   concurrent-reader workloads once for the accumulated implementation. **Checkpoint parcial
   concluído:** no board real `generation-1`, a página de 500 nodes ficou em mediana `0,514 s` e a
   continuação em `0,469 s`, mas ambas ainda escanearam 2.171 linhas. O fan-out retornou 793 edges
   em 66 tabelas; o caminho híbrido/indexado quente ficou em `1,167 s`, contra `0,866 s` forçando
   scan, com mesmo digest, 73 chamadas multi-key e 4.808 probes. Open mediu `1,210 s`, autocommit
   relacional `2,260 s` e o mesmo batch sob um snapshot `0,890 s` para 3.256 rows. O checkpoint
   combinado pós-WAL/recovery passou 411 testes de WAL, protocolo de commit, integração WAL e
   crash recovery. O complemento delimitado de vector, transfer e concorrência foi executado
   em 07/09/2026, sem consumir specs pendentes: caso sintético com dois leitores/dois writers,
   mais restauração isolada do backup completo do board real. Evidência, medidas e limites em
   `docs/V004_VECTOR_TRANSFER_CONCURRENCY_CHECKPOINT.md`. Isso fecha esse complemento de
   checkpoint, não declara eliminados o fan-out residual nem os itens da decision queue.
5. **Decisão material:** BATCH-REL-1 foi executado antes de CURSOR-1; não foram adicionados
   residuais menores. A solução integrada em `567a6a3` é deliberadamente estreita: somente a
   ausência do extent — prova durável de que nenhuma página relacional foi alocada — responde
   vazio sem abrir o scan ou certificados dos índices. `next_record_id == FIRST_RECORD_ID` não é
   prova suficiente quando há página física, pois o scan canônico ainda deve validá-la e revelar
   eventual corrupção. O corpus focado passou 47/47 casos e quatro mutantes; a regressão de query
   executada no handoff passou 2.191/2.191 casos. No board real, o resultado permaneceu em 824
   edges/66 layouts/zero falhas e as aberturas de scan relacional caíram de 47 para 4. As rodadas
   quentes ficaram em `1,117-1,135 s` na base e `1,208-1,225 s` no candidato, sem ganho de parede
   material comprovado; o checkpoint aceita a redução estrutural de 43 scans sem inflar a
   conclusão. Dois modelos de custo calibrados foram rejeitados: um regrediu a tabela densa do
   frontier 500 de aproximadamente `1,8 s` para `9,4 s`, e o segundo introduziria constantes
   dependentes de máquina para ganhos residuais. Handoff Nexus
   `hof_2b5c1046401c4d3db22b03e01f6cb4ea` concluído e verificado.
6. **Experimento delimitador de CURSOR-1:** reutilizar diretamente o `QueryCursor` público não é
   a solução selecionada para a paginação HTTP do Pulse. Uma consulta única com teto suficiente
   para todo o resultado e lotes de 500 fez a primeira entrega em `1,658 s` e a segunda em
   `0,075 s`, depois de varrer as 2.184 linhas uma única vez. Isso melhora a continuação, mas
   triplica aproximadamente a latência inicial observada e mantém uma transação/snapshot de
   leitura aberta entre consumos. O cursor foi fechado sem vazamento de transação. CURSOR-1 só
   avança com uma materialização destacada e limitada ou um acesso ordenado persistido que prove
   a geração/snapshot, preserve as recusas fail-closed e não retenha leitores durante o tempo de
   interação do usuário; o atalho ingênuo foi encerrado como **NO-GO**.
7. **CURSOR-1 selecionado após revisão adversarial:** a materialização destacada foi rejeitada
   porque torna a primeira página estritamente mais cara e é invalidada pelas escritas frequentes
   do Pulse, restaurando `O(P*N)`. O consenso Claude/Codex selecionou um índice exato ordenado,
   persistente e copy-on-write em `(TIMESTAMP, STRING)`, com revalidação obrigatória no heap.
   `docs/ORDERED_CURSOR_ACCESS_0_0_4.md` congela o desenho, incluindo duas páginas físicas de
   descritor de raiz, watermark de redo no mesmo descritor, páginas imutáveis append-only,
   publicação `WAL -> COW -> raiz -> commit-state`, rebuild compactante e fallback canônico.
8. **OIX-0 implementado:** o catálogo continua em formato 2 e ganhou a capability obrigatória
   `ordered_secondary_indexes_v1`; o byte reservado de metadados passou a discriminar layout sem
   alterar os bytes de índices hash. O header ordenado usa formato 3, as páginas têm tipos próprios
   e as duas raízes físicas têm payload checksummed. Uma versão 0.0.3 recusa a capability antes de
   interpretar o record; `HashIndex` recusa o layout ordenado até o store dedicado existir. O
   corpus focado passou 174 testes e a regressão completa de `tests/index` passou a 100%; Ruff,
   compileall e diff-check estão verdes. O handoff `NODE-IN-SEEK`
   `hof_f05eb759297a4598845a9d312d9c339c` segue independente em worktree isolado.
9. **OIX-1A implementado:** a chave `(TIMESTAMP, STRING)` preserva a ordem canônica inclusive nos
   extremos assinados, empates, prefixos, NUL, Unicode e NULL. O bulk builder produz páginas
   leaf/internal imutáveis e contíguas; o verificador prova tipo, ordem estrita, separadores,
   ranges disjuntos, ausência de ciclos/shared children, altura e contagem. A caminhada
   descendente aplica upper bound lógico exclusivo — removendo todos os `RecordRef` históricos da
   chave do cursor — e LIMIT sem materializar a árvore. As duas raízes independentes selecionam
   somente gerações iguais/adjacentes, degradam com uma cópia danificada e recusam split-brain,
   formato futuro ou duas cópias inválidas. O corpus focado de 26 testes cobre árvores multi-nível
   em páginas de 512/1.024 bytes e shapes com empates.
10. **OIX-1B implementado:** o store dedicado cria somente artefatos ordenados com nonce físico
   não zero e publica em três fases duráveis (`tree -> raízes A/B -> header estático`). A abertura
   faz verificação estrutural integral; cada leitura de statement fica entre certificados frescos
   de page 0 e das duas raízes, carrega somente as páginas visitadas e repete toda a tentativa se
   a raiz mudar. A raiz pode estar à frente do snapshot apenas porque cada candidato exato é
   relido no heap, reavaliado pela visibilidade do snapshot e tem sua chave rederivada; candidatos
   invisíveis ou antigos não consomem o LIMIT. Uma raiz danificada degrada para a cópia íntegra e
   duas inválidas recusam fail-closed. O corpus focado acumulado de OIX-0/1 passa 32 testes nesta
   etapa, com Ruff, compileall e diff-check verdes. Nenhuma query seleciona o caminho antes de
   OIX-2 manter o índice no protocolo transacional.
11. **OIX-2A implementado:** o mutador COW aplica o lote lógico completo copiando cada folha e
   ancestral afetados no máximo uma vez, compartilha filhos imutáveis não afetados e grava apenas
   as páginas alcançáveis da nova geração. O publicador exige WAL já durável, serializa pela seção
   de escrita da page 0 e respeita `páginas COW -> barrier de dados -> raiz alternada -> barrier de
   raiz`; o watermark da raiz torna o replay idempotente sem novo crescimento. Testes cobrem lote
   diferencial aleatório, coalescência na mesma folha, árvore vazia, tombstone/remove, recusa da
   raiz e escrita que pousa antes de lançar exceção. O corpus focado passa 16/16. A conexão com
   registry/DDL, commit/replay em lote, rebuild compactante e a matriz crash/multiprocesso ficam
   explicitamente em OIX-2B antes de qualquer seleção pelo planner.
12. **OIX-2B parcialmente implementado — registry/commit/replay:** `OrderedIndex` participa do
   contrato comum de staging e do registry, publica o conjunto completo de uma transação em uma
   raiz COW e o recovery coalesce toda a subsequência WAL do mesmo store em uma única publicação
   idempotente. Lookup exato navega somente os ramos capazes de conter a chave e continua dentro
   do certificado com revalidação obrigatória no heap; gerações destacadas são construídas em
   bulk sem alterar autoridade do catálogo. O checkpoint completo de `tests/index` passou a
   100%. DDL/ativação de catálogo, rebuild compactante e crash/multiprocesso permanecem antes do
   fechamento do OIX-2B.
13. **NODE-IN-SEEK implementado e validado:** o shape estrito `MATCH (n:Label) WHERE
   n.<primary-key> IN $parameter` usa a porta exata multi-key sem scan no caso elegível, mantendo
   o predicado acima do operador e revalidando cada candidato no heap. Tabela suja, store
   ausente/stale/custom sem a capacidade, tipo/encoding incompleto e todos os near misses mantêm
   o scan canônico. Um seletor pelo high-water durável escolhe scan quando a tabela alocou no
   máximo metade das chaves distintas; ele evita a regressão medida das listas maiores sem
   constante dependente de hardware. Revisão independente: 57/57 casos focados, 18/18 mutantes,
   2.248/2.248 testes de query no branch isolado e 178 testes combinados após integração. Handoff
   Nexus `hof_f05eb759297a4598845a9d312d9c339c` concluído e verificado por Codex.
14. **OIX-2B DDL/ativação, compactação e matriz concluídos:** `28d76ea` expõe
   `OPTIONS layout = ordered`, `create_index(..., layout="ordered")` e
   `rebuild_index()`. A ativação usa geração nonced `BUILDING -> ACTIVE`, o rebuild constrói e
   verifica uma geração compacta nova antes da rotação `ACTIVE -> STALE`, e `rehash_index()`
   recusa explicitamente o layout ordenado. Tipos, posições, derivação, sizing e reopen foram
   provados no corpus focado. O handoff Nexus `hof_4fe5ef3390c148c4ba668a83f6b3ac58`
   entregou 17 casos de crash/recovery e 8 de processos reais. A matriz revelou que uma raiz mais
   nova danificada podia impedir o open quando a publicação seguinte tentava decodificar a cópia
   descartável antes de substituí-la. A correção escreve a raiz não-header completa pela porta
   serializada do pool sem ler o alvo, e não promove uma raiz sobrevivente danificada além do
   high-water da tabela quando o WAL correspondente já não permite reconstrução. Todos os 25
   casos passam sem xfail; dois writers e readers pinados preservam as garantias originais.
15. **OIX-3 implementado; checkpoint acumulado executado em 2026-09-07:** o novo `OrderedNodeMerge` reconhece
   somente a página polimórfica fechada `ORDER BY TIMESTAMP DESC, primary-key STRING DESC LIMIT K`.
   Ele empurra o cursor estrito `(timestamp,id)` ao índice, mantém uma cabeça certificada por
   tabela e faz merge `O((K+S) log T)` com memória `O(T)`, revalidando candidatos no heap. O
   resultado fica privado até o fechamento dos certificados; drift, dano e geração incompleta
   propagam erro tipado sem misturar prefixo com fallback. Antes da primeira leitura, capability
   ausente, tabela suja, cursor incompatível e todo near miss executam o pipeline canônico.
   No corpus de 24 nós/2 tabelas, a primeira página examinou no máximo 10 candidatos em vez dos
   24 do scan, com linhas idênticas; a continuação também foi idêntica e inferior em trabalho.
   Os 18 casos focados de store/query e a regressão combinada de 440 testes de planner,
   polimorfismo, projeção e executor passaram. Não há alteração em WAL, OCC, writer fence,
   snapshot, durabilidade ou participação multi-reader/multi-writer.
16. **Poda polimórfica necessária ao Pulse implementada:** uma tabela sem as colunas de ordenação
   só deixa de participar quando a análise ternária prova que seu predicado completo nunca pode
   ser verdadeiro por causa de propriedade ausente. Isso remove `BoardMeta` do merge real do
   Pulse sem modificar o Core nem relaxar a exigência de índice em qualquer tabela que possa
   produzir uma linha. Conjunções admitem a prova por um ramo; `OR` exige prova nos dois ramos.
   Dois casos focados cobrem a poda válida e o `OR` inseguro que conserva o scan canônico.
17. **Retomada pós-Discovery (2026-09-07):** os 14 cards do Pulse foram validados via API e UI
   após a implementação nativa do OPTIONAL correlacionado (`6b0b66e`), sem mascarar warnings.
   O checkpoint OIX acumulado passou 96 casos de chaves/árvore/store/manager/formato, crash,
   multiprocesso, ordered merge e optional. Uma comparação read-only no mesmo snapshot do board
   real, com as consultas Core atuais e `SKIP 0` como oráculo canônico, obteve linhas/digests
   idênticos nas duas páginas de 500: 657/512 candidatos examinados pelo merge contra 2.411/2.411
   linhas no scan (redução estrutural de 72,7%/78,8%). Tempos observados no par único foram
   0,376/0,338 s contra 0,729/0,515 s; sem alegação de benchmark controlado nem gate temporal.
   A integração idempotente OIX-4 está em Community `40ed1a3` e passou 62 testes no checkpoint
   anterior; o Core continua independente da tecnologia. Claude recebeu o handoff delimitado
   `hof_4bb4dae3f00d45c792b6ab8fe807bd17` para reconciliar exclusivamente O1–O3 da sua revisão
   anterior e corrigir bloqueios concretos, se ainda existirem. Custos marginais não reabrem a
   rodada. Nenhuma nova lista exploratória foi acrescentada.
18. **OIX residual fechado (`a52940d`):** O1 (segunda publicação do watermark no replay) é
   custo marginal, sem correção selecionada; O3 é a semântica de visão capturada do registry,
   atualizada após `execute`, `begin("read")` ou `verify`, não por `status()` isolado. O2 era
   um bloqueio real para consumidores que exigem verificação limpa após crash: páginas COW
   alocadas e nunca publicadas permaneciam como `page_unwritten`. A proposta inicial
   `c20fa85` foi rejeitada porque escondia página alcançável zerada com cache quente. A revisão
   `0d441f0`, integrada junto com a correção em um único commit, exige certificado fresco e
   estável e verificação estrutural completa das árvores das duas raízes. Só a página não
   escrita fora desse conjunto é isenta; qualquer falha de prova conserva todos os findings.
   Header, raízes e artefatos hash não recebem a isenção. A regressão integrada de crash
   ordenado, verifier, recovery público e chain-relink passou 152 testes; Ruff passou. A
   verificação física deve usar `pages` ou `all`: o escopo `indexes` continua sendo o walk
   lógico e não substitui a inspeção física com cache quente. Nenhum patch foi aplicado aos
   dados do board; nenhuma operação de WAL, OCC, snapshot ou participação foi alterada.

## Explicit decision queue

These items are not silently included in the waves above:

| Priority | Item | Required proof/decision |
|---:|---|---|
| 1 | CONCUR-2, one exact certificate per file/transaction | Snapshot-by-epoch equivalence, publication races, file replacement, corruption, crash and two-process matrix; must preserve fail-closed behavior. |
| 2 | D-8/STORID-2 minimal descriptor identity proof | Exact difference between `strict` and `generation`, including Windows open-descriptor semantics. |
| 3 | `exec` code generation | Security/debuggability decision; only the incremental gain over EXEC-CSE counts. |
| 4 | `vector_math="numpy"` default | Determinism, dependency and cross-machine ranking policy. |
| 5 | `retain_lease` for bulk load | Explicit writer-exclusion policy and bounded timeout behavior. |
| 6 | persisted high-water and CURSOR-1 | On-disk format/migration/recovery design. |

No decision above may remove multi-reader/multi-writer operation or weaken WAL, durability,
snapshot, OCC or consistency.

## Test cadence and acceptance

Runtime follow-up (2026-09-07): scalar PK `9420b49` and Community filtered context
`440070b` were loaded into Pulse 0.3.3. Real UI pagination reached 1000 nodes;
warm graph REST returned 500 nodes/703 edges with no failed relationship tables.
The cold 42.464 s request remains distinct from warm observations. Community
`5ee613d` removes duplicate census requests caused by permission hydration and
decouples historical completion from Health latency (26 focused UI tests).
See Community `docs/KG_CENSUS_REQUEST_ISOLATION.md`. This is integration follow-up,
not a new performance gate or closure of the remaining layout fan-out work.

- Each item gets focused correctness, hostile-boundary and component A/B tests.
- Several low-risk items are accumulated before the related regression slice.
- Long crash, recovery, multi-process and full-suite runs occur at coherent wave checkpoints,
  not after every small patch.
- A marginal or noisy wall-time result is recorded without becoming a gate. A structural
  reduction may be accepted when equivalence tests pass and the removed work is counted.
- Any exception or semantic divergence found during implementation is fixed before the item is
  accepted; no known blocker is carried into the next wave.

## Coordination record

Claude and Codex reached consensus on 2026-09-06 after the L5/L6 surveys and adversarial review.
The first isolated implementation handoff is
`hof_75b4003eac9f4ebe84f16b2f2bf42f68` (KGRUN-M4 + KGRUN-M3 + RELSEEK-M4). Its six commits were
integrated as `0e30b20..69b2a51` after adversarial fixes for Unicode encoding parity and optional
index collaborators. The focused combined-head gate passed 127 tests; the single accumulated
query/vector/storage-core regression passed 3,835 tests in 358.35 seconds. Codex owns Wave 0,
the first storage/identity lane and final integration. Pulse-specific work remains in Community
adapters and composition roots.

The Wave 2 KGRUN-3 handoff is `hof_57a62b5a23d34d2c8644894b1ef002d1`. Claude supplied
`13bac09`/`f65bc51`; Codex's adversarial review found that the totality cache omitted the
`RowBinding.polymorphic` mode. Both agents agreed on the defect and on an exact two-mode proof.
The integrated tuple-keyed variant also caches the dominant polymorphic KG path; the shared test
exercises both arrival orders and mixed repetitions. The first accumulated query run exposed a
second interaction with bounded spill; `d1b775f` added a private spill tag for the projection
sentinel and updated scan-consumption instrumentation. The failing block then passed, followed by
the complete query regression at 100%.

The LADYBUG-M4 handoff is `hof_87ab57fb01084030bea3fae7be43c239`. Its adversarial result was
accepted as a NO-GO, not as an implementation: the measured `19.7x` indexed path necessarily
removed a currently pinned non-incident corruption refusal, so its patch and test relaxation
were excluded. STO-M1 then proceeded on the canonical branch and removed the independently
measured repeated-PK residual without changing the fail-closed contract.

Wave 3 started with two independent low-risk lanes. Codex integrated the exact-type parameter
and O(1) column lookup in `b8906f6`, then the Windows exact-name storage proof in `fd111cd`.
Claude delivered the WAL planning lane in `hof_ab1156bc405346f3b767bbef80a0eeb3`. Codex rejected
its first retained-lifetime design, Claude corrected the three findings in `f0fc97d`, and the
handoff then passed independent review and the accumulated 411-test checkpoint. The real-board
re-profile kept only BATCH-REL-1 and CURSOR-1 as material next work, in that order; the decision
and measurements are recorded in Wave 3 above. BATCH-REL-1 was then closed in `567a6a3`: the
adversarial review narrowed the proof from an allocation counter to the exclusive absence of an
extent, and the real board confirmed 43 fewer relationship scans with identical results, though
without a material wall-time gain. No item in this wave changes writer/reader
participation, snapshot visibility, either OCC validation or durability semantics.

CURSOR-1 was then resolved through Nexus trace
`trc_c2b021d3d44c4ec69ab96cdbbf8fc818`. Claude's detached-spine proposal was rejected after both
agents agreed that it adds work to the first page and loses its benefit whenever Pulse advances
the node frontier. The accepted persistent ordered design and F1--F7 safety conditions are frozen
in `docs/ORDERED_CURSOR_ACCESS_0_0_4.md`; OIX-0 is implemented, while Claude owns the independent
`NODE-IN-SEEK` handoff `hof_f05eb759297a4598845a9d312d9c339c`.

### Current execution checkpoint — 2026-09-07

Codex now proceeds alone; earlier Claude ownership above is historical, not an
active dependency. Community `270d84a` records subgraph/census phase timing and
off-loop dispatch through the existing generic Core port. The cold live load
remains unresolved (27.751 s graph, 34.351 s census in the instrumented run).
Authorization/dispatch were below 3 ms per phase; separate-process native and
routed probes were much faster but are not an equivalent full-app benchmark.
The 15-second stack sample shows concurrent graph, census and Health work,
not proof of a single bottleneck. See Community `docs/KG_READ_PHASE_PROFILE.md`
for measurements, limitations and the 51-test focused checkpoint.

No native protocol change, further consolidation or new performance gate was
introduced. Preserve the 21 pending specs for write benchmarks. Continue the
existing cold admission/census investigation without expanding acceptance scope.

The census investigation then identified the native RelationshipScan gap in the
existing scalar vector-free landing proof. Its extension validates both endpoints
without retaining vector components, keeping the full path for any vector/entity
consumer and specialized hooks. The final 64-test slice passed, with a separate
48-test initial slice (overlap). Real routed count batches preserved all 70 table
answers; the short median change was only 2.837 → 2.698 s, not a new timing gate.
The native change was loaded in Pulse PID 15796 and pagination reached 1500 nodes
with zero failed edge tables; total cold UI latency remains unresolved.
See `docs/RELATIONSHIP_SCAN_VECTOR_FREE_0_0_4.md` for safety, limitations and live
evidence. No spec was consolidated and no authority-cache initiative was added.

Cold admission then exposed a duplicate exact-index adoption around the
TransactionManager constructor. Read-only assembly now loads the catalog first
and performs one full adoption at the existing final admission point. Writable
recovery and later foreign-DDL synchronization retain their existing callbacks.
The real-board sample reduced 366 adoptions to 183 with opening medians
1.479 → 1.242 s; this is not an end-to-end UI speedup claim. Startup/read-only
slices passed (15 and 57, overlapping), plus a targeted foreign-DDL test.
See `docs/READ_ONLY_SINGLE_ADOPTION_0_0_4.md`. Queue this change for accumulated
Pulse deployment; PID 15796 is not claimed to have loaded it yet.

Concurrent workload check: isolated graph/stats took 0.884/3.770 s; alongside
Health they took 1.610/10.995 s with identical counts and HTTP 200. The residual
is not yet attributed to a specific lock/GIL/storage cause. A native aggregate
projection candidate passed 66 focused tests but showed no consistent real-board
timing gain (warm medians full/projected 0.287/0.307 s), so it was withdrawn,
not integrated. No separate spill feature or prolonged marginal gate was added.
See `docs/KG_CONCURRENT_CENSUS_FINDINGS_0_0_4.md`; the existing concurrent-read
investigation remains the next target. Reserved specs and durability are untouched.

### Write-path source consolidation — 2026-09-07

Community `31b8439` and Core `c4a01bf` consolidate the Global identity index,
digest-anchored link probes and bounded reconciliation corrections described in
`PULSE_COGNITIVE_WRITE_AUDIT_2026_09_07.md`. No delivery was replayed or cognitive
spec consumed to record these source milestones. The exact isolated candidates
passed 198 Community tests (502.66 s) and 19 Core tests (26.71 s).

Review caught an uncommitted adapter regression: inline endpoint maps with a
leading relationship-rule WHERE predicate lost the endpoint seek in edge_exists.
Restore the earlier committed endpoint-first equality form; the 8-node fixture
drops from 9 examined rows to at most one edge. Both 8/32-node fixtures pass.
Pair creation itself already used two native seeks in its previous WHERE form,
so the explicit PK syntax is equivalent, not an additional performance gain.
No planner refusal semantics, OCC, WAL or multi-reader/writer guarantees changed.
See Community `docs/GRAFX_WRITE_PATH_MILESTONE.md` for scope and deployment
boundaries. Cold UI admission and full write latency remain unresolved; this
checkpoint does not add a new exploratory target or timing gate.

### Full verification history cost — 2026-09-07

The recorded post-flush write-cost investigation identified quadratic traversal
of MVCC suffixes. Native verification now reuses only fully checked suffix lengths
within one table/call, retaining all page, record and index validation and custom
collaborator fallbacks. A single read-only private Global comparison returned
identical complete reports in 64.359 s versus 16.075 s. This is a stage-specific
observation, not end-to-end write acceleration or a new timing gate. Tests prove
linear healthy-history visits and identical corruption findings. Details, safety,
test counts and deployment status: `VERIFICATION_VERSION_SUFFIXES_0_0_4.md`.
No live consolidation or backlog processing was used; the 21 reserved specs remain.
