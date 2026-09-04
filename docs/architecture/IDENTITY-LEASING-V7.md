# DESIGN V7 — Identity-range leasing

> **Status:** PROPOSTA / BLOCKED — requer revisão conjunta Codex/Claude antes de implementação.
>
> **SHA de referência da auditoria/linhas — não é parent de implementação:** milestone M1
> `539e94e8fa8c084c8322d4d23c6105610333b684`, no worktree de auditoria
> `D:\Projetos\Techridy\okto_grafx-m1`. As referências de linhas deste documento foram conferidas
> nessa árvore limpa. A revisão Claude anterior usou o `main` `23794daf...`; suas conclusões
> L1–L4/O1/O2 foram preservadas, mas suas linhas não são evidência para este desenho.
>
> **Parent obrigatório de implementação — ainda não registrado / BLOCKED:** será o milestone final
> integrado coordenado `P_integrated`, merge-descendant que contenha a ancestry aceita de M1
> `539e94e8fa8c084c8322d4d23c6105610333b684`, M2
> `ae0cc54a565c407d729c1ba5726ad7d45c6d321e`, M3
> `ca7a7a5231d81fba8ea7d7071459b2f620997577`, C13
> `32b864cf4012488976dab02306b3d69b7f03e665` e lint
> `e8d5a701885a3fcf0c61b0e4ea24ad081dbd7c5b`. Seu SHA completo só é escrito no acceptance record
> depois dos merges coordenados e do Gate 0; nenhuma slice V7 pode partir de M1 isolado, rebasear para
> perder ancestry aceita ou re-cherry-pickar commits já ancestrais.
>
> **Oráculo de integridade que deve ser preservado:**
> `m2/record-id-counter-verifier@40a42014913394d00f3446316edba68569f26d44`.
> Ele precisa ser ancestor de `P_integrated` (normalmente via M2), não patch reaplicado na Slice 4.
>
> **Rastreabilidade:** V7 preserva, mas não altera,
> `IDENTITY-LEASING-V4.md` (SHA-256
> `8301053a57e7c3e76ee15da515e76945b22aeaf8a0728d6c504f95426b154726`).
> Preserva também `IDENTITY-LEASING-V5.md` byte-identical (SHA-256
> `02018552bfcd427b5ec72a653284ab6da9e5664ea1a41724f0d9e1e8968e6488`) e
> `IDENTITY-LEASING-V6.md` byte-identical (SHA-256
> `f8406a7107ecd8b89c5d2038d6fd608942507f708947ab70b16bdcb3694132b3`). V7 incorpora integralmente
> a V6, mas substitui expressamente suas cláusulas sobre flags WAL v1, manifest de índices,
> read-view, freshness/ABA, pipeline de statement, budgets e recovery/checkpoint. Em qualquer
> conflito interno, as seções V7 marcadas **SUBSTITUI V6** prevalecem.
> Após ACCEPT conjunto, V7 substitui as propostas V4/V5/V6 e constitui emenda substitutiva ao
> `ROUND7-PLAN.md` §4 e ao Option 1 de `W6-WRITE-CEILING.md`. Até esse ACCEPT, nenhum writer,
> upgrade, migração ou leasing v2 pode ser implementado ou habilitado.

> **Reserva WAL v2 posterior:** o ADR aceito
> [`WAL_PAGE_COMPRESSION_V1.md`](WAL_PAGE_COMPRESSION_V1.md) atribui `0x0004` a
> `PAGE_IMAGE_ZLIB1`, `0x0008` a SKIPPABLE e admite hoje somente `WRITE_PAGE` v2 com
> `REQUIRED | PAGE_IMAGE_ZLIB1`.
> Qualquer retomada desta proposta V7 deve compor seu mask/gramática com esse formato já publicado
> ou selecionar uma versão WAL posterior; não pode reinterpretar o bit nem emitir outro tipo v2
> sem atualizar o classificador fechado e a capability persistente.

## 0. Fechamento normativo V7 — SUBSTITUI V6

Esta revisão fecha blockers objetivos encontrados contra o SHA-base e não relaxa nenhuma garantia
anterior. Os fatos que motivam a substituição são reproduzíveis:

- `domain/wal/record.py:168-180` aceita qualquer `flags` u16 no WAL format v1, e
  `tests/wal/test_wal_record_format.py:97-102` congela `0x1234`; o round-trip em
  `tests/wal/test_wal_manager_recovery.py:913` congela `flags=9`. Portanto, bits de v1 não podem
  adquirir significado retroativo;
- a publicação em `control/commit.state` é posterior à WAL barrier e ao apply. Um crash pode deixar
  COMMIT durável acima do published, inclusive apply parcial, e esse gap precisa ser resolvido antes
  de qualquer snapshot, não apenas antes do próximo writer;
- o manifest V6 enumerava somente CREATE/DROP/ALTER, omitindo RESET, REBUILD, INDEX_RECONCILE e
  outras mutações index-only;
- `_stale_reason` mistura sinal durável compartilhado com poison local, enquanto o grafo HNSW é
  cacheado apenas por `built_through_lsn`;
- em M1, `QueryEngine._run` materializa `rows` e chama `context.release()` em sequência
  (`query_engine.py:859-860` na árvore auditada). Uma certificação posterior não pode simplesmente
  recusar, pois os intents já podem ter migrado para a transação;
- `participant_section` é participant-local. Ela não exclui mutadores de outro processo e não
  protege uma caminhada de índice;
- `Database.verify`, `inspect_index`, status, reconcile, recovery e checkpoint também leem ou
  alteram estado derivado e precisam do mesmo protocolo, não de exceções ad hoc.

V7 toma as decisões fechadas seguintes:

1. **WAL v1 continua legado e opaco.** Required/skippable e manifest existem somente no WAL format
   v2, após um fence durável em `grafx.meta`. Nenhum writer emite record v2 antes desse fence.
2. **Snapshot depende de um DAG de três provas.** `WalFrontierCertification` exige que o sufixo WAL
   durável acima de `published` esteja sem COMMIT/incomplete/required desconhecido;
   `RuntimeDeviceReplayProof` exige materialização até o mesmo published; `WalPublicationProof`
   envolve ambas somente depois da conversion de uma fresh reader key.
3. **Toda mutação de índice é manifestada e fenced.** Um `IndexMutationManifest` fechado cobre row
   effects e também cada mutação index-only, inclusive maintenance e checkpoint.
4. **ABA é excluído por posse, não inferido de um contador de 32 bits.** Readers mantêm um guard
   interprocesso `INDEX_VIEW` em modo SHARED durante toda a observação/materialização; todo mutador
   toma o mesmo guard em EXCLUSIVE antes do primeiro byte mutável e até a certificação final.
   Uma shared mutation generation com incremento checked invalida caches entre scopes; `Page.seq` e
   hashes detectam drift, mas não substituem guard/generation.
5. **Statement é atômico também em staging.** Há preflight detached, mark externo único e rollback
   completo de rows, catálogo, attachments, índices e partições. IDs já consumidos não sofrem rewind.
6. **Recovery/checkpoint nunca ressuscitam grant.** Eles materializam `F` já autorizado por WAL,
   invalidam authorities locais e tratam ranges não ativados como queimados. Ambos usam fresh writer
   lease e o protocolo certify/revalidate de duas passagens.
7. **Budgets são dados exatos.** Toda espera/repetição possui limite de tentativas, deadline
   monotônico e erro terminal tipado; nenhum `while changed` ilimitado permanece.
8. **Cold open não confunde publicação com materialização durável.** Como commit ordinário não faz
   data barrier, toda runtime nova prova ou refaz de Q até P antes de expor um handle write-capable;
   read-only só abre quando `P == Q` e o checkpoint físico é diretamente certificado.
9. **Legado não ganha controles V7 por lazy open.** Sidecars, carriers e baseline nascem em um
   bootstrap offline, durável e retomável, antes do fence em `grafx.meta`; bootstrap parcial bloqueia
   uso normal até resume/restore explícito.

As seções 5.2–5.4, 7, 8.4, 10, 11, 16–17, 19 e 20 abaixo detalham essas decisões e são normativas.

Todo contador u64 usado como safety generation neste contrato obedece a uma única primitive
`checked_increment_u64(current, domain)`: exact built-in int (bool/subclasse recusados),
`0 <= current <= MAX_U64`; se `current == MAX_U64`, a operação recusa com erro de integridade typed e
non-retryable **antes** de alterar registration, slot, carrier, WAL, device, catálogo, índice, frame,
cache ou qualquer outro estado. Caso contrário, publica/certifica `current + 1` antes do efeito que
ela fenceia. Clamp em MAX, wrap, reset, reuse e “continuar sem incrementar” são proibidos.

Isso vale sem exceção para marker/checkpoint publication, reader registry, database/runtime,
coordination namespace, WAL mutation, device replay, index namespace e per-index mutation
generations. MAX em qualquer safety generation persistida/shared é terminal para novas mutações da
database source: status `SAFETY_GENERATION_EXHAUSTED(domain)` e
`GrafxIntegrityError(field="safety_generation_exhausted", retryable=False)`; não existe DDL, rekey,
carrier replacement, rebootstrap de coordinator ou database-runtime rotation **in-place** que
contorne o incremento impossível. Em particular, index generation MAX não pode ser “resolvida” por
DROP/ALTER/CREATE, pois tocar o catálogo/identity antiga também exigiria seu permit.

As únicas saídas são restore cold de backup anterior ao exhaustion ou export lógico cold, read-only e
diretamente certificado sob directory fence/zero participants, para genesis com **novo
`database_uuid`**; o export não registra reader/pin, não escreve um byte da source e deixa seus
carriers/identities imutáveis para forensic. A source não é reaberta para write. `pool.derived_epoch`
é a única generation exclusivamente process-local: no máximo, o discard recusa antes de remover
frame/cache, envenena e fecha o pool antigo; somente um pool novo, sem pins/frames/graphs herdados,
pode cold-certificar a mesma database não exaurida. Nenhuma proof/token/cache atravessa troca de
database identity ou pool.

### 0.1 Codec escalar único V7

Todo layout, digest preimage, fingerprint, manifest, plan, proof, trailer, state slot e enum chamado
“V7” neste contrato usa `ScalarCodecV1`, ainda que a figura local omita a anotação:

- `u8/u16/u32/u64` são unsigned little-endian de largura exata; range é validado **antes** de encode e
  decode não aceita sign extension, varint, native endian ou largura alternativa;
- input inteiro precisa ser exact built-in `int`; `bool`, enum/subclasse hostil, coerção e objeto com
  `__index__` são recusados antes de I/O/lock;
- `bytes[N]` exige exact built-in bytes e comprimento N; strings são UTF-8 somente quando o layout
  declara string, com length prefix explícito e regras de normalização próprias;
- todo count/length tem limite declarado, soma/offset checked u64, igualdade exata com a quantidade ou
  payload realmente decodificado e zero trailing bytes;
- enum tem tabela numérica versionada; zero/reserved e unknown são recusados salvo quando a tabela
  declara explicitamente um zero canônico. Campo `reserved_zero` precisa ser byte a byte zero;
- hash/CRC é calculado sobre esses bytes canônicos; decode seguido de canonical re-encode precisa ser
  bit-identical. Um decoder não preserva spelling alternativo nem “normaliza” bytes malformed.

Ausência de tabela/width para novo discriminante persistido ou hasheado é blocker de formato, não
liberdade de implementação. Gates comuns fazem round-trip de min/max de cada scalar e todos os enums,
e matam mutantes native/big-endian, overflow/truncate, bool/subclasse, zero/unknown/reserved aceito,
count/length mismatch, trailing byte e re-encode não canônico.

### 0.1.1 Registry único de domains SHA-256 V7

Esta é a **única** fonte normativa dos domain separators V7. Os bytes da coluna Domain são ASCII
exatos; somente uma entrada que mostra `\0` contém NUL terminal. Não existe NUL implícito, spelling
curto, case folding nem `str.encode()` dependente da implementação. Nas tabelas, `R` significa o
record `ScalarCodecV1` integral, do magic ao CRC e EOF; `T(...)` é a concatenação de fields nas larguras
e ordem citadas; `V` é o vector canônico já length/count-framed. “1” significa exatamente uma preimage
por objeto lógico; intervalos são por container. O SHA é sempre `SHA256(domain_bytes || preimage)`.
Campo local com outro domain/preimage é malformed mesmo se o SHA coincidir por acaso.

| Field/type V1 | Identifier e domain bytes exatos | Total-order / cardinalidade da preimage |
|---|---|---|
| `ArtifactImageVector.sha256` | `DOMAIN_ARTIFACT_IMAGE_VECTOR_V1=b"OKAIVECFP7"` | `R`; 1 |
| `GenesisImageManifest.sha256` | `DOMAIN_GENESIS_IMAGE_MANIFEST_V1=b"OKIMGMAFP7"` | `R`; 1 |
| `CarrierManifest.sha256` | `DOMAIN_CARRIER_MANIFEST_V1=b"OKCARRMFP7"` | `R`; 1 |
| `CutoverSpacePlan.sha256` | `DOMAIN_CUTOVER_SPACE_PLAN_V1=b"OKSPACEFP7"` | `R`; 1 |
| `BindSubstepBase.sha256` | `DOMAIN_BIND_SUBSTEP_BASE_RECORD_V1=b"OKBINDBFP7"` | `R`; 1 por BIND ordinal |
| `IndexRebuildSubstepBase.sha256` | `DOMAIN_INDEX_REBUILD_SUBSTEP_BASE_RECORD_V1=b"OKRBSUBFP7"` | `R`; 1 por rebuild ordinal |
| `CutoverFeasibilityCore.digest` | `DOMAIN_CUTOVER_CORE_V1=b"OKCUTCORE7"` | `R`; 1 |
| `BoundStableBindContext.sha256` | `DOMAIN_BOUND_STABLE_BIND_V1=b"OKBINDSTABLE7\0"` | `R`; 1 por BIND ordinal |
| `NormalizedOperationProjection.sha256` | `DOMAIN_NORMALIZED_PROJECTION_V1=b"OKNRPRJFP7"` | `R`; `N+2` |
| `CutoverFeasibilityManifest.digest` | `DOMAIN_CUTOVER_FEASIBILITY_V1=b"OKCUTFEAS7"` | `R`; 1 |
| `GenerationCapacityPlan.sha256` | `DOMAIN_GENERATION_CAPACITY_PLAN_V1=b"OKGENCAP7"` | `R`; 1 |
| `GenerationTransitionPlan.sha256` | `DOMAIN_GENERATION_TRANSITION_PLAN_V1=b"OKGTRPLFP7"` | `R`; 1 por stable operation plan |
| `GenerationSuccessorSet.sha256` | `DOMAIN_GENERATION_SUCCESSOR_SET_V1=b"OKGENSSFP7"` | `R`; 1 por outer attempt |
| `GenerationAdmissionReservation.sha256` | `DOMAIN_GENERATION_ADMISSION_V1=b"OKGENARFP7"` | `R`; 1 por claim |
| `FrozenOuterOperationPlan.sha256` | `DOMAIN_FROZEN_OUTER_PLAN_V1=b"OKFROZNFP7"` | `R`; 1 por outer operation |
| `WalEntropySeed.sha256` | `DOMAIN_WAL_ENTROPY_SEED_V1=b"OKWENTSFP7"` | `R`; 1 por substep/attempt pre-WAL |
| `AttemptFencingContext.sha256` | `DOMAIN_ATTEMPT_FENCING_CONTEXT_V1=b"OKFENCEFP7"` | `R`; 1 por substep/attempt |
| `WalPlanSeedFinal.sha256` | `DOMAIN_WAL_PLAN_SEED_FINAL_V1=b"OKWSEEDFP7"` | `R`; 1 por grouped txn |
| `WalAppendDraft.fingerprint` | `DOMAIN_WAL_APPEND_DRAFT_V1=b"OKWAPFP7"` | record bytes do magic até o último blob, fingerprint field ZERO32 e **CRC terminal excluído**; 1 |
| `LogicalOperationSubstepContext.sha256` | `DOMAIN_SUBSTEP_LOGICAL_V1=b"OKSUBLGFP7\0"` | `R`; 1 por substep |
| `ActualSubstepSourceProof.sha256` | `DOMAIN_SUBSTEP_ACTUAL_V1=b"OKSUBACT7\0"` | `R`; 1 por substep/attempt |
| `BoundOperationSubstepContext.sha256` | `DOMAIN_SUBSTEP_BOUND_V1=b"OKSUBCTX7\0"` | `R`; 1 por substep/attempt |
| `StableNamespaceBindingProjection.sha256` | `DOMAIN_STABLE_NAMESPACE_BINDING_V1=b"OKNSBNDFP7"` | `R`; 1 por directory capability projetada |
| `ControlPublicationCore.sha256` | `DOMAIN_CONTROL_PUBLICATION_CORE_V1=b"OKCTLCORE7"` | `R`; 1 por publication |
| `PendingControlPublication.sha256` | `DOMAIN_PENDING_CONTROL_PUBLICATION_V1=b"OKCTPEND7"` | `R`; 1 por publication |
| `NamespaceMutationProjection.sha256` | `DOMAIN_NAMESPACE_MUTATION_PROJECTION_V1=b"OKNSPRFP7"` | `R`; 1 por publication |
| `ControlPublicationStageProof.sha256` | `DOMAIN_CONTROL_STAGE_PROOF_V1=b"OKCTSTFP7"` | `R`; 1 por stage |
| `GenesisOrphanPartialTempProof.sha256` | `DOMAIN_GENESIS_ORPHAN_PROOF_V1=b"OKGORPFP7"` | `R`; 1 por orphan candidate |
| `CheckpointCompositePlanCore.sha256` | `DOMAIN_CHECKPOINT_COMPOSITE_CORE_V1=b"OKQCPCFP7"` | `R`; 1 por candidate |
| `CheckpointCompositeSemanticBase.sha256` | `DOMAIN_CHECKPOINT_SEMANTIC_BASE_V1=b"OKQCSBFP7"` | `R`; 1 por checkpoint stable plan |
| `CheckpointIndexManifest.sha256` | `DOMAIN_CHECKPOINT_INDEX_MANIFEST_V1=b"OKQIMNFP7"` | `R`; 1 por checkpoint target |
| `PendingCheckpointPublication.sha256` | `DOMAIN_PENDING_CHECKPOINT_V1=b"OKQCPNFP7"` | `R`; 1 por candidate |
| `CheckpointDeviceApplyManifest.sha256` | `DOMAIN_CHECKPOINT_DEVICE_MANIFEST_V1=b"OKQDVMFP7"` | `R`; 1 por checkpoint manifest |
| `RecoveryApplyPlan.sha256` | `DOMAIN_RECOVERY_APPLY_PLAN_V1=b"OKRCPLNFP7"` | `R`; 1 por recovery attempt |
| `ColdReplayPlan.sha256` | `DOMAIN_COLD_REPLAY_PLAN_V1=b"OKCRPLNFP7"` | `R`; 1 por cold replay attempt |
| `IndexFencePlan.sha256` | `DOMAIN_INDEX_FENCE_PLAN_V1=b"OKIXFENFP7"` | `R`; 1 por recovery/catch-up plan, count pode ser 0 |
| `CommittedHistoryEvidence.sha256` | `DOMAIN_COMMITTED_HISTORY_EVIDENCE_V1=b"OKHISTEFP7"` | `R`; 1 por sealed scan |
| `WriteSnapshotProof.sha256` | `DOMAIN_WRITE_SNAPSHOT_PROOF_V1=b"OKWSPRFFP7"` | `R`; 1 por mutator attempt |
| `WalFrontierCertification.sha256` | `DOMAIN_WAL_FRONTIER_CERTIFICATION_V1=b"OKWFRNFP7"` | `R`; 1 por certified frontier process-local |
| derived-own-commit frontier evidence | `DOMAIN_OWN_COMMIT_FRONTIER_EVIDENCE_V1=b"OKWOCFEV7"` | tuple fixed-width da seção 8.4.1; 1 por grouped commit local |
| `WalPublicationProof.sha256` | `DOMAIN_WAL_PUBLICATION_PROOF_V1=b"OKWALPFFP7"` | `R`; 1 por reader publicável/action 16 |
| `ReaderRegistrySnapshot.sha256` | `DOMAIN_READER_REGISTRY_SNAPSHOT_V1=b"OKRRSNFP7"` | `R`; 1 por scan |
| `ProcessRegistryTransitionProof.sha256` | `DOMAIN_PROCESS_REGISTRY_TRANSITION_PROOF_V1=b"OKPRTRNFP7"` | `R`; 1 por lifecycle CAS |
| `ProtectedWalRangeSet.sha256` | `DOMAIN_PROTECTED_WAL_RANGE_SET_V1=b"OKPWRGFP7"` | `R`; 1 por scan-pin ACTIVE |
| PROCESS WAL scan-pin snapshot | `DOMAIN_WAL_SCAN_PIN_SNAPSHOT_V1=b"OKWPSNFP7"` | `R` de `ProcessRegistryStateV1`; 1 por mutation draft |
| `WalFeatureFenceProof.sha256` | `DOMAIN_WAL_FEATURE_FENCE_PROOF_V1=b"OKWFFPFP7"` | `R`; 1 por scan/frontier |
| `WalPhysicalScanEvidence.sha256` | `DOMAIN_WAL_PHYSICAL_SCAN_EVIDENCE_V1=b"OKWSCANFP7"` | `R`; 1 por sealed scan |
| `CheckpointDirectProof.sha256` | `DOMAIN_CHECKPOINT_DIRECT_PROOF_V1=b"OKQDIRFP7"` | `R`; 1 por recovery/cold plan |
| `WalFrontierEvidenceEnvelope.sha256` | `DOMAIN_WAL_FRONTIER_EVIDENCE_ENVELOPE_V1=b"OKWFENVP7"` | `R`; 1 por frontier current |
| `ExactEvidenceReceipt.sha256` | `DOMAIN_EXACT_EVIDENCE_RECEIPT_V1=b"OKEVRCPFP7"` | `R`; 1 por receipt field mapeado |
| forensic ledger COW progress | `DOMAIN_FORENSIC_LEDGER_COW_PROGRESS_V1=b"OKFLCWFP7"` | `R`; 1 por adoption attempt |
| `WalMutationDraft.fingerprint` | `DOMAIN_WAL_MUTATION_DRAFT_V1=b"OKWMUTFP7"` | `R`; 1 por exact low-level mutation |
| `WalMutationExactSet.sha256` | `DOMAIN_WAL_MUTATION_EXACT_SET_V1=b"OKWMSETFP7"` | `R`; 1 por draft |
| `WalImmutableTreeManifest.sha256` | `DOMAIN_WAL_IMMUTABLE_TREE_V1=b"OKWMTREFP7"` | `R`; 1 por quarantine tree |
| `ForensicLedgerAppendPlan.sha256` | `DOMAIN_FORENSIC_LEDGER_APPEND_V1=b"OKFLAPFP7"` | `R`; 1 por append/replace |
| `TailPreservationEvent.sha256` | `DOMAIN_TAIL_PRESERVATION_EVENT_V1=b"OKTPREVFP7"` | `R`; 1 por composite |
| `DiscardedRangeProjection.sha256` | `DOMAIN_DISCARDED_RANGE_PROJECTION_V1=b"OKDSRNGFP7"` | `R`; 1 por tail disposition |
| `LedgerPredecessorBoundary.sha256` | `DOMAIN_LEDGER_PREDECESSOR_BOUNDARY_V1=b"OKLDBNDFP7"` | `R`; 1 por tail plan |
| `TailMutationIdentitySeed.sha256` | `DOMAIN_TAIL_IDENTITY_SEED_V1=b"OKTAILSFP7"` | `R`; 1 por tail plan |
| `BaseTailMutationIntent.sha256` | `DOMAIN_BASE_TAIL_INTENT_V1=b"OKTAILBFP7"` | `R`; 1 por tail plan |
| `TailDisposition.sha256` | `DOMAIN_TAIL_DISPOSITION_V1=b"OKTAILDFP7"` | `R`; 1 por recovery plan |
| `TailMutationCompositePlan.sha256` | `DOMAIN_TAIL_COMPOSITE_PLAN_V1=b"OKTAILCP7"` | `R`; 1 por truncation |
| `TailMutationExecution.sha256` | `DOMAIN_TAIL_EXECUTION_V1=b"OKTAILEFP7"` | `R`; 1 por progress state |
| `WalMonotonicProgressPlan.sha256` | `DOMAIN_WAL_MONOTONIC_PROGRESS_PLAN_V1=b"OKWMPGPLFP7"` | `R`; 1 por tail composite ou checkpoint recycle |
| `WalMonotonicProgressProof.sha256` | `DOMAIN_WAL_MONOTONIC_PROGRESS_PROOF_V1=b"OKWMPGPFFP7"` | `R`; 1 por resume attempt |
| WAL monotonic logical state | `DOMAIN_WAL_MONOTONIC_LOGICAL_STATE_V1=b"OKWMPGSTFP7"` | tuple/projection canônica da seção 10.3; `step_count+1` por plan |
| `CommitRedoResult.sha256` | `DOMAIN_COMMIT_REDO_RESULT_V1=b"OKCRDRFP7"` | `R`; 1 por apply bem-sucedido |
| `RuntimeDeviceReplayProof.sha256` | `DOMAIN_RUNTIME_DEVICE_REPLAY_PROOF_V1=b"OKRDRPFFP7"` | `R`; 1 por `(runtime,Q,P,materialization step)`; só uma current, retenções estreitas explícitas |
| `OperationBudget.sha256` | `DOMAIN_OPERATION_BUDGET_V1=b"OKBUDGTFP7"` | `R`; 1 por entrada pública |
| `OperationAttempt.sha256` | `DOMAIN_OPERATION_ATTEMPT_V1=b"OKATMPTFP7"` | `R`; 1..17 por budget |
| `BoundaryProof.sha256` | `DOMAIN_BOUNDARY_PROOF_V1=b"OKBNDRYFP7"` | `R`; 1 por boundary authority |
| `RequestedCheckpointBoundaryProof.sha256` | `DOMAIN_REQUESTED_BOUNDARY_PROOF_V1=b"OKQBOUND7\0"` | `R`; 1 por checkpoint attempt |
| `TerminalFloorReleasePlan.sha256` | `DOMAIN_TERMINAL_FLOOR_RELEASE_PLAN_V1=b"OKTFRPLFP7"` | `R`; 1 por release set |
| `TerminalFloorReleaseRecipe.sha256` | `DOMAIN_TERMINAL_FLOOR_RELEASE_RECIPE_V1=b"OKTFRRCFP7"` | `R`; 1 por checkpoint release set |
| `SystemFloorInstallEntryBase.sha256` | `DOMAIN_SYSTEM_FLOOR_INSTALL_ENTRY_BASE_V1=b"OKSFINBFP7"` | `R`; 1 por SYSTEM record |
| `SystemFloorInstallBatchPlan.sha256` | `DOMAIN_SYSTEM_FLOOR_INSTALL_BATCH_PLAN_V1=b"OKSFIBPFP7"` | `R`; 1 por action24 set |

Identities, projections e collections que não são um record integral usam a tabela seguinte; a ordem
mostrada é parte da preimage, nunca kwargs/map host:

| Field V1 | Identifier e domain bytes exatos | Total-order / cardinalidade da preimage |
|---|---|---|
| `baseline_cutover_identity` | `DOMAIN_CUTOVER_BASE_V1=b"OKCUTBASE7\0"` | `T(database_uuid,nonce,B,baseline hashes)`; 1 |
| BIND `outer_operation_identity` | `DOMAIN_BIND_OUTER_V1=b"OKBINDOUTER7\0"` | `T(baseline identity,core digest)`; 1 |
| BIND `full_plan_sha256` | `DOMAIN_BIND_FULL_PLAN_V1=b"OKBINDPLAN7"` | `T(outer,core,N,ordered base SHAs)`; 1 |
| `bind_plan_source_digest` | `DOMAIN_BIND_PLAN_SOURCE_V1=b"OKBINDSRC7\0"` | `T(baseline identity,N,ordered base bytes)`; 1 |
| logical BIND chain successor | `DOMAIN_BIND_LOGICAL_CHAIN_V1=b"OKBINDCHAIN7"` | prefix, then ordinal 0..N-1, then BOUND; `N+2` |
| `reservation_plan_identity` | `DOMAIN_SPACE_RESERVATION_ID_V1=b"OKSPRID7"` | `T(database,nonce,ordinal,kind,path,sizes)`; 1/resource |
| persisted `domain_identity` | `DOMAIN_GENERATION_IDENTITY_V1=b"OKGENDOM7\0"` | `T(database,domain_kind_u8,stable_scope_key)`; 1/domain |
| PROCESS `domain_identity` | `DOMAIN_PROCESS_IDENTITY_V1=b"OKPRDOM7"` | `T(database,coordinator_incarnation)`; 1/incarnation |
| rebuild base/outer/full plan | `DOMAIN_REBUILD_BASE_V1=b"OKRBLDBASE7\0"`; `DOMAIN_REBUILD_OUTER_V1=b"OKRBLDOUT7\0"`; `DOMAIN_REBUILD_PLAN_V1=b"OKRBLDPLAN7\0"` | cada `T(...)` na ordem da seção 5.1; 1/outer |
| `operation_context_sha256` | `DOMAIN_OPERATION_CONTEXT_V1=b"OKOPCTX7"` | `T(logical_stable,actual_source,fencing)`; 1/attempt |
| `proof_nonce_sha256` | `DOMAIN_PROOF_NONCE_V1=b"OKPROOFNONCE7\0"` | tuple fixed-width integral da seção 5.4; 1/attempt, single-use |
| generation semantic edge | `DOMAIN_GENERATION_SEMANTIC_EDGE_V1=b"OKGTREDGE7"` | tuple fixed-width integral da seção 3.1.1; 1/stable transition edge |
| recovery/cold authority nonce | `DOMAIN_RECOVERY_AUTHORITY_NONCE_V1=b"OKRCAUTN7"`; `DOMAIN_COLD_AUTHORITY_NONCE_V1=b"OKCRAUTN7"` | tuples fixed-width integrais da seção 5.4; 1/attempt, single-use |
| legacy/new index identity | `DOMAIN_INDEX_LEGACY_V1=b"OKIDXLEG7"`; `DOMAIN_INDEX_NEW_V1=b"OKIDXNEW7"` | tuples da seção 5.2; 1/index |
| rebuild stable logical | `DOMAIN_REBUILD_STABLE_V1=b"OKRBLDSTABLE7\0"` | tuple logical da seção 5.2; 1/substep |
| semantic/raw effect/apply | `DOMAIN_SUBSTEP_SEMANTIC_EFFECT_V1=b"OKSUBSEM7\0"`; `DOMAIN_SUBSTEP_SEMANTIC_APPLY_V1=b"OKSUBAPSEM7"`; `DOMAIN_SUBSTEP_RAW_EFFECT_V1=b"OKSUBRAW7"`; `DOMAIN_SUBSTEP_RAW_APPLY_V1=b"OKSUBAPRAW7"` | vectors ordered por semantic ordinal/raw LSN; 1/substep |
| substep source | `DOMAIN_SUBSTEP_SOURCE_V1=b"OKSUBSRC7\0"` | plan body integral; 1/outer |
| `PendingOuterOperationMap.sha256` | `DOMAIN_PENDING_OUTER_MAP_V1=b"OKPENDMAP7\0"` | `R` sorted persisted identity; 0→ZERO32, senão 1 |
| checkpoint pending projection | `DOMAIN_CHECKPOINT_PENDING_PROJECTION_V1=b"OKQPEND7"` | pending entries sorted identity; 0→ZERO32, senão 1 |
| active/terminal/release projections | `DOMAIN_CHECKPOINT_ACTIVE_SET_V1=b"OKQACTV7"`; `DOMAIN_CHECKPOINT_TERMINAL_SET_V1=b"OKQTERM7"`; `DOMAIN_CHECKPOINT_RELEASE_SET_V1=b"OKQREL7"` | filtros disjuntos da mesma lista sorted; cada 0→ZERO32, senão 1 |
| checkpoint device target set | `DOMAIN_CHECKPOINT_DEVICE_TARGET_SET_V1=b"OKQDVTSET7"` | `LE32(count)||device_target[0..count)` em ordinal; 0→ZERO32, senão 1 |
| runtime redo target set | `DOMAIN_RUNTIME_DEVICE_TARGET_SET_V1=b"OKRDVTSET7"` | page targets e barrier targets projetados do `CommitRedoResultV1`, em suas ordens canônicas; 1 por result |
| checkpoint source effect leaf/node | `DOMAIN_CHECKPOINT_EFFECT_LEAF_V1=b"OKQEFLEAF7"`; `DOMAIN_CHECKPOINT_EFFECT_NODE_V1=b"OKQEFNODE7"` | leaves em `(lsn,segment,offset,effect_ordinal)`; 1..1048576 por target |
| index carrier plan | `DOMAIN_INDEX_CARRIER_PLAN_V1=b"OKIXCPLAN7\0"` | tuple integral da seção 5.2; 1/index effect set |
| authority body wrappers | `DOMAIN_NAMESPACE_AUTHORITY_V1=b"OKNSAUT7"`; `DOMAIN_CONTROL_AUTHORITY_V1=b"OKCFAUT7"`; `DOMAIN_WAL_FRONTIER_AUTHORITY_V1=b"OKWFAUT7"`; `DOMAIN_DEVICE_AUTHORITY_V1=b"OKDVAUT7"` | authority body bytes; exatamente 1/authority |
| catalog/history roots | `DOMAIN_CATALOG_V1=b"OKCATV7\0"`; `DOMAIN_HISTORY_BASE_V1=b"OKHIST7\0"`; `DOMAIN_HISTORY_LEGACY_V1=b"OKHISTL7"`; `DOMAIN_HISTORY_CUTOVER_V1=b"OKHISTC7"` | ordem da seção 7.4; 1/horizon |
| transaction/BIND/rebuild logical context | `DOMAIN_TRANSACTION_STABLE_CONTEXT_V1=b"OKWSTXN7"`; `DOMAIN_BIND_STABLE_CONTEXT_V1=b"OKWSBIND7\0"`; `DOMAIN_REBUILD_STABLE_CONTEXT_V1=b"OKWSIDX7"` | exact logical core; 1/substep |
| SYSTEM floor set | `DOMAIN_SYSTEM_FLOOR_SET_V1=b"OKSYSSET7"` | records sorted pela floor key/entry-base SHA; 0→ZERO32, senão 1 |
| SYSTEM floor pending-entry identity | `DOMAIN_SYSTEM_FLOOR_PENDING_ENTRY_V1=b"OKSFPEND7"` | projection de uma única pending entry NONTERMINAL com floor key ZERO; 1 por SYSTEM record |
| WAL mutation payload/parents | `DOMAIN_WAL_MUTATION_PAYLOAD_V1=b"OKWMPAY7"`; `DOMAIN_WAL_PARENT_SET_V1=b"OKWMPAR7"` | target-kind+payload, ou parents sorted canonical; 1/draft |
| tail quarantine/composite/event/publication identity | `DOMAIN_TAIL_QUARANTINE_ID_V1=b"OKTAILQID7"`; `DOMAIN_TAIL_COMPOSITE_ID_V1=b"OKTAILCID7"`; `DOMAIN_TAIL_EVENT_ID_V1=b"OKTAILEID7"`; `DOMAIN_TAIL_PUBLICATION_ID_V1=b"OKTPPUB7"` | tuples acíclicas da seção 8.4.2; 1/composite |

Todo identifier acima aparece uma vez nesta tabela. Se uma seção local precisar da fórmula, ela
referencia o identifier e o codec/tuple, sem repetir os bytes. Um gate mecânico enumera este registry,
exige unicidade tanto de identifier quanto de `domain_bytes`, canonicaliza golden min/max e procura em
todo source/spec qualquer literal `OK*` usado como SHA domain fora desta seção. Reuso intencional é
expresso pelo **mesmo identifier**, nunca por copiar o literal; domain collision entre fields distintos,
NUL adicionado/removido, record-vs-fields-only ou vector order diferente bloqueia a build.

O mesmo single source vale aos magic bytes. Cada codec local referencia o identifier desta tabela e
nenhum repete o literal:

| Codec/type | Identifier e magic bytes exatos |
|---|---|
| `BootstrapStateV7` | `MAGIC_BOOTSTRAP_STATE_V7=b"OKBOOT7\0"` |
| `ArtifactImageVectorV1` | `MAGIC_ARTIFACT_IMAGE_VECTOR_V1=b"OKAIVEC7"` |
| `GenesisImageManifestV1` | `MAGIC_GENESIS_IMAGE_MANIFEST_V1=b"OKIMGMA7"` |
| `CarrierManifestV1` | `MAGIC_CARRIER_MANIFEST_V1=b"OKCARRM7"` |
| `CutoverSpacePlanV1` | `MAGIC_CUTOVER_SPACE_PLAN_V1=b"OKSPACE7"` |
| `SpaceReservationStageProofV1` | `MAGIC_SPACE_STAGE_PROOF_V1=b"OKSPSTG7"` |
| `ManifestIdentityStageProofV1` | `MAGIC_MANIFEST_IDENTITY_STAGE_V1=b"OKMISTG7"` |
| `WalStructuralTemplateV1` | `MAGIC_WAL_STRUCTURAL_TEMPLATE_V1=b"OKWSTMP7"` |
| `FenceWalStructuralTemplateV1` | `MAGIC_FENCE_WAL_TEMPLATE_V1=b"OKWFENC7"` |
| `BindSubstepBaseV1` | `MAGIC_BIND_SUBSTEP_BASE_V1=b"OKBINDB7"` |
| `WalStructuralTemplateVectorV1` | `MAGIC_WAL_STRUCTURAL_VECTOR_V1=b"OKWSTV7\0"` |
| `CutoverFeasibilityCoreV1` | `MAGIC_CUTOVER_CORE_V1=b"OKCTCOR7"` |
| `BoundStableBindContextV1` | `MAGIC_BOUND_BIND_CONTEXT_V1=b"OKBNDCX7"` |
| `NormalizedOperationProjectionV1` | `MAGIC_NORMALIZED_PROJECTION_V1=b"OKNRPRJ7"` |
| `NormalizedCutoverHistoryChainV1` | `MAGIC_NORMALIZED_HISTORY_CHAIN_V1=b"OKHCHN7\0"` |
| `CutoverStructuralPlanVectorV1` | `MAGIC_CUTOVER_STRUCTURAL_VECTOR_V1=b"OKCSPV7\0"` |
| `CutoverFeasibilityManifestV1` | `MAGIC_CUTOVER_MANIFEST_V1=b"OKCUTMF7"` |
| `GenerationCapacityPlanV1` | `MAGIC_GENERATION_CAPACITY_PLAN_V1=b"OKGENCP7"` |
| `ProcessRegistryStateV1` | `MAGIC_PROCESS_REGISTRY_STATE_V1=b"OKPRGST7"` |
| `GenerationTransitionPlanV1` | `MAGIC_GENERATION_TRANSITION_PLAN_V1=b"OKGTRPL7"` |
| `GenerationSuccessorSetV1` | `MAGIC_GENERATION_SUCCESSOR_SET_V1=b"OKGENSS7"` |
| `GenerationAdmissionReservationV1` | `MAGIC_GENERATION_ADMISSION_V1=b"OKGENAR7"` |
| `IndexRebuildSubstepBaseV1` | `MAGIC_INDEX_REBUILD_BASE_V1=b"OKRBSUB7"` |
| `FrozenOuterOperationPlanV1` | `MAGIC_FROZEN_OUTER_PLAN_V1=b"OKFROZN7"` |
| `WalEntropySeedV1` | `MAGIC_WAL_ENTROPY_SEED_V1=b"OKWENTS7"` |
| `AttemptFencingContextV1` | `MAGIC_ATTEMPT_FENCING_CONTEXT_V1=b"OKFENCE7"` |
| `WalPlanSeedFinalV1` | `MAGIC_WAL_PLAN_SEED_FINAL_V1=b"OKWSEED7"` |
| `WalAppendDraftV1` | `MAGIC_WAL_APPEND_DRAFT_V1=b"OKWAPDR7"` |
| `CommitPayloadV2` | `MAGIC_COMMIT_PAYLOAD_V2=b"OKCMV2\0\0"` |
| `OperationSubstepTrailerV1` | `MAGIC_SUBSTEP_TRAILER_V1=b"OKSUBTR7"` |
| `LogicalBoundOperationSubstepContextV1` | `MAGIC_SUBSTEP_LOGICAL_V1=b"OKSUBLG7"` |
| `ActualSubstepSourceProofV1` | `MAGIC_SUBSTEP_ACTUAL_V1=b"OKSUBAC7"` |
| `BoundOperationSubstepContextV1` | `MAGIC_SUBSTEP_BOUND_V1=b"OKSUBCX7"` |
| `OperationSubstepManifestV1` | `MAGIC_SUBSTEP_MANIFEST_V1=b"OKSUBPL7"` |
| `PendingOuterOperationMapV1` | `MAGIC_PENDING_OUTER_MAP_V1=b"OKPENDM7"` |
| `BeginPayloadV2` | `MAGIC_BEGIN_PAYLOAD_V2=b"OKBGV2\0\0"` |
| `CommitStateV1/V2` shared legacy prefix (4 bytes, única exceção à magic V7 de 8 bytes) | `MAGIC_COMMIT_STATE_SHARED=b"OGCS"` |
| `CheckpointIndexManifestV1` | `MAGIC_CHECKPOINT_INDEX_MANIFEST_V1=b"OKIXMF7\0"` |
| `CheckpointDeviceApplyManifestV1` | `MAGIC_CHECKPOINT_DEVICE_MANIFEST_V1=b"OKQDVM7\0"` |
| `CheckpointIndexStateV1` | `MAGIC_CHECKPOINT_INDEX_STATE_V1=b"OKIXCP7\0"` |
| `ControlPublicationCoreV1` | `MAGIC_CONTROL_PUBLICATION_CORE_V1=b"OKCTLCO7"` |
| `ControlPublicationWitnessV1` | `MAGIC_CONTROL_PUBLICATION_WITNESS_V1=b"OKCTLWIT"` |
| `PendingControlPublicationV1` | `MAGIC_PENDING_CONTROL_PUBLICATION_V1=b"OKCTPEN7"` |
| `NamespaceSourceAuthorityV1` | `MAGIC_NAMESPACE_AUTHORITY_V1=b"OKNSAU7\0"` |
| `NamespaceInventoryManifestV1` | `MAGIC_NAMESPACE_INVENTORY_V1=b"OKNSINV7"` |
| `ControlFileSourceAuthorityV1` | `MAGIC_CONTROL_FILE_AUTHORITY_V1=b"OKCFAU7\0"` |
| `WalPublicationFrontierAuthorityV1` | `MAGIC_WAL_FRONTIER_AUTHORITY_V1=b"OKWFAU7\0"` |
| `DeviceApplyTargetSetAuthorityV1` | `MAGIC_DEVICE_AUTHORITY_V1=b"OKDVAU7\0"` |
| `NamespaceMutationProjectionV1` | `MAGIC_NAMESPACE_MUTATION_PROJECTION_V1=b"OKNSPRJ7"` |
| `ControlPublicationStageProofV1` | `MAGIC_CONTROL_STAGE_PROOF_V1=b"OKCTSTG7"` |
| `GenesisOrphanPartialTempProofV1` | `MAGIC_GENESIS_ORPHAN_PROOF_V1=b"OKGORPH7"` |
| `CheckpointCompositePlanCoreV1` | `MAGIC_CHECKPOINT_COMPOSITE_CORE_V1=b"OKQCPCO7"` |
| `CheckpointCompositeSemanticBaseV1` | `MAGIC_CHECKPOINT_SEMANTIC_BASE_V1=b"OKQCSBV7"` |
| `PendingCheckpointPublicationV1` | `MAGIC_PENDING_CHECKPOINT_V1=b"OKQCPEN7"` |
| `StableNamespaceBindingProjectionV1` | `MAGIC_STABLE_NAMESPACE_BINDING_V1=b"OKNSBND7"` |
| `GrafxMetaV2` | `MAGIC_GRAFX_META_V2=b"OKWALV2\0"` |
| `IdentityControlPayloadV1` | `MAGIC_IDENTITY_CONTROL_PAYLOAD_V1=b"OKIDV7\0\0"` |
| `WriteSnapshotProofV1` | `MAGIC_WRITE_SNAPSHOT_PROOF_V1=b"OKWSPRF7"` |
| `IndexFencePlanV1` | `MAGIC_INDEX_FENCE_PLAN_V1=b"OKIXFEN7"` |
| `CommittedHistoryEvidenceV1` | `MAGIC_COMMITTED_HISTORY_EVIDENCE_V1=b"OKHISTE7"` |
| `RecoveryApplyPlanV1` | `MAGIC_RECOVERY_APPLY_PLAN_V1=b"OKRCPLN7"` |
| `ColdReplayPlanV1` | `MAGIC_COLD_REPLAY_PLAN_V1=b"OKCRPLN7"` |
| `RecoveryApplyAuthorityV1` | `MAGIC_RECOVERY_AUTHORITY_V1=b"OKRCAUT7"` |
| `ColdReplayAuthorityV1` | `MAGIC_COLD_AUTHORITY_V1=b"OKCRAUT7"` |
| `WalFrontierCertificationV1` | `MAGIC_WAL_FRONTIER_CERTIFICATION_V1=b"OKWFRNT7"` |
| `WalPublicationProofV1` | `MAGIC_WAL_PUBLICATION_PROOF_V1=b"OKWALPF7"` |
| `ReaderRegistrySnapshotV1` | `MAGIC_READER_REGISTRY_SNAPSHOT_V1=b"OKRRSNP7"` |
| `ProcessRegistryTransitionProofV1` | `MAGIC_PROCESS_REGISTRY_TRANSITION_PROOF_V1=b"OKPRTRN7"` |
| `ProtectedWalRangeSetV1` | `MAGIC_PROTECTED_WAL_RANGE_SET_V1=b"OKPWRNG7"` |
| `WalFeatureFenceProofV1` | `MAGIC_WAL_FEATURE_FENCE_PROOF_V1=b"OKWFFPR7"` |
| `WalPhysicalScanEvidenceV1` | `MAGIC_WAL_PHYSICAL_SCAN_EVIDENCE_V1=b"OKWSCAN7"` |
| `CheckpointDirectProofV1` | `MAGIC_CHECKPOINT_DIRECT_PROOF_V1=b"OKQDIRP7"` |
| `WalFrontierEvidenceEnvelopeV1` | `MAGIC_WAL_FRONTIER_EVIDENCE_ENVELOPE_V1=b"OKWFENV7"` |
| `ExactEvidenceReceiptV1` | `MAGIC_EXACT_EVIDENCE_RECEIPT_V1=b"OKEVRCP7"` |
| `ForensicLedgerCowProgressProofV1` | `MAGIC_FORENSIC_LEDGER_COW_PROGRESS_V1=b"OKFLCWP7"` |
| `BoundaryProofV1` | `MAGIC_BOUNDARY_PROOF_V1=b"OKBNDRY7"` |
| `RequestedCheckpointBoundaryProofV1` | `MAGIC_REQUESTED_BOUNDARY_PROOF_V1=b"OKQREQB7"` |
| `TerminalFloorReleasePlanV1` | `MAGIC_TERMINAL_FLOOR_RELEASE_PLAN_V1=b"OKTFRPL7"` |
| `TerminalFloorReleaseRecipeV1` | `MAGIC_TERMINAL_FLOOR_RELEASE_RECIPE_V1=b"OKTFRRC7"` |
| `SystemFloorInstallEntryBaseV1` | `MAGIC_SYSTEM_FLOOR_INSTALL_ENTRY_BASE_V1=b"OKSFINB7"` |
| `SystemFloorInstallBatchPlanV1` | `MAGIC_SYSTEM_FLOOR_INSTALL_BATCH_PLAN_V1=b"OKSFIBP7"` |
| `WalCoordinationSlotV1` | `MAGIC_WAL_COORDINATION_SLOT_V1=b"OKWCOOR7"` |
| `SystemOperationFloorMapSlotV1` | `MAGIC_SYSTEM_FLOOR_MAP_V1=b"OKSYSFL7"` |
| `WalMutationDraftV1` | `MAGIC_WAL_MUTATION_DRAFT_V1=b"OKWMUT7\0"` |
| `WalMutationOperationBodyV1` | `MAGIC_WAL_MUTATION_BODY_V1=b"OKWMBOD7"` |
| `WalMutationExactSetV1` | `MAGIC_WAL_MUTATION_EXACT_SET_V1=b"OKWMSET7"` |
| `AppendRangeSetPayloadV1` | `MAGIC_WAL_APPEND_PAYLOAD_V1=b"OKWMAP17"` |
| `SegmentCreateOrRollImagePayloadV1` | `MAGIC_WAL_SEGMENT_PAYLOAD_V1=b"OKWMSG27"` |
| `SuffixCutSetPayloadV1` | `MAGIC_WAL_SUFFIX_PAYLOAD_V1=b"OKWMTR37"` |
| `PrefixSegmentSetPayloadV1` | `MAGIC_WAL_PREFIX_PAYLOAD_V1=b"OKWMRC47"` |
| `QuarantineRepairBundlePayloadV1` | `MAGIC_WAL_QUARANTINE_PAYLOAD_V1=b"OKWMQR57"` |
| `WalNamespaceRenamePayloadV1` | `MAGIC_WAL_RENAME_PAYLOAD_V1=b"OKWMRN67"` |
| `WalNamespaceDeletePayloadV1` | `MAGIC_WAL_DELETE_PAYLOAD_V1=b"OKWMDL77"` |
| `WalImmutableTreeManifestV1` | `MAGIC_WAL_IMMUTABLE_TREE_V1=b"OKWMTRE7"` |
| `ForensicLedgerAppendPlanV1` | `MAGIC_FORENSIC_LEDGER_APPEND_V1=b"OKFLAPN7"` |
| `TailPreservationEventV1` | `MAGIC_TAIL_PRESERVATION_EVENT_V1=b"OKTPREV7"` |
| `TailPreservationEventAdoptionProofV1` | `MAGIC_TAIL_EVENT_ADOPTION_PROOF_V1=b"OKTPRAP7"` |
| `IndexNamespaceCarrierSlotV1` | `MAGIC_INDEX_NAMESPACE_CARRIER_V1=b"OKIXNSC7"` |
| `IndexViewCarrierSlotV1` | `MAGIC_INDEX_VIEW_CARRIER_V1=b"OKIXVW7\0"` |
| `IndexIdentityBindingV2` | `MAGIC_INDEX_IDENTITY_BINDING_V2=b"OKIXID7\0"` |
| `DiscardedRangeProjectionV1` | `MAGIC_DISCARDED_RANGE_PROJECTION_V1=b"OKDSRNG7"` |
| `LedgerPredecessorBoundaryV1` | `MAGIC_LEDGER_PREDECESSOR_BOUNDARY_V1=b"OKLDBND7"` |
| `TailMutationIdentitySeedV1` | `MAGIC_TAIL_IDENTITY_SEED_V1=b"OKTAILS7"` |
| `BaseTailMutationIntentV1` | `MAGIC_BASE_TAIL_INTENT_V1=b"OKTAILB7"` |
| `TailDispositionV1` | `MAGIC_TAIL_DISPOSITION_V1=b"OKTAILD7"` |
| `TailMutationCompositePlanV1` | `MAGIC_TAIL_COMPOSITE_PLAN_V1=b"OKTAILC7"` |
| `TailMutationExecutionV1` | `MAGIC_TAIL_EXECUTION_V1=b"OKTAILE7"` |
| `WalMonotonicProgressPlanV1` | `MAGIC_WAL_MONOTONIC_PROGRESS_PLAN_V1=b"OKWMPGL7"` |
| `WalMonotonicProgressProofV1` | `MAGIC_WAL_MONOTONIC_PROGRESS_PROOF_V1=b"OKWMPGF7"` |
| `PreserveAndLedgerSemanticBodyV1` | `MAGIC_PRESERVE_LEDGER_BODY_V1=b"OKTLPSV7"` |
| `TruncateSemanticBodyV1` | `MAGIC_TRUNCATE_BODY_V1=b"OKTLTRV7"` |
| `CommitRedoResultV1` | `MAGIC_COMMIT_REDO_RESULT_V1=b"OKCRDOR7"` |
| `RuntimeDeviceReplayProofV1` | `MAGIC_RUNTIME_DEVICE_REPLAY_PROOF_V1=b"OKRDRPF7"` |
| `OperationBudgetV1` | `MAGIC_OPERATION_BUDGET_V1=b"OKBUDGT7"` |
| `OperationAttemptV1` | `MAGIC_OPERATION_ATTEMPT_V1=b"OKATMPT7"` |

Gate de byte-contract enumera **todo** literal Python bytes com `rg -n 'b"[^"\\r\\n]*"'`, não apenas
prefixos `OK`: cada literal precisa aparecer exatamente uma vez, nesta subseção 0.1.1, e zero literal
bytes é permitido no restante do documento ou source normativo. Uma segunda passada separa identifiers
`DOMAIN_*`/`MAGIC_*`, confirma que cada layout/fórmula só referencia identifier, e verifica a largura
declarada: magic é 8 bytes salvo `MAGIC_COMMIT_STATE_SHARED`, explicitamente 4; domain tem os bytes
exatos da row. Alias referencia identifier, nunca repete bytes. O gate falha em literal ausente/
duplicado, identifier sem uso, reuse de bytes entre identifiers ou nova exceção de largura.

## 1. Decisão global

O contador de identidade deixa de ser um campo mutado em toda inserção e passa a ser um único
**piso global persistido** `F` no diretório do heap. Uma reserva durável transforma
`F := stop`; o intervalo database-bound `[start, stop)` torna-se autoridade local apenas depois da barreira WAL,
da aplicação exata, da durabilidade de `heap.dat`, da verificação direta e da liberação confirmada
do writer lease. IDs entregues, abandonados ou perdidos nunca voltam ao conjunto alocável.

O formato v2 é heap-only e usa materialização pura/COW. Insert, update, delete, overflow, relink,
extent hints, criação da primeira página e DDL+primeira row são planejados em imagens privadas.
Antes do primeiro byte WAL:

- zero `StorageDevice.allocate`, `write`, `truncate`, `recycle` ou `barrier`;
- zero frame novo residente, dirty ou aplicado;
- zero mutação em catálogo, heap ou índice vivos. As únicas mutações locais de identity permitidas
  são o ticket single-flight da reservation e, no consumer, `manager.pos += n` antes do planner; são
  monotônicas, não persistem bytes e nunca sofrem rewind em abort/conflito;
- páginas novas são endereços virtuais do plano, não alocações físicas;
- o batch completo, seus LSNs e bytes já estão congelados.

Esse zero-pre-WAL rege toda transação de data/control depois da exposição, inclusive CREATE e
BIND maintenance: nem target final **nem temp** IndexV2 pode existir antes da WAL barrier. As únicas
criações não transacionais são (a) artefatos de genesis/bootstrap autorizados pelo marker PREPARING e
pela session operator, antes de expor a database, e (b) carriers estritamente de coordenação no
coordinator namespace, sem catálogo/page/header de dados. Nenhuma dessas exceções autoriza path de
índice transacional ou é chamável por DDL/recovery comum.

A identidade lógica continua sendo **exatamente `(table_id, record_id)`**, conforme o contrato atual.
Duplicatas numéricas de `record_id` entre tabelas v1 diferentes são válidas e não são renumeradas no
upgrade. O piso global apenas torna disjuntos os números **novos pós-transição** e simplifica a reserva;
ele não transforma `record_id` isolado em chave global.

Este desenho **não promete ordem global de números entre processos**. Promete somente disjunção
entre ranges novos e não reutilização de cada identidade lógica. Dentro de um mesmo manager vivo, IDs efetivamente
entregues crescem; entre managers, um ID maior pode ser observado antes de um menor reservado por
outro processo.

O piso global permite reservar autoridade **antes** de planejar “catalogar tabela + inserir primeira
row”, sem conhecer ainda todas as tabelas consumidoras. O grant é vinculado ao database/manager, não
a uma tabela. Uma `RANGE_RESERVATION` interna, concluída e ativada antes do planejamento, fornece IDs
ao batch multi-table; o planejamento incorpora a reservation posterior ao snapshot por
`RebasedPage0Proof`. Não existe `COUNTER_CONSUMER`, placeholder, diretório RESERVED por tabela nem
primeira materialização fora do batch consumidor.

## 2. Invariantes normativos

1. Para todo `record_id` que já tenha sido consumido pelo manager, exposto por callback ou tornado
   visível em row, o heap v2 persistido deve ter `F > record_id` antes da exposição/publicação.
2. Genesis/upgrade estabelecem o `F` inicial uma única vez. Depois disso, somente um batch
   `RANGE_RESERVATION` cometido autoriza aumento lógico de `F`. Consumer nunca avança `F`;
   recovery pode apenas materializar idempotentemente um stop já cometido.
3. Um ID entregue, queimado, perdido por crash ou pertencente a um manager encerrado jamais é
   reutilizado, mesmo se nenhuma row chegou a commitá-lo.
4. Cada manager possui no máximo um range database-bound ativo. Ao precisar de `n` IDs com remainder
   menor que `n`, queima o remainder inteiro **antes** de reservar `max(identity_lease_size, n)`.
   Trocar de tabela não queima o range; um mesmo range serve intents de quaisquer tabelas do database.
5. Nenhum range fica ativo antes de writer lease liberado com sucesso. Liberação incerta queima o
   range pendente, envenena o manager e exige recovery.
6. Nenhum caminho público aceita `record_id` explícito para criação de row. A autoridade é sempre o
   manager; nenhum cursor ou handle possui `pos` nem entrega ID sem voltar a ele.
7. Sob o lock do manager, `pos` é incrementado antes de retornar ou invocar código do usuário.
   `BaseException` do usuário queima o valor já consumido.
8. Um batch cujo WAL cruzou a barreira é irrevogável. Falha posterior não autoriza rollback da
   autoridade WAL em memória; ela marca `recovery_required` e bloqueia nova escrita e todo novo
   read-view até completion exata.
9. Imagem com LSN igual e fingerprint canônico igual é no-op idempotente. LSN igual e fingerprint
   diferente é corrupção. LSN maior só subsume uma imagem se uma transação cometida posterior
   prova exatamente aquela imagem.
10. `wal_semantics_cutover_lsn`, `format_transition_lsn`, `identity_generation` e `F` nunca
    diminuem. Heap v2 e DatabaseIdentity v2 nunca fazem downgrade in-place.
11. Read-only nunca repara, migra, completa gap, aplica redo, trunca, recicla, atualiza mtime ou
    ativa grants.
12. O cache nunca é sua própria prova de durabilidade; toda certificação final lê `heap:0` e cada
    page0 de índice diretamente do device ou por uma instância fria independente.
13. Um read-view público só nasce de `WalPublicationProof`, wrapper que liga
    `WalFrontierCertification` e `RuntimeDeviceReplayProof` no mesmo P/Q/fence/runtime generation a uma
    reader key fresh/action16. Frontier prova igualdade entre durable committed tail e
    `control/commit.state.last_committed_lsn`, zero transação incomplete acima dele e zero required
    desconhecido/malformado; device prova materialização; wrapper prova o lifecycle do snapshot.
14. `flags` de todo record WAL format v1 são bytes opacos sem semântica V7. Required/skippable e
    manifest só são interpretados em record format v2 cujo LSN esteja estritamente acima do cutover
    durável de `grafx.meta`.
15. Toda página/header de índice é lida sob `INDEX_VIEW(..., SHARED)` e toda mutação é feita sob
    `INDEX_VIEW(..., EXCLUSIVE)`. O guard cobre a caminhada inteira e a materialização/certificação
    do resultado; mutation generation avança antes do primeiro efeito e não faz wrap/reset enquanto
    cache pode viver. Nenhuma mutação live ocorre antes do EXCLUSIVE nem depois de sua liberação.
16. Todo efeito de índice pertence a exatamente um scope do manifest aplicável, com operação
    semântica exata. Isso inclui CREATE, DROP, ALTER, INSERT, TOMBSTONE, REMOVE/INDEX_RECONCILE,
    RESET, REBUILD, RECONCILE_ADVANCE, MARK_STALE, CLEAR_STALE e avanço de checkpoint; ausência,
    duplicidade e extra são refusal antes de append/apply.
17. Sinal stale compartilhado é refreshable; poison local é sticky. Um header estrangeiro clean
    nunca apaga falha/uncertainty local nem torna frames/graph locais reutilizáveis por si só.
18. Nenhum resultado, iterator, callback ou intent transacional escapa antes da certificação final
    do statement. Falha depois de qualquer staging local executa rollback até um mark único que
    cobre todas as coleções mutáveis; somente IDs consumidos permanecem queimados.
19. Checkpoint/recovery não serializam `start`, `stop`, `pos`, nonce ou handle e jamais criam
    `ACTIVE`. Qualquer recovery incrementa a generation do manager, revoga authorities locais e
    considera ranges não observavelmente ativos como burn; novo refill começa no `F` certificado.
20. Toda repetição tem `max_attempts` e deadline monotônico capturado uma vez. Expiração nunca
    converte corruption/uncertainty em retry nem permite fallback a cache/lock local.
21. Uma `WalFrontierCertification` prova WAL/publicação, não prova que data pages de commits ordinários
    sobreviveram a power loss. Uma runtime fria não expõe Database/Transaction enquanto não possuir
    `RuntimeDeviceReplayProof` corrente; read-only só a deriva de `P == Q` checkpoint-certificado e
    nunca a produz por replay.
22. Ausência de controles V7 sob meta v1 e sem marker é `LEGACY_UNFENCED`, não corrupção. Depois do
    primeiro marker de bootstrap, ausência/mismatch de sidecar ou carrier é maintenance-incomplete;
    depois do fence V2 é corrupção/inconclusivo. Nenhuma dessas ausências é reparada em lazy open.

Essas invariantes governam heap v2. `_ConsumedIdentity` pertencente a plano prewrite já avançou o
manager e é queimado mesmo se o plano for descartado; a distinção fechada está na seção 7. Heap v1 em
modo compatible preserva sua semântica legada nos IDs não reservados e reporta leasing inativo.

## 3. Formato persistido e máquina de estados

### 3.1 Versões e compatibilidade

O layout binário de `FileHeader` e `TableExtent` permanece inalterado. A mudança é semântica e
kind-aware:

- `DEFAULT_FILE_FORMAT = 1` continua sendo o formato geral;
- `MAX_READABLE_FILE_FORMAT = 2`;
- `HEAP_IDENTITY_FORMAT = 2`;
- somente `FileKind.HEAP` aceita `format_version == 2`;
- META, CATALOG e INDEX v2 são recusados como schema/corrupção, nunca interpretados como heap;
- novo banco criado com leasing habilitado nasce heap v2;
- banco v1 só migra por API explícita e write-capable;
- binários pre-fence não são compatíveis com heap v2 e devem ser encerrados antes do upgrade.

Esses bullets tratam de `FileHeader.format_version`. `grafx.meta` continua com FileHeader META v1;
é o record `DatabaseIdentity` de seu slot 1 que ganha format v2/WalFeatureFence na seção 5.3. Logo
“META v2 recusado” e “DatabaseIdentity v2 obrigatório” não são afirmações conflitantes.

No SHA-base, `domain/page/file_header.py:44` fixa versão `1`, e
`FileHeader.decode` (`:146-152`) recusa qualquer versão maior globalmente. Slice 1 deve tornar essa
decisão kind-aware sem ampliar silenciosamente os outros kinds.

Configuração:

```text
identity_leasing_mode = "compatible" | "require"
identity_lease_size   = exact built-in int em [1, MAX_RECORD_ID]
```

`identity_leasing_mode` tem default explícito `"compatible"`; aceita somente exact built-in `str`
igual byte/codepoint-for-codepoint a `"compatible"` ou `"require"`. Não faz trim/casefold/coerção e
recusa bool/bytes/enum/subclasse/outro valor com
`GrafxConfigError(field="identity_leasing_mode")` antes de lock/I/O. `identity_lease_size` tem default
`64`; bool/subclasse/coerção também recusam antes de lock/I/O. Definem-se
`IDENTITY_EXHAUSTED = MAX_U64` e `MAX_RECORD_ID = MAX_U64 - 1`, em paridade com o exhausted marker
do allocator atual (`engine/heap_store.py:562-576`). Banco novo em qualquer dos dois modos nasce
heap v2; a diferença dos modos é a política ao encontrar legado v1.

Em `compatible`, v1 usa o allocator legado **somente para table IDs em
`[1, MAX_REAL_TABLE_ID]`** e reporta leasing inativo. Tabelas legadas `0xFFFFFFFE` ou `0xFFFFFFFF`
continuam legíveis, mas qualquer DML ou DDL que as toque — ALTER/DROP, índice/espaço/ref incluídos —
recusa typed antes de staging, participant/COMMIT, OCC, lock, clock ou device. DDL v1 novo também
aplica `MAX_REAL_TABLE_ID`: se
`Catalog.next_table_id()` produzir um namespace reservado, recusa typed antes de construir ou
persistir o novo `TableDef`. Assim `compatible` não promete write compatibility para esses dois IDs
legados e nunca cria novos casos. Em `require`, abrir v1 para write falha com erro typed antes de
qualquer escrita; read-only v1 continua legível. O default de formato geral não muda por causa desta
feature.

Leitura v1 FE/FF usa uma porta interna read-only que nunca insere esses números nos sets OCC públicos;
um read transaction sem efeitos não precisa publicar partições. `note_read(s)`, `note_write(s)` e sets
crus continuam recusando reserved namespaces. Se uma operação de leitura exigir upgrade para write,
maintenance, index repair ou qualquer efeito, ela recusa antes do primeiro efeito em vez de forjar uma
partição. Mutantes provam scan/lookup read-only funcional e zero reserved key no `CommitPayload`.

#### 3.1.1 Bootstrap V7 de database legado

Meta v1 sem qualquer marker V7 é o estado fechado `LEGACY_UNFENCED`. Em `compatible`, ele usa
exatamente o comportamento M1 legado e pode abrir read-only sem criar arquivo, diretório, carrier,
slot, lock persistido ou alegação de integridade V7. Status expõe
`integrity_fence_active=False`, `v7_bootstrap_phase="LEGACY_UNFENCED"` e
`device_replay_certified=False`; verify continua sendo o verifier legado e não retorna
`WalFrontierCertification`/`WalPublicationProof`/`RuntimeDeviceReplayProof` V7. Em `require`, write recusa; read-only legado continua possível com a
mesma qualificação explícita. Ausência de `checkpoint.index.*` ou de carriers nesse estado não é dano.

Todo database genesis V7 e todo cutover legado possuem dois slots alternados diretamente na root do
database, `.okto-grafx-v7.bootstrap.0` e `.okto-grafx-v7.bootstrap.1`. Cada slot é uma imagem integral:

`BootstrapKindV1` é `u8`: `INVALID=0`, `LEGACY_CUTOVER=1`, `V7_GENESIS=2`; 3..255 recusam.
`BootstrapPhaseV1` é `u8`: `INVALID=0`, `PREPARING=1`, `PREPARED=2`, `FENCED=3`, `BOUND=4`;
5..255 recusam.

```text
V7BootstrapStateV1 =
  magic[8]                    = MAGIC_BOOTSTRAP_STATE_V7
  format_version u16          = 1
  bootstrap_kind u8           # LEGACY_CUTOVER=1, V7_GENESIS=2
  phase u8                    # PREPARING=1, PREPARED=2, FENCED=3, BOUND=4
  publication_generation u64
  database_uuid[16]
  predecessor_slot_sha256[32] # zero em PREPARING; raw SHA-256 da phase anterior depois
  baseline_lsn u64            # B legado; zero no genesis
  baseline_commit_state_sha256[32]
  baseline_catalog_sha256[32]
  baseline_heap_inventory_sha256[32]
  baseline_index_inventory_sha256[32]
  feasibility_manifest_length u32 # zero somente no V7_GENESIS
  cutover_feasibility_digest[32]   # ZERO32 somente no V7_GENESIS
  feasibility_manifest_bytes[feasibility_manifest_length]
  generation_capacity_plan_length u32
  generation_capacity_plan_sha256[32]
  generation_capacity_plan_bytes[generation_capacity_plan_length]
  image_manifest_length u32
  image_manifest_sha256[32]
  image_manifest_bytes[image_manifest_length]
  carrier_manifest_length u32
  carrier_manifest_sha256[32]
  carrier_manifest_bytes[carrier_manifest_length]
  publication_witness_length u32
  publication_witness_bytes[publication_witness_length]
  crc32c u32
```

`image_manifest_bytes` é um encoding canônico, versionado e length-prefixed de todos os artefatos
duráveis de database que a phase pode criar/validar, **exceto** o próprio envelope bootstrap, com path relativo normalizado, kind, file identity quando já
existente, tamanho e raw/semantic SHA-256 target. No legacy cutover ele cobre commit.state B,
catálogo/heap/index inventory e o baseline checkpoint `(0,0,Q=B)`; no genesis cobre a lista fechada
da seção 3.1.2 e um `GenesisNamespaceBindingV1` formado por backend/volume identity, database-root
directory identity, ordered parent directory identities e final component canônico. Handles,
PID/process birth, artifact/alias inventory mutável e o SHA do próprio marker não entram nesse
binding. O image manifest autentica separadamente o alias inventory alvo dos artefatos enumerados,
sem criar self-hash quando o primeiro slot passa a existir. Extra/ausente/mismatch recusa antes de
avançar phase.
“Path relativo” é somente uma key lógica; toda resolução/mutação obedece à
`StableNamespaceCapabilityV1` da seção 5.2.1 e o manifest inclui parent/file identities + alias
inventory SHA. Normalização lexical isolada nunca é authority.

Os paths finais root-relative `.okto-grafx-v7.bootstrap.{0,1}`, seus temps internos de
`_publish_control_state_durable` e metadata efêmera do coordinator ficam obrigatoriamente fora de ambos os
manifests; incluir qualquer prefixo reservado desses no codec/preflight é malformed. Eles são o
envelope de autoridade: CRC autentica o slot corrente; PREPARING exige predecessor zero. Na primeira
publicação de cada phase seguinte, enquanto o slot anterior ainda está intacto, o publisher e o
selector exigem `predecessor_slot_sha256` igual ao raw SHA-256 daquela phase anterior diretamente
certificada, além de kind/DB UUID/manifest hashes idênticos. Esse predecessor SHA vira commitment
histórico; não é exigido que sua preimage física sobreviva ao overwrite alternado posterior.

Com exatamente dois slots, selection é fechada: `publication_generation` é numericamente igual à
phase (1..4), e cada phase é publicada no slot `((generation-1) mod 2)`. Se os dois slots são válidos e
consecutivos, o mais novo só domina quando seu predecessor SHA casa o raw SHA do outro; qualquer outra
dupla válida é inconclusive. Se apenas um slot é CRC/canonical-valid — inclusive porque a tentativa de
escrever a phase seguinte deixou o outro torn — o survivor continua autoritativo sem dereferenciar seu
predecessor já sobrescrito, mas precisa recertificar integralmente seus manifests, carriers, artifacts,
phase predicates e control witness. Ele só pode ser retomado para a phase imediatamente seguinte.
Assim, ao escrever FENCED sobre PREPARING torn, PREPARED sobrevivente é self-contained; ao escrever
BOUND sobre PREPARED torn, FENCED sobrevivente também. Pred SHA incorreto nunca é aceito quando ambas
as preimages existem, e nenhum slot isolado autoriza pular generation/phase ou relaxar direct proofs.

`generation_capacity_plan_length` é nonzero para LEGACY_CUTOVER e V7_GENESIS, exact u32 sem trailing;
seu SHA cobre exatamente os bytes e o CRC externo do slot cobre length/SHA/body. Plan ausente, hash/
CRC divergente ou mudança entre phases recusa antes de qualquer resume.

Antes do primeiro marker de `LEGACY_CUTOVER`, uma passagem integral **read-only** sob
`OfflineMaintenanceSession` executa o DAG fechado abaixo. `cutover_operation_nonce[32]` é CSPRNG
nonzero congelado antes de I/O mutável; se ainda não existe marker, failure pode descartá-lo com zero
efeito. O resource envelope usa exact built-in u64 e os limites efetivos
`MAX_TOTAL_LENGTH=0xffffffff`, `MAX_SEGMENT_READ_BYTES=1073741824`,
`MAX_SEGMENT_NUMBER=999999999999`, todos os counts/paths u16/u32 e limites de page/catalog/manifest;
bool, subclasse, negativo ou overflow recusam sem I/O mutável.

```text
(database_uuid, cutover_operation_nonce, B, baseline commit/catalog/heap/index hashes,
 carrier-manifest base, ordered BindSubstepBaseV1 entries)
    -> baseline_cutover_identity + ordered bind_substep_base_sha
    -> bind_plan_source_digest
(all preceding bytes/digests, codec IDs, resource envelope, GenerationCapacityPlanV1 bytes/SHA,
 structural WAL templates with concrete bind_plan_source_digest and symbolic post-core fields)
    -> canonical CutoverFeasibilityCoreV1 bytes
    -> cutover_core_digest = SHA256(DOMAIN_CUTOVER_CORE_V1 || core bytes)
    -> outer_operation_identity = SHA256(DOMAIN_BIND_OUTER_V1 || baseline_cutover_identity || core digest)
    -> full_plan_sha = SHA256(DOMAIN_BIND_FULL_PLAN_V1 || outer identity || core digest ||
                              ordered(ordinal, kind, bind_substep_base_sha))
    -> NormalizedCutoverPrefixProjectionV1 -> logical_history_chain[0]
    -> BoundStableBindContextV1[0] -> NormalizedBindProjectionV1[0] -> logical_history_chain[1]
    -> ... -> BoundStableBindContextV1[N-1] -> NormalizedBindProjectionV1[N-1]
    -> logical_history_chain[N] -> NormalizedCutoverBoundCheckpointProjectionV1
    -> normalized_cutover_terminal_chain
    -> canonical CutoverFeasibilityManifestV1 envelope bytes
    -> CutoverFeasibilityDigest = SHA256(DOMAIN_CUTOVER_FEASIBILITY_V1 || envelope bytes)
    -> V7BootstrapStateV1(PREPARING)
```

`baseline_cutover_identity = SHA256(DOMAIN_CUTOVER_BASE_V1 || database_uuid || cutover_operation_nonce || B
|| baseline_commit/catalog/heap/index hashes || carrier_manifest_base_sha256)`. Cada
`BindSubstepBaseV1` contém somente kind/ordinal, source file identity/size/page count/full raw
inventory, nova `PersistedIndexIdentity` + immutable identity generation + path, target COW byte
length/full raw+semantic hash, catalog diff/manifest, codecs, record shapes/counts/lengths, LSN/
offset/roll placements e workspace/reservation bytes. Ele **não** contém outer identity, full-plan
SHA, bound-context SHA, projection SHA, final feasibility digest ou marker SHA.

`bind_plan_source_digest = SHA256(DOMAIN_BIND_PLAN_SOURCE_V1 || baseline_cutover_identity ||
ordered(ordinal,kind,bind_substep_base_sha))`. Ele nasce **antes** do core e é o único source key
permitido no `OperationSubstepTrailerV1` dos BINDs. Os structural templates no core codificam esse
valor concreto e tags de largura fixa para outer/full-plan/bound-context derivados pós-core; o codec
prova o layout/length/CRC recalculado, não tenta conhecer esses hashes prematuramente.

`full_plan_sha` cobre somente os SHA domain-separated dos payloads `BindSubstepBaseV1`, não os
contexts que o contêm. `NormalizedCutoverPrefixProjectionV1` cobre somente persisted fence/meta/
checkpoint-C structural state+manifest, catalog/header targets semânticos e WAL/history raw
normalizados desde B; exclui marker/PREPARED SHA, `LegacyFenceWriteProof`, final feasibility e runtime
proof/certificate. `NormalizedCutoverBoundCheckpointProjectionV1` cobre somente persisted checkpoint
state/manifest, catalog/header targets semânticos e WAL/history/CRC normalizados do sufixo BOUND;
exclui `CutoverFeasibilityDigest`, marker/raw bootstrap slot SHA, image/carrier final digest,
OperationBudget/attempt/proof nonces e `ColdOpenDeviceCertificate`. Ambos preservam
shape/order/semantic target e tag-normalizam epoch/txn/UUID/clock/raw history/CRC/fingerprint como a
projection BIND, recalculando derivados. A cadeia é:

```text
L[0] = SHA256(DOMAIN_BIND_LOGICAL_CHAIN_V1 || cutover_core_digest ||
              normalized_projection_sha256(prefix))
L[i+1] = SHA256(DOMAIN_BIND_LOGICAL_CHAIN_V1 || L[i] || i_u32 ||
                  normalized_projection_sha256(BIND[i]))
L_terminal = SHA256(DOMAIN_BIND_LOGICAL_CHAIN_V1 || L[N] ||
                    normalized_projection_sha256(BOUND checkpoint))
```

O encoding length-prefixed dessa sequência completa se chama `NormalizedCutoverHistoryChainV1`;
`LogicalBindPredecessorChainV1(i)` é exatamente `(i,L[i])`, sem outro root implícito.

No runtime, marker/envelope direct-valid é apenas autoridade **externa** para executar a projection
prefix/BIND/suffix que já estava congelada. Para o sufixo, checkpoint state/manifest é primeiro
publicado, barrier/direct-certified e só depois o novo marker phase/predecessor é publicado. Nenhum
proof/marker/certificate runtime volta como input da projection, portanto não há edge reversa.

`BoundStableBindContextV1[i]` é
`(baseline_cutover_identity, cutover_core_digest, outer_operation_identity, full_plan_sha, L[i],
BindSubstepBaseV1[i])`. Assim o predecessor lógico do BIND0 já inclui fence/checkpoint C e o sufixo
BOUND liga todos os BINDs; nenhum raw committed-history root entra no core/context estável/envelope.
O envelope final é length-prefixed, ordenado por persisted index identity,
sem trailing bytes, e contém core bytes/digest, todas as derived identities/contexts/projections, os
exact fence/checkpoint-C/BIND/checkpoint-BOUND structural plans, toda a cadeia L incluindo terminal,
maior segmento e somas checked de memória/temp/target/WAL/directory space.

Nenhum `CutoverFeasibilityDigest`, envelope digest, bound-context/projection SHA ou raw marker SHA
aparece em seu próprio input ou em qualquer predecessor do DAG. Em particular, final feasibility/
marker SHA é proibido em `BindSubstepBaseV1`, structural template, `OperationSubstepTrailerV1` BIND e
`NormalizedBindProjectionV1`. Decoder/recompute parte apenas dos inputs à esquerda, recalcula cada
seta na ordem e recusa placeholder nonzero, edge divergente, self-hash ou backward reference antes do
primeiro marker.

Separadamente, `AttemptFencingContextV1` contém apenas valores que podem avançar/revogar sem mudar o
target lógico: process birth + database runtime generation, budget/attempt SHAs, current index
namespace/**mutation** generation, per-index mutation generation, WAL coordination generation,
`wal_predecessor_mutation_generation=g` e
`wal_planned_successor_mutation_generation=checked_increment_u64(g)`, device replay generation,
reader/floor generation se presente, writer lease epoch, WAL transaction-record `(epoch, txn_id)`,
SEGMENT_HEADER epoch/clock, ticket/lease/guard/COMMIT nonces, `clock_stamp`, transaction UUID e
**somente** capabilities já existentes antes de congelar o draft (`_IndexMutationPermit` e
namespace/coordinator permit quando aplicável). `_WalMutationPermit` não pertence ao context: ele só
pode nascer depois de existir o fingerprint/range exato do draft ao qual será vinculado.
A immutable `carrier_identity_generation`, database UUID, WAL feature-fence generation, persisted
index identity/generation e qualquer campo de `BoundStableBindContextV1` ficam fora dessa lista.

O tuple raw de `AttemptFencingContextV1` não contém seu próprio SHA, `operation_context_sha256`,
`proof_nonce_sha256`, record CRC, plan fingerprint/blob nem `_IrrevocableWalPermit`. A ordem
pre-append/pós-barrier é acíclica e única:

```text
BoundStableBindContextV1 + logical substep core -> logical_stable_operation_context_sha256
WalEntropySeed detached + current fences -> WalPlanSeedFinal
ActualSubstepSourceProofV1 raw -> actual_substep_source_proof_sha256
AttemptFencingContextV1 raw(g, checked(g+1), sem WAL permit) -> attempt_fencing_context_sha256
(logical stable SHA, actual source SHA, fencing SHA) -> operation_context_sha256
(COMMIT nonce, budget/attempt, operation context) -> proof_nonce_sha256
WalPlanSeedFinal + proofs -> encode records/CRCs -> WalAppendDraft(blob, ranges, fp, g, g+1)
WalAppendDraft + exact source/pin/boundary/barrier state -> WalMutationDraftV1(APPEND)
revalidate mutation draft/tail/current==g -> CAS/publicar WAL generation g->g+1
    -> _WalMutationPermit(mutation-draft fp/kind/ranges, predecessor=g, successor=g+1)
append exact mutation+append drafts exige permit vivo + current==g+1
    -> WAL durability barrier + direct terminal proof
    -> _IrrevocableWalPermit(raw plan fingerprint, terminal, target)
    -> target/workspace/device apply
```

`_IrrevocableWalPermit` nasce somente na seta pós-barrier e jamais é input de context, proof, seed ou
plan. `_WalMutationPermit` nasce na seta intermediária, depois de o `WalMutationDraftV1` estar frozen;
seu nonce não
entra no seed, proof, encoding, CRC ou fingerprint. Antecipar qualquer um dos dois permits, derivá-los
de template/feasibility ou criar permit↔fingerprint/CRC self-reference é type/refusal. Se o CAS
`g→g+1` vencer e o processo falhar antes do byte zero, o incremento extra é seguro e o retry precisa
replanejar de predecessor `g+1` para sucessor `g+2`. Dois drafts sobre o mesmo `g`: só um CAS vence;
o outro recusa stale com zero byte.

`NormalizedBindProjectionV1` não é um blob WAL antecipado. Ela preserva version/type/flags/order,
descriptor, record/payload lengths, `bind_plan_source_digest`, core lógico do
`OperationSubstepTrailerV1` BIND/`BoundOperationSubstepContextV1`, `BoundStableBindContextV1` e L[i].
Ela substitui cada primitive de `ActualSubstepSourceProofV1` (inclusive raw history/commit.state/
effect/apply hashes) e cada campo fechado de
`AttemptFencingContextV1` por tag simbólica de mesma largura. Pre-marker, o template encoder emite
essas tags diretamente e prova as relações de grouping; no attempt real, a mesma projeção só roda
**depois** de o validator raw provar que somente os records transacionais `BEGIN_V2..COMMIT_V2` usam
o mesmo `(epoch,txn_id)`, BEGIN/COMMIT usam o mesmo UUID e o par é current/distinto conforme lease/
history. `SEGMENT_HEADER`, quando roll o exige, antecede BEGIN, fica fora do grouped transaction, tem
grammar própria com `txn_id=0` exato e normaliza separadamente epoch/clock, recomputando depois seu
CRC; roll decision,
record type/flags/order/length/LSN continuam estáveis. Uma função única mapeia valores iguais à mesma
tag; CRC/fingerprint normalizado é sempre recalculado depois das substituições, nunca usado como
input. Mismatch não é apagado e recusa antes dela. Nenhum outro campo pode ser
normalizado. Depois das tags, recalcula Actual proof SHA, attempt-fencing SHA, operation-context SHA,
proof nonce, bound-context SHA, CommitPayload encoding, record CRCs, plan fingerprint e chain
projection SHA; nenhum output derivado é tag independente.
Feasibility prova shape/capacity/placement e a cadeia lógica inteira, mas não promete raw history root,
blob/fencing de attempt. Cada BIND actual exige que sua raw history valide e projete exatamente para
L[i]; o checkpoint BOUND exige que a história raw completa projete para L_terminal.

#### 3.1.1.1 Codecs integrais do transcript de feasibility

Os tipos abaixo eram inputs persistidos ou hash-eados; portanto “encoding canônico” não é liberdade de
implementação. Todos usam `ScalarCodecV1`, CRC32C sobre todos os bytes anteriores do próprio record,
EOF exato e `record_length` incluindo CRC. `MAX_CUTOVER_TRANSCRIPT_BYTES_V1=67108864`,
`MAX_CUTOVER_ENTRY_COUNT_V1=1048576`, `MAX_PATH_BYTES_V1=65535`; cada nested length é u32, checked,
e o nested record precisa decodificar integralmente no codec/magic indicado. SHA próprio e CRC externo
nunca entram em sua própria preimage. Magic/version/kind desconhecido, list sem count+length, entry
length divergente, reorder/duplicate, reserved/trailing ou canonical re-encode divergente recusam antes
de PREPARING.

As tabelas auxiliares u8 são fechadas:

- `ManifestObjectKindV1`: `INVALID=0`, `REGULAR_FILE=1`, `DIRECTORY=2`, `DECLARED_ABSENT=3`,
  `SPACE_RESERVATION=4`; 5..255 recusam;
- `BootstrapArtifactKindV1`: `INVALID=0`, `GRAFX_META=1`, `HEAP=2`, `CATALOG=3`,
  `COMMIT_STATE=4`, `CHECKPOINT_INDEX=5`, `WAL_DIRECTORY=6`, `INDEX_DATA=7`,
  `CONTROL_DIRECTORY=8`; 9..255 recusam;
- `CarrierArtifactKindV1`: `INVALID=0`, `WAL_COORDINATION=1`, `INDEX_NAMESPACE=2`,
  `INDEX_VIEW=3`; 4..255 recusam;
- `NamespaceAliasKindV1`: `INVALID=0`, `PRIMARY_COMPONENT=1`, `HARDLINK_ALIAS=2`; 3..255 recusam;
- `SpaceResourceKindV1`: `INVALID=0`, `MEMORY=1`, `TEMP_FILE=2`, `TARGET_FILE=3`, `WAL_BYTES=4`,
  `DIRECTORY_ENTRY=5`, `DURABLE_RESERVATION=6`; 7..255 recusam;
- `ManifestIdentityStateV1`: `INVALID=0`, `CURRENT_EXACT=1`, `PLANNED_POSTIMAGE=2`,
  `DECLARED_ABSENT=3`; 4..255 recusam;
- `SpaceReservationRequirementV1`: `INVALID=0`, `PROBE_ONLY=1`,
  `REQUIRE_DURABLE_RESERVATION=2`; 3..255 recusam.
- `CarrierBackendKindV1`: `INVALID=0`, `REGULAR_FILE_FIXED_SLOTS=1`,
  `BACKEND_EQUIVALENT_ATOMIC_SLOTS=2`; 3..255 recusam. Kind 2 ainda exige identity/byte image
  canônica direct-readable ligada ao root binding; não autoriza blob opaco.

```text
ArtifactImageVectorV1 = (
  magic=MAGIC_ARTIFACT_IMAGE_VECTOR_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], root_binding_sha256[32], entry_count u32, entries_length u32,
  entries[entry_count], crc32c u32
)
artifact_entry := (
  entry_length u32, ordinal u32, artifact_kind u8, object_kind u8,
  identity_state u8, reserved_zero u8,
  path_length u16, reserved_zero u16, file_identity[32], byte_length u64,
  raw_sha256[32], semantic_sha256[32], parent_identity[32], path_bytes[path_length]
)

GenesisImageManifestV1 = (
  magic=MAGIC_GENESIS_IMAGE_MANIFEST_V1, version u16=1, bootstrap_kind u8, reserved_zero u8,
  record_length u32, database_uuid[16], root_binding_sha256[32],
  namespace_binding_length u32,
  artifact_count u32, alias_count u32, artifacts_length u32, aliases_length u32,
  namespace_binding_bytes[namespace_binding_length],
  artifacts[artifact_count], aliases[alias_count], crc32c u32
)
namespace_alias_entry := (
  entry_length u32, ordinal u32, object_kind u8, alias_kind u8,
  identity_state u8, reserved_zero u8,
  component_length u16, reserved_zero u16, observed_link_count u32,
  file_identity[32], parent_identity[32], component_bytes[component_length]
)

CarrierManifestV1 = (
  magic=MAGIC_CARRIER_MANIFEST_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], root_binding_sha256[32], carrier_count u32, entries_length u32,
  carriers[carrier_count], crc32c u32
)
carrier_entry := (
  entry_length u32, ordinal u32, carrier_kind u8, backend_kind u8,
  slot_count u8, identity_state u8, path_length u16, reserved_zero u16,
  file_identity[32], raw_length u64, raw_sha256[32], initial_state_sha256[32],
  initial_publication_generation u64, path_bytes[path_length]
)

CutoverSpacePlanV1 = (
  magic=MAGIC_CUTOVER_SPACE_PLAN_V1, version u16=1, bootstrap_kind u8, reserved_zero u8,
  record_length u32, database_uuid[16], cutover_operation_nonce[32],
  resource_count u32, entries_length u32, peak_memory_bytes u64,
  total_temp_bytes u64, total_target_bytes u64, total_wal_bytes u64,
  total_directory_margin_bytes u64, resources[resource_count], crc32c u32
)
space_resource_entry := (
  entry_length u32, ordinal u32, resource_kind u8, required_reservation_mode u8,
  reserved_zero u16, path_length u16, reserved_zero u16,
  minimum_available_bytes u64, workspace_bytes u64, memory_bytes u64,
  direct_available_bytes u64, source_scope_identity[32], content_sha256[32],
  reservation_plan_identity[32], path_bytes[path_length]
)

SpaceReservationStageProofV1 = (
  magic=MAGIC_SPACE_STAGE_PROOF_V1, version u16=1, bootstrap_phase u8, reserved_zero u8,
  record_length u32, database_uuid[16], cutover_space_plan_sha256[32],
  observed_entry_count u32, entries_length u32, observed_entries[observed_entry_count], crc32c u32
)
observed_space_reservation := (
  entry_length u32=148, ordinal u32, observed_state u8, reserved_zero[3],
  reservation_plan_identity[32], observed_file_identity[32],
  observed_available_bytes u64, observed_raw_sha256[32], barrier_proof_sha256[32]
)

ManifestIdentityStageProofV1 = (
  magic=MAGIC_MANIFEST_IDENTITY_STAGE_V1, version u16=1, manifest_kind u8, bootstrap_phase u8,
  record_length u32, database_uuid[16], source_manifest_sha256[32],
  binding_count u32, entries_length u32, bindings[binding_count], crc32c u32
)
manifest_identity_binding := (
  entry_length u32=148, source_ordinal u32, observed_state u8, reserved_zero[3],
  planned_postimage_sha256[32], observed_file_identity[32], observed_raw_length u64,
  observed_raw_sha256[32], direct_certificate_sha256[32]
)
```

`artifact_entry` tem `entry_length==152+path_length`; alias tem `84+component_length`; carrier tem
`128+path_length`; resource tem `144+path_length`. Vectors/manifests são sorted/unique por ordinal e
depois por `(artifact/carrier/resource kind,path bytes)`; ordinals são densos desde zero. A matriz
zero/nonzero é total. `CURRENT_EXACT` exige file/directory identity nonzero; REGULAR_FILE exige raw/
semantic SHA nonzero e length qualquer, DIRECTORY exige length zero/raw ZERO32/semantic nonzero.
`PLANNED_POSTIMAGE` exige file identity ZERO32, mas path/parent/target length/raw+semantic de FILE ou
semantic de DIRECTORY já exatos; após create, stage/direct proof liga a identity real sem alterar o
manifest. `DECLARED_ABSENT` só combina com object kind DECLARED_ABSENT e exige identity/length/raw/
semantic zeros. SPACE_RESERVATION usa PLANNED_POSTIMAGE, identity ZERO, `byte_length>0` e target hashes
nonzero. Alias CURRENT exige identity nonzero/link count exact; PLANNED aceita somente
PRIMARY_COMPONENT com identity/link-count zero e liga a identity pelo mesmo proof do target; planned
hardlink é proibido. Qualquer outra combinação recusa. Symlink/junction/reparse nunca é object kind; hardlink aparece integralmente no alias vector e precisa satisfazer a policy no-alias. Carrier
slot count/length/state precisam casar os codecs fixed-size deste contrato. Carrier CURRENT exige file
identity/raw SHA nonzero; PLANNED exige file identity ZERO e raw target/state SHA nonzero. Totais são a soma checked das
entries de cada classe, e `peak_memory_bytes` é o máximo, não soma. Nenhum manifest aceita apenas root
sem suas entries; hashes usam, respectivamente, `DOMAIN_ARTIFACT_IMAGE_VECTOR_V1`,
`DOMAIN_GENESIS_IMAGE_MANIFEST_V1`, `DOMAIN_CARRIER_MANIFEST_V1` e
`DOMAIN_CUTOVER_SPACE_PLAN_V1` sobre o record canônico respectivo.

`namespace_binding_length` é nonzero, cabe em 16 KiB e decodifica exatamente um
`StableNamespaceBindingProjectionV1` da seção 5.2.1; seu digest é bit-identical a
`root_binding_sha256`. O mesmo root binding aparece no vector/image/carrier. O alias histórico
`GenesisNamespaceBindingV1` designa exatamente esses bytes/tag/version e não possui encoder, domain
ou forma alternativa.

No `CutoverSpacePlanV1`, `required_reservation_mode`, sizes, path, source scope e
`reservation_plan_identity=SHA256(DOMAIN_SPACE_RESERVATION_ID_V1||database_uuid||cutover_operation_nonce||ordinal||kind||
path||minimum/workspace bytes)` são projeção estável pre-marker; nenhum observed file identity ou
barrier certificate entra no plan. PROBE_ONLY não cria artefato. REQUIRE_DURABLE é materializado entre
PREPARING/PREPARED e provado por `SpaceReservationStageProofV1`, cuja entry state é
`ABSENT=1` ou `DIRECT_CERTIFIED=2` (zero e 3..255 recusam); DIRECT exige actual file identity/raw/
barrier nonzero e available>=minimum. Phase PREPARING pode provar ABSENT; PREPARED/FENCED/BOUND exigem
DIRECT para todas as required entries, revalidado sem mudar o space-plan SHA. Count/ordinal casam
bijetivamente ao required subset. Assim feasibility não prediz inode futuro nem altera bytes entre
phases.

`ManifestIdentityStageProofV1.manifest_kind` é `INVALID=0`, `IMAGE=1`, `CARRIER=2`; outros recusam;
observed state reutiliza ABSENT=1/DIRECT_CERTIFIED=2. Ele contém exatamente uma binding por entry
PLANNED_POSTIMAGE do source manifest, sorted pelo ordinal. ABSENT zera observed identity/length/raw/
certificate; DIRECT exige identity/raw/certificate nonzero, length/raw/postimage bit-identical ao
manifest e no-alias/link-count proof. CURRENT_EXACT/DECLARED_ABSENT não entram no vector e são
recertificados diretamente. Assim inode/file-id futuro nunca entra no transcript estável.

O template estrutural, que nunca contém UUID/epoch/LSN raw futuro, também tem codec fechado:

```text
WalStructuralTemplateV1 = (
  magic=MAGIC_WAL_STRUCTURAL_TEMPLATE_V1, version u16=1, workflow_kind u8, reserved_zero u8,
  record_length u32, database_uuid[16], bind_plan_source_digest[32],
  substep_kind u8, reserved_zero[3], plan_ordinal u32,
  record_shape_count u32, field_descriptor_count u32,
  record_shapes[record_shape_count], field_descriptors[field_descriptor_count], crc32c u32
)
record_shape := (
  record_order u32, wal_format_version u8, record_type u8, flags u16,
  record_length u32, payload_length u32, segment_delta u32, reserved_zero u32
) # 24 bytes
field_descriptor := (
  record_order u32, field_ordinal u16, semantic_kind u8, stability_kind u8,
  field_width u16, reserved_zero u16, constant_value_sha256[32]
) # 44 bytes

FenceWalStructuralTemplateV1 = (
  magic=MAGIC_FENCE_WAL_TEMPLATE_V1, version u16=1, fence_template_kind u8=1, reserved_zero u8,
  record_length u32, database_uuid[16], logical_ordinal u32=MAX_U32-2,
  legacy_baseline_lsn u64, planned_cutover_lsn u64, wal_format_version u8=1,
  reserved_zero[3], record_shape_count u32, field_descriptor_count u32,
  legacy_fence_target_semantic_sha256[32], record_shapes[record_shape_count],
  field_descriptors[field_descriptor_count], crc32c u32
)
```

`TemplateFieldStabilityV1` é `INVALID=0`, `STABLE_BYTES=1`, `SYMBOLIC_VOLATILE=2`,
`DERIVED_RECOMPUTED=3`; 4..255 recusam. `TemplateFieldSemanticKindV1` é a tabela total:
`INVALID=0`, `EPOCH=1`, `TXN_ID=2`, `TRANSACTION_UUID=3`, `CLOCK=4`, `LSN=5`, `RAW_HISTORY=6`,
`ACTUAL_SOURCE_SHA=7`, `FENCING_SHA=8`, `OPERATION_CONTEXT_SHA=9`, `CRC32C=10`,
`PLAN_FINGERPRINT=11`, `STABLE_PAYLOAD=12`; 13..255 recusam. Shapes são sorted por record order,
descriptors por `(record_order,field_ordinal)`, unique e cada descriptor cabe no record shape.
STABLE_BYTES exige constant SHA nonzero; SYMBOLIC/DERIVED exige ZERO32. CRC/fingerprint somente
DERIVED; stable payload somente STABLE. SEGMENT_HEADER é shape separado, txn_id zero, e não compartilha
grouped equality.

`FenceWalStructuralTemplateV1` reutiliza apenas os layouts fixed de shape/descriptor, não os campos
BIND. `fence_template_kind` aceita somente 1 `LEGACY_V1_FENCE_TRANSACTION`; workflow/substep/source
digest não existem nesse codec. C é o terminal planejado da única transaction fence e é maior que B;
shapes usam WAL v1/flags opacos, exatamente um terminal COMMIT e o target semantic meta V2. Qualquer
BIND kind/source/ordinal interno, C/B inválido ou template v2 recusa. Golden N=0 ainda contém este
único fence template.

```text
BindSubstepBaseV1 = (
  magic=MAGIC_BIND_SUBSTEP_BASE_V1, version u16=1, substep_kind u8=BIND_IDENTITY,
  reserved_zero u8, record_length u32, database_uuid[16], plan_ordinal u32,
  source_codec_id u16, target_codec_id u16, wal_codec_id u16, catalog_codec_id u16,
  target_path_length u16, reserved_zero u16,
  source_file_identity[32], source_byte_length u64, source_page_count u64,
  source_raw_sha256[32], source_inventory_sha256[32],
  persisted_index_identity[32], immutable_identity_generation u64,
  target_byte_length u64, target_raw_sha256[32], target_semantic_sha256[32],
  catalog_diff_sha256[32], wal_record_count u32, wal_payload_length u32,
  wal_batch_length u32, reserved_zero u32, first_segment_number u64,
  first_segment_offset u64, workspace_bytes u64, memory_peak_bytes u64, temp_bytes u64,
  source_inventory_length u32, target_inventory_length u32,
  catalog_manifest_length u32, structural_template_length u32,
  target_path_bytes[target_path_length],
  source_inventory_bytes[source_inventory_length],
  target_inventory_bytes[target_inventory_length],
  catalog_manifest_bytes[catalog_manifest_length],
  structural_template_bytes[structural_template_length], crc32c u32
)
bind_substep_base_sha256 = SHA256(DOMAIN_BIND_SUBSTEP_BASE_RECORD_V1 || canonical base bytes)
```

Source/target inventories são `ArtifactImageVectorV1`; catalog manifest é outro vector com somente
CATALOG/INDEX_DATA semantic targets; structural template é `WalStructuralTemplateV1`. Todos os quatro
lengths são nonzero e seus embedded DB UUID/root/ordinal/identity precisam casar. BIND bases são
sorted/unique por `(persisted_index_identity,plan_ordinal)` e ordinal é a posição estável do plano
integral, nunca renumerada por retry/resume. Codecs IDs u16 usam registry V1 fechado
`LEGACY_INDEX=1`, `INDEX_V2=2`, `WAL_V2=3`, `CATALOG_V1=4`; zero/5..65535 recusam e cada slot aceita
somente o ID de sua coluna. Nenhum campo derivado pós-core existe nesse record.

```text
WalStructuralTemplateVectorV1 = (
  magic=MAGIC_WAL_STRUCTURAL_VECTOR_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], template_count u32, templates_length u32,
  templates[template_count], crc32c u32
)
```

O vector contém exatamente um `WalStructuralTemplateV1` por BIND, em plan ordinal, cada record
delimitado pelo próprio `record_length`; `templates_length` é a soma e count é 0..65536. Count zero
exige length zero; duplicate, gap, source digest/base mismatch ou template extra recusa.

```text
CutoverFeasibilityCoreV1 = (
  magic=MAGIC_CUTOVER_CORE_V1, version u16=1, bootstrap_kind u8=LEGACY_CUTOVER,
  reserved_zero u8, record_length u32, database_uuid[16], cutover_operation_nonce[32], baseline_lsn u64,
  baseline_commit_state_sha256[32], baseline_catalog_sha256[32],
  baseline_heap_inventory_sha256[32], baseline_index_inventory_sha256[32],
  carrier_manifest_base_sha256[32], baseline_cutover_identity[32],
  bind_plan_source_digest[32], resource_codec_registry_version u16=1,
  reserved_zero u16, bind_count u32, bind_entries_length u32,
  capacity_plan_length u32, space_plan_length u32, structural_templates_length u32,
  bind_bases[bind_count], generation_capacity_plan_bytes[capacity_plan_length],
  cutover_space_plan_bytes[space_plan_length],
  structural_template_vector_bytes[structural_templates_length], crc32c u32
)
cutover_core_digest = SHA256(DOMAIN_CUTOVER_CORE_V1 || canonical core bytes)
```

Cada bind base é delimitado pelo próprio `record_length`; `bind_entries_length` é a soma exata e
bind_count é 0..65536; zero exige `bind_entries_length=0`. Templates decodificam como um
`WalStructuralTemplateVectorV1`; count precisa
igualar bind_count e ordinal/base/source digest casar.
Capacity/space são os records integrais já definidos, não seus hashes isolados. Baseline/core fields
recomputam `baseline_cutover_identity` e `bind_plan_source_digest`; qualquer backward/final digest,
outer identity, full-plan, marker SHA ou placeholder fora dos exact template tags recusa.

```text
BoundStableBindContextV1 = (
  magic=MAGIC_BOUND_BIND_CONTEXT_V1, version u16=1, workflow_kind u8=LEGACY_BIND, reserved_zero u8,
  record_length u32, database_uuid[16], plan_ordinal u32, substep_count u32,
  baseline_cutover_identity[32], cutover_core_digest[32],
  outer_operation_identity[32], full_plan_sha256[32],
  logical_predecessor_chain_sha256[32], bind_substep_base_sha256[32],
  bind_substep_base_length u32, bind_substep_base_bytes[bind_substep_base_length], crc32c u32
)
bound_stable_bind_context_sha256 = SHA256(DOMAIN_BOUND_STABLE_BIND_V1 || canonical context bytes)
```

Embedded base decodifica como `BindSubstepBaseV1` e todos os identity/ordinal/count/SHA fields casam;
count é o bind count do core e ordinal < count. O context não contém actual source, fencing, attempt,
record CRC/fingerprint, final feasibility ou marker. Não existe versão hash-only: recovery obtém a
preimage aqui e a compara byte a byte ao core.

As três projections usam um único codec físico com kind distinto:

```text
NormalizedOperationProjectionV1 = (
  magic=MAGIC_NORMALIZED_PROJECTION_V1, version u16=1, projection_kind u8, workflow_kind u8,
  record_length u32, database_uuid[16], plan_ordinal u32, record_count u32,
  logical_stable_context_sha256[32], logical_predecessor_chain_sha256[32],
  bind_plan_source_digest[32], structural_target_sha256[32], records_length u32,
  records[record_count], crc32c u32
)
normalized_record_entry := (
  entry_length u32, record_order u32, wal_format_version u8, record_type u8, flags u16,
  segment_delta u32, original_record_length u32, original_payload_length u32,
  field_count u32, fields_length u32, normalized_record_crc32c u32,
  fields[field_count]
)
normalized_field_entry := (
  entry_length u32, field_ordinal u16, semantic_kind u8, value_kind u8,
  value_length u32, value_bytes[value_length]
)
normalized_projection_sha256 = SHA256(DOMAIN_NORMALIZED_PROJECTION_V1 || canonical projection bytes)
NormalizedCutoverPrefixProjectionV1 = NormalizedOperationProjectionV1(projection_kind=1)
NormalizedBindProjectionV1 = NormalizedOperationProjectionV1(projection_kind=2)
NormalizedCutoverBoundCheckpointProjectionV1 = NormalizedOperationProjectionV1(projection_kind=3)
```

`NormalizedProjectionKindV1` é `INVALID=0`, `CUTOVER_PREFIX=1`, `BIND=2`,
`BOUND_CHECKPOINT=3`; 4..255 recusam. Os nomes
`NormalizedCutoverPrefixProjectionV1`, `NormalizedBindProjectionV1` e
`NormalizedCutoverBoundCheckpointProjectionV1` são aliases estritos desses três tags, não codecs
alternativos. Prefix/BOUND usam ordinal `MAX_U32`; BIND usa ordinal < substep count. Prefix tem
workflow LEGACY_BIND, predecessor ZERO32 e stable context igual ao core digest; BIND exige os SHAs do
`BoundStableBindContextV1`/L[i]; BOUND exige stable context full-plan e predecessor L[N]. Source digest
é nonzero igual em todos.

`NormalizedProjectionValueKindV1` é `INVALID=0`, `STABLE_BYTES=1`, `SYMBOLIC_TAG=2`,
`DERIVED_RECOMPUTED=3`; 4..255 recusam. `SymbolicFieldTagV1` u16 é `INVALID=0`,
`TXN_EPOCH=1`, `TXN_ID=2`, `TRANSACTION_UUID=3`, `CLOCK=4`, `LSN=5`,
`ACTUAL_HISTORY_ROOT=6`, `ACTUAL_SOURCE_PROOF=7`, `FENCING_CONTEXT=8`,
`OPERATION_CONTEXT=9`, `PROOF_NONCE=10`, `SEGMENT_EPOCH=11`; 12..65535 recusam. SYMBOLIC exige
`value_length` igual à largura original e `value_bytes = tag_u16 || ZERO[value_length-2]`;
STABLE contém os bytes originais; DERIVED contém bytes recalculados sobre a projection já taggeada e é
legal somente para semantic kinds CRC32C/PLAN_FINGERPRINT/derived hash da tabela anterior. Field e
record orders são densos/sorted/unique; record lengths/flags/types/segment placement precisam casar o
`WalStructuralTemplateV1`. Mesmo primitive raw recebe o mesmo tag em todos os records; mismatch raw é
recusado antes da projection e nunca apagado. `normalized_record_crc32c` cobre a entry até antes desse
campo com o field vector canônico, não é cópia do CRC WAL raw.

```text
NormalizedCutoverHistoryChainV1 = (
  magic=MAGIC_NORMALIZED_HISTORY_CHAIN_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], cutover_core_digest[32], bind_count u32, chain_entry_count u32,
  entries[chain_entry_count], terminal_chain_sha256[32], crc32c u32
)
chain_entry := (
  logical_ordinal u32, chain_kind u8, reserved_zero[3],
  predecessor_chain_sha256[32], projection_sha256[32], successor_chain_sha256[32]
) # 104 bytes
```

`LogicalChainKindV1` é `INVALID=0`, `PREFIX=1`, `BIND=2`, `BOUND=3`; 4..255 recusam.
`chain_entry_count == bind_count+2`: PREFIX ordinal MAX_U32 e predecessor core digest; depois exatamente
N BIND ordinals 0..N-1; por fim BOUND ordinal MAX_U32. Cada successor é recomputado pela fórmula L
normativa e vira predecessor bit-identical da próxima; `terminal_chain_sha256` é o último successor.
`projection_sha256` é sempre o digest registrado `DOMAIN_NORMALIZED_PROJECTION_V1` do record integral
correspondente, nunca SHA-256 nu de seus bytes. Nenhum raw history root aparece neste record.

O vector de planos estruturais fecha os nested codecs sem body opaco:

```text
CutoverStructuralPlanVectorV1 = (
  magic=MAGIC_CUTOVER_STRUCTURAL_VECTOR_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], entry_count u32, entries_length u32, entries[entry_count], crc32c u32
)
structural_plan_entry := (
  entry_length u32, plan_kind u8, nested_codec_kind u8, reserved_zero u16,
  logical_ordinal u32, nested_length u32, nested_sha256[32], nested_bytes[nested_length]
)
```

`CutoverStructuralPlanKindV1` é `INVALID=0`, `FENCE=1`, `CHECKPOINT_C=2`, `BIND=3`,
`CHECKPOINT_BOUND=4`; 5..255 recusam. `CutoverNestedCodecKindV1` é `INVALID=0`,
`BIND_WAL_TEMPLATE=1`, `ARTIFACT_VECTOR=2`, `CONTROL_PUBLICATION_CORE=3`,
`CHECKPOINT_COMPOSITE_CORE=4`, `FENCE_WAL_TEMPLATE=5`; 6..255 recusam. A coverage/multiplicidade é exata:

| Plan kind / ordinal | Entries obrigatórias, exatamente uma de cada |
|---|---|
| FENCE / `MAX_U32-2` | FENCE_WAL_TEMPLATE, CONTROL_PUBLICATION_CORE |
| CHECKPOINT_C / `MAX_U32-1` | ARTIFACT_VECTOR, CONTROL_PUBLICATION_CORE, CHECKPOINT_COMPOSITE_CORE |
| BIND / cada `i in [0,N)` | BIND_WAL_TEMPLATE |
| CHECKPOINT_BOUND / `MAX_U32` | ARTIFACT_VECTOR, CONTROL_PUBLICATION_CORE, CHECKPOINT_COMPOSITE_CORE |

Logo `entry_count==N+8`. Entries são sorted/unique por `(plan_kind,logical_ordinal,nested_codec_kind)`;
missing/extra/duplicate, outro ordinal ou outra tuple recusa. `nested_length`/SHA/preimage decodificam
integralmente no magic mapeado: kind 1 é somente `WalStructuralTemplateV1`, kind 2 somente
`ArtifactImageVectorV1`, kind 3 somente `ControlPublicationCoreV1`, kind 4 somente
`CheckpointCompositePlanCoreV1` e kind 5 somente `FenceWalStructuralTemplateV1`. Hash isolado, codec
cruzado, template BIND no FENCE, template FENCE no BIND ou body futuro desconhecido recusa.

```text
CutoverFeasibilityManifestV1 = (
  magic=MAGIC_CUTOVER_MANIFEST_V1, version u16=1, bootstrap_kind u8=LEGACY_CUTOVER,
  reserved_zero u8, record_length u32, database_uuid[16], baseline_cutover_identity[32],
  bind_plan_source_digest[32], cutover_core_digest[32],
  outer_operation_identity[32], full_plan_sha256[32], bind_count u32,
  core_length u32, bound_contexts_length u32, projections_length u32,
  history_chain_length u32, structural_plans_length u32,
  image_manifest_length u32, carrier_manifest_length u32, space_plan_length u32,
  peak_memory_bytes u64, total_temp_bytes u64, total_target_bytes u64,
  total_wal_bytes u64, total_directory_margin_bytes u64,
  core_bytes[core_length], bound_contexts[bound_contexts_length],
  projections[projections_length], history_chain_bytes[history_chain_length],
  structural_plans_bytes[structural_plans_length],
  image_manifest_bytes[image_manifest_length],
  carrier_manifest_bytes[carrier_manifest_length], space_plan_bytes[space_plan_length], crc32c u32
)
cutover_feasibility_digest = SHA256(DOMAIN_CUTOVER_FEASIBILITY_V1 || canonical manifest bytes)
```

`bound_contexts` é concatenação de exatamente bind_count `BoundStableBindContextV1` em ordinal e
length zero quando count zero;
`projections` é PREFIX, N BINDs e BOUND, cada um delimitado por `record_length`. History/structural/
image/carrier/space usam exatamente os codecs acima. Core bytes/digest, derived outer/full-plan/source,
counts, nested DB UUIDs e somas de resource precisam recomputar; o space plan é bit-identical ao
embedded no core. `full_plan_sha256` usa apenas outer/core/ordered base SHAs como no DAG. Nenhum final
feasibility digest/marker SHA aparece em nested input. Golden vectors N=0/1/N=max-bound, segment roll,
resume após BIND k e tag/CRC recomputation são congelados; mutantes de nested length/count/order,
self-hash, body-swap e projection tag estável/volátil recusam byte a byte.

O encoder de feasibility percorre todos os bytes source e executa os mesmos encoders target/WAL em
modo streaming count+hash/normalized projection, sem criar target/temp, alocar page, dirty frame ou escrever database. Cada
full BIND precisa caber simultaneamente em seu record/batch/segment/read-back/count/path codec e no
resource envelope; o pico materializado do `WalAppendPlan` precisa caber no memory limit. Excesso,
OOM ao provar a reserva de memória, incapacidade do backend de relatar/reservar workspace ou falha de
encoding recusa `enable_wal_semantics_v2` **antes do primeiro marker**, deixando zero efeito durável.
Não é permitido “validar depois do fence”, truncar o target ou dividir implicitamente um BIND.

Feasibility/genesis também produzem, antes do primeiro marker, um `GenerationCapacityPlanV1`
canônico, nonempty nos dois bootstrap kinds e persistido integralmente em todo slot:

```text
GenerationCapacityPlanV1 = (
  magic=MAGIC_GENERATION_CAPACITY_PLAN_V1, version u16=1, bootstrap_kind u8, reserved_zero u8,
  record_length u32, database_uuid[16], integrity_budget_max_attempts_ceiling u32,
  entry_count u32, total_step_count u32, registry_version u16=1, reserved_zero u16,
  entries[entry_count], crc32c u32
)
capacity_entry := (
  entry_length u32, domain_kind u8, domain_lifetime u8, reserved_zero u16,
  domain_identity[32], initial_observed_current u64,
  initial_terminal_cleanup_liability u64, post_bound_operational_reserve u64,
  step_count u32, reserved_zero u32, steps[step_count]
)
capacity_step := (
  phase u8, action_kind u8, reserved_zero u16, logical_ordinal u32,
  mandatory_success_increments u32, maximum_increments_per_failed_attempt u32
)
generation_capacity_plan_sha256 = SHA256(DOMAIN_GENERATION_CAPACITY_PLAN_V1 || canonical plan bytes)
```

Cada step tem exatamente 16 bytes; entry length é `72 + 16*step_count`, record length inclui CRC e
EOF precisa coincidir. Bounds built-in: 1..65536 entries, 1..1048576 total steps, 1..65536 steps por
entry e record até 67,108,864 bytes. A soma dos `step_count` iguala `total_step_count`; lists são
parseadas somente pelos counts/lengths, nunca por magic scanning. Bootstrap kind/database UUID são os
enums/16 bytes exatos, entry/step ordering e uniqueness seguem abaixo. Truncation, entry length +/−1,
count overflow, reserved nonzero, registry version desconhecida, CRC/trailing e canonical re-encode
divergente recusam antes de PREPARING. Domain 15 é runtime-only e **não pode** aparecer neste plan
persistido; seu headroom/liability pertence ao coordinator incarnation descrito abaixo. Domain 9 cobre
somente actions 14/24 duráveis.

`GenerationDomainLifetimeV1` é `INVALID=0`, `PHASE_ONLY=1`, `OPERATIONAL=2`; outro byte recusa. A
tabela completa de `GenerationDomainKindV1` é:

| Valor | Nome | Lifetime | Authority/scope key |
|---:|---|---|---|
| 0 | `INVALID` | — | sempre recusado |
| 1 | `BOOTSTRAP_MARKER_PUBLICATION` | PHASE_ONLY | slots bootstrap; ZERO32 |
| 2 | `CHECKPOINT_INDEX_PUBLICATION` | OPERATIONAL | slots checkpoint.index; ZERO32 |
| 3 | `RESERVED_NO_COUNTER` | — | recusado: commit.state não possui generation própria |
| 4 | `DATABASE_RUNTIME_GENERATION` | OPERATIONAL | runtime coordinator; ZERO32 |
| 5 | `WRITER_LEASE_EPOCH` | OPERATIONAL | writer-lease coordinator; ZERO32 |
| 6 | `WAL_COORDINATION_NAMESPACE` | OPERATIONAL | WAL coordination carrier; ZERO32 |
| 7 | `WAL_MUTATION_GENERATION` | OPERATIONAL | WAL coordination carrier; ZERO32 |
| 8 | `DEVICE_REPLAY_GENERATION` | OPERATIONAL | WAL coordination carrier; ZERO32 |
| 9 | `SYSTEM_FLOOR_GENERATION` | OPERATIONAL | durable pending/checkpoint floor map no WAL carrier; ZERO32 |
| 10 | `INDEX_NAMESPACE_GENERATION` | OPERATIONAL | global index namespace carrier; ZERO32 |
| 11 | `INDEX_MUTATION_GENERATION` | OPERATIONAL | per-index carrier; PersistedIndexIdentity |
| 12 | `RESERVED_NO_COUNTER` | — | recusado: catálogo v1 não possui generation própria |
| 13 | `WAL_FEATURE_FENCE_GENERATION` | PHASE_ONLY | DatabaseIdentity; ZERO32 |
| 14 | `HEAP_IDENTITY_GENERATION` | PHASE_ONLY | heap0 identity field; ZERO32 |
| 15 | `PROCESS_REGISTRY_GENERATION` | OPERATIONAL | runtime-only external coordinator incarnation; prohibited in persisted capacity plan |

Valores 3, 12 e 16..255 são reserved/unknown e recusam. `domain_lifetime` precisa ser exatamente o valor da
tabela; não é escolha do encoder. `carrier_identity_generation` per-index é cópia imutável do valor
`INDEX_NAMESPACE_GENERATION` que criou o carrier e **não** é counter separado; contá-la outra vez é
malformed. `Page.seq` é detector u32, WAL `(epoch,txn_id)` é grammar identity, e o
`namespace_generation` observado do filesystem não é incrementado por Grafx: nenhum entra no plan.
`pool.derived_epoch`, manager/Transaction generations process-local também não entram no plan
persistido e continuam sujeitos ao checked increment local nas próprias seções; crash cria outro
pool/manager, nunca “retoma allowance” persistida deles.

`GenerationActionKindV1` é a tabela u8 fechada abaixo; 0, 4, 10, 11, 21 e 25..255 recusam:

| Valor | Action | Único domain permitido |
|---:|---|---|
| 1 | `INITIALIZE_COUNTER` | 4–11, quando o carrier nasce sem executar outra action semântica |
| 2 | `PUBLISH_BOOTSTRAP_MARKER` | 1 |
| 3 | `PUBLISH_CHECKPOINT_INDEX_STATE` | 2 |
| 4 | `RESERVED_NO_COUNTER` | recusado |
| 5 | `ADVANCE_DATABASE_RUNTIME` | 4 |
| 6 | `ACQUIRE_WRITER_LEASE` | 5 |
| 7 | `ADVANCE_WAL_COORDINATION_NAMESPACE` | 6 |
| 8 | `AUTHORIZE_WAL_MUTATION` | 7 |
| 9 | `PUBLISH_DEVICE_REPLAY` | 8 |
| 10 | `RESERVED_DIRECT_READER_REGISTER` | recusado; begin usa 15→16→17 |
| 11 | `RESERVED_DIRECT_READER_TERMINATE` | recusado; close usa 17 |
| 12 | `INSTALL_CHECKPOINT_READER_FLOOR` | 15 |
| 13 | `RELEASE_CHECKPOINT_READER_FLOOR` | 15 |
| 14 | `RELEASE_PENDING_OPERATION_FLOOR` | 9 |
| 15 | `INSTALL_WAL_SCAN_PIN` | 15 |
| 16 | `CONVERT_WAL_SCAN_PIN_TO_READER` | 15 |
| 17 | `TERMINATE_WAL_SCAN_OR_CONVERTED_READER` | 15 |
| 18 | `ADVANCE_INDEX_NAMESPACE_CREATE` | 10 |
| 19 | `ADVANCE_INDEX_NAMESPACE_DROP` | 10 |
| 20 | `AUTHORIZE_INDEX_MUTATION` | 11 |
| 21 | `RESERVED_NO_COUNTER` | recusado |
| 22 | `PUBLISH_WAL_FEATURE_FENCE` | 13 |
| 23 | `PUBLISH_HEAP_IDENTITY` | 14 |
| 24 | `INSTALL_PENDING_OPERATION_FLOOR_SET` | 9 |

`INITIALIZE_COUNTER` jamais é combinado com a action semântica que já publica `0→1`: o primeiro
marker usa 2, checkpoint usa 3 e fence/heap usam 22/23. commit.state é autenticado por raw SHA+P/Q e
catálogo por WAL/raw inventory/index generations; nenhum inventa counter. Uma imagem
física que inicializa três counters do mesmo carrier produz três steps, um por domain; isso não é
dupla contagem porque são counters distintos.

O `phase` de step é exatamente `BootstrapPhaseV1 PREPARING=1, PREPARED=2, FENCED=3, BOUND=4`; zero e
5..255 recusam. `FrozenGenerationActionScheduleV1`, derivado do bootstrap kind, N identities e dos
manifests/flows fechados, atribui um `logical_ordinal` global a **cada site que pode incrementar um
counter**. Cada step representa exatamente um checked successor naquele site:
`mandatory_success_increments == 1` e `maximum_increments_per_failed_attempt` é 0 ou 1. Site capaz de
incrementar N vezes é expandido em N ordinals; aggregation silenciosa é proibida. Uma action física
que incrementa K domains repete o mesmo ordinal em K entries, nunca duplica o mesmo domain.

Entries são ordenadas lexicograficamente pelos bytes `(domain_kind,domain_identity)` e únicas por essa
chave. Steps de uma entry são ordenados por `(phase,logical_ordinal,action_kind)` e únicos pela tuple.
O registry estático `GenerationActionRegistryV1` mapeia bijetivamente cada primitive/callsite
autorizado a `(domain kind,action kind,phase,ordinal,scope key)`. Antes de calcular headroom, verifier e
feasibility enumeram o flow graph inteiro, todas as identities/carriers/manifests e comparam o multiset
exato ao schedule: missing/extra/duplicate, domain/action ilegal, scope errado ou primitive nova sem
registry recusa antes do primeiro marker. Só depois dessa coverage proof somam os valores.

O inventário é portanto total para toda safety generation persistida/shared que o caminho restante
pode consumir. Counter ainda inexistente usa current zero e inclui sua única primeira publicação; N
índices produzem N entries `INDEX_MUTATION`. Na criação/PREPARING,
`initial_observed_current` precisa ser igual à direct certification e
`initial_terminal_cleanup_liability` à liability live direta (zero sob offline zero-participants);
depois ambos permanecem baselines imutáveis do marker e **não** são comparados por igualdade ao estado
corrente.

Os limites independentes V1 são `MAX_SYSTEM_PENDING_RECORDS_V1=4096` e
`MAX_PROCESS_LIFECYCLE_RECORDS_V1=4096`; **não** existe soma `MAX_LIFECYCLE_RECORDS`, carrier combinado
nem transferência de capacidade entre eles. `PendingOuterOperationMap`, checkpoint manifest e
`SystemOperationFloorMapSlotV1` recusam count SYSTEM acima de 4096; o coordinator externo recusa
qualquer PROCESS admission acima de 4096. Bootstrap reserva/certifica somente os 4096 records SYSTEM
no carrier do database; a capacidade PROCESS pertence à incarnation externa. Um checkpoint que
precisaria instalar a system record 4097 recusa antes de action 24, sem WAL, temp ou publication
persistida; MARK por si só não instala record SYSTEM.

`post_bound_operational_reserve` é zero somente para lifetime PHASE_ONLY. Para OPERATIONAL ele cobre
por domain o máximo exact entre os increments do primeiro cold public open, de um generic commit
mínimo e da primeira operação de integridade/checkpoint. Para `SYSTEM_FLOOR_GENERATION` é ao menos
`checked(1 + MAX_SYSTEM_PENDING_RECORDS_V1) = 4097`: uma action 24 instala atomicamente um set
nonempty e reserva liability, e até 4096 actions 14 posteriores removem uma key por successor. Actions
PROCESS 12/13/15/16/17 não entram nessa conta nem no plan persistido; 10/11 são reserved/refused. A
incarnation externa reserva atomicamente 2 transitions para checkpoint floor ou 3 para scan-pin
conversion; frontier-only scan reserva exatamente 2 (install/terminate). O
máximo é a capacidade built-in do catálogo/índices, não config renovável. Para os demais domains
persistidos é ao menos 1 salvo fórmula maior do registry. Não se publica BOUND normal que já nasce
terminal. V7 não define estado alternativo.

Para domains persistidos,
`domain_identity = SHA256(DOMAIN_GENERATION_IDENTITY_V1 || database_uuid || domain_kind_u8 || stable_scope_key)`.
`stable_scope_key` é ZERO32 para domínio database-global, a `PersistedIndexIdentity` pre-marker para
domínio per-index, ou um ordinal built-in canônico quando o enum declarar múltiplos scopes. Nunca
contém file/directory identity, marker/slot/plan SHA, runtime/attempt nonce, generation corrente ou
qualquer output criado pela phase. StableNamespace/carrier file identities ficam na direct proof/
manifest externo e precisam mapear bijetivamente para o domain, mas não entram na sua fórmula. Assim
counters ainda inexistentes têm name acíclico pre-marker. A única exceção fechada é o domain 15,
runtime-only:
`process_domain_identity = SHA256(DOMAIN_PROCESS_IDENTITY_V1 || database_uuid || coordinator_incarnation_identity)`.
A incarnation CSPRNG nonzero é obrigatória em todo ProcessRegistry transition proof e checkpoint
snapshot; ZERO32, incarnation velha ou mistura com a fórmula persistida recusa. Esse identity nunca
entra em CapacityPlan/SuccessorSet persistido, e nenhum outro domain aceita runtime nonce.

`minimum_path_remaining(domain,progress)` é a soma checked de todos os
`mandatory_success_increments` ainda necessários no caminho sem falha até BOUND, derivada apenas de
marker phase, checkpoint/grouped WAL, direct carriers e ordinals committed/materialized — nunca de
RAM. Para o `OperationBudget` corrente,
`worst_case_burns_this_budget` soma, **por domínio**, `maximum_increments_per_failed_attempt` para
todos os attempts ainda possíveis, assumindo que todos falham e nenhum reduz o minimum path; múltiplas
increments do mesmo attempt (lease + WAL/index/device etc.) são contadas separadamente no domínio
correto. Antes do primeiro marker exige-se, para cada entry, com aritmética checked:

```text
initial_observed_current + initial_terminal_cleanup_liability
  + worst_case_burns_this_budget + minimum_path_remaining
  + post_bound_operational_reserve <= MAX_U64
```

O mesmo guard é recertificado antes da primeira generation increment de **cada attempt** e antes de
cada incremento subsequente. Em resume, `recertified_current >= initial_observed_current` é obrigatório;
`consumed = recertified_current - initial_observed_current` usa checked subtraction.
`completed_success_increments(progress)` vem da phase/grouped history/direct states; consumed menor é
regressão/corrupção e todo excesso `consumed-completed_success_increments` é burn conservador, nunca
“crédito”. Novo resume/budget não renova allowance: lê o current maior, preserva o mesmo plan/formula/
config ceiling e recalcula a folga `MAX-current-minimum_path`; max_attempts acima do ceiling é recusado.
Um step que incrementou mas falhou antes de progresso durável conta como burn e o minimum permanece;
só phase/transaction/checkpoint direct-valid reduz o minimum. Um step bem-sucedido descarta apenas os
burns não usados daquele step, nunca os transfere nem reseta counter.

Para não contar duas vezes, no começo do attempt:
`attempts_remaining = max_attempts - attempt_number + 1` e
`worst_case_future_burns = attempts_remaining * maximum_increments_per_failed_attempt` por domínio.
Após k increments no attempt corrente, usa-se apenas o máximo **ainda possível antes de failure** nele
mais `(attempts_remaining-1)*maximum_increments_per_failed_attempt`; os k já estão em
`recertified_current`, não na soma futura. A desigualdade runtime é sempre
`recertified_current + live_terminal_cleanup_liability + worst_case_future_burns +
 minimum_path_remaining_after_all_failures
 + post_bound_operational_reserve <= MAX_U64`.

`LifecycleOwnerKindV1` é `u8`: `INVALID=0`, `PROCESS=1`, `SYSTEM_OPERATION=2`; 3..255 recusam.
PROCESS e SYSTEM usam states fisicamente distintos; nunca compartilham counter/liability.

```text
ProtectedWalRangeSetV1 = (
  magic=MAGIC_PROTECTED_WAL_RANGE_SET_V1, version u16=1, reserved_zero u16,
  record_length u32, database_uuid[16], database_runtime_generation u64,
  stable_wal_root_binding_sha256[32], checkpoint_q u64, sealed_frontier_p u64,
  floor_segment_number u64, floor_offset u64,
  physical_tail_segment_number u64, physical_tail_offset u64,
  scan_seed_wal_runtime_mutation_generation u64,
  range_count u32, reserved_zero u32,
  ranges[range_count], crc32c u32
)
protected_wal_range := (
  entry_length u32=72, segment_state u8, reserved_zero[3], segment_number u64,
  file_identity[32], start_offset u64, end_offset_exclusive u64,
  observed_file_length u64
)
protected_wal_range_set_sha256 =
  SHA256(DOMAIN_PROTECTED_WAL_RANGE_SET_V1 || canonical ProtectedWalRangeSetV1 bytes)

ProcessRegistryStateV1 = (
  magic=MAGIC_PROCESS_REGISTRY_STATE_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], database_runtime_generation u64,
  coordinator_incarnation_identity[32], process_registry_generation u64,
  terminal_cleanup_liability u64, registration_count u32, reserved_zero u32,
  registrations[registration_count], crc32c u32
)
process_registration := (
  entry_length u32, record_kind u8, record_state u8, owner_kind u8=PROCESS,
  reserved_zero u8, owner_pid u64, process_birth_identity[16], manager_identity[32],
  snapshot_lsn u64, seed_nonce[32], protected_range_set_length u32,
  terminal_cleanup_liability u32, protected_range_set_sha256[32],
  protected_range_set_bytes[protected_range_set_length]
)

ProcessRegistryTransitionProofV1 = (
  magic=MAGIC_PROCESS_REGISTRY_TRANSITION_PROOF_V1, version u16=1,
  action_kind u8, reserved_zero u8, record_length u32,
  database_uuid[16], database_runtime_generation u64,
  coordinator_incarnation_identity[32], predecessor_generation u64, successor_generation u64,
  record_kind u8, predecessor_state u8, successor_state u8, reserved_zero u8,
  owner_pid u64, process_birth_identity[16], manager_identity[32], seed_nonce[32],
  predecessor_snapshot_lsn u64, successor_snapshot_lsn u64,
  predecessor_protected_range_set_sha256[32], successor_protected_range_set_sha256[32],
  predecessor_liability u32, successor_liability u32,
  predecessor_registry_state_length u32, successor_registry_state_length u32,
  predecessor_registry_state_raw_sha256[32], successor_registry_state_raw_sha256[32],
  predecessor_registry_state_bytes[predecessor_registry_state_length],
  successor_registry_state_bytes[successor_registry_state_length], crc32c u32
)
process_registry_transition_proof_sha256 =
  SHA256(DOMAIN_PROCESS_REGISTRY_TRANSITION_PROOF_V1 || canonical proof bytes)
```

`ProcessRegistryRecordKindV1` é `INVALID=0`, `RESERVED_DIRECT_READER=1`,
`PUBLIC_BEGIN_WAL_SCAN_PIN=2`, `CHECKPOINT_READER_FLOOR=3`, `FRONTIER_ONLY_WAL_SCAN_PIN=4`;
5..255 recusam. `ProcessRegistryRecordStateV1` é `INVALID=0`,
`ACTIVE=1`, `CONVERTED_TO_READER=2`; 3..255 recusam e CONVERTED só vale para kind 2. O registry vive
no coordinator **externo ao database StorageDevice** (OS shared-memory/lock service ou backend
equivalente), é interprocesso linearizável/owner-death-aware e não cria/modifica file, mtime, WAL,
control ou carrier dentro da root DB. Cada action 12/13/15/16/17 faz checked increment no
`PROCESS_REGISTRY_GENERATION` desse coordinator incarnation, mas não aparece no WalCoordinationSlot,
GenerationCapacityPlan ou barrier de storage; actions 10/11 e record kind 1 sempre recusam.

`ProtectedWalRangeSetV1` é a preimage integral, enumerável e process-shared do pin; seu
`record_length == 140 + 72*range_count`, `range_count` é 0..65535 e o record não excede 4718660
bytes. `ProtectedWalSegmentStateV1` é `INVALID=0`, `CLOSED_EXACT=1`, `ACTIVE_PREFIX=2`; 3..255
recusam. Ranges são sorted/unique por `segment_number`, há no máximo um por segmento e
`0 <= start_offset < end_offset_exclusive <= observed_file_length`; file identity é nonzero. A lista
enumera, sem gap lógico, cada segmento canônico tocado pelo scan
frozen desde o locator de Q até o physical tail: primeira entry começa no primeiro byte requerido
depois de `floor_segment_number/floor_offset`, última termina exatamente em
`physical_tail_segment_number/physical_tail_offset`, e todo segmento intermediário é listado. Um set
vazio só vale quando floor e physical tail locators são iguais e nenhum byte precisa ser escaneado.
`checkpoint_q <= sealed_frontier_p`; root binding/UUID/runtime são current e nonzero.

O pin inicial autentica **descriptors**, file identities, observed lengths, locators, namespace root e
generation, não content SHA. Ele nasce no primeiro COMMIT antes do scan; portanto nenhum hash de WAL
proporcional ao sufixo, placeholder ou cache entra no codec. Depois de sair de COMMIT, o scanner lê e
hasheia os bytes protegidos; `WalPhysicalScanEvidenceV1` carrega os SHAs finais e o segundo COMMIT
revalida descriptors/generation bit-identical antes de aceitar essa evidence. `CLOSED_EXACT` exige
fresh file identity e length iguais; somente a última entry,
no physical tail, pode ser `ACTIVE_PREFIX`, e então
`end_offset_exclusive == physical_tail_offset == observed_file_length`: fresh length pode crescer,
mas nunca encolher nem alterar o prefixo/identity. O set autentica o
`scan_seed_wal_runtime_mutation_generation` observado; incremento posterior por mutação comprovadamente
disjunta não apaga o pin, mas faz o segundo COMMIT do próprio scanner reiniciar. O overlap de um
mutador é decidido pelos bytes decodificados, nunca pelo SHA: recycle/delete/rename de segmento entre
floor e physical tail, truncate/repair que intersecte um intervalo, ou replace/file-identity change
recusa/restart; append cujo primeiro byte é `>=physical_tail_offset` no mesmo ACTIVE_PREFIX e criação
de segmento posterior são disjuntos, embora ainda incrementem a geração. Namespace não canônico,
alias/reparse ou mutação que não possa calcular seu exact range é conflito conservador.

Cada `process_registration` tem `entry_length == 144 + protected_range_set_length`.
`ProcessRegistryStateV1.record_length == 100 + sum(entry_length)`, count é 0..4096, o record integral
é 100..16777216 bytes, EOF/CRC/canonical re-encode são exatos e a soma checked das liabilities u32 de
todas as entries precisa igualar a liability u64 do header. Entries são
sorted/unique por `(record_kind,owner_pid,process_birth_identity,manager_identity,seed_nonce)`; o
`registrations_sha256` de snapshots cobre os bytes integrais nessa ordem. A matriz total é:

Admission calcula successor length antes do CAS; exceder count/range/state bound recusa typed com zero
registration e solicita checkpoint/backpressure, nunca trunca/coalesce ranges nem instala só o SHA.

| Kind/state | `snapshot_lsn` | `seed_nonce` | length/SHA/bytes do range set | liability | Próxima transition legal |
|---|---|---|---|---:|---|
| `PUBLIC_BEGIN_WAL_SCAN_PIN/ACTIVE` | P selado, inclusive 0 | nonzero | record integral nonempty com Q/P correspondentes | 2 | action 16 converte, ou action 17 remove e libera a reserva não consumida |
| `PUBLIC_BEGIN_WAL_SCAN_PIN/CONVERTED_TO_READER` | S=P escolhido atomicamente na action 16 | mesmo nonzero | `0/ZERO32/empty` | 1 | action 17 remove |
| `FRONTIER_ONLY_WAL_SCAN_PIN/ACTIVE` | P selado, inclusive 0 | nonzero | record integral nonempty com Q/P correspondentes | 1 | somente action 17 remove; action16 recusa |
| `CHECKPOINT_READER_FLOOR/ACTIVE` | target_Q, inclusive 0 | nonzero | `0/ZERO32/empty` | 1 | action 13 remove |

ACTIVE kind 2/4 exige length 140..4718660, bytes que decodificam canonicamente, SHA recomputado e
`database_uuid/runtime/sealed_frontier_p` iguais ao registry/snapshot P; não existe locator externo,
cache process-local ou preimage recuperada apenas pelo digest. Qualquer outra combinação kind/state,
nonce zero, length/SHA/bytes indevidos ou liability diferente recusa. Action 16 é CAS única que
preserva uma key kind 2/nonce, muda ACTIVE→CONVERTED, fixa S=P e zera atomicamente length/SHA/bytes
2→1; não há janela sem pin/reader. Action terminal remove exatamente a entry e decrementa a liability
pela liability anterior reservada; owner-death usa a mesma transition e nunca edita em place sem
incremento. Admission em MAX-1 para begin/install, conversion sem slot terminal, sum mismatch ou
reuso de entry de incarnation anterior recusam com zero registration persistente no coordinator.

No transition proof, `ProcessRegistryTransitionEndpointStateV1` reutiliza ACTIVE=1/CONVERTED=2 e
define ABSENT=0 **somente como endpoint do proof**, nunca como stored registration. As únicas tuples
são: action12 `ABSENT→CHECKPOINT_FLOOR/ACTIVE`; 13 `ACTIVE→ABSENT`; action15
`ABSENT→PUBLIC_BEGIN_WAL_SCAN_PIN/ACTIVE` para admission kind 1 ou
`ABSENT→FRONTIER_ONLY_WAL_SCAN_PIN/ACTIVE` para admission kind 2; action16 somente kind2
`ACTIVE→CONVERTED`; action17 kind2/kind4 `ACTIVE|CONVERTED→ABSENT`. Actions 10/11,
outro record kind/state ou cross-key recusam. Successor generation é checked predecessor+1. Os dois
embedded bytes decodificam como `ProcessRegistryStateV1`, seus raw SHAs são SHA256 sem domain dos
bytes exatos, e diferem somente pela única registration/liability/header generation prescrita; todas
as demais entries precisam ser bit-identical. Owner/process/manager/seed e endpoint fields repetem a
entry antes/depois; os endpoint range SHAs repetidos precisam casar o record integral embutido, e o
verifier obtém length/bytes decodificando essa entry, não aceitando o SHA como locator. ABSENT exige
os campos inaplicáveis ao endpoint zero. Assim o proof é verificável sem confiar num boolean do
coordinator e não pode representar duas CAS, outra key ou uma lista parcial. Cada embedded state
length é 100..16777216 e
`record_length==344+predecessor_registry_state_length+successor_registry_state_length<=33554776`;
soma/EOF/CRC são checked. Action16 exige ambas preimages integrais e é emitida somente pelo CAS
linearizável que publicou o successor.

Invariante externa permanente é `process_generation + process_terminal_liability<=MAX_U64`.
`ProcessLifecycleAdmissionKindV1` é fechado: `PUBLIC_BEGIN=1` reserva
scan install+convert+terminate (3), `FRONTIER_SCAN_ONLY=2` reserva install+terminate (2), e
`CHECKPOINT_FLOOR=3` reserva floor+release (2); zero/4..255 recusam. A admission escolhida uma vez na
entrada da rota reserva o set integral atomicamente antes do primeiro record. FULL_SCAN de cold,
recovery ou checkpoint-rebind usa exatamente kind 2 e proíbe action16; begin usa kind 1. Não há registration adicional no begin: a
única reader key é o mesmo `PUBLIC_BEGIN_WAL_SCAN_PIN` convertido, e close/abort/owner-death executa
exatamente action 17 uma vez. MAX-1 recusa
nonterminal com zero registration.
Coordinator restart só pode ocorrer após prova OS de zero owners/records; cria nova CSPRNG incarnation
e generation 1. Todos os tokens/snapshots incluem incarnation+database runtime, portanto isso não é
wrap/ABA nem permite token antigo. Owner vivo não expira por TTL; owner-death termina usando liability.
Read-only filesystem pode assim registrar/proteger ranges sem database write.

SYSTEM floor state permanece no `WalCoordinationSlotV1` + `SystemOperationFloorMapSlotV1`; seu
`system_floor_generation + system_terminal_cleanup_liability <= MAX_U64` é persistido. Action 24
instala atomicamente um nonempty set de pending-operation records e reserva uma future action14 por
record; action14 remove exatamente um record/publication. Não existe reader/scan PROCESS nesse map.
Release/deregister em ambos registries nunca renova, empresta ou reseta headroom.

Reader registrations, `WAL_SCAN_PIN` e reader/checkpoint attempt floors são `PROCESS`, ligados a
manager/PID/process-birth/runtime/coordinator incarnation; owner-death OS comprovado pode executar sua
transition terminal externa. `PENDING_OPERATION_FLOOR_PIN` é **SYSTEM_OPERATION**, keyed por
`(database_uuid, system_floor_key_sha256)` e repete outer operation, full plan e retention boundary;
contém o
`SystemFloorInstallEntryBaseV1` SHA e a projection SHA da pending entry que o originou, ambas
recomputáveis da grouped history/plan source sem depender de um map slot histórico. Não possui PID
owner, TTL, heartbeat ou owner-death reap;
kill do processo apenas perde o handle efêmero. Recovery/checkpoint posterior sob fresh lease pode
assumir um handle local depois de recertificar registry record, grouped pending state, marker/Q e
history floor, sem alterar generation/record. Action 14 é a única remoção e exige BOUND/CLEAR terminal
do mesmo outer/full-plan **já incorporado por checkpoint**; faz slot barrier/direct cert. Não existe
release abort/pre-effect: antes da action24 não há SYSTEM key/liability, e depois dela o MARK committed
já é effect obrigatório. Missing system pin quando Q já atravessou seu retention boundary, extra sem pending/terminal ou
key divergente é CORRUPT/INCONCLUSIVE, nunca owner cleanup.

```text
GenerationTransitionPlanV1 = (
  magic=MAGIC_GENERATION_TRANSITION_PLAN_V1, version u16=1,
  semantic_operation_code u16, record_length u32, database_uuid[16],
  outer_operation_identity[32], full_plan_sha256[32], semantic_plan_source_sha256[32],
  entry_count u32, entries_length u32,
  entries[entry_count], crc32c u32
)
generation_transition_entry := (
  entry_length u32=156, logical_ordinal u32, domain_kind u8,
  domain_lifetime u8, action_code u8, target_kind u8,
  domain_identity[32], predecessor u64, successor u64,
  target_identity[32], semantic_target_sha256[32],
  semantic_edge_sha256[32]
)
generation_transition_plan_sha256 =
  SHA256(DOMAIN_GENERATION_TRANSITION_PLAN_V1 || canonical bytes)

GenerationSuccessorSetV1 = (
  magic=MAGIC_GENERATION_SUCCESSOR_SET_V1, version u16=1, outer_operation_code u16, record_length u32,
  database_uuid[16], operation_budget_sha256[32], operation_attempt_sha256[32],
  actual_source_proof_sha256[32], operation_plan_sha256[32],
  entry_count u32, total_edge_count u32, entries[entry_count], crc32c u32
)
successor_entry := (
  entry_length u32, domain_kind u8, domain_lifetime u8, reserved_zero u16,
  domain_identity[32], direct_current u64, edge_count u32, reserved_zero u32,
  edges[edge_count]
)
successor_edge := (
  edge_role u8, action_kind u8, phase u8, reserved_zero u8, logical_ordinal u32,
  predecessor u64, successor u64, liability_pre u64, liability_post u64
)
generation_successor_set_sha256 = SHA256(DOMAIN_GENERATION_SUCCESSOR_SET_V1 || canonical bytes)

GenerationAdmissionReservationV1 = (
  magic=MAGIC_GENERATION_ADMISSION_V1, version u16=1, reservation_state u8, reserved_zero u8,
  record_length u32, generation_successor_set_sha256[32], participant_identity[32],
  owner_pid u64, process_birth_identity[16], operation_budget_sha256[32],
  operation_attempt_sha256[32], planned_lease_epoch u64, fresh_lease_nonce[32],
  claim_count u32, claims[claim_count], crc32c u32
)
claim := (
  domain_kind u8, reserved_zero[3], logical_ordinal u32, domain_identity[32],
  predecessor u64, successor u64
)
```

`GenerationSemanticOperationCodeV1` é u16 fechado: `INVALID=0`, `BOOTSTRAP=1`,
`CHECKPOINT_ADVANCE=2`, `GAP_RECOVERY=3`, `DEVICE_CATCH_UP=4`, `TRANSACTION_COMMIT=5`, `DDL=6`,
`INDEX_MAINTENANCE=7`, `IDENTITY_REFILL=8`, `LEGACY_BIND=9`, `UPGRADE_OR_REPAIR=10`,
`WAL_TAIL_COMPOSITE=11`, `TERMINAL_FLOOR_RELEASE=12`, `SYSTEM_FLOOR_INSTALL=13`; 14..65535 recusam. Ele identifica a operação
semântica estável e **não** a porta pública que a está adotando. A matriz total entre semantic code,
source digest e `OuterOperationCodeV1` do attempt é:

| Semantic code | `semantic_plan_source_sha256` decodifica/iguala | Outer codes admitidos |
|---|---|---|
| 1 | `CutoverFeasibilityCoreV1` ou `FrozenOuterOperationPlanV1` de genesis | 31, 32 |
| 2 | `CheckpointCompositeSemanticBaseV1` | 16, 30, 39 |
| 3 | `RecoveryApplyPlanV1` | 16, 17, 27, 29 |
| 4 | `ColdReplayPlanV1` | 16, 17, 28 |
| 5 | `FrozenOuterOperationPlanV1` TRANSACTION | 18 |
| 6 | `FrozenOuterOperationPlanV1` DDL | 19, 20, 21 |
| 7 | `FrozenOuterOperationPlanV1` index-only | 22, 23, 24, 25, 26 |
| 8 | `FrozenOuterOperationPlanV1` refill/reservation | 37 |
| 9 | `CutoverFeasibilityCoreV1`/stable BIND plan source anterior ao próprio transition plan | 33, 34 |
| 10 | `FrozenOuterOperationPlanV1` upgrade/repair | 35, 36 |
| 11 | `TailMutationCompositePlanV1` | 29, 30, 36 |
| 12 | `TerminalFloorReleaseRecipeV1` já publicado no checkpoint manifest | 16, 30, 39 |
| 13 | `SystemFloorInstallBatchPlanV1` acíclico | 16, 30, 39 |

Uma rota composta pode ter mais de um transition plan, cada qual com um único semantic code da
tabela; uma action de control/namespace é uma target edge do semantic parent e não inventa outro
code. Em particular, action24 aparece somente num plan code13 e é proibida no plan code2; checkpoint
code2 começa da system-floor preimage já instalada/direct-certified. O outer 16 só adota
`CHECKPOINT_ADVANCE` quando existe `PendingCheckpointPublicationV1`
bit-identical, e só adota GAP/DEVICE quando o cold-open proof correspondente existe. Outer 17 só
adota GAP/DEVICE durante `begin(write)` sob o budget original. Nenhuma outra combinação é inferida por
nome de helper. Para code 9, a source é o `cutover_core_digest`/`bind_plan_source_digest` acíclico da
seção 5, nunca feasibility/marker final que contém o próprio transition plan.

Para semantic code 11 a tuple é única, sem escolha do encoder:

```text
tail_plan = decode_canonical_TailMutationCompositePlanV1(source_bytes)
semantic_plan_source_sha256 = tail_mutation_composite_plan_sha256
outer_operation_identity    = tail_plan.composite_identity
full_plan_sha256            = tail_mutation_composite_plan_sha256
```

Os dois SHAs iguais são o digest registrado do `tail_plan` integral e `composite_identity` é
recomputado pelo DAG da seção 10.3 a partir de base-intent + dois semantic bodies. O tail plan não
contém transition-plan SHA, semantic edge, successor set, attempt ou seu próprio digest; logo
`base/semantic bodies → composite identity → tail plan SHA → transition plan` é acíclico. Event/
publication/quarantine identity usado como outer, full ZERO/outro SHA, source diferente de full,
tail plan sem preimage ou plan que embuta qualquer successor recusa antes de claim/publication.

`GenerationTransitionTargetKindV1` é u8 fechado: `INVALID=0`, `BOOTSTRAP_SLOT=1`,
`CHECKPOINT_SLOT=2`, `DATABASE_RUNTIME_CARRIER=3`, `WRITER_LEASE_CARRIER=4`,
`WAL_COORDINATION_CARRIER=5`, `WAL_BYTES_OR_NAMESPACE=6`, `DEVICE_REPLAY_CARRIER=7`,
`SYSTEM_FLOOR_MAP=8`, `INDEX_NAMESPACE_CARRIER=9`, `INDEX_MUTATION_CARRIER=10`,
`DATABASE_IDENTITY_FIELD=11`, `HEAP_IDENTITY_FIELD=12`; 13..255 recusam. A matriz total
`action_code→target_kind` é: 2→1, 3→2, 5→3, 6→4, 7→5, 8→6, 9→7, 14/24→8, 18/19→9,
20→10, 22→11 e 23→12. Action 1 usa respectivamente 3, 4, 5, 7, 8, 9 ou 10 conforme seu domain
4..11; qualquer outra combinação recusa. `target_identity` é ZERO32 para o único carrier/field
database-global e a `PersistedIndexIdentity` para target per-index; ele nunca é file ID, path hash ou
handle. `semantic_target_sha256` é nonzero e iguala o digest registrado da preimage/target semântica
específica apontada pela source plan: slot/control body, namespace projection, exact WAL set, device
manifest, system-floor map/set, index manifest/recipe ou identity payload. Um digest opaco não
decodificável por essa source plan recusa.

Cada transition entry tem exatamente `4+4+4+32+8+8+32+32+32 = 156` bytes; o terceiro termo `4` são
os quatro u8. `entry_length` inclui seus próprios quatro bytes, como todos os outros entries V7.
`entries_length == 156*entry_count` e
`record_length == 140 + entries_length`, com multiplicação/soma checked e EOF exatamente depois do
CRC. Golden fixtures N=1 e N=2 têm respectivamente record lengths 296 e 452; decoder truncado em −1,
length declarado +1 e byte após EOF são gates obrigatórios. Nenhum decoder pode procurar a próxima
magic ou consumir o prefixo da entry seguinte como padding.

`GenerationTransitionPlanV1` é a projection **estável**, persistível e independente de attempt:
entries 1..65536 são sorted/unique por `(domain_kind,domain_identity,logical_ordinal)`, usam os mesmos
enums/tabelas numéricas de domain/lifetime/action, `successor==checked_increment(predecessor)` e
`semantic_edge_sha256` precisa ser exatamente:

```text
SHA256(DOMAIN_GENERATION_SEMANTIC_EDGE_V1 ||
  LE16(semantic_operation_code) || database_uuid || outer_operation_identity ||
  full_plan_sha256 || semantic_plan_source_sha256 || LE32(logical_ordinal) ||
  domain_kind_u8 || domain_lifetime_u8 || action_code_u8 || target_kind_u8 ||
  domain_identity || LE64(predecessor) || LE64(successor) || target_identity ||
  semantic_target_sha256)
```

`semantic_plan_source_sha256` é sempre nonzero. Multi-step BIND/rebuild/tail exige
`outer_operation_identity` e `full_plan_sha256` nonzero e iguais ao frozen stable plan; uma operação
unitária exige ambos ZERO32. Eles não podem apontar para record que contém este transition plan ou
seu SHA. Budget, attempt, lease, claim, actual source, runtime generation volátil e permit nonce não
entram nessa fórmula. Cada stable publication obrigatória mapeia bijetivamente a uma entry, nenhuma
publication compartilha entry e o verifier recomputa target kind, identity, target digest e edge SHA
a partir da source preimage, sem aceitar field hash como autoridade autônoma.

`GenerationSuccessorEdgeRoleV1` é `INVALID=0`, `STABLE_PLAN_EDGE=1`,
`ATTEMPT_AUXILIARY_EDGE=2`; 3..255 recusam. `GenerationSuccessorSetV1` é a claim/certificação
**attempt-local** do plan. Seu `operation_plan_sha256` é exatamente
`generation_transition_plan_sha256`. Edges role 1 cobrem todas e somente as entries estáveis ainda
não adotadas. Em resume, para cada domain, direct state precisa formar um prefixo do stable plan: edge
já em seu successor é certificada por exact adoption proof e omitida do fresh set; edge ainda no
predecessor aparece uma vez no fresh set com current/predecessor/successor idênticos; terceiro valor,
gap, reorder ou target divergente recusa. A união ordered de adopted-prefix + fresh role-1 edges é
bijetiva ao plan integral.

A única adoption não terminal é a edge estável da phase 0 de `WAL_TAIL_COMPOSITE`: seu próprio
successor slot precisa carregar `pending_tail_phase0_state=HELD_OPEN` e o predecessor exato, conforme
a seção 10.3. Enquanto esse estado persistido estiver ativo, a edge não é considerada aplicada nem
pode ser usada por outra operação; ela fica **held-open**, é omitida do fresh set e só a continuation
exact-type fechada pode completar seu target original. Nenhuma outra domain/action aceita successor
sem postimage terminal exacta.

Role 2 existe somente para increments obrigatórios daquele attempt que não são publication estável
do plan — por exemplo fresh writer lease, runtime replay/incarnation ou liability terminal exigida
pela matriz da rota. Seu `(domain,action,phase,ordinal)` precisa constar no
`GenerationActionRegistryV1`, não pode duplicar/anteceder uma stable edge do mesmo current e é
recriado a partir do current direto sob cada novo `OperationAttempt`. Ele nunca entra no transition
plan/candidate persistido nem autoriza target semântico. Assim budget/attempt/lease novos mudam o
successor-set SHA e podem mudar somente role-2 edges; nunca renumeram ou mudam uma edge estável.

Successor entry length é `56 + 40*edge_count`; record length inclui CRC. Bounds são 1..65536 entries,
1..1048576 total edges e 1..65536 edges/entry; soma dos edge counts iguala total. Entries são
sorted/unique por `(domain_kind,domain_identity)`, edges por chain order e cada role/domain/lifetime/
action/phase/ordinal usa as tabelas versionadas desta seção. `outer_operation_code` é o code do budget
público atual, casa exatamente à matriz semantic→outer acima e não é copiado para o transition plan.
Budget, attempt e plan SHAs são nonzero/current e não podem ser trocados entre operations. No resume
de checkpoint por cold open, portanto, o stable semantic code continua 2 enquanto o fresh outer code
é 16; reescrever o candidate para 16 ou recusar essa adoção é erro de contrato.
`actual_source_proof_sha256` precisa ser bit-identical ao campo homônimo do `WriteSnapshotProofV1`:
é ZERO32 para `TRANSACTION` quando a matriz desse proof o exige e nonzero para os kinds
`NO_TRANSACTION` que exigem prova actual; qualquer outra combinação zero/nonzero recusa. Direct current
é a leitura autenticada; edge 0 predecessor==current, cada successor é checked predecessor+1 e vira
o predecessor seguinte. Liability pre/post casa a action registry e nunca excede MAX/headroom. CRC,
entry/count/trailing/reserved/unknown/cross-domain ou duplicate edge recusa antes de admission.

`GenerationAdmissionReservationStateV1` é `INVALID=0`, `PLANNED_LEASE=1`, `FRESH_LEASE_BOUND=2`;
3..255 recusam. Claim count iguala total edges e é sorted/unique pelo key
`(domain_kind,domain_identity,predecessor,successor)`; cada claim mapeia bijetivamente a uma edge/SHA do
set. PLANNED exige fresh nonce ZERO e planned epoch nonzero; promoção linearizável exige state 2 e
nonce nonzero sem alterar set/claims. O record vive no registry coordinator, não no database file, mas
usa esse codec para cross-process/crash-reap; owner/participant/budget/attempt mismatch recusa. O
reservation SHA é `SHA256(DOMAIN_GENERATION_ADMISSION_V1||canonical bytes)` e todas as primitives recebem também o
`generation_successor_set_sha256`, nunca um list/object não autenticado.

Domain 15 e actions 10/11/12/13/15/16/17 são proibidos em `GenerationSuccessorSetV1` e
`GenerationAdmissionReservationV1`: sua transação exata é a CAS linearizável do
`ProcessRegistryStateV1`, com transition kind/record key/predecessor/successor/liability autenticados
pelo snapshot da incarnation. Assim begin read-only não precisa entrar em participant, fresh writer
lease ou admission persistida. Omitir esse CAS, aceitar PROCESS no successor set de mutador, ou exigir
um counter SYSTEM para uma transition PROCESS recusa.

Todo mutador multi-domain congela detached um `GenerationSuccessorSetV1`, sorted/unique por
`(domain_kind,domain_identity)`. Cada entry contém `direct_current` e uma
`successor_chain[(role,action,ordinal,predecessor,successor,liability_pre,liability_post)]` nonempty,
ordenada, em que o primeiro predecessor é current, cada successor é checked predecessor+1 e o
successor anterior é o predecessor seguinte. Assim duas phases da mesma operação/domínio são duas
arestas na **mesma** entry, não duas keys conflitantes nem um salto `g→g+2`. O set cobre todos os
counters que lease, namespace, WAL, K índices, checkpoint/recovery e publication podem tocar. Depois
do preflight detached, a operação precisa entrar em `participant`; somente então, antes de adquirir
lease mutável ou publicar o primeiro slot, `GenerationAdmissionReservationV1` usa um registry
interprocesso linearizável do coordinator
existente para instalar atomicamente claims unique por `(domain identity,current,successor)`. A breve
instalação toma apenas locks internos desse registry em ordem lexical e os libera antes de fresh lease;
não toma nem retém writer lease, NAMESPACE, INDEX_VIEW ou COMMIT e não altera counter/file/device.
Claim é admission-only, manager/PID/process-birth/budget/attempt-bound, initially bound ao planned
lease epoch e promovido uma vez ao fresh lease nonce; o participant identity também entra no claim.
Não é correctness authority nem novo counter.
Só release explícito do owner ou owner-death provado o remove; deadline/TTL não permite reap de owner
vivo. Backend process-local/advisory ou sem install-all-or-none recusa shared mutation.

Há uma única exceção de bootstrap, necessária porque o registry não pode autorizar o próprio
nascimento. `BootstrapInitializationExceptionV1` cobre **somente** (a) a publication
`FIRST_PREPARING` 0→1 e (b) cada `INITIALIZE_COUNTER` 0→1/carrier create-exclusive já enumerado no
carrier manifest e no `FrozenGenerationActionScheduleV1`, antes de PREPARED. Não cobre phase successor,
lease, WAL/device/lifecycle/index mutation, DDL, repair ou carrier adicional. Ela requer uma
`GenesisCreationSession` com exclusão interprocesso OS handle-relative sobre
`(trusted_parent_identity,requested_root_component,current_root_identity)` — sem criar arquivo/entry —
ou uma `OfflineMaintenanceSession` legada já exclusiva; backend sem essa primitive recusa genesis/
cutover. A sessão congela/direct-certifica ausência/zero de todos os targets e o capacity plan integral
antes do first marker, emite `_BootstrapInitializationPermit` exact-type por ordinal e mantém a exclusão
até todos os carriers iniciais terem data+parent barrier e cold direct cert.

Cada permit é single-use e target/ordinal/image/0→1-bound; create-existing só adota uma imagem
bit-identical do mesmo manifest/session, nunca overwrite. A criação do WAL coordination carrier e seu
map vazio pode preceder os demais, mas sua mera existência não alarga a exceção. Quando todos os
initialization ordinals foram direct-certified, a sessão publica um completion proof em memória ligado
ao PREPARING/manifest, fecha irrevogavelmente a exceção e, a partir do primeiro successor de phase
(inclusive PREPARED), **toda** aresta usa participant+GenerationAdmission no WAL coordinator já
autoritativo. Crash antes desse ponto reabre somente em bootstrap/cutover mode, recertifica marker/
manifest/targets e conclui os mesmos ordinals; não renumera nem cria outro carrier. Dois genesis
concorrentes sobre root ausente/vazio são serializados pela exclusão parent/root; o loser reabre e
classifica marker/temp/root, sem publicar bytes. Esta exceção não é um claim implícito nem permite que
um database exposto opere sem registry.

Durante install, o registry direct-certifica current/preimage/liability/headroom de todos os domains;
qualquer drift, MAX, missing domain ou conflict libera tudo com zero **persistent** slot/temp/WAL/
barrier/generation (criar/remover o claim efêmero não é publication). Claims bloqueiam qualquer writer
que tente reservar/avançar qualquer aresta da mesma chain. Depois, a operação preserva exatamente a ordem da
seção 11 — participant → GENERATION_ADMISSION claim → fresh LEASE → NAMESPACE → INDEX_VIEW → COMMIT — sem carregar locks de
registry admission. Ao chegar a cada lock canônico e imediatamente antes da primeira publication, recertifica
claim/current/preimage; claim stale cancela tudo. Cada primitive aceita somente seu exact claim; uma
vez publicado o primeiro
successor, I/O/hard-kill pode deixar apenas burns/progress já enumerados e recovery distingue cada um
por slot/WAL/pending proof. MAX previsível jamais é descoberto depois de A já publicar porque B só foi
lido tardiamente.

O claim é liberado/owner-death-reaped antes de `exit participant` em todo success, refusal e unwind;
um thread que falha ao entrar participant nunca o instala. `close/drain` conta esse participant e não
fecha coordinator/runtime enquanto o claim puder estar live. Depois de owner-death, recovery remove o
claim stale antes de admitir sucessor. Event gate claim-installed versus close/recovery precisa provar
que ou A libera e sai, ou close espera; nunca resta claim de PID vivo fora de participant.

Se o pior caso de todos os attempts restantes mais caminho mínimo + operational reserve não cabe, o attempt recusa
`GrafxIntegrityError(field="generation_headroom", retryable=False)` **antes** de lease/CAS/slot/marker/
byte. Após PREPARING/FENCED, a phase fica resume/restore-only sem consumir o último headroom; por ser
counter monotônico, a saída operator é cold restore anterior ou logical export para novo database
UUID, nunca rollback/wrap/rekey. Hard crashes externos ilimitados ainda podem gastar a folga finita,
mas uma execução sem falha e todos os failures permitidos pelo budget congelado nunca começam num
estado previsivelmente incompletável.

O digest e os bytes integrais entram em PREPARING e ficam imutáveis até BOUND. Depois de PREPARING,
mas antes de PREPARED/fence, `CutoverSpacePlanV1` cria e data/directory-certifica por backend os
artefatos opacos `control/v7.reserve/<operation-identity>` autorizados por `_BootstrapArtifactPermit`,
com magic de reservation (nunca Index/WAL), tamanho físico non-sparse quando suportado e lifecycle
exato no image manifest. PREPARED exige cada reservation ou uma capability backend equivalente
durável. Só após a WAL barrier do BIND, `_IrrevocableWalPermit` pode transformar a reservation exata
em workspace, escrever o target integral e renomeá-lo; ela não é target/temp IndexV2 pre-WAL.
Reservation de WAL reduz risco, mas não inventa transferência atômica de espaço entre arquivos.

Capacidade livre pode diminuir externamente. ENOSPC/EDQUOT/OOM transitório depois do fence nunca
reclassifica excesso estrutural nem autoriza rollback: mantém phase FENCED/binding, source e
feasibility/space plan, publica `CUTOVER_RESOURCE_REQUIRED` com backend/required/available/progress e
permite somente resume idempotente por uma `OfflineMaintenanceSession` reacquired que recertifique o
mesmo digest após o operador prover capacidade.
Crash ou retry deriva progresso apenas de grouped WAL, target hashes e marker; não reusa bytes
parciais sem exact preimage/target proof. Somente substep `NO_TRANSACTION` comprovadamente com zero WAL
byte pode usar seed/UUID/raw blob novo que gere a mesma normalized projection; Transaction/fence
preserva seu transaction UUID. Depois do primeiro WAL byte, somente recovery
classifica os bytes, e após barrier reconstitui/reaplica o UUID/blob efetivamente committed sem
replan/novo seed. Restore cold é a outra saída; generic/read/query/maintenance
continuam bloqueados até BOUND.

Todos os campos `baseline_*`, feasibility, generation capacity plan e ambos os demais manifests são
imutáveis de PREPARING a
BOUND. No legacy
cutover eles descrevem B, não o catálogo pós-BIND; o estado final é autenticado por grouped history +
checkpoint `BOUND_V2` e seu catálogo/path/header manifest. Phase advance só muda phase,
publication generation, predecessor e CRC. Genesis não possui efeitos intermediários e seus targets
baseline já são os targets BOUND.

O carrier manifest é um encoding canônico, versionado e length-prefixed de **todos** os namespaces estáveis:
`WAL_COORDINATION/<database_uuid>`, `INDEX_NAMESPACE_CATALOG/<database_uuid>` e um
`INDEX_VIEW/<database_uuid,index_identity>` para cada índice do catálogo baseline, com backend,
identity/path e estado inicial. Ordenação, unicidade, limites, hash, CRC e zero trailing bytes são
checked. `publication_generation` começa em 1 e cresce exatamente uma unidade a cada mudança de
phase; não faz wrap. Escreve-se o slot não corrente por `_publish_control_state_durable` e seu exact
marker permit da seção 5.2, cold
direct decode/hash e só então ele
se torna corrente; o slot anterior permanece até a publicação seguinte. Dois slots máximos válidos
divergentes, predecessor divergente ou salto/regressão de phase são dano.

`enable_wal_semantics_v2()` só pode iniciar sob `OfflineMaintenanceSession` e o runbook da seção 9.3:

1. com binários antigos parados e backup cold registrado, executar checkpoint legado M1 até
   `published_lsn == checkpoint_lsn == B`, data barrier de todos os arquivos e verify/inventário
   direto integral; qualquer stale, I/O inconclusivo, gap ou dirty não drenado recusa;
2. ainda com zero durable effect, congelar DB UUID, commit.state, catálogo, heap/index inventory,
   carrier manifest e `CutoverFeasibilityManifestV1`; recertificar source/tail bit-identical e só
   então publicar `PREPARING` com o digest antes de criar o primeiro artefato V7;
3. criar por `bootstrap_create_entry_durable` os carriers estáveis e, pelo protocol control permit-bound, o baseline
   `CheckpointIndexStateV1(0,0,Q=B,LEGACY_BASELINE)`, incluindo todas as parent-directory barriers;
   materializar/certificar também o `CutoverSpacePlanV1`;
   fazer as visibility/data barriers de cada
   backend e cold direct certificar bytes, inode/file identity, lock domain, slots iniciais e todos os
   hashes do marker; artefato já existente só é aceito se bit-identical ao manifest da mesma generation;
4. publicar `PREPARED`. Somente então a transação de fence da seção 5.3 pode escrever WAL/meta;
5. após fence/checkpoint C, publicar `FENCED`; após todos os `BIND_IDENTITY`, ainda com marker FENCED,
   persistir/barrier/direct-certificar o checkpoint `BOUND_V2` e provar L_terminal; só então publicar
   o slot marker `BOUND` cujo predecessor é o FENCED direct-valid. Fechar a maintenance runtime e fazer
   cold reconnect integral **depois** do marker BOUND. O marker BOUND é retido, não removido por cleanup.

`V7BootstrapStateV1` v1 não carrega raw checkpoint SHA forward; o cold open liga independentemente
marker BOUND + checkpoint state por database UUID, C/Q, history/manifest roots, identities e a
projection L_terminal. Crash depois do checkpoint direct cert mas antes do marker deixa FENCED: somente
resume maintenance recertifica os dois estados e publica BOUND; nenhuma API pública infere a phase.

Checkpoint publication e WAL recycle são separados durante cutover por um pin lógico durável,
derivável sem janela do marker direct-valid:

```text
CUTOVER_PHASE_RETENTION_PIN =
  PREPARING/PREPARED -> (marker_sha, recycle_ceiling=B)
  FENCED             -> (marker_sha, recycle_ceiling=C)
  BOUND              -> absent
```

O ceiling é sempre baseline/terminal COMMIT completo. PREPARED nasce antes do fence e retém a
transação fence inteira mesmo depois de checkpoint Q=C; só marker FENCED durable/direct-certified
substitui esse pin. FENCED nasce antes de BIND0 e retém todos os BIND grouped records, trailers/bodies/
source manifests acima de C mesmo depois de checkpoint Q=P; só marker BOUND durable/direct-certified
remove o pin. Marker slot torn/ambíguo escolhe o predecessor conservador ou recusa recycle; pin nunca
expira por TTL/owner death.

O checkpoint de cutover publica slot + commit.state, barrier/direct-certifica e retorna um
`DeferredCutoverRecycleV1(checkpoint_state_sha,marker_predecessor_sha,recycle_ceiling)` sem remover
bytes além do phase ceiling. A porta publica/direct-certifica o marker sucessor, fecha o primeiro
COMMIT/lease/guards e só em passagem posterior com fresh lease/COMMIT revalida marker+checkpoint+WAL,
congela `WalMutationDraftV1(RECYCLE_PREFIX)` e pode reciclar até Q. Crash em qualquer boundary deixa ou
as raw preimages retidas, ou o marker sucessor já autoritativo; nunca só um history SHA opaco.

Crash em `PREPARING`/`PREPARED`, ou meta v1 junto de qualquer marker, produz
`V7_BOOTSTRAP_INCOMPLETE`: somente resume/restore pela mesma `OfflineMaintenanceSession` é aceito;
generic writer, read-view V7, maintenance e lazy repair recusam
`GrafxUnsupportedOperation(field="v7_bootstrap_incomplete")`. Um processo antigo pode ignorar o
marker enquanto meta ainda é v1, por isso o marker não substitui o runbook/offline fence. Se o crash
deixar o fence WAL/meta durável com marker ainda `PREPARED`, a transação exata é a única prova que
autoriza recovery a avançar para `FENCED`; nenhum estado é inferido por arquivos soltos.

Depois que meta é V2, marker/sidecar/carrier ausente, corrupto ou estranho é
`CORRUPT/INCONCLUSIVE`, nunca `LEGACY_UNFENCED`. Genesis V7 cria baseline, carriers e marker `BOUND`
antes de expor o primeiro handle. Read-only nunca cria, completa nem avança marker. Assim os estados
de operação são exatamente `LEGACY_UNFENCED`, `V7_BOOTSTRAP_INCOMPLETE`,
`V7_INTEGRITY_FENCED` (inclui os modos restritos pós-C) e `V7_BOUND`; somente o último habilita
writer genérico V2 e as invariantes V7 de read/index proof.

#### 3.1.2 Genesis V7 crash-safe e retomável

`V7_GENESIS` não reutiliza implicitamente o fluxo legado. Antes de qualquer write, uma
`GenesisCreationSession` exact-type adquire `StableNamespaceParentCapabilityV1` quando o target está
ausente, ou `StableNamespaceCapabilityV1` quando já existe vazio, conforme a seção 5.2.1. Ela valida
por handles que o target está ausente ou vazio, sem link/reparse/alias. O diretório
vazio, se criado, recebe parent-directory barrier e ainda não é database; o primeiro artefato
**autoritativo/final** de database é sempre o slot root-level `V7_GENESIS/PREPARING`, cuja publicação
não depende de criar `control/`. O único temp deterministic reserved da mesma publicação pode ser a
primeira entry física transitória e segue a proof/disposition fechada abaixo. Esse primeiro slot persiste e autentica o
`GenesisNamespaceBindingV1` da root observada pelo attempt. Antes de o target final desse slot ficar
durable/direct-certified, não existe database UUID nem directory identity persistida a preservar: se
o processo morrer, um attempt novo pode adotar a root **atualmente** vazia e handle-certified no mesmo
final component, ainda que seu file identity seja diferente. Ele gera sessão/DB UUID/manifests novos
e não alega continuidade com RAM perdida. Sem marker final válido, somente o único temp reserved
`PendingControlPublicationV1` do first-marker protocol pode ser retomado ou exact-disposed pela API
explícita da seção 5.2; qualquer outra entry é `GENESIS_ORPHANED/INCONCLUSIVE`, nunca adivinhada nem
apagada por lazy create.
Depois do primeiro PREPARING válido, todo resume exige exatamente o binding persistido; replacement
vazio também é mismatch/inconclusive.

O preflight detached gera DB UUID e congela `GenesisImageManifestV1` com paths, comprimentos, raw
SHA-256, codecs e ordem de criação de todas as imagens-alvo, além do `GenerationCapacityPlanV1` para
as quatro phases, carriers/checkpoint/runtime/reconnects e worst-case burns do budget:

```text
baseline_lsn = P = Q = C = T = 0
DatabaseIdentity = V2(C=0, fence_generation=1, feature_bits=0x7)
heap = format V2, identity_generation=1, T=0, F=FIRST_RECORD_ID,
       identity entry única e zero extent real
catalog = empty canonical catalog generation 1
commit.state = canonical raw state P=0, Q=0, sem generation implícita
checkpoint.index.0 = CheckpointIndexStateV1(C=0, fence_generation=1, Q=0,
                                            BOUND_V2, empty manifest, H_0)
checkpoint.index.1 = ABSENT
WAL retained inventory = empty canonical suffix; primeiro append criará segment/header WAL V2
index file/header inventory = empty
carriers = WAL_COORDINATION generation 1 + INDEX_NAMESPACE_CATALOG generation 1
```

`H_0` usa a fórmula da seção 5.2 e os bytes exatos acima. O
`baseline_commit_state_sha256`, baseline catalog/heap/index hashes, image manifest e carrier manifest do marker
precisam casar com esses targets. Nenhum clock, callback, user object ou path enumeration participa do
encoding.

Ordem de genesis, toda por `bootstrap_create_entry_durable`/`_publish_control_state_durable` com os
permits concrete correspondentes:

1. publicar slot 0 `V7_GENESIS/PREPARING`, generation 1, antes de meta/heap/catalog/WAL/control/
   carrier;
2. criar os dois carriers estáveis e seus slots iniciais, depois `grafx.meta`, heap, catálogo,
   diretório WAL vazio e os control states exatamente na ordem do image manifest; cada file data
   barrier e parent-directory barrier precede o próximo artefato referenciador;
3. cold direct certificar todos os paths/file identities/bytes/CRCs/hashes e ausência declarada do
   segundo checkpoint slot; publicar `PREPARED`, generation 2;
4. abrir um contexto interno frio, derivar policy WAL V2/C=0, provas P=Q=0 e certificar cruzadamente
   meta/heap/catalog/commit/checkpoint/carriers; publicar `FENCED`, generation 3;
5. fechar todos os handles/pools, reabrir novamente frio, repetir inventário/proofs e publicar
   `BOUND`, generation 4. Só então expor Database ou permitir primeiro append.

Cada passo roda o capacity guard antes de sua primeira generation increment. Create/resume é
idempotente por `(database_uuid,image_manifest_sha256,carrier_manifest_sha256,
generation_capacity_plan_sha256)`.
Artefato ausente na phase PREPARING é criado; artefato existente só é aceito se path, file identity
quando já registrado e bytes forem exatamente os targets. Mismatch nunca é truncado/substituído.
`PREPARED` exige inventário integral; `FENCED` exige todas as autoridades V2; `BOUND` exige o segundo
cold reopen. Um gate mata após cada byte range/write-all/barrier/direct-cert ao publicar PREPARED,
FENCED e BOUND; target torn sempre seleciona o old current self-contained e retoma somente o successor,
enquanto target íntegro só domina com predecessor hash correto. Um único slot torn usa o outro válido; dois slots inválidos com artefatos presentes são
INCONCLUSIVE e exigem restore/operator decision.

Marker genesis válido em PREPARING/PREPARED/FENCED classifica `V7_GENESIS_INCOMPLETE`. Normal open,
read-only e writer recusam `GrafxUnsupportedOperation(field="v7_genesis_incomplete")`; somente
`resume_genesis(GenesisCreationSession)` ou restore/remoção operator-explicit do database inteiro pode
prosseguir. Não se converte genesis parcial em legado. `ABSENT_HEAP` só é aceitável quando uma proof
PREPARING genesis bit-identical declara que heap ainda não foi criado. O protocolo fica implementado/
habilitado junto do heap V2 no Slice 6; slices anteriores apenas fornecem codecs/tombstones e nunca
criam um genesis V7 pela metade.

Open classifica sempre nesta ordem: path vazio/artefatos, slots bootstrap por direct read, phase/kind,
e somente então meta/commit/checkpoint/heap/WAL. Portanto meta V2 visível durante genesis PREPARING não
é confundida com database BOUND nem com corrupção aleatória; sem marker válido, porém, ela permanece
orphaned/inconclusive e não autoriza resume automático.

### 3.2 Header heap v2

O layout atual `<8sHHIIQ>` é preservado. Para heap v2:

```text
format_version = 2
root_page      = identity_generation = 1
payload_length = format_transition_lsn = T
```

`T == 0` significa banco nascido v2. `T > 0` é exatamente o LSN do COMMIT que realizou o upgrade.
Depois de publicado, `T` é imutável. `identity_generation` começa em `1`; zero ou mudança posterior
é corrupção. `root_page` não é interpretado como raiz de estrutura no heap v2.

Heap v2 exige DatabaseIdentity/WAL fence v2. Genesis tem `C == T == 0`; upgrade legado exige
`0 < C < T`, checkpoint/baseline pós-C e records do transition em WAL format v2. Heap v2 com meta v1,
`T <= C` no upgrade ou transition v1 são corrupção/refusal, nunca compatibilidade implícita.

### 3.3 Entrada de identidade

O primeiro slot do diretório v2 é reservado e possui layout físico de `TableExtent`, mas codec
próprio:

```text
table_id       = IDENTITY_DIRECTORY_TABLE_ID = 0xFFFFFFFE
first_page     = NO_PAGE
last_page      = NO_PAGE
page_count     = 0
next_record_id = F
```

`IDENTITY_DIRECTORY_TABLE_ID` e `IDENTITY_PARTITION_TABLE_ID` são aliases semânticos do mesmo
valor `0xFFFFFFFE`: o primeiro nome só aparece no codec do diretório; o segundo só no high word da
partição OCC. O tipo/porta impede misturá-los acidentalmente.

Há exatamente uma entrada. `F` está em `[FIRST_RECORD_ID, MAX_RECORD_ID + 1]`; o extremo superior
é sentinela de exaustão, não ID entregável. Nenhum `TableExtent.decode` é autorizado a receber esse
slot. A capacidade de tabelas materializadas em v2 é `max_tables_v1 - 1`; isso deve ser exposto em
status e validado antes de upgrade.

Todo `TableExtent` real materializado em heap v2 mantém `next_record_id == F` como espelho
redundante. Somente `RANGE_RESERVATION` clona page0 e eleva, na mesma imagem, a identity entry e
todos os extents reais. Consumer não avança F; ao materializar uma tabela ABSENT, cria seu primeiro
extent já espelhando o F corrente certificado pela reservation anterior. Qualquer drift entre
identity entry e extent real é `CORRUPT` ou redo pendente
demonstrado; nunca se escolhe silenciosamente o maior. Essa varredura explica o custo CPU O(slots)
mesmo com uma única page read/write.

O SHA-base calcula a capacidade em `engine/heap_store.py:363-368`. Upgrade recusa um v1 cuja
ocupação não cabe na capacidade v2, sem compactação implícita.

### 3.4 L1 — discriminação antes do decoder

Os callsites reais que iteram entradas precisam passar por um único helper
`iter_heap_directory_v2()` que lê primeiro o `table_id` bruto e só então escolhe codec:

1. `engine/heap_store.py:1161-1171`, `HeapStore._find_extent`; o decode atual está em `:1168`.
2. `engine/heap_store.py:1207-1222`, `HeapStore._write_extent`; o decode atual está em `:1214`.
3. `engine/verifier.py:_verify_record_id_counters` do oráculo `40a4201`, decode em `:635`.
4. Qualquer novo iterator de recovery, upgrade, status, checkpoint, verifier ou planner deve usar o
   mesmo helper e não copiar o loop.

No v1 o helper preserva a sequência antiga literalmente. No v2, ao ver `0xFFFFFFFE`, chama apenas
`IdentityEntry.decode`; nos demais slots chama `TableExtent.decode`. Um mutante substitui
`TableExtent.decode` por trap quando os primeiros quatro bytes são `0xFFFFFFFE`: bootstrap,
reopen, status, verify, recovery, checkpoint, reservation, consumer e upgrade v2 devem manter
contador do trap em zero.

Os dois callsites produtivos do SHA 539 são alcançados por `allocate_record_id`
(`heap_store.py:538,560`), `observe_record_id` (`:580,592`), `next_record_id` (`:607,609`),
`insert`/`update`/`_store_version` (`:612-747`), `scan`/`scan_all`/`lookup`/`_walk`
(`:766-793,:1447-1453`), `extent_of` (`:829-839`), `pages_of` (`:841-857`) e `_extent_for`
(`:1105-1125`). A proteção é central no iterator, não repetida em cada ancestral.

Defesa adicional recusa IDs reservados em `_initialize_data_page` (`heap_store.py:1098-1103`) e no
descriptor `_page_table_id` (`:1263+`). `TableDef` (`domain/model/schema.py:197-209`),
`IndexDefinition` (`domain/index/definition.py:131-142`) e `Catalog.next_table_id`
(`domain/model/catalog.py:140-142`) continuam decodificando legado v1, mas mutação/DDL v2 via
`query_engine.py:962-985` aplica o ceiling format-aware antes de reservar ID.

`_find_extent` recusa pedido cujo table ID seja reservado antes de pin/scan; `_write_extent` aceita
somente extent real e uma porta `_write_identity` separada altera o slot de identidade. Os callsites
test-only existentes são `tests/recovery/test_verifier.py:322,360` e
`tests/storage_core/test_heap_store.py:1405,1411,1413,2916,3086,3101`; fixtures v1 continuam usando
o decoder real, e fixtures v2 usam o dispatcher para que o trap também cubra a suíte.

### 3.5 Checksum e fingerprint

A página conserva o CRC já existente. Para idempotência, usa-se ainda fingerprint canônico
SHA-256 sobre os bytes semânticos decodificados, incluindo `page_lsn`, header, slots e payload, mas
excluindo CRC físico e sequence/write-back stamp. O control record carrega o fingerprint da imagem
exata de `heap:0` que autoriza.

### 3.6 Estados classificados

O estado persistido é uma destas alternativas fechadas:

- `ABSENT_HEAP`: arquivo inexistente/zero bytes somente em `V7_GENESIS/PREPARING` válido cujo image
  manifest declara heap ainda não criado, ou no namespace integralmente vazio anterior ao primeiro
  marker; nunca por decode failure;
- `LEGACY_V1`: header e diretório v1 válidos;
- `CLEAN_V1`: v1 completamente certificado pelo verifier aceito;
- `REPAIRABLE_V1_COUNTER`: única divergência é contador abaixo do maior ID físico, com inventário
  completo e ownership inequívoco;
- `CLEAN_V2`: header v2 válido, exatamente uma identity entry, extents válidos, `F` suficiente e
  ownership físico completo;
- `CORRUPT`: bytes presentes indecodificáveis, versão/kind impossível, checksum, duplicata,
  cross-owner, orphan, CSN/payload ambíguo ou transição contraditória;
- `INCONCLUSIVE`: I/O, permission, inventário incompleto ou evidência retida insuficiente.

Erro de decode jamais vira `ABSENT_HEAP`. `INCONCLUSIVE` e `CORRUPT` recusam qualquer write,
repair ou upgrade.

## 4. Namespaces OCC e capability privada

```text
PAGE_PARTITION_TABLE_ID     = 0xFFFFFFFF
IDENTITY_PARTITION_TABLE_ID = 0xFFFFFFFE
MAX_REAL_TABLE_ID           = 0xFFFFFFFD
```

`identity_partition()` representa mudança de `F`; `identity_format_partition()` representa
mudança de formato/transição. Catálogo v2 recusa IDs acima de `MAX_REAL_TABLE_ID`. Novo DDL v1 sob
binário V7 aplica o mesmo ceiling preventivo. v1 que já use qualquer ID reservado permanece legível
em `compatible`, mas DML/DDL sobre a tabela reservada e upgrade são recusados typed e pre-mutation.

O mapping numérico é fechado:

```text
identity_partition()        = partition_key(0xFFFFFFFE, 0)
identity_format_partition() = partition_key(0xFFFFFFFE, 1)
page_partition(heap, 0)     = regra CRC-32C já existente em partitions.py:104-125
```

`FORMAT_TRANSITION` declara format + page0; `RANGE_RESERVATION` declara counter + page0;
`CONSUMER` declara somente seus interesses reais e qualquer page partition efetivamente alterada.

No SHA-base, `domain/txn/partitions.py:83-88` aceita qualquer table id e usa
`MAX_TABLE_ID` como namespace de página. `domain/txn/context.py:283-302` expõe sets públicos
mutáveis via `note_read(s)`/`note_write(s)`. Portanto:

- as portas públicas recusam os dois namespaces reservados; para tabela v1 legada FE/FF a recusa é
  `GrafxUnsupportedOperation(field="legacy_reserved_table_id")`, não corrupção;
- commit valida novamente os sets crus, pois um caller pode mutá-los diretamente;
- apenas `_IdentityOccPermit` pode declarar interesses identity.

Mutantes obrigatórios: remover o fence antes de `stage_row_insert`, permitir `next_table_id == FE`,
tratar FE/FF legado como corrupção, ou deixar uma row/header/page dirty antes da recusa. Todos devem
morrer; leitura/scan/verify de fixture v1 FE/FF permanece byte-identical e upgrade sempre recusa.

O inventário de superfícies inclui ainda `domain/wal/commit.py:50,93-103`
(`MAX_TABLE_ID`/`partition_key`), `engine/txn_manager.py:511-513`
(`TransactionManager.partition_of`), `engine/public_views.py:452-470`,
`domain/txn/context.py:210-211,647-661` (sets crus e `_require_partition`) e
`CommitPayload.__post_init__` em `domain/wal/commit.py:128-150`. Decode de WAL continua aceitando o
namespace reservado; autorização ocorre antes do append, e recovery valida a gramática agrupada.

`_IdentityOccPermit` adapta o padrão real `_RecoveryPermit` de
`engine/recovery_manager.py:284-338`: seal module-private, tipo exato, binding ao manager, txn,
attempt, PID e generation, estado live somente dentro de COMMIT e revogação irrevogável na saída.
Não é token estrutural, callback ou objeto exportado; API/public views nunca o retêm.
O padrão de callback bound de `_page_staging_capability`
(`engine/txn_manager.py:361-364,515-534` e `domain/txn/context.py:365-418`) pode ser reutilizado para
que o allocator receba uma operação autenticada, nunca o permit bruto.

Uma reserva posterior ao snapshot pode ser incorporada por `RebasedPage0Proof`, que contém
fingerprint direto de `heap:0`, `F`, `T`, generation, intervalo de committed history observado e
stops incorporados. A exceção OCC é permitida somente quando:

- cada transação sobreposta é um batch agrupado `RANGE_RESERVATION` válido;
- a interseção é subconjunto exato de `{identity_partition(), page_partition(heap, 0)}`;
- o proof incorpora em ordem todos os stops e a imagem atual;
- não houve transition, consumer, schema, user page ou page0 genérica disfarçada.

O primeiro OCC acontece antes do planejamento; o segundo, depois que imagens e interesses reais
foram descobertos. Ambos permanecem necessários. O predicado atual em
`engine/txn_manager.py:1726-1757` só intersecta sets de COMMITs achatados e não é prova suficiente.

## 5. WAL exato e transações agrupadas

### 5.1 `WalAppendPlan`

O caminho atual não pode ser embrulhado apenas para leasing. No SHA-base:

- `engine/wal_manager.py:812-825`, `planned_terminal_lsn`, faz preview;
- `WalManager.append_many` em `:827-949` revalida apenas o terminal esperado;
- `_plan_batch` em `:951-986` deriva o terminal de `last_lsn + cardinalidade`;
- o append ainda consulta clock e decide header em `:874-881`;
- `engine/txn_manager.py:1477-1497` faz preview, retarget e depois append;
- `_build_records` em `:1789-1892` prediz LSNs;
- `_retarget_commit_batch` em `:1894-1973` muta staging privado antes do append.

V7 substitui isso, primeiro no caminho genérico, pelo protocolo `WalAppendPlan`. Seu artefato encoded
prewrite é um `WalAppendDraft` frozen e single-use criado sob COMMIT; o permit de mutação não faz parte
de seus bytes/fingerprint. Antes de entrar em participant, **cada grouped WAL transaction substep**
captura exatamente uma vez um
`WalEntropySeed(clock_stamp, transaction_uuid, candidate_txn_id, attempt_number, attempt_sha256,
plan_ordinal, outer_operation_identity, logical_substep_base_sha256)` detached. `candidate_txn_id` é
somente a escolha transaction-local ainda sem autoridade; epoch/currentness são certificados depois.
Esse é o único passo que chama clock, CSPRNG ou outro provider. Um mesmo `OperationAttempt` pode
possuir vários entropy seeds, um por `WalTransactionPlan`, nunca um UUID compartilhado pelo outer.
Retry só captura outro entropy seed depois de liberar todas as sections. “Outro seed” não significa
sempre outro UUID:

Neste documento, a forma abreviada “congelar/validar um `WalAppendPlan`” significa congelar/validar o
`WalAppendDraft` e depois executar o handshake separado; não existe objeto alternativo que embuta o
permit. A única porta de bytes é
`append_planned(wal_mutation_draft, append_draft, permit)`.

- `context_tag=TRANSACTION` reutiliza obrigatoriamente o `transaction_uuid` imutável criado junto da
  Transaction; retries mudam clock/attempt context, nunca a identity persistida usada por CREATE;
- `context_tag=NO_TRANSACTION` recebe um `plan_ordinal u32` canônico de seu
  `FrozenOuterOperationPlan` e, para **cada** grouped transaction plan, captura CSPRNG UUID nonzero novo fora de sections; ele
  permanece igual no BEGIN/COMMIT daquele único plan. N BINDs e MARK→rebuild→CLEAR no mesmo attempt
  possuem N e 3 UUIDs distintos, respectivamente;
- depois do primeiro WAL byte não existe retry normal/seed novo: append incerto entra em recovery; se
  a barrier foi alcançada, recovery decodifica UUID/clock/bytes do grouped transaction committed e
  reaplica esse mesmo plan lógico idempotentemente.

`plan_ordinal` **não** é posição dinâmica no attempt. `FrozenOuterOperationPlan` contém
`(outer_operation_identity[32], full_plan_sha256, ordered_substeps[(ordinal u32, kind,
substep_base_sha256)])`; `full_plan_sha256` cobre somente esses base SHAs, jamais um bound context
que o contenha. Ordinals são únicos pela chave `(outer_operation_identity, ordinal)`,
atribuídos sobre o plano lógico **completo** e nunca renumerados quando predecessores já committed são
skipped. O BIND plan usa
`outer_operation_identity = SHA256(DOMAIN_BIND_OUTER_V1 || baseline_cutover_identity ||
cutover_core_digest)` e o `full_plan_sha` derivados exatamente na ordem da seção 3.1.1; ordinals são os
índices da lista integral de `BindSubstepBaseV1` no core, não do envelope/marker.
MARK/REBUILD/CLEAR usam ordinals fixos 0/1/2. Antes de sections e antes de codificar o manifest, o
outer captura `rebuild_operation_nonce[32]` CSPRNG nonzero e congela
`rebuild_base_plan_digest = SHA256(DOMAIN_REBUILD_BASE_V1 || substep_count_u32 ||
ordered(ordinal_u32,substep_kind_u8,base_length_u32,substep_base_sha256))`, onde cada base exclui
nonce, outer identity, full-plan e qualquer hash que o
contenha. Então
`outer_operation_identity = SHA256(DOMAIN_REBUILD_OUTER_V1 || database_uuid || persisted_index_identity ||
rebuild_operation_nonce || rebuild_base_plan_digest)` e
`full_plan_sha256 = SHA256(DOMAIN_REBUILD_PLAN_V1 || outer_operation_identity ||
rebuild_base_plan_digest)`. A tuple `(database_uuid,index identity,outer identity)` é consultada em
pending/history retida antes do MARK; collision não bit-identical recusa com zero WAL byte. Retry
pre-WAL da mesma operação conserva essa identity; resume a obtém do MARK/manifest committed, sem
capturar novo nonce. Assim a identity é conhecida antes de hashear o manifest e nenhum input contém
seu output. Outros workflows multi-transaction precisam declarar tabela fechada equivalente;
operação unitária usa ordinal 0.

Antes do primeiro WAL byte, uma chamada sem efeito anterior pode descartar o plano inteiro. Depois do
primeiro substep committed, grouped history/marker autentica operation identity, full-plan SHA e
ordinal; nova chamada/novo `OperationBudget` de resume reconstrói o mesmo
`FrozenOuterOperationPlan`, marca ordinals committed como skipped e começa no próximo ordinal original.
Retry pre-WAL de `NO_TRANSACTION` pode trocar UUID/attempt; retry de `TRANSACTION` troca seed/clock/
attempt mas preserva o transaction UUID imutável. Nenhum troca operation identity/full-plan/ordinal/substep base ou
plan-source lógico. Para BIND, o stable context também é bit-identical. Para INDEX_REBUILD, uma recipe
pode autorizar commit estrangeiro sem efeito sobre a identity entre attempts; nesse caso o
`BoundOperationSubstepContextV1` actual é recomputado da source/horizon certificada e pode mudar antes
do byte zero, sem alterar aqueles campos lógicos. Depois de qualquer WAL byte, somente o context/raw
blob efetivamente escrito é válido. Missing, duplicate, regressão/renumeração ou substep fora da
tabela é MALFORMED/INCONCLUSIVE pre-append.

Depois de participant → fresh lease → guards → COMMIT, e sem provider call, o build combina o
`WalEntropySeed` com epoch/txn pair validado, tail/current generations, permits pre-draft, source/
horizon certificada e SHAs logical-stable/actual-source/fencing/operation para formar um
`WalPlanSeedFinal`. Ele exige que
attempt number/SHA casem com o `OperationAttempt` corrente, que ordinal/context casem com o substep
exato e que o UUID não apareça em outro plan live ou história necessária. Cleanup/saída de qualquer
section invalida o final seed; retry recomeça pelo entropy seed somente depois de soltar todas as
sections. Somente retry `NO_TRANSACTION` comprovadamente pre-WAL do mesmo substep pode capturar outro
UUID; TRANSACTION/CREATE conserva o UUID/`PersistedIndexIdentity`. Seed/
UUID de plan que escreveu qualquer WAL byte jamais é reutilizado/regerado pela porta normal. O build
nunca chama clock/provider. O plan
contém:

- identidade do `WalManager`, PID/generation e nonce de uso;
- fresh writer lease identity/epoch e o `(epoch,txn_id)` corrente do grouped plan; todos os records
  entre BEGIN e COMMIT repetem exatamente esse par, validado contra manager/history antes de encode;
  eventual SEGMENT_HEADER é framing anterior com txn_id zero e validator separado;
- prova direta do `WalFeatureFence` (`identity_format`, C, generation, feature bits) e a versão WAL
  exata de cada record; plano comum não pode cruzar C;
- prova do tail: epoch, `last_lsn`, `max_epoch`, segmento/número/tamanho atuais e próximo número;
- decisão de roll e eventual `SEGMENT_HEADER`;
- clock stamp já capturado no `WalEntropySeed`, sem provider retido;
- sequência completa de records tipados com todos os LSNs;
- imagens já retargeted para o terminal exato;
- bytes codificados, CRCs, offsets, placements e terminal COMMIT;
- fingerprint e comprimento do blob total;
- `wal_predecessor_mutation_generation=g` e
  `wal_planned_successor_mutation_generation=checked_increment_u64(g)`, recusando MAX antes de efeito.

Esses objetos usam os codecs integrais seguintes; nenhum tuple/dataclass alternativo participa de
fingerprint, retry ou recovery:

```text
IndexRebuildSubstepBaseV1 = (
  magic=MAGIC_INDEX_REBUILD_BASE_V1, version u16=1, workflow_kind u8=INDEX_REBUILD,
  substep_kind u8, record_length u32, database_uuid[16], persisted_index_identity[32],
  plan_ordinal u32, source_horizon_policy u8, row_visibility_policy u8,
  target_codec_id u16, plan_source_ordinal u32,
  stable_source_predicate_sha256[32], semantic_target_effect_manifest_sha256[32],
  semantic_apply_plan_sha256[32], target_count u32, apply_step_count u32,
  targets[target_count], apply_steps[apply_step_count], crc32c u32
)
semantic_target := (
  target_ordinal u32, target_kind u8, reserved_zero[3], owner_identity[32],
  semantic_key_sha256[32], semantic_postimage_sha256[32]
) # 104 bytes
semantic_apply_step := (
  step_ordinal u32, apply_kind u8, barrier_flags u8, reserved_zero u16,
  target_ordinal u32, semantic_input_sha256[32], semantic_output_sha256[32]
) # 76 bytes
index_rebuild_substep_base_sha256 =
  SHA256(DOMAIN_INDEX_REBUILD_SUBSTEP_BASE_RECORD_V1 || canonical base bytes)
```

`source_horizon_policy` é `INVALID=0`, `EXACT_P=1`, `ABSORB_COMPATIBLE_ROWS_THROUGH_P=2`;
`row_visibility_policy` é `INVALID=0`, `BLOCK_ROWS=1`, `ABSORB_BY_RECIPE=2`; outros recusam. Target/
apply kinds usam os enums exactos de `DeviceApplyTargetSetAuthorityV1`; counts são 0..262144, ordinals
densos, apply target precisa existir e os três semantic SHAs são recomputados dos vectors. MARK base
declara recipe/manifest; REBUILD/CLEAR bases referenciam `plan_source_ordinal=0` e não carregam raw
future WAL/page/LSN/epoch. Backend sem derivação semântica bijetiva recusa antes de MARK.

```text
FrozenOuterOperationPlanV1 = (
  magic=MAGIC_FROZEN_OUTER_PLAN_V1, version u16=1, substep_mode u8, workflow_kind u8,
  record_length u32, database_uuid[16], outer_operation_identity[32],
  full_plan_sha256[32], base_plan_digest[32], operation_nonce[32],
  substep_count u32, entries_length u32, substeps[substep_count], crc32c u32
)
frozen_substep_entry := (
  entry_length u32, plan_ordinal u32, substep_kind u8, base_codec_kind u8,
  reserved_zero u16, base_length u32, substep_base_sha256[32],
  base_bytes[base_length]
)
```

`SubstepBaseCodecKindV1` é `INVALID=0`, `BIND_SUBSTEP_BASE=1`,
`INDEX_REBUILD_SUBSTEP_BASE=2`; 3..255 recusam. LEGACY_BIND exige codec 1/
`BindSubstepBaseV1`; INDEX_REBUILD exige codec 2/record acima; UNITARY exige count/length/digests/
nonce zero e nenhuma entry. Entries são sorted/densas por ordinal e obedecem à matriz workflow×ordinal
da seção 5.2; base SHA é recalculado pelo domain de record nativo selecionado pelo codec: kind 1 usa
`DOMAIN_BIND_SUBSTEP_BASE_RECORD_V1` e kind 2 usa
`DOMAIN_INDEX_REBUILD_SUBSTEP_BASE_RECORD_V1`. Para BIND, operation nonce é o
cutover_operation_nonce; para rebuild é o rebuild_operation_nonce; ambos nonzero. `base_plan_digest`,
outer identity e full plan são recomputados na ordem acíclica já definida, não usados para hashear
base bytes. O record SHA é `SHA256(DOMAIN_FROZEN_OUTER_PLAN_V1||canonical bytes)`; pending/history collision compara a
preimage integral, nunca apenas outer identity.

```text
WalEntropySeedV1 = (
  magic=MAGIC_WAL_ENTROPY_SEED_V1, version u16=1, context_tag u8, workflow_kind u8,
  record_length u32, database_uuid[16], outer_operation_identity[32],
  logical_substep_base_sha256[32], operation_attempt_sha256[32],
  outer_attempt u32, plan_ordinal u32, candidate_txn_id u64, clock_stamp u64,
  transaction_uuid[16], entropy_nonce[32], crc32c u32
)
wal_entropy_seed_sha256 = SHA256(DOMAIN_WAL_ENTROPY_SEED_V1 || canonical seed bytes)

AttemptFencingContextV1 = (
  magic=MAGIC_ATTEMPT_FENCING_CONTEXT_V1, version u16=1, context_tag u8, operation_kind u8,
  record_length u32, outer_operation_code u16, reserved_zero u16,
  database_uuid[16], process_birth_identity[16], database_runtime_generation u64,
  coordinator_incarnation_identity[32], process_registry_generation u64,
  system_floor_generation u64, operation_budget_sha256[32], operation_attempt_sha256[32],
  outer_attempt u32, plan_ordinal u32, wal_feature_fence_generation u64,
  index_namespace_generation u64, coordination_namespace_generation u64,
  wal_predecessor_mutation_generation u64, wal_planned_successor_mutation_generation u64,
  device_replay_generation u64, writer_lease_epoch u64,
  transaction_epoch u64, transaction_txn_id u64, segment_header_epoch u64,
  clock_stamp u64, transaction_uuid[16], ticket_nonce[32], lease_nonce[32],
  index_view_guard_nonce[32], commit_section_nonce[32],
  index_entry_count u32, capability_entry_count u32,
  index_entries[index_entry_count], capabilities[capability_entry_count], crc32c u32
)
fencing_index_entry := (
  persisted_index_identity[32], carrier_identity_generation u64,
  predecessor_mutation_generation u64, planned_successor_mutation_generation u64
) # 56 bytes
fencing_capability_entry := (
  capability_kind u8, reserved_zero[3], logical_ordinal u32,
  authority_identity[32], authority_generation u64, authority_nonce[32]
) # 80 bytes
attempt_fencing_context_sha256 = SHA256(DOMAIN_ATTEMPT_FENCING_CONTEXT_V1 || canonical context bytes)
```

`FencingCapabilityKindV1` é `INVALID=0`, `INDEX_MUTATION_PERMIT=1`,
`INDEX_NAMESPACE_PERMIT=2`, `CONTROL_PUBLICATION_PERMIT=3`, `COORDINATION_ADMISSION=4`;
5..255 recusam. `_WalMutationPermit` e `_IrrevocableWalPermit` são proibidos. Index entries são
sorted/unique por identity; capability entries por `(kind,logical_ordinal,authority_identity)` e cada
kind precisa corresponder à rota/operation kind. ZERO/nonzero segue a matriz de WriteSnapshotProof:
TRANSACTION exige outer identity ZERO32 somente se unitária, UUID bit-identical ao UUID imutável e
ticket nonce ZERO; NO_TRANSACTION exige outer identity/base/ticket conforme kind. WAL successor é
checked predecessor+1; cada index successor idem. Segment header fields são zeros se não há roll e,
se há, `transaction_txn_id` continua separado/nonzero enquanto header usa txn zero. Clock/UUID precisam
casar o entropy seed. Não há CRC, fingerprint, operation-context SHA ou proof nonce neste input.

```text
WalPlanSeedFinalV1 = (
  magic=MAGIC_WAL_PLAN_SEED_FINAL_V1, version u16=1, context_tag u8, workflow_kind u8,
  record_length u32, database_uuid[16], entropy_seed_length u32,
  fencing_context_length u32, entropy_seed_sha256[32], fencing_context_sha256[32],
  logical_stable_operation_context_sha256[32], actual_source_proof_sha256[32],
  operation_context_sha256[32], proof_nonce_sha256[32],
  wal_predecessor_mutation_generation u64, wal_planned_successor_mutation_generation u64,
  entropy_seed_bytes[entropy_seed_length], fencing_context_bytes[fencing_context_length], crc32c u32
)
wal_plan_seed_final_sha256 = SHA256(DOMAIN_WAL_PLAN_SEED_FINAL_V1 || canonical final-seed bytes)
```

Nested records são exatamente `WalEntropySeedV1`/`AttemptFencingContextV1`; duplicated fields/SHA
casam. `operation_context_sha256 = SHA256(DOMAIN_OPERATION_CONTEXT_V1 || logical stable SHA || actual
source SHA || fencing SHA)` e `proof_nonce_sha256` segue a única fórmula integral da seção 5.4; zero
rules vêm do
WriteSnapshotProof. Final seed nasce sob COMMIT somente combinando entropy capturada e state current;
nenhum provider/callback é invocado.

```text
WalAppendDraftV1 = (
  magic=MAGIC_WAL_APPEND_DRAFT_V1, version u16=1, context_tag u8, workflow_kind u8,
  record_length u32, database_uuid[16], wal_manager_identity[32],
  wal_plan_seed_final_sha256[32], commit_section_nonce[32],
  wal_predecessor_mutation_generation u64, wal_planned_successor_mutation_generation u64,
  first_lsn u64, terminal_commit_lsn u64, record_count u32, segment_count u32,
  blob_length u32, records_length u32, segments_length u32,
  terminal_record_raw_sha256[32], blob_sha256[32], plan_fingerprint[32],
  records[record_count], segments[segment_count], blob_bytes[blob_length], crc32c u32
)
append_record_entry := (
  entry_length u32=116, record_order u32, wal_format_version u8, record_type u8,
  flags u16, lsn u64, segment_number u64, segment_offset u64,
  blob_offset u32, encoded_length u32, payload_length u32, reserved_zero u32,
  encoded_record_sha256[32], operation_context_sha256[32]
)
append_segment_entry := (
  entry_length u32=112, segment_number u64, segment_action u8, reserved_zero[3],
  source_file_identity[32], source_length u64, append_offset u64, append_length u64,
  blob_offset u32, reserved_zero u32, append_bytes_sha256[32]
)
```

`WalAppendSegmentActionV1` é `INVALID=0`, `APPEND_EXISTING=1`, `CREATE_AND_APPEND=2`;
3..255 recusam. Records são dense por order/LSN e segmentos sorted/unique; ranges são contíguos e
particionam o blob sem gap/overlap. Existing exige source identity nonzero/append offset==source
length; CREATE exige ZERO32/source length+offset zero. Record/segment/blob lengths e SHAs são
recomputados. `plan_fingerprint = SHA256(DOMAIN_WAL_APPEND_DRAFT_V1 || canonical record com o próprio
plan_fingerprint=ZERO32 e sem crc32c)`; blob SHA/record CRCs já estão fixos antes dessa fórmula. Decode
reconstrói essa preimage, exige fingerprint igual e depois o CRC externo. Nenhum permit/nonce criado
pós-draft entra nos bytes. Bounds são `record_count` 1..1048576, `segment_count` 1..65535,
blob 1..MAX_TOTAL_LENGTH e transcript <=67 MiB. Além do count local, freeze calcula sobre a projection
post-append o número de segmentos canônicos entre Q e o physical tail: ele precisa ser <=65535, o
mesmo limite do `ProtectedWalRangeSetV1` (uma range por segmento) e dos codecs tail. Um append que
criaria o segmento 65536 desse horizonte recusa **antes** do generation CAS/primeiro byte e só pode
ser tentado depois de checkpoint/recycle que reduza o horizonte; não é permitido produzir WAL que a
reabertura não consiga pin/scan. Golden vectors cobrem exact 65535, 65536 com zero efeito, roll, N
grouped plans no mesmo attempt, TRANSACTION retry UUID igual,
NO_TRANSACTION pre-WAL UUID novo, stale g/g+1, bool/overflow/CRC/fingerprint/body swap.

O append draft só vira appendable por este handshake sob o mesmo COMMIT: embrulhá-lo num
`WalMutationDraftV1(APPEND)` com source/pin/boundary/barrier state exact; revalidar **todos** os campos
desse draft, tail/meta/C e `current_generation==g`; CAS/publicar/certificar o slot `g→g+1`; obter
`_WalMutationPermit` single-use ligado a manager/PID/COMMIT nonce/mutation-draft fingerprint/kind/
exact ranges/g/g+1; então chamar `append_planned(mutation_draft, append_draft, permit)`, que exige
match dos dois fingerprints, permit vivo e
`current_generation==planned_successor_generation`. Divergência antes do CAS produz conflito retryable
`wal_plan_stale` sem write; derrota no CAS também deixa zero byte. Falha após CAS e antes do byte zero
deixa só o incremento extra seguro, e retry replana sobre o novo predecessor. Depois da
validação, não consulta clock, não aloca LSN, não reencoda, não retargeta e não materializa; escreve
o blob exato. Falha que não restaura byte a byte o preimage aciona uncertainty latch e recovery.
Uso repetido do nonce é recusado pelo manager; o dataclass frozen não carrega flag mutável.

Essa é a única transição de currentness deliberada da proof: antes do CAS ela exige current `g`; depois,
o exact permit prova que **essa mesma proof/draft** publicou `g+1`, e a validação pós-CAS exige `g+1`.
Uma checagem genérica que continue exigindo `current==g` após acquire é mutante inválido; qualquer
generation diferente de `g+1`, ou qualquer outro campo tail/fence alterado, revoga tudo sem append.

Construção implementável, sem ponto fixo implícito:

1. canonicalizar templates com cardinalidade e comprimentos invariantes; LSN/T/page_lsn são u64 de
   largura fixa;
2. sob a tail proof, decidir roll e presença do segment header e atribuir todos os LSNs;
3. bindar o terminal exato nas imagens, no `CommitPayload` e, para transition, no campo T;
4. codificar uma única vez e verificar que cada comprimento coincide com o template;
5. calcular CRC/fingerprint/offset final e congelar. Qualquer diferença de comprimento é erro de
   planejamento prewrite, não uma segunda tentativa mutável.

O plan contém somente built-ins exatos, `bytes` e `tuple`s detached; nenhum provider, callback,
subclasse hostil, frame, store ou capability é retido.

O transition LSN é, portanto, o LSN exato do COMMIT que já existe no plano imutável. É proibido
estimar `last_lsn + n`, usar header LSN ou descobri-lo depois do append.

### 5.2 Histórico cometido

`domain/recovery/decision.py:57-72` hoje possui `CommittedReplay` achatado; `committed_replay`
(`:183-234`) agrupa internamente por `(epoch, txn_id)`, mas devolve apenas efeitos ordenados e perde
o terminal e `CommitPayload`. Isso não permite reconhecer control batches com segurança.

Introduzir:

```text
CommittedTransaction(
  epoch, txn_id,
  records_in_wal_order,
  effects_in_wal_order,
  terminal_commit,
  decoded_commit_payload,
  commit_lsn
)
CommittedHistory(transactions, incomplete_transactions, scan_evidence)
```

O decoder exige um terminal, nenhum record pós-terminal, epoch/txn consistentes e payload/touches/
partitions coerentes. Segment header pertence ao WAL, não à transação. A visão achatada existe
somente como projeção derivada para consumidores legados.

`decoded_commit_payload` preserva também os raw bytes do `OperationSubstepTrailerV1`, de seu bound
context e do eventual `OperationSubstepManifestV1`, junto do UUID/LSN/segment locator certificado;
não devolve somente hashes ou objetos reserializados. `CommittedHistory` inclui esses bytes no root e
no relevant horizon até o terminal/floor permitir recycle. Recovery/verifier sempre redecodificam a
preimage WAL e comparam o encoding canônico; cache de objeto ou digest isolado não é autoridade.

Todo commit novo **estritamente após** o cutover durável da seção 5.3 usa record WAL format v2 e
`CommitPayloadV2`, selecionado por `COMMIT_V2_FLAG_MANIFEST`. Nenhum COMMIT format v1 recebe essa
interpretação. O payload preserva o prefixo lógico V1 e acrescenta um trailer canônico autenticado
pelo CRC do COMMIT:

`PersistedIndexIdentity` é um valor de 32 bytes reconstruível sem path mutável. Para uma identity
legada presente no primeiro checkpoint V7, ele é
`SHA256(DOMAIN_INDEX_LEGACY_V1 || database_uuid || canonical_IndexDefinition_bytes)`; unicidade do catálogo
torna esse baseline bijetivo. Cada Transaction V2 recebe na construção, fora de sections e antes de
qualquer statement, um `transaction_uuid[16]` CSPRNG nonzero, exact built-in bytes. Ele é persistido
no BEGIN e no `CommitPayloadV2`, integra o `WalEntropySeed`/`WalPlanSeedFinal` de cada plan dessa
Transaction e permanece bit-identical
em todos os attempts; duplicata entre transactions live ou na história retida necessária
recusa. Para CREATE
pós-cutover, a identity é
`SHA256(DOMAIN_INDEX_NEW_V1 || database_uuid || transaction_uuid || create_ordinal_u32 ||
canonical_IndexDefinition_bytes)`. O ordinal canônico é frozen no `StatementPreflight`; duas CREATE
da mesma definição/ordinal na mesma transação são inválidas. Assim identity/path/carrier são conhecidos
antes de participant/lease/epoch/guard, sem depender de clock ou LSN. ALTER preserva a identity; DROP
a tombstoneia; um CREATE posterior, ainda que reuse nome/definição, recebe outro transaction UUID e
outra identity. O `CheckpointIndexManifest` persiste a projeção atual e
o grouped history pós-Q reproduz CREATE/ALTER/DROP; não se adiciona campo incompatível ao IndexHeader
v1. Direct page0 liga essa identity à definição/table do catálogo autenticado e recusa divergência.

V7 não promete um registry eterno de todos os `transaction_uuid` depois do recycle: isso exigiria
outro estado persistido que este layout não possui. A unicidade probabilística do CSPRNG não é usada
como prova de correctness. Collision de uma CREATE é excluída estruturalmente: create-exclusive do
carrier/path da `PersistedIndexIdentity` consulta também carriers tombstoned/órfãos retidos e recusa
qualquer identity já materializada, salvo redo bit-identical da mesma transação ainda provada. DROP
nunca remove seu carrier online; GC offline não pode apagar tombstone necessário a essa detecção sem
publicar uma nova namespace generation e um registry equivalente em emenda futura. Transação sem
CREATE pode repetir UUID já reciclado sem aliasar objeto; epoch/txn/LSN e hash-chain continuam sendo
sua identidade de ordem. Assim nenhum requisito depende de membership set impossível no checkpoint.

```text
CommitPayloadV2 = (
  magic=MAGIC_COMMIT_PAYLOAD_V2, manifest_version u16=1, reserved_zero u16,
  record_length u32, transaction_uuid[16], affected_table_count u32,
  index_mutation_count u32, operation_substep_length u32,
  affected_table_ids[affected_table_count],
  index_mutations[index_mutation_count],
  operation_substep_bytes[operation_substep_length], crc32c u32
)

index_mutation_scope := (
  entry_length u32, index_identity_sha256[32], table_id u32,
  operation_count_entry_count u32, carrier_predecessor_generation u64,
  carrier_successor_generation u64, target_carrier_state u8, reserved_zero[7],
  carrier_plan_sha256[32], before_definition_sha256[32],
  after_definition_sha256[32], effect_sha256[32],
  operation_counts[operation_count_entry_count]
)
operation_count_entry := (operation u8, reserved_zero[3], count u32) # 8 bytes

OperationSubstepTrailerV1 = (
  magic=MAGIC_SUBSTEP_TRAILER_V1, version u16=1, mode u8, workflow_kind u8,
  record_length u32, substep_kind u8, plan_source_kind u8, reserved_zero u16,
  plan_ordinal u32, substep_count u32,
  outer_operation_identity[32], full_plan_sha256[32], substep_base_sha256[32],
  bound_operation_substep_context_sha256[32], plan_source_sha256[32],
  source_transaction_uuid[16], source_commit_lsn u64,
  bound_context_length u32, plan_body_length u32,
  bound_context_body[bound_context_length], plan_body[plan_body_length], crc32c u32
)

LogicalBoundOperationSubstepContextV1 = (
  magic=MAGIC_SUBSTEP_LOGICAL_V1, version u16=1, workflow_kind u8, substep_kind u8,
  record_length u32, plan_source_kind u8, reserved_zero[3],
  plan_ordinal u32, substep_count u32,
  outer_operation_identity[32], full_plan_sha256[32], substep_base_sha256[32],
  logical_stable_substep_identity_sha256[32], plan_source_sha256[32],
  stable_source_predicate_sha256[32], semantic_target_effect_manifest_sha256[32],
  semantic_apply_plan_sha256[32], crc32c u32
)

ActualSubstepSourceProofV1 = (
  magic=MAGIC_SUBSTEP_ACTUAL_V1, version u16=1, reserved_zero u16, record_length u32,
  source_transaction_uuid[16], source_commit_lsn u64,
  certified_source_lsn u64, certified_source_history_root[32],
  commit_state_raw_sha256[32], certified_source_inventory_raw_sha256[32],
  raw_target_effect_manifest_sha256[32], raw_exact_apply_plan_sha256[32], crc32c u32
)

BoundOperationSubstepContextV1 = (
  magic=MAGIC_SUBSTEP_BOUND_V1, version u16=1, workflow_kind u8, substep_kind u8,
  record_length u32, logical_context_length u32, actual_source_proof_length u32,
  logical_context_sha256[32], actual_source_proof_sha256[32],
  logical_context_bytes[logical_context_length],
  actual_source_proof_bytes[actual_source_proof_length], crc32c u32
)
```

`affected_table_count` é 0..1048576 e IDs u32 são sorted/unique; mutation count é 0..262144,
scopes são sorted/unique por `(index_identity,table_id)` e delimitados por `entry_length`. Cada scope
tem fixed prefix 196 bytes e `entry_length==196+8*operation_count_entry_count`; operation counts são
1..12 entries sorted/unique por enum, count nonzero e checked sum. `operation_substep_length` é nonzero
e decodifica exatamente um trailer; outer WAL record length/CRC e este CRC interno ambos precisam
passar. Payload inteiro é limitado por `MAX_TOTAL_LENGTH` e não aceita trailing.

Logical/actual nested lengths são nonzero e casam seus `record_length`; seus SHAs são
`SHA256(DOMAIN_SUBSTEP_LOGICAL_V1||logical bytes)`. Os domain separators únicos são os identifiers
`DOMAIN_SUBSTEP_ACTUAL_V1` e `DOMAIN_SUBSTEP_BOUND_V1` do registry; actual e bound
usam respectivamente `SHA256(DOMAIN_SUBSTEP_ACTUAL_V1||actual bytes)` e
`SHA256(DOMAIN_SUBSTEP_BOUND_V1||bound bytes)`. Workflow/kind no bound casam o logical; source UUID/LSN existem
somente no actual proof e são duplicados no trailer para lookup, bit-identical. BIND exige ambos zero;
REBUILD/CLEAR exige o MARK committed exact. Trailer/body/plan source fields obedecem à matriz abaixo.
Todos os records incluem CRC; não se usa mais “prefixo até semantic apply” de um record maior como
codec implícito.

Esses três domain constants são definidos uma única vez; encoder/reviewer gate proíbe literal
duplicado com spelling/terminator alternativo. Golden vectors congelam os bytes incluindo o NUL final.

Os quatro discriminantes usam tabelas u8 fechadas:

- `SubstepModeV1`: `UNITARY=0`, `MULTI_STEP=1`; 2..255 recusam;
- `WorkflowKindV1`: `NONE=0`, `LEGACY_BIND=1`, `INDEX_REBUILD=2`; 3..255 recusam;
- `SubstepKindV1`: `NONE=0`, `BIND_IDENTITY=1`, `MARK_STALE=2`, `REBUILD=3`,
  `CLEAR_STALE=4`; 5..255 recusam;
- `PlanSourceKindV1`: `NONE=0`, `BOOTSTRAP_BIND_SOURCE=1`,
  `INLINE_OPERATION_MANIFEST=2`, `PRIOR_COMMITTED_MANIFEST=3`; 4..255 recusam.

Combinações/ordinals legais são **somente**:

| Mode/workflow | substep_count/ordinal | Substep kind | Plan source kind |
|---|---|---|---|
| UNITARY/NONE | 0/0 | NONE | NONE |
| MULTI_STEP/LEGACY_BIND | N>0 / cada i em `[0,N)` | BIND_IDENTITY | BOOTSTRAP_BIND_SOURCE |
| MULTI_STEP/INDEX_REBUILD | 3/0 | MARK_STALE | INLINE_OPERATION_MANIFEST |
| MULTI_STEP/INDEX_REBUILD | 3/1 | REBUILD | PRIOR_COMMITTED_MANIFEST |
| MULTI_STEP/INDEX_REBUILD | 3/2 | CLEAR_STALE | PRIOR_COMMITTED_MANIFEST |

`FrozenOuterOperationPlan.ordered_substeps.kind`, `OperationSubstepManifestV1.entries.kind`, trailer,
bound context, base digest e full-plan usam exatamente o mesmo byte `SubstepKindV1`; não existe enum
paralelo. Qualquer outra combinação, ordinal, count, zero/nonzero cruzado ou unknown recusa antes de
hash/append e em recovery/verifier.

UNITARY exige todos os demais campos/bytes zero e é canônico para uma grouped transaction que não
pertence a workflow multi-transaction. MULTI_STEP exige nonzero identities/hashes,
`plan_ordinal < substep_count` e bound context de comprimento canônico exato. Seu SHA é
`sha256(DOMAIN_SUBSTEP_BOUND_V1 || canonical_BoundOperationSubstepContextV1_bytes)`; trailer e body precisam
casar byte a byte. Todo campo duplicado entre trailer e body (workflow/kind/source kind, ordinal/count,
outer/full/base/source SHA e source UUID/LSN) precisa ser bit-identical. UNITARY exige
`bound_operation_substep_context_sha256=ZERO32`, comprimento zero,
body ausente e também todos os demais campos zero.

Ambos os comprimentos são exact u32, a soma/offsets usa checked u64 e o COMMIT completo precisa caber
simultaneamente em `MAX_TOTAL_LENGTH`, batch, segmento, read-back e memory budget. Para BIND isso é
provado antes do primeiro bootstrap marker; para rebuild é provado antes do MARK/primeiro WAL byte.
Excesso estrutural recusa sem marker/MARK. `bound_context_body` é autenticado por seu SHA e pelo CRC
externo do COMMIT; `plan_body` tem CRC canônico interno e o mesmo CRC externo. Truncation, trailing
bytes, non-minimal encoding ou length mismatch são MALFORMED, não “manifest ausente”.

Todo base usa uma fórmula explícita e disjunta por codec. Para BIND,
`substep_base_sha256==bind_substep_base_sha256==SHA256(DOMAIN_BIND_SUBSTEP_BASE_RECORD_V1 ||
canonical_BindSubstepBaseV1_bytes)`. Para INDEX_REBUILD,
`substep_base_sha256==SHA256(DOMAIN_INDEX_REBUILD_SUBSTEP_BASE_RECORD_V1 ||
canonical_IndexRebuildSubstepBaseV1_bytes)`. Não existe wrapper genérico, digest de fields-only ou
segundo SHA para o mesmo field. O BIND full plan consome o primeiro digest; para INDEX_REBUILD,
`rebuild_base_plan_digest = SHA256(DOMAIN_REBUILD_BASE_V1 || substep_count_u32 ||
ordered(ordinal_u32,substep_kind_u8,base_length_u32,substep_base_sha256))` e o full-plan usa
`DOMAIN_REBUILD_PLAN_V1` exatamente como na seção 5.1. Encodings são exact built-ins, length-prefixed, sem trailing
bytes; nenhum SHA de bound context/trailer entra no base ou no full-plan.

`logical_stable_substep_identity_sha256` é, para BIND,
`sha256(DOMAIN_BOUND_STABLE_BIND_V1 || canonical_BoundStableBindContextV1_bytes)`; para INDEX_REBUILD é
`SHA256(DOMAIN_REBUILD_STABLE_V1 || workflow/kind/ordinal/count || outer identity || full-plan SHA ||
substep base SHA || logical_plan_source_ordinal)`, em que MARK aponta para si/ordinal 0 e REBUILD/CLEAR
apontam logicamente para MARK/ordinal 0, nunca para o UUID/LSN de attempt. O
`stable_source_predicate_sha256` cobre somente predecessor ordinal/semantic horizon/inventário direto
esperados pela recipe; os dois `semantic_*` cobrem target/device effects sem epoch/txn/UUID/record
header/CRC/fingerprint. Esses três são parte do core lógico.

`canonical_logical_BoundOperationSubstepContextV1_bytes` é exatamente o record autônomo
`LogicalBoundOperationSubstepContextV1`, com record length/CRC/domain próprios e sem qualquer
placeholder/SHA do `ActualSubstepSourceProofV1`; não é obtido zerando a proof no encoding completo. Isso dá uma única
preimage aos domains `DOMAIN_BIND_STABLE_CONTEXT_V1`/`DOMAIN_REBUILD_STABLE_CONTEXT_V1` e impede
colisão entre “ausente” e ZERO fields.

`ActualSubstepSourceProofV1` liga esse core à source **actual**: P/history root, raw commit.state,
inventário raw e manifests/apply hashes raw do plan corrente. Seu SHA implícito é
`sha256(DOMAIN_SUBSTEP_ACTUAL_V1 || canonical_actual_bytes)` e entra no SHA externo
`DOMAIN_SUBSTEP_BOUND_V1`; ele pode variar entre attempts. Generations/permit nonces continuam exclusivamente no
`AttemptFencingContextV1`, nunca são duplicados aqui. O body integral fica no COMMIT; live, recovery e
verifier o decodificam, recalculam ambos os SHAs e provam locator/horizon/inventory contra grouped
history e targets/effects do próprio substep. Hash sem body nunca é autoridade.

`certified_source_lsn` é exatamente o `pre_append_published_lsn=P` da mesma `WriteSnapshotProof`, e
`certified_source_history_root` termina em P: ambos excluem BEGIN/effects/COMMIT do próprio draft.
`commit_state_raw_sha256` é a leitura que publicou esse P. Qualquer root que inclua o substep corrente,
ou P diferente entre actual proof e WriteSnapshotProof, é self-reference/mismatch pre-WAL.

Os preimages são fechados e não autorreferentes. `semantic_target_effect_manifest_sha256` é
`SHA256(DOMAIN_SUBSTEP_SEMANTIC_EFFECT_V1 || canonical semantic non-control effect templates || canonical semantic
device targets)`; `semantic_apply_plan_sha256` usa `DOMAIN_SUBSTEP_SEMANTIC_APPLY_V1` sobre a ordem/kinds/owners dos passos
device. Os correspondentes campos raw do actual usam `DOMAIN_SUBSTEP_RAW_EFFECT_V1`/
`DOMAIN_SUBSTEP_RAW_APPLY_V1` sobre os exact
**non-BEGIN/non-COMMIT** effect records sem seus CRCs e sobre target/apply images/steps já retargeted.
Todos excluem BEGIN, COMMIT, `CommitPayloadV2`, trailer, bound-context body/SHA, proof nonce, record
CRC e draft fingerprint. Construção/recompute é obrigatoriamente:
`effect templates/device plan → semantic hashes → encoded non-control effects → raw actual hashes →
bound context/body SHA → trailer/CommitPayload → COMMIT encode/CRC → draft fingerprint`. Mutante que
inclui qualquer output à direita num preimage à esquerda é cycle/MALFORMED pre-WAL.

Para LEGACY_BIND, `plan_source_kind=BOOTSTRAP_BIND_SOURCE(1)`, plan body/source transaction são zero e
`plan_source_sha256` é exatamente o `bind_plan_source_digest` predecessor da seção 3.1.1, nunca o
`CutoverFeasibilityDigest` final. O marker/envelope retido fornece a preimage integral de core e de
todos os `BindSubstepBaseV1`; verifier exige que ela recompute o mesmo source digest e que seu final
digest/marker chain seja válido. A feasibility prevê bit-identical somente o core lógico do
`BoundOperationSubstepContextV1`; sua `NormalizedBindProjectionV1` tag-normaliza a proof actual e
recalcula os SHAs/CRC derivados. Para INDEX_REBUILD no primeiro MARK, source transaction UUID/LSN são
zero,
`plan_source_kind=INLINE_OPERATION_MANIFEST(2)`,
`plan_source_sha256 = SHA256(DOMAIN_SUBSTEP_SOURCE_V1 || plan_body)` e `plan_body` contém:

```text
OperationSubstepManifestV1 = (
  magic=MAGIC_SUBSTEP_MANIFEST_V1, version u16=1, workflow_kind u8, reserved_zero u8,
  record_length u32, database_uuid[16], persisted_index_identity[32], rebuild_operation_nonce[32],
  rebuild_base_plan_digest[32], outer_operation_identity[32],
  substep_count u32, entries_length u32, full_plan_sha256[32],
  ordered_entries[substep_count], crc32c u32
)
operation_substep_manifest_entry := (
  entry_length u32, ordinal u32, substep_kind u8,
  base_codec_kind u8=INDEX_REBUILD_SUBSTEP_BASE, reserved_zero u16,
  base_length u32, substep_base_sha256[32], substep_base_bytes[base_length]
) # entry_length == 48 + base_length
```

O full-plan SHA é recomputado pelo encoding/domain explícitos acima somente dos ordered base
SHA/kinds/ordinals. Decoder primeiro recalcula cada base SHA e `rebuild_base_plan_digest`, depois outer
identity a partir de database/index/nonce, e só então full-plan; zero/duplicate nonce, input/output
inconsistente ou collision pending/history recusa. Os base bytes de rebuild
contêm index identity/header/path/definition, algorithm+codec versions/parameters, target **semântico**
de MARK (STALE/identity/recipe, nunca page_lsn/terminal/offset/raw header/CRC/WAL placement), a
recipe fechada `REBUILD_FROM_CERTIFIED_HEAP_AND_GROUPED_WAL` com seus source/horizon predicates e o
CLEAR dependency target ordinal; nenhum callback/provider fica retido. A recipe precisa derivar
bijetivamente o próximo raw plan de direct heap/catalog + grouped WAL corrente. O body inline pode
conter somente templates semânticos + parâmetros/recipe versionados; raw future page/WAL preimage,
page_lsn/terminal/offset/epoch/txn/UUID/CRC é proibido, pois ficaria stale se P/tail avançar. Backend/
algoritmo sem essa recipe determinística recusa antes do MARK com zero WAL byte; V7 não oferece
fallback por blob futuro nem reserva global de LSN.
Foreign append entre freeze do base e MARK apenas muda source certification/target raw no
`BoundOperationSubstepContextV1`; o base/full-plan/outer permanecem iguais e um novo draft é formado
pre-byte. Se o efeito estrangeiro toca a identity, o pending/predecessor predicate recusa em vez de
silenciosamente alterar o base.

REBUILD e CLEAR posteriores usam `plan_source_kind=PRIOR_COMMITTED_MANIFEST(3)`, body vazio,
`plan_source_sha256` igual ao mesmo `DOMAIN_SUBSTEP_SOURCE_V1` SHA do manifest inline e source UUID/commit LSN iguais ao MARK
completo. Cada trailer repete outer identity/full plan/count e seu ordinal/base/bound-context SHA/body. Live,
recovery e verifier recuperam a preimage da source indicada, recomputam todas as hashes e recusam
digest opaco sem bytes, source ausente, body extra, ordinal skipped sem predecessor ou divergência.

Um MARK multi-step completo cria somente o pending **lógico**, derivável do grouped WAL; MARK não
publica `SystemOperationFloorMapSlotV1`, não executa action 24 e não instala implicitamente um floor.
Enquanto essa entry é `NONTERMINAL`, `active_checkpoint_ceiling` mantém Q estritamente abaixo do MARK
COMMIT, portanto recycle preserva todos os records/body necessários. Se MARK→...→CLEAR/BOUND completa
antes de qualquer checkpoint que tenha instalado um floor, a entry desaparece ao se tornar terminal:
o checkpoint posterior pode incorporar a história inteira sem criar/remover record SYSTEM. Se um
checkpoint encontra a entry ainda NONTERMINAL, ele executa action 24 antes de publicar seu candidate e
instala o floor durável; a partir daí o record não expira por TTL. CLEAR/BOUND bit-identical muda essa
entry instalada para `TERMINAL_PENDING_FLOOR_RELEASE`: deixa de limitar Q, mas o system floor impede
recycle do prefixo MARK/body/CLEAR até um checkpoint posterior incorporar/autenticar a história e o
exact terminal release set. Só então actions 14 publicam o map sem as keys, uma por successor; recycle
vem numa passagem posterior à direct certification. Assim cold recovery nunca depende de RAM e os
dois schedules — CLEAR antes de qualquer floor, ou floor→CLEAR→checkpoint→release — são inequívocos.

O mesmo scan mais o `SystemOperationFloorMapSlotV1` constrói:

```text
PendingOuterOperationMapV1 = (
  magic=MAGIC_PENDING_OUTER_MAP_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], entry_count u32, entries_length u32,
  entries[entry_count], crc32c u32
)
pending_outer_entry := (
  entry_length u32=256, pending_state u8, workflow_kind u8, reserved_zero u16,
  persisted_index_identity[32], outer_operation_identity[32], full_plan_sha256[32],
  next_ordinal u32, substep_count u32, mark_transaction_uuid[16], mark_commit_lsn u64,
  mark_predecessor_complete_commit_lsn u64, plan_source_sha256[32],
  last_compatible_effect_lsn u64, retention_boundary u64,
  installed_system_floor_key_sha256[32], committed_chain_sha256[32]
)
```

O map é keyed por `PersistedIndexIdentity`. `PendingStateV1` é
`INVALID=0`, `NONTERMINAL=1`, `TERMINAL_PENDING_FLOOR_RELEASE=2`; 3..255 recusam. NONTERMINAL vem da
cadeia MARK→next, com ou sem floor instalado; TERMINAL entra no mapa somente quando exige CLEAR/BOUND
exact na history **e** a mesma system floor key ainda está no carrier. CLEAR terminal sem floor prévio
e terminal já liberado não entram. Antes de **todo** mutador, depois de adquirir o EXCLUSIVE canônico e sob
COMMIT, o mapa é recomputado/revalidado contra o tail current. Para uma identity pending, somente o
substep `next_ordinal` do mesmo outer/full plan/base/source pode produzir efeito; segundo MARK/rebuild,
DROP, ALTER, RESET, RECONCILE, CLEAR antigo/adiantado ou qualquer row/index mutation que tocaria essa
identity recusa `INDEX_OUTER_OPERATION_PENDING` com zero WAL byte. V7 não implementa uma policy de
absorção de writes concorrentes na recipe; commits estrangeiros que comprovadamente não tocam a
identity/tabela/definition source podem avançar P e obrigam recomputar o bound actual antes do byte
zero, sem mudar o plano lógico.

NONTERMINAL sem action24 prévia exige `installed_system_floor_key_sha256=ZERO32`; após install exige a
key nonzero current. TERMINAL exige key nonzero e chain terminando em CLEAR/BOUND. Outer/full/source/
chain/UUID são sempre nonzero, ordinals obedecem workflow e predecessor/retention são terminal
boundaries. Qualquer combinação cruzada recusa.

Entries são sorted/unique pela persisted identity e todos os nonzero/hash/ordinal fields são
recomputados de history+system map. Para count nonzero, o digest é
`SHA256(DOMAIN_PENDING_OUTER_MAP_V1 || canonical_pending_map_bytes)`; count zero tem digest canônico ZERO32, nunca
o SHA do encoding vazio. Duplicate identity, next ordinal fora da tabela, predecessor boundary
não-terminal ou trailing byte recusa.

REBUILD exige MARK predecessor e CLEAR exige a cadeia por-identity MARK→REBUILD, com hashes/base/
targets bit-identical e nenhum efeito incompatível interposto; commits estrangeiros fora do scope
podem intercalar no WAL. Dois pendings live para a mesma identity, predecessor ausente, branches
incompatíveis ou mapa que muda entre proof e append são `CORRUPT/INCONCLUSIVE`, nunca escolha por LSN.
Close/crash não libera o pending; cold recovery o reconstrói antes de expor writer/read view e retoma
apenas o próximo substep ou recusa. CLEAR muda a state lógica para TERMINAL mas não remove bytes/key.
Action 14, após terminal/checkpoint-incorporation proof e antes de recycle, publica o map
slot+coordination successor sem a key;
somente então a entrada desaparece. Nunca TTL, PID death, state RAM ou hash-subset oportunista.
TERMINAL não bloqueia commits ordinários cuja recipe já terminou, mas bloqueia outro MARK multi-step
na mesma identity até release para manter uma única key por identity.

Recovery sem novo WAL é distinto de “produzir o próximo substep”: se MARK/REBUILD/CLEAR já está
committed mas não direct-materialized, `RecoveryApplyPlan` pode e deve reaplicar **esse mesmo** raw
substep idempotentemente, mesmo que `next_ordinal` lógico já aponte adiante. A permissão é bijetiva ao
UUID/LSN/trailer/body committed e não autoriza novo append/replan. Nenhum next substep congela draft
até `RuntimeDeviceReplayProof.materialized_through_lsn` incluir o predecessor committed.

O enum é fechado e não colapsa operações que o WAL já distingue: `CREATE=1`, `DROP=2`, `ALTER=3`,
`INSERT=4`, `TOMBSTONE=5`, `REMOVE=6`, `RESET=7`, `REBUILD=8`,
`RECONCILE_ADVANCE=9`, `MARK_STALE=10`, `CLEAR_STALE=11` e `BIND_IDENTITY=12`.
`CHECKPOINT_ADVANCE=13` só é válido no `CheckpointIndexManifest` abaixo. Zero e 14..255 recusam;
código 13 dentro de `CommitPayloadV2` ou 1..12 usado como operação global do
checkpoint também recusa. Em particular, todo
`IndexOperation.REMOVE` carregado por um record `INDEX_RECONCILE` projeta para `REMOVE=6`; ele não
pode ser reclassificado como write genérico nem apenas absorvido por `RECONCILE_ADVANCE`.

Existe exatamente um scope por `index_identity` afetada em cada transação. `operation_counts` é
não-vazio e conta cada efeito lógico uma vez sob seu código exato; cada `IndexChange` INSERT,
TOMBSTONE, REMOVE ou RESET contribui uma unidade. CREATE/DROP/ALTER e os efeitos header-only
REBUILD/RECONCILE_ADVANCE/MARK/CLEAR/BIND contribuem a unidade lógica correspondente. Num rebuild, os
records componentes continuam contados por seu código decodificado e o efeito de orquestração/header
contribui `REBUILD=1`; não há segundo scope nem page owner duplicado.

`IndexCarrierStateV1` é u8 `LIVE=0`, `TOMBSTONED=1`; 2..255 recusam. Successor precisa ser
`checked_increment_u64(predecessor)` e target TOMBSTONED só é legal quando counts contém DROP=1;
qualquer outra operação exige LIVE, inclusive CREATE antes de exposição. `carrier_plan_sha256` é
`SHA256(DOMAIN_INDEX_CARRIER_PLAN_V1 || index identity || predecessor || successor || target state ||
canonical operation_counts/before/after/effect semantic hashes)`, excluindo seu próprio campo,
BEGIN/COMMIT, UUID/LSN/CRC e WalAppendDraft. Feasibility BIND tag-normaliza predecessor/successor raw e
recalcula o carrier-plan SHA; live/recovery preserva os raw valores committed. Assim grouped WAL
carrega a preimage completa que recovery precisa para publicar o mesmo slot sem inventar generation.

O `effect_sha256` autentica a tuple canônica, ordenada por LSN/ordinal, de **todos e somente** os
records, target pages, pre/post headers e catalog/schema effects atribuídos àquela identity. Cada
efeito físico/lógico pertence a uma identity scope e a exatamente uma contagem; cada page target
pertence a uma única identity scope mesmo que consolide várias mudanças. A contagem e o digest são
reconstruídos independentemente, portanto trocar INSERT por TOMBSTONE/REMOVE, omitir o header final
ou duplicar um record recusa. Digest zero é permitido somente no lado inexistente de CREATE/DROP;
identity nunca é zero e não é o nome/path mutável. Counts e bytes têm limites checked antes do freeze.
`CommittedTransaction.decoded_commit_payload` expõe o manifest detached; o CRC do record, grouped
terminal e `WalAppendPlan` autenticam-no.

Todo BEGIN format v2 usa payload fixo
`MAGIC_BEGIN_PAYLOAD_V2 || begin_version_u16=1 || reserved_u16=0 || transaction_uuid[16]`.
COMMIT V2 repete exatamente o mesmo UUID no trailer acima. Zero, mismatch BEGIN/COMMIT, reuse em
outra `(epoch,txn_id)` da história necessária ou UUID presente em manifest mas ausente no BEGIN é
malformed antes de apply. UUID é identidade persistida, não fonte de ordem; epoch/txn_id continuam
governando o agrupamento WAL.

Antes de append e novamente em recovery, um validator total reconstrói o manifest obrigatório a
partir de row intents canonicalizados, ownership/table descriptors das page images, records
`INDEX_WRITE`/`INDEX_RECONCILE` — distinguindo INSERT, TOMBSTONE, REMOVE e RESET —, efeitos de
rebuild, target headers e catalog/schema diff.
`affected_table_ids` é exatamente o conjunto de tabelas cujo conteúdo lógico pode mudar; cada
mutação física/lógica index-only corresponde bijetivamente a uma identity scope e a uma entrada de
`operation_counts`. Omissão, extra, operação trocada, contagem/digest divergente ou page touch sem owner provado recusa antes do
append/apply. A ausência de um `INDEX_WRITE` esperado não permite omitir a tabela: ou a preparação
recusa, ou a tabela permanece affected e o relevant horizon avança, deixando o header curto
detectável. Alterar header/bucket sem `IndexMutationScope` é proibido mesmo quando não há row effect.

Não existe manutenção normal unlogged. RESET, rebuild, reconcile e publicação durable de STALE/CLEAN
usam transação WAL v2 e o enum acima. Depois de uma falha, `_local_poison_reason` pode ser instalado
sem I/O para fechar este processo; isso não é mutação compartilhada. Se um WAL já cruzou barrier, o
gap gate da seção 8.4 impede leitores até recovery aplicar seu manifest. Uma ferramenta offline que
precise reconstruir antes de haver WAL v2 só pode operar no runbook de cutover, com database fechado,
backup e troca atômica integral; ela não é rota pública de maintenance.

Checkpoint não inventa um COMMIT. Ele usa um `CheckpointIndexManifest` frozen, autenticado pelo
checkpoint state durável descrito abaixo, contendo todas as identities conhecidas em ordem, operação
`CHECKPOINT_ADVANCE=13`, relevant horizon de entrada, target built/reconciled, binding state e SHA-256 semântico do
IndexHeader/page0 alvo (exclui CRC/seq de write-back). A direct certificação posterior registra o raw
SHA-256 observado em sua proof detached, não tenta predizê-lo no manifest.
O manifest não usa o antigo `pending_checkpoint_ceiling` indistinto. Seu byte contract inclui as
projeções active/terminal e a preimage integral necessária à liberação:

```text
CheckpointIndexManifestV1 = (
  magic=MAGIC_CHECKPOINT_INDEX_MANIFEST_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], source_q u64, target_q u64, published_p u64,
  index_count u32, pending_entry_count u32,
  active_pending_count u32, terminal_retention_count u32,
  terminal_release_count u32, reserved_zero u32,
  active_checkpoint_ceiling u64,
  pending_outer_operation_map_sha256[32], checkpoint_pending_projection_sha256[32],
  active_pending_set_sha256[32],
  terminal_retention_floor_sha256[32], terminal_release_set_sha256[32],
  committed_history_root_sha256[32],
  device_apply_manifest_length u32, wal_prefix_recycle_plan_length u32,
  terminal_floor_release_recipe_length u32, reserved_zero u32,
  device_apply_manifest_sha256[32], wal_prefix_recycle_plan_sha256[32],
  terminal_floor_release_recipe_sha256[32],
  pending_entries[pending_entry_count], index_entries[index_count],
  device_apply_manifest_bytes[device_apply_manifest_length],
  wal_prefix_recycle_plan_bytes[wal_prefix_recycle_plan_length],
  terminal_floor_release_recipe_bytes[terminal_floor_release_recipe_length], crc32c u32
)
pending_entry := (
  persisted_index_identity[32], pending_state u8, release_eligible u8,
  workflow_kind u8, reserved_zero u8, next_ordinal u32,
  outer_operation_identity[32], full_plan_sha256[32], plan_source_sha256[32],
  retention_boundary u64, mark_predecessor_complete_commit_lsn u64,
  mark_commit_lsn u64, terminal_commit_lsn u64,
  system_floor_record_raw_sha256[32], terminal_chain_sha256[32]
)
index_entry := (
  persisted_index_identity[32], operation u8=CHECKPOINT_ADVANCE,
  binding_state u8, reserved_zero u16,
  relevant_horizon_in u64, target_built_through_lsn u64,
  target_reconciled_through_lsn u64, semantic_page0_sha256[32]
)
checkpoint_index_manifest_sha256 =
  SHA256(DOMAIN_CHECKPOINT_INDEX_MANIFEST_V1 || canonical manifest bytes)

`wal_prefix_recycle_plan_length==0` exige SHA ZERO32 e nenhum segmento elegível; length nonzero exige
SHA igual a `DOMAIN_WAL_MONOTONIC_PROGRESS_PLAN_V1` sobre os bytes embedded, workflow PREFIX_RECYCLE,
stable source REQUESTED_CHECKPOINT_BOUNDARY e source/target inventories derivados do mesmo
`source_q,target_q,published_p`. O requested wrapper embedded no plan precisa igualar o proof usado na
semantic base do checkpoint. O manifest digest acima cobre os bytes integrais, não apenas o nested
SHA. Extra plan quando nenhum prefixo pode ser reciclado, plan zero com removed set nonempty, plan de
outro workflow/boundary ou nested/trailing mismatch recusa antes da candidate publication.
`terminal_release_count==0` exige release set/recipe length/recipe SHA ZERO. Count nonzero exige
recipe length 1..2097152, digest registrado sobre bytes embedded e uma recipe integral cujo source
map/coord/release entries casam ao pending vector; set SHA nonzero/igual. `pending_entry` tem 232 bytes e `index_entry` 92 bytes. Counts são respectivamente 0..4096 e
0..262144; `device_apply_manifest_length` é 1..67108864. O prefix plan length é zero ou
1..134217728. O manifest exige
`record_length == 396 + 232*pending_entry_count + 92*index_count + device_apply_manifest_length +
wal_prefix_recycle_plan_length + terminal_floor_release_recipe_length <=268435456`, com produto/soma checked antes de allocation. Golden
fixtures cobrem os dois zero modes do prefix plan, N=1 e bounds máximos; truncation/length ±1, count
overflow e troca entre device/progress bytes recusam.

As três projections não reutilizam o map domain: `active_pending_set_sha256 =
SHA256(DOMAIN_CHECKPOINT_ACTIVE_SET_V1 || canonical NONTERMINAL pending_entry bytes)`,
`terminal_retention_floor_sha256 = SHA256(DOMAIN_CHECKPOINT_TERMINAL_SET_V1 || canonical
TERMINAL_PENDING_FLOOR_RELEASE bytes)` e `terminal_release_set_sha256 =
SHA256(DOMAIN_CHECKPOINT_RELEASE_SET_V1 || canonical release_eligible terminal bytes)`, sempre na
ordem do vector do manifest. O conjunto vazio de cada projection é ZERO32; nonempty nunca é ZERO.
Counts e filtros precisam ser bijetivos e disjuntos salvo que release é o subset marcado do terminal;
trocar domain, reordenar ou hashear o map integral recusa.

CheckpointDeviceApplyManifestV1 = (
  magic=MAGIC_CHECKPOINT_DEVICE_MANIFEST_V1, version u16=1, recipe_version u16=1, record_length u32,
  database_uuid[16], source_q u64, target_q u64, published_p u64,
  target_count u32, reserved_zero u32,
  committed_history_root_sha256[32], target_set_sha256[32],
  targets[target_count], crc32c u32
)
device_target := (
  target_kind u8, barrier_flags u8, reserved_zero u16, target_ordinal u32,
  owner_identity[32], file_identity[32], page_number u64, byte_offset u64,
  byte_length u32, reserved_zero u32, source_effect_set_sha256[32],
  expected_postimage_sha256[32]
)
checkpoint_device_apply_manifest_sha256 =
  SHA256(DOMAIN_CHECKPOINT_DEVICE_MANIFEST_V1 || canonical manifest bytes)
target_set_sha256 = SHA256(DOMAIN_CHECKPOINT_DEVICE_TARGET_SET_V1 ||
                           LE32(target_count) || canonical device_target bytes)

checkpoint_source_effect_leaf := (
  effect_ordinal u32, segment_number u64, record_offset u64, record_length u32,
  wal_format_version u8, record_type u8, reserved_zero u16, lsn u64,
  transaction_uuid[16], record_raw_sha256[32], owner_identity[32],
  semantic_effect_sha256[32]
) # exatamente 148 bytes
```

Entries são sorted/unique por persisted identity; count total é a soma checked das states
NONTERMINAL e TERMINAL. `release_eligible=1` exige TERMINAL, CLEAR/BOUND completo
`terminal_commit_lsn<=target_q`, exact system floor record ainda presente e history/manifest do
checkpoint cobrindo MARK/body/CLEAR; qualquer outro entry exige zero. As três contagens/digests são
recomputadas filtrando a mesma lista: active por state NONTERMINAL, terminal retention por state
TERMINAL e release por `release_eligible=1`, com os identifiers de domain canônicos da seção 0.1.1.
`pending_outer_operation_map_sha256` é **somente** o digest do record integral
`PendingOuterOperationMapV1` definido acima; ele nunca é recalculado sobre `pending_entries`.
`checkpoint_pending_projection_sha256` é o digest separado da lista inteira `pending_entries`, sob o
domain `DOMAIN_CHECKPOINT_PENDING_PROJECTION_V1`. O count do map e `pending_entry_count` são iguais e
existe bijeção sorted pela identity: cada entry projetada repete exatamente state/workflow/identity/
outer/full-plan/next/source/retention/mark fields do map, enquanto terminal/release/system-floor raw
fields derivados são recomputados da mesma history e carrier; missing/extra ou common field distinto
recusa. Map vazio exige ambos os digests ZERO32; map nonempty exige ambos nonzero, sem exigir que sejam
iguais entre si. Active vazio exige ceiling=P; active
nonempty exige o terminal predecessor completo mais antigo **somente** entre NONTERMINAL. Terminal
retention nunca reduz ceiling/Q, mas impede recycle. Os bodies não são duplicados: esta entry +
grouped history/plan source locators são a preimage fechada e o system floor mantém o WAL até release.
CRC cobre o manifest inteiro; record length/EOF, enum/state/release cross-rules e todos os digests são
verificados antes de usar Q ou emitir action 14.

O device manifest tem 0..`MAX_CHECKPOINT_DEVICE_TARGETS_V1` targets sorted/unique por ordinal e por
physical `(file_identity,page_number,byte_offset,byte_length)`; target kind/barrier flags usam os enums
da authority de controle. Cada target final aparece uma vez mesmo que muitos effects o tenham tocado;
`source_effect_set_sha256` é o Merkle root da lista que a recipe v1 rederiva integralmente do grouped
history `(source_q,published_p]`. Cada target exige 1..1048576 leaves sorted/unique por
`(lsn,segment_number,record_offset,effect_ordinal)` e bijetivas aos effects que contribuíram ao
postimage. Leaf hash é `SHA256(DOMAIN_CHECKPOINT_EFFECT_LEAF_V1 || leaf_bytes)`. No nível zero os leaf
hashes mantêm essa ordem; cada nível seguinte agrupa pares e calcula
`SHA256(DOMAIN_CHECKPOINT_EFFECT_NODE_V1 || LE32(level) || left[32] || right[32])`; quando o count é
ímpar, o último usa `right=ZERO32`, nunca duplicação/promote. Repete até um root; empty effect set
recusa para target existente. `target_set_sha256` usa a fórmula acima sobre os encodings completos de
160 bytes; zero targets exige ZERO32 em vez do hash do vector vazio. `device_apply_manifest_sha256`
no parent é exatamente `checkpoint_device_apply_manifest_sha256`, sobre o record integral incluindo
CRC. Offset/length desses bytes no successor payload é calculado antes do control core e
entra no locator; nenhum SHA/offset/witness entra na própria preimage.

`TerminalFloorReleasePendingGateV1` é obrigatório antes de criar/publicar outro checkpoint candidate:
se o checkpoint current direct-valid contém release entries cuja exact system key ainda aparece no
map carrier, sua `TerminalFloorReleaseRecipeV1` embedded é a preimage durável integral e a única
progressão write-capable é derivar/retomar action 14 sobre **esse mesmo** manifest/recipe; novo
target Q/candidate/slot recusa `TERMINAL_FLOOR_RELEASE_PENDING` com zero effect. Assim crash depois de
C1 ou depois de qualquer step i<N não permite C2 sobrescrever a única authority durável do set.
Read-only pode
continuar em Q=C1 se as demais proofs forem válidas; não libera/recycle. Só quando todas as keys de C1
sumiram e o map/header foram direct-certified um checkpoint C2 pode nascer.
O conjunto deve ser bijetivo ao catálogo direto e a todas as page0 tocadas. O mesmo validator de
`IndexMutationScope` atende commit, recovery, verify e checkpoint; não há segundo enum.

Como `CommitStateV1` atual só carrega, além de magic/version/CRC, os três u64 publicados, o manifest não pode existir apenas em memória nem ser
inferido depois que o WAL for reciclado. V7 adiciona dois slots alternados
`control/checkpoint.index.0` e `control/checkpoint.index.1`, cada um uma imagem integral:

```text
CheckpointIndexStateV1 =
  magic[8]                    = MAGIC_CHECKPOINT_INDEX_STATE_V1
  format_version u16          = 1
  reserved u16                = 0
  publication_generation u64
  database_uuid[16]
  wal_cutover_lsn u64         = C
  wal_fence_generation u64
  checkpoint_lsn u64          = Q
  index_binding_state u8       # LEGACY_BASELINE=0, BOUND_V2=1
  reserved2[7]                 = zero
  catalog_sha256[32]
  history_baseline_sha256[32]
  composite_plan_core_length u32
  composite_plan_core_sha256[32]
  composite_plan_core_bytes[composite_plan_core_length]
  manifest_length u32
  manifest_sha256[32]
  manifest_bytes[manifest_length]
  publication_witness_length u32
  publication_witness_bytes[publication_witness_length]
  crc32c u32
```

`composite_plan_core_bytes` é vazio/ZERO32 somente no baseline inicial, antes de qualquer candidate;
candidate exige um único `CheckpointCompositePlanCoreV1` canônico nonempty e seu SHA domain-separated.
`manifest_bytes` é o encoding canônico length-prefixed do `CheckpointIndexManifest`; count/length,
ordenação, unicidade, enum 13, u32/u64 e ausência de trailing bytes são checked. O CRC cobre todos os
bytes anteriores e `manifest_sha256` cobre exatamente `manifest_bytes`. Em genesis WAL v2, Q=C=0 e
existe um baseline vazio canônico publication generation 1, fence generation 1, `BOUND_V2`, antes de qualquer
recycle. Um baseline criado ainda sob meta/WAL v1 usa exclusivamente
`wal_cutover_lsn=0, wal_fence_generation=0, LEGACY_BASELINE`; esses valores não são válidos em
operação V2 normal.

`IndexBindingStateV1` é o `u8` fechado `LEGACY_BASELINE=0`, `BOUND_V2=1`; 2..255 recusam. ZERO é
semântica legado explícita, não INVALID. Kind desconhecido ou combinação LEGACY com C/generation V2
normal recusa.

Os três targets control têm carrier físico fechado; `encode_target_v7` não é uma abstração deixada ao
backend. Constantes built-in, não configuráveis:

```text
MAX_CONTROL_WITNESS_BYTES_V1 = 1_048_576
MAX_BOOTSTRAP_OR_CHECKPOINT_STATE_BYTES_V1 = 67_108_864
MAX_COMMIT_STATE_V2_BYTES_V1 = 1_048_616
MAX_CONTROL_TARGET_STATE_BYTES_V1 = 67_108_864
MIN_V7_BOOTSTRAP_STATE_BYTES_V1 = 402
MAX_CHECKPOINT_DEVICE_TARGETS_V1 = 262_144
```

402 é o lower bound estrutural `348 fixed-prefix + 4 witness-length + 46 minimum witness envelope +
4 CRC`; imagem com esse comprimento ainda precisa passar o core/authority decoder integral. O
classifier de temp lê no máximo `MAX_CONTROL_TARGET_STATE_BYTES_V1+1`: byte extra classifica oversized
e recusa cleanup automático, sem allocation/read unbounded.

`publication_witness_length` é 1..`MAX_CONTROL_WITNESS_BYTES_V1`,
deve igualar exatamente os bytes seguintes e esses bytes decodificam como um único
`ControlPublicationWitnessV1` do mesmo target kind/component. ZERO, trailing, truncation, witness de
outro target ou total acima do limite recusam.
Para qualquer target, a inequality única é
`46 + control_publication_core.record_length <= MAX_CONTROL_WITNESS_BYTES_V1`; o core length inclui
todos os wrappers `40+body_length`, paths e authority bodies. Cada body ainda precisa satisfazer seu
limite próprio e nenhum encoder aloca antes de somar checked todas as parcelas. Boundary exato cabe;
+1 recusa antes de temp/WAL/device. Não existe “split público” implícito.

Commit pequeno pode usar entries inline. Checkpoint/recovery globais usam o
`CheckpointDeviceApplyManifestV1` integral dentro de `CheckpointIndexManifestV1` e deixam no witness
somente um locator/root fechado para esses bytes; recovery/commit cumulativo também pode usar o WAL
retido como locator determinístico. Verifier precisa abrir o container, extrair/redecodificar toda a
preimage e recomputar root/count antes de aceitar — digest isolado não serve. O allocator/growth
preflight simula o próximo checkpoint com **todos** os files/pages atualmente conhecidos mais os
targets novos do commit. Se target count ultrapassa 262144 ou a imagem checkpoint completa ultrapassa
67,108,864, recusa `DATABASE_CHECKPOINT_CAPACITY` antes do primeiro WAL byte/allocation; assim uma
série de commits individualmente pequenos nunca cria um estado que o próximo checkpoint não consegue
representar. A capacity proof entra no `WriteSnapshotProof`/manifest e é revalidada pre-append.

Para `V7BootstrapStateV1`, chamando de F/G/I/R os quatro blob lengths em ordem, o offset do
`publication_witness_length u32` é exatamente `348+F+G+I+R`; witness começa em `352+F+G+I+R` e o
`crc32c u32` está imediatamente depois dele. Para `CheckpointIndexStateV1`, com
C=`composite_plan_core_length` e M=`manifest_length`, esses offsets são, respectivamente,
`204+C+M`, `208+C+M` e depois do witness. Em ambos, cada soma é
checked em u64 antes de limitar/representar em u32; o CRC cobre do magic até o último byte do witness,
exclui apenas o próprio CRC, e o raw SHA cobre a imagem inteira inclusive CRC. O
`successor_payload` da seção 5.2 são exatamente os bytes do magic até o último byte R/M, excluindo
`publication_witness_length`, witness e CRC.

`control/commit.state` ganha um codec formal apenas depois do feature fence:

```text
CommitStateV2 =
  magic[4]                    = MAGIC_COMMIT_STATE_SHARED
  format_version u16          = 2
  reserved_zero u16           = 0
  last_committed_lsn u64      = P
  last_csn u64
  checkpoint_lsn u64          = Q
  publication_witness_length u32
  publication_witness_bytes[publication_witness_length]
  crc32c u32
```

O witness-length fica no offset 32, witness no 36 e o total é exatamente `40+W`, no máximo
1_048_616 bytes. CRC/raw SHA e `successor_payload` seguem a mesma regra; para esse target o payload é
o prefixo exato de 32 bytes. `CommitStateV1` começa pelo mesmo `MAGIC_COMMIT_STATE_SHARED`, continua
sendo os 32 bytes de body existentes + CRC, total 36, version=1 e **sem witness**. O decoder V7
primeiro lê magic/version de um prefixo bounded e então
exige exatamente 36 para v1 ou `40+W` para v2; nunca tenta interpretar prefixo v2 como v1. O decoder
antigo exige length 36 e portanto recusa V2 antes de poder usar seus P/Q, que é o comportamento seguro.

Legacy normal usa somente V1. Durante cutover offline, V1 permanece até a transaction fence/meta V2
estar durable/direct-certified; o checkpoint C publica o primeiro V2, com expected-old V1 autenticado.
Entre fence e essa publicação, o modo restrito não oferece read-view/writer comum. Genesis V7 cria V2
desde P=Q=0. Depois que DatabaseIdentity é V2, V1 só é aceito na janela restrita, provada por marker+
fence history, de completion do checkpoint C; em BOUND ou operação V7 normal é missing-feature/
inconclusive, nunca fallback. `_publish_control_state_durable` aceita COMMIT_STATE somente V2; a porta
V1 anterior ao fence permanece no writer legado e está tombstoned assim que PREPARING torna a DB
offline.

Cada decoder recomputa o `ControlPublicationCoreV1` extraído, exige target kind, component,
database UUID, payload length/SHA, semantic generation/state e composite core coerentes com os campos
do carrier. O witness de PREPARING também é obrigatório (`authority_count=1` NAMESPACE_SOURCE, old absent); não existe
exceção “first image sem witness”. Para bootstrap phases, o witness inclui ainda o
`SpaceReservationStageProofV1` e os direct artifact/carrier identity bindings correspondentes ao
mesmo planned-postimage manifest; PREPARING admite ABSENT, PREPARED/FENCED/BOUND exigem o conjunto
DIRECT_CERTIFIED integral. Esses stage proofs não alteram image/carrier/space plan bytes. Canonical
re-encode precisa ser byte-identical.

Não existe `atomic_replace_durable(namespace,...,component,image)` genericamente chamável. Esse nome/
assinatura fica tombstoned: namespace capability e bytes não são authority para criar temp ou trocar
entry. Control publication usa exclusivamente a primitive module-private
`_publish_control_state_durable(namespace,parent_handle,target_component,image,permit)`.

`ControlTargetKindV1` é u8: `INVALID=0`, `BOOTSTRAP_SLOT=1`, `CHECKPOINT_INDEX_SLOT=2`,
`COMMIT_STATE=3`; 4..255 recusam. Cada kind aceita somente, respectivamente,
`.okto-grafx-v7.bootstrap.{0,1}` na root, `control/checkpoint.index.{0,1}` ou
`control/commit.state`, com decoder/
magic próprio. Prefixo `indexes/`, heap, catálogo, WAL, carrier, reservation ou qualquer outro
component recusa antes do temp. A primitive exige uma das classes concretas exact-type, noncopyable e
single-use `_BootstrapMarkerPublicationPermit`, `_CheckpointStatePublicationPermit` ou
`_CommitStatePublicationPermit`; base class, subclasse, duck type e permit de target diferente recusam.

Cada permit é bound a manager/PID/process-birth/runtime, `OperationBudget`/`OperationAttempt`, stable
namespace/root+parent identity, target kind/component, conjunto de authorities que permanecerá vivo,
replace-target absent ou exact old identity/raw SHA, successor phase/Q/P, exact image length/raw SHA,
`GenerationSuccessorSetV1` claim, namespace mutation projection e plano de data+parent barrier/direct
reopen. Nenhum campo de path/image é caller-controlled depois da emissão. Permit não autoriza
data/index/heap create, nem é conversível em `_BootstrapArtifactPermit` ou `_IrrevocableWalPermit`.

Publicação control tem uma derivação acíclica e reconstruível. `successor_payload` são os bytes
semânticos do codec específico do target **antes** do witness e do checksum exterior. O core abaixo
não contém raw SHA/length da imagem exterior, SHA do pending/temp, file identity futura nem permit:

`RelativeComponentV1 := byte_length u16 || ascii_bytes[byte_length]`, com length 1..255 e somente
`[a-z0-9._-]`; `.`/`..`, uppercase, NUL/control, slash/backslash, colon/ADS, trailing dot/space,
Windows device name e byte fora de ASCII recusam. `CanonicalRelativePathV1 := component_count u16 ||
RelativeComponentV1[component_count]`, count 1..64 e total até 4096 bytes. Não há normalização
silenciosa: qualquer spelling não canônico recusa, e backend case-insensitive ainda precisa provar a
mesma namespace key/no alias. No core, `target_component` e o temp são exatamente um
`RelativeComponentV1` relativos ao parent handle bound pelo target kind; o body CONTROL_FILE usa um
`CanonicalRelativePathV1`. Namespace projection/pending usam os mesmos bytes, nunca string host ou
length implícito.

```text
ControlPublicationCoreV1 = (
  magic=MAGIC_CONTROL_PUBLICATION_CORE_V1, version u16=1, record_length u32,
  target_kind u8, transition_kind u8, expected_old_present u8, reserved_zero u8,
  authority_count u16, reserved_zero u16,
  database_uuid[16], stable_namespace_binding_sha256[32],
  publication_incarnation_identity[32], namespace_mutation_projection_sha256[32],
  target_component, expected_old_file_identity[32], expected_old_raw_sha256[32],
  successor_semantic_generation u64, successor_horizon u64,
  successor_semantic_state_sha256[32], successor_payload_length u32,
  successor_payload_sha256[32], composite_plan_core_sha256[32],
  authorities[authority_count], crc32c u32
)
authority := (
  authority_kind u8, reserved_zero[3], body_length u32,
  body_sha256[32], body[body_length]
)
control_publication_core_sha256 = SHA256(DOMAIN_CONTROL_PUBLICATION_CORE_V1 || canonical core bytes)

ControlPublicationWitnessV1 = (
  magic=MAGIC_CONTROL_PUBLICATION_WITNESS_V1, version u16=1, core_length u32,
  core_bytes[core_length], control_publication_core_sha256[32]
)
final_image = target_codec_prefix(successor_payload) || LE32(length(witness)) ||
              witness || LE32(crc32c(all preceding final-image bytes))

PendingControlPublicationV1 = (
  magic=MAGIC_PENDING_CONTROL_PUBLICATION_V1, version u16=1, record_length u32,
  target_kind u8, transition_kind u8, reserved_zero u16,
  database_uuid[16], publication_incarnation_identity[32],
  namespace_mutation_projection_sha256[32], control_publication_core_sha256[32],
  target_component_length u16, temp_component_length u16,
  final_image_length u32, final_image_raw_sha256[32],
  target_component_bytes[target_component_length],
  temp_component_bytes[temp_component_length], crc32c u32
)
pending_control_publication_sha256 = SHA256(DOMAIN_PENDING_CONTROL_PUBLICATION_V1 || canonical pending bytes)
temp_component = ".okctl7." || database_uuid_hex || "." || publication_incarnation_hex || ".tmp"
```

Pending record length/CRC/EOF e dois component encodings são exatos; target/transition obey a matriz,
incarnation/projection/core/final SHA são nonzero e final length é 1..target-specific MAX. O temp
component precisa ser byte-identical à fórmula e o target ao allowlist do kind. O pending não é um
arquivo separado obrigatório: é a preimage canônica reconstruída de core+temp/target e persistida no
witness/temp bytes; onde materializado como record, usa exatamente este codec. Unknown/extra field,
component swap, CRC stale ou mesmo final SHA sob incarnation distinta recusa.

`target_component`, cada authority body e seus counts/lengths usam o codec da seção 0.1;
authorities são estritamente ordenadas por `(authority_kind,body_sha256,body bytes)` e únicas.
`ControlAuthorityKindV1` é `u8`: `INVALID=0`, `NAMESPACE_SOURCE=1`, `CONTROL_FILE=2`,
`WAL_PUBLICATION_FRONTIER=3`, `DEVICE_APPLY_TARGET_SET=4`; 5..255 recusam. Não há `body bytes`
opaco: o decoder escolhe exatamente um dos quatro codecs abaixo pelo kind antes de calcular o core.
`MAX_CONTROL_AUTHORITY_BODY_BYTES_V1=67108864`; record/count/entry length checked precisa caber nesse
limite e igualdade ao EOF do body, incluindo CRC.

Todo body começa com `magic[8], version u16=1, authority_kind u8, authority_role u8,
record_length u32` (16 bytes) e termina em CRC32C u32 sobre todos os bytes anteriores. O kind do header,
wrapper e magic precisam concordar. `ControlAuthorityRoleV1` é u8 fechado:
`INVALID=0`, `GENESIS_NAMESPACE=1`, `BOOTSTRAP_ARTIFACT_NAMESPACE=2`,
`BOOTSTRAP_PREDECESSOR=3`, `CHECKPOINT_PREDECESSOR=4`, `COMMIT_STATE_PREDECESSOR=5`,
`BASELINE_CHECKPOINT=6`, `CHECKPOINT_CANDIDATE=7`, `BOUND_CHECKPOINT=8`,
`CURRENT_WAL_FRONTIER=9`, `CURRENT_DEVICE_TARGETS=10`; 11..255 recusam. Roles 1–2 só aceitam kind 1,
3–8 só kind 2, 9 só kind 3 e 10 só kind 4. O wrapper `body_sha256` é, respectivamente,
`SHA256(DOMAIN_NAMESPACE_AUTHORITY_V1||body)`, `SHA256(DOMAIN_CONTROL_AUTHORITY_V1||body)`,
`SHA256(DOMAIN_WAL_FRONTIER_AUTHORITY_V1||body)` ou
`SHA256(DOMAIN_DEVICE_AUTHORITY_V1||body)`; SHA genérico, domain trocado ou CRC stale
recusa.

```text
NamespaceSourceAuthorityV1 = (
  common_header(magic=MAGIC_NAMESPACE_AUTHORITY_V1, kind=1, role=1|2),
  database_uuid[16], source_kind u8, inventory_encoding_mode u8,
  manifest_source_kind u8, backend_kind u8, access_policy u8, reserved_zero[3],
  stable_namespace_binding_sha256[32], namespace_mutation_projection_sha256[32],
  root_directory_identity[32], trusted_anchor_directory_identity[32],
  backend_identity_length u16, volume_identity_length u16,
  parent_count u16, final_component_length u16, inventory_count u32,
  inline_inventory_count u32, root_path_length u32,
  manifest_container_identity_sha256[32], manifest_offset u64,
  manifest_length u64, inventory_manifest_sha256[32],
  backend_identity[backend_identity_length], volume_identity[volume_identity_length],
  root_path[root_path_length], final_component[final_component_length],
  parents[parent_count], inventory[inline_inventory_count], crc32c
)
parent := namespace_ancestor_entry do `StableNamespaceBindingProjectionV1`
inventory_entry := (
  entry_kind u8, semantic_state u8, reserved_zero u16,
  path_length u32, path_bytes[path_length], file_identity[32],
  raw_length u64, raw_sha256[32], semantic_generation u64,
  semantic_horizon u64, semantic_state_sha256[32]
)

NamespaceInventoryManifestV1 = (
  magic=MAGIC_NAMESPACE_INVENTORY_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], inventory_count u32, reserved_zero u32,
  inventory[inventory_count], crc32c u32
)
```

`NamespaceSourceKindV1` é `INVALID=0`, `GENESIS_EMPTY_ROOT=1`, `LEGACY_BASELINE=2`,
`BOOTSTRAP_ARTIFACT_SET=3`; 4..255 recusam. Role GENESIS aceita kind 1/2; role ARTIFACT aceita somente
source kind 3. `NamespaceAuthorityInventoryEncodingModeV1` é `INVALID=0`, `INLINE=1`,
`DURABLE_MANIFEST_LOCATOR=2`; 3..255 recusam. `NamespaceAuthorityManifestSourceKindV1` é
`INVALID=0`, `INLINE_SELF=1`, `SUCCESSOR_BOOTSTRAP_PAYLOAD=2`, `RETAINED_BOOTSTRAP_FILE=3`;
4..255 recusam. INLINE exige source 1, inline count==logical count e container/offset/length/manifest
SHA ZERO. LOCATOR exige inline count zero, source 2 ou 3 e nonzero container identity/length/manifest
SHA: source 2 identifica exatamente o successor payload SHA e checked offset/length do manifest
anterior ao core; source 3 identifica o body SHA de uma retained BOOTSTRAP CONTROL_FILE authority no
mesmo core e um span dentro de seu payload. Verifier extrai/redecodifica o
`NamespaceInventoryManifestV1`, prova database/count/SHA e compara cada entry ao direct namespace;
locator SHA isolado não serve. A inequality do witness vale depois dessa escolha.

`NamespaceInventoryEntryKindV1` é `INVALID=0`, `CONTROL=1`, `CARRIER=2`, `WAL=3`, `HEAP=4`,
`CATALOG=5`, `INDEX=6`, `RESERVATION=7`; 8..255 recusam. Backend/volume/final component têm
1..256/1..512/1..255 bytes; parents 0..64, inventory 0..65536, root/path são
`CanonicalRelativePathV1` bytes, component é `RelativeComponentV1`. Backend/access policy usam os
enums exatos do `StableNamespaceBindingProjectionV1`; trusted anchor/root são nonzero. Esses fields
reconstroem byte a byte a projection, incluindo ordinals, e seu digest precisa ser
`stable_namespace_binding_sha256`. Handles/generation/process não entram nesse digest. Parents seguem
root→leaf e identities são nonzero/unique; inventory é sorted/
unique por `(entry_kind,path bytes)`, sem alias de file identity. `NamespaceInventorySemanticStateV1`
é u8: `INVALID=0`, `PRESENT_IMMUTABLE=1`, `LIVE_MUTABLE=2`, `TOMBSTONED_RETAINED=3`,
`SPACE_RESERVATION=4`; 5..255 recusam. A matriz total é: CONTROL aceita 1/2; CARRIER somente 2; WAL
somente 2; HEAP/CATALOG/INDEX aceitam 1/2/3 conforme manifest explícito; RESERVATION somente 4. Não
há state ABSENT: ausência é ausência de entry. Todo state permitido exige file identity/raw SHA/state
SHA nonzero; raw_length pode ser zero apenas para file regular vazio expressamente enumerado e seu
SHA é SHA256(empty), nunca ZERO32. Cross-kind state, INVALID/unknown, tombstone sem retention proof ou
reservation fora do namespace de reserva recusa. Toda authority exige
`namespace_mutation_projection_sha256` nonzero e bit-identical ao
`NamespaceMutationProjectionV1` formado antes do core. `inventory_count`/manifest representam o
**foreign projected inventory**: direct enumeration exclui somente o target component e o único temp
determinístico nomeados nessa projection; qualquer entry excluída precisa ser simultaneamente provada
pelo `ControlPublicationStageProofV1` da mesma projection (identity/length/raw/link-count/state), não é
ignorada por nome. Antes de create ambos estão ausentes; depois de create o temp exact é projetado;
depois de rename o target successor exact é projetado. Segundo temp, target/temp alias, state parcial
não permitido ou SHA da projection divergente recusa. GENESIS_EMPTY_ROOT exige projected
inventory_count=0 e nenhum parent posterior ao root; LEGACY/ARTIFACT exigem inventário foreign
integral direct-certified, nunca subset/hash-only. Assim a recertificação do passo 3 vê o próprio temp
sem alterar a source authority congelada.

```text
ControlFileAuthorityV1 = (
  common_header(magic=MAGIC_CONTROL_FILE_AUTHORITY_V1, kind=2, role=3..8),
  database_uuid[16], target_kind u8, source_codec_kind u8,
  source_lifetime u8, payload_encoding_mode u8, reserved_zero[4], path_length u32,
  file_identity[32], raw_length u64, raw_sha256[32],
  semantic_generation u64, semantic_horizon u64, semantic_state_sha256[32],
  source_control_core_sha256[32], semantic_payload_length u32,
  encoded_payload_length u32, semantic_payload_sha256[32], path_bytes[path_length],
  encoded_payload[encoded_payload_length], crc32c
)
```

`ControlSourceCodecKindV1` é `INVALID=0`, `BOOTSTRAP_V1=1`, `CHECKPOINT_INDEX_V1=2`,
`COMMIT_STATE_V1=3`, `COMMIT_STATE_V2=4`; 5..255 recusam. `ControlSourceLifetimeV1` é
`INVALID=0`, `RETAINED_SLOT=1`, `CONSUMED_BY_EXACT_REPLACE=2`; 3..255 recusam.
`ControlPayloadEncodingModeV1` é `INVALID=0`, `INLINE=1`, `RETAINED_SOURCE_LOCATOR=2`; 3..255
recusam. A matriz total é:

| role | target / codec / lifetime / mode |
|---|---|
| BOOTSTRAP_PREDECESSOR | BOOTSTRAP_SLOT / BOOTSTRAP_V1 / RETAINED_SLOT / RETAINED_SOURCE_LOCATOR |
| CHECKPOINT_PREDECESSOR | CHECKPOINT_INDEX_SLOT / CHECKPOINT_INDEX_V1 / RETAINED_SLOT / RETAINED_SOURCE_LOCATOR |
| COMMIT_STATE_PREDECESSOR | COMMIT_STATE / COMMIT_STATE_V1 ou V2 conforme feature fence / CONSUMED_BY_EXACT_REPLACE / INLINE |
| BASELINE_CHECKPOINT | CHECKPOINT_INDEX_SLOT / CHECKPOINT_INDEX_V1 / RETAINED_SLOT / RETAINED_SOURCE_LOCATOR |
| CHECKPOINT_CANDIDATE | CHECKPOINT_INDEX_SLOT / CHECKPOINT_INDEX_V1 / RETAINED_SLOT / RETAINED_SOURCE_LOCATOR |
| BOUND_CHECKPOINT | CHECKPOINT_INDEX_SLOT / CHECKPOINT_INDEX_V1 / RETAINED_SLOT / RETAINED_SOURCE_LOCATOR |

Qualquer outra tuple recusa. Path é canônico. `semantic_payload_length` vale
0..`MAX_CONTROL_TARGET_STATE_BYTES_V1`; INLINE exige `encoded_payload_length==semantic_payload_length`
e contém esses bytes. LOCATOR exige encoded length zero e o source file/slot exact permanecer retido,
direct-readable e decodificar o payload integral no mesmo offset/codec/hash; não é aceito se o replace
consome o source. Em ambos os modos, checked
`encoded_payload_length <= MAX_CONTROL_AUTHORITY_BODY_BYTES_V1 - fixed_body_bytes - path_length - 4`
e a inequality global do witness precisam passar antes de alocar. O payload redecodifica o prefixo sem witness/outer CRC do codec indicado; seu SHA,
state SHA, generation/horizon e target/role precisam casar. `raw_length/raw_sha` autenticam a imagem
exterior integral observada pre-replace; `source_control_core_sha256` é ZERO somente para baseline v1
pre-cutover, nonzero para V7. RETAINED_SLOT precisa continuar direct-readable; CONSUMED_BY_EXACT_REPLACE
só é permitido para o predecessor do próprio target e é recertificado antes do replace — adoption usa
o body persistido e successor target, não inventa old file identity ainda existente. O body contém a
preimage semântica completa inline ou por locator durável integral, não encadeia recursivamente a
imagem exterior anterior.

```text
WalPublicationFrontierAuthorityV1 = (
  common_header(magic=MAGIC_WAL_FRONTIER_AUTHORITY_V1, kind=3, role=9),
  database_uuid[16], boundary_kind u8, inventory_encoding_mode u8, reserved_zero[6],
  checkpoint_q u64, frontier_p u64, terminal_segment_number u64,
  terminal_record_offset u64, terminal_record_length u32,
  segment_count u32, range_count u32, inline_segment_count u32, inline_range_count u32,
  terminal_epoch u64, terminal_txn_id u64, terminal_transaction_uuid[16],
  terminal_record_raw_sha256[32], terminal_manifest_sha256[32],
  committed_history_root_sha256[32], commit_state_raw_sha256[32],
  stable_wal_root_binding_sha256[32], segment_inventory_sha256[32], range_set_sha256[32],
  segments[inline_segment_count], ranges[inline_range_count], crc32c
)
segment := (
  segment_state u8, reserved_zero[7], segment_number u64, file_identity[32],
  certified_length u64, certified_bytes_sha256[32]
)
range := (segment_number u64, file_identity[32], start u64, end u64, raw_sha256[32])
```

`WalAuthorityInventoryEncodingModeV1` é `INVALID=0`, `INLINE=1`,
`STABLE_WAL_ROOT_LOCATOR=2`; 3..255 recusam. Logical counts são 1..1048576. INLINE exige inline counts
iguais e lists sorted/unique/non-overlapping; LOCATOR exige inline counts zero. Em **ambos** os modes,
stable WAL root binding, segment inventory SHA e range-set SHA são nonzero e recomputados; INLINE não
aceita root binding ZERO/lixo e LOCATOR não aceita inline bytes. Verifier abre a capability WAL no-follow, enumera exatamente os logical
segments/ranges, re-encoda e recomputa inventory/range SHAs; LOCATOR não aceita os SHAs sem essa scan
integral do **prefixo certificado**. Pins/floors mantêm esses bytes até a control publication/adoption.
`WalAuthoritySegmentStateV1` é `INVALID=0`, `CLOSED_EXACT=1`, `ACTIVE_PREFIX=2`; 3..255 recusam.
Entries têm exatamente 88 bytes. Todos os segmentos anteriores ao terminal são `CLOSED_EXACT`:
fresh length precisa ser igual a `certified_length` e SHA cobre o arquivo integral. Exatamente a entry
do `terminal_segment_number` pode ser `ACTIVE_PREFIX`: `certified_length` termina no primeiro byte
após o terminal record P, SHA cobre **somente** `[0,certified_length)`, fresh file identity é igual e
fresh length precisa ser `>= certified_length`. Truncamento, prefix byte divergente, identity trocada,
ACTIVE em outro segment ou dois ACTIVE recusam. CLOSED continua válido se era imutável; se o backend
permite append posterior no mesmo file, o terminal precisa ser ACTIVE mesmo quando o length observado
no momento era exatamente o prefix length.

O inventory desta authority enumera somente os segmentos/ranges necessários para reconstruir e
autenticar `(Q,P]` até esse prefixo; `stable_wal_root_binding_sha256` autentica a directory capability/
namespace, não promete ausência de nomes posteriores. Bytes extras no ACTIVE_PREFIX e segmentos com
número maior não entram no SHA antigo nem são ignorados: adoption, gap recovery e read-view fazem fresh
sealed scan sob `WAL_SCAN_PIN`, desde `certified_length` até o physical tail, e os classificam por
grammar como terminal grouped commits, incomplete ou unknown-required. O witness P pode assim ser
adotado quando um append posterior alongou o mesmo segment, sem transformar extensão legítima em
INCONCLUSIVE; a extensão nunca é usada para fabricar prova de P. LOCATOR retém a lista/preimage
integral desses entries e ranges, não somente seus roots. Append que criaria o segment
1048577 entra em checkpoint/backpressure sob o budget antes do WAL byte; não faz roll irrepresentável.
Boundary usa exatamente `BoundaryKindV1`: BASELINE exige locator/epoch/txn/
UUID/terminal/manifest zeros e `frontier_p==checkpoint_q`; V1 COMMIT exige UUID ZERO e grammar/terminal
v1 e `terminal_manifest_sha256=ZERO32`; V2 exige UUID/manifest nonzero e BEGIN=COMMIT. O terminal range e record SHA precisam apontar ao
último terminal completo P certificado por esta authority, e history root cobre exatamente `(Q,P]`.
Ausência ou presença acima de P nunca é inferida deste witness: vem do fresh sealed scan. Segment header
não herda txn/UUID. Gate obrigatório publica P, appenda BEGIN/effect/COMMIT no mesmo ACTIVE segment e
mata antes/depois de seu WAL barrier e antes do commit.state successor; reopen adota P pelo prefixo e
classifica o sufixo como gap completo/incomplete, sem aceitar full-file SHA antigo nem pular o scan.

```text
DeviceApplyTargetSetAuthorityV1 = (
  common_header(magic=MAGIC_DEVICE_AUTHORITY_V1, kind=4, role=10),
  database_uuid[16], durability_class u8, authority_result u8,
  encoding_mode u8, manifest_source_kind u8, reserved_zero[4],
  source_q u64, target_p u64,
  runtime_predecessor_generation u64, runtime_successor_generation u64,
  target_count u32, step_count u32, inline_target_count u32, inline_step_count u32,
  grouped_manifest_sha256[32],
  apply_plan_sha256[32], barrier_plan_sha256[32], apply_result_sha256[32],
  manifest_container_identity_sha256[32], manifest_offset u64, manifest_length u64,
  manifest_sha256[32],
  targets[inline_target_count], steps[inline_step_count], crc32c
)
target := (
  target_kind u8, barrier_flags u8, reserved_zero u16, target_ordinal u32,
  owner_identity[32], file_identity[32], page_number u64, byte_offset u64,
  byte_length u32, reserved_zero u32, source_effect_raw_sha256[32],
  expected_postimage_sha256[32]
)
step := (
  step_ordinal u32, target_ordinal u32, effect_lsn u64,
  effect_raw_sha256[32], expected_result u8, reserved_zero[7]
)
```

`DeviceTargetDurabilityClassV1` é `INVALID=0`, `APPLY_ONLY=1`, `DURABLE_DIRECT=2`; 3..255 recusam.
`DeviceAuthorityResultV1` é `INVALID=0`, `APPLY_OBSERVED=1`, `DIRECT_CERTIFIED=2`; 3..255 recusam e
casa 1↔1, 2↔2. `DeviceApplyTargetKindV1` é `INVALID=0`, `HEAP_PAGE=1`, `CATALOG_PAGE=2`,
`INDEX_PAGE=3`, `HEAP_CONTROL=4`, `INDEX_CONTROL=5`, `META_CONTROL=6`; 7..255 recusam.
`DeviceApplyExpectedResultV1` é `INVALID=0`, `APPLIED_EXACT=1`, `ALREADY_EXACT=2`,
`SUPERSEDED_COMMITTED=3`; 4..255 recusam. Os valores 1/2/3 são bijetivos a
`ApplyPageImageResultV1` APPLIED/IDENTICAL/SUPERSEDED_COMMITTED; CONFLICTING_EQUAL_LSN e
UNPROVEN_FUTURE jamais são publicáveis. Colapsar 3 em 2 altera result SHA e recusa.
`DeviceAuthorityEncodingModeV1` é `INVALID=0`, `INLINE=1`, `DURABLE_MANIFEST_LOCATOR=2`,
`RETAINED_WAL_DERIVATION=3`; 4..255 recusam. `DeviceAuthorityManifestSourceKindV1` é
`INVALID=0`, `INLINE_SELF=1`, `SUCCESSOR_CHECKPOINT_PAYLOAD=2`, `RETAINED_WAL_FRONTIER=3`,
`RETAINED_CONTROL_FILE=4`; 5..255 recusam.

INLINE exige source INLINE_SELF, inline counts iguais aos logical counts e container identity/offset/length/
manifest SHA zeros. DURABLE_MANIFEST_LOCATOR aceita source 2 ou 4, inline counts zero e identifica
exact container identity nonzero, checked positive byte offset/length e SHA de um
`CheckpointDeviceApplyManifestV1` integral; source 2 usa como container identity exatamente o
`successor_payload_sha256` e aponta dentro desse payload **anterior ao core**, source 4 usa exatamente
o `body_sha256` de uma CONTROL_FILE authority RETAINED no mesmo core e aponta dentro do file payload
direct-readable. RETAINED_WAL_DERIVATION aceita somente source 3, inline counts zero, container
identity igual exatamente ao `body_sha256` da única `WalPublicationFrontierAuthorityV1` do mesmo core,
`manifest_offset=manifest_length=0` (não existe byte span multi-file fictício) e manifest SHA nonzero
é o canonical virtual device manifest que a recipe v1 rederiva do stable WAL root + inventory/range
roots daquela authority. Nenhum mode permite container criado pelo próprio witness, field não
aplicável nonzero ou ZERO32 em field obrigatório.
Em mode 2/3, verifier/recovery deve extrair ou derivar a lista completa, provar counts, re-encode e
recomputar os quatro SHAs antes de publication/adoption; locator unreadable ou WAL reciclado recusa.
`barrier_flags` usa somente bits DATA=0x01, PARENT_NAMESPACE=0x02, DIRECT_READ=0x04; bit desconhecido
recusa. Counts são 0..262144 e inline entries sorted/unique por ordinal, dense desde zero; cada step referencia
um target, WAL effect e result exatos, e cada target é coberto ao menos uma vez salvo target control
explicitamente enumerado pelo transition. Byte range checked não pode cruzar file/page. Os quatro
SHAs são recomputados como `ExactEvidenceReceiptV1` kinds 6/7/8 sobre os mesmos target/step vectors:
apply-plan e barrier-plan projetam expected pre/post/flags, apply-result projeta cada result tag 1/2/3
e direct observed postimage. `grouped_manifest_sha256` continua o digest do manifest nested integral.
Receipt kind/entries/subject/runtime divergente, result omitido ou SHA sem direct preimage recusa;
nenhum dos três é aceito opacamente. Runtime predecessor/
successor são ZERO fora de COMMIT_RECOVERY_ADVANCE; nessa transition são nonzero e successor é checked
predecessor+1. APPLY_ONLY autentica o resultado observado pelo publicador, **não** afirma persistência
após power loss; DURABLE_DIRECT exige cada flag/barrier/direct postimage. Nenhum body admite callback,
cache-only proof, trailing bytes ou SHA sem as entries/preimages que o produzem.

`ControlTransitionKindV1` é `u8`: `INVALID=0`, `FIRST_PREPARING=1`, `BOOTSTRAP_PHASE=2`,
`CHECKPOINT_CANDIDATE=3`, `COMMIT_GENESIS=4`, `COMMIT_ADVANCE=5`,
`COMMIT_CHECKPOINT=6`, `CHECKPOINT_BASELINE=7`, `COMMIT_RECOVERY_ADVANCE=8`; 9..255 recusam. A matriz total é:

| Target/transition | Authorities obrigatórias, sem extras |
|---|---|
| BOOTSTRAP/FIRST_PREPARING | NAMESPACE_SOURCE(GENESIS_NAMESPACE); old absent; phase PREPARING/gen 1 |
| BOOTSTRAP/BOOTSTRAP_PHASE→PREPARED | CONTROL_FILE(BOOTSTRAP_PREDECESSOR) + NAMESPACE_SOURCE(BOOTSTRAP_ARTIFACT_NAMESPACE) com artifacts direct-valid |
| BOOTSTRAP/BOOTSTRAP_PHASE→FENCED | CONTROL_FILE(BOOTSTRAP_PREDECESSOR) + WAL_PUBLICATION_FRONTIER(CURRENT_WAL_FRONTIER) do fence + DEVICE_APPLY_TARGET_SET(CURRENT_DEVICE_TARGETS,DURABLE_DIRECT) de meta/C |
| BOOTSTRAP/BOOTSTRAP_PHASE→BOUND | CONTROL_FILE(BOOTSTRAP_PREDECESSOR) + CONTROL_FILE(BOUND_CHECKPOINT) + WAL_PUBLICATION_FRONTIER(CURRENT_WAL_FRONTIER) L_terminal + DEVICE_APPLY_TARGET_SET(CURRENT_DEVICE_TARGETS,DURABLE_DIRECT) final |
| CHECKPOINT/CHECKPOINT_BASELINE | CONTROL_FILE(BOOTSTRAP_PREDECESSOR) + NAMESPACE_SOURCE(BOOTSTRAP_ARTIFACT_NAMESPACE); target old absent; baseline Q exato |
| CHECKPOINT/CHECKPOINT_CANDIDATE | CONTROL_FILE(CHECKPOINT_PREDECESSOR) + CONTROL_FILE(COMMIT_STATE_PREDECESSOR) + WAL_PUBLICATION_FRONTIER(CURRENT_WAL_FRONTIER,P) + DEVICE_APPLY_TARGET_SET(CURRENT_DEVICE_TARGETS,DURABLE_DIRECT,Q) |
| COMMIT_STATE/COMMIT_GENESIS | CONTROL_FILE(BOOTSTRAP_PREDECESSOR) + CONTROL_FILE(BASELINE_CHECKPOINT); old absent; P=Q=CSN=0 |
| COMMIT_STATE/COMMIT_ADVANCE | CONTROL_FILE(COMMIT_STATE_PREDECESSOR) + WAL_PUBLICATION_FRONTIER(CURRENT_WAL_FRONTIER,Pnew) + DEVICE_APPLY_TARGET_SET(CURRENT_DEVICE_TARGETS,APPLY_ONLY,Pnew); Pnew>Pold, Qnew=Qold |
| COMMIT_STATE/COMMIT_CHECKPOINT | CONTROL_FILE(COMMIT_STATE_PREDECESSOR) + CONTROL_FILE(CHECKPOINT_CANDIDATE) + WAL_PUBLICATION_FRONTIER(CURRENT_WAL_FRONTIER,Pold) + DEVICE_APPLY_TARGET_SET(CURRENT_DEVICE_TARGETS,DURABLE_DIRECT,Qnew); P/CSN iguais, Qnew>Qold |
| COMMIT_STATE/COMMIT_RECOVERY_ADVANCE | CONTROL_FILE(COMMIT_STATE_PREDECESSOR) + mesmas WAL/device authorities do advance, mais runtime predecessor/successor `r→r+1` no device body; Pnew>=Pold e completion exata |

Para o device body, FENCED aceita INLINE (meta/C bounded) ou RETAINED_WAL_DERIVATION; BOUND exige
DURABLE_MANIFEST_LOCATOR para o BOUND_CHECKPOINT; CHECKPOINT_CANDIDATE exige locator source
SUCCESSOR_CHECKPOINT_PAYLOAD; COMMIT_ADVANCE aceita INLINE se a inequality do witness passa, senão
RETAINED_WAL_DERIVATION; COMMIT_CHECKPOINT exige locator para CHECKPOINT_CANDIDATE; e
COMMIT_RECOVERY_ADVANCE exige RETAINED_WAL_DERIVATION. Source/mode diferente é cross-transition e
recusa. O WAL authority correspondente é obrigatório enquanto source 3 existir; o CONTROL_FILE role
correspondente é obrigatório para source 4.

Bootstrap phase incompatível, count/kind extra/ausente, combined P+Q advance, checkpoint sem candidate,
commit sem terminal/device target set ou baseline fora da janela fechada recusam. Para authorities kind 3,
os raw terminal ranges ficam fisicamente retidos por `Q<=Pold<Pnew` até a publicação ser certificada;
kind 4 é validado bijetivamente contra manifest/WAL; somente DURABLE_DIRECT exige ainda direct reads
no publish attempt, sem substituir a classificação cold abaixo. Assim nenhuma authority lógica depende do mutable tail depois
que outro append seja permitido.

`expected_old_present=0` exige ambos os
old fields ZERO; `=1` exige ambos nonzero. O witness inteiro fica dentro dos bytes autenticados pelo
outer checksum/raw SHA do bootstrap slot, checkpoint slot ou commit.state. Decoder sempre extrai a
preimage, recomputa core, payload, final image e pending; aceitar somente o SHA sem a preimage é
proibido. Nenhum digest final entra em sua própria preimage.

`publication_incarnation_identity` é 32 bytes CSPRNG capturados detached antes das sections no primeiro
planejamento daquele successor e duplicate-checked contra targets/temps válidos. Retry/resume do mesmo
plano preserva essa identity; um novo plano lógico, permitido somente quando nenhum temp/target
successor existe, captura outra. Ao encontrar temp ou target successor, um novo
`OperationAttempt` **adota explicitamente** a incarnation persistida e recebe permit/claim novos — não
finge ser o attempt criador. Budget/attempt/permit nonce continuam fora do core/imagem.

```text
NamespaceMutationProjectionV1 = (
  magic=MAGIC_NAMESPACE_MUTATION_PROJECTION_V1, version u16=1, target_kind u8, transition_kind u8,
  record_length u32, database_uuid[16], stable_namespace_binding_sha256[32],
  publication_incarnation_identity[32], target_transition u8, temp_policy u8,
  expected_old_present u8, reserved_zero[5],
  target_component_length u16, temp_component_length u16, foreign_entry_count u32,
  expected_old_file_identity[32], expected_old_raw_sha256[32],
  successor_semantic_generation u64, successor_horizon u64,
  successor_semantic_state_sha256[32], successor_payload_length u32,
  successor_payload_sha256[32], foreign_inventory_sha256[32],
  target_component_bytes[target_component_length],
  temp_component_bytes[temp_component_length], crc32c u32
)
namespace_mutation_projection_sha256 = SHA256(DOMAIN_NAMESPACE_MUTATION_PROJECTION_V1 || canonical bytes)

ControlPublicationStageProofV1 = (
  magic=MAGIC_CONTROL_STAGE_PROOF_V1, version u16=1, record_length u32,
  namespace_mutation_projection_sha256[32], observed_target_state u8,
  observed_temp_state u8, reserved_zero[6],
  target_file_identity[32], target_length u64, target_raw_sha256[32],
  temp_file_identity[32], temp_length u64, temp_raw_sha256[32],
  foreign_inventory_sha256[32], parent_chain_proof_sha256[32], crc32c u32
)
```

`NamespaceTargetTransitionV1` é `INVALID=0`, `ABSENT_TO_SUCCESSOR=1`,
`PREDECESSOR_TO_SUCCESSOR=2`; 3..255 recusam e casa com expected-old 0/1. `NamespaceTempPolicyV1` é
`INVALID=0`, `ABSENT_PARTIAL_EXACT_ABSENT=1`; 2..255 recusam. `ControlEntryObservedStateV1` é
`INVALID=0`, `ABSENT=1`, `PARTIAL=2`, `EXACT_PREDECESSOR=3`, `EXACT_SUCCESSOR=4`; 5..255 recusam.
ABSENT exige identity/length/SHA zeros; qualquer state presente exige identity/SHA nonzero e PARTIAL
jamais vale para target. Component lengths igualam exatamente dois `RelativeComponentV1`, 3..257
bytes cada. Expected-old zero/nonzero, target/transition/component allowlist, counts, payload bound,
foreign SHA e CRC são validados antes do projection SHA; stage proof é ephemeral/exact-type e seu SHA
é `SHA256(DOMAIN_CONTROL_STAGE_PROOF_V1||bytes)`.

`NamespaceMutationProjectionV1` é formado **antes** do core e evita comparar um alias inventory cru
com a própria mutação. Ele fixa target kind, stable namespace, incarnation, target/temp components,
expected-old fields, successor semantic state/payload SHA e `foreign_inventory_sha256`, calculado
depois de excluir **somente** `target_component` e o único `temp_component` determinístico. Ele não
contém core SHA, pending SHA, final-image SHA nem witness. Assim não existe ciclo
projection -> core -> final -> pending. A projection fixa as transições permitidas
`target: predecessor|absent -> exact successor` e `temp: absent -> partial|exact -> absent`, com
os hashes finais adicionados apenas ao `ControlPublicationStageProofV1`. Antes de create, antes de replace e depois
do barrier, todas as entries estrangeiras precisam produzir o mesmo SHA e não podem aliasar target/
temp; as duas entries próprias recebem um `ControlPublicationStageProofV1` com file identity, length,
raw SHA, regular/no-reparse/link-count-one e estado exato. Inode novo do próprio target e o próprio
temp não são drift; entry extra, hardlink/junction/reparse, foreign alias, segundo temp, component
trocado ou qualquer mudança estrangeira são INCONCLUSIVE antes de nova mutação.

BOOTSTRAP/CHECKPOINT usam suas publication generations reais e exact `GenerationSuccessorSetV1`
claims, salvo exclusivamente `FIRST_PREPARING` 0→1 coberto pela
`BootstrapInitializationExceptionV1`; todo phase successor usa claim. COMMIT_STATE exige
`successor_semantic_generation=0`, não possui predecessor/successor counter
e usa state SHA + old/new P/Q no payload/core; nenhum step/headroom de generation é criado para ele.
Catálogo nunca é target dessa primitive e também não possui generation V7 inventada.
`authority_count >= 1`; CONTROL_FILE é entry separada do target e permanece durável durante a troca,
enquanto WAL/DEVICE/NAMESPACE bodies identificam preimages que continuam diretamente recertificáveis.
O successor generation/state deve ser a única transição autorizada a partir dessas authorities; old
target identity é a CAS histórica pré-replace, não uma authority que se exige reler depois que foi
substituída.

A primitive executa somente esta máquina:

1. sob exact permit/claim, reler authorities, predecessor target, projection estrangeira e qualquer
   temp determinístico. Se target já é o successor, seguir obrigatoriamente o passo 4; se não é nem o
   predecessor exato nem o successor exato, recusar INCONCLUSIVE;
2. com predecessor ainda current, se temp ausente, create-exclusive, escrever exatamente
   `final_image`, data barrier e cold hash; se existe completo/bit-identical do mesmo pending, repetir
   barrier/cert e reutilizar. Temp parcial/divergente nunca é truncado/overwrite;
3. recertificar atomicamente authorities, predecessor, successor set e projection; fazer replace
   handle-relative do temp para target, sem publicar outro successor, e seguir ao passo 4;
4. para target successor já instalado, decodificar o witness, reconstruir core/final/pending,
   recertificar todas as authorities ainda vivas, validar a transição target-kind e a projection
   estrangeira, repetir parent namespace barrier, cold reopen e exact file identity/length/decoder/
   CRC/raw SHA. Esse branch **não** exige a identidade física old já destruída e nunca recria temp;
5. consumir o permit somente depois dessa direct certification. Temp duplicado, se houver, é removido
   por permit de disposition separado depois da certificação do successor.

Assim, kill logo após rename, depois do parent barrier ou depois do primeiro cold cert encontra no
target a intenção/preimage completa, repete barrier/cert e adota o mesmo successor idempotentemente.
Kill anterior encontra predecessor + temp classificado. Não existe branch que trate simples nome/
generation como prova.

Rename não é ainda exposição. BOOTSTRAP permanece sob a maintenance session exclusiva; CHECKPOINT e
COMMIT_STATE retêm writer lease+COMMIT desde a revalidação pré-replace até o fim do passo 5. Nenhum
outro writer entra nessa janela. Se o owner morre, o lock OS é liberado, mas o **primeiro** participante
seguinte sob COMMIT deve executar `ControlSuccessorAdoptionGateV1`: direct-read do target, branch 4,
barrier/cold cert e recertificação da matriz de authorities antes de append, apply, checkpoint,
recovery ou nova control publication. `begin` também executa esse gate dentro de seu sandwich COMMIT
antes de usar P/Q. Sucesso produz somente `ControlImageAdoptionProofV1(target file identity/raw SHA,
core/witness SHA, namespace proof, transition, P,Q,WAL-frontier evidence)`. Essa proof module-private
autentica a imagem control; não contém/não pode ser convertida em `DeviceMaterializationProof`,
`RuntimeDeviceReplayProof`, `WriteSnapshotProof` ou snapshot público.

Para COMMIT_ADVANCE, o gate sela o WAL e exige que o terminal/ranges do authority body ainda sejam o
frontier completo Pnew, sem incomplete/unknown-required acima, e que APPLY_TARGET_SET seja bijetivo aos
efeitos — não que eles sobreviveram power loss. Para COMMIT_CHECKPOINT, exige candidate/composite
bit-identical, frontier Pold ainda completo e target set de Qnew. Se já existe COMMIT durável acima do
P do successor, o gate não tenta recertificar witness antigo como current: entra no gap flow antes de
qualquer snapshot/write. Cache de certificação vale apenas para a mesma
`(target_file_identity,raw_sha,core_sha,database_runtime_generation)` e é revogado por foreign inode,
WAL generation ou runtime change. Assim um append futuro não precisa preservar eternamente authorities
de um control state já superseded, mas nunca pode correr antes de certificar o current.

Depois da control adoption, uma classificação **separada** deriva de checkpoint Q + toda grouped
history `(Q,P]` o conjunto final completo, não apenas o último commit. Somente `P==Q` pode produzir
diretamente `RuntimeDeviceReplayProofV1(CHECKPOINT_DIRECT)`: exige
`DeviceApplyTargetSetAuthorityV1(DURABLE_DIRECT)` do checkpoint e seu exact direct-result set cobrindo
bijetivamente o target set do próprio Q. Não existe `DeviceMaterializationProofV1`, alias intermediário
ou conversão de WAL/control proof em device proof.

Em cold `P>Q`, a classificação é sempre `CATCH_UP_REQUIRED`, inclusive quando todas as postimages
parecem exactas no direct read: commits ordinários não fizeram data/namespace barriers e V7 não
transforma observação pós-crash em proof durável. A authority concede somente a capacidade interna
para `ColdReplayPlan`/catch-up da seção 10.5, mantendo begin/snapshot/append/DDL bloqueados. Missing
page é trabalho do mesmo replay; target diferente sem explicação committed é
CONFLICTING/INCONCLUSIVE. Catch-up reaplica/classifica todos os efeitos, registra
`CommitRedoResultV1`, faz os barriers de todos os touched pages/files e só então produz
`RuntimeDeviceReplayProofV1(REPLAY)`. Portanto power loss após COMMIT_ADVANCE pode adotar P e reparar
Q→P sem circularmente exigir a proof que o replay ainda criará, mas jamais ganha CHECKPOINT_REBASE:
esse kind exige a predecessor process-local retida antes do crash.

Para `COMMIT_RECOVERY_ADVANCE`, stage 4 só termina quando o carrier publica/direct-certifica também o
runtime successor `r+1` bound no witness. Se crash deixa commit.state successor com carrier ainda em
r, o próximo gate sob fresh recovery lease+COMMIT completa exatamente `r→r+1` antes de expor P ou
permitir append; outro successor, MAX ou body divergente recusa. Se carrier já está em r+1, adota
idempotentemente. Não existe intervalo release→runtime-CAS.

Temp parcial, obsoleto ou duplicado só muda por `_ControlTempDispositionPermit` exact-type, bound ao
pending/target kind, source component/file identity/observed length+raw SHA, core/incarnation,
authorities/target current, projection estrangeira e ação `DELETE_PARTIAL_RETRY=1`,
`DELETE_ALREADY_PUBLISHED=2` ou `QUARANTINE_OBSOLETE=3`, além dos barriers source/target. Remoção/
rename é handle-relative e parent-barriered. Se seu predecessor ainda é current, um pending completo
de outro image deve ser concluído ou classificado INCONCLUSIVE; não pode ser apagado para pular
generation. Se target já é successor bit-identical, o temp duplicado pode ser apagado depois do passo
4; se predecessor foi validamente superseded, pode ser quarantined. Cross-kind permit ou disposition
de outro target falha antes de unlink/rename/temp.

O primeiro PREPARING usa `_FirstBootstrapMarkerPublicationPermit`, concrete subtype que aceita somente
BOOTSTRAP_SLOT/phase PREPARING/generation 1/old absent. Ele nasce aciclicamente de
`GenesisCreationSession(root binding atual, frozen DB UUID/manifests/capacity)` ou
`OfflineMaintenanceSession(legacy baseline B + feasibility)`; não exige marker anterior nem contém
seu SHA. Crash pre-marker com o único temp reserved completo permite `resume_genesis/cutover` redecodificar
o PREPARING integral, recertificar source/root e emitir permit novo para o mesmo pending. Temp reserved
parcial não finge possuir core/pending/authorities que nunca chegaram ao disco. Ele usa a única exceção
estreita abaixo; entry não-reserved continua `GENESIS_ORPHANED/INCONCLUSIVE`.

```text
FirstMarkerOrphanPartialTempProofV1 = (
  magic=MAGIC_GENESIS_ORPHAN_PROOF_V1, version u16=1, record_length u32,
  bootstrap_kind u8, source_kind u8, reserved_zero u16,
  trusted_parent_capability_sha256[32], current_root_capability_sha256[32],
  requested_root_component, root_directory_identity[32],
  reserved_temp_component, parsed_candidate_database_uuid[16],
  parsed_publication_incarnation_identity[32],
  source_file_identity[32], source_length u64, source_raw_sha256[32],
  first_marker_target_component, first_marker_absence_proof_sha256[32],
  expected_baseline_inventory_count u32, observed_foreign_entry_count u32,
  expected_baseline_inventory_sha256[32], observed_foreign_inventory_sha256[32],
  cutover_feasibility_digest[32],
  classification u8, reserved_zero[3], crc32c u32
)
first_marker_orphan_partial_temp_proof_sha256 =
  SHA256(DOMAIN_GENESIS_ORPHAN_PROOF_V1 || canonical proof bytes)
```

`FirstMarkerOrphanPartialClassificationV1` é u8: `INVALID=0`, `TRUNCATED_OR_PARTIAL=1`,
`FULL_LENGTH_INVALID=2`; 3..255 recusam. O component precisa casar exatamente
`.okctl7.<32 lowercase hex DB UUID>.<64 lowercase hex incarnation>.tmp`; os valores parseados são
candidatos de namespace, não uma identidade DB já publicada. A proof nasce de handles atuais
no-follow para trusted parent/root/temp, direct-read integral limitada por
`MAX_CONTROL_TARGET_STATE_BYTES_V1`, e prova simultaneamente: nenhum marker target válido ou inválido,
nenhum segundo temp, root sem alias/reparse/hardlink e inventário estrangeiro exatamente classificado.
V7_GENESIS exige source GENESIS_EMPTY_ROOT, expected/observed counts zero, ambos inventory SHAs e
feasibility ZERO. LEGACY_CUTOVER exige source LEGACY_BASELINE, `OfflineMaintenanceSession` ainda
exclusiva, feasibility digest nonzero e expected count/SHA/preimage integral do manifest; o inventário
observado exclui somente o reserved temp e precisa ser bit-identical ao baseline heap/catalog/WAL/
índices, inclusive com 0/1/N índices. Entry adicional, ausente ou alterada recusa. Length menor que
`MIN_V7_BOOTSTRAP_STATE_BYTES_V1`, EOF antes do `record_length`, short read, CRC/body/witness inválido ou imagem full-length que
não re-encode bit-identical recebe uma das duas classifications. Uma imagem PREPARING integralmente
válida é **proibida** nesta proof e só segue o resume normal acima.

`_FirstMarkerOrphanTempDispositionPermit` é module-private/exact-type, single-use e ligado à proof raw,
root+parent handles/identities, source file identity/length/hash, component, operation budget/attempt e
ação `QUARANTINE_EXACT_ORPHAN=1` ou `DELETE_EXACT_ORPHAN_AFTER_EXPORT=2`; 0 e 3..255 recusam. Ele é
emitido somente por `resume_first_marker_dispose_orphan`, depois de recertificar a proof e exigir confirmação
operator explícita da ação. QUARANTINE move handle-relative para um evidence root estável fora do DB,
com target create-exclusive, data+ambas parent barriers e receipt hash; DELETE exige receipt de export
bit-identical já durable e faz unlink+parent barrier. Em ambos os casos, a porta aceita somente o exact
reserved temp e jamais autoriza target marker, control/data/index/heap/WAL/carrier, adoption ou
publication. DELETE_AFTER_EXPORT é permitido somente para V7_GENESIS; LEGACY_CUTOVER aceita apenas
QUARANTINE e nunca remove/troca uma baseline entry. Depois, GENESIS direct-certifica root vazio e pode
iniciar preflight novo; CUTOVER direct-certifica novamente o baseline integral e reinicia feasibility/
PREPARING sem alegar que o temp tinha core. Um byte, todos os comprimentos parciais, bytes aleatórios ou
imagem full-looking mas inválida seguem esta proof; temp non-reserved, dois temps, marker presente,
baseline/source drift ou parent/root swap recusam sem rename/unlink.

```text
CheckpointCompositeSemanticBaseV1 = (
  magic=MAGIC_CHECKPOINT_SEMANTIC_BASE_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], source_checkpoint_generation u64,
  target_checkpoint_generation u64, source_q u64, target_q u64, published_p u64,
  source_checkpoint_file_identity[32], source_checkpoint_raw_sha256[32],
  source_checkpoint_control_core_sha256[32],
  source_commit_state_file_identity[32], source_commit_state_raw_sha256[32],
  source_commit_state_control_core_sha256[32],
  requested_boundary_proof_length u32, target_checkpoint_manifest_length u32,
  target_commit_state_payload_length u32, reserved_zero u32,
  requested_boundary_proof_sha256[32], reader_floor_requirement_lsn u64,
  candidate_database_runtime_generation u64,
  candidate_reader_registry_incarnation_identity[32],
  candidate_reader_registry_snapshot_sha256[32], pending_outer_map_sha256[32],
  active_pending_set_sha256[32], terminal_retention_floor_sha256[32],
  terminal_release_set_sha256[32], target_checkpoint_manifest_sha256[32],
  target_commit_state_payload_sha256[32],
  candidate_publication_incarnation_identity[32],
  commit_state_publication_incarnation_identity[32],
  requested_boundary_proof_bytes[requested_boundary_proof_length],
  target_checkpoint_manifest_bytes[target_checkpoint_manifest_length],
  target_commit_state_payload_bytes[target_commit_state_payload_length], crc32c u32
)
checkpoint_composite_semantic_base_sha256 =
  SHA256(DOMAIN_CHECKPOINT_SEMANTIC_BASE_V1 || canonical bytes)

CheckpointCompositePlanCoreV1 = (
  magic=MAGIC_CHECKPOINT_COMPOSITE_CORE_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], semantic_base_length u32, generation_transition_plan_length u32,
  semantic_base_sha256[32], checkpoint_generation_transition_plan_sha256[32],
  semantic_base_bytes[semantic_base_length],
  generation_transition_plan_bytes[generation_transition_plan_length], crc32c u32
)
checkpoint_composite_plan_core_sha256 = SHA256(DOMAIN_CHECKPOINT_COMPOSITE_CORE_V1 || canonical bytes)

PendingCheckpointPublicationV1 = (
  magic=MAGIC_PENDING_CHECKPOINT_V1, version u16=1, pending_state u8, reserved_zero u8,
  record_length u32, database_uuid[16], composite_core_length u32,
  composite_core_sha256[32], candidate_component_length u16, reserved_zero u16,
  candidate_generation u64, candidate_q u64, candidate_file_identity[32],
  candidate_raw_length u64, candidate_raw_sha256[32],
  candidate_control_core_sha256[32], candidate_manifest_sha256[32],
  current_commit_state_file_identity[32], current_commit_state_raw_sha256[32],
  current_p u64, current_q u64,
  candidate_component_bytes[candidate_component_length],
  composite_core_bytes[composite_core_length], crc32c u32
)
pending_checkpoint_publication_sha256 = SHA256(DOMAIN_PENDING_CHECKPOINT_V1 || canonical bytes)
```

`PendingCheckpointStateV1` é u8 fechado: `INVALID=0`,
`CANDIDATE_AWAITING_COMMIT_STATE=1`; 2..255 recusam. A semantic base exige source/target generation
checked +1, `source_q<=target_q<=published_p`, duas incarnation identities nonzero/distintas,
boundary proof, target manifest e commit-state payload canônicos **embutidos** com lengths/SHAs
recomputados, `reader_floor_requirement_lsn==target_q` e todos os fields bit-identical ao
`CheckpointPreparation`. Ela não contém transition plan/SHA, composite core/SHA, candidate image,
witness, admission, future file identity ou pending SHA.

O core decodifica integralmente uma semantic base e um `GenerationTransitionPlanV1`; DB UUID casa,
`semantic_base_sha256` e `checkpoint_generation_transition_plan_sha256` são os digests registrados dos
bytes embedded, e toda transition entry usa semantic code 2 com
`semantic_plan_source_sha256==semantic_base_sha256`. Target kind/identity/digest é recomputado dos
manifest/payload/source bytes da base. O transition não pode apontar ao composite core que o contém,
nem a candidate/witness/future output. Lengths são nonzero, total checked, CRC/EOF/canonical
re-encode exatos. Mutante que substitui a base SHA por core SHA, inclui transition SHA na base ou
aceita hash sem preimage recusa no decoder.

`MAX_CHECKPOINT_SEMANTIC_BASE_BYTES_V1=268435456`; cada um dos três nested lengths é
1..67108864 e `semantic_base.record_length == 652 + requested_boundary_proof_length +
target_checkpoint_manifest_length + target_commit_state_payload_length <= MAX`. No core,
`semantic_base_length` é 652..MAX, `generation_transition_plan_length` é 1..67108864 e
`core.record_length == 108 + semantic_base_length + generation_transition_plan_length <=335544428`.
Todas as somas são checked antes de slice/allocation; ±1, overflow, nested EOF/trailing, swapped
manifest/payload e canonical re-encode divergente recusam. Golden DAG recomputa na ordem
`requested boundary → prefix progress plan → checkpoint manifest/payload → semantic base → generation
transition → composite core → candidate image → commit.state image`; nenhuma seta inversa é aceita.
Candidate payload é derivado somente depois de codificar esse core. Pending exige o core integral,
component allowlisted, candidate exact decode/raw SHA/generation/Q/manifest e commit.state predecessor
current; não é cache por hash. Record length, maximum target size, component/CRC/EOF e canonical
re-encode são obrigatórios.

Checkpoint é publicação composta na ordem acíclica: `CheckpointCompositeSemanticBaseV1` contém old
active slot+commit.state identities/raw SHA, requested boundary proof, target
`(generation=g+1,Qnew,manifest)` e payload semântico target de commit.state; seu SHA alimenta o stable
transition plan; `CheckpointCompositePlanCoreV1` liga as duas preimages. Nenhuma delas contém witness,
final-image SHA/control-core ou file identity futura. O domain-separated core SHA é o
`composite_plan_core_sha256` nonzero nos dois control cores. O candidato persiste a preimage completa
desse composite core no próprio payload; suas authorities são active slot+old commit.state. Só depois
de derivar/publicar/certificar a imagem final do candidato existe sua raw SHA/file identity, que se
torna a authority única e persistente do control core de commit.state. Este segundo core também exige
que expected-old state e desired P/Q sejam exatamente os do composite core. A ordem é portanto
composite semantic core -> candidate control core/image -> commit.state control core/image, sem aresta
reversa. Kill depois de candidate slot barrier/direct cert e antes de
commit.state **não queima nem renumera** o candidato: recovery/checkpoint toma fresh proofs/floors,
budget/attempt, `GenerationSuccessorSetV1` e admission, revalida predecessor/história/manifest e
finaliza commit.state para esse candidato bit-identical,
desde que a admission/headroom recertificada prove `g+1 < MAX` e preserve o
`post_bound_operational_reserve`. O fresh successor set contém somente o suffix ainda não publicado
do stable transition plan; candidate/control edges já target-exact entram por adoption proof. Um
candidate observado com generation MAX não poderia ter sido
admitido por V7; cold gate o classifica `INCONCLUSIVE(generation_admission_violation)` e não o finaliza.
Candidate same `(generation,Q)` com bytes/manifest diferente, dois
candidates concorrentes ou predecessor mismatch são CORRUPT/INCONCLUSIVE e jamais last-wins/
overwrite. Só commit.state direct-certified torna Q ativo; até lá o slot anterior continua authority.

Cold discovery de um checkpoint candidate válido com generation superior que não casa com o
commit.state current cria obrigatoriamente `PendingCheckpointPublicationV1` derivado do composite core.
O slot/commit.state antigo continua a única authority de leitura e read-only pode devolver o P antigo
somente se frontier/device correntes e seu fresh reader wrapper ainda o certificarem, mas **todo** writer — commit comum,
recovery de gap, novo checkpoint, reconcile, repair ou cutover — recusa
`CHECKPOINT_PUBLICATION_PENDING` antes de WAL/temp/page/commit.state. Sob fresh integrity budget,
lease/floors/guards/COMMIT, a única progressão é recertificar composite, active-old state, candidate,
history e floors e publicar o commit.state target bit-identical; depois do direct cert o gate some.
`candidate_reader_registry_snapshot_sha256`, incarnation e runtime são evidência histórica do seal que
criou o candidate, não precondition bit-identical de resume. Resume lê um snapshot PROCESS **current**:
mesma incarnation é aceita se não há registration com `S<target_q`; incarnation nova exige o restart
certificate OS de zero owner/record da anterior e igualmente nenhum current S abaixo de Q. Runtime
generation pode ser successor por recovery, o que invalida tokens antigos; nunca pode regredir nem
ser reutilizada. Close/owner-death apenas remove registration. Todo begin posterior ao candidate
lineariza com `S>=P>=target_q`; antes de commit.state, resume instala fresh reader floor para target Q
sob o budget atual. Registration current abaixo de Q, restart sem certificate ou reader admission que
contorne o floor é INCONCLUSIVE; snapshot SHA novo por fechamento legítimo não reescreve o core.
Não há rebase, renumeração, abandono do highest generation nem supersession por outro candidate. Estado
insuficiente/divergente permanece INCONCLUSIVE e bloqueia writer. Portanto “usar slot antigo após
crash” significa apenas read authority conservadora, nunca permissão para avançar P ou trocar o inode
de commit.state antes de concluir o candidate.

Falha ou backend sem create/replace/directory durability/conditional exactness deixa a publicação
inconclusiva e bloqueia feature. Direct read anterior ao crash não substitui barrier do diretório.

#### 5.2.1 Namespace filesystem confinado e sem alias

`parent`/`target` acima nunca são strings re-resolvidas. Database root existente precisa obter uma
`StableNamespaceCapabilityV1` pelo caminho completo desde o volume/root confiável. Para genesis com
root ausente existe uma etapa distinta:

```text
StableNamespaceBindingProjectionV1 = (
  magic=MAGIC_STABLE_NAMESPACE_BINDING_V1, version u16=1, backend_kind u8, binding_kind u8,
  record_length u32, database_uuid[16], access_policy u16, ancestor_count u16,
  backend_identity_length u16, volume_identity_length u16,
  root_path_length u16, final_component_length u16,
  ancestors_length u32, root_directory_identity[32],
  trusted_anchor_directory_identity[32], backend_identity[backend_identity_length],
  volume_identity[volume_identity_length], root_path[root_path_length],
  final_component[final_component_length], ancestors[ancestor_count], crc32c u32
)
namespace_ancestor_entry := (
  entry_length u32, ordinal u16, component_length u16,
  directory_identity[32], component_bytes[component_length]
)

StableNamespaceParentCapabilityV1 = (
  backend/volume identity, trusted parent directory identity + live handle,
  ordered ancestor directory identities + live handles,
  absent final_component, no_follow/no_reparse/no_mount_escape guarantees,
  namespace_generation, opened_process_birth_identity
)
StableNamespaceCapabilityV1 = (
  backend/volume identity, database-root directory identity + live handle,
  ordered parent directory identities + live handles,
  no_follow/no_reparse/no_mount_escape guarantees,
  namespace_generation, opened_process_birth_identity
)
```

`NamespaceBackendKindV1` é `INVALID=0`, `POSIX_FILE=1`, `WINDOWS_NT_FILE=2`,
`OBJECT_NAMESPACE_EQUIVALENT=3`, `PROCESS_MEMORY=4`; 5..255 recusam.
`NamespaceBindingKindV1` é `INVALID=0`, `CURRENT_DIRECTORY=1`; 2..255 recusam: V7 nunca persiste uma
directory identity futura. `NamespaceAccessPolicyV1` é u16 `INVALID=0` e
`STRICT_HANDLE_RELATIVE_NOFOLLOW_SINGLE_LINK=1`; 2..65535 recusam. Backend/volume são 1..256/1..512
bytes, root path 1..4096, final component 1..255, ancestor count 0..64 e soma de ancestor entries até
4096 bytes. Ordinals são densos 0..count-1 na ordem do trusted anchor até o parent imediato; cada
component é um `RelativeComponentV1`, cada identity é nonzero/unique no chain e a última tuple mais o
final component rederiva exatamente root path/root identity sob a grammar do backend. Trusted anchor,
root identity, DB UUID e CRC são nonzero/valid; EOF/trailing, path com spelling alternativo,
ancestor reorder/alias, backend-policy impossível ou volume/root mismatch recusa.

`stable_namespace_binding_sha256 = SHA256(DOMAIN_STABLE_NAMESPACE_BINDING_V1 || canonical record
bytes)`. O record é handle-free: não contém PID, process birth, live handle, lease, namespace/runtime
generation, inventory ou marker SHA. `GenesisNamespaceBindingV1` é alias normativo, byte-identical,
de `StableNamespaceBindingProjectionV1`; não há segundo codec. O `root_binding_sha256` de
`GenesisImageManifestV1`, `ArtifactImageVectorV1` e `CarrierManifestV1`, o
`stable_namespace_binding_sha256` de control/namespace authority/projection e cada parent capability
projection aplicável são esse digest exato da projection correspondente. A live capability precisa
reconstruí-la dos próprios handles e comparar byte a byte imediatamente antes de cada effect e após
barrier/direct reopen. Implementações POSIX/Windows diferentes não podem hashear structs nativas,
paths host, handles ou um subset desses fields.

`StableNamespaceParentCapabilityV1` exige lookup handle-relative que prove final component ausente.
Genesis cria esse único diretório create-exclusive pelo trusted parent handle, faz parent namespace
barrier, abre o novo root no-follow, rejeita reparse/mount/alias, direct-certifica sua directory identity
e só então promove para `StableNamespaceCapabilityV1`. Target já existente e vazio começa diretamente
pela capability completa. Não existe capability “root futura” nem identity inventada. A capability é
autoridade live do attempt, não prova durável de continuidade depois que esse processo morre.

Crash depois de mkdir/barrier e antes do primeiro marker final válido perde essa autoridade live. Como
nenhum artefato/DB UUID foi publicado, retry reabre o trusted chain e o final component no-follow e
pode adotar **qualquer** directory identity atualmente vazia, unique/no-alias/no-reparse e
handle-certified. Replacement vazio pré-marker é portanto aceito canonicamente como genesis novo, não
como replay do anterior. Conteúdo, temp, slot parcial, symlink/junction/reparse, alias ou root não
certificável é `GENESIS_ORPHANED/INCONCLUSIVE`, sem cleanup. A publicação durable/direct-certified de
PREPARING grava `GenesisNamespaceBindingV1` no image manifest; a partir desse instante cada open/resume
exige a mesma backend/volume, root, parent chain e final component. Replacement — ainda que vazio —,
rename/swap ou identity desconhecida pós-marker recusa, nunca é adotado.

Cada componente é aberto sem seguir symlink e inspecionado pelo próprio handle. POSIX exige primitive
equivalente a handle-relative `openat/openat2` com `NOFOLLOW`, `RESOLVE_BENEATH` e rejeição de mount/
device escape; Windows exige directory handles/reparse-point inspection e rejeita symlink, junction,
mount point ou qualquer reparse tag em root/ancestrais/parents. Apenas comparar `abspath`, prefixo,
`isfile`, `realpath` ou fazer `root / relative_name` é insuficiente. Backend/API que não oferece
create/open/replace/rename/unlink/barrier handle-relative e race-free recusa V7/cutover/genesis antes
do primeiro efeito com `GrafxUnsupportedOperation(field="stable_namespace")`.

Todo durable entry V7 é regular file, non-reparse e segue policy `link_count == 1`; duas logical names,
dois manifest entries ou dois databases nunca podem compartilhar `(volume_identity,file_identity)`.
Hardlink existente no legado bloqueia PREPARING; hardlink/reparse descoberto no open é
CORRUPT/INCONCLUSIVE, nunca adotado. Diretórios têm sua própria policy de identidade/handle, não a
regra de link-count de regular files. Temps, reservations, quarantine/ledger, WAL segments, control,
heap/catalog e index carriers entram no mesmo alias inventory. Um control temp reserved não é
“extra ignorado”: aparece classificado por `PendingControlPublicationV1`, target/predecessor e exact
file identity/hash; só essa classificação permite resume/disposition sem sinalizar drift.

Cada operação congela parent-chain identities + alias inventory SHA, usa somente relative components
validados (sem vazio, `.`/`..`, separator alternativo, stream/ADS, device/UNC/drive prefix ou trailing
normalization ambígua), revalida handles/identities/no-reparse/link-count imediatamente antes do
primeiro efeito e novamente depois de rename/barrier/direct reopen. Parent/root swap, rename,
mount/reparse injection, file-id reuse ou alias drift revoga capability/runtime generation e recusa/
poison; nunca faz fallback por path textual. Operações já irrevogáveis completam somente no original
handle-confined namespace e depois recusam exposição se a chain pública divergiu.

Assim, `_publish_control_state_durable`, as três create primitives, `WalMutationDraftV1` rename/delete/
quarantine/recycle e todos os direct reads recebem `StableNamespaceCapabilityV1` mais a tuple ordenada
de **todos** os `(parent directory identity, live parent handle, single relative component)` envolvidos.
Rename/quarantine cross-directory usa primitive equivalente a
`renameat(source_parent_handle,source_component,target_parent_handle,target_component)`, autentica
pre/post identity dos dois lados e faz namespace durability barrier em source e target parent quando
distintos; same-parent reduz canonicamente a uma só entry/barrier. Resolver qualquer lado por string ou
usar handle do outro parent recusa. Data barrier do file e todas as parent barriers são parte da proof.
Um backend non-file precisa provar equivalentes object namespace identity, no-alias e conditional
mutation; `:memory:` process-private usa capability local sem path.

Há três primitives de criação disjuntas, todas exigindo a mesma stable namespace capability e sem
overload comum:

- `bootstrap_create_entry_durable(namespace,parent_handle,_BootstrapArtifactPermit,...)` exige marker PREPARING + session
  operator exact-type e só cria entries enumeradas no image/carrier manifest antes da exposição.
  DDL/BIND/recovery de data nunca recebe esse permit;
- `coordinator_create_carrier_durable(namespace,parent_handle,_CoordinatorCarrierPermit,...)` cria exclusivamente carrier
  stable no coordinator namespace sob seu namespace guard; é incapaz de abrir path/device de
  heap/catalog/index e pode deixar apenas carrier órfão inexposto pre-WAL;
- `create_entry_durable_after_wal(namespace,parent_handle,_IrrevocableWalPermit,target_component,image)` é a única porta para
  path IndexV2 ou outro file data/control **transacional** novo. Antes da WAL barrier não existe target
  nem temp. Depois dela, cria temp sibling com nome canônico derivado do frozen WAL plan, escreve a
  imagem integral, faz data barrier, rename atômico para target comprovadamente ausente,
  parent-directory barrier e cold reopen. Target já existente só é aceito como redo bit-identical do
  mesmo plan/manifest; mismatch recusa. Crash nunca deixa target final parcial.

`_IrrevocableWalPermit` é module-private exact-type, emitido somente pelo runner depois de confirmar a
WAL barrier do grouped transaction. Ele é vinculado a manager/PID/process-birth, database/runtime/
WAL generation, epoch/txn/terminal COMMIT, manifest SHA, allowed target path+image hashes,
lease/COMMIT/INDEX_VIEW nonces e attempt; cada entry é single-consume. Recovery reconstitui o permit
somente da mesma transaction committed e prova novamente guards/lease. Temp órfão pós-barrier só pode
ser removido/recriado por esse permit ao provar final target ausente e path/hash exatos; normal open
não o limpa. Para diretório bootstrap novo, cria-se a entry, faz-se parent-directory barrier e
exige-se vazio antes do primeiro marker.
Nenhum stable carrier usa replace; somente seus slots fixed-size são escritos in-place.
Toda publicação V7 de `control/commit.state`, checkpoint ou marker usa a concrete permit class e o
pending protocol acima; a ordem normativa “publicar” inclui temp data barrier, conditional replace,
parent-directory barrier e direct certification, ainda que tabelas posteriores abreviem esses
subpassos. Nenhuma das três create primitives chama o internal replace sem o exact control permit.

Checkpoint com target Q igual ao Q publicado é no-op certificado: valida o slot corrente e não
escreve nem incrementa generation. Com Q crescente, escreve por `_publish_control_state_durable` e
`_CheckpointStatePublicationPermit` o slot cujo Q **não**
é o Q publicado, faz barrier do control file e cold direct decode, e somente depois publica
`commit.state.checkpoint_lsn=Q`. O slot anterior
que casa com o checkpoint publicado permanece intacto até a nova publicação. Open/recovery escolhe
exatamente um slot CRC-valid cujo Q, DB UUID, C/fence generation, catalog/history e manifest casem com
commit.state; dois candidatos divergentes, ausência ou mismatch são CORRUPT/INCONCLUSIVE e não
autorizam proof/recycle. Crash antes da publicação usa o slot antigo; crash depois encontra o novo.
Só depois de commit.state e do novo slot serem diretamente certificados o slot obsoleto pode ser
sobrescrito numa próxima geração. Assim o baseline de índices sobrevive ao recycle sem depender de
cache.

`publication_generation` começa em 1, cresce exatamente uma unidade por Q publicado e nunca faz
wrap; no máximo u64, checkpoint crescente recusa antes de write. Os digests têm projeção única:

- `catalog_sha256 = SHA256(DOMAIN_CATALOG_V1 || canonical_catalog_snapshot_at_Q)`. O snapshot usa os
  encodings versionados/length-prefixed de todas as TableDef/space/index definitions, identities,
  physical V2 paths e binding states, ordenados por IDs/identity; não usa repr, dict order, cache ou
  directory listing;
- `history_baseline_sha256 = H_Q`. Genesis define
  `H_0 = SHA256(DOMAIN_HISTORY_BASE_V1 || database_uuid || C || fence_generation || feature_bits)`. Para cada
  `CommittedTransaction` em `(old_Q,Q]`, em ordem de commit LSN,
  `H_n = sha256(H_(n-1) || epoch || txn_id || commit_lsn || terminal_record_sha256 ||
  commit_payload_sha256 || ordered_effects_sha256)`. Campos numéricos são little-endian fixed-width;
  hashes cobrem bytes WAL validados, não objetos decodificados reencodados livremente.

O primeiro baseline legado, que não possui raiz anterior recuperável, ancora explicitamente
`H_Q_legacy = SHA256(DOMAIN_HISTORY_LEGACY_V1 || database_uuid || Q || commit_state_bytes || catalog_sha256 ||
manifest_sha256 || direct_heap_index_inventory_sha256)`. O checkpoint C do cutover transforma uma
única vez para
`H_C = SHA256(DOMAIN_HISTORY_CUTOVER_V1 || H_Q_legacy || exact_fence_transaction_sha256 || C || 0x7)`.
Depois disso somente a recurrence normal acima é válida; nenhuma raiz é recalculada por scan físico
de rows ou por máximo heurístico.

Assim um checkpoint novo deriva H_Q do slot anterior + grouped transactions retidas e o valida antes
de publicar. Se o catálogo direto atual está adiante de Q, ele só casa quando replay íntegra dos
catalog/manifests retidos em `(Q,P]` transforma exatamente o baseline no digest atual; comparar
ingenuamente o digest histórico com bytes atuais é proibido.

Há uma única exceção transitória, fechada pelo cutover: meta V2 com C>0 e `checkpoint_lsn=Q<C` aceita
o slot legado `(wal_cutover_lsn=0, fence_generation=0, checkpoint_lsn=Q, LEGACY_BASELINE)` **somente** se o fence
transaction v1 exato `(Q,C]` ainda está integralmente retido, C é seu terminal publicado, não existe
outro COMMIT/record v2 e o cold reconnect está em modo `POST_FENCE_CHECKPOINT_REQUIRED`. Esse modo
permite apenas recovery/inspeção de control state pelo contexto maintenance interno ou checkpoint
target C; não expõe read-view/query público e bloqueia
generic writer, recycle, upgrade e tipo 14. O checkpoint C grava o slot `(C,1,Q=C)` antes de publicar Q=C. Crash antes
mantém a exceção repetível; crash depois seleciona o slot normal. A exceção desaparece para sempre
quando Q alcança C.

Se o checkpoint C ainda enumera qualquer IndexHeader v1, seu slot conserva `LEGACY_BASELINE` e o
reconnect seguinte entra em `INDEX_IDENTITY_BINDING_REQUIRED`. Sob a mesma
`OfflineMaintenanceSession`, cada índice é migrado em uma transação WAL v2 manifestada com
`BIND_IDENTITY=12`. Sob guards, preflight lê source path/header/page inventory e congela **detached**
o path/arquivo target COW completo com IndexHeaderV2/DB UUID/identity, catalog diff, hashes e
logical WAL/effect templates; `WalEntropySeed` e o `WalAppendDraft` com UUID/CRCs/raw blob são
congelados apenas no attempt real. Antes do append, o draft é projetado por
`NormalizedBindProjectionV1`: projection, lengths, peaks, placements e todos os
targets/hashes não-voláteis devem ser bit-identical à entry do `CutoverFeasibilityManifestV1`, ou o
cutover pausa como damage/inconclusive sem append. Separadamente, o raw plan/`WriteSnapshotProof`
revalida o `AttemptFencingContextV1` current sob COMMIT; ele pode diferir do attempt anterior, mas
nunca é aceito por cache/feasibility nem autoriza reuse de permit. Até aqui
target e temp IndexV2 não existem. Faz append + WAL barrier, obtém o
`_IrrevocableWalPermit` e só então chama `create_entry_durable_after_wal` para a imagem target,
executa exact catalog/apply, data/directory barriers e cold direct verify antes de publicar. A
transação autentica source e todos os targets. Crash pre-barrier deixa zero target/temp; crash
pós-barrier é replay exato que recria/aceita somente o mesmo arquivo bit-identical. O path legado não
é adotado nem removido antes do checkpoint final/cold verify e depois só é
coletado pelo runbook offline. Nenhum read/query, generic commit ou outro maintenance é exposto nesse
modo. Depois de todos os binds, checkpoint no published P grava `BOUND_V2`, e cold reconnect prova que
catálogo, paths, headers, carriers e manifest casam. Normativamente, o checkpoint/barrier/direct cert
ocorre com marker ainda FENCED, depois publica-se marker BOUND, encerra-se a runtime maintenance e só
então o cold reconnect prova o conjunto. Se não havia índice legado, o próprio checkpoint C grava
`BOUND_V2` e segue a mesma ordem. Somente marker BOUND **mais** `BOUND_V2` habilitam operação normal.

`RANGE_RESERVATION` e `FORMAT_TRANSITION` carregam manifest vazio, provado por exatamente heap0,
partições identity/format e ausência de row/catalog/index effect. Antes de habilitar tipo 14, um
checkpoint certificado cria o baseline e garante que o sufixo necessário para relevant horizons é
inteiramente WAL format v2 com `CommitPayloadV2`; payload V1 retido é validado conservadoramente e
nunca recebe manifest vazio inventado.

### 5.3 Fence durável WAL v2; flags v1 permanecem opacos — SUBSTITUI V6

> **SUPERADA PARCIALMENTE pelo ADR aceito de compressão:** a tabela/mask proposta nesta seção não é
> mais implementável como escrita. O formato publicado reserva também `0x0004=PAGE_IMAGE_ZLIB1` e
> `0x0008=SKIPPABLE`; `WRITE_PAGE` v2 usa `0x0005`, e tipo desconhecido só é pulável com `0x0008`
> exato, nunca flags zero. Ao retomar V7, toda ocorrência abaixo de mask `0x0003`, unknown/zero
> skippable ou `0x0005` malformed deve ser rebased sobre `WAL_PAGE_COMPRESSION_V1.md`. As regras V7
> específicas de manifest/tipo 14 continuam proposta, não autoridade do runtime atual.

`flags` de WAL format v1 já são domínio persistido do usuário interno, não espaço reservado V7.
Todos os 65.536 valores continuam round-trippable e **nenhum bit v1** significa required, manifest ou
feature. Um scanner v1 preserva a regra legada para tipos desconhecidos e nunca usa flags para decidir
apply/recycle. Os fixtures `0x1234` e `9` permanecem byte-identical.

V7 adiciona WAL record `format_version=2`. O header continua com 48 bytes e os mesmos offsets; é a
versão, não heurística por LSN/tipo/payload, que muda a semântica do u16 `flags`:

```text
WAL_V2_FLAG_REQUIRED       = 0x0001
COMMIT_V2_FLAG_MANIFEST    = 0x0002  # somente COMMIT e sempre junto de REQUIRED
WAL_V2_KNOWN_FLAG_MASK     = 0x0003
```

`WAL_FORMAT_VERSION=2` passa a significar máximo emitível pelo build, não escolha global automática.
Cada `WalManager` recebe um `WalFormatPolicy` frozen derivado por direct meta read: V1 antes/até C e
V2 depois de C. `WalRecord.encode` não decide cutover; `WalAppendPlan` valida policy/LSN por record.
Assim abrir database legado com binário novo não começa a escrever V2 por mera troca de constante.

- para tipo realmente unknown v2, `flags == 0x0000` **exatamente** significa SKIPPABLE: framing, CRC,
  continuidade e agrupamento são validados e o record opaco é preservado, mas não produz efeito;
- para tipo realmente unknown v2, `flags == 0x0001` **exatamente** produz
  `UNSUPPORTED_REQUIRED_RECORD`, com zero apply/publication/checkpoint/recycle;
- para tipo realmente unknown v2, qualquer outro valor (`0x0002`, `0x0003`, bit fora da mask,
  REQUIRED combinado com extra, inclusive `0x0005`) é MALFORMED antes da classificação/apply;
- tipo conhecido v2: flags precisam ser exatamente os definidos por sua gramática. Record de efeito
  é REQUIRED; COMMIT pós-cutover é `REQUIRED|MANIFEST`; conhecido malformed nunca degrada a unknown;
- `IDENTITY_CONTROL` é sempre REQUIRED. O `flags` u8 interno de seu payload permanece zero e não é
  o campo do header.

Na primeira versão V2, a tabela é fechada: todo tipo conhecido não-COMMIT com produção legal —
inclusive BEGIN, ABORT e SEGMENT_HEADER — usa exatamente `0x0001`; COMMIT usa exatamente `0x0003`.
O código numérico legado `CHECKPOINT=5` é reconhecido pelo decoder, mas tem **zero produção legal em
WAL v2**: V7 persiste checkpoint somente em commit.state + `CheckpointIndexStateV1`, e qualquer record
CHECKPOINT v2 é `MALFORMED_KNOWN_RECORD`, não unknown/skippable. Record CHECKPOINT v1 preserva apenas
sua gramática legada e flags opacos. Não existe tipo conhecido skippable. `0x0000` fica reservado
exclusivamente a tipo **desconhecido** realmente pulável; isso impede um mutante de remover REQUIRED
de um efeito conhecido e ainda passar pelo scan.

O fence persistido está em `grafx.meta`, porque um primeiro record v2 pode depois ser reciclado e não
impediria para sempre um binário antigo de escrever. `DatabaseIdentity` ganha format v2 com extensão
obrigatória. Para que um decoder v1 recuse como version mismatch — e não falhe prematuramente no CRC —
o prefixo antigo permanece exatamente válido:

```text
V2 = V1_HEAD(format_version=2) || descriptor_utf8 || legacy_prefix_crc32c
     || WalFeatureFence || full_crc32c

WalFeatureFence =
  magic[8]                  = MAGIC_GRAFX_META_V2
  extension_version u16     = 1
  reserved u16              = 0
  required_feature_bits u64 = 0x0000_0000_0000_0007
    WAL_FEATURE_REQUIRED_SKIPPABLE       = 0x0000_0000_0000_0001
    WAL_FEATURE_COMMIT_MANIFEST          = 0x0000_0000_0000_0002
    WAL_FEATURE_INDEX_MUTATION_MANIFEST  = 0x0000_0000_0000_0004
  wal_semantics_cutover_lsn u64 = C
  fence_generation u64      = 1
```

O CRC do prefixo fica no offset que o decoder v1 espera; o CRC final cobre prefixo, CRC legado e
extensão. Na extension version 1, `required_feature_bits` precisa ser exatamente `0x7`; zero, subset,
superset ou bit desconhecido recusa. Decoder v2 exige comprimento exato, ambos os CRCs, generation 1
e C u64.
Decoder v1 valida o prefixo e então recusa `format_version=2` typed antes de abrir WAL, heap, catálogo
ou qualquer writer. `format_version=2` sem extensão, extensão em identity v1, trailing bytes em V2,
bit desconhecido ou C regressivo são corruption/version refusal, nunca fallback.

Em database genesis V7, `C=0` e todo record real é format v2. Em database legado, o cutover explícito
`enable_wal_semantics_v2()` é uma transação offline de formato, executada somente pelo binário novo:

1. PID/closed check; exigir runbook offline, backup confirmado, ausência de outro participante e
   marker `PREPARED` da seção 3.1.1 diretamente certificado com baseline/carriers bit-identical;
2. sob participant + COMMIT certification-only, provar DB identity v1, WAL retido contínuo, zero gap
   acima de published, zero incomplete, checkpoint certificado exatamente em published e todos os
   hashes/identities ainda iguais ao marker; sair;
3. adquirir writer lease fresh; reentrar COMMIT sem `INDEX_VIEW`, revalidar toda a prova e congelar
   um `WalAppendPlan` **format v1**, com flags opacos iguais a zero, contendo exatamente
   `WRITE_PAGE(grafx.meta,0)` para a identity V2 e COMMIT terminal `C`. Page touch/partition/meta
   owner, target semantic SHA-256 e COMMIT payload V1 precisam ser bijetivos; zero heap/catalog/index/row
   effect e nenhuma outra page são permitidos;
4. WAL barrier; exact apply de `grafx.meta:0`; barrier do meta; direct cold read de V2/C/generation/
   feature bits e fingerprint; publicar commit.state em C;
5. sair de COMMIT, liberar lease confirmado, sair de participant e tornar o Database terminal. É
   obrigatório cold reconnect em `POST_FENCE_CHECKPOINT_REQUIRED`; esse reconnect executa o
   checkpoint target C e publica `CheckpointIndexStateV1(C,1,Q=C)` conforme seção 5.2. Um segundo
   cold maintenance reconnect encontra checkpoint `BOUND_V2` se não havia index legado, ou em
   `INDEX_IDENTITY_BINDING_REQUIRED` para emitir exclusivamente os binds WAL v2 da seção 5.2. Neste
   último caso, checkpoint final no novo P é obrigatório. Em ambos, a porta maintenance publica marker
   BOUND somente depois do checkpoint direct-cert e fecha; o cold reconnect público vem por último.
   Generic writer, upgrade heap e tipo 14 só são habilitados pelo par marker BOUND + `BOUND_V2`;
   marker avança para `FENCED` e depois `BOUND` somente nas boundaries diretamente certificadas da
   seção 3.1.1.

O transaction do fence termina em C e é inteiramente v1. Todo record com `lsn <= C` precisa ser v1;
todo record com `lsn > C` precisa ser v2. Transação, segment roll ou append que cruze C é proibido.
Para genesis C=0, a segunda regra vale desde o primeiro record. `grafx.meta` V1 junto de record v2,
V2 junto de record v1 pós-C, dois fences ou C diferente do terminal são dano/refusal pre-apply.

Crash do cutover antes do COMMIT terminal deixa somente sufixo incomplete, tratável pela recovery
strict. Crash depois do terminal torna a identity page irrevogável: recovery do binário novo aplica a
imagem exata, certifica e publica C. O runbook garante que nenhum binário antigo esteja vivo durante
a transição; depois da page V2 durável, qualquer conexão antiga recusa pelo próprio format da identity.
Cada entrada pública de writer no binário V7 relê o fence direto e confere C/generation contra o
manager antes de COMMIT, fechando handle local anterior ao cutover.

Scanner, writer, `CommittedHistory`, recovery, checkpoint, verifier e read-only passam a compreender
ambas as versões antes de `enable_wal_semantics_v2` ou tipo 14 serem expostos. Checkpoint jamais
remove a autoridade do fence: após reciclar C, `grafx.meta` V2 é a prova durável. Mutantes que
reinterpretam qualquer flag v1, emitem V2 sob meta V1, aceitam V1 pós-C, pulam required desconhecido,
classificam conhecido malformed como skippable ou reciclam sem meta V2 devem morrer.

### 5.4 Record type e gramática fechada

Adicionar um único `WalRecordType.IDENTITY_CONTROL = 14`, sempre REQUIRED e emitível somente em WAL
format v2 com `lsn > C`. O payload é fixo de 80 bytes, little-endian e auto-delimitado pelo record:

```text
magic[8]          = MAGIC_IDENTITY_CONTROL_PAYLOAD_V1
control_version   u16 = 1
operation         u8
flags             u8  = 0
generation        u32 = 1
transition_lsn    u64
start             u64
stop              u64
commit_lsn        u64
page0_sha256[32]
```

Operações numéricas: `FORMAT_TRANSITION=1`, `RANGE_RESERVATION=2`; o código 3 fica permanentemente
reservado e recusado — V7 não possui `COUNTER_CONSUMER`. Demais códigos/flags/version são recusados
em fase v2. O heap é implícito e único no database/WAL; não se serializa path ou
provider-controlled file ID.

Para format, `start == stop == F` e `transition_lsn == commit_lsn == terminal`. Para reservation,
`start == old F`, `stop == new F`, `start < stop`, `transition_lsn == header.T` e
`commit_lsn == terminal`. O CRC do próprio WAL cobre todos os 80 bytes; `page0_sha256` cobre a
imagem semântica alvo definida na seção 3.5.

Gramática de transação:

- `FORMAT_TRANSITION`: sequência total exata e contígua
  `BEGIN_V2 -> WRITE_PAGE(heap,0) -> IDENTITY_CONTROL(FORMAT_TRANSITION) -> COMMIT_V2`;
- `RANGE_RESERVATION`: sequência total exata e contígua
  `BEGIN_V2 -> WRITE_PAGE(heap,0) -> IDENTITY_CONTROL(RANGE_RESERVATION) -> COMMIT_V2`;
- `CONSUMER`: começa com exatamente um `BEGIN_V2`, termina com exatamente um `COMMIT_V2` e entre eles
  contém somente os effect records bijetivamente exigidos por seu manifest. Exige zero
  `IDENTITY_CONTROL`, zero target heap0 que altere counter/T/identity generation, zero avanço de `F`;
  outras imagens heap0 só são legais se manifestadas e semanticamente alheias ao counter.

Nos dois controls, são exatamente quatro records da transação: nenhum extra, ABORT, duplicata ou
interleaving é legal. `SEGMENT_HEADER` é framing WAL-level fora da transação e pode anteceder BEGIN
após roll; nunca aparece entre BEGIN e COMMIT. O mesmo parser de máquina total é usado por live
append, recovery, verify e feasibility; control-before-page, page depois do control ou terminal não
V2 são MALFORMED.

Cada control exige `new_floor > old_floor`, salvo format sem range (`old == new`), fingerprint e
partições exatas. Marker solto, duplicado, fora de ordem ou com payload divergente é inválido.
Depois do fence WAL v2 e antes de uma transição heap exata, um tipo 14 REQUIRED só é aceito se formar exatamente
`FORMAT_TRANSITION`; outro shape conhecido é corrupção/refusal e nunca é tratado como legado opaco.
Unknown SKIPPABLE de outros números continua opaco. A transição só é reconhecida se seu COMMIT LSN é
igual a `T` codificado na imagem de heap0. Depois de v2, qualquer shape inválido é corrupção/refusal.

O scan base valida framing/comprimento/CRC/required sem decodificar eager o payload identity. Em fase
heap v1 já sob WAL v2, somente uma transação com shape completo, imagem heap0 v2 e
`T == commit_lsn` é decodificada como transition; outro tipo 14 é required-malformed e recusa. WAL
format v1 nunca conhece tipo 14. Isso impede que lixo pretransition eleve F
ou que required work seja silenciosamente reciclado.

## 6. Range local, reserva, exaustão e takeover

### 6.1 Estado local

Um grant frozen contém DB UUID, manager ID, origin PID, generation, nonce, `start`, `stop` e
fingerprint da prova. Ele é database-bound e não contém table ID. Somente o manager possui `pos` e o
estado mutável. Um range handle é frozen e contém apenas manager identity, PID/generation e nonce do
grant; toda consumação volta a `IdentityRangeManager.consume(handle, n)`. O handle não possui
autoridade autônoma, callback, referência a `pos` ou método capaz de avançar sem o manager. Estados:

```text
NONE -> REFILLING(attempt) -> PENDING_IRREVOCABLE(attempt) -> ACTIVE(nonce)
                    \-----------------> NONE/EXHAUSTED
                                      \-> BURNED/RECOVERY_REQUIRED
```

Há no máximo um `ACTIVE` e um refill single-flight por manager, não uma fila de grants.
`REFILLING/PENDING_IRREVOCABLE` nunca são visíveis a row planning. `ACTIVE` só ocorre por CAS exato de
`attempt + manager generation` depois de release confirmado. `BURNED`, `EXHAUSTED` e
`RECOVERY_REQUIRED` revogam todos os handles/conditions e são terminais para aquela generation.

O grant serve intents de quaisquer tabelas do mesmo database. Troca de tabela não altera `pos`, não
faz refill e não queima remainder. Se `stop - pos < n`, o manager:

1. avança `pos := stop` e contabiliza todo remainder como queimado;
2. remove a autoridade ativa;
3. reserva exatamente `grant = max(identity_lease_size, n)` (ou o restante até a sentinela);
4. nunca soma apenas o deficit e nunca reativa o remainder antigo.

Isso fecha a contradição V4/L2: ranges simultâneos de processos diferentes tornam falsa a ordem
global, mas não afetam disjunção de números pós-transição nem unicidade de `(table_id, record_id)`.
O contrato de quantidade é “uma reservation por refill”. Apenas
para handouts unitários, sequenciais e sem reinício vale `ceil(handouts/lease_size)`; batches que
queimam remainder, close e crash podem elevar o número.

### 6.2 Protocolo de reserva

Reserva é database-bound e, por isso, pode anteceder DDL. Um prepass detached canonicaliza todos os
row intents e calcula a demanda exata `n` sem atribuir ID, tocar pool/device ou invocar provider. Se o
range ativo cobre `n`, a seção 6.5 consome os números; caso contrário, a ordem fechada de refill é:

1. PID/generation check como primeira instrução;
2. sob manager lock, revalidar generation/demanda; queimar remainder insuficiente e instalar por CAS
   um único `REFILLING(attempt)`. Outro caller espera a condition, que libera o lock, e reinicia no
   PID check ao acordar;
3. liberar manager lock; ele nunca atravessa a entrada/saída de participant, acquire/release de lease
   ou entrada/saída de COMMIT. A única readquisição intermediária é a revalidação breve do passo 5,
   já dentro de participant e obrigatoriamente liberada antes de tentar lease;
4. entrar na participant section;
5. sob manager lock breve, revalidar que o mesmo attempt/generation ainda é `REFILLING`, liberar o
   manager lock, congelar o `GenerationSuccessorSetV1` completo do refill e instalar atomicamente a
   `GenerationAdmissionReservationV1` dentro desse participant; MAX/conflict/close libera claim e
   participant com zero lease/slot/WAL;
6. adquirir writer lease **fresh**, promover/revalidar o claim para seu nonce, sem segurar COMMIT;
7. entrar em COMMIT;
8. validar lease/claim/epoch e exigir `WalFrontierCertification` + `RuntimeDeviceReplayProof` com
   `materialized_through_lsn == current P`. A saída é fechada:
   - se falta frontier/há gap, sair de COMMIT, liberar lease confirmado, claim e participant; sob manager
     lock fazer CAS do mesmo `REFILLING(attempt,generation)` para `RECOVERY_REQUIRED`, revogar
     ticket/nonce/range e notificar todos. Executar recovery separada; ela incrementa
     `database_runtime_generation`. Em sucesso, devolver `RESTART_OUTER_ATTEMPT` ao único loop do
     outer `OperationBudget`; se admitir retry, a factory preserva o envelope/start/deadline, cria
     outro `OperationAttempt(attempt_number + 1)` e reinicia **no passo 1**, instalando novo ticket sob
     a nova generation. O antigo nunca volta aos
      passos 5/22;
   - se a frontier existe e só a device proof falta/está atrás, sair/liberar lease+claim+participant e executar
     `ColdReplayPlan`/catch-up. O mesmo REFILLING só pode voltar uma vez ao passo 4 quando a operação
     prova que `database_runtime_generation`, manager generation, ticket e close state ficaram
     bit-identical; usa outro lease fresh. Se catch-up promove recovery/muda generation, aplica o ramo
     anterior. Segundo drift no mesmo outer attempt encerra/revoga o ticket e pede
     `RESTART_OUTER_ATTEMPT`, impedindo loop nested;
   - lease release incerto envenena/revoga o attempt. Nunca reutilizar lease nem completar apply com
     proof antiga;
9. se ambas as proofs existem/casam em P, congelar um `WriteSnapshotProof` privado com tail/epoch atual e executar OCC
   identity/page0 no COMMIT corrente. Refill não registra reader público nem chama
   `BufferPool.begin_read_view`;
10. ler `heap:0` diretamente e exigir igualdade com o `Heap0MaterializationCertificate` derivado do
   checkpoint + todos os effects heap0 até P na device proof: v2/T/generation/F, raw/semantic
   fingerprint e **o page_lsn exato do último effect heap0 provado** (que pode ser menor que P).
   `page_lsn <= current` isolado nunca autoriza refill;
11. calcular `start = F`, `stop` com aritmética checked e recusar wrap/exaustão;
12. construir clone privado de heap0, `WalAppendDraft` `RANGE_RESERVATION` e
    `WalMutationDraftV1(APPEND)` estritos com `g/g+1`;
13. provar novamente PID/lease/**claim**/tail/pin/source draft e todas as successor edges, publicar CAS generation, obter o permit e
    `append_planned(mutation_draft,append_draft,permit)`;
14. `wal.barrier()`; o runner marca seu resultado detached como `PENDING_IRREVOCABLE(attempt)` e
    fecha o evento built-in sem tomar manager lock; nenhum ID é consumível;
15. preflight/apply exato de heap0, `write_back(heap,0)`, data barrier de heap;
16. read direto: header, T, generation, fingerprint, page_lsn e `F >= stop`;
17. publicar `commit.state`/published LSN, direct reler, finalizar
    `WalFrontierCertificationV1(DERIVED_OWN_COMMIT)` a partir da frontier predecessora + grouped
    reservation exata e instalar `RuntimeDeviceReplayProofV1(OWN_COMMIT)` com o
    `CommitRedoResultV1` heap0/barrier integral e `materialized_through_lsn=terminal`; não action15/16;
18. sair de COMMIT;
19. liberar writer lease; confirmação é obrigatória;
20. liberar o GENERATION_ADMISSION claim em success/refusal/unwind;
21. sair da participant section;
22. sob manager lock, PID/generation + CAS do mesmo attempt ativam o grant e notificam waiters.

É proibido adquirir lease segurando COMMIT, certificar migração com writer lease, reutilizar lease
retido, publicar antes de data barrier/prova direta, ou ativar antes de release. `BaseException`
depois da barreira marca recovery; falha/ambiguidade de release queima o range pendente e envenena o
manager.

Falha comprovadamente pre-WAL sai de todas as sections, toma manager lock, faz CAS do mesmo attempt
para `NONE`/`EXHAUSTED` e notifica waiters; o remainder anterior continua queimado. Falha após barrier
faz CAS para `RECOVERY_REQUIRED`, revoga nonce/ticket e notifica. Se o CAS falhar por close/generation,
nenhum estado é ressuscitado. Cleanup nunca chama provider nem métrica sob manager lock.

Waiter acordado pela transição de gap sempre reinicia em PID/closed/database generation; não pode
assumir o ticket antigo nem iniciar refill enquanto o recovery latch estiver ativo. Close concorrente
revoga o manager e faz o owner, depois de recovery/catch-up, falhar no passo 1 sem instalar novo
ticket. Mutantes que retornam ao passo 4 após recovery, preservam CAS/nonce antigo, ativam com
generation anterior ou deixam waiter roubar REFILLING precisam morrer.

### 6.3 Exaustão e overflow

Todas as contas são u64 checked em exact built-in ints. Se `F == MAX_RECORD_ID + 1`, allocator
retorna erro typed de identity exhaustion antes de qualquer write. Se o grant pedido cruza a
sentinela, reserva somente `[F, MAX_RECORD_ID + 1)` se isso satisfaz `n`; caso contrário recusa sem
write. Nunca há wrap para `0`, truncamento, modulo ou entrega da sentinela.

### 6.4 Stale owner e takeover

O estado durável não contém owner nem remainder: contém somente `F`. Por isso takeover nunca
reclama IDs. Um manager que morre perde seu remainder, já coberto por `F`, como gap autorizado.

No caminho normal, o mesmo manager object/PID/DB generation consome ACTIVE apenas sob seu lock; não
toma lease por ID. Se observar mudança da database/runtime coordination generation (não o epoch
normal de cada writer lease) ou entrar numa porta de
retomada após uncertainty, precisa suspender consumação e, sob fresh lease+COMMIT, provar diretamente
`F >= stop` antes de continuar; recovery local já revoga conforme 6.6. Restart do manager, fork,
mismatch ou qualquer prova inconclusiva queima a autoridade. Outro processo pode reservar a partir
de `F`, jamais a partir de `pos` alheio.

### 6.5 Handle e consumação multi-table

PID check é a primeira instrução do handle e de qualquer bulk/callback. O manager então toma seu
lock, exige tipo/manager/generation/nonce exatos e prova que o grant ainda é o `ACTIVE` corrente:

```text
require current ACTIVE nonce and stop - pos >= n
values = tuple(range(pos, pos + n))
pos += n
ids_handed_out_total += n
release manager lock
return/expose(values)
```

Handles podem ser copiados como valores, mas uma cópia não duplica autoridade: ambas voltam ao mesmo
manager/nonce/pos. Não há rewind, pickle, fork-share ou decremento. Burn/close/poison/generation
change revogam o nonce antes de notificar waiters. `KeyboardInterrupt`, `SystemExit` e falha do
callback preservam a exceção e deixam os IDs já consumidos.

### 6.6 Reservation em recovery e checkpoint — SUBSTITUI V6

O estado persistido de identity é somente heap0/F + WAL control. `start`, `stop`, `pos`, manager ID,
nonce, attempt, handle e ACTIVE nunca entram em checkpoint, commit.state, WAL consumer ou meta. Assim:

- replay de `RANGE_RESERVATION [start,stop)` valida cadeia/fingerprint e materializa `F=stop`, mas
  **não** recria grant; todo o intervalo é conservadoramente burn para o processo recuperado;
- recovery incrementa `database_runtime_generation` antes de liberar callers, revoga tickets/ranges/
  handles locais e acorda waiters com `RECOVERY_REQUIRED`; nenhum CAS antigo pode ativar depois;
- manager novo começa `NONE` e, quando precisar, reserva a partir do F final direto. Nunca usa
  `start`, máximo row físico, remainder ou metadata de processo morto;
- checkpoint pode consolidar/reaplicar heap0 e reciclar controls somente depois de direct cert do F
  final e grouped history; ele não observa nem invalida por inferência um ACTIVE vivo. O mesmo
  manager/PID pode continuar seu range já ativo, pois F já cobre stop; restart continua perdendo-o;
- crash após reservation COMMIT/publication mas antes de release/activation queima o intervalo. Mesmo
  que release tenha ocorrido mas activation não seja comprovável no manager vivo, a decisão é burn;
- consumer commit nunca atualiza F. Criar extent após checkpoint copia o F corrente sem reservar ou
  elevar nada.

Recovery/checkpoint adquirem fresh writer lease explicitamente segundo seção 11. Retained lease,
lease herdado e lease usado na certification pass são proibidos. Failure/uncertainty de release após
completion mantém bytes corretos, mas envenena o manager e impede activation/refill até reconnect.

## 7. Consumer e DDL+primeira row em COW puro

### 7.1 Pipeline de statement e rollback mark — SUBSTITUI V6

Há duas promessas distintas, para não chamar staging local de corrupção durável:

- **pre-device/pre-live-mutation:** validação de tipos built-in, limites, reserved namespaces,
  parâmetros/config, forma do plano e canonicalização ocorre antes de participant, lease, COMMIT,
  INDEX_VIEW, pool pin, allocate/write/barrier ou mutação de catálogo/índice vivos;
- **pre-staging:** antes de qualquer `stage_*`, `note_*`, attachment ou schema effect local, o runner
  instala um único `TransactionStagingMark`. Validação posterior pode usar estado direto, mas falha
  precisa restaurar exatamente esse mark antes de se tornar observável.

`StatementPreflight` é frozen/detached e contém txn/PID/generation, plan/parameter/catalog digests,
demanda de identity ainda não consumida, lista canônica de intents e `StatementIndexPlan`. Ele é
construído fora da participant section, sem provider/callback. Ao entrar em participant, o runner
instala o mark e revalida imediatamente preflight/txn/close/generation antes de chamar o executor.

O mark captura comprimentos/generations de **todas** as coleções mutáveis da transação: row intents,
catalog/schema/space intents, index WAL/effects e attachments, page images/virtual allocations,
read/write partitions, endpoint/reference checks, identity permit uses, vector effects e callbacks
internos pendentes. `discard_since(mark)` exige exact txn/PID/generation e restaura todas em LIFO;
mark parcial, reused, foreign ou fora de ordem é state error. Não há device undo nessa porta porque
nenhum device/live object pode ter sido mutado. IDs previamente consumidos e métricas contábeis de
handout são explicitamente fora do rollback e permanecem burn-only.

`QueryEngine._run` é dividido: `_run_materialized` devolve um `PendingStatementResult` interno com
rows built-in e `_Context` ainda não liberado. Nenhuma porta pública recebe esse objeto. O runner:

1. executa sob a `StatementIndexScope` SHARED da seção 8.4 e materializa todas as rows;
2. faz direct post-certification ainda sob os mesmos guards;
3. revalida imediatamente txn/mark/preflight/catalog/index set;
4. chama `pending.release_to_transaction(mark, certificate)`, que transfere rows/partitions uma vez;
5. constrói a cópia pública detached, zera pins e libera guards;
6. somente fora de participant/guard chama métricas/callback e retorna.

O `context.release()` atual em `query_engine.py:860` deixa de ser chamável sem certificate/mark e não
fica logo após `rows = tuple(...)`. Roots de schema que hoje retornam cedo também passam pelo mesmo
finalizer; nenhum CREATE/ALTER/vector attachment contorna o mark. Qualquer `BaseException` antes do
passo 4 descarta apenas o pending context e volta ao mark; durante/depois do passo 4 chama
`discard_since(mark)`. Falha do rollback instala local/database poison e não expõe resultado. Retry
só começa depois de rollback, saída das sections e consulta ao budget. Um mutante que certifica depois
de release, cobre somente rows, omite attachments/partitions ou faz rewind de ID deve morrer.

### 7.2 Consumer COW e identidade

Todo batch consumidor executa primeiro o prepass de demanda. Se necessário, conclui uma
`RANGE_RESERVATION` interna independente antes do planejamento; em seguida consome do range ACTIVE
todos os `n` números sob o manager e associa cada um ao respectivo row intent, mesmo quando o batch
mistura tabelas existentes, várias tabelas novas, nodes, edges e índices. A reservation posterior ao
snapshot é incorporada por `RebasedPage0Proof`; o batch consumidor não avança `F` e não contém
identity control.

Antes dele, um `ConsumerPreflight` detached canonicaliza o staging completo, calcula demanda e
congela `IndexFencePlan`/manifest skeleton sem participant, lease, COMMIT, guard ou device. O
prepass/refill/consume ocorre antes de entrar na participant section do **commit consumidor** e
sem main lease/COMMIT retidos. A reservation usa e libera sua própria participant/lease/COMMIT. Depois
do consume, o commit consumidor entra normalmente em participant e revalida txn/close/generation;
se close ou abort vencer no intervalo, os IDs consumidos viram gap e nada é reutilizado.

O commit consumidor, já dentro de participant, segue a ordem global de locks e usa somente o
preflight/planner detached já congelado:

1. adquire main lease fresh;
2. adquire `INDEX_VIEW(EXCLUSIVE)*` em ordem canônica conforme o `IndexFencePlan`, e só então
   entra em COMMIT;
3. revalida lease e exige `WriteSnapshotProof` frozen com `WalFrontierCertification.P ==
   RuntimeDeviceReplayProof.materialized_through_lsn == current P`, além de catálogo/identity
   set/preflight; captura current/read view e executa primeiro OCC. Qualquer drift ou proof device
   atrás segue o fluxo fechado abaixo, sem append/device mutation;
4. clona catálogo, heap e staging de índice em estruturas detached;
5. usa allocator virtual baseado em `page_count` e reusable-set certificado, sem device growth;
6. associa os IDs já consumidos aos intents canonicalizados e constrói catálogo,
   heap0, páginas de dados/overflow/refs e índices numa única imagem lógica;
7. materializa zero ou mais extents novos sem placeholder; o mesmo grant cobre todas as tabelas;
8. descobre interesses reais, usa permit privado para identity quando aplicável e executa segundo
   OCC/rebase;
9. valida bijetivamente `IndexFencePlan` contra manifest/efeitos e prova estruturalmente zero
   write/allocate/dirty/resident mutation desde a entrada;
10. congela `WalAppendPlan`, append exato e WAL barrier;
11. faz preflight de **todas** as imagens antes de mutar uma só;
12. aplica páginas novas/overflow, depois links alterados, depois heap0; catálogo/índices seguem sua
    dependência registrada;
13. write-back somente das touched pages; consumer ordinário não avança `F` e usa WAL como autoridade;
14. publica, sai de COMMIT, libera `INDEX_VIEW(EXCLUSIVE)*` em ordem inversa e libera lease;
15. o remainder já estava ACTIVE e permanece database-bound.

`WriteSnapshotProof` é obrigatório para **todo mutador ordinário** em estado
`V7_INTEGRITY_FENCED/BOUND`, não somente consumer. `LEGACY_UNFENCED` continua M1 exact; a transação
única de fence sob marker PREPARED usa seu `LegacyFenceWriteProof(P==Q)` fechado da seção 5.3, e
genesis/bootstrap pre-exposure usam marker permits — não são bypass reutilizável:

```text
WriteSnapshotProofV1 = (
  magic=MAGIC_WRITE_SNAPSHOT_PROOF_V1, version u16=1, context_tag u8, operation_kind u8,
  record_length u32, outer_operation_code u16, reserved_zero u16,
  database_uuid[16], process_birth_identity[16], database_runtime_generation u64,
  checkpoint_lsn u64, pre_append_published_lsn u64,
  wal_feature_fence_proof_sha256[32], wal_frontier_certification_sha256[32],
  reader_publication_proof_sha256[32], runtime_device_replay_proof_sha256[32],
  committed_history_root[32],
  commit_state_raw_sha256[32], catalog_and_file_inventory_sha256[32],
  lease_nonce[32], commit_section_nonce[32], index_fence_plan_sha256[32],
  outer_attempt u32, reserved_zero u32,
  operation_budget_sha256[32], operation_attempt_sha256[32],
  logical_stable_operation_context_sha256[32], actual_source_proof_sha256[32],
  attempt_fencing_context_sha256[32],
  operation_context_sha256[32], proof_nonce_sha256[32],
  transaction_snapshot_lsn u64, transaction_staging_digest[32], crc32c u32
) # exatamente 672 bytes
write_snapshot_proof_sha256 = SHA256(DOMAIN_WRITE_SNAPSHOT_PROOF_V1 || canonical proof bytes)
```

O nome curto `WriteSnapshotProof` significa exatamente esse record fixed-size; não existe dataclass
com ordem/width alternativa. Q/P, frontier e device SHAs são nonzero/current; o reader SHA obedece à
regra tag-dependent abaixo; tags/kinds/zero rules
são validadas antes do hash. Token é module-private/single-use e sua preimage integral, não apenas o
SHA, é entregue ao planner/freeze.
Seu validation bundle entrega integralmente o `WalFrontierEvidenceEnvelopeV1`, de onde se extraem
bit-identical fence/frontier/history Q/P; SHA isolado não basta. O
`catalog_and_file_inventory_sha256` é obrigatoriamente o `ExactEvidenceReceiptV1(kind=12)` reconstruído
por direct inventory do mesmo P conforme a tabela da seção 7. Receipt/fence/envelope ausente,
incompatível ou de outro runtime/attempt recusa antes do append.

Tags/kinds são fechados: `WriteSnapshotContextTagV1` é `INVALID=0`, `TRANSACTION=1`,
`NO_TRANSACTION=2` (3..255 recusam); `WriteSnapshotOperationKindV1` é `INVALID=0`,
`TRANSACTION_COMMIT=1`, `REFILL=2`, `CHECKPOINT=3`, `INDEX_MAINTENANCE=4`, `REPAIR=5`,
`UPGRADE=6`, `BIND_IDENTITY=7` (8..255 recusam). `context_tag=TRANSACTION(1)` permite somente
`operation_kind=TRANSACTION_COMMIT(1)` e exige S, staging digest, txn epoch/id, preflight, OCC e
IndexFencePlan bijetivamente cobertos por
`logical_stable_operation_context_sha256=SHA256(DOMAIN_TRANSACTION_STABLE_CONTEXT_V1 ||
canonical_transaction_context)`. Para transação unitária, `actual_source_proof_sha256=ZERO32`; a
frontier/device raw e o `reader_publication_proof_sha256` nonzero já estão nos campos superiores e no
fencing current. O wrapper de reader referencia a mesma frontier/device/P e a own-key ainda live. O staging digest não
pode ser zero e precisa casar com o outer mark imediatamente antes do append.

`context_tag=NO_TRANSACTION(2)` permite exatamente `REFILL(2)`, `CHECKPOINT(3)`,
`INDEX_MAINTENANCE(4)`, `REPAIR(5)`, `UPGRADE(6)` ou `BIND_IDENTITY(7)` e exige
`transaction_snapshot_lsn=0` + `transaction_staging_digest=ZERO32` +
`reader_publication_proof_sha256=ZERO32`; essa rota certifica frontier/device sem registrar reader
público. Seu context digest é domain-
separated e bijetivo ao plan específico:

- REFILL: manager/database generation, ticket nonce, demand, Heap0MaterializationCertificate e
  reservation preflight;
- CHECKPOINT: CheckpointPreparation, ReaderRegistrySnapshot/floor pin, target Q e manifest skeleton;
- INDEX_MAINTENANCE: exact operation enum, source/target header/page/catalog plan e manifest; para
  substep multi-step o logical SHA usa somente o core sem actual proof, enquanto
  `actual_source_proof_sha256` é o SHA raw do `ActualSubstepSourceProofV1`; inclui ainda o
  `PendingOuterOperationMap` current;
- REPAIR/UPGRADE: classifier/UpgradeDigest, target images e maintenance session identity;
- BIND_IDENTITY:
  `logical_stable_operation_context_sha256 = SHA256(DOMAIN_BIND_STABLE_CONTEXT_V1 ||
  canonical_BoundStableBindContextV1_bytes || canonical_logical_BoundOperationSubstepContextV1_bytes)`;
  source inventory, target identity/path/full image, manifest, outer operation identity/full-plan
  SHA/ordinal ficam no logical body. `actual_source_proof_sha256` autentica o raw actual separado; seu
  attempt fencing usa exatamente a lista `AttemptFencingContextV1`, inclusive generations/epoch/txn
  e permits pre-draft correntes, mas nunca `_WalMutationPermit`/`_IrrevocableWalPermit`.

O logical stable SHA é igual entre retries/resume do mesmo substep e nunca contém a actual proof.
INDEX_REBUILD usa `DOMAIN_REBUILD_STABLE_CONTEXT_V1` sobre seu logical substep core. Se uma recipe permite commit
estrangeiro fora do scope, o core outer/full-plan/ordinal/base/source lógico permanece igual, mas o
actual-source SHA é recalculado. BIND exige igualdade bit a bit apenas do logical SHA; feasibility
tag-normaliza actual/fencing raw e recalcula todos os outputs derivados.

Para todo tag/kind,
`operation_context_sha256 = SHA256(DOMAIN_OPERATION_CONTEXT_V1 ||
logical_stable_operation_context_sha256 ||
actual_source_proof_sha256 || attempt_fencing_context_sha256)`. A proof/plan raw autentica e revalida
os três; feasibility BIND cobre somente o logical stable SHA e normaliza/tag-recalcula actual +
fencing SHAs/valores derivados. Tag/kind unitário que não declara actual proof exige ZERO32; multi-step
exige nonzero e igualdade ao body. Nenhuma generation/permit/proof raw antiga é retida entre attempts.

`outer_operation_code` deve ser exatamente `OuterOperationCodeV1` da seção 12.4 e bit-identical ao
campo da porta que criou o `OperationBudget`; `outer_attempt`
é cópia de `OperationAttempt.attempt_number`. Os dois SHA precisam casar com o mesmo envelope object e
contexto frozen corrente recebidos pela operação; contexto anterior/revoked ou criado por helper
recusa. `proof_nonce_sha256` é exatamente
`SHA256(DOMAIN_PROOF_NONCE_V1 || database_uuid[16] || context_tag_u8 || operation_kind_u8 ||
LE16(outer_operation_code) || LE32(outer_attempt) || operation_budget_sha256[32] ||
operation_attempt_sha256[32] || commit_section_nonce[32] ||
logical_stable_operation_context_sha256[32] || actual_source_proof_sha256[32] ||
attempt_fencing_context_sha256[32] || operation_context_sha256[32])`, nessa ordem e sem padding.
Todos os fields são copiados/rederivados do mesmo `AttemptFencingContextV1`; qualquer divergência
entre final seed, snapshot proof, append draft ou normalized projection recusa. O hash é registrado
single-use no manager e consumido pelo primeiro freeze/append; não usa clock/CSPRNG
sob sections. Tag/kind desconhecido, kind/tag incompatível, zero/nonzero cruzado, digest/outer attempt
divergente ou reuse recusa pre-WAL. Não existe sentinel escolhido pela implementação.

Row/DDL/index commit, refill, MARK/CLEAR/RESET/rebuild/reconcile, checkpoint, repair, upgrade e BIND
consomem essa proof central antes do primeiro WAL byte; reservation pura pode ter set de índice vazio.
Recovery/gap completion e cold device catch-up **não** constroem nem consomem `WriteSnapshotProof`:
isso seria circular, pois ela exige justamente as duas predecessor proofs ainda ausentes. São exceções
fechadas, sem append de nova grouped transaction, que usam tipos distintos:

```text
WalFeatureFenceProofV1 = (
  magic=MAGIC_WAL_FEATURE_FENCE_PROOF_V1, version u16=1, reserved_zero u16,
  record_length u32, database_uuid[16], identity_format_version u16,
  wal_record_format_version u16, reserved_zero u32,
  interpreted_required_feature_bits u64, cutover_lsn_c u64, fence_generation u64,
  checkpoint_q u64, grafx_meta_raw_sha256[32], bootstrap_marker_sha256[32],
  checkpoint_state_sha256[32], feature_manifest_sha256[32], crc32c u32
) # exatamente 204 bytes
wal_feature_fence_proof_sha256 =
  SHA256(DOMAIN_WAL_FEATURE_FENCE_PROOF_V1 || canonical record bytes)

WalPhysicalScanEvidenceV1 = (
  magic=MAGIC_WAL_PHYSICAL_SCAN_EVIDENCE_V1, version u16=1, reserved_zero u16,
  record_length u32, database_uuid[16], database_runtime_generation u64,
  wal_runtime_mutation_generation u64, checkpoint_q u64, published_p u64,
  durable_commit_frontier u64, physical_tail_segment_number u64, physical_tail_offset u64,
  last_valid_segment_number u64, last_valid_record_offset u64,
  last_valid_record_length u32, boundary_kind u8, reserved_zero[3],
  sealed_scan_byte_count u64, committed_count u32, incomplete_count u32,
  required_unknown_count u32, range_hash_count u32, protected_range_set_length u32,
  reserved_zero u32, committed_history_root_sha256[32], terminal_record_raw_sha256[32],
  protected_range_set_sha256[32],
  protected_range_set_bytes[protected_range_set_length],
  range_hashes[range_hash_count], crc32c u32
)
scan_range_hash := (range_ordinal u32, reserved_zero u32, protected_bytes_raw_sha256[32]) # 40 bytes
wal_physical_scan_evidence_sha256 =
  SHA256(DOMAIN_WAL_PHYSICAL_SCAN_EVIDENCE_V1 || canonical record bytes)

CheckpointDirectProofV1 = (
  magic=MAGIC_CHECKPOINT_DIRECT_PROOF_V1, version u16=1, reserved_zero u16,
  record_length u32, database_uuid[16], database_runtime_generation u64,
  checkpoint_q u64, published_p u64, boundary_proof_length u32,
  checkpoint_state_length u32, checkpoint_index_manifest_length u32,
  checkpoint_device_manifest_length u32, boundary_proof_sha256[32],
  checkpoint_state_raw_sha256[32], checkpoint_index_manifest_sha256[32],
  checkpoint_device_manifest_sha256[32], bootstrap_marker_sha256[32],
  commit_state_raw_sha256[32], boundary_proof_bytes[boundary_proof_length],
  checkpoint_state_bytes[checkpoint_state_length],
  checkpoint_index_manifest_bytes[checkpoint_index_manifest_length],
  checkpoint_device_manifest_bytes[checkpoint_device_manifest_length], crc32c u32
)
checkpoint_direct_proof_sha256 =
  SHA256(DOMAIN_CHECKPOINT_DIRECT_PROOF_V1 || canonical record bytes)

ExactEvidenceReceiptV1 = (
  magic=MAGIC_EXACT_EVIDENCE_RECEIPT_V1, version u16=1, evidence_kind u16,
  record_length u32, database_uuid[16], database_runtime_generation u64,
  subject_identity_sha256[32], source_generation u64, target_generation u64,
  barrier_flags u32, entry_count u32, entries[entry_count], crc32c u32
)
exact_evidence_entry := (
  entry_length u32, object_kind u8, predecessor_state u8, successor_state u8,
  proof_state u8, ordinal u32, locator_length u32, predecessor_length u64,
  successor_length u64, capability_binding_sha256[32],
  predecessor_file_identity[32], successor_file_identity[32],
  predecessor_raw_sha256[32], successor_raw_sha256[32], semantic_sha256[32],
  locator_bytes[locator_length]
) # entry_length == 224 + locator_length
exact_evidence_receipt_sha256 =
  SHA256(DOMAIN_EXACT_EVIDENCE_RECEIPT_V1 || canonical record bytes)

IndexFencePlanV1 = (
  magic=MAGIC_INDEX_FENCE_PLAN_V1, version u16=1, guard_mode u8=EXCLUSIVE, reserved_zero u8,
  record_length u32, database_uuid[16], entry_count u32, reserved_zero u32,
  entries[entry_count], crc32c u32
)
index_fence_entry := (
  persisted_index_identity[32], carrier_identity_generation u64,
  expected_mutation_generation u64, required_effect_set_sha256[32]
) # 80 bytes
index_fence_plan_sha256 = SHA256(DOMAIN_INDEX_FENCE_PLAN_V1 || canonical record bytes)

CommittedHistoryEvidenceV1 = (
  magic=MAGIC_COMMITTED_HISTORY_EVIDENCE_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], checkpoint_q u64, source_p u64, target_h u64,
  committed_count u32, incomplete_count u32, scan_evidence_length u32, reserved_zero u32,
  committed_history_root_sha256[32], scan_evidence_sha256[32], scan_range_set_sha256[32],
  committed[committed_count], incomplete[incomplete_count],
  scan_evidence_bytes[scan_evidence_length], crc32c u32
)
history_txn_entry := (
  epoch u64, txn_id u64, transaction_uuid[16], begin_lsn u64, terminal_lsn u64,
  grouped_raw_sha256[32], terminal_manifest_sha256[32]
) # 112 bytes
committed_history_evidence_sha256 =
  SHA256(DOMAIN_COMMITTED_HISTORY_EVIDENCE_V1 || canonical record bytes)

WalFrontierEvidenceEnvelopeV1 = (
  magic=MAGIC_WAL_FRONTIER_EVIDENCE_ENVELOPE_V1, version u16=1, producer_kind u8,
  reserved_zero u8, record_length u32, database_uuid[16], reserved_zero[8],
  certification_length u32, feature_fence_proof_length u32,
  physical_scan_evidence_length u32, retained_range_set_length u32,
  producer_evidence_length u32, reserved_zero u32,
  certification_sha256[32], feature_fence_proof_sha256[32],
  physical_scan_evidence_sha256[32], retained_range_set_sha256[32],
  producer_evidence_sha256[32], certification_bytes[certification_length],
  feature_fence_proof_bytes[feature_fence_proof_length],
  physical_scan_evidence_bytes[physical_scan_evidence_length],
  retained_range_set_bytes[retained_range_set_length],
  producer_evidence_bytes[producer_evidence_length], crc32c u32
)
wal_frontier_evidence_envelope_sha256 =
  SHA256(DOMAIN_WAL_FRONTIER_EVIDENCE_ENVELOPE_V1 || canonical record bytes)

RecoveryApplyPlanV1 = (
  magic=MAGIC_RECOVERY_APPLY_PLAN_V1, version u16=1, tail_mode u8, reserved_zero u8,
  record_length u32, database_uuid[16], source_p u64, target_h u64,
  checkpoint_proof_sha256[32], raw_scan_evidence_sha256[32],
  checkpoint_proof_length u32, raw_scan_evidence_length u32,
  history_length u32, tail_disposition_length u32,
  device_apply_manifest_length u32, index_fence_plan_length u32,
  checkpoint_proof_bytes[checkpoint_proof_length],
  raw_scan_evidence_bytes[raw_scan_evidence_length], history_bytes[history_length],
  tail_disposition_bytes[tail_disposition_length],
  device_apply_manifest_bytes[device_apply_manifest_length],
  index_fence_plan_bytes[index_fence_plan_length], crc32c u32
)
recovery_apply_plan_sha256 = SHA256(DOMAIN_RECOVERY_APPLY_PLAN_V1 || canonical record bytes)

ColdReplayPlanV1 = (
  magic=MAGIC_COLD_REPLAY_PLAN_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], checkpoint_q u64, target_p u64,
  wal_frontier_certification_sha256[32], checkpoint_proof_sha256[32],
  frontier_evidence_envelope_sha256[32], checkpoint_proof_length u32,
  frontier_evidence_envelope_length u32, history_length u32,
  device_apply_manifest_length u32, index_fence_plan_length u32, reserved_zero u32,
  checkpoint_proof_bytes[checkpoint_proof_length],
  frontier_evidence_envelope_bytes[frontier_evidence_envelope_length],
  history_bytes[history_length], device_apply_manifest_bytes[device_apply_manifest_length],
  index_fence_plan_bytes[index_fence_plan_length], crc32c u32
)
cold_replay_plan_sha256 = SHA256(DOMAIN_COLD_REPLAY_PLAN_V1 || canonical record bytes)

RecoveryApplyAuthorityV1 = (
  magic=MAGIC_RECOVERY_AUTHORITY_V1, version u16=1, tail_mode u8, reserved_zero u8,
  record_length u32, outer_operation_code u16, reserved_zero u16,
  database_uuid[16], owner_pid u64, process_birth_identity[16],
  database_runtime_generation u64, operation_budget_sha256[32],
  operation_attempt_sha256[32], fresh_lease_nonce[32], index_guard_set_nonce[32],
  commit_section_nonce[32], recovery_apply_plan_sha256[32],
  checkpoint_proof_sha256[32], raw_scan_evidence_sha256[32],
  history_evidence_sha256[32], source_p u64, target_h u64,
  exact_device_index_apply_sha256[32], index_fence_plan_sha256[32],
  tail_mutation_execution_sha256[32], authority_nonce_sha256[32], crc32c u32
)

ColdReplayAuthorityV1 = (
  magic=MAGIC_COLD_AUTHORITY_V1, version u16=1, reserved_zero u16, record_length u32,
  outer_operation_code u16, reserved_zero u16, database_uuid[16], owner_pid u64,
  process_birth_identity[16], database_runtime_generation u64, device_replay_generation u64,
  operation_budget_sha256[32], operation_attempt_sha256[32], fresh_lease_nonce[32],
  index_guard_set_nonce[32], commit_section_nonce[32], cold_replay_plan_sha256[32],
  wal_frontier_certification_sha256[32], checkpoint_q u64, target_p u64,
  exact_device_index_apply_sha256[32], index_fence_plan_sha256[32],
  authority_nonce_sha256[32], crc32c u32
)
```

`ExactEvidenceKindV1` é `u16`: `INVALID=0`, `DATA_BARRIER=1`,
`PARENT_NAMESPACE_BARRIER=2`, `DIRECT_CERTIFICATE=3`, `PARENT_CHAIN=4`, `ABSENCE=5`,
`DEVICE_APPLY_PLAN=6`, `DEVICE_BARRIER_PLAN=7`, `DEVICE_APPLY_RESULT=8`,
`CAPABILITY_PROJECTION=9`, `FAILED_TEMP_RELEASE=10`, `FILE_INVENTORY=11`,
`CATALOG_HEAP_INDEX_INVENTORY=12`, `HEAP0_SEMANTIC=13`,
`MANIFEST_IDENTITY_DIRECT=14`; 15..65535 recusam. Object kind `u8` é
`INVALID=0`, `FILE=1`, `DIRECTORY=2`, `PAGE=3`, `WAL_RANGE=4`, `CATALOG=5`, `HEAP0=6`,
`INDEX=7`, `CONTROL=8`; states `INVALID=0`, `ABSENT=1`, `CURRENT=2`, `PLANNED=3`; proof state
`INVALID=0`, `OBSERVED_PRE=1`, `OBSERVED_POST=2`, `BARRIER_CONFIRMED=3`,
`DIRECT_CERTIFIED=4`. Unknown/reserved/cross-kind recusam.

Receipt entries são sorted/unique por `(object_kind,locator_bytes,ordinal)`, ordinal denso dentro de
cada kind e locator é `CanonicalRelativePathV1` ou o fixed page/WAL locator do object kind. Count é
0..262144, locator 1..65535, `entry_length==224+locator_length` e
`record_length==100+sum(entry_length)<=67108864`; count zero só vale para inventory 11/12 cujo exact
source enumera vazio. File identities/lengths/raw/semantic, capability binding, pre/post states e
generation vêm dos fields do enclosing plan mais direct reads; field inaplicável é ZERO canônico.
Barrier flags são o allowlist do kind e proof-state BARRIER_CONFIRMED só nasce após a syscall seguida
de direct recertification. O backend não fornece bytes/hash próprios: fornece o resultado da primitive,
e o encoder V7 monta este record. Falta de locator/preimage, I/O ou re-read produz INCONCLUSIVE;
jamais “confia nos 32 bytes”.

A tabela é total para receipts safety-critical já usados pelo contrato:

| Field consumidor | `ExactEvidenceKindV1` | Fonte determinística da preimage |
|---|---:|---|
| `SpaceReservationStageProof.barrier_proof_sha256` | 1 | reservation target/data+parent entries e barrier flags do space plan |
| `ManifestIdentityStageProof.direct_certificate_sha256` | 14 | manifest/identity target bytes e locator do stage |
| `ControlPublicationStageProof.parent_chain_proof_sha256` | 4 | root capability + cada parent identity/locator do namespace projection |
| orphan `first_marker_absence_proof_sha256` | 5 | final marker + deterministic temp locators, ambos direct ABSENT/current conforme bootstrap kind |
| `DeviceApplyTargetSetAuthority.apply_plan_sha256` | 6 | ordered exact target/pre/post vector da authority |
| mesma authority `barrier_plan_sha256` | 7 | ordered file/parent barrier entries do target vector |
| mesma authority `apply_result_sha256` | 8 | ordered actual result entries, incluindo `SUPERSEDED_COMMITTED` semantic tag |
| `WalMutationExactSet.parent.capability_receipt_sha256` | 9 | `ParentCapabilityReceiptSubjectV1` integral daquela parent, que exclui o próprio receipt; capability/root/path current |
| `WalMutationExactSet.barrier.expected_direct_proof_sha256` | 3 | exact target/parent postimage da barrier entry |
| `WalNamespaceReleaseProofKindV1.FAILED_TEMP` | 10 | temp source CURRENT→ABSENT + final target ABSENT, capability/path/identity/raw exatos |
| `WriteSnapshotProof.catalog_and_file_inventory_sha256` | 12 | catálogo + heap + index/carrier inventory sorted do mesmo P |
| `RuntimeDeviceReplayProof.final_file_inventory_sha256` | 11 | files touched pelo redo/direct checkpoint result, sorted |
| `RuntimeDeviceReplayProof.heap0_semantic_sha256` | 13 | heap0 identity/F/T/generation/page-LSN semantic projection |
| `RuntimeDeviceReplayProof.final_catalog_heap_index_headers_sha256` | 12 | direct final catalog/heap/index header projection do mesmo result |

Cada field acima é **exatamente**
`SHA256(DOMAIN_EXACT_EVIDENCE_RECEIPT_V1||canonical ExactEvidenceReceiptV1 bytes)` do kind indicado.
O verifier reconstrói o record integral dos locators/preimages atuais e do plan, re-encoda e compara;
um receipt pode ser retido como bytes para diagnóstico, mas não é authority adicional. Kind trocado,
JSON/tuple host, lista parcial, reorder, ZERO quando a tabela exige nonempty, receipt de outro subject/
generation ou backend que não consegue expor direct locator recusa. Dedicated proofs de fence/scan/
checkpoint usam seus codecs próprios acima e nunca são reinterpretados como um receipt genérico.

`IndexGuardModeV1` é `INVALID=0`, `EXCLUSIVE=1`; 2..255 recusam. Fence entries são sorted/unique por
identity e count 0..262144; empty exige zero entries e o SHA ordinário do record integral count-zero
sob `DOMAIN_INDEX_FENCE_PLAN_V1`, nunca ZERO32; `record_length==44+80*entry_count<=20971564`.

O feature-fence proof é self-contained: `feature_manifest_sha256` é SHA256 raw da fixed-width
projection `LE16(identity_format)||LE16(wal_format)||LE64(required_bits)||LE64(C)||LE64(fence_generation)||LE64(Q)`;
não é outro record/locator. Formats/bits/C/generation/Q precisam casar às direct decodes
de `grafx.meta`, bootstrap/checkpoint e feature manifest, cujos raw/digests são repetidos; bit
required sem interpretação, proof de outro Q/C ou qualquer nested source indisponível recusa. O
`WalPhysicalScanEvidenceV1` fecha o scan iniciado pelo `ProtectedWalRangeSetV1`: range-set length é
140..4718660, `range_hash_count` é exatamente o nested range count 0..65535, ordinal é denso e cada
SHA é SHA256 raw dos bytes `[start,end)` lidos fora de COMMIT. Descriptors/file identities/lengths/
generation são os instalados na action15 e recertificados no segundo COMMIT. Seu
`record_length == 244 + protected_range_set_length + 40*range_hash_count <= 8388608`; content hash não
existe no pin inicial. Tail/last-valid/frontier/counts/history root são derivados pela grammar única
do mesmo byte scan; missing bytes, digest host/JSON, range omitido ou SHA fornecido sem leitura exata
recusa.

`CheckpointDirectProofV1` exige boundary length exatamente 244, checkpoint state 1..67108864,
index manifest 1..268435456 e device manifest 1..67108864. Cada nested magic/DB/Q/P/SHA decodifica e
casa, e `record_length == 268 + boundary_proof_length + checkpoint_state_length +
checkpoint_index_manifest_length + checkpoint_device_manifest_length <= 402653696`; bootstrap/
commit-state raw SHAs são direct-certified no mesmo sandwich. Um checkpoint digest sem esses bytes,
locator externo não declarado ou nested swap recusa.

History committed entries
são sorted/unique por `(terminal_lsn,epoch,txn_id,transaction_uuid)` e grouped grammar-valid, count
0..1048576. `incomplete_count` é somente 0 ou 1: a única incomplete, quando existe, é o sufixo físico
final, usa terminal LSN zero/manifest ZERO e sua identity é `(begin_lsn,epoch,txn_id,UUID,grouped raw
SHA)`. Seu `begin_lsn` é estritamente maior que o último `terminal_lsn` committed, ou maior que
`checkpoint_q` quando committed_count é zero; os bytes agrupados começam nessa locator e ocupam o
sufixo físico contíguo até o tail direto coberto por `scan_range_set_sha256`. Nenhum complete record,
gap, segundo BEGIN, terminal ou byte fora do último frame parcial pode aparecer depois dele. Duas
incompletes, duplicate identity, reorder/interleaving ou incomplete sobreposta a qualquer committed
span não têm encoding válido e produzem REFUSE_INCONCLUSIVE, não um array permutável. Root/range SHA
são recomputados do `WalPhysicalScanEvidenceV1` embedded, com length 384..8388608 e SHA/domain exatos;
seu nested range set é a mesma preimage integral. `record_length == 172 +
112*(committed_count+incomplete_count) + scan_evidence_length <= 134217728`, com produtos/somas
checked antes de allocation; o canonical re-encode preserva essa ordem.
`RecoveryTailModeV1`
é `INVALID=0`, `KEEP=1`, `TRUNCATE_COMPOSITE=2`; outro recusa. Ambos exigem exatamente um
`TailDispositionV1` nonempty: KEEP exige `disposition_kind=KEEP` e execution SHA ZERO32; TRUNCATE exige
`disposition_kind=TRUNCATE_SUFFIX` e eventual execution SHA conforme phase. Length zero recusa.
Device apply nested bytes são `CheckpointDeviceApplyManifestV1`; seu field SHA é exatamente
`DOMAIN_CHECKPOINT_DEVICE_MANIFEST_V1` sobre essa preimage integral. Fence/history nested
DB/horizons/SHAs casam. Plan SHAs usam os identifiers `DOMAIN_RECOVERY_APPLY_PLAN_V1`/
`DOMAIN_COLD_REPLAY_PLAN_V1`. Authority checkpoint/raw-scan fields precisam ser bit-identical aos
nested `CheckpointDirectProofV1`/`WalPhysicalScanEvidenceV1` do plan; a authority nunca recebe esses
digests como argumentos independentes.
No plan, `checkpoint_proof_sha256` é exatamente `checkpoint_direct_proof_sha256` e
`raw_scan_evidence_sha256` é exatamente `wal_physical_scan_evidence_sha256`; lengths/bytes são
obrigatórios e ambos repetem a mesma DB/Q/P/history/boundaries da history evidence.

Os limits de plan são únicos: `MAX_RECOVERY_OR_COLD_PLAN_BYTES_V1=1073741824`.
Recovery exige checkpoint proof 268..402653696, raw scan 384..8388608, history 1..134217728,
tail disposition 1..134217728, device manifest 1..67108864 e fence plan 44..20971564;
`record_length == 140 + sum(seis nested lengths) <= MAX`. Cold exige os mesmos checkpoint/history/
device/fence limits, frontier envelope 1..268435456 e
`record_length == 172 + sum(cinco nested lengths) <= MAX`. Zero, u32 overflow, soma acima do max,
EOF/trailing, nested SHA/DB/Q/P divergente ou allocation antes de checked-sum recusa. Decoders fazem
preflight dos headers/lengths antes de reservar memória; golden min/max e cada field em max+1/
`0xffffffff` produzem o mesmo erro typed em todo backend.
No cold plan, `frontier_evidence_envelope_sha256` usa o domain registrado do envelope integral e seu
nested certification SHA precisa igualar `wal_frontier_certification_sha256`; checkpoint proof e
history também precisam ser os mesmos projetados pelo envelope/Q→P. Não há escolha de encoder.

O `WalFrontierEvidenceEnvelopeV1` é obrigatório em toda validação de frontier — o SHA de certification
sozinho nunca é authority. Certification length é 404, fence 204, physical scan 384..8388608,
retained range set 140..4718660. FULL_SCAN exige producer kind 1 e producer bytes iguais ao
`CommittedHistoryEvidenceV1` integral (1..134217728); DERIVED_OWN_COMMIT exige kind 2 e exatamente os
240 bytes, na ordem já registrada, de predecessor frontier SHA, append fingerprint, grouped raw,
terminal manifest, redo result, commit-state raw, tail evidence e old/new P. O envelope tem
`record_length == 228 + sum(cinco nested lengths) <= 268435456`; todos os cinco SHAs são recomputados,
certificate fields casam aos nested proofs e FULL history repete scan/range/history bit-identical.
Runtime/public wrapper recebe o envelope integral e projeta seu `certification_sha256`; trocar fence,
tail/range/producer ou aceitar qualquer digest sem preimage recusa.

O recovery `authority_nonce_sha256` é exatamente
`SHA256(DOMAIN_RECOVERY_AUTHORITY_NONCE_V1 || database_uuid[16] || LE16(outer_operation_code) ||
LE64(owner_pid) || process_birth_identity[16] || LE64(database_runtime_generation) ||
operation_budget_sha256[32] || operation_attempt_sha256[32] || fresh_lease_nonce[32] ||
index_guard_set_nonce[32] || commit_section_nonce[32] || recovery_apply_plan_sha256[32] ||
checkpoint_proof_sha256[32] || raw_scan_evidence_sha256[32] || history_evidence_sha256[32] ||
LE64(source_p) || LE64(target_h) || exact_device_index_apply_sha256[32] ||
index_fence_plan_sha256[32] || tail_mutation_execution_sha256[32])`. O cold nonce usa
`SHA256(DOMAIN_COLD_AUTHORITY_NONCE_V1 || database_uuid[16] || LE16(outer_operation_code) ||
LE64(owner_pid) || process_birth_identity[16] || LE64(database_runtime_generation) ||
LE64(device_replay_generation) || operation_budget_sha256[32] || operation_attempt_sha256[32] ||
fresh_lease_nonce[32] || index_guard_set_nonce[32] || commit_section_nonce[32] ||
cold_replay_plan_sha256[32] || wal_frontier_certification_sha256[32] || LE64(checkpoint_q) ||
LE64(target_p) || exact_device_index_apply_sha256[32] || index_fence_plan_sha256[32])`.
`exact_device_index_apply_sha256` é o digest registrado do exact nested
`CheckpointDeviceApplyManifestV1`, não um resultado host separado. Esses nonces não usam clock/CSPRNG
sob sections. Authority CRC/record length/EOF,
outer code, PID/birth/runtime current e single-use são obrigatórios; body/hash opaco sem plan preimage,
guard set fora do fence plan ou route cruzada recusa.

`TailMutationExecutionV1` é o composite exact-type da seção 10.3; ZERO32 é obrigatório para KEEP e
qualquer recovery sem truncation. Um SHA opaco, um `WalMutationDraftV1` singular ou um permit capaz de
servir às duas phases é malformed. As authorities são module-private exact-type, single-use, criadas somente na segunda passagem depois
de revalidar o plan inteiro sob fresh lease/guards/COMMIT; não possuem overload para user mutation,
new WAL COMMIT, refill, DDL ou index maintenance. Recovery aplica/quarentena/trunca apenas conforme o
plan, faz barriers/direct cert e publica o H completo. Depois de sair/liberar a mutation pass, executa
um frontier scan sandwich separado sob o mesmo budget: action15 protege ranges e action17 sempre
termina a scan pin, sem action16/reader key. Só então produz
`WalFrontierCertificationV1(FULL_SCAN,H)` **e**
`RuntimeDeviceReplayProof(materialized=H)`. Catch-up, com frontier já
válida, aplica Q→P e produz apenas a nova `RuntimeDeviceReplayProofV1(REPLAY,P)`. Só um begin posterior
envolve frontier+device correntes em `WalPublicationProofV1`; depois uma operação
ordinária pode combinar as predecessor proofs num `WriteSnapshotProof`. Genesis/bootstrap continuam
autoridades marker pre-exposure, também sem reutilizar esse struct. Nenhum outro mutador possui bypass.

Se falta frontier certification/há gap durante commit de Transaction existente, o attempt libera COMMIT/guards/
namespace/lease/participant, rollbacka qualquer staging do attempt ao outer mark e aborta/revoga a
Transaction antes de recovery. Recovery usa o mesmo `INTEGRITY_BUDGET`, incrementa
`database_runtime_generation` e, ainda que conclua com sucesso, a chamada termina com
`GrafxWriteConflict(field="recovery_generation", retryable=True)`; user code/intents/context antigo não
são replayados automaticamente e IDs consumidos ficam queimados. Nova escrita exige nova Transaction.

Se apenas a device proof local está atrás e a frontier certification em P é válida, o attempt libera todas as
sections e executa catch-up separado sob o mesmo budget. Como não muda `database_runtime_generation`,
pode repetir **somente pre-WAL**, depois de fresh lease/guards/COMMIT e revalidação integral de
transaction snapshot/staging digest, OCC, catálogo e nova proof em P. Drift contínuo pede outer retry;
failure/poison aborta a Transaction. Nenhum retained lease/guard/proof atravessa o catch-up.

Antes do COMMIT, bindings públicos de row exibem `record_id = 0`/unbound; nenhum ID consumido vaza.
Crash antes do primeiro byte WAL consumer não deixa alocação ou frame, mas queima os IDs já consumidos.
Crash após WAL barrier consumer é resolvido por recovery. Separadamente, crash da reservation após
publicação e antes da ativação queima o range inteiro, sem reutilização.

Um número associado ao clone é `_ConsumedIdentity`: veio de range já ACTIVE, incrementou `pos` e
handout accounting antes de entrar no planner, mas ainda não alcança binding, callback, índice vivo ou
public view. Se o plano é descartado, conflita ou falha prewrite, esses números deixam gaps e nunca
podem ser recalculados/reutilizados. O WAL consumer não precisa provar novo F: a reservation anterior
já tornou `F > id` durável. Um batch multi-table preserva uma lista fechada
`(intent_ordinal, table_id, consumed_id, active_nonce)` e o segundo OCC prova que nenhum intent foi
adicionado, removido ou reordenado depois da consumação.

No SHA-base, `HeapStore._extent_for` (`engine/heap_store.py:1105-1125`) aloca página antes de haver
extent, e `allocate_record_id` (`:538-578`) avança o contador via pool. Essas portas não podem ser
chamadas pelo planner v2. Row intents atualmente começam sem ID em
`domain/txn/context.py:53-71`, uma propriedade que deve ser preservada.

## 8. Apply exato, durabilidade e inventário de `flush`

### 8.1 Resultado e enum de apply

`CommitRedoResultV1` carrega `touched_pages`, ordenada e sem duplicatas, e
`files_requiring_barrier`, não somente `touched_files`. Todo resultado
bem-sucedido — `APPLIED`, `IDENTICAL` ou `SUPERSEDED_COMMITTED` — devolve a página e exige write-back
exato. Tanto identical quanto uma imagem posterior comprovada podem existir apenas dirty no pool por
uma tentativa anterior; retry idempotente ainda deve colocá-las no device antes da publicação.
`CONFLICTING_EQUAL_LSN` e `UNPROVEN_FUTURE` devolvem zero touched e recusam a transação inteira.
Logical index apply retorna
explicitamente header, tail relinkado e cada página nova; nunca se infere touched set pelo delta
global de frames modified.

`apply_page_image` retorna exatamente `ApplyPageImageResultV1`, um `u8` fechado:

| Valor | Nome | Significado |
|---:|---|---|
| 0 | `INVALID` | nunca serializável/retornável |
| 1 | `APPLIED` | target image foi instalada no frame |
| 2 | `IDENTICAL` | frame já continha exatamente a target image |
| 3 | `SUPERSEDED_COMMITTED` | frame contém imagem posterior autenticada pela história committed |
| 4 | `CONFLICTING_EQUAL_LSN` | mesmo LSN, bytes/semântica incompatíveis |
| 5 | `UNPROVEN_FUTURE` | imagem posterior sem prova committed exata |

Valores 6..255, `bool`, enum de outra classe ou inteiro fora de `u8` recusam pela seção 0.1.
O resultado não entra no `WalAppendPlan`, no manifest WAL nem em qualquer digest **pré-apply**: isso
criaria dependência do resultado observado e tornaria retry `APPLIED` -> `IDENTICAL` não canônico. Ele
entra uma única vez neste codec pós-apply:

```text
CommitRedoResultV1 = (
  magic=MAGIC_COMMIT_REDO_RESULT_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], source_materialized_lsn u64, target_materialized_lsn u64,
  page_count u32, barrier_target_count u32,
  pages[page_count], barrier_targets[barrier_target_count], crc32c u32
)
commit_redo_page_entry := (
  target_kind u8, apply_result u8, file_binding_kind u8, reserved_zero u8,
  file_identity[32], canonical_relative_path_sha256[32], page_index u64,
  target_page_lsn u64, observed_page_lsn u64,
  target_raw_sha256[32], observed_raw_sha256[32],
  superseding_committed_history_entry_sha256[32]
) # 188 bytes
commit_redo_barrier_target := (
  target_kind u8, barrier_kind u8, file_binding_kind u8, reserved_zero[5],
  file_identity[32], canonical_relative_path_sha256[32]
) # 72 bytes
commit_redo_result_sha256 =
  SHA256(DOMAIN_COMMIT_REDO_RESULT_V1 || canonical CommitRedoResultV1 bytes)
record_length = 60 + 188*page_count + 72*barrier_target_count
```

`CommitRedoTargetKindV1` é `INVALID=0`, `HEAP=1`, `CATALOG=2`, `OVERFLOW=3`, `INDEX=4`;
5..255 recusam. `CommitRedoBarrierKindV1` é `INVALID=0`, `DATA=1`, `FILE_METADATA=2`,
`PARENT_DIRECTORY=3`; 4..255 recusam. Pages são sorted/unique por
`(target_kind,canonical_relative_path_sha256,file_identity,page_index)`; barrier targets, por
`(barrier_kind,target_kind,canonical_relative_path_sha256,file_identity)`. Counts são 0..262144,
o record integral cabe em 64 MiB, source é exatamente a predecessor materializada e target é o P
que a proof sucessora declarará. Todo page entry pertence ao exact target set do plan e todo barrier
target é bijetivo ao `files_requiring_barrier`; omitir/duplicar/reclassificar recusa.

`CommitRedoFileBindingKindV1` é `INVALID=0`, `CURRENT_EXISTING=1`, `CREATED_POSTIMAGE=2`;
3..255 recusam. Em ambos o result pós-apply exige `file_identity` actual nonzero, direct-certified e
igual em toda page/barrier entry do mesmo path. CURRENT precisa igualar a source authority pré-apply;
CREATED precisa corresponder a um `ArtifactImageVectorV1.PLANNED_POSTIMAGE` do plan e a seu binding
posterior namespace/no-alias. O result nunca inventa file identity futura nem aceita ZERO depois do
create.

`APPLIED(1)` registra a preimage observada antes do install, exige history SHA ZERO e direct read final
igual a `target_raw_sha256/target_page_lsn`. Source page existente exige observed raw nonzero/LSN
iguais à exact preimage do plan; page nova `ABSENT→CREATED_POSTIMAGE` exige ambos
`observed_page_lsn=0`/`observed_raw_sha256=ZERO32`. Nenhuma outra combinação zero vale.
`IDENTICAL(2)` exige observed raw/LSN iguais aos target e
history SHA ZERO. `SUPERSEDED_COMMITTED(3)` exige observed LSN estritamente maior, history entry SHA
nonzero que decodifica para a imagem observed exata e direct read final igual à observed; ela nunca é
colapsada em IDENTICAL. Conflitos 4/5 produzem um resultado de recusa separado e nunca um record
publicável. CRC, tamanho, re-encode, ordem e ausência de duplicata são obrigatórios; trocar tag,
target kind, path, raw hash ou history evidence mantendo o restante invalida o resultado.

Para REPLAY/OWN, `device_apply_target_set_sha256` é recomputado sem depender do resultado observado:

```text
SHA256(DOMAIN_RUNTIME_DEVICE_TARGET_SET_V1 || LE32(page_count) ||
       concat_sorted(target_kind_u8 || file_binding_kind_u8 ||
                     projected_file_identity[32] || canonical_path_sha256[32] ||
                     LE64(page_index) || LE64(target_page_lsn) || target_raw_sha256[32]) ||
       LE32(barrier_target_count) || concat_sorted(canonical barrier target entries))
```

Result tag, observed fields e superseding evidence ficam fora dessa
projection, mas permanecem autenticados pelo result record. O digest é nonzero inclusive para o
vector vazio e precisa igualar o exact pre-apply plan target set; projetar observed/final state, trocar
order ou usar o domain de checkpoint recusa.
`projected_file_identity` é a actual identity em CURRENT e ZERO32 em CREATED; o binding kind/path/
target bytes permanecem. Barrier projection aplica a mesma regra. Assim o digest casa ao plan frozen
sem prever inode/file-id, enquanto o result/direct proof ainda autentica a identity actual.

O SHA-base em `engine/buffer_pool.py:1242-1307` trata todo `page_lsn >= target` como skip em
`:1299`; isso mascara equal-LSN diferente e higher-LSN sem prova. V7 decodifica/fingerprinta antes
da decisão. `SUPERSEDED_COMMITTED` requer lookup no `CommittedHistory` por uma transação posterior
que contenha a imagem canônica exata. Os dois estados de conflito recusam publicação. Mutantes que
omitem `IDENTICAL` ou `SUPERSEDED_COMMITTED` de `touched_pages`, ou escrevem somente `APPLIED`, morrem
num retry com frame superior/igual dirty e cold reopen.

Toda transação é preflighted por inteiro antes do primeiro apply. `durable_exact` ordena/deduplica
e chama `write_back(file,page)` somente nas touched pages. Frames dirty não relacionados continuam
dirty e não podem ser tornados duráveis por acidente.

Depois do exact apply e antes de expor sucesso, o commit só pode construir successor process-local se
o `WriteSnapshotProof` consumido autentica predecessor
`RuntimeDeviceReplayProof.materialized_through_lsn == pre_append P`. O successor encadeia esse raw
predecessor hash + todos e somente os próprios grouped effects em `(P,terminal]`, terminal/history e
final-page fingerprints; nunca usa S nem salta `(S,P]`. Só depois de publicar/reler commit.state no
terminal constrói `WalFrontierCertificationV1(DERIVED_OWN_COMMIT)` e depois
`RuntimeDeviceReplayProofV1(OWN_COMMIT)` com o `CommitRedoResultV1` integral e
`materialized_through_lsn=terminal`; não executa action15/16, não cria reader key e não pode emitir
`WalPublicationProofV1`. Um begin posterior sela a frontier corrente com sua própria fresh pin/action16.
Falha deixa as proofs predecessor/recovery-required. Isso não afirma
durabilidade após power loss — commit ordinário continua sem data barrier — e por isso a proof morre
com a runtime. Outro processo que observa P acima de sua proof faz catch-up; não copia a alegação
local do writer.

### 8.2 Classificação completa dos sites `BufferPool.flush` no SHA 539

Esta tabela é normativa para L3. Ela inclui a primitive, os 12 callsites diretos produtivos e as
duas rotas indiretas críticas. “Migrar” significa substituir o whole-file flush do caminho por
`durable_exact(touched_pages)`/write-back exato; “manter” exige o motivo independente do WAL.

| Evidência no SHA-base | Símbolo/uso | Decisão V7 |
|---|---|---|
| `engine/buffer_pool.py:547-563` | `BufferPool.flush` | manter como primitive explícita whole-file |
| `engine/buffer_pool.py:669-685` (`:679`) | `BufferPool.checkpoint -> flush` | manter; checkpoint explícito drena o pool inteiro |
| `engine/commit_redo.py:185-199` | `CommitRedo.flush` | migrar para touched pages |
| `engine/database.py:1592-1603` | `Database.flush` público | manter whole-pool somente pelo protocolo operator/checkpoint com fresh lease + EXCLUSIVE; nunca atalho cru |
| `engine/database.py:1998` | `Database._flush_pages`/close | manter apenas após checkpoint/plan fenced; dirty index sem plan recusa/poison |
| `engine/index_manager.py:352-378` | `IndexStore.create` | CREATE DDL congela target virtual, vira WAL/manifest e só cria IndexV2 pós-barrier com `_IrrevocableWalPermit`; bootstrap nunca cria data path transacional |
| `engine/index_manager.py:544-578` | `IndexStore.mark_stale` | migrar para effect WAL v2 `MARK_STALE`, manifest e header touched; emergency poison é só local/zero I/O |
| `engine/index_manager.py:602-621` | `_lift_short_commit_mark` | remover atalho; recuperação/CLEAR_STALE logged prova completion antes de limpar |
| `engine/index_manager.py:871-892` | `advance_built_through` | somente effect WAL manifestado ou `CheckpointIndexManifest`; header em touched pages |
| `engine/index_manager.py:894-915` | `clear_stale` | migrar para effect WAL v2 `CLEAR_STALE` final de rebuild, exact header touched |
| `engine/index_manager.py:1098-1117` | `note_reconciled` | migrar para `RECONCILE` WAL/manifest; header + buckets em touched pages |
| `engine/index_manager.py:2110-2147` | `IndexManager.commit` | migrar para touched pages do commit |
| `engine/txn_manager.py:2500-2528` | `_apply_images` | migrar para touched pages do commit |
| `engine/txn_manager.py:1246-1247` | redo via `CommitRedo.flush` | migra indiretamente com `CommitRedo` |
| `engine/recovery_manager.py:1454-1455` | recovery via `CommitRedo.flush` | migra indiretamente com `CommitRedo` |

`LESSONS.md:560-578` e `PUNCHLIST.md:805-826` demonstram que deixar esses headers apenas no cache é
incorreto. V7 fecha mais forte: em rota produtiva compartilhada, create/mark/lift/clear/reconcile/
advance não são writes independentes; tornam-se effects WAL v2 do manifest total, aplicados sob
EXCLUSIVE e reportados em touched pages. `_lift_short_commit_mark` deixa de ser mecanismo autônomo.
Somente bootstrap offline de arquivo ainda não exposto pode exact-write pages sem WAL, sob fence
externo e direct cert. Não existe “header não-WAL” público a que um crash precise dar significado.

Os cinco callers de `BufferPool.checkpoint` — `api/assembly.py:436,1595,1604`,
`database.py:MetaStore.create:521` e `txn_manager.py:checkpoint:1043` — são bootstrap/checkpoint
deliberados e mantêm semântica global. Não contam como commit/index apply acidental.

Mutante obrigatório: `BufferPool.flush` lança se chamado de commit, redo, recovery ou index apply;
todos esses fluxos devem passar. Explicit `Database.flush`/close só chamam flush depois do protocolo
checkpoint-like; checkpoint e bootstrap isolado permanecem globais. Mutantes que chamam qualquer uma
das cinco portas antigas fora de WAL/manifest/permit morrem. Um frame dirty sentinela em outro
arquivo/página permanece byte-identical e dirty após durable exact.

### 8.3 Barreiras

Commit ordinário continua com WAL como autoridade e não faz data fsync, como
`CONTRACT.md:667-671`. Exceções identity/format fazem data barrier de `heap.dat` antes da publicação,
pois ativação de range e interpretação de formato dependem da prova física imediata. Recovery e
checkpoint seguem as regras da seção 10.

### 8.4 Publication proof, horizonte relevante e INDEX_VIEW — SUBSTITUI V6

#### 8.4.1 Nenhum snapshot sobre gap durável; frontier WAL não é proof do device

Definem-se por direct reads; P/Q/seed e o seal final são capturados sob COMMIT certification-only,
mas o scan byte a byte ocorre fora de COMMIT:

- `P`: `control/commit.state.last_committed_lsn` publicado;
- `Q`: `checkpoint_lsn` certificado;
- `retained_complete_commits`: terminais COMMIT válidos ainda retidos, todos estritamente acima do
  baseline Q quando o prefixo já foi reciclado;
- `durable_commit_frontier`: `max(Q, retained_complete_commits.commit_lsn)`, com Q como seed
  autenticado do prefixo reciclado — não um COMMIT inventado;
- `tail_evidence`: segmentos/offsets/lengths/CRCs, continuidade, epoch e SHA-256 do scan físico;
- `incomplete`: transações com BEGIN/effects sem terminal, terminal duplicado, record pós-terminal ou
  framing truncado;
- `required_unknown`: records v2 REQUIRED que este build não interpreta em qualquer parte retida do
  WAL. Um record já reciclado só pode estar coberto por Q quando o `WalFeatureFence` e o certificado
  de checkpoint provam que este build conhece integralmente os feature bits que o produziram.

O contrato separa três objetos e proíbe tratá-los como aliases: certificação do frontier WAL sem
reader lifecycle, prova de materialização da runtime ligada a esse frontier e, por último, wrapper
público ligado à action 16. Os codecs são:

```text
WalFrontierCertificationV1 = (
  magic=MAGIC_WAL_FRONTIER_CERTIFICATION_V1, version u16=1, reserved_zero u16,
  record_length u32, database_uuid[16], process_birth_identity[16],
  database_runtime_generation u64, producer_kind u8, reserved_zero[7],
  wal_runtime_mutation_generation u64,
  checkpoint_q u64, published_p u64, durable_commit_frontier u64,
  sealed_physical_tail_length u64, incomplete_count u32, required_unknown_count u32,
  wal_feature_fence_proof_sha256[32], bootstrap_marker_sha256[32],
  checkpoint_state_sha256[32], commit_state_raw_sha256[32],
  committed_history_root_sha256[32], tail_evidence_sha256[32],
  retained_range_set_sha256[32], predecessor_frontier_certification_sha256[32],
  producer_evidence_sha256[32], crc32c u32
) # 404 bytes
wal_frontier_certification_sha256 =
  SHA256(DOMAIN_WAL_FRONTIER_CERTIFICATION_V1 || canonical certification bytes)

WalPublicationProofV1 = (
  magic=MAGIC_WAL_PUBLICATION_PROOF_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], process_birth_identity[16], database_runtime_generation u64,
  coordinator_incarnation_identity[32], scan_pin_generation u64, reader_install_generation u64,
  snapshot_p u64, wal_frontier_certification_sha256[32],
  runtime_device_replay_proof_sha256[32], scan_pin_registry_snapshot_sha256[32],
  pin_to_reader_transition_proof_sha256[32], reader_registry_snapshot_sha256[32],
  converted_reader_record_raw_sha256[32], crc32c u32
) # 308 bytes
wal_publication_proof_sha256 =
  SHA256(DOMAIN_WAL_PUBLICATION_PROOF_V1 || canonical proof bytes)
```

`WalFrontierProducerKindV1` é `INVALID=0`, `FULL_SCAN=1`, `DERIVED_OWN_COMMIT=2`; 3..255
recusam. `FULL_SCAN` exige predecessor ZERO e `producer_evidence_sha256` igual ao SHA de um
`CommittedHistoryEvidenceV1` integral que cobre Q→P, os mesmos retained ranges/tail/history e as
direct reads correntes. `DERIVED_OWN_COMMIT` exige predecessor nonzero/current em `old_P`, Q
inalterado, `published_p=durable_commit_frontier=terminal>old_P`, e:

```text
producer_evidence_sha256 = SHA256(
  DOMAIN_OWN_COMMIT_FRONTIER_EVIDENCE_V1 ||
  predecessor_frontier_certification_sha256[32] ||
  wal_append_draft_fingerprint[32] || grouped_transaction_raw_sha256[32] ||
  terminal_manifest_sha256[32] || commit_redo_result_sha256[32] ||
  commit_state_raw_sha256[32] || tail_evidence_sha256[32] || LE64(old_P) || LE64(terminal)
)
```

Esses fields nunca são receipts soltos. Toda certification viaja/é validada dentro do
`WalFrontierEvidenceEnvelopeV1` da seção 7: `wal_feature_fence_proof_sha256` é o digest do nested
`WalFeatureFenceProofV1`, `tail_evidence_sha256` é o nested `WalPhysicalScanEvidenceV1`,
`retained_range_set_sha256` é o nested `ProtectedWalRangeSetV1` e `producer_evidence_sha256` é o body
FULL/DERIVED exato do envelope. Q/P/runtime/generation/tail length/counts/history/marker/checkpoint
precisam casar em todas as preimages. O locator do scan é a stable WAL root + ranges integrais; pins
retêm a namespace durante seal e qualquer revalidação posterior sem bytes disponíveis refaz FULL_SCAN,
jamais aceita o digest lembrado.

O derived producer aceita somente a grouped transaction que este mesmo mutator acabou de append/barrier,
aplicar exatamente e publicar; o history root sucessor é a extensão bijetiva do predecessor por esse
único grupo. Retry/recovery que não consegue provar essa cadeia faz `FULL_SCAN`; nunca inventa um
producer kind ou reutiliza evidence de outra transaction. Ambos exigem incomplete/unknown counts zero;
qualquer nonzero recusa, não é warning. Q/P/frontier, marker/fence/control/history/ranges casam às
direct reads ou à extensão own-commit exata. A certificação é process-local/frozen e não contém
registration, transition proof nem `RuntimeDeviceReplayProof`.

`WalPublicationProofV1` é exclusivamente o wrapper de snapshot público e só pode ser finalizado
**depois** de existir `WalFrontierEvidenceEnvelopeV1` contendo `WalFrontierCertificationV1(P)` e
`RuntimeDeviceReplayProofV1(P)` independentes
e depois da action 16. Seus nested records devem ser fornecidos integralmente: UUID/runtime/Q/P/
frontier/history/fence casam, `snapshot_p=P=materialized_through_lsn`; nenhum SHA opaco basta. A
transition proof decodifica exatamente action16 sobre a mesma `PUBLIC_BEGIN_WAL_SCAN_PIN` key/nonce,
jamais `FRONTIER_ONLY_WAL_SCAN_PIN`, com predecessor generation
igual a `scan_pin_generation`, successor igual a
`reader_install_generation=checked_increment(predecessor)`, snapshot P e range set histórico
nonzero→ZERO. `scan_pin_registry_snapshot_sha256` é exatamente o raw SHA do predecessor registry
state embedded no transition; `reader_registry_snapshot_sha256` deriva exatamente do successor state.
A reader snapshot deriva do embedded successor registry state e
`converted_reader_record_raw_sha256` é o SHA raw da única entry convertida. Candidate pre-CAS e a
frontier certification pronta **não** são `WalPublicationProofV1` e não chegam a `begin_read_view`.

A ordem de hashes é estritamente acíclica:
`scan/fence/ranges/own-commit evidence → WalFrontierCertification → frontier evidence envelope →
CommitRedoResult/direct checkpoint proof →
RuntimeDeviceReplayProof → action16 transition/successor snapshot → WalPublicationProof`.
Nenhum predecessor contém SHA de um sucessor. Placeholder, ZERO temporário posteriormente trocado,
self-hash/fixed point ou re-encode retroativo recusa.

Fork, remoção/mudança da própria converted key, runtime/coordinator-incarnation change ou WAL mutation
que invalide P revoga o token. Incremento global posterior por registration/close de **outra** key não
o revoga: validade corrente relê a própria key por
`(record_kind,owner_pid,process_birth_identity,manager_identity,seed_nonce)`, exige bytes/snapshot P
iguais e generation atual `>=reader_install_generation`. Regressão, incarnation diversa ou own-key
ausente recusa. Isso evita auto-invalidação na action16 e evita que outro reader encerre transações
legítimas. SHA isolado sem record, transition proof, own-key e scan preimage não é autoridade.

`WalFrontierCertification(P,Q,durable_commit_frontier,tail_evidence,history_sha256)` só existe quando:

1. identity/WAL cutover da seção 5.3 é coerente em todo record retido;
2. prefix recycling é provado e o sufixo físico é contínuo;
3. `durable_commit_frontier == P`. Se `P == Q` e nenhum COMMIT pós-Q está retido, o frontier é Q;
   se `P > Q`, continuidade exige que todos os records necessários em `(Q,P]`, inclusive o COMMIT
   terminal P, ainda estejam retidos e autenticados;
4. não há COMMIT nem effect transaction incomplete acima de P, e não há required desconhecido em
   **nenhum** record retido ou feature bit necessário ao baseline Q;
5. transações completamente ABORTED e segment headers posteriores são estruturalmente válidos e
   provadamente sem efeito; elas entram em `tail_evidence`, mas não movem `durable_commit_frontier`;
6. commit.state, checkpoint e grouped history concordam e nenhum page image posterior foi aceito sem
   terminal cometido exato.

Um COMMIT válido em `(P, durable_tail]` é um `DURABLE_PUBLICATION_GAP` mesmo se heap0 e todos os index
headers parecem saudáveis, pois apply pode ter parado entre páginas. Header/LSN/cache não substituem
completion. Um sufixo incomplete é `INCONCLUSIVE/RECOVERY_REQUIRED`; read-only não o trunca nem
presume abort.

Unknown REQUIRED nunca é considerado mero gap recuperável por este build: ele bloqueia apply,
publication, checkpoint e recycle, mesmo quando aparece dentro de uma transação sem terminal. Uma
extensão futura precisa primeiro elevar duravelmente o feature/version fence que builds antigos
recusam; não pode depender de o record sobreviver ao recycle.

`TransactionManager.begin` usa um sandwich, dentro de participant:

1. entra em COMMIT somente para certification, sem lease, INDEX_VIEW, page pin, graph guard, clock,
   callback ou métrica;
2. lê direto meta/fence, os dois checkpoint slots, commit.state e inventário WAL, e congela
   `WalScanSeed(P,Q,raw_state_hashes,segment_file_ids_sizes,tail_offset,
   wal_runtime_mutation_generation)`. O seed materializa canonicamente o
   `ProtectedWalRangeSetV1(Q,P,floor locator,physical tail,exact ranges)` integral. A admission registry
   reserva atomicamente o lifecycle máximo de três increments e só então registra
   `PUBLIC_BEGIN_WAL_SCAN_PIN(snapshot_lsn=P,seed_nonce,range_set_bytes)` por action 15; falta de headroom
   recusa antes do registro. P, e não Q, é o snapshot da registration e será preservado pela action 16;
   Q permanece no range set como floor do scan. O pin impede recycle que cruze o locator Q e qualquer
   mutação dos ranges enumerados, mas ainda não é snapshot público; sai de COMMIT;
3. fora de COMMIT, escaneia exatamente os byte ranges congelados, valida record CRC/framing/version,
   constrói `CommittedHistory`, history root e `WalFrontierCertificationCandidate`. Closed segments são
   imutáveis; append no tail só ocorre depois do frozen offset. Todo append/truncate/recycle/repair
   interno incrementa `wal_runtime_mutation_generation` no carrier estável do WAL;
4. reentra em COMMIT, relê diretamente todos os campos do seed, file identities/sizes/tail offset,
   generation e os bytes/SHA integrais do range set instalado. Qualquer drift termina o scan pin pela
   action 17 já reservada, sai e reinicia pelo
   budget; nunca mistura candidate antigo com seed novo;
5. com seed bit-identical, valida as seis condições. Se já existe frontier process-local current
   (FULL_SCAN ou DERIVED_OWN_COMMIT) cujos fields/tail/history/producer evidence casam integralmente ao
   candidate direto, reutiliza exatamente seus bytes/SHA; senão finaliza
   `WalFrontierCertificationV1(FULL_SCAN)` a partir do candidate. Exige ainda
   `RuntimeDeviceReplayProof` live que referencie exatamente a frontier selecionada e seja bit-identical em DB
   UUID/fence/Q/P/runtime generation;
   então action 16 converte atomicamente o scan pin na **mesma** reader key exatamente em P, zera
   length/SHA/bytes do range set, consome uma liability e preserva a última para terminate. O CAS retorna
   `ProcessRegistryTransitionProofV1` + successor state; ainda sob COMMIT, o manager deriva a
   `ReaderRegistrySnapshotV1` successor, finaliza/valida o wrapper `WalPublicationProofV1`, relê own-key e
   confirma que WAL/runtime não mudou. Só agora fixa `Snapshot(P)` e instala
   `BufferPool.begin_read_view(global_token)` com WAL/device/transition proofs detached. Essa é pin
   lógica de snapshot, não page/frame pin nem graph guard;
6. sai de COMMIT/participant e só então publica métrica/objeto ao caller.

Como o segundo COMMIT exclui append/publication durante seal+registro, não é necessária a antiga
escolha `max(first_read, second_read)`. Um writer posterior lineariza depois de P; a pin preserva o
horizonte. Falha em qualquer passo remove scan/reader pin parcial antes da saída. O scan completo não
segura COMMIT, embora seu custo continue real e seja declarado na seção 12.3.

Gate multiprocess obrigatório usa `Q<P`: action 15 precisa persistir `snapshot_lsn=P` e range-set
integral com `checkpoint_q=Q`; action 16 conserva P, zera exatamente os bytes do set e
`oldest_active_snapshot_lsn` observa P, nunca Q. Mutantes que gravam Q no field snapshot, trocam Q/P
no set, omitem uma entry intermediária, aceitam digest sem preimage, alteram file identity/range após
o install ou deixam bytes não zero depois da conversão recusam antes do handout. Mutante que tenta
hashear conteúdo WAL no primeiro COMMIT, grava placeholder content SHA no pin ou aceita scan evidence
sem SHA final por range também falha. Em paralelo, append
após o physical tail é permitido com generation bump e faz o scanner reiniciar; truncate/recycle/
rename/delete que toque floor/intervalo recebe conflito sem tocar WAL. Owner-death prova que outro
processo consegue decodificar a mesma preimage integral e executar action 17 sem cache do owner.

A reader registration permanece cross-process e live por toda a Transaction, inclusive entre
statements, até commit/abort/close/owner-death comprovado; INDEX_VIEW SHARED continua sendo por
statement. O coordinator fornece atomicamente:

```text
ReaderRegistrySnapshotV1 = (
  magic=MAGIC_READER_REGISTRY_SNAPSHOT_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], database_runtime_generation u64,
  coordinator_incarnation_identity[32], process_registry_generation u64,
  terminal_cleanup_liability u64,
  registration_count u32, reserved_zero u32, registrations_sha256[32],
  oldest_active_snapshot_lsn u64, crc32c u32
) # exatamente 140 bytes
```

Registration/deregistration e scan/checkpoint-reader-floor transitions usam exclusivamente o
`PROCESS_REGISTRY_GENERATION`/coordinator incarnation externo da seção 3.1.1, sem escrever o database
`StorageDevice`; cada action incrementa exatamente uma vez e a admission reserva cleanup antes do
install. Não faz wrap/TTL eviction dentro da incarnation. O
digest cobre nonce, process-birth/database generation e snapshot LSN de todas as registrations
ordenadas; count zero exige digest ZERO32 e oldest=P, nonzero exige min exato. O snapshot SHA usa
`SHA256(DOMAIN_READER_REGISTRY_SNAPSHOT_V1||canonical bytes)`. Separadamente, o system-floor generation/map SHA são lidos do
WalCoordination carrier e entram no `CheckpointPreparation`; misturar os dois counters ou aceitar só
um deles recusa. Do `PendingOuterOperationMap` current derivam-se duas projeções disjuntas e canônicas:

- `active_checkpoint_ceiling`: considera **somente** entries `NONTERMINAL`; se não há nenhuma é P,
  senão é o terminal COMMIT completo imediatamente anterior ao BEGIN do MARK mais antigo desse
  subconjunto. Nunca se usa `MARK.commit_lsn-1`, pois isso pode cair dentro do grouped transaction;
- `terminal_retention_floor`: conjunto ordenado de entries
  `TERMINAL_PENDING_FLOOR_RELEASE`, com sua system-floor key, MARK/body/CLEAR locators, history root e
  retention boundary. Não reduz Q; ele apenas impede recycle até checkpoint incorporation + action 14.

Se o MARK ativo é a primeira transação retida após Q, seu predecessor é exatamente o baseline Q
autenticado pelo checkpoint/history root. Ausência/ambiguidade de boundary, entry que aparece nas duas
projeções ou mismatch carrier/history é INCONCLUSIVE, não aritmética.

`requested_Q` é só uma preferência u64 exact, não uma boundary. O grouped history calcula
`canonical_requested_Q = max({current_Q} ∪ {commit_lsn de terminal COMMIT completo e validado |
commit_lsn <= min(requested_Q,P)})`. Request menor que current_Q recusa; ausência, incomplete/
unknown-required ou duas interpretações da maior boundary recusam. Nunca se usa o LSN de BEGIN/effect/
SEGMENT_HEADER nem `requested_Q` raw. Checkpoint só escolhe
`target_Q = min(canonical_requested_Q, P, oldest_active_snapshot_lsn,
  active_checkpoint_ceiling)` e exige
`target_Q >= current_Q`; igualdade
é no-op. Registration live com S < current_Q é estado impossível/coordination poison e recusa, não
autoriza elevar S ou ignorar o reader. Logo Q pode alcançar o snapshot mais antigo, nunca ultrapassá-lo. Sua primeira passagem
congela o registry snapshot, mapa e três projeções; a segunda os relê bit-identical sob COMMIT imediatamente
antes do plan mutável. O `CheckpointPreparation` autentica separadamente
`active_checkpoint_ceiling`, `active_pending_set_sha256`, `terminal_retention_floor_sha256` e o exact
`terminal_release_set_sha256` com **todas** as keys SYSTEM terminais cujo CLEAR/BOUND completo está
incorporado em `target_Q` (inclusive se `terminal_commit_lsn<=current_Q`); nenhum
digest pode ser reutilizado entre essas projeções. Action 12 instala atomicamente **somente**
`READER_FLOOR_PIN(target_Q,process_registry_generation,owner_kind=PROCESS)` no coordinator externo e
reserva ali sua action 13. Separadamente, se há entries NONTERMINAL sem record SYSTEM idêntico, action
24 publica numa única successor generation o set completo de
`PENDING_OPERATION_FLOOR_PIN(owner_kind=SYSTEM_OPERATION,outer_operation_identity,full_plan_sha,
retention_boundary)` ausente. O map SHA/active ceiling autentica o conjunto, mas não é owner identity.
Admission durável reserva uma action 24 mais uma future action 14 por key nova; records SYSTEM já
existentes mantêm a liability previamente reservada e não são duplicados. Depois de adquirir todas as
locks/capabilities na ordem global, o substep action24 recertifica conjuntamente seu batch plan/
entry bases,
system preimage, successor/headroom/liabilities e pending history antes do CAS. Sem espaço para
action24 + **todas** as futuras action14, recusa antes de qualquer PROCESS floor ou SYSTEM write.
Action12 só é instalada na mutation pass posterior, quando action24 já está direct-certified; qualquer
falha depois de action12 usa action13 sob o budget original. Nunca se tenta “desfazer” action24 por
falha de checkpoint: seu floor conservador permanece e o checkpoint seguinte o incorpora. MARK→CLEAR
sem action24 não gera key nem release liability.
Novo begin
que linearize depois recebe S >= P >= target_Q; um begin iniciado antes precisa revalidar Q/P/seed e
não consegue registrar snapshot antigo depois do seal.

Todo cut/recycle/checkpoint usa a mesma proof de boundary fechada; não existe u64 nu:

```text
BoundaryProofV1 = (
  magic=MAGIC_BOUNDARY_PROOF_V1, version u16=1, proof_kind u8, boundary_kind u8,
  record_length u32, database_uuid[16], checkpoint_q u64, frontier_p u64,
  boundary_lsn u64, terminal_segment_number u64, terminal_record_offset u64,
  terminal_record_length u32, wal_format_version u8, reserved_zero[3],
  terminal_epoch u64, terminal_txn_id u64, terminal_transaction_uuid[16],
  terminal_record_raw_sha256[32], terminal_manifest_sha256[32],
  committed_history_root_sha256[32], checkpoint_state_sha256[32], crc32c u32
) # exatamente 244 bytes
boundary_proof_sha256 = SHA256(DOMAIN_BOUNDARY_PROOF_V1 || canonical record bytes)

RequestedCheckpointBoundaryProofV1 = (
  magic=MAGIC_REQUESTED_BOUNDARY_PROOF_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], requested_q u64, current_q u64, frontier_p u64,
  canonical_requested_q u64, boundary_proof_length u32,
  boundary_proof_sha256[32], boundary_proof_bytes[boundary_proof_length], crc32c u32
)
requested_checkpoint_boundary_proof_sha256 =
  SHA256(DOMAIN_REQUESTED_BOUNDARY_PROOF_V1 || canonical record bytes)
```

`BoundaryProofKindV1` é `INVALID=0`, `CHECKPOINT_CANONICAL_REQUEST=1`, `RECYCLE_PREFIX=2`,
`RECOVERY_TAIL=3`, `CUTOVER_FENCE=4`; 5..255 recusam. `BoundaryKindV1` é `INVALID=0`,
`BASELINE=1`, `V1_COMMIT=2`, `V2_COMMIT=3`; 4..255 recusam. BASELINE exige
`boundary_lsn==checkpoint_q<=frontier_p`, locator/format/epoch/txn/UUID/terminal raw/manifest todos
zero e history/checkpoint SHAs nonzero. V1/V2 exigem
`checkpoint_q<=boundary_lsn<=frontier_p`, locator inteiro in-bounds, terminal length/raw/history/
checkpoint SHA e epoch/txn nonzero; V1 exige `wal_format_version=1`, UUID ZERO16, manifest ZERO32 e
grouped grammar v1; V2 exige format 2, UUID/manifest nonzero e UUID bit-identical em BEGIN/COMMIT.
SEGMENT_HEADER nunca satisfaz terminal locator. Kind `CUTOVER_FENCE` admite somente V1 fence terminal;
kind CHECKPOINT/RECYCLE/RECOVERY admite BASELINE/V1/V2 conforme a history real, sem inventar semântica
V2. Apenas uma route específica de cold read/compaction que declare explicitamente quiescência pode
adicionar `frontier_p==checkpoint_q`; esse requisito jamais deriva de BASELINE por si só. Cross-kind
zero/nonzero, unknown format, terminal que não produz exatamente o history root ou
checkpoint state de Q recusa.

O wrapper requested contém exatamente um `BoundaryProofV1` de kind
CHECKPOINT_CANONICAL_REQUEST, length 244, DB/current/P iguais e
`boundary_lsn==canonical_requested_q`; seu hash e preimage são recomputados. `requested_q>=current_q`,
canonical é a maior terminal boundary completa `<=min(requested_q,frontier_p)` e não pode exceder P.
Ele entra no `CheckpointPreparation`, é redecodificado/recomputado da mesma `CommittedHistory` na
segunda passagem e precisa continuar apontando à maior boundary ≤ request; cache de
`canonical_requested_Q` isolado não serve. Golden vectors cobrem BASELINE, fence V1 e grouped V2;
mutantes de UUID/format/kind/locator, interior BEGIN/effect, raw SHA e wrapper/base swap recusam.

O release de floors SYSTEM também possui preimage integral estável, separada da admission de cada
attempt:

```text
TerminalFloorReleaseRecipeV1 = (
  magic=MAGIC_TERMINAL_FLOOR_RELEASE_RECIPE_V1, version u16=1, reserved_zero u16,
  record_length u32, database_uuid[16], source_pending_outer_map_sha256[32],
  source_system_floor_generation u64, source_system_map_slot u8, reserved_zero[7],
  source_system_map_length u32=655412, source_coordination_length u32=204,
  release_count u32, entries_length u32, source_system_map_raw_sha256[32],
  source_coordination_raw_sha256[32], release_set_sha256[32],
  source_system_map_bytes[source_system_map_length],
  source_coordination_bytes[source_coordination_length], entries[release_count], crc32c u32
)
terminal_floor_release_recipe_entry := (
  entry_length u32=272, logical_ordinal u32, persisted_index_identity[32],
  terminal_commit_lsn u64, system_floor_key_sha256[32],
  system_floor_record_raw_sha256[32], system_floor_record_bytes[160]
)
terminal_floor_release_recipe_sha256 =
  SHA256(DOMAIN_TERMINAL_FLOOR_RELEASE_RECIPE_V1 || canonical recipe bytes)

TerminalFloorReleasePlanV1 = (
  magic=MAGIC_TERMINAL_FLOOR_RELEASE_PLAN_V1, version u16=1, release_reason u8, reserved_zero u8,
  record_length u32, database_uuid[16], checkpoint_generation u64, checkpoint_q u64,
  checkpoint_state_raw_sha256[32], checkpoint_manifest_sha256[32],
  committed_history_root_sha256[32], release_recipe_length u32,
  transition_plan_length u32, release_recipe_sha256[32],
  generation_transition_plan_sha256[32],
  release_recipe_bytes[release_recipe_length],
  transition_plan_bytes[transition_plan_length], crc32c u32
)
terminal_floor_release_plan_sha256 =
  SHA256(DOMAIN_TERMINAL_FLOOR_RELEASE_PLAN_V1 || canonical record bytes)
```

`TerminalFloorReleaseReasonV1` é `INVALID=0`, `CHECKPOINT_INCORPORATED=1`; 2..255 recusam. SYSTEM floor
segundo projection kind. A recipe tem count 1..4096, `entries_length=272*count`,
`record_length=196+655412+204+entries_length<=1769924`; entries são sorted/unique por
`(system_floor_key_sha256,persisted_index_identity)`, com ordinals densos. Source map/coord bytes
decodificam integralmente e seus raw SHAs/generation/slot/count/set casam; cada embedded 160-byte
record existe bit-identical no map, seu raw SHA/key/terminal identity casa ao pending entry e sua
entry-base SHA recomputa. O decoder reconstrói o `PendingOuterOperationMapV1` source integral a partir
da grouped history ainda retida **e do map M0 embedded**, inclusive keys já removidas por um prefixo de
actions14, re-encoda seus bytes e exige SHA igual a `source_pending_outer_map_sha256`. Ele não consulta
um cache/RAM map current nem tenta derivar a preimage antiga de um digest isolado.

O checkpoint manifest persiste a recipe integral **antes** da primeira action14 e seu próprio digest
cobre esses bytes. O release plan exige `release_recipe_length==recipe.record_length`, embedded recipe
bit-identical à do manifest, transition length nonzero e
`1<=transition_plan_length<=1048576`,
`record_length=220+release_recipe_length+transition_plan_length<=4194304`. Portanto source M0 e todo
record removido continuam disponíveis mesmo depois de alternar/overwrite M0/M1; outro checkpoint é
bloqueado até o plan terminal. CHECKPOINT_INCORPORATED exige checkpoint generation/Q/state/manifest/
history/recipe todos nonzero e cada recipe entry bijetiva a uma `release_eligible=1` do exact
checkpoint manifest e ao record SYSTEM source.
Qualquer checkpoint field ZERO, abort projection, floor sem MARK/action24 origin ou matrix alternativa
recusa.

O transition bytes decodifica exatamente um `GenerationTransitionPlanV1` semantic code 12 com
`semantic_plan_source_sha256==release_recipe_sha256` e N entries do mesmo
SYSTEM_FLOOR domain, action 14 e predecessor/successor `g+i→g+i+1`; cada entry de release repete a
mesma edge derivada da recipe. O decoder simula M0: target i é a canonical preimage anterior sem
exatamente a próxima recipe key e source i+1 é target i; cada target coordination slot aponta
ao target map/generation; logo recovery reabre as preimages do carrier e não reconstrói subset de hash.
O fresh `GenerationSuccessorSetV1`/admission referencia o stable transition-plan SHA e cobre apenas o
suffix ainda não direct-adopted, sob o budget/attempt/lease atuais. Extra/missing key, map/coord chain
quebrada, release order diferente, plan hash-only sem bytes ou fresh edge fora do stable plan recusa
antes de publication.

O pending pin system-owned não expira e qualquer tentativa incompatível de criar/avançar outro pending
na mesma identity é probe nonblocking sob COMMIT ou espera apenas fora de COMMIT pelo mesmo
`OperationBudget`. O **exact next substep** do mesmo outer/full plan e seu CLEAR podem avançar depois
de recertificar o pin; o record não troca owner/map a cada substep. O `NONTERMINAL` ceiling desaparece
imediatamente quando a história current prova CLEAR/BOUND terminal válido, mas o
`terminal_retention_floor` permanece. Após um checkpoint successor direct-certified cujo Q/history/
manifest e `terminal_release_set_sha256` incorporam esse terminal integralmente, action 14 executa um
`TerminalFloorReleasePlanV1` fechado acima: sob fresh admission/lease/COMMIT, recertifica checkpoint,
terminal history e exact system records. O plano ordena as N keys lexicograficamente e contém N
arestas estáveis `SYSTEM_FLOOR_GENERATION`/action14 `g→g+1→...→g+N`; o fresh successor set/admission
reserva/claims o suffix restante antes do primeiro step. Cada step
publica um `SystemOperationFloorMapSlotV1` successor sem **exatamente uma** próxima key, depois um
`WalCoordinationSlotV1` successor `current→checked(current+1)` que o referencia, e direct-certifica os
dois antes do step seguinte. Não existe publication única com jump g→g+N nem remoção de várias keys
por uma aresta. A ausência produzida por cada step é sua condição terminal — ausência anterior jamais
é precondição. Crash encontra o maior prefixo direct-valid, verifica que as keys anteriores faltam e
as restantes persistem e retoma a próxima aresta do mesmo manifest; subset diferente, reorder ou key
extra recusa. Release plan sem MARK completo, sem terminal BOUND/CLEAR ou sem
CHECKPOINT_INCORPORATED é malformed e não pode consumir generation/liability. Action 13 libera no coordinator externo o reader floor PROCESS depois da checkpoint
publication; cada action 14 consome o incremento/liability reservado de um system record e custa uma
map+header publication. Recycle só pode
ocorrer numa passagem posterior que observe o checkpoint e o map successor direct-valid; observar
header CLEAN, PID morto ou state RAM não basta. Kill antes/depois de qualquer slot/barrier mantém a
preimage ou um successor adotável bit-identical; cold recovery retoma o mesmo release plan sob fresh
lease e nunca transforma record SYSTEM em PROCESS.

Reconcile/REMOVE congela e revalida a mesma registry proof. Define-se
`safe_reconcile_cutoff = oldest_active_snapshot_lsn`; uma versão/entry só pode ser removida fisicamente
se seu limite superior de visibilidade é **estritamente menor** que o cutoff. Tombstone/versão cuja
boundary é igual ao snapshot mais antigo permanece. `reconciled_through_lsn` nunca excede esse cutoff.
Mudança de registry antes do primeiro WAL byte descarta o plan; depois do seal, o floor pin impede
registro abaixo do corte até direct cert/publication. Assim uma transaction pausada entre statements
retoma no mesmo S sem perder versions, tombstones ou WAL necessários.

`wal_runtime_mutation_generation` é durável e pertence ao namespace
`WAL_COORDINATION/<database_uuid>` do manifest de bootstrap. `WAL_SCAN_PIN` também é interprocesso,
mas pertence exclusivamente ao `ProcessRegistryStateV1` externo e apenas autentica
`database_uuid/runtime/coordinator incarnation` mais o range WAL durável; não reside nem incrementa
este carrier. O carrier WAL obedece ao mesmo
contrato de inode/file identity estável dos carriers de índice: create-exclusive em genesis/bootstrap,
nunca replace/truncate/unlink enquanto uma database generation possa ter handle, lock do OS e dois
slots fixed-size CRC-valid no mesmo carrier. Missing/corrupt não é recriado online. Um backend
non-file precisa provar a mesma identidade única e durável do lock/state entre processos e os mesmos
bytes/offsets/atomicity abaixo.

```text
WAL_COORDINATION_SLOT_BYTES_V1 = 204
WAL_COORDINATION_SLOT_COUNT_V1 = 2
WAL_COORDINATION_HEADER_BYTES_V1 = 408
WAL_COORDINATION_FILE_BYTES_V1 =
  2*204 + 2*655412 = 1311232

WalCoordinationSlotV1 = (
  magic=MAGIC_WAL_COORDINATION_SLOT_V1, version u16=1, slot_index u8, reserved_zero u8,
  slot_length u32=204, database_uuid[16],
  database_runtime_generation u64, writer_lease_epoch u64,
  coordination_namespace_generation u64, wal_runtime_mutation_generation u64,
  device_replay_generation u64, system_floor_generation u64,
  system_terminal_cleanup_liability u64, system_operation_floor_count u32, reserved_zero u32,
  system_operation_floor_min_boundary u64, system_operation_floor_set_sha256[32],
  system_floor_map_slot u8, reserved_zero[7],
  system_floor_map_publication_generation u64,
  system_floor_map_raw_sha256[32], pending_tail_phase0_state u8, reserved_zero[7],
  pending_tail_phase0_predecessor_generation u64, crc32c u32
) # exatamente 204 bytes
```

Offsets fixos são slot 0 em 0, slot 1 em 204, system map 0 em 408 e system map 1 em 655820; EOF
exato é 1311232. `slot_index` precisa casar com o offset. CRC32C cobre os primeiros 200 bytes e o raw
SHA cobre os 204. Os seis counters começam em 1 no genesis/bootstrap e usam somente
`checked_increment_u64`; o `system_floor_map_publication_generation` não é counter adicional: é cópia
exata do `system_floor_generation` que publicou aquele map, começa em 1 e permanece <= o
system-floor current. Map slot é 0 ou 1 e seu raw SHA precisa casar o slot indicado; count/min/set SHA
precisam ser derivados dos records dessa imagem.

`PendingTailPhase0StateV1` é `NONE=0`, `HELD_OPEN=1`; 2..255 recusam. NONE exige
`pending_tail_phase0_predecessor_generation=0`. HELD_OPEN só pode nascer na action 8 da phase 0 do
semantic code 11: o mesmo slot publica `wal_runtime_mutation_generation g→g+1`, grava predecessor `g`
e mantém todos os demais fields conforme as edges simultâneas autorizadas. Seu target semantic digest
inclui essa projection do slot; portanto pending state e generation são uma única publication durável,
não duas janelas. Enquanto HELD_OPEN, qualquer lease/runtime/header publication permitida copia os
dois pending fields bit-identical e toda outra mutação WAL, checkpoint recycle ou segundo tail
composite recusa. Somente (a) a continuation phase-0 exata descrita na seção 10.3 ou (b) a phase-1 edge
do mesmo composite depois do event direct-certified pode avançar; (b) incrementa WAL generation para
`g+2` e limpa atomicamente para NONE/zero. Slot HELD_OPEN sem source reconstruível, predecessor cujo
`checked_increment` não seja o current, event de outro composite ou limpeza sem event terminal é
CORRUPT/INCONCLUSIVE. Assim fresh writer-lease publications após owner death não apagam a authority.

O vetor de autoridade é, nessa ordem, os seis counters. Uma publicação escreve o slot de header
inativo integralmente e seu successor precisa dominar componente a componente o predecessor, sem
regressão; cada componente diferente é exatamente `checked_increment(predecessor)` e casa
bijetivamente com uma aresta claim/permit do `GenerationSuccessorSetV1`; componentes não listados são
bit-identical. Nenhum update é válido com vetor igual. Cold selection aceita exatamente um slot
CRC-valid cujo vetor domina o outro; vetores incomparáveis, dois slots válidos com vetor igual — mesmo
se os demais bytes coincidirem —, salto, geração sem action ou EOF/trailing byte recusam. O slot
inativo inicial é todo ZERO e portanto não é um slot válido. Update faz write-all, visibility barrier,
direct re-read/decode/hash antes de liberar o lock. Este carrier contém somente records
`SYSTEM_OPERATION`; sua liability precisa casar com a soma conservadora das keys do map. Update de
system generation/map/liability é uma publication única sob o lock canônico. Owner-death, coordinator
restart ou cold open não remove nenhuma key SYSTEM; somente action 14 com terminal proof fechado pode
fazê-lo. PROCESS pins/readers são validados no coordinator externo e nunca são inferidos destes bytes.

PROCESS records vivem no coordinator/OS e podem ser terminados por owner-death. SYSTEM_OPERATION não
depende deles nem de reconstruir um subset a partir de SHA: o mesmo carrier contém dois map slots
alternados de tamanho fixo.

```text
SYSTEM_FLOOR_RECORD_BYTES_V1 = 160
SYSTEM_FLOOR_MAP_CAPACITY_BYTES_V1 = 4096 * 160 = 655360
SYSTEM_FLOOR_MAP_SLOT_BYTES_V1 = 48 + 655360 + 4 = 655412

SystemFloorInstallEntryBaseV1 = (
  magic=MAGIC_SYSTEM_FLOOR_INSTALL_ENTRY_BASE_V1, version u16=1, reserved_zero u16,
  record_length u32=148, database_uuid[16], outer_operation_identity[32],
  full_plan_sha256[32], retention_boundary u64, install_checkpoint_generation u64,
  install_pending_entry_sha256[32], crc32c u32
)
system_floor_install_entry_base_sha256 =
  SHA256(DOMAIN_SYSTEM_FLOOR_INSTALL_ENTRY_BASE_V1 || canonical entry-base bytes)

SystemFloorInstallBatchPlanV1 = (
  magic=MAGIC_SYSTEM_FLOOR_INSTALL_BATCH_PLAN_V1, version u16=1, reserved_zero u16,
  record_length u32, database_uuid[16], source_system_floor_generation u64,
  target_system_floor_generation u64, source_map_slot_raw_sha256[32],
  source_floor_set_sha256[32], pending_outer_operation_map_sha256[32],
  new_record_count u32, entries_length u32,
  entry_bases[new_record_count], crc32c u32
)
system_floor_install_batch_plan_sha256 =
  SHA256(DOMAIN_SYSTEM_FLOOR_INSTALL_BATCH_PLAN_V1 || canonical batch-plan bytes)

SystemOperationFloorMapSlotV1 = (
  magic=MAGIC_SYSTEM_FLOOR_MAP_V1, version u16=1, record_bytes u16=160,
  record_count u32, records_length u32, reserved_zero u32,
  system_floor_generation u64, database_uuid[16],
  records[record_count], zero_padding[655360-records_length], crc32c u32
)
SystemOperationFloorRecordV1 = (
  owner_kind u8=SYSTEM_OPERATION, record_format u8=1, reserved_zero[14],
  outer_operation_identity[32], full_plan_sha256[32], retention_boundary u64,
  install_checkpoint_generation u64, install_pending_entry_sha256[32],
  install_entry_base_sha256[32]
) # exatamente 160 bytes
```

Cada entry-base é self-contained e seu digest é recomputável somente do `database_uuid` do header do
map target mais os cinco fields per-record repetidos no target record, sem source slot, batch SHA ou
future output. `install_pending_entry_sha256` é
`SHA256(DOMAIN_SYSTEM_FLOOR_PENDING_ENTRY_V1 ||` a projection fixed-width
`database_uuid || persisted_index_identity || outer_operation_identity || full_plan_sha256 ||
workflow_kind_u8 || mark_transaction_uuid || LE64(mark_commit_lsn) ||
LE64(mark_predecessor_complete_commit_lsn) || plan_source_sha256 || LE64(retention_boundary) ||
installed_system_floor_key=ZERO32)` da única NONTERMINAL grouped entry. Ela é rederivável da history/
operation manifest mesmo depois que outros floors foram instalados/liberados; nunca é SHA do map
global histórico. O batch tem
`target=checked_increment(source)`, `entries_length=148*new_record_count`, count 1..4096,
`record_length=156+entries_length<=606364` e entry bases sorted/unique por
`(outer_operation_identity,full_plan_sha256,install_pending_entry_sha256)`. A terceira chave permite
N identities do mesmo full BIND/rebuild plan sem colisão; omiti-la ou deduplicar apenas por outer/full
recusa. Cada base precisa estar ausente no source e
`source_record_count + new_record_count <= MAX_SYSTEM_PENDING_RECORDS_V1` por soma checked; o target
map é exatamente a união sorted source+novos e não pode remover/alterar record preexistente.
Cada uma corresponde a NONTERMINAL grouped MARK sem floor e repete a pending-entry projection; sua preimage não
contém o próprio digest, target map ou generation transition. O target
`SystemOperationFloorRecordV1` deriva copiando os cinco fields per-record da entry base; o
`database_uuid` vem exclusivamente do enclosing map header. Anexa exatamente seu
`system_floor_install_entry_base_sha256`; records já existentes preservam seu digest.
`system_floor_key_sha256` é, sem outro encoding ou domain,
**exatamente `system_floor_install_entry_base_sha256`**. O field
`installed_system_floor_key_sha256` do `PendingOuterOperationMapV1` e o field homônimo da release
recipe precisam repetir esse valor; `system_floor_record_raw_sha256` continua sendo o SHA256 raw sem
domain dos 160 bytes do record e não pode substituir a key. Como a pending-entry projection usa key
ZERO antes de formar a entry base, a derivação é acíclica:
`grouped pending projection → entry base → floor key/record → pending entry installed`.
Assim cada record ainda live valida isoladamente depois de N alternâncias/releases, mesmo quando o
source map do batch foi sobrescrito.

`records_length == record_count*160` checked; records são sorted/unique por
`system_floor_install_entry_base_sha256` (a floor key), identities/plan/map/install-entry SHA são nonzero e
count é 0..4096.
Padding é integralmente zero e CRC cobre header+capacidade inteira; trailing/slot menor recusa. Count
zero exige header min-boundary 0/set SHA ZERO32 no coordination slot; count nonzero exige min igual ao
mínimo dos records e set SHA `SHA256(DOMAIN_SYSTEM_FLOOR_SET_V1 || records bytes)`. Header coordination referencia
exatamente slot 0 ou 1, a mesma system-floor generation e seu raw SHA; generation/hash/count/min precisam
casar. Dois map slots máximos divergentes sem header autoritativo são corruption.

Antes da action24, o coordinator congela `SystemFloorInstallBatchPlanV1`, um
`GenerationTransitionPlanV1(semantic code 13, source=batch-plan SHA, action24 target=exact target map SHA)` e
o fresh SuccessorSet/admission; recertifica source slot/pending map/generation sob as locks canônicas e
só então obtém o permit exact-type. Action 24 escreve/barrier/direct-certifica primeiro o map slot
inativo completo com todo o set novo e só então publica o coordination slot successor que o
referencia; action 14 faz a mesma ordem removendo exatamente a key
terminal. Crash pré-header deixa map antigo autoritativo; pós-header encontra a preimage inteira nova.
Crash entre target-map write e header usa source slot + target new records/entry-base bytes para
recomputar o batch/edge e adotar ou descartar somente por exact proof; não inventa checkpoint manifest.
`PendingSystemFloorPublicationGateV1` proíbe outra action14/24 ou overwrite de qualquer map slot até
resolver esse target/header e direct-certificar exatamente um authority. Depois disso o batch pode
ser descartado, pois cada record carrega uma entry-base verificável. Sem esse gate, source slot ou
entry base, adoption recusa.
Outras publications do WAL carrier copiam a referência sem reescrever o map. O record continua no map
depois de CLEAR/BOUND como `TERMINAL_PENDING_FLOOR_RELEASE` lógico até action 14; grouped history prova
se ele é ainda NONTERMINAL ou terminal, mas nunca seleciona um subset por hash. O
`PendingOuterOperationMap` inclui ambas as states enquanto o carrier contém a key. Cold open lê o map
slot primeiro, proíbe recycle abaixo do mínimo, valida cada body contra history/marker/checkpoint e só
então assume/libera. Missing history, record extra, body divergente ou slot sem key quando Q já
atravessou a boundary é CORRUPT/INCONCLUSIVE. Assim crash CLEAR→release, releases parciais A..N,
shutdown limpo ou morte de todos os PID owners nunca perde nem torna exponencial o floor set.

Toda rota de mutação congela primeiro um `WalMutationDraftV1` detached:

```text
WalMutationDraftV1 = (
  magic=MAGIC_WAL_MUTATION_DRAFT_V1, version u16=1, operation_kind u8, target_kind u8,
  record_length u32,
  database_uuid[16], manager_identity_sha256[32], process_birth_identity[16],
  coordination_namespace_generation u64, commit_section_nonce[32],
  predecessor_mutation_generation u64, successor_mutation_generation u64,
  source_inventory_sha256[32], source_preimage_sha256[32],
  exact_ranges_count u32, operation_body_length u32,
  wal_scan_pin_snapshot_length u32, reserved_zero u32,
  exact_ranges[exact_ranges_count],
  wal_scan_pin_snapshot_sha256[32], boundary_proof_sha256[32],
  target_postimage_sha256[32], ledger_quarantine_plan_sha256[32], barrier_plan_sha256[32],
  operation_body[operation_body_length],
  wal_scan_pin_snapshot_bytes[wal_scan_pin_snapshot_length], crc32c u32
)
exact_mutation_range := (file_identity[32], start u64, end u64) # 48 bytes
wal_mutation_draft_fingerprint = SHA256(DOMAIN_WAL_MUTATION_DRAFT_V1 || canonical draft bytes)
```

Ranges count é 0..1048576, sorted/unique por `(file_identity,start)`, half-open/non-overlapping por
file e comprimento fixed 48; body length é 1..67108864 e decodifica no exact target codec. Pin
snapshot length é 100..16777216, decodifica integralmente um `ProcessRegistryStateV1` current da
mesma DB/runtime/coordinator incarnation, e
`wal_scan_pin_snapshot_sha256 = SHA256(DOMAIN_WAL_SCAN_PIN_SNAPSHOT_V1 || canonical registry state bytes)`
é sempre nonzero, inclusive count zero. `record_length == 380 + 48*exact_ranges_count +
operation_body_length + wal_scan_pin_snapshot_length <= 268435456`, com produto/soma checked antes
de allocation. Record length inclui CRC/EOF. Fingerprint cobre o record integral incluindo CRC (não é campo embedded), logo
decoder primeiro valida/re-encoda/CRC e só então calcula o domain SHA.

`WalMutationOperationKindV1` é u8 fechado: `INVALID=0`, `APPEND=1`,
`SEGMENT_CREATE_OR_ROLL=2`, `TRUNCATE_SUFFIX=3`, `RECYCLE_PREFIX=4`,
`QUARANTINE_REPAIR=5`, `RENAME=6`, `DELETE=7`; 8..255 recusam.
`WalMutationTargetKindV1` discrimina o codec do body, não é FileKind: `INVALID=0`,
`APPEND_RANGE_SET=1`, `SEGMENT_CREATE_OR_ROLL_IMAGE=2`, `SUFFIX_CUT_SET=3`,
`PREFIX_SEGMENT_SET=4`, `QUARANTINE_REPAIR_BUNDLE=5`,
`WAL_TEMP_TO_SEGMENT_RENAME=6`, `WAL_TEMP_TO_LEDGER_RENAME=7`,
`WAL_TEMP_TO_QUARANTINE_RENAME=8`, `WAL_TEMP_DELETE=9`,
`RESERVED_RETIRED_LEDGER_DELETE=10`, `RESERVED_RETIRED_QUARANTINE_DELETE=11`; 10..255 recusam.
As únicas combinações são `1→1`, `2→2`, `3→3`, `4→4`, `5→5`, `6→{6,7,8}` e
`7→9`. Não existe target genérico `NAMESPACE_ENTRY`, `FILE`, `PATH`, `RENAME` ou `DELETE`.
Cross-kind/target, body de outro codec e unknown recusam antes de fingerprint/permit. O target kind
fica no fingerprint e na `RecoveryApplyAuthority`; não é hint runtime ignorável.
`successor=checked_increment_u64(predecessor)` é obrigatório.

A rejeição cross-kind acima vale para **drafts top-level**. Os dois compounds Round 7 têm uma segunda
dispatch table fechada para não transformar um permit kind 3/5 em rename/create genérico.
`_CompoundWalChildPermit` é module-private, single-use, não serializado e só pode ser derivado dentro
do executor do parent depois de validar composite/progress plan, phase e ordinal current. Sua authority
tag interna é `LIVE_PARENT_DRAFT=1` ou `HELD_OPEN_PHASE0_CONTINUATION=2`: tag 1 exige parent permit/
fingerprint normal; tag 2 só vale para parent kind5/phase0, exige o pending slot e
`_TailPhase0ContinuationPermit` da seção 10.3, e proíbe parent draft/fingerprint. Sua preimage é
`(parent_authority_tag,parent_draft_fingerprint_or_ZERO,pending_slot_raw_sha256_or_ZERO,
original_phase0_event_sha256_or_ZERO,parent_operation_kind,parent_target_kind,composite_or_progress_plan_sha,
phase_ordinal,child_ordinal,child_kind,source/target capability+path+identity+raw hashes,
barrier_flags,current_generation)`; qualquer field divergente recusa. A tabela total é:

`CompoundWalChildKindV1` é `u8`: `INVALID=0`, `CREATE_QUARANTINE_TEMP=1`,
`FILL_QUARANTINE_TEMP=2`, `PUBLISH_QUARANTINE_TARGET8=3`, `CREATE_LEDGER_TEMP=4`,
`FILL_LEDGER_TEMP=5`, `PUBLISH_LEDGER_TARGET7=6`, `LEDGER_BARRIERS_CERT=7`,
`RECYCLE_WHOLE_SEGMENT=8`, `CREATE_EMPTY_RESERVATION=9`, `CREATE_ROLL_TEMP=10`,
`FILL_ROLL_TEMP=11`, `PUBLISH_SEGMENT_TARGET6=12`, `RELEASE_CUT_SEGMENT=13`,
`CUT_BARRIERS_CERT=14`; 15..255 recusam. Parent kind5 permite somente 1..7 na ordem do COW/tree
plan; parent kind3 permite somente 8..14 na ordem/densidade do progress plan. Não há child DELETE,
APPEND, control ou arbitrary rename.

| Parent draft | Child kinds exatos, em ordem do plan | Portas que aceitam esse child |
|---|---|---|
| `QUARANTINE_REPAIR→QUARANTINE_REPAIR_BUNDLE` | quarantine temp create/fill → target-8 publish → ledger temp create/fill → target-7 publish → data/parent/direct cert | somente primitives exactas das duas temp classes e `_wal_publish_quarantine_from_temp`/`_wal_publish_ledger_from_temp` |
| `TRUNCATE_SUFFIX→SUFFIX_CUT_SET` | recycle whole segment → empty reservation ou roll-temp create/fill → target-6 publish → cut-segment release → barriers/direct cert | somente `_execute_suffix_cut_progress` e `_wal_publish_segment_from_temp` no ordinal correspondente |

O low-level entrypoint aceita ou seu `_WalMutationPermit` top-level do **mesmo** kind normal ou o
`_CompoundWalChildPermit` cujo child kind/target exatos estão na linha; nunca aceita o parent permit
diretamente. Child não cria draft, não publica generation própria e não pode escapar do executor: a
única edge da phase cobre o compound inteiro, enquanto `WalMonotonicProgressPlan`/ledger COW progress
persistem cada fronteira de crash. Para tag 2, essa edge é a HELD_OPEN já publicada, não uma edge nova.
Derivar target 6/7/8 fora do ordinal, reutilizar child, chamar
DELETE/APPEND, trocar capability/path ou passar parent kind 3/5 à primitive é type/refusal antes do
syscall. Assim as duas edges continuam contabilizadas sem bypass da matriz exact-type.

Ranges são non-overlapping/sorted/half-open e source inventory/preimage autentica file identity,
names, sizes e todos os bytes que serão preservados/trocados/removidos. Target é ou bytes inline
length-checked, ou locator create-exclusive de uma COW image imutável cuja length/raw SHA aparece no
body; callback/path mutável sem preimage é proibido. Pin snapshot enumera registrations/ranges current;
boundary proof é terminal/baseline canônico para truncate/recycle; ledger/quarantine e barrier plan
cobrem exact names/bytes/order/data+directory barriers. Campos não aplicáveis têm ZERO encoding
específico ao kind, nunca são ignorados. Acquire faz probe nonblocking e exige o registry state live
bit-identical aos bytes embedded imediatamente antes do generation CAS; drift replana. O field
homônimo de `PrefixSegmentSetPayloadV1` precisa ser bit-identical ao outer draft SHA e usa essa mesma
preimage, nunca raw SHA, `ReaderRegistrySnapshot` ou registrations-only digest. Omitting bytes,
ZERO32, domain alternativo ou snapshot antigo recusa antes do permit/WAL.

APPEND especializa o body com `(WalAppendDraft fingerprint, blob_length, blob_sha256, segment/roll
postimage, exact write ranges)`; seu blob continua fora do permit e é escrito exatamente uma vez.
TRUNCATE/RECYCLE carregam cut boundary e retained/removed inventories completos. QUARANTINE_REPAIR
carrega classifier, old→new/quarantine/ledger images e names. RENAME/DELETE nunca recebem um permit de
outro kind, mesmo se ranges coincidirem.

`WalNamespaceClassV1` também é u8 fechado: `INVALID=0`, `WAL_SEGMENT=1`, `WAL_LEDGER=2`,
`WAL_QUARANTINE=3`, `WAL_TEMP_SEGMENT=4`, `WAL_TEMP_LEDGER=5`, `WAL_TEMP_QUARANTINE=6`,
`WAL_RETIRED_LEDGER=7`, `WAL_RETIRED_QUARANTINE=8`; 9..255 recusam. Cada class é resolvida contra uma
directory capability distinta, direct-certified no carrier manifest por database UUID, volume/
filesystem identity, root file identity e access policy; nenhum root é reconstruído por string.
Dentro do root, o `CanonicalRelativePathV1` da seção 5.2 é adicionalmente restrito pela tabela:

| class | depth e componentes canônicos | semântica |
|---|---|---|
| WAL_SEGMENT | 1; `[0-9]{12}.wal`, decimal zero-padded que re-encoda o segment number 1..999999999999 | único segmento ativo |
| WAL_LEDGER | 1; exatamente `ledger.log` | ledger ativo único |
| WAL_QUARANTINE | 1; `q-` + 64 lowercase hex | directory tree imutável cujo tree SHA está no body |
| WAL_TEMP_SEGMENT | 1; `s-` + 64 lowercase hex + `.tmp` | COW temp destinado somente a WAL_SEGMENT |
| WAL_TEMP_LEDGER | 1; `l-` + 64 lowercase hex + `.tmp` | COW temp destinado somente a WAL_LEDGER |
| WAL_TEMP_QUARANTINE | 1; `q-` + 64 lowercase hex + `.tmp` | COW temp destinado somente a WAL_QUARANTINE |
| WAL_RETIRED_LEDGER | 1; `l-` + 64 lowercase hex + `.retired` | predecessor de ledger já substituído, nunca ativo |
| WAL_RETIRED_QUARANTINE | 1; `q-` + 64 lowercase hex + `.retired` | entry cuja retention/release proof autorizou descarte |

O ponto em `.wal`/`.tmp`/`.retired` é literal; regex ASCII, case e zero-padding são canônicos.
Segment 0, overflow decimal, path absoluto, separador, `.`/`..`, ADS, drive/UNC, alternate spelling,
extra component e trailing byte recusam. A directory capability é aberta no-follow; cada component e
parent identity é revalidado imediatamente antes de create/rename/delete e depois da namespace
barrier. Symlink, junction, reparse point, mount swap ou hardlink/link-count fora da policy da seção
5.2 recusa antes de criar temp. Root/class capability diversa nunca é “equivalente” por path textual.

A matriz de namespace é total:

| target kind | source class | target class | única porta low-level |
|---|---|---|---|
| 6 WAL_TEMP_TO_SEGMENT_RENAME | WAL_TEMP_SEGMENT | WAL_SEGMENT | `_wal_publish_segment_from_temp` |
| 7 WAL_TEMP_TO_LEDGER_RENAME | WAL_TEMP_LEDGER | WAL_LEDGER | `_wal_publish_ledger_from_temp` |
| 8 WAL_TEMP_TO_QUARANTINE_RENAME | WAL_TEMP_QUARANTINE | WAL_QUARANTINE | `_wal_publish_quarantine_from_temp` |
| 9 WAL_TEMP_DELETE | uma de WAL_TEMP_SEGMENT/LEDGER/QUARANTINE | absent | `_wal_delete_exact_temp` |
| 10 reserved | — | — | sempre recusa na V7 |
| 11 reserved | — | — | sempre recusa na V7 |

Para rename, body/fingerprint autentica ambas capabilities, paths, source file/tree identity,
source bytes/tree SHA, target absence ou exact predecessor, target postimage e data+source-parent+
target-parent barriers; para delete autentica class/path/identity/bytes/tree SHA, release predicate e
parent barrier. Source e target iguais, class pair diferente, active ledger class em DELETE, active
segment em DELETE ou temp class destinada a outro target recusam. Segment retirement continua
exclusivamente `RECYCLE_PREFIX/PREFIX_SEGMENT_SET`, com seu inventário/boundary; não há delete de
segment por esta matriz. `_IrrevocableWalPermit`/`_WalMutationPermit` jamais autoriza bootstrap/control,
heap, catalog, index, carrier ou arbitrary namespace, ainda que o file identity/range/hash coincida.

Nenhum `operation_body` é opaco. O envelope e os subcodecs fechados são:

```text
MAX_WAL_MUTATION_OPERATION_BODY_BYTES_V1 = 67_108_864
WalMutationOperationBodyV1 = (
  magic=MAGIC_WAL_MUTATION_BODY_V1, version u16=1, operation_kind u8, target_kind u8,
  record_length u32, database_uuid[16], logical_operation_identity[32],
  payload_length u32, payload_sha256[32], payload[payload_length], crc32c u32
)
payload_sha256 = SHA256(DOMAIN_WAL_MUTATION_PAYLOAD_V1 || target_kind_u8_exactly_one_byte || payload)

WalMutationExactSetV1 = (
  magic=MAGIC_WAL_MUTATION_EXACT_SET_V1, version u16=1, reserved_zero u16, record_length u32,
  parent_count u32, file_count u32, range_count u32, barrier_count u32,
  parent_capability_set_sha256[32], parents[parent_count],
  files[file_count], ranges[range_count], barriers[barrier_count], crc32c u32
)
parent_capability_projection := (
  entry_length u32, parent_ordinal u32, namespace_class u8, access_policy u8,
  reserved_zero u16, path_length u32, root_binding_sha256[32],
  directory_identity[32], capability_receipt_sha256[32], path_bytes[path_length]
)
ParentCapabilityReceiptSubjectV1 := (
  subject_length u32, parent_ordinal u32, namespace_class u8, access_policy u8,
  reserved_zero u16, path_length u32, root_binding_sha256[32],
  directory_identity[32], path_bytes[path_length]
)
file := (
  entry_length u32, namespace_class u8, image_role u8, presence u8, reserved_zero u8,
  path_length u32, parent_ordinal u32, segment_number u64, file_identity[32], raw_length u64,
  raw_sha256[32], tree_manifest_length u32, tree_manifest_sha256[32],
  path_bytes[path_length], tree_manifest_bytes[tree_manifest_length]
)
range := (
  file_ordinal u32, reserved_zero u32, start u64, end u64,
  source_blob_offset u64, raw_length u64, raw_sha256[32]
)
barrier := (
  ordinal u32, barrier_kind u8, reserved_zero[3], file_ordinal u32,
  parent_ordinal u32, expected_direct_proof_sha256[32]
)
```

`WalFileImageRoleV1` é `INVALID=0`, `SOURCE=1`, `TARGET=2`, `TEMP=3`, `LEDGER=4`,
`QUARANTINE=5`, `RETAINED=6`, `REMOVED=7`; 8..255 recusam. `WalFilePresenceV1` é
`INVALID=0`, `ABSENT=1`, `CURRENT=2`, `PLANNED_POSTIMAGE=3`; 4..255 recusam. ABSENT exige file
identity/length/SHA/tree zeros. CURRENT exige identity e raw SHA nonzero; PLANNED_POSTIMAGE exige
future identity ZERO e raw SHA nonzero. Em ambos, `raw_length` pode ser zero e então raw SHA precisa
ser exatamente `SHA256(empty bytes)`, distinguindo file vazio presente de ABSENT; length zero com
ZERO32 ou SHA não-empty recusa. Tree length/SHA são nonzero somente para role QUARANTINE directory e
ZERO nos regular files, inclusive empty reservation/roll temp. PLANNED regular nonempty exige
length>0/SHA exato, mas nunca file identity inventada. `WalBarrierKindV1` é `INVALID=0`,
`FILE_DATA=1`, `SOURCE_PARENT_NAMESPACE=2`, `TARGET_PARENT_NAMESPACE=3`, `COLD_DIRECT_READ=4`;
5..255 recusam. `WalParentAccessPolicyV1` é `INVALID=0`, `READ_ONLY_SOURCE=1`,
`MUTABLE_SOURCE=2`, `CREATE_EXCLUSIVE_TARGET=3`; 4..255 recusam. Parents são ordinals densos,
sorted/unique por `(namespace_class,path bytes,directory_identity)`, com root/directory/capability SHA
nonzero e path da class;
`parent_capability_projection.entry_length == 112 + path_length` e
`ParentCapabilityReceiptSubjectV1.subject_length == 80 + path_length`, ambos com EOF no fim do path;
o subject é a projection byte a byte dos mesmos fields, na mesma ordem, **omitindo somente**
`capability_receipt_sha256`. Define-se
`parent_capability_subject_sha256 = SHA256(DOMAIN_WAL_PARENT_SET_V1 || LE32(subject_length) ||
canonical ParentCapabilityReceiptSubjectV1 bytes)` e
`parent_capability_set_sha256=SHA256(DOMAIN_WAL_PARENT_SET_V1||canonical parent entries)`.

Cada `capability_receipt_sha256` é o digest de um `ExactEvidenceReceiptV1(kind=9)` com exatamente uma
entry: header DB/runtime current, `subject_identity_sha256=parent_capability_subject_sha256`,
`source_generation==target_generation==coordination_namespace_generation`, barrier flags zero;
entry ordinal zero, object DIRECTORY, CURRENT→CURRENT, DIRECT_CERTIFIED, locator igual ao path do
subject, lengths zero, capability binding igual a `root_binding_sha256`, predecessor/successor file
identity iguais a `directory_identity`, raw SHAs ZERO e
`semantic_sha256=parent_capability_subject_sha256`. O verifier primeiro decodifica o subject embedded,
faz o direct read da capability e só então reconstitui o receipt; o receipt ou seu SHA jamais entra no
subject que hasheia. Cada `expected_direct_proof_sha256` é kind 3 sobre o file/parent post-state indicado
pela barrier. ZERO, receipt de outra parent/file/generation, field self-referential antigo,
self-reference/zeragem ad hoc ou hash do struct host recusa; há uma
única preimage canônica e ela vem dos locators/fields do exact set e direct reads, não de store opaco.
Cada file e barrier referencia parent existente; source/target roles exigem policy compatível e rename
cross-parent contém os dois parents distintos. Files são sorted/unique por `(namespace_class,path bytes,image_role)`, ranges por
`(file_ordinal,start)`, barriers por ordinal dense. Counts máximos são 65536 files, 1048576 ranges e
65536 barriers e 65536 parents; every ordinal/index/range checked. Path/class segue a allowlist acima, tree manifest é
nonempty somente para QUARANTINE directory image, e exact-set SHA é
`SHA256(DOMAIN_WAL_MUTATION_EXACT_SET_V1||canonical bytes)`.

```text
AppendRangeSetPayloadV1 = (
  header(magic=MAGIC_WAL_APPEND_PAYLOAD_V1, operation=1, target=1),
  wal_append_draft_fingerprint[32], blob_length u64, blob_sha256[32],
  pre_tail_lsn u64, post_terminal_lsn u64,
  pre_tail_raw_sha256[32], post_tail_raw_sha256[32],
  record_count u32, exact_set_length u32,
  records[record_count], exact_set_bytes[exact_set_length], crc32c u32
)
record := (
  ordinal u32, record_type u8, reserved_zero u8, flags u16, lsn u64,
  blob_offset u64, encoded_length u32, reserved_zero u32, raw_sha256[32]
)

SegmentCreateOrRollImagePayloadV1 = (
  header(magic=MAGIC_WAL_SEGMENT_PAYLOAD_V1, operation=2, target=2),
  segment_number u64, predecessor_segment_number u64,
  segment_header_length u32, exact_set_length u32,
  segment_header_sha256[32], segment_header_bytes[segment_header_length],
  exact_set_bytes[exact_set_length], crc32c u32
)

SuffixCutSetPayloadV1 = (
  header(magic=MAGIC_WAL_SUFFIX_PAYLOAD_V1, operation=3, target=3),
  tail_disposition_length u32, composite_plan_length u32, progress_proof_length u32,
  exact_set_length u32, next_step_ordinal u32, reserved_zero u32,
  tail_disposition_sha256[32], composite_plan_sha256[32],
  wal_monotonic_progress_plan_sha256[32], progress_proof_sha256[32],
  preservation_event_sha256[32], target_retained_tail_sha256[32],
  tail_disposition_bytes[tail_disposition_length],
  composite_plan_bytes[composite_plan_length], progress_proof_bytes[progress_proof_length],
  exact_set_bytes[exact_set_length], crc32c u32
)

PrefixSegmentSetPayloadV1 = (
  header(magic=MAGIC_WAL_PREFIX_PAYLOAD_V1, operation=4, target=4),
  boundary_proof_codec_kind u8, reserved_zero[3], recycle_boundary_lsn u64,
  boundary_proof_length u32, progress_plan_length u32,
  progress_proof_length u32, exact_set_length u32,
  boundary_proof_sha256[32], wal_scan_pin_snapshot_sha256[32],
  retained_inventory_sha256[32], removed_inventory_sha256[32],
  progress_plan_sha256[32], progress_proof_sha256[32],
  boundary_proof_bytes[boundary_proof_length], progress_plan_bytes[progress_plan_length],
  progress_proof_bytes[progress_proof_length], exact_set_bytes[exact_set_length], crc32c u32
)

QuarantineRepairBundlePayloadV1 = (
  header(magic=MAGIC_WAL_QUARANTINE_PAYLOAD_V1, operation=5, target=5),
  tail_disposition_length u32, preservation_event_length u32,
  quarantine_tree_manifest_length u32, ledger_append_plan_length u32,
  exact_set_length u32, reserved_zero u32,
  tail_disposition_sha256[32], preservation_event_sha256[32],
  quarantine_tree_manifest_sha256[32], ledger_append_plan_sha256[32],
  tail_disposition_bytes[tail_disposition_length],
  preservation_event_bytes[preservation_event_length],
  quarantine_tree_manifest_bytes[quarantine_tree_manifest_length],
  ledger_append_plan_bytes[ledger_append_plan_length],
  exact_set_bytes[exact_set_length], crc32c u32
)

WalNamespaceRenamePayloadV1 = (
  header(magic=MAGIC_WAL_RENAME_PAYLOAD_V1, operation=6, target=6|7|8),
  source_namespace_class u8, target_namespace_class u8,
  expected_target_present u8, reserved_zero[5],
  source_file_ordinal u32, target_file_ordinal u32, exact_set_length u32,
  expected_target_file_identity[32], expected_target_raw_sha256[32],
  exact_set_bytes[exact_set_length], crc32c u32
)

WalNamespaceDeletePayloadV1 = (
  header(magic=MAGIC_WAL_DELETE_PAYLOAD_V1, operation=7, target=9),
  source_namespace_class u8, release_proof_kind u8, reserved_zero[6],
  source_file_ordinal u32, release_proof_length u32, exact_set_length u32,
  release_proof_sha256[32], release_proof_bytes[release_proof_length],
  exact_set_bytes[exact_set_length], crc32c u32
)
```

Cada `header` acima é exatamente `magic[8], version u16=1, operation_kind u8, target_kind u8,
record_length u32`; payload CRC/length/EOF é independente do envelope CRC. `record` é sorted por
ordinal/LSN e a união de `[blob_offset,blob_offset+encoded_length)` precisa ser exatamente
`[0,blob_length)`, sem gap/overlap. V1 **não possui padding** fora de record; encoder que precisaria
alignment bytes recusa pre-WAL ou os representa por um WAL record type definido em emenda de formato,
nunca por type inventado. Cada raw SHA
recalcula do blob. Segment header tem 1..4096 bytes, `txn_id=0`, segment number igual ao path e grammar
própria. Suffix exige codecs integrais de `TailDispositionV1`/composite/event e exact SHA equality.
`PrefixBoundaryProofCodecKindV1` é `INVALID=0`, `REQUESTED_CHECKPOINT=1`, `DIRECT_BOUNDARY=2`;
3..255 recusam. Kind 1 decodifica exatamente `RequestedCheckpointBoundaryProofV1` e só vale na rota
CHECKPOINT/RECYCLE imediatamente derivada daquele checkpoint; kind 2 decodifica exatamente
`BoundaryProofV1(proof_kind=RECYCLE_PREFIX)` e só vale no cold resume da **mesma** publication de
checkpoint já direct-certified, nunca como recycle avulso de recovery/maintenance. Em ambos, o
underlying `boundary_lsn` é exatamente
`recycle_boundary_lsn`, history/checkpoint/DB casam ao exact set e pin, e o field SHA usa o domain do
codec selecionado. Body-swap, direct proof com outro proof kind, wrapper de outro request ou u64 nu
recusa.

RECYCLE_PREFIX não é um unlink-loop agregado sem memória. Antes do primeiro unlink, o
`CheckpointIndexManifestV1` autoritativo já contém `wal_prefix_recycle_plan_bytes/SHA`, exatamente um
`WalMonotonicProgressPlanV1(workflow=PREFIX_RECYCLE)` cuja source/target inventory e N step-1 entries
casam bijetivamente ao boundary proof, removed/retained inventory e exact set deste payload. Zero
segment removível exige plan length 0/SHA ZERO e proíbe draft; nonzero exige plan nonempty/nonzero.
`PendingPrefixRecycleGateV1` impede publicar/iniciar outro checkpoint enquanto o latest manifest
contiver plan nonempty que ainda não seja target-exact + barriers/direct cert. Assim os bytes de
source preimage não somem com RAM nem são sobrescritos antes de adoption.

O primeiro attempt usa uma `WalMutationDraftV1(RECYCLE_PREFIX)` e uma stable generation edge. Depois
que seu CAS fica visível, qualquer resume usa proof fresh e uma role-2 auxiliary edge/current→current+1,
sem alterar o
manifest estável. `_execute_prefix_recycle_progress` é o único compound entrypoint, recebe o exact
permit e executa step 1 em ordinal crescente. Após cada unlink/recycle, repete source-parent barrier e
direct cert antes do próximo; retorno Windows deferred com path ainda current-exact é source sem
progresso, enquanto retorno false mas path absent-exact é target barrier-uncertain. Kill após qualquer
unlink/deferred/barrier é classificado pelo generic proof: exatamente `[0,k)` absent e `[k,N)` current
retoma em k; missing fora desse prefixo, reaparição, file identity/hash diferente ou gap recusa. Ao
chegar N, barriers+cold cert são repetidos, o old manifest pode ser substituído somente por checkpoint
posterior e nenhum SYSTEM/tail floor é confundido com este gate.
Quarantine bundle exige os três codecs abaixo. Rename classes casam a matriz 6–8 e target-present
zero exige old identity/SHA ZERO; delete casa somente target 9 e `WalNamespaceReleaseProofKindV1` é
`INVALID=0`, `FAILED_TEMP=1`; 2..255 recusam. O proof body canônico autentica criação incompleta do
temp, identidade/raw bytes/path/capability e ausência de qualquer target adotável: ele é exatamente
um `ExactEvidenceReceiptV1(evidence_kind=FAILED_TEMP_RELEASE)` integral, length 1..67108864, e
`release_proof_sha256` usa exclusivamente `DOMAIN_EXACT_EVIDENCE_RECEIPT_V1`. Nenhum proof de
checkpoint, LSN, TTL, owner death ou action 14 pode ser reinterpretado como esse kind. Body/payload/
embedded lengths são somados checked e precisam caber simultaneamente no max acima, record/batch/u32/
segment limits. Magic/tag/body swap, unknown/trailing, exact-set field divergente do outer draft,
inline path fora de class ou payload hash stale recusa antes do generation CAS/temp/file mutation.

```text
WalImmutableTreeManifestV1 = (
  magic=MAGIC_WAL_IMMUTABLE_TREE_V1, version u16=1, reserved_zero u16, record_length u32,
  tree_identity[32], discarded_range_projection_sha256[32], source_range_count u32,
  entry_count u32, source_ranges[source_range_count], entries[entry_count], crc32c u32
)
tree_source_range := (
  entry_length u32, range_ordinal u32, namespace_class u8, reserved_zero[3],
  path_length u32, file_identity[32], start u64, end u64,
  raw_sha256[32], path_bytes[path_length]
)
tree_entry := (
  entry_length u32, path_length u32, node_kind u8, source_kind u8,
  reserved_zero u16, mode u32, raw_length u64, raw_sha256[32],
  first_source_range u32, source_range_count u32, inline_length u32,
  path_bytes[path_length], inline_bytes[inline_length]
)

ForensicLedgerAppendPlanV1 = (
  magic=MAGIC_FORENSIC_LEDGER_APPEND_V1, version u16=1, publication_kind u8, reserved_zero u8,
  record_length u32, ledger_path_length u32, ledger_temp_path_length u32,
  event_record_length u32, reserved_zero u32,
  ledger_predecessor_file_identity[32], ledger_predecessor_length u64,
  ledger_predecessor_raw_sha256[32], append_offset u64,
  event_record_sha256[32], ledger_successor_length u64,
  ledger_successor_raw_sha256[32], ledger_path_bytes[ledger_path_length],
  ledger_temp_path_bytes[ledger_temp_path_length],
  event_record_bytes[event_record_length], crc32c u32
)
forensic_ledger_append_plan_sha256 =
  SHA256(DOMAIN_FORENSIC_LEDGER_APPEND_V1 || canonical plan bytes)

ForensicLedgerCowProgressProofV1 = (
  magic=MAGIC_FORENSIC_LEDGER_COW_PROGRESS_V1, version u16=1, observed_state u8,
  reserved_zero u8, record_length u32, ledger_append_plan_sha256[32], event_sha256[32],
  publication_identity[32], predecessor_file_identity[32], predecessor_length u64,
  predecessor_raw_sha256[32], temp_file_identity[32], temp_current_length u64,
  temp_prefix_raw_sha256[32], target_file_identity[32], target_length u64,
  target_raw_sha256[32], data_barrier_completed u8, parent_barrier_completed u8,
  direct_certificate_completed u8, reserved_zero[5], crc32c u32
) # exatamente 340 bytes
forensic_ledger_cow_progress_proof_sha256 =
  SHA256(DOMAIN_FORENSIC_LEDGER_COW_PROGRESS_V1 || canonical proof bytes)
```

Tree node kind é `FILE=1`, `DIRECTORY=2`; source kind é `INLINE=1`, `SOURCE_RANGES=2`; zero e outros
recusam. INLINE exige range fields zero e inline length==raw length; SOURCE_RANGES exige inline zero,
nonempty contiguous range slice dentro do vector embedded e recomputa raw bytes/SHA. Source ranges são
ordinals densos, sorted por `(namespace_class,path,start)`, non-overlapping, path/class/file identity
canônicos. O vector embedded é transformado bijetivamente no codec
`DiscardedRangeProjectionV1`, descartando somente path/class já rederiváveis do segment number; o
campo `discarded_range_projection_sha256` é o digest desse **record integral** sob seu único domain
registrado na seção 0.1.1. Quando o manifest pertence a `TailPreservationEventV1`, esse SHA precisa ser
bit-identical ao `discarded_range_projection_sha256` do seed/event e ao range vector projetado de
TailDisposition/WalMutationExactSet;
reorder, outro plan/range container ou slice fora dele recusa. Entries são sorted/unique paths confinados,
parents precedem children, directory raw/inline/source fields zero e file mode allowlisted.

`LedgerPublicationKindV1` conserva o tag legado `APPEND_EXISTING=1`, mas ele é **reservado/recusado
em toda V7**; `COW_REPLACE=2` é a única publication legal, para ledger healthy ou damaged. Path ativo
é WAL_LEDGER, temp é WAL_TEMP_LEDGER derivado de `publication_identity`; ambos são nonempty/canônicos,
distintos e no mesmo atomic-replace domain. Predecessor/append offset/event e successor length/SHA
são recomputados integralmente: `append_offset==predecessor_length` e successor bytes são exatamente
`predecessor bytes || event_record_bytes`. O source ativo fica intocado enquanto o temp é produzido.
`ledger_path_length`/temp path são 1..65535, event é 1..268435456 e
`record_length==188+ledger_path_length+ledger_temp_path_length+event_record_length<=268435456`, com
sum checked antes de create/allocation. Event decodifica `TailPreservationEventV1`; plan SHA usa
`DOMAIN_FORENSIC_LEDGER_APPEND_V1` e tree SHA `DOMAIN_WAL_IMMUTABLE_TREE_V1`.

`ForensicLedgerCowObservedStateV1` é `INVALID=0`, `SOURCE_EXACT=1`,
`TEMP_CANONICAL_PREFIX=2`, `TEMP_COMPLETE_BARRIER_PENDING=3`,
`TARGET_EXACT_BARRIER_PENDING=4`, `TARGET_DIRECT_CERTIFIED=5`; 6..255 recusam. O classifier fresh é
total e ordenado: (1) active ledger predecessor exact + temp absent; (2) predecessor exact + temp
exists com `0<=k<successor_length` e SHA raw igual ao prefixo canônico do successor — k=0 distingue-se
de absent pela file identity; (3) predecessor exact + temp completo; (4) active ledger já é successor
exact e temp absent; (5) somente o attempt que repetiu data+parent barriers e direct cert sobre (4).
Qualquer partial active ledger, prefixo temp divergente, predecessor/target terceiro, ambos temp+target,
identity/path swap ou outra combinação é INCONCLUSIVE/CORRUPT.

O CAS inicial da phase 0 publica no **mesmo** `WalCoordinationSlotV1` a edge estável `g→g+1` e
`HELD_OPEN/g`; esse slot é a authority durável da janela pós-CAS/pré-event. Após crash, recovery não
cria novo event nem replana a generation. Sob fresh lease+COMMIT, reconstrói de forma única
`BaseTailMutationIntentV1`, composite, disposition e os bytes originais do event a partir do WAL source
ainda intacto, do ledger predecessor (ou do prefixo/event current que o repete), e do par persistido
`g/g+1`; revalida que o composite é o único pending e que current WAL generation continua `g+1`.
Produz então um `ForensicLedgerCowProgressProofV1` fresh sobre exatamente um dos states 1..4 e um
`_TailPhase0ContinuationPermit` module-private, single-use, ligado aos raw 204 bytes do current slot,
`g/g+1`, composite/base/event/append-plan SHAs, source/quarantine/temp/target identities e bytes,
progress proof, fresh lease/COMMIT nonce e budget/attempt. Esse permit não tem codec persistido nem
digest autônomo: sua preimage é a tuple frozen desses records canônicos e direct reads.

A continuation não publica outra WAL generation, não aceita um novo `WalMutationDraftV1` e não troca
os bytes do event; ela completa a mutação já autorizada pela edge HELD_OPEN. Pode derivar somente os
child kinds phase-0 1..7 ainda não target-exact. Qualquer generation diferente, pending NONE, plan/
event/temp de outro attempt, active ledger terceiro ou outro WAL mutation desde o CAS recusa antes de
syscall. Se o event já estiver TARGET_DIRECT_CERTIFIED, nenhuma continuation mutante é emitida: a
phase 1 valida o mesmo event e, em seu CAS `g+1→g+2`, limpa HELD_OPEN atomicamente. Novo crash durante
continuation preserva o mesmo slot/pair e os mesmos bytes canônicos, de modo que a próxima tentativa
repete esta derivação sem depender do permit/draft morto.

O executor COW, sob o child permit exato da phase 0 live ou derivado da continuation acima, cria/adota
o temp, escreve somente o sufixo
faltante do successor canônico, faz data barrier, atomic replace temp→ledger, parent barrier e direct
cert. Crash após qualquer byte deixa state 2 retomável; após temp completo, replace ou cada barrier
deixa 3/4 e repete apenas barriers/syscall ainda não target-exact. Nunca reexecuta append in-place,
trunca active ledger nem escolhe novo temp/event/publication identity. Backend sem create-exclusive,
prefix read/hash e atomic same-volume no-follow replace recusa **antes da phase-0 CAS**. O progress
proof fixed 340 bytes repete plan/event/publication/source/temp/target e flags canônicos; seu SHA é o
único receipt aceito pelo adoption proof. Golden kills cobrem k=0, cada byte, temp completo,
data-barrier, replace, parent-barrier e direct-cert; mutante kind1/in-place ou prefixo não canônico
falha antes de tocar o active ledger.

```text
TailPreservationEventV1 = (
  magic=MAGIC_TAIL_PRESERVATION_EVENT_V1, version u16=1, publication_kind u8, adoption_policy u8,
  record_length u32, database_uuid[16], event_identity[32], composite_identity[32],
  base_tail_mutation_intent_sha256[32],
  predecessor_wal_mutation_generation u64, successor_wal_mutation_generation u64,
  retention_boundary_lsn u64, retention_release_lsn u64,
  tail_disposition_length u32, composite_plan_length u32,
  quarantine_manifest_length u32, quarantine_path_length u32, ledger_path_length u32,
  barrier_flags u8, reserved_zero[3],
  source_tail_raw_sha256[32], target_retained_tail_sha256[32],
  discarded_range_projection_sha256[32], quarantine_tree_identity[32], quarantine_tree_sha256[32],
  ledger_predecessor_file_identity[32], ledger_predecessor_length u64,
  ledger_predecessor_raw_sha256[32], ledger_append_offset u64,
  planned_ledger_successor_length u64, publication_identity[32],
  tail_disposition_bytes[tail_disposition_length],
  composite_plan_bytes[composite_plan_length],
  quarantine_manifest_bytes[quarantine_manifest_length],
  quarantine_path_bytes[quarantine_path_length], ledger_path_bytes[ledger_path_length],
  crc32c u32
)
tail_preservation_event_sha256 = SHA256(DOMAIN_TAIL_PRESERVATION_EVENT_V1 || canonical event bytes)

TailPreservationEventAdoptionProofV1 = (
  magic=MAGIC_TAIL_EVENT_ADOPTION_PROOF_V1, version u16=1, reserved_zero u16, record_length u32,
  event_sha256[32], ledger_file_identity[32], ledger_raw_sha256[32],
  quarantine_tree_identity[32], quarantine_tree_sha256[32],
  ledger_cow_progress_proof_length u32, reserved_zero u32,
  ledger_cow_progress_proof_sha256[32],
  ledger_cow_progress_proof_bytes[ledger_cow_progress_proof_length], crc32c u32
)
```

Publication kind aceita somente 2 COW_REPLACE; tag 1 APPEND_EXISTING recusa. `TailEventAdoptionPolicyV1` é
`INVALID=0`, `SOURCE_OR_TARGET_EXACT=1`; 2..255 recusam. Barrier flags permitem somente ledger DATA,
quarantine DATA, source parent, target parent e cold direct bits definidos pela tabela desta seção.
Generation successor é checked predecessor+1 e casa **sempre** a edge estável phase-0 `g→g+1`
persistida como HELD_OPEN; jamais é substituído pelo current lease, attempt ou por uma edge de
continuation. Paths são exatamente classes
WAL_QUARANTINE/WAL_LEDGER. Disposition/composite/tree decodificam integralmente e seus identities,
ranges/tails/retention casam. `quarantine_tree_identity` é exatamente o `quarantine_identity`
derivado do `TailMutationIdentitySeedV1`; `WalImmutableTreeManifestV1.tree_identity`, event,
quarantine component e adoption proof repetem esses mesmos 32 bytes. ZERO, identidade independente,
hash do path/tree usado como identity ou qualquer divergência recusa antes de create/append/adoption.
`event_identity` segue `DOMAIN_TAIL_EVENT_ID_V1` no DAG e
`publication_identity=SHA256(DOMAIN_TAIL_PUBLICATION_ID_V1||event_identity||ledger_predecessor_file_identity||
ledger_predecessor_length||ledger_predecessor_raw_sha256)`; ambos são determinísticos, nonzero e não
contêm PID/attempt/generation/CSPRNG. Quarantine/temp component deriva de `quarantine_identity`, então
kill após phase-0 CAS/create/rename/data barrier e antes do event reencontra exatamente o mesmo target
e o adota somente se tree manifest/bytes/parent direct proofs casam. O pending slot conserva o par
original necessário para reconstruir os mesmos event/temp bytes; trocar `g/g+1`, ainda que o path seja
igual, ou encontrar outro target é inconclusive. O evento não contém draft fingerprint, operation-body SHA, seu próprio
SHA nem whole-ledger successor SHA: estes seriam ciclos. O COW plan exterior incorpora os event
bytes e calcula o successor SHA. Adoption proof é ephemeral/module-private, recomputado do event +
target direto após todos os barriers; exige progress proof length exatamente 340, nested codec/SHA/
plan/event/publication/target iguais e observed state TARGET_DIRECT_CERTIFIED. Qualquer outro state,
SHA de barrier solto ou receipt genérico recusa. Não é persistido dentro do evento e não autoriza truncate.
`retention_boundary_lsn` é exatamente `TailDispositionV1.truncate_after_lsn`, cuja locator/terminal
boundary está no `RecoveryApplyPlanV1` e no `BoundaryProofV1` direct-certified do mesmo scan; não é um
horizon livre escolhido pelo caller. `retention_release_lsn` é **sempre zero canônico** na V7 e
significa `NOT_RELEASED`, jamais baseline ou release no LSN zero. Event, ledger, tree e seus source
ranges são retidos indefinidamente nesta versão. Não há SYSTEM floor/action 14 implícito para tail,
nem DELETE de WAL_RETIRED_LEDGER/WAL_RETIRED_QUARANTINE: targets 10/11 e release-proof kinds 2/3 são
reserved/refused. Owner death, TTL, checkpoint, close, repair ou upgrade não removem esses bytes. Uma
versão futura só poderá coletá-los com novo codec/fence durável que crie e libere um floor concreto;
um build V7 permanece fail-closed ao encontrá-lo.

Todo append, truncate, recycle, quarantine-repair ou mudança de bytes/nome/inventário do WAL toma
COMMIT e, **antes do primeiro byte ou remoção**, avança exatamente uma vez a generation no slot
inativo e certifica sua visibilidade. Acquire revalida integralmente o `WalMutationDraftV1`, pin/
boundary/source preimage e `current==g`, faz CAS `g→g+1` e só então emite `_WalMutationPermit` ligado
ao **wal-mutation draft fingerprint**, kind, ranges e g/g+1. Toda porta low-level exige
`current==g+1` e match integral; append exige adicionalmente o embedded `WalAppendDraft` exact.
Falha antes dessa certificação produz zero mutação WAL; falha
incerta envenena o runtime. Incremento extra sem mutação é seguro; mutação sem incremento é proibida.
No máximo u64, qualquer mutação recusa typed e torna a source terminal conforme a regra global; nem
maintenance offline troca runtime/carrier in-place. Nunca há wrap/reset enquanto seed/pin possa viver. Isso faz truncate+append dos mesmos
bytes ainda divergir do seed e fecha ABA interprocesso.

“Exatamente uma vez” inclui a phase 0 tail que fica HELD_OPEN: o CAS inicial é a única advance que
autoriza toda sua execução e cada continuation pós-crash somente completa aquela authority persistida;
ela não soma outro advance nem abre outra mutation. A phase 1 é uma mutação distinta e usa sua própria
edge `g+1→g+2`, que também encerra o pending state.

As portas low-level `append_planned(wal_mutation_draft, append_draft, permit)`, segment create/roll,
`truncate_after`, `recycle`, quarantine repair e somente as seis rotas namespace da tabela recebem seu exact
`WalMutationDraftV1` e exigem `_WalMutationPermit` module-private exact-type, vinculado a manager/PID/
process-birth/coordination generation/COMMIT nonce, kind/ranges/full fingerprint e g/g+1 do novo slot.
O permit é
single-use e revogado na saída de COMMIT; paths internos não possuem overload sem permit. Ele não é
input do draft/fingerprint. Scanner/read-only nunca o recebe. Assim um mutador novo não consegue
esquecer generation, pin conflict ou owner-death check sem falhar antes do primeiro byte.

O `WAL_SCAN_PIN(snapshot_lsn=P, ProtectedWalRangeSetV1(Q,P,...), seed_nonce)` é uma registration
cross-process no mesmo coordinator, vinculada a PID/process birth identity, database generation,
stable WAL root, frozen segment identities/ranges e owner-death do OS. A preimage integral fica inline
na entry canônica do coordinator; mutation probe, snapshot e transition proof a decodificam, e nenhum
processo tenta enumerar ranges a partir de um SHA opaco. Recycle/truncate/repair não pode remover nem
alterar qualquer byte/segment coberto por pin vivo; append fora da faixa ainda incrementa generation
e força retry no segundo COMMIT. Pin vivo nunca expira por
TTL, heartbeat atrasado ou pressão; só release explícito ou prova do backend/OS de owner death o
revoga. O segundo COMMIT converte atomicamente a mesma registration em reader pin, sem janela
desprotegida. Backend incapaz desse contrato recusa compartilhamento; `:memory:` comprovadamente
process-private pode usar carrier local equivalente.

Conflito com `WAL_SCAN_PIN` observado sob COMMIT é sempre um **probe nonblocking** sobre todos os
records ACTIVE integralmente decodificados; SHA/length/codec inválido é coordination poison, não
“sem conflito”. Nenhuma porta faz
condition wait, sleep ou callback segurando COMMIT. Append cujo exact write range começa depois do
frozen tail offset pode prosseguir somente após incrementar/certificar a WAL mutation generation, de
modo que o segundo seal do scanner reinicie. Recycle/checkpoint calcula um prefixo que não intersecta
nenhum pin vivo ou adia o recycle; nunca espera. Truncate/quarantine-repair/recovery que intersecte um
range pinned libera COMMIT, guards e lease e retorna `RESTART_OUTER_ATTEMPT(WAL_PIN_CONFLICT)` ao
mesmo `OperationBudget`; no terminal do budget devolve o erro typed correspondente. Owner death é a
única revogação não explícita. Assim o scanner sempre consegue reentrar COMMIT para converter/liberar
o pin, e nenhum mutador toca o range enquanto ele está fora de COMMIT.

Se não há frontier certification, aplica-se o gap flow abaixo. Se a frontier existe mas a device proof está
ausente/stale, não se publica snapshot: read-only recusa e write-capable executa o device catch-up da
seção 10.5, sempre fora do COMMIT desta tentativa e sob o mesmo budget.

Se não há frontier certification:

- conexão estritamente read-only recusa com
  `GrafxUnsupportedOperation(field="read_only_consistency")` e prova zero bytes/calls mutáveis no
  database `StorageDevice`; registration/release no coordinator é permitida e não altera arquivos,
  tamanhos, mtimes, WAL, control state ou dados da database;
- conexão write-capable sai do COMMIT sem mutação, executa recovery de duas passagens com fresh lease
  e INDEX_VIEW EXCLUSIVE conforme seção 10, e reinicia `begin` sob o mesmo `OperationBudget`;
- corruption/unknown-required nunca vira retry; I/O inconclusive nunca usa última proof/cache.

Mutantes pausam depois de WAL barrier, depois de cada page apply e antes de commit.state. Em todos, um
novo read transaction recusa ou completa recovery antes de devolver snapshot; nunca enxerga P antigo
sobre páginas parcialmente novas.

#### 8.4.2 Horizonte relevante total

No SHA 539, `IndexStore.check_freshness` compara `built_through_lsn` com published global
(`engine/index_manager.py:623-680`), `IndexManager.commit` avança tabelas não escritas
(`:2106-2147`) e recovery usa `mark_built_through` global. Esses caminhos são removidos.

Para cada identidade persistida I:

`relevant_horizon(I,Q,P)` é o máximo de:

- checkpoint Q certificado;
- COMMITs em `(Q,P]` cujo `affected_table_ids` contém a tabela de I;
- COMMITs em `(Q,P]` cujo `IndexMutationScope.index_identity` é I, para qualquer operação 1–12.

A consulta usa somente `CommittedHistory` autenticada pela frontier embedded no
`WalPublicationProof` e o manifest total
da seção 5.2. Control-only, reservation, format e outra tabela/identity não avançam I. RESET, REBUILD,
RECONCILE, MARK/CLEAR_STALE e qualquer index-only avançam exatamente a identity nomeada. CREATE/DROP/
ALTER nomeiam identities old/new conforme existam. Descriptor, nome/path, quantidade de records,
CSN global e ausência de effect nunca são inferência de relevância.

`CommitPayload.page_touches`, WRITE_PAGE/INDEX records, owners, affected tables e index mutations têm
igualdade bijetiva. Se uma row effect omite o INDEX_WRITE esperado, preparation recusa; se um índice
legitimamente não recebe effect, a tabela ainda avança o horizon e o header curto recusa. Recovery
deriva o mesmo mapa, sem `mark_built_through(published)`. Checkpoint consolida a base somente pelo
`CheckpointIndexManifest` completo.

#### 8.4.3 Guard interprocesso shared/exclusive e anti-ABA

O nome estável é:

`INDEX_VIEW/<sha256(database_uuid || persisted_index_identity)>`

O coordinator oferece um RW guard real entre processos:

- SHARED: vários readers, de qualquer processo, podem coexistir;
- EXCLUSIVE: espera todos os SHARED/EXCLUSIVE e exclui qualquer novo reader/mutator;
- identities múltiplas são adquiridas por digest crescente e liberadas em ordem inversa;
- writer preference/fairness e owner-death/revocation são explícitos; timeout usa seção 12.4;
- ausência de backend interprocesso recusa database compartilhável. Somente `:memory:`
  comprovadamente process-private pode usar RW guard local.

SHARED vivo não é evicto por TTL/heartbeat atrasado: writer espera ou termina em timeout. Release por
owner death exige prova do backend/OS de que o owner morreu; process stalled continua protegendo sua
caminhada. Isso segue a mesma política conservadora das reader pins. Tickets são FIFO monotônicos;
quando um EXCLUSIVE está enfileirado, SHARED posteriores esperam, evitando starvation do writer sem
retirar readers já ativos.

Lock e state usam um **carrier estável**. Num backend por arquivo, o path/inode do carrier é criado
uma vez com create-exclusive, nunca sofre atomic-replace, truncate ou unlink enquanto a database
generation pode ter handle vivo, e o RW lock do OS é tomado nesse mesmo inode. O state ocupa dois
slots CRC-valid de tamanho fixo no carrier; sob EXCLUSIVE, o writer grava in-place o slot inativo e o
coordinator executa sua visibility barrier antes de liberar. Readers sob SHARED escolhem a maior
generation válida. Substituir o arquivo que está lockado é proibido: lock num inode antigo e state
num inode novo seriam split-brain. Backend sem arquivo deve oferecer a garantia equivalente de uma
única identity de lock/state por namespace durante toda a lifetime.

O carrier global estável `INDEX_NAMESPACE_CATALOG/<database_uuid>` nasce no bootstrap V7 antes da
exposição e tem o byte contract completo:

```text
INDEX_NAMESPACE_SLOT_BYTES_V1 = 140
INDEX_NAMESPACE_FILE_BYTES_V1 = 280
IndexNamespaceCarrierSlotV1 = (
  magic=MAGIC_INDEX_NAMESPACE_CARRIER_V1, version u16=1, slot_index u8, publication_kind u8,
  slot_length u32=140, database_uuid[16],
  index_namespace_generation u64, predecessor_generation u64,
  reserved_zero[8], carrier_plan_sha256[32],
  target_persisted_index_identity[32], transaction_uuid[16], crc32c u32
) # exatamente 140 bytes
```

`IndexNamespacePublicationKindV1` é u8: `INVALID=0`, `INITIALIZE=1`,
`CREATE_RESERVATION=2`, `DROP_RESERVATION=3`; 4..255 recusam. INITIALIZE exige generation 1,
predecessor 0, target/transaction ZERO e `carrier_plan_sha256` igual à entry do carrier manifest.
CREATE/DROP exigem `generation=checked_increment(predecessor)`, target identity, transaction UUID e
plan SHA nonzero, todos bit-identical ao preflight/transaction V2; o UUID TRANSACTION não muda em
retry. Offsets são slot 0 em 0 e slot 1 em 140, EOF 280; CRC cobre os primeiros 136 bytes e raw SHA
cobre 140. Slot index/offset mismatch, trailing bytes e slot inativo inicial diferente de all-ZERO
recusam.

Cold/live seleciona o único slot CRC-valid de maior generation. Dois slots válidos empatados — mesmo
com demais campos iguais —, predecessor que não é a generation anterior, salto/regressão, kind/zero
mismatch ou plan/target divergente são CORRUPT/INCONCLUSIVE. Toda publication escreve o slot inativo
integral, visibility barrier e direct re-read/decode/hash antes do unlock. A generation começa em 1,
usa `checked_increment_u64` e avança sob seu EXCLUSIVE antes de cada tentativa CREATE/DROP; incremento extra pre-WAL é
invalidação segura, nunca identidade de catálogo. CREATE toma seu EXCLUSIVE, revalida a nova
`PersistedIndexIdentity` já frozen no preflight,
cria/verifica o carrier per-index por `coordinator_create_carrier_durable` antes do append DDL e então toma também o EXCLUSIVE per-index;
carrier órfão por abort pre-WAL fica inexposto e só é coletado offline. DROP nunca remove o carrier;
recriar o mesmo nome usa outra identity/carrier. Namespace generation pre-WAL não contém state bit que
mude pós-WAL; nenhuma publicação posterior reutiliza seu mesmo generation com bytes diferentes.
Reader SHARED apenas abre, locka e lê carrier existente; não cria/trunca/escreve arquivo nem altera o
database `StorageDevice`. Fila/ticket é responsabilidade da primitive de coordenação/OS.

Carrier ausente/corrupt para identity catalogada instala poison/refusal; repair online **não** pode
recriá-lo. Recriação só ocorre em `OfflineMaintenanceSession` que mantém o carrier global, prova zero
participants/readers/handles em todos os processos, fecha pools, incrementa a database runtime
generation persistida no coordinator e faz cold reopen. Assim nenhum broadcast local é usado para
revogar handles estrangeiros. Deleção externa enquanto um handle pode viver exige shutdown/repair
offline; nunca se abre um segundo lock domain.

O state per-index compartilhado possui igualmente um único codec:

```text
INDEX_VIEW_SLOT_BYTES_V1 = 188
INDEX_VIEW_FILE_BYTES_V1 = 376
IndexViewCarrierSlotV1 = (
  magic=MAGIC_INDEX_VIEW_CARRIER_V1, version u16=1, slot_index u8, publication_kind u8,
  slot_length u32=188, database_uuid[16], persisted_index_identity[32],
  carrier_identity_generation u64, index_mutation_generation u64,
  predecessor_mutation_generation u64, carrier_state u8, reserved_zero[7],
  carrier_plan_sha256[32], transaction_uuid[16], terminal_commit_lsn u64,
  commit_manifest_sha256[32], crc32c u32
) # exatamente 188 bytes
```

`IndexCarrierPublicationKindV1` é u8: `INVALID=0`, `INITIALIZE=1`, `COMMITTED_MUTATION=2`; 3..255
recusam. INITIALIZE exige mutation generation 1/predecessor 0/terminal 0/manifest ZERO; para baseline,
UUID é ZERO e plan SHA é a carrier-manifest entry, e para CREATE pre-WAL UUID/plan são os frozen da
Transaction V2 que criou a nova persisted identity. COMMITTED_MUTATION exige checked predecessor+1,
UUID/terminal/manifest/plan todos nonzero e bijetivos ao grouped COMMIT/`IndexMutationScope`.
`carrier_state` usa somente `LIVE=0` ou `TOMBSTONED=1`; outros bytes recusam e zero aqui é valor
explicitamente válido, não default ignorado.

Offsets são slot 0 em 0 e slot 1 em 188, EOF 376; CRC cobre os primeiros 184 bytes. A seleção é o
único slot CRC-valid de maior `index_mutation_generation`; empate válido, predecessor/salto, slot/
offset mismatch, cross-identity/DB, reserved/trailing byte, UUID/manifest zero errado ou mesmo
generation com qualquer payload distinto recusa. Publication é write-all no slot inativo, visibility
barrier e direct re-read/decode/raw hash sob EXCLUSIVE. O
`carrier_identity_generation` é a
`index_namespace_generation` que criou o carrier e nunca muda; o token lê também a generation global
corrente. Sob EXCLUSIVE+COMMIT, **antes** do WAL apenas `_IndexMutationDraftV1` detached congela o
scope committed, direct current `g`, checked `g+1`, target state, `carrier_plan_sha256`, exact slot
bytes e GenerationSuccessorSet claim; não grava slot, não devolve permit e não altera frame/header/
catálogo. Todo `IndexMutationScope` do CommitPayload persiste esses mesmos campos.

Depois da grouped WAL barrier, ainda sob os guards, o runner revalida manifest/draft/current e publica
uma única vez no slot inativo exatamente `(carrier_identity_generation,g+1,target_state)`, faz
visibility barrier e direct state cert, e somente então emite `_IndexMutationPermit` exact-type bound
ao committed UUID/terminal/manifest/carrier plan/slot raw SHA. Para K índices, todos os K slots são
publicados/certificados em identity order **antes do primeiro** index ou catalog effect. Só então
header/bucket/catalog apply começa; commit.state é o último control target. Mutação sem successor
publicado é proibida.

Falha pre-WAL deixa zero per-index slot. Kill pós-barrier antes/durante os K slots deixa WAL authority:
recovery toma fresh lease/EXCLUSIVE/COMMIT, redecodifica cada carrier plan e repete idempotentemente o
target exato, completando os slots ausentes antes de apply. Slot já bit-identical é redo; same
generation com carrier_state/bytes/plan diferente é CORRUPT/INCONCLUSIVE. Nunca se publica
`(g+1,LIVE)` pre-WAL e `(g+1,TOMBSTONED)` pós-WAL, não se reescreve active slot in-place e não existe
last-wins para empate. Esta regra vale DROP e toda outra mutação que altere qualquer field do carrier.

Readers
SHARED recebem o par atual; qualquer incremento invalida cache/graph, ainda que page0/LSN/Page.seq
voltem aos mesmos bytes.

Generation nunca faz wrap nem reset enquanto algum processo/cache da database generation pode viver.
No máximo u64, nova mutação recusa typed e torna a database source terminal para write; DDL/rekey da
identity antiga não é bypass. Somente cold export para novo DB UUID ou restore anterior segue a regra
global. O state
de coordenação não substitui o header/checkpoint durável após cold restart — caches também não
sobrevivem a restart —, mas é a prova shared necessária enquanto qualquer cache cross-process pode
sobreviver.

`participant_section` nunca satisfaz esse contrato: seu namespace inclui participant e não contende
com outro processo. Um mutex thread-local também não é fallback.

V7 escolhe um único modo implementável: reader mantém SHARED desde a direct pre-certification até o
resultado estar completamente detached, staging liberado atomicamente e direct post-certification
concluída. Todo mutador mantém EXCLUSIVE desde antes do primeiro header/bucket/frame mutável até
WAL/apply/barriers/direct final cert/publication. Logo nenhuma sequência stale→rebuild→clean pode
ocorrer durante a caminhada; ABA dentro da scope é excluído pela posse e ABA entre scopes pela
mutation generation, não deduzido de igualdade de bytes.

`Page.seq` 32-bit pode wrap e dois writers com frame antigo podem repetir valor; fingerprint canônico
V6 excluía seq. Nenhum dos dois é prova anti-ABA. V7 ainda valida seq/checksum/torn read e usa hash raw
como detector/cache key, mas correctness depende do RW guard + mutation generation e do inventário
fechado de mutadores.
Modo detached optimistic da V6 continua proibido nesta revisão. A shared mutation generation seria
parte necessária de uma emenda futura, mas não prova sozinha que todas as páginas/bytes/dependências
foram copiadas antes da liberação nem fecha staging/result exposure.

Todas as portas low-level que alteram index page/header exigem uma capability module-private
`_IndexMutationPermit`, exact-type, manager/PID/generation/identity/guard-nonce/attempt-bound e
revogada na saída EXCLUSIVE. Ele só nasce pós-WAL barrier e pós-direct-cert dos K successor slots,
e autentica exact committed carrier plan/UUID/terminal; `_IndexMutationDraftV1` nunca satisfaz essa
interface. `IndexStore._write_header`, bucket writes, RESET, create/grow, clear,
reconcile e graph **persistido** não são chamáveis sem ela. Isso torna o inventário estrutural:
um mutador novo que não declara identity/manifest/permit falha antes de pin/write.

#### 8.4.4 Certificado completo e cache/HNSW

V7 introduz `IndexHeader` record format v2 sem alterar o `FileHeader` INDEX v1. O prefixo de 44 bytes
do header atual permanece nos mesmos offsets, com `format_version=2`, seguido de extensão exata:

```text
IndexHeaderV2 = IndexHeaderV1Prefix(format_version=2)
  binding_magic[8]       = MAGIC_INDEX_IDENTITY_BINDING_V2
  binding_version u16    = 1
  reserved u16           = 0
  database_uuid[16]
  persisted_index_identity[32]
  extension_crc32c u32
```

O CRC da extensão cobre prefixo+extensão sem o próprio CRC; o Page CRC continua cobrindo a page0
inteira. Decoder antigo lê o prefixo e recusa format 2 typed. Decoder V7 exige tamanho exato, ambos
os CRCs, magic/version/reserved, DB UUID e identity vindos do checkpoint/history; trailing bytes,
header v1 em estado `BOUND_V2` e header v2 sob identity diferente são corruption. ALTER preserva a
identity. CREATE V7 sempre escreve header v2.

O nome físico V2 é imutável e derivado da identity, por exemplo
`indexes/v2/<persisted_index_identity_hex>.idx`; catálogo/runtime nunca adota arquivo por nome lógico.
DROP/recreate não reutilizam path. Arquivo já existente na CREATE só é aceito como redo da mesma
transação/identity com fingerprint exato; qualquer outro caso recusa. Isso impede que arquivo antigo
ou de outro database com definição igual seja aceito por contextual binding. Bucket pages continuam
protegidas por seus CRCs, WAL exact apply e pelo guard da mesma identity; page0 não é alegada como
Merkle root de todo o arquivo.

CREATE segue o mesmo timing de BIND: preflight congela virtual path/page images/manifest e pode criar
somente o carrier de coordenação órfão; afirma ausência de target/temp IndexV2 imediatamente antes do
append. Apenas depois da WAL barrier, `_IrrevocableWalPermit` autoriza
`create_entry_durable_after_wal`; catalog/page apply, directory/data barriers e direct cert precedem
commit.state. Recovery grouped recria o target do plan. Nenhum overload de `IndexStore.create` pode
usar a primitive bootstrap/coordinator para arquivo de dados.

Uma direct page0 read usa `StorageDevice.read_page`/codec frio, valida tamanho, checksum, FileHeader,
DB/index identity, definition, slot/flags/LSNs e torn read. Calcula SHA-256 sobre **todos os bytes raw
validados da page0**, incluindo Page.seq e CRC; não usa apenas payload canônico nem
`IndexStore._read_header()` cacheado.

Há dois valores frozen. O primeiro descreve estado persistido/cacheável e não contém snapshot nem
nonce efêmero:

```text
IndexStateCertificate(
  database_uuid,
  wal_fence=(identity_format, C, fence_generation),
  catalog_direct_raw_sha256,
  index_identity_sha256,
  definition_sha256,
  built_through_lsn,
  reconciled_through_lsn,
  durable_stale_flag,
  direct_page0_raw_sha256,
  index_namespace_generation,
  carrier_identity_generation,
  index_mutation_generation,
  pool_identity,
  pool_derived_epoch
)
```

O token de uma scope inclui o certificado integral e a prova snapshot/guard:

```text
IndexReadToken(
  state_certificate,
  snapshot_lsn=S,
  scope_published_lsn=R,
  checkpoint_lsn=Q,
  wal_publication_proof_sha256,
  runtime_device_replay_proof_sha256,
  tail_fingerprint,
  relevant_horizon,
  index_view_guard_nonce
)
```

Ele só é emitido se header está clean,
`relevant_horizon(I,Q,S) <= built_through_lsn <= R`, `S <= R`, e reconciled/checkpoint são coerentes.
S é o snapshot fixado pela conjunção `WalPublicationProof` + `RuntimeDeviceReplayProof`; R é
commit.state lido diretamente no
precheck apenas como teto contra header de gap/futuro. Um header legítimo pode estar à frente de S e
continua apto porque entries/tombstones são snapshot-aware e a reader pin impede reconcile além de S.
O postcheck reutiliza S/R/proof detached e relê header/catalog; commit posterior irrelevante não
invalida o statement. O **pre-token efetivo** (definido abaixo, após eventual refresh zero-I/O) e o
post-token precisam ser bit-identical e o guard precisa continuar SHARED/live. O token provisório
formado pela direct PRE antes de um CAS de epoch nunca é comparado ao POST nem autoriza traversal. A
post-read é feita depois da
última heap validation/row materialization e antes de `context.release()`. Guard revogado, I/O,
catálogo/identity diferente ou token drift descarta o resultado e executa rollback-to-mark antes de
retry/refusal.

`BufferPool.begin_read_view` recebe token global + tuple ordenada de tokens de índices. Existe **um só**
contador `pool.derived_epoch` por `pool_identity`, compartilhado por todas as identities desse pool. A
primitive process-local `POOL_DERIVED_EPOCH/<pool_identity>` é mutex+CAS atômico do contador; “global”
nesta subseção significa global ao pool do processo, não carrier cross-process. Coordenação entre
processos continua sendo SHARED/EXCLUSIVE + direct certificate; pools de processos diferentes jamais
compartilham epoch, lock, frame ou graph.

Cada direct **pre-read** do statement produz um token **provisório** e um
`IndexRefreshCandidateV1(database_uuid,index_identity,shared_guard_nonce,operation_attempt_sha256,
pre_read_ordinal,direct_page0_raw_bytes,direct_catalog_raw_sha256,carrier_generations,
expected_pool_identity,expected_pool_derived_epoch,state_certificate)`. Ele é frozen, exact built-in
bytes/scalars, bound ao attempt/guard e calculado fora de qualquer pool/cache lock. Quando o token muda,
a única porta permitida é
`refresh_index_cache(candidate, expected_state_certificate, expected_epoch, shared_guard_nonce)`. A
primitive não abre arquivo nem chama `read_page`: consome bit-identical o candidato que a pre-read
deste mesmo attempt já criou. Sua ordem total, sem overload ou fast path, é:

1. receber/revalidar shape, attempt, identity, guard e pre-read ordinal do candidato, com **zero direct
   I/O**; caller sem candidato precisa iniciar um novo outer attempt e sua pre-read normal, nunca fazer
   uma terceira leitura escondida;
2. tomar `POOL_DERIVED_EPOCH/<pool_identity>`;
3. tomar `INDEX_CACHE_REFRESH/<pool_identity,index_identity>`; a ordem inversa é proibida em **toda**
   rota que tome ambos para refresh ou publicação/invalidação de certificado/grafo. Page latch/pin e
   cache-miss normal não tomam locks de refresh, não podem escalonar do per-index para o global e
   continuam sob as primitives próprias do buffer pool;
4. sem I/O, reler atomicamente `pool.derived_epoch`, o certificado process-local e o inventário de
   frames. Exigir epoch corrente igual a `expected_epoch`, SHARED nonce ainda live e candidato ainda
   compatível. Divergência libera ambos os locks e retorna `RESTART_OUTER_ATTEMPT`, sem descarte,
   poison, troca de referência ou outra mutação;
5. executar `successor = checked_increment_u64(expected_epoch, "pool_derived_epoch")` **antes** de
   tocar frames/cache. Em MAX, fechar/envenenar o pool antigo conforme a regra global, liberar locks e
   recusar typed; nenhum frame/certificado/grafo é removido, substituído ou marcado;
6. inventariar o arquivo-alvo integralmente. Dirty ou pinned frame instala local poison/refusal sem
   executar o CAS nem descarte; não chama o atual `invalidate()`, que poderia flushar bytes antigos
   sobre repair estrangeiro;
7. fazer CAS exato `expected_epoch -> successor`. O vencedor publica o successor com release ordering
   **antes do primeiro discard**, descarta todos e somente os frames clean do arquivo, invalida as
   referências derivadas de epochs anteriores e publica o novo state certificate; retorna ainda
   `IndexRefreshSuccessorEvidenceV1(candidate identity/attempt/pre-read ordinal,
   expected_epoch,successor,published_state_certificate)` frozen e só então libera os
   locks. CAS loser libera ambos e retorna `RESTART_OUTER_ATTEMPT` com zero discard/poison/cache write;
8. emissão/reuso de token ou graph faz acquire-read do epoch e nunca observa a janela intermediária:
   toda porta capaz de observá-la passa pelo mesmo lock global. O post-token contém o successor já
   efetivo.

Depois do passo 7 e antes de qualquer traversal, o caller verifica integralmente a successor evidence
contra o candidato deste attempt/guard. Sem I/O e sem reler cache/device, reconstrói o certificado
efetivo substituindo **somente** `pool_derived_epoch=successor` no state certificate do candidato;
todos os bytes direct de page0/catálogo, generations, identity, definition e horizons permanecem
bit-identical. O resultado precisa ser bit-identical ao `published_state_certificate` retornado. O
caller então reconstrói o `IndexReadToken` efetivo com esse certificado e os mesmos S/R/Q/proofs/
tail/horizon/guard do provisório e descarta o token provisório. Se não houve refresh, token efetivo é
o próprio provisório. Evidence ausente, outro candidate/attempt/ordinal, campo além do epoch alterado,
successor diferente de checked `g+1` ou cache current diferente recusa/reinicia antes do traversal.
Assim stale cache em g pode retornar com PRE efetivo g+1 e POST g+1; exigir PRE provisório g==POST,
aceitar g!=g+1 ou fazer uma terceira direct read são três violações distintas.

Mesmo que o mutex serialize o CAS na implementação inicial, a comparação e o CAS continuam
normativos e testáveis: um `expected_epoch` capturado antes de esperar o mutex pode estar stale. Se A
e B, em identities diferentes, capturam MAX-1, somente um pode publicar MAX e descartar; o loser
reinicia sem descarte e então a regra MAX recusa antes de qualquer efeito. Não é permitido serializar
apenas por `(pool,index_identity)`, pois isso perde incremento global e viola o zero-effect refusal.

SHARED impede qualquer writer de mudar page0/carrier enquanto o candidato é consumido; drift
process-local de epoch/cache é detectado no passo 4. Portanto refresh não precisa nem pode repetir a
preimage no device. O refresh continua single-flight por `(pool,index_identity)` **dentro** da primitive global. Outro
SHARED da mesma identity espera os locks e usa o epoch/certificado já publicado; não tenta descartar
frames que o primeiro acabou de pinar. Nenhum dos dois locks atravessa direct I/O, traversal, callback,
métrica ou guard release, e ambos são proibidos sob COMMIT. Como todo writer EXCLUSIVE esperou readers
anteriores, um frame antigo ainda pinned/dirty nesse ponto é violação real e instala poison, não race
normal. Lock acquisition/restart consome o `OperationBudget`/`OperationAttempt` já selecionado; não
cria deadline nem attempt próprio.

O cache HNSW publica somente grafo detached imutável com:

`HnswGraphCertificate(index_state_certificate completo, pool_derived_epoch, graph_sha256)`.

Construir/publicar esse cache process-local usa `_DerivedGraphPermit` distinto, exact-type e bound ao
SHARED nonce/token/epoch; ele autoriza somente troca atômica de referência imutável e zero device/
header/bucket write. Não usa `_IndexMutationPermit` nem EXCLUSIVE. A construção detached pode ocorrer
sem pool lock, mas a publicação toma `POOL_DERIVED_EPOCH` e depois `INDEX_CACHE_REFRESH`, revalida
token/nonce/epoch e faz a troca atômica; epoch diferente descarta apenas o objeto detached ainda não
publicado e reinicia sem tocar cache. Eviction/invalidation que precise dos dois locks segue a mesma
ordem; nenhuma rota adquire primeiro o lock per-index e depois tenta o global.

`_graph_mark == built_through_lsn` é removido. Reuso exige active SHARED guard da mesma identity,
um `IndexReadToken` corrente cuja `state_certificate` seja bit-identical e epoch idêntico. Snapshot S
pode mudar sem reconstruir o grafo porque cada entry continua filtrada por S; header/definition/raw
page/epoch não podem. Failed cert, poison, discard de frame, mark/clear/rebuild ou guard revogado apaga
graph antes de liberar a scope. `visible_count`, exact vector scan, HNSW traversal e filter-aware
traversal usam a mesma scope; não há atalho por `k`/frontier.

#### 8.4.5 Shared stale versus local poison

Estado process-local separa:

`_shared_stale_reason: str | None`
: projeção refreshable do direct durable header/relevant horizon. Pode aparecer ou desaparecer após
  outra process concluir WAL+barrier+direct cert sob EXCLUSIVE.

`_local_poison_reason: str | None`
: sticky para partial apply local, dirty/pinned cache em token change, I/O uncertainty, guard/lease
  revogado, failed graph cert ou release incerto. Header estrangeiro clean nunca o limpa.

`stale == (_shared_stale_reason is not None or _local_poison_reason is not None)`. Lookup recusa se
qualquer metade existe. Local poison só limpa por (a) fechar e reconstruir manager/pool numa nova
process generation depois de recovery completo ou (b) repair local concluído sob fresh lease +
EXCLUSIVE, com zero frame dirty/pinned, barriers e cold direct certification; não existe setter
público. Refresh shared nunca mascara poison.

`check_freshness` torna-se puro: direct header atrás/stale apenas atualiza `_shared_stale_reason` e
recusa. Uma read/verify/status nunca chama `mark_stale`, flush ou WAL. Se o operador quiser publicar
STALE durável, usa a transação `MARK_STALE` write-capable separada.

Toda manutenção compartilhada é logged e manifestada. MARK_STALE durável precede primeira mutação de
RESET/rebuild; CLEAR_STALE só é alvo final depois de buckets/header/graph state duráveis e verificados.
Crash intermediário deixa gap WAL, durable STALE ou header atrás do horizon. `stage_reset` em índice
clean não começa a limpar bucket: primeiro precisa do effect MARK_STALE cometido, ou recusa.

#### 8.4.6 Statement scope e ponto de linearização

Planner/catálogo detached produzem `StatementIndexPlan` com todas as identities potenciais — exact
seek, relationship endpoints, primary-key uniqueness, vector exact/HNSW e índices necessários a
writes. O conjunto é congelado antes da primeira aquisição. Descobrir identity extra depois disso
libera tudo, faz rollback e reinicia; nunca adquire fora de ordem.

Fluxo de statement indexado:

1. preflight detached e staging mark externo da seção 7;
2. acquire SHARED de todas as identities em ordem;
3. exatamente uma direct pre-read por identity forma token provisório + `IndexRefreshCandidateV1`;
   cache divergente é descartado somente passando esse mesmo candidato à primitive zero-I/O acima.
   Sucesso do refresh reconstrói o token PRE efetivo a partir da successor evidence g→g+1; sem
   divergência, o provisório já é efetivo. Somente o PRE efetivo autoriza o passo 4;
4. atravessar páginas/live graph e validar heap sob snapshot P;
5. materializar `rows` e resultado em built-ins detached, sem callback/metric;
6. direct post-tokens ainda sob os mesmos SHARED;
7. somente depois do post-cert, `context.release()` sob o mark externo; falha executa rollback;
8. construir a cópia pública, zerar page pins/graph guards e liberar SHARED;
9. fora de guards/participant, emitir métricas/callback e retornar.

O ponto de linearização é o post-token do passo 6. Como EXCLUSIVE não pode começar antes da liberação,
`context.release()` e a cópia detached continuam protegidos. Mudança depois da liberação pertence ao
próximo statement; OCC do commit protege intents de write já staged. Nenhum COMMIT é adquirido por
reader enquanto SHARED/pin/graph guard está retido.

Um attempt estável que retorna faz exatamente duas page0 direct reads por identity usada (pre/post);
attempt recusado faz no máximo duas. Logo uma chamada faz no máximo
`2 * max_attempts * identities_planejadas`, nunca por candidato, row, heap read, `k` ou frontier
node. Instrumentação atribui cada read ao ordinal PRE=1 ou POST=2; terceiro ordinal, direct I/O dentro
de refresh ou reutilização de candidato de outro attempt/guard é assertion failure. Um statement sem
índice ainda exige ambas as proofs globais, mas não toma INDEX_VIEW.

Gate Event obrigatório parte de cache stale em epoch g, direct PRE current e frames clean, força o
refresh vencedor g→g+1 e retorna o statement uma única vez com exatamente duas direct reads,
`PRE_effective == POST == g+1`. Mutante que compara o PRE provisório g, mantém seu token após a
successor evidence, altera outro campo do certificado ou executa terceira read falha antes do
traversal/handout; CAS loser continua zero-discard e reinicia.

#### 8.4.7 Inventário obrigatório de readers e mutadores

| Porta/rota | Modo e lifetime |
|---|---|
| generic/exact `IndexSeek` | SHARED PRE efetivo → candidates → heap validation → POST/result |
| relationship from/to endpoint | mesmo scope central; proíbe lookup direto fora dela |
| primary-key/uniqueness lookup | mesmo scope; staging mark cobre a write preparation |
| vector visible_count/exact/HNSW/filter | SHARED durante count/traversal/ranking/materialização; graph cert completo |
| `Database.verify` e `IndexManager.verify` | SHARED de todas as identities ordenadas durante header+bucket+heap coverage; finding detached antes de release |
| `inspect_index`/CLI inspect | SHARED da identity durante header/páginas; output detached |
| status/open freshness | proof sandwich + SHARED curto, direct header-only; gap e local poison reportados separadamente |
| read-only verifier/reopen | WalPublicationProof + RuntimeDeviceReplayProof(P==Q) + SHARED; zero database mutation em sucesso/recusa |
| row/index commit e redo | WriteSnapshotProof no current P + fresh lease + EXCLUSIVE do `IndexFencePlan` antes de append/apply; recovery usa authority separada e só produz predecessor proofs depois do cert |
| CREATE/DROP/ALTER | NAMESPACE EXCLUSIVE para CREATE/DROP, depois EXCLUSIVE das identities old/new; carrier estável/tombstone e manifest bijetivo |
| MARK/CLEAR_STALE, RESET, rebuild | transação WAL v2 + EXCLUSIVE do começo ao direct final cert |
| reconcile/`INDEX_RECONCILE` | ReaderRegistrySnapshot/floor pin + fresh lease + EXCLUSIVE; cutoff estrito; nenhuma remoção antes de WAL barrier |
| recovery/gap completion/repair | certify sem guard; fresh lease; EXCLUSIVE; COMMIT revalidate/apply |
| checkpoint | EXCLUSIVE de todas as identities catalogadas; `CheckpointIndexManifest` completo |
| close/explicit flush capaz de escrever índice | EXCLUSIVE ou recusa se não puder provar zero dirty index frame |

Admin paths não têm privilégio para usar cache. Verify/inspect não reparam; reconcile é mutador e não
pode ser chamado por conexão read-only. Status que encontra I/O/poison devolve classificação detached
ou erro typed conforme API existente, nunca `healthy` por ausência de dados.

Verify físico/global faz ainda um sandwich sem manter COMMIT durante scan: certification-only proof
e catálogo antes dos SHARED; scan/materialização; libera pins/guards; nova certification-only proof.
Proof/catalog/history diferentes tornam o relatório `INCONCLUSIVE(concurrent_change)` ou retry pelo
budget, nunca findings/clean de épocas misturadas. A segunda proof termina antes de expor findings;
mudança posterior lineariza depois do verify. Inspect de uma identity usa o mesmo padrão projetado;
status/open que pretenda declarar `healthy` também usa o sandwich, ainda que sua fase SHARED leia só
page0. Se o proof anterior ou posterior detecta gap/incomplete/required desconhecido, status reporta
o estado correspondente e nunca `healthy`; a direct read sob SHARED é seu ponto de observação do
header, não substituto da prova global.

#### 8.4.8 Gates e mutantes

Gates multiprocess usam spawn + Event/barrier com timeout, sem sleep:

- pause reader depois do PRE efetivo; filho tenta MARK_STALE/RESET/rebuild: EXCLUSIVE só entra depois do
  result detached e o primeiro statement é integral; o seguinte vê novo estado;
- pause durante candidates, primeiro heap.read e HNSW frontier; mesmo resultado;
- dois readers de processos distintos mantêm SHARED simultaneamente; writer espera; nenhum reader
  espera outro reader;
- writer owner death/revocation faz reader post-cert recusar e limpa graph/cache, nunca aceitar token;
- direct page0 final com payload igual mas Page.seq/raw diferente invalida cache;
- mutante força Page.seq wrap/header bytes iguais; mutation generation muda, próximo scope descarta
  cache/graph e o guard ainda impede ABA dentro do statement;
- cache aquece buckets/graph, filho rebuilda no mesmo LSN; próximo scope descarta todos frames clean e
  exige novo HNSW certificate;
- dirty/pinned frame estrangeiro não é flushed por invalidação e instala local poison;
- duas threads no mesmo pool, identities A/B e epoch MAX-1, capturam o mesmo predecessor e pausam por
  Event depois da validação detached: exatamente uma vence o CAS, publica MAX antes de descartar e a
  outra reinicia com zero discard; no retry/MAX ambas as contagens de discard/cache/graph write do
  loser continuam zero. Variantes fazem A dirty e B clean, A pinned e B clean, e começam em MAX:
  poison de A não causa deadlock/inversão, B só avança se ainda houver successor, e MAX recusa antes de
  remover qualquer frame;
- parametrizar generic, exact, endpoints, uniqueness, visible_count, vector exact e HNSW;
- verify/inspect/status/reconcile/checkpoint passam pelo guard correto;
- commit durável acima de commit.state, incomplete tail e unknown-required impedem `begin`;
- I/O/page0 count permanece no máximo 2×identities por attempt e 2×max_attempts por chamada,
  independente de candidates/rows/k/frontier;
- fake clock parametriza RO begin, RW begin que muda clean→gap, RW cold open P>Q e query statement:
  cada helper recebe por identidade o mesmo `OperationBudget` e o mesmo `OperationAttempt` do attempt;
  somente o outer produz contextos frozen distintos na sequência exata `1..N`, sempre ligados ao
  mesmo envelope/digest e nunca além de max. Cancel vence timeout e nenhum nested flow cria factory,
  muta contexto, reseta ou amplifica budget.

Mutantes obrigatórios: usar published global; reinterpretar flag v1; esvaziar/forjar manifest; omitir
RESET/RECONCILE; consultar só stale local; usar header cacheado; hashear somente payload; omitir
derived_epoch do HNSW; proteger epoch só com lock per-index; inverter ordem per-index→global; fazer
discard antes do CAS; tratar CAS stale como sucesso; manter `_graph_mark`; substituir RW guard por participant/thread lock; adquirir
SHARED depois de candidates; liberar antes de materializar/context.release; usar detached optimistic;
permitir mutador sem EXCLUSIVE/permit; invalidar apenas page0; flushar dirty durante discard; certificar
snapshot sem scan acima de commit.state; criar target/temp IndexV2 pre-WAL, remover/forjar
`_IrrevocableWalPermit`, usar marker/bootstrap permit em CREATE/BIND; ou repetir sem budget.


## 9. Verifier, saneamento e upgrade

### 9.1 Oráculo preservado

O verifier aceito `40a4201` não pode regredir. Sua `_physical_heap_inventory`
(`engine/verifier.py:471-568`) inventaria owners e máximos fisicamente; falha de `page_count` produz
`FILE_UNREADABLE` e nenhum máximo. `_verify_record_id_counters` (`:571-682`) só acusa contador
quando o inventário é completo. V7 adiciona branch v2 antes do decoder, sem alterar os resultados
v1, identidades de findings, accounting, ordenação ou casefold aceitos.

No v2, verifier exige:

- exatamente uma identity entry canônica e zero slots reservados adicionais;
- extents reais únicos, catálogo/ownership coerentes e capacity respeitada;
- `F > max(record_id físico)` para todo ID entregável observado e todo extent real espelha F;
- header/T/generation válidos e page LSN justificável;
- nenhuma row/overflow/ref/index ambígua ou cross-owner. O par `(table_id,record_id)` precisa ser
  único; o mesmo número em tabelas distintas é válido, inclusive depois de upgrade v1.

Inventário recusado nunca produz false clean ou repair suggestion.

### 9.2 Repair v1 limitado

Repair automático só existe para `REPAIRABLE_V1_COUNTER`: todos os findings são exatamente
`RECORD_ID_COUNTER`, inventário físico é completo, ownership único e novo contador é apenas raise
até cobrir IDs observados. Não remove orphan, não libera página, não altera CSN/payload/ref e não
“corrige” extent chain ambígua.

O repair faz certification-only sem lease, sai, adquire fresh writer lease e qualquer EXCLUSIVE do
plano, reentra COMMIT e revalida digest/proof antes de imagens COW, `WalAppendPlan`, WAL barrier,
apply/write-back exatos, heap barrier e leitura direta final. Qualquer generation/tail/digest mudou:
sai, libera tudo e reinicia pelo `OperationBudget`. `CORRUPT`/`INCONCLUSIVE`: zero write.

### 9.3 Upgrade explícito

`db.upgrade_identity_leasing(maintenance_session)` é a única porta. A session exact-type é obtida
pelo runbook/coordinator offline e prova exclusividade naquele backend; booleano/string não é
aceito. Ela ainda não transforma upgrade em operação online segura. Read-only e lazy open não
migram. Pré-condições:

- fence WAL v2 da seção 5.3 já persistido em `grafx.meta`, C/checkpoint certificados e todo writer
  binário antigo encerrado; direct meta check existe no início de toda operação pública de escrita e
  novamente dentro de COMMIT;
- CLEAN_V1 recertificado sem lease, ou counter-only repair terminado e recertificado. A certificação
  produz `UpgradeDigest` frozen com DB UUID/generation, identidade+tamanho+SHA-256 direto de catalog e
  heap, catálogo canônico completo, inventário físico/ownership/maxima, heap0 fingerprint/page_lsn,
  checkpoint e tail proof/CommittedHistory;
- tabela IDs/capacidade v2 válidos; nenhum reserved ID legado;
- duplicatas numéricas cross-table são preservadas e não participam da recusa; duplicata dentro da
  mesma tabela continua `CORRUPT`;
- F inicial é o máximo checked entre `FIRST_RECORD_ID`, todos os `next_record_id` v1 certificados e
  `max_record_id_físico + 1`; a imagem eleva todos os extents reais para
  esse mesmo F e nunca reduz um contador legado;
- certification pass sem lease exige ambas as proofs; gap/device-behind sai e usa recovery/catch-up separado da
  seção 10. Com proof clean, sai de COMMIT, adquire fresh lease (e EXCLUSIVE das identities que o
  plano declarar), reentra em COMMIT e, ainda pre-WAL, relê catalog+heap e revalida **todo** o
  `UpgradeDigest`, current checkpoint/history/tail/classifier. Qualquer drift, inclusive catálogo
  alterado sem extent/heap0 alterado, descarta o plan, sai, libera guards/lease e reinicia pelo
  `OperationBudget`; não existe comparação apenas de heap0;
- `WalAppendPlan` frozen fornece `T` exato; imagem v2 codifica esse mesmo T;
- batch `FORMAT_TRANSITION`, WAL barrier, apply/write-back heap0, heap barrier, direct exact verify,
  publish, release;
- nenhum range é reservado ou ativado no upgrade.

Runbook offline mínimo, normativo:

1. parar e desabilitar restart automático de todos os processos/binários pre-fence;
2. fechar handles pre-V7 e adquirir o upgrade fence exclusivo do diretório; a API adquirirá seu próprio
   writer lease fresh e nunca aceita guard retido pelo runbook;
3. para writer lease, aplicar somente sua regra própria de expiry/takeover e owner fencing, se o
   backend a possui; TTL de lease **não** prova quiescência dos demais objetos. Reader registrations,
   `WAL_SCAN_PIN`, `READER_FLOOR_PIN`, participant records, `INDEX_VIEW` guards e handles precisam de
   close/deregistration/release explícito ou prova de owner death do backend/OS — nunca expiram por
   TTL, heartbeat atrasado ou espera temporal. Sob o upgrade fence, fazer visibility barrier e duas
   observações diretas bit-identical da mesma coordination generation, ambas com inventário vazio e
   zero lock/pin/guard/handle vivo; qualquer órfão sem owner-death comprovado bloqueia o runbook;
4. registrar backup cold, DB UUID, hashes de todos os arquivos e tail; reabrir cold read-only e verify;
5. executar a API uma única vez mantendo o fence externo até cold reopen v2 + verify;
6. só então iniciar binários V7. Qualquer processo antigo observado invalida o procedimento e exige
   restore do backup, não tentativa de downgrade.

O upgrade fence bloqueia participantes cooperativos, mas **não pode ensinar protocolo novo a um
binário pre-fence**. Portanto nenhuma API/CLI afirma provar quiescência por booleano; violar o runbook
é precondição operacional quebrada e risco explícito de overwrite v1 tardio. A identity V2 durável é
o fence permanente para todo reconnect antigo.

“Toda operação pública de escrita” inclui insert/update/delete, DDL de tabela/índice/espaço/vector,
maintenance que persiste header, checkpoint, recovery/repair, explicit flush e close que drenaria
dirty state. O segundo fence dentro de COMMIT impede que uma visão v1 cacheada sobreviva ao upgrade
feito por outro participante.

Crash antes do primeiro WAL byte deixa v1. Crash durante append parcial é uncertainty/recovery.
Crash pós-barreira com header ainda v1 é reconhecido pelo exact transition batch e recovery o
aplica. Crash pós-heap barrier/pré-publicação reaplica idempotentemente e só então publica.
Downgrade in-place é sempre recusado; export/restore para outro formato é operação futura distinta.

## 10. Recovery, checkpoint, read-only e continuidade do WAL

### 10.1 Prefix-only, não “gap recuperável”

O código real sustenta a política **prefix-only**:

- `domain/wal/segment.py:119-150`, `recyclable_prefix`, examina `segments[:-1]` e para no primeiro
  ilegível ou no primeiro segmento que alcança o horizon;
- `engine/wal_manager.py:1708-1787`, `WalManager.recycle`, oferece apenas esse prefixo e para se um
  nome não foi removido;
- `WalManager.truncate_after` (`:1502-1595`) retira sufixo novo e faz roll do corte, não cria gap
  interior normal.

As provas existentes ficam em `tests/wal/test_wal_segment_rules.py:105-149` (horizon, newest,
ilegível e stop-at-hole), `tests/wal/test_wal_manager_recycling.py:225-253` (deferral que conserva
o nome versus pending-delete que já o removeu) e `:406-416` (continuidade após recycle). Mutantes
obrigatórios trocam `break` por `continue`, incluem newest, trocam `< horizon` por `<=` e continuam
após recycle falhar mantendo o nome.

Assim, V7 não “completa” ou tolera buraco arbitrário. O conjunto retido é um sufixo lógico contíguo;
deletion deferral pode preservar fisicamente um nome, mas não legitima um interior ausente. Falha de
I/O que impede provar continuidade é `INCONCLUSIVE`; checksum, overlap ou descontinuidade confirmada
é `CORRUPT`. Nenhum dos dois ativa range ou publica recovery.

O item conhecido em `PUNCHLIST.md:258` sobre `_unflushed` de segmento deferred deve ser corrigido ou
formalmente excluído antes do gate v2: perder um segmento retido não pode ser reclassificado como
prefix recycling saudável.

### 10.2 Determinação de fase

Recovery lê header físico, checkpoint e `CommittedHistory` sob COMMIT:

Primeiro classifica o fence de semântica WAL:

- namespace vazio sem marker/artefato reconhecido: `GENESIS_EMPTY`, somente create explícito pode
  iniciar; artefato reconhecido sem marker é `GENESIS_ORPHANED/INCONCLUSIVE`;
- marker `V7_GENESIS` PREPARING/PREPARED/FENCED: `V7_GENESIS_INCOMPLETE`, mesmo se meta/heap ainda
  ausente ou já V2; somente resume/restore da seção 3.1.2, por hashes exatos, pode avançar;
- marker `V7_GENESIS/BOUND` exige meta V2 C=0, heap V2 T=0, P=Q=0 no baseline, checkpoint/carriers e
  manifests integrais; qualquer mismatch é CORRUPT/INCONCLUSIVE, nunca fallback legado;
- DatabaseIdentity v1 + nenhum marker/fence exato: `LEGACY_UNFENCED/WAL_V1_LEGACY`; nenhum record v2
  é aceitável, controles V7 ausentes não são dano e recovery V7 não cria nada;
- DatabaseIdentity v1 + marker `PREPARING`/`PREPARED`: `V7_BOOTSTRAP_INCOMPLETE`; somente
  resume/restore offline pode prosseguir. Carrier/sidecar ausente é comparado ao manifest, nunca
  criado por normal open;
- DatabaseIdentity v1 + fence transaction v1 completo/commitido ainda retido: crash pós-WAL barrier;
  exige marker `PREPARED` correspondente; recovery deve aplicar `grafx.meta:0` V2, fazer
  barrier/direct cert, publicar C e avançar o marker quando a boundary permitir;
- `grafx.meta:0` presente mas torn/indecodificável + único fence transaction committed exato ainda
  retido e checkpoint anterior a C: `META_FENCE_REDO`; recovery usa a target raw image autenticada
  pelo WAL para substituir a página, nunca chama isso de database ausente nem repair genérico;
- DatabaseIdentity V2 com `C == 0`: `WAL_V2_GENESIS`; todo record é v2;
- DatabaseIdentity V2, `C > 0`, `checkpoint < C`: exige fence transaction v1 exato ainda retido,
  slot checkpoint legado `(0,0,Q)` e modo `POST_FENCE_CHECKPOINT_REQUIRED`; nenhum record v2,
  generic writer, recycle, upgrade ou tipo 14 é permitido até checkpoint C + cold reconnect;
- DatabaseIdentity V2, `C > 0`, `checkpoint >= C`, slot `LEGACY_BASELINE`: modo
  `INDEX_IDENTITY_BINDING_REQUIRED`; só recovery/bind/checkpoint final são emitíveis e todo v1 header
  precisa constar no baseline;
- DatabaseIdentity V2 com slot `BOUND_V2`: meta+checkpoint são autoridade; transaction do fence e
  binds podem ter sido reciclados somente como prefixo, e todo índice catalogado exige HeaderV2/path
  identity-bound. Marker `FENCED` junto de checkpoint BOUND direct-valid é resume-only: maintenance
  recertifica L_terminal e publica marker `BOUND`, depois fecha; marker `BOUND` coerente permite que o
  cold reconnect final certifique/abra. Cold open público nunca avança phase;
- meta v1 com record v2, meta v2 com v1 pós-C, C diferente do terminal ou suposto fence abaixo de
  checkpoint enquanto meta ainda é v1 são estados impossíveis/corrupt. Meta V2 sem marker/bootstrap
  de genesis correspondente é sempre `GENESIS_ORPHANED/INCONCLUSIVE` — a ausência não prova se BOUND
  já existiu — e exige restore/operator decision; marker que contradiz slot/carrier é
  CORRUPT/INCONCLUSIVE.
  Meta ilegível sem a prova única acima é CORRUPT/INCONCLUSIVE, não guessing.

Só depois classifica o heap identity:

- heap v2, `T == 0`: `V2_GENESIS`;
- heap v2, `T > 0`, `checkpoint < T`: exige batch transition exato ainda retido;
- heap v2, `T > 0`, `checkpoint >= T`: header+checkpoint são autoridade; marker pode ter sido
  reciclado apenas como prefixo;
- heap v1 com transition exato retido/commitido: crash após WAL barrier e antes de aplicar; recovery
  deve aplicar a transição;
- heap v1 cujo suposto transition estaria abaixo de checkpoint é estado impossível: checkpoint não
  poderia ultrapassar T antes de heap0 v2 durável e certificado.

Depois do fence de `grafx.meta` e antes do exact heap transition, tipo 14 WAL format v2/REQUIRED só
pode ser a transition exata; outro shape recusa. Antes de C não existe tipo 14 conhecido e flags v1
são opacos. Unknown v2 SKIPPABLE continua opaco. Depois, a gramática identity v2 é obrigatória.

### 10.3 Simulação e floors

A base de `F` é confiável se o `page_lsn` de heap0 é `<= checkpoint_lsn`; se for maior, precisa ser a
imagem exata de uma transação cometida posterior. Recovery percorre `CommittedTransaction` em ordem,
valida `old_floor == simulated_floor`, fingerprint e shape, e só então avança para `new_floor`.
Consumers sem counter não avançam F. Reservation stops precisam ser contíguos a partir da base
confiável; qualquer interior ausente é dano/insuficiência, não gap de IDs autorizado.

O floor final publicado é o máximo **provado pela cadeia**, não o máximo de markers soltos. Recovery
aplica todos os efeitos exatos, `durable_exact`, e para format/reservation faz heap barrier e read direto
com `F >= floor`, T/generation/fingerprint corretos antes de publicar.

Para respeitar a ordem de sections, recovery tem duas passagens. A primeira entra em COMMIT somente
para classificar/ler história e produzir `RecoveryApplyPlan` + `IndexFencePlan` detached, sai sem
mutação e sem reter guard/pin. Então adquire **writer lease fresh**, adquire
`INDEX_VIEW(EXCLUSIVE)*` em ordem canônica, e reentra em COMMIT. A segunda relê meta/C, commit.state, tail/checkpoint/
history/catalog/index set e exige fingerprint idêntico antes do full preflight/apply; drift sai de
COMMIT, libera guards/lease/participant e reinicia pre-mutation pelo budget. Jamais adquire lease ou
`INDEX_VIEW` dentro do primeiro COMMIT, jamais reutiliza retained lease e jamais aplica com a
certificação antiga.

`RecoveryApplyPlan` contém também exatamente um `TailDispositionV1` total. Seus enums numéricos são:

| Enum `u8` | 0 | 1 | 2 | 3 | Demais |
|---|---|---|---|---|---|
| `TailDispositionKindV1` | `INVALID` | `KEEP` | `TRUNCATE_SUFFIX` | `REFUSE_INCONCLUSIVE` | recusam |
| `TailLocatorKindV1` | `INVALID` | `NONE` | `SEGMENT_OFFSET` | reservado/recusa | recusam |

`TailRefusalReasonV1` é `u16`: `NONE=0`, `INCOMPLETE_INTERIOR=1`, `INTERLEAVED=2`,
`TERMINAL_AFTER=3`, `SEGMENT_GAP=4`, `IO_INCONCLUSIVE=5`, `UNKNOWN_REQUIRED=6` e
`PREIMAGE_AMBIGUOUS=7`; 8..65535 recusam. O encoding canônico, sujeito integralmente à seção 0.1, é:

```text
DiscardedRangeProjectionV1 = (
  magic=MAGIC_DISCARDED_RANGE_PROJECTION_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], range_count u32, reserved_zero u32,
  ranges[range_count], crc32c u32
)
discarded_range_projection_entry := (
  range_ordinal u32, segment_number u64, file_identity[32],
  start_offset u64, end_offset u64, raw_sha256[32]
) # 92 bytes
discarded_range_projection_sha256 = SHA256(DOMAIN_DISCARDED_RANGE_PROJECTION_V1 || canonical projection bytes)

LedgerPredecessorBoundaryV1 = (
  magic=MAGIC_LEDGER_PREDECESSOR_BOUNDARY_V1, version u16=1, boundary_kind u8, reserved_zero u8,
  record_length u32, database_uuid[16], ledger_path_length u32,
  ledger_file_identity[32], ledger_raw_length u64, ledger_raw_sha256[32],
  last_event_offset u64, last_event_length u32, reserved_zero u32,
  last_event_sha256[32], ledger_root_binding_sha256[32],
  ledger_path_bytes[ledger_path_length], crc32c u32
)
ledger_predecessor_boundary_sha256 = SHA256(DOMAIN_LEDGER_PREDECESSOR_BOUNDARY_V1 || canonical boundary bytes)

TailMutationIdentitySeedV1 = (
  magic=MAGIC_TAIL_IDENTITY_SEED_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], source_tail_raw_sha256[32], discarded_range_projection_sha256[32],
  truncate_segment_number u64, truncate_segment_file_identity[32], truncate_offset u64,
  target_retained_tail_sha256[32], ledger_predecessor_boundary_sha256[32],
  discarded_range_projection_length u32, ledger_boundary_length u32,
  discarded_range_projection_bytes[discarded_range_projection_length],
  ledger_boundary_bytes[ledger_boundary_length], crc32c u32
)
tail_mutation_identity_seed_sha256 = SHA256(DOMAIN_TAIL_IDENTITY_SEED_V1 || canonical seed bytes)
quarantine_identity = SHA256(DOMAIN_TAIL_QUARANTINE_ID_V1 || tail_mutation_identity_seed_sha256)

BaseTailMutationIntentV1 = (
  magic=MAGIC_BASE_TAIL_INTENT_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], pre_tail_segment_count u32, discarded_range_count u32,
  pre_tail_logical_length u64, pre_tail_inventory_sha256[32], source_tail_raw_sha256[32],
  target_retained_tail_sha256[32], last_valid_segment_number u64,
  last_valid_segment_file_identity[32], last_valid_segment_length u64,
  last_valid_record_boundary_lsn u64, physical_tail_segment_number u64,
  physical_tail_segment_file_identity[32], physical_tail_segment_length u64,
  truncate_segment_number u64, truncate_segment_file_identity[32], truncate_offset u64,
  truncate_global_offset u64, required_alignment u32, reserved_zero u32,
  discarded_suffix_length u64, discarded_suffix_sha256[32],
  ledger_predecessor_boundary_sha256[32], barrier_plan_sha256[32],
  identity_seed_length u32, exact_set_length u32,
  tail_mutation_identity_seed_sha256[32], quarantine_identity[32],
  identity_seed_bytes[identity_seed_length], pre_tail_segments[pre_tail_segment_count],
  discarded_ranges[discarded_range_count], exact_set_bytes[exact_set_length], crc32c u32
)
base_tail_mutation_intent_sha256 = SHA256(DOMAIN_BASE_TAIL_INTENT_V1 || canonical intent bytes)

TailDispositionV1 = (
  magic=MAGIC_TAIL_DISPOSITION_V1, version u16=1, record_length u32,
  disposition_kind u8, locator_kind u8, refusal_reason u16,
  pre_tail_segment_count u32, discarded_range_count u32,
  pre_tail_logical_length u64, pre_tail_inventory_sha256[32], pre_tail_raw_sha256[32],
  last_valid_segment_number u64, last_valid_segment_file_identity[32],
  last_valid_segment_length u64, last_valid_record_boundary_lsn u64,
  physical_tail_segment_number u64, physical_tail_segment_file_identity[32],
  physical_tail_segment_length u64,
  truncate_segment_number u64, truncate_segment_file_identity[32], truncate_offset u64,
  truncate_global_offset u64, required_alignment u32, reserved_zero u32,
  discarded_suffix_length u64, discarded_suffix_sha256[32],
  base_tail_mutation_intent_sha256[32], quarantine_identity[32],
  ledger_predecessor_boundary_sha256[32],
  truncate_after_lsn u64, tail_mutation_composite_plan_sha256[32],
  pre_tail_segments[pre_tail_segment_count],
  discarded_ranges[discarded_range_count], crc32c u32
)
pre_tail_segment :=
  (segment_number u64, file_identity[32], raw_length u64, raw_sha256[32])
discarded_range :=
  (segment_number u64, file_identity[32], start_offset u64, end_offset u64, raw_sha256[32])
```

Identity seed length é nonzero e sua preimage/SHA precisa recomputar dos primitive fields e do range
set, antes de formar qualquer quarantine/temp path. Base intent repete seed SHA/quarantine identity e
seu exact set só pode usar os components derivados desse identity; mismatch ou derivar identity do
base/exact-set que já a contém é cycle/refusal. Exact-set/body embedded não pode conter base-intent,
composite, disposition, event ou ledger-successor SHA; ele autentica somente primitive source/
planned target/path/barrier preimages.

`LedgerBoundaryKindV1` é `INVALID=0`, `ABSENT_CANONICAL=1`, `CURRENT_EXACT=2`; 3..255 recusam.
ABSENT exige file identity/length/raw/last-event fields zero, mas root binding/path nonzero e target
absence direct-certified; CURRENT exige file identity/raw/root/path nonzero. Presence do último event é
codificada por `(last_event_length,last_event_sha256)`: ambos zero somente para ledger vazio; ambos
nonzero para ledger nonempty, enquanto `last_event_offset` pode legitimamente ser zero para o primeiro
event e precisa satisfazer `offset+length<=ledger_raw_length` com soma checked e raw bytes/hash exact.
Offset nonzero com length/hash zero, somente um de length/hash zero ou span fora do raw recusa. Path é
WAL_LEDGER canônico. Seed embedded
lengths são nonzero e decodificam exatamente `DiscardedRangeProjectionV1`/boundary; duplicated SHAs e
DB UUID casam.

O mapping do range é total. Entry i de `TailDisposition.discarded_ranges` vira projection ordinal i
copiando `(segment,file identity,start,end,raw SHA)`. No `WalMutationExactSetV1`, ela precisa apontar a
file entry WAL_SEGMENT com o mesmo segment/path/file identity; `raw_length==end-start`, range bytes/SHA
casam e `source_blob_offset` não entra na projection. Em `WalImmutableTreeManifestV1`, source range i
precisa ter class WAL_SEGMENT e path igual à re-encodificação canônica do mesmo segment; path/class são
validados mas não duplicados na projection. Counts/order/fields dos três mapeiam bijetivamente à mesma
preimage `DiscardedRangeProjectionV1`; missing/extra/reorder, file ordinal para outro segment ou hash
de outro domain recusa. Golden vector multi-segment congela os 92 bytes por entry e ambos os domain
separators.

`tail_disposition_sha256 = SHA256(DOMAIN_TAIL_DISPOSITION_V1 || canonical bytes)`; CRC cobre todos os bytes
anteriores, record length inclui CRC e canonical re-encode/EOF é obrigatório.

Truncation é necessariamente um composite fechado, nunca um permit genérico. Seus enums `u8` são
`TailMutationPhaseKindV1: INVALID=0, PRESERVE_AND_LEDGER=1, TRUNCATE=2` e
`TailMutationProgressV1: INVALID=0, PLANNED=1, PRESERVED=2, TRUNCATE_IN_PROGRESS=3,
TARGET_BARRIER_PENDING=4, TRUNCATED=5`; demais valores recusam.

```text
TailMutationCompositePlanV1 = (
  magic=MAGIC_TAIL_COMPOSITE_PLAN_V1, version u16=1, record_length u32,
  composite_identity[32], database_uuid[16], pre_tail_raw_sha256[32],
  target_retained_tail_sha256[32], base_intent_length u32,
  phase_count u8=2, reserved_zero[3], base_tail_mutation_intent_sha256[32],
  base_intent_bytes[base_intent_length], phase_templates[2],
  crc32c u32
)
phase_template := (
  phase_kind u8, operation_kind u8, target_kind u8, semantic_body_codec_kind u8,
  ordinal u32, semantic_body_length u32, semantic_body[semantic_body_length],
  source_preimage_sha256[32], target_postimage_sha256[32], barrier_plan_sha256[32]
)
tail_mutation_composite_plan_sha256 =
  SHA256(DOMAIN_TAIL_COMPOSITE_PLAN_V1 || canonical composite plan bytes)

TailMutationExecutionV1 = (
  magic=MAGIC_TAIL_EXECUTION_V1, version u16=1, record_length u32,
  tail_mutation_composite_plan_sha256[32], observed_progress u8,
  remaining_draft_count u8, reserved_zero u16,
  next_step_ordinal u32, progress_proof_length u32,
  generation_successor_set_sha256[32], progress_proof_sha256[32],
  remaining_wal_mutation_drafts[remaining_draft_count],
  progress_proof_bytes[progress_proof_length], crc32c u32
)

PreserveAndLedgerSemanticBodyV1 = (
  magic=MAGIC_PRESERVE_LEDGER_BODY_V1, version u16=1, reserved_zero u16, record_length u32,
  base_tail_mutation_intent_sha256[32], quarantine_identity[32],
  quarantine_tree_manifest_length u32, quarantine_path_length u32, ledger_path_length u32,
  quarantine_tree_manifest_sha256[32], ledger_predecessor_boundary_sha256[32],
  preservation_barrier_plan_sha256[32],
  quarantine_tree_manifest_bytes[quarantine_tree_manifest_length],
  quarantine_path_bytes[quarantine_path_length], ledger_path_bytes[ledger_path_length], crc32c u32
)

TruncateSemanticBodyV1 = (
  magic=MAGIC_TRUNCATE_BODY_V1, version u16=1, reserved_zero u16, record_length u32,
  base_tail_mutation_intent_sha256[32], truncate_segment_number u64,
  truncate_segment_file_identity[32], truncate_offset u64,
  discarded_range_projection_sha256[32], source_tail_raw_sha256[32],
  target_retained_tail_sha256[32], truncation_barrier_plan_sha256[32],
  progress_plan_length u32, reserved_zero u32, progress_plan_sha256[32],
  progress_plan_bytes[progress_plan_length], crc32c u32
)

WalMonotonicProgressPlanV1 = (
  magic=MAGIC_WAL_MONOTONIC_PROGRESS_PLAN_V1, version u16=1, workflow_kind u8,
  stable_source_codec_kind u8, record_length u32, database_uuid[16],
  stable_source_plan_sha256[32], source_inventory_sha256[32], target_inventory_sha256[32],
  stable_source_plan_length u32, step_count u32, exact_set_length u32, reserved_zero u32,
  stable_source_plan_bytes[stable_source_plan_length],
  steps[step_count], exact_set_bytes[exact_set_length], crc32c u32
)
monotonic_progress_step := (
  ordinal u32, step_kind u8, source_presence u8, target_pre_presence u8,
  target_post_presence u8, source_file_ordinal u32, target_file_ordinal u32,
  source_parent_ordinal u32, target_parent_ordinal u32,
  source_raw_length u64, source_raw_sha256[32],
  target_raw_length u64, target_raw_sha256[32],
  barrier_flags u8, allow_canonical_prefix u8, reserved_zero u16,
  pre_state_projection_sha256[32], post_state_projection_sha256[32],
  barrier_plan_sha256[32]
) # exatamente 204 bytes
wal_monotonic_progress_plan_sha256 =
  SHA256(DOMAIN_WAL_MONOTONIC_PROGRESS_PLAN_V1 || canonical plan bytes)

WalMonotonicProgressProofV1 = (
  magic=MAGIC_WAL_MONOTONIC_PROGRESS_PROOF_V1, version u16=1, workflow_kind u8,
  observed_state_kind u8, record_length u32, database_uuid[16],
  progress_plan_sha256[32], durable_parent_plan_sha256[32],
  current_wal_mutation_generation u64, completed_step_count u32,
  active_step_ordinal u32, current_inventory_length u32, reserved_zero u32,
  current_inventory_sha256[32], active_target_length u64,
  active_target_prefix_sha256[32], current_inventory_bytes[current_inventory_length], crc32c u32
)
wal_monotonic_progress_proof_sha256 =
  SHA256(DOMAIN_WAL_MONOTONIC_PROGRESS_PROOF_V1 || canonical proof bytes)
```

`TailMutationExecutionV1` tem SHA `SHA256(DOMAIN_TAIL_EXECUTION_V1||canonical bytes)`; ambos record lengths
incluem CRC e nenhum trailing/unknown phase/draft é aceito.

Os bounds tail/recovery são parte do byte contract, não defaults de allocator:
`MAX_TAIL_SEGMENT_OR_RANGE_COUNT_V1=65535`, `MAX_TAIL_PATH_BYTES_V1=65535`,
`MAX_TAIL_NESTED_BYTES_V1=134217728`, `MAX_TAIL_TRANSCRIPT_BYTES_V1=268435456` e
`MAX_TAIL_EXECUTION_BYTES_V1=1073741824`. A tabela usa S=segment count, R=range count,
L=path/nested length, B0/B1=semantic bodies e Dᵢ=record_length de draft:

| Codec | Counts/lengths | Equação exata de `record_length` |
|---|---|---|
| `DiscardedRangeProjectionV1` | R 0..65535 | `44 + 92*R <= MAX_TAIL_NESTED` |
| `LedgerPredecessorBoundaryV1` | path 1..65535 | `192 + path_length <= 65727` |
| `TailMutationIdentitySeedV1` | projection/boundary nonzero e nos maxima acima | `220 + discarded_range_projection_length + ledger_boundary_length <= MAX_TAIL_NESTED` |
| `BaseTailMutationIntentV1` | S/R 0..65535; seed/exact-set 1..67108864 | `492 + identity_seed_length + 80*S + 88*R + exact_set_length <= MAX_TAIL_NESTED` |
| `TailDispositionV1` | S/R 0..65535 | `446 + 80*S + 88*R <= MAX_TAIL_NESTED` |
| `TailMutationCompositePlanV1` | phase count exatamente 2; base 1..134217728; cada body 1..134217728 | `386 + base_intent_length + B0 + B1 <= MAX_TAIL_TRANSCRIPT` |
| `PreserveAndLedgerSemanticBodyV1` | manifest 1..134217728; paths 1..65535 | `192 + manifest_length + quarantine_path_length + ledger_path_length <= MAX_TAIL_TRANSCRIPT` |
| `TruncateSemanticBodyV1` | progress plan 1..134217728 | `268 + progress_plan_length <= MAX_TAIL_TRANSCRIPT` |
| `TailMutationExecutionV1` | remaining draft count 0..2; proof 0 ou 197..67109060 conforme state | `126 + sum(D_i) + progress_proof_length <= MAX_TAIL_EXECUTION` |
| `WalImmutableTreeManifestV1` | source/entry counts 0..65536; cada path 1..65535; inline 0..67108864 | `92 + sum(96+source_path_length) + sum(68+entry_path_length+inline_length) <= MAX_TAIL_TRANSCRIPT` |
| `ForensicLedgerAppendPlanV1` | paths 1..65535; event 1..MAX_TAIL_TRANSCRIPT | `188 + ledger_path_length + ledger_temp_path_length + event_record_length <= MAX_TAIL_TRANSCRIPT` |
| `TailPreservationEventV1` | disposition/composite/tree 1..MAX; paths 1..65535 | `468 + disposition_length + composite_length + manifest_length + quarantine_path_length + ledger_path_length <= MAX_TAIL_TRANSCRIPT` |
| `TailPreservationEventAdoptionProofV1` | progress length exatamente 340 | `220 + 340 == 560` |

Em vectors variáveis, cada entry_length precisa igualar sua fórmula e caber no enclosing remaining
bytes; counts densos/order/uniqueness continuam obrigatórios. Todos os produtos/somas são checked em
u64, comparados ao max e ao u32 `record_length` **antes** de slice/allocation/hash; só então se aloca.
Para S/R, o valor 65535 é deliberadamente idêntico ao máximo de ranges do pin e ao máximo de
segmentos de `WalAppendDraftV1`: a mesma projection Q→physical-tail deve caber nos três codecs.
Nenhum field tail de segment/range count aceita 65536 ainda que seu byte length isolado coubesse no
enclosing max; counts auxiliares de nodes/steps não aumentam esse horizonte físico.
Zero quando nonzero é exigido, `0xffffffff`, max+1, count×entry overflow, nested EOF/trailing e swap
de dois nested codecs recusam pelo mesmo erro typed. Golden fixtures cobrem 0/1/N, exact max, ±1 e
truncamento em cada boundary. Os bounds já declarados de `WalMonotonicProgressPlan/Proof` permanecem
mais estreitos e prevalecem.

`TailSemanticBodyCodecKindV1` é `INVALID=0`, `PRESERVE_AND_LEDGER_V1=1`, `TRUNCATE_V1=2`;
3..255 recusam. O composite semantic plan contém as **preimages completas** necessárias à execução,
não apenas seus SHAs, e é independente de process birth, COMMIT nonce, generation, permit e attempt.
Seu base intent decodifica bit-identical ao record acima. A matriz total é fixa: ordinal 0 é exatamente
`PRESERVE_AND_LEDGER/QUARANTINE_REPAIR/QUARANTINE_REPAIR_BUNDLE/codec 1`; ordinal 1 é exatamente
`TRUNCATE/TRUNCATE_SUFFIX/SUFFIX_CUT_SET/codec 2`. Bodies decodificam nos dois layouts, repetem o
mesmo base SHA e todos seus source/cut/range/target/barrier fields casam ao intent. Qualquer outra
count/order/combinação/body-swap recusa.

`WalMonotonicProgressWorkflowKindV1` é `INVALID=0`, `SUFFIX_CUT=1`, `PREFIX_RECYCLE=2`; 3..255
recusam. `WalMonotonicStepKindV1` é `INVALID=0`, `RECYCLE_WHOLE_SEGMENT=1`,
`CREATE_EMPTY_NUMBER_RESERVATION=2`, `CREATE_ROLL_TEMP=3`, `FILL_ROLL_TEMP=4`,
`PUBLISH_ROLLED_SEGMENT=5`, `RELEASE_CUT_SEGMENT=6`; 7..255 recusam.
`WalProgressPresenceV1` é `INVALID=0`, `ABSENT=1`, `CURRENT_EXACT=2`,
`CURRENT_CANONICAL_PREFIX=3`; 4..255 recusam. A matriz total é:

`WalMonotonicStableSourceCodecKindV1` é `INVALID=0`, `BASE_TAIL_INTENT=1`,
`REQUESTED_CHECKPOINT_BOUNDARY=2`; 3..255 recusam. SUFFIX_CUT exige kind 1, preimage integral
`BaseTailMutationIntentV1` e
`stable_source_plan_sha256=SHA256(DOMAIN_BASE_TAIL_INTENT_V1||canonical embedded bytes)`.
PREFIX_RECYCLE exige kind 2, preimage integral `RequestedCheckpointBoundaryProofV1` e
`stable_source_plan_sha256=SHA256(DOMAIN_REQUESTED_BOUNDARY_PROOF_V1||canonical embedded bytes)`.
Ambos repetem o mesmo database UUID. Length/bytes/SHA são nonzero e canonical; zero, cross-workflow,
direct `BoundaryProofV1` usado como stable source, hash sem preimage ou outro codec recusa. A source
preexiste ao progress plan: nunca é CheckpointIndexManifest/CheckpointCompositePlanCore, composite/
event ou digest que contém os próprios progress bytes. No resume PREFIX, a requested wrapper embedded
permanece a stable source; o direct boundary proof fresh apenas recertifica sua boundary.

A matriz de steps é:

| Step | source→target | prefix | barriers mínimos |
|---|---|---:|---|
| 1 | CURRENT_EXACT→ABSENT do mesmo WAL_SEGMENT | 0 | source-parent namespace |
| 2 | ABSENT→CURRENT_EXACT empty reservation, mesmo ordinal | 0 | file-data + target-parent |
| 3 | ABSENT→CURRENT_CANONICAL_PREFIX length 0 do roll temp | 1 | nenhum antes do fill |
| 4 | CURRENT_CANONICAL_PREFIX→CURRENT_EXACT do mesmo temp | 1 | file-data |
| 5 | temp CURRENT_EXACT→ABSENT e final ABSENT→CURRENT_EXACT | 0 | source+target parent |
| 6 | cut CURRENT_EXACT→ABSENT do mesmo WAL_SEGMENT | 0 | source-parent namespace |

Step 1 é permitido nos dois workflows; 2–6 somente SUFFIX_CUT. PREFIX_RECYCLE contém exatamente uma
step 1 por segmento removível em ordem crescente de segment number — o mesmo prefixo lógico que
`WalManager.recycle` remove — e nenhum temp/cut-roll. SUFFIX_CUT contém steps 1 dos segmentos
**newest-first**, depois: se o corte retém bytes, 3,4,5,6 exatamente uma vez; se retém nada, step 2
precede a release do último live segment e 3–6 não aparecem. Um cut já em boundary de arquivo omite
3–6 somente quando a target inventory bit-identical prova que não há old cut image a substituir.
`source_file_ordinal`, `target_file_ordinal` e parents indexam o embedded
`WalMutationExactSetV1`; nenhuma sentinel/ordinal fora do count existe.

`MAX_WAL_MONOTONIC_PROGRESS_PLAN_BYTES_V1=134217728`; `step_count` é 1..65536,
`stable_source_plan_length` e `exact_set_length` são 1..67108864, ordinals são densos 0..N−1 e
`record_length == 148 + stable_source_plan_length + 204*step_count + exact_set_length <= MAX`;
CRC/EOF são exatos, com todas as multiplicações/somas checked. O exact set contém
a união fechada de todos os source/temp/target
components, capabilities, raw images e barriers, sem entry alheia. Future temp/final usa
PLANNED_POSTIMAGE/file identity ZERO no stable plan; direct proof posterior usa CURRENT com file
identity real e autentica esse binding em `current_inventory_sha256`. Para cada boundary lógico
0..N, o verifier simula em ordem a matriz acima e calcula:

```text
SHA256(DOMAIN_WAL_MONOTONIC_LOGICAL_STATE_V1 || workflow_kind_u8 ||
       stable_source_plan_sha256 || LE32(boundary_ordinal) ||
       canonical_normalized_inventory_projection)
```

Esse valor precisa igualar `pre_state_projection_sha256` da step `i` para boundary i e
`post_state_projection_sha256` para boundary i+1; adjacentes precisam ser bit-identical no elo comum.
A projection normaliza somente future file identity PLANNED→tag; path, class, segment number,
presence, length, raw/tree SHA, source identities, ordering e parents permanecem. Primeiro state
iguala `source_inventory_sha256`, último `target_inventory_sha256`; missing/extra/reorder, dois states
iguais para steps destrutivas ou SHA opaco não recomputável recusa.

`WalMonotonicObservedStateKindV1` é `INVALID=0`, `AT_STEP_SOURCE=1`,
`AT_CANONICAL_TARGET_PREFIX=2`, `AT_STEP_TARGET_BARRIER_UNCERTAIN=3`,
`TERMINAL_TARGET_BARRIER_UNCERTAIN=4`, `TERMINAL_DIRECT_CERTIFIED=5`; 6..255 recusam. A proof é
attempt-local e seu inventory embedded decodifica exatamente `WalMutationExactSetV1` com somente
ABSENT/CURRENT e file identities atuais — PLANNED_POSTIMAGE é proibido. `current_inventory_length` é
1..67108864, `record_length==196+current_inventory_length<=67109060`, sem trailing/padding. O
classifier cold/direct de **todos** os components segue esta função total, em ordem, e nunca testa
formas concorrentes:

1. se todas as N postconditions estão exatas, retorna kind 4, completed/active=N;
2. se a step 4 ativa possui bytes com
   `0 < active_target_length < target_raw_length` e raw prefixo canônico da postimage, retorna kind 2,
   completed/active=k dessa step;
3. seja k a única boundary interior cuja inventory tem postconditions `[0,k)` e preconditions
   `[k,N)`. Se a step `k-1` possui qualquer barrier flag, retorna **kind 3 da predecessora** com
   completed/active=`k-1`; seu postimage físico existe, mas a barrier será repetida;
4. se `k==0`, ou se `k>0` e a step `k-1` é declaradamente barrier-free, retorna kind 1 com
   completed/active=k;
5. kind 5 jamais nasce de cold bytes: só o attempt que repetiu todos os barriers exigidos e concluiu
   direct cert pode emiti-lo para a forma terminal.

Assim a mesma boundary física depois de uma syscall com barrier é **sempre** kind 3, tenha o crash
ocorrido imediatamente antes ou depois da syscall de barrier; resume repete a barrier e não infere
sua durabilidade. A boundary terminal é sempre kind 4 em cold scan, nunca kind 3. Após repetir uma
barrier interior, o executor segue diretamente à próxima step no mesmo permit; não serializa kind 1
sobre os mesmos bytes sem evidence durável. Crash nessa janela volta a kind 3 e repete a barrier.

Length 0 é exclusivamente a source exata da step 4/kind 1; length==target é target da step 4/kind 3.
Nenhum endpoint é kind 2. Para kind 1, `active=completed<N`; kind 2 idem. Para kind 3,
`active=completed<N` aponta a step cujo postimage já existe e que ainda não conta como completa.
Kinds 4/5 exigem ambos N. `active_target_length/prefix_sha` são nonzero somente em kind 2; demais
exigem zero. Kind 5 só pode ser emitido pelo attempt que acabou de repetir barriers/direct cert.
Nenhuma outra partialidade é progresso.
Segmento removido fora da ordem, file que reaparece, temp
parcial que não é prefixo canônico, ambos temp+final, identity/path/hash diferente, source cut ausente
antes de final existir, subset com gap ou terceiro byte state é INCONCLUSIVE/CORRUPT. Como barrier não
é inferível de bytes após crash, kinds 3/4 **sempre** repetem idempotentemente data/parent barriers;
nunca assumem durabilidade. Só então podem avançar ou emitir kind 5. Para SUFFIX_CUT,
`durable_parent_plan_sha256` é exatamente `tail_preservation_event_sha256` do event já ledger-
barriered/direct-certified que embute o composite/progress plan. Para PREFIX_RECYCLE é exatamente o
digest registrado do `CheckpointIndexManifestV1` já publicado/direct-certified que embute o mesmo
progress plan. O prefixo retomável é `k∈[0,N)`, inclusive `k=0` quando o stable CAS foi publicado e o
processo morreu antes do primeiro unlink; current generation/edge adoption distingue esse estado do
pre-CAS source sem exigir mudança física. ZERO, core/composite SHA no lugar do parent, parent não durável ou bytes não disponíveis
recusa antes do primeiro unlink.

O DAG é único e sem future raw output:

```text
TailMutationIdentitySeed primitives/bytes/SHA
  -> quarantine_identity = SHA256(DOMAIN_TAIL_QUARANTINE_ID_V1 || identity_seed_sha)
  -> exact-set target names + BaseTailMutationIntent bytes/SHA
  -> WalMonotonicProgressPlan SUFFIX_CUT (sem event/composite/attempt fields)
  -> two semantic bodies (truncate embeds o progress plan; sem event/ledger successor/draft fields)
  -> composite_identity = SHA256(DOMAIN_TAIL_COMPOSITE_ID_V1 || base_intent_sha || SHA256(body0) || SHA256(body1))
  -> TailMutationCompositePlan bytes/SHA
  -> TailDisposition bytes/SHA (referencia somente base/composite/quarantine/ledger predecessor)
  -> event_identity = SHA256(DOMAIN_TAIL_EVENT_ID_V1 || base_intent_sha || composite_plan_sha || disposition_sha)
  -> TailPreservationEvent bytes
  -> ForensicLedgerAppendPlan e ledger successor raw length/SHA/boundary
```

TailDisposition não contém event SHA/identity nem ledger successor. Semantic bodies não contêm
TailDisposition/event/draft. O event não contém successor raw SHA; o append plan o calcula depois.
Mutante que inclui output à direita numa preimage à esquerda, body opaco ou self-hash recusa.

A matriz de execution também é total. `PLANNED` tem exatamente duas formas desjuntas: (a) antes da
phase-0 CAS, pending NONE, `remaining_draft_count=2`, drafts phase ordinals 0/1, stable chain
`g→g+1→g+2` e proof length/SHA zero; (b) depois da CAS, pending HELD_OPEN/g, current `g+1`,
`remaining_draft_count=1` contendo somente a draft estável phase 1 `g+1→g+2`,
`next_step_ordinal=0`, e progress proof exatamente os 340 bytes de
`ForensicLedgerCowProgressProofV1` state 1..4 para o event original. Na forma (b), o fresh successor
set omite a edge phase 0 held-open e conserva a phase-1 edge; não contém draft/edge auxiliar phase 0.
Qualquer outra combinação PLANNED de pending/count/proof/generations recusa no decoder.
`PRESERVED` exige count 1, somente phase 1, proof
kind 1 com `completed_step_count=next_step_ordinal=0`, pending ainda HELD_OPEN/g e a stable phase-1 edge ainda não queimada;
nenhum byte destrutivo ocorreu. `TRUNCATE_IN_PROGRESS` tem a mesma única draft phase 1 e exige
exatamente kind 1 com `completed_step_count>=0` **ou** kind 2/3, `next_step_ordinal` exato e role
`ATTEMPT_AUXILIARY_EDGE`, porque a stable phase-1 edge já foi queimada. A combinação
IN_PROGRESS/kind1/k=0 é obrigatória no resume quando o CAS da edge ficou durável mas o processo morreu
antes do primeiro syscall; diferencia-se de PRESERVED pelo successor/current generation e edge role,
não pelos bytes físicos iguais. `TARGET_BARRIER_PENDING` exige
count 0/set WAL vazio, next=N e exclusivamente proof kind 4;
`TRUNCATED` exige count 0/set WAL vazio, next=N e proof kind 5. Nos states sem proof, length/SHA são
zero; nos demais ambos são nonzero e os bytes decodificam bit-identical.

`_TailTruncationBarrierContinuationPermit` exact-type deriva exclusivamente de
`event_identity + base_intent_sha + composite_plan_sha + progress_plan_sha + progress_proof_sha +
target_retained_tail_sha + fresh direct target proof + budget/attempt`; ela não tenta reconstruir o
fingerprint da draft perdida e autoriza somente repetir os barriers da step ativa/prefixo e cold
reads, nunca unlink/truncate/create/write/rename. `INVALID`, PRESERVED com kind 2/3/4,
IN_PROGRESS k=0 sem auxiliary/adoption da stable edge, target kind 4 com draft, endpoint 0/full
encoded como prefixo, matriz
count/proof divergente, ordinal
fora de 0..N, role stable/aux errada ou edge/action/fingerprint que não case bijetivamente com a única
draft compound restante recusa no decoder antes de permit/publication.

Na primeira execução, os dois `WalMutationDraftV1` exact-type são materializados sob o attempt
corrente: preserve usa `g→g+1`, truncate compound usa `g+1→g+2`. O `GenerationSuccessorSetV1` contém
essas duas stable edges ordenadas na única entry `WAL_MUTATION_GENERATION`, e admission/
recertification provam headroom, claims, source/postimage e ambos os burns **antes do primeiro
efeito**. O CAS phase 0 grava HELD_OPEN/g junto do successor; isso é parte do target exato da primeira
edge. Cada phase adquire seu próprio `_WalMutationPermit`, ligado a fingerprint/kind/target/ranges;
permit da phase 0 não chama truncate e permit da phase 1 não cria quarantine/ledger. Antes do CAS da
phase 1, current é exatamente successor da phase 0, o event está direct-certified, a postimage phase 0
é a source preimage phase 1 e o mesmo slot successor phase 1 limpa HELD_OPEN/g para NONE/zero.
O permit phase 1 é single-use pelo único entrypoint compound `_execute_suffix_cut_progress`, que
revalida a precondition de cada step imediatamente antes do syscall e não expõe seus primitives.

A phase 0 cria/adota o artefato quarantine bit-identical, faz seus data/namespace barriers e publica/
direct-certifica `TailPreservationEventV1` no forensic ledger. Esse evento persiste a preimage completa
do composite plan, composite identity/SHA, source-tail/ranges/quarantine hashes, draft-0 result e o
target-retained-tail SHA; evento já existente só é aceito bit-identical. Só então `observed_progress`
pode ser PRESERVED e a phase 1 adquirir seu permit. TRUNCATED é derivado somente do mesmo evento mais
um cold scan que prove a target retained tail exata; não depende de RAM nem exige escrever ledger com
permit de truncate.

Crash/burn invalida permits/drafts, não o stable plan/event nem HELD_OPEN/g. Antes do event terminal,
resume usa exclusivamente a forma PLANNED(b) e `_TailPhase0ContinuationPermit`: reconstrói os bytes do
event com o par persistido, nunca com a generation/attempt fresh, e termina/adota o COW sem novo CAS.
Depois do event, resume relê event, faz direct inventory de
todos os components e produz a classificação **total de prefixo monotônico** acima. Source integral é
k=0; cada segmento newest-first já ausente, temp empty/prefix/full, rename publicado com old cut ainda
presente e old cut liberado são estados legítimos distintos do mesmo plan. Kind 1/2 reconstrói uma
única fresh draft para `[active step,N)` sob current generation; kind 3 repete primeiro os barriers da
step e então continua; target integral kind 4 repete apenas barriers/direct cert; só kind 5 vira
TRUNCATED e permite publicar H. PLANNED sem event só reconstrói ambas phases sobre current fresh quando
pending é NONE; com HELD_OPEN, trocar event/draft ou publicar `g+1→g+2` antes do event é recusa. O
`OperationBudget`/deadline original permanece e a nova `GenerationSuccessorSetV1` reserva worst-case
burns dos attempts/phases restantes. Estado fora da cadeia, event sem quarantine, tail progress sem
event ou duas composite identities é CORRUPT/INCONCLUSIVE; não se repete syscall já target-exact nem se
pula step.

`record_length` é o número exato de bytes desde magic até **e incluindo** o `crc32c u32` posterior ao
último range. `pre_tail_segments` é estritamente
ordenado por `segment_number`, único, contíguo no inventário retido e casa byte a byte com o
`WAL_SCAN_PIN`; `pre_tail_inventory_sha256` cobre seus encodings e `pre_tail_raw_sha256` cobre a
concatenação, na mesma ordem, dos bytes físicos integrais. `pre_tail_logical_length` é a soma checked
dos `raw_length`. O locator `last_valid_*` deve apontar para o último record/frame integral
decodificado e sua boundary LSN; ele nunca é inferido de length apenas. `physical_tail_*` casa
exatamente com a última entry de `pre_tail_segments` e pode ter número maior quando um BEGIN/record
incomplete ocupa segmentos posteriores. Confundir os dois recusa.

`KEEP` exige `locator_kind=NONE`, `refusal_reason=NONE`, `discarded_range_count=0` e **zero canônico**
em todo campo exclusivo de truncation: locator/offset/alignment do corte, suffix length/hash,
base-intent/quarantine, ledger predecessor boundary, `truncate_after_lsn` e composite-plan SHA. `TRUNCATE_SUFFIX` exige
`locator_kind=SEGMENT_OFFSET`, `refusal_reason=NONE`, todos esses campos preenchidos/nonzero, uma
sequência não vazia de ranges estritamente ordenada lexicograficamente por
`(segment_number,start_offset)` e não sobreposta. `truncate_offset` é local ao
`truncate_segment_number`; `truncate_global_offset` é obrigatoriamente
`checked_sum(raw_length dos segmentos anteriores) + truncate_offset`. O primeiro range começa
exatamente nesse par, cada range termina no EOF de seu segmento, todos os segmentos seguintes até
`physical_tail_segment_number` aparecem com start zero e nenhum segmento/range intermediário falta.
O último range termina em `physical_tail_segment_length`; `last_valid_segment_number` serve apenas de
boundary authority e pode ser anterior ao corte. Isso forma exatamente o
sufixo global `[truncate_global_offset,pre_tail_logical_length)` sem misturar unidades. O offset local respeita
`required_alignment`, aponta para boundary decodificada, `discarded_suffix_length` é exatamente
`pre_tail_logical_length-truncate_global_offset` e também iguala a soma
checked dos ranges, seu SHA cobre exatamente os bytes concatenados desses ranges e
`tail_mutation_composite_plan_sha256` referencia o composite integral acima. `REFUSE`
exige reason 1..7, `locator_kind=NONE`, nenhum range e os mesmos campos de mutação zerados; uma
`RecoveryApplyAuthority` não pode ser emitida para ele. Combinação cruzada, ZERO32 onde obrigatório,
range fora de segmento ou KEEP com lixo recusam no decoder.

Um sufixo incomplete só é truncável quando o scanner prova que ele começa em uma boundary de
transação **depois de todo último terminal completo/no-effect record**, ocupa o final físico do WAL e
contém zero unknown REQUIRED. `truncate_after_lsn` é o LSN imediatamente anterior ao primeiro
BEGIN/effect do sufixo; fragmento parcial começa na última boundary validada. Incomplete interior,
transação interleaved, terminal posterior, gap de segmento, I/O inconclusivo ou REQUIRED desconhecido
produzem `REFUSE_INCONCLUSIVE` com reason exato e recusam recovery.

Na segunda passagem, depois de reaplicar e certificar todos os COMMITs completos até o frontier H,
mas antes de publicar H, recovery revalida o `TailMutationExecutionV1` PLANNED. Com pending NONE,
publica/certifica a primeira aresta da generation chain já gravando HELD_OPEN/g e obtém exclusivamente
o permit QUARANTINE_REPAIR; com HELD_OPEN, recusa novo CAS/draft e deriva somente o continuation permit
fechado do slot e do COW proof. Com o permit permitido por uma dessas duas formas
preserva cada range bruto pelo protocolo fechado de quarantine,
faz barrier+cold hash do artefato e publica no forensic ledger um evento idempotente derivado de
`(database_uuid, composite_identity, tail_fingerprint, truncate_after_lsn, ranges_sha256)`. O ledger faz sua barrier e
direct verify; evento já existente só é aceito se todos os bytes/digests coincidirem. Falha antes do
corte deixa WAL intacto e pode repetir preserve/ledger sem duplicar significado.

Só então recovery recertifica **antes de agir** file identities, comprimentos, todos os raw hashes,
locator/alignment, ranges e a ledger predecessor boundary contra o `TailDispositionV1`; qualquer preimage
drift libera COMMIT/guard/lease e reinicia pelo budget original sem chamar truncate. Com a preimage ou
um prefixo monotônico certificado idêntico, revalida tail/plan/quarantine/ledger, exige PRESERVED ou
TRUNCATE_IN_PROGRESS, publica/certifica a stable edge inicial ou uma auxiliary edge fresh de resume e
obtém um **novo** permit exact-type TRUNCATE_SUFFIX. Somente esse permit chama
`_execute_suffix_cut_progress`, que conserva a semântica de `truncate_after_lsn` mas executa o plan
stepwise newest-first/COW. Depois de cada recycle, temp-create, write-prefix, data barrier, rename,
source release e parent barrier há uma fronteira de crash coberta pela progress proof. O cold scan
precisa terminar exatamente na boundary planejada, sem incomplete/unknown required; só então
`commit.state` pode avançar para H (ou permanecer em P se não havia novo COMMIT).

Kill após syscall e antes/durante barrier gera proof kind 2/3/4, nunca volta genericamente a
PRESERVED: resume adota o prefixo exato, repete barriers incertos e chama somente steps ainda não
target-exact. Target terminal emite continuation permit e não repete unlink/rename/release; source
integral começa em zero; apenas state fora da cadeia recusa. Depois do último barrier e antes da
publication, event+progress proof deriva TRUNCATED e reaplica/publica H. Nenhum permit cruza phase e
cada burn stable/auxiliary aparece na successor chain. Recovery
nunca sintetiza ABORT nem descarta um COMMIT.
Transações ABORTED completas e segment headers válidos podem permanecer depois de H porque a proof as
classifica como no-effect; elas não fazem parte do sufixo truncado.

O apply cobre todos os index effects sob EXCLUSIVE até write-back/barriers/direct cert e publication.
Depois de commit.state `COMMIT_RECOVERY_ADVANCE` final exato, **ainda sob o mesmo fresh lease+
EXCLUSIVE+COMMIT**, recovery revalida a edge reservada `database_runtime_generation r→r+1`, publica o
slot successor e faz visibility barrier/direct cert; tickets/ranges/handles de r ficam revogados e
nenhuma operação normal é liberada. Sai de COMMIT, libera guards, lease, claim e participant. Sob o
mesmo deadline/budget, um participant separado usa admission `FRONTIER_SCAN_ONLY`, instala action15
kind4, escaneia fora de COMMIT, sela sob um
segundo COMMIT breve e executa action17 em toda saída; não executa action16. Então cria
`WalFrontierCertification(FULL_SCAN)` + `RuntimeDeviceReplayProof(REPLAY)` bound a r+1 e publica o
estado normal/métricas. A proof REPLAY embute o `CommitRedoResultV1` da recertificação combinada
Q→H da seção 10.5, nunca apenas o delta gap P→H; source é Q e predecessor runtime SHA é ZERO.
Crash entre commit.state e
runtime slot é retomado pelo adoption gate acima; foreign writer em r nunca passa. Release incerto
mantém recovery-required; não reativa grant nem permite reader.

No SHA-base, `domain/recovery/decision.py:48-54` nem inclui control records nos efeitos e
`engine/recovery_manager.py:1350-1478` opera sobre replay achatado. Essas portas devem migrar no
slice 2/3 antes de qualquer WAL v2 existir.

### 10.4 Checkpoint

`TransactionManager.checkpoint` em `engine/txn_manager.py:996-1069` entra em COMMIT, faz redo até o
published e chama `BufferPool.checkpoint()` em `:1043`. Isso é uma operação explicitamente global:
ela pode e deve drenar todos os frames dirty, ordenar writes e executar a barreira requerida. V4
estava amplo demais ao exigir exact-write do próprio checkpoint; V7 restringe exact-write a
commit/recovery/index apply e preserva o flush global explícito do checkpoint.

V7 divide também checkpoint em certify/revalidate. Um COMMIT inicial certification-only exige
`WalFrontierCertification` + `RuntimeDeviceReplayProof`, sem reader público, enumera história e
identities **somente pelo catálogo direto**, congela um
`CheckpointPreparation` com proof/catalog/index-set digests,
`RequestedCheckpointBoundaryProofV1`, `ReaderRegistrySnapshot`,
`PendingOuterOperationMap`/map SHA, `active_checkpoint_ceiling`, active/terminal/release projections,
direct marker/`CUTOVER_PHASE_RETENTION_PIN` proof,
`effective_recycle_ceiling=min(target_Q,cutover_phase_ceiling quando presente)` e o `target_Q` canônico
limitado por ambos os floors, e sai. Essa passagem não lê page/header
de índice, não cria `CheckpointIndexManifest` definitivo e não contorna SHARED.

Se há NONTERMINAL sem SYSTEM floor, essa preparation primeiro congela detached o
`SystemFloorInstallBatchPlanV1`/entry bases, transition plan code 13 e attempt
SuccessorSet/admission **antes** de CAS,
toma fresh lease+EXCLUSIVE+COMMIT, recertifica pending/source map e executa somente action24. Libera
tudo depois do direct cert. Esse substep conservador não instala reader floor nem lê/muta page0;
failure deixa zero ou um floor target reconstruível e retry o adota. Portanto não existe janela
action24→“manifest ainda não calculável” sem source authority.

Com todos os floors SYSTEM já current, uma planning pass toma `INDEX_VIEW(SHARED)*` em ordem, sem
writer lease nem COMMIT retido, lê cada page0 diretamente e forma os targets, o
`CheckpointIndexManifestV1` integral, `CheckpointCompositeSemanticBaseV1`, stable transition plan
code 2 e attempt SuccessorSet. Libera os SHARED sem efeito. Só então instala GenerationAdmission,
toma fresh writer lease, `INDEX_VIEW(EXCLUSIVE)*` e COMMIT na ordem global, recertifica meta/C,
checkpoint/published/tail/history/catalog/index set, boundary, registry, pending/system maps,
cutover pin e **cada page0 source** bit-identical, instala o PROCESS
`READER_FLOOR_PIN(target_Q)` por action12 e revalida plan/admission imediatamente antes do primeiro
efeito. Drift termina floor por action13, libera tudo e reinicia pelo budget; não reescreve plan. Só
essa mutation pass executa os passos abaixo. Assim checkpoint nunca lê header sem guard, nunca executa
action24 sem plan, nunca adquire lease enquanto possui o primeiro COMMIT e nunca usa retained lease.

Antes de publicar `checkpoint_lsn >= T` ou reciclar prefixo, checkpoint exige:

1. grouped history íntegro e redo exato até o horizon;
2. relevant horizons calculados; `CheckpointIndexManifest` bijetivo; cada índice clean é
   certificado/avançado exatamente até o novo checkpoint sob EXCLUSIVE. Identity
   `NONTERMINAL` no `PendingOuterOperationMap` é persistida STALE com header/built/reconciled
   inalterados e tuple pending bit-identical, sem CLEAR/advance. Identity
   `TERMINAL_PENDING_FLOOR_RELEASE` exige CLEAR/BOUND já materializado, header final CLEAN e hashes/
   horizons bit-identical ao terminal manifest; ela é incorporada no terminal retention/release set,
   não rebaixada a STALE. Qualquer stale/incompleto fora do caso NONTERMINAL fechado recusa;
3. whole-pool checkpoint concluído;
4. data barrier dos arquivos drenados;
5. direct heap0 cert para v2 (T/generation/F/fingerprint/LSN) e direct fresh cert de todos os index
   headers;
6. encoding/publicação/barrier/cold direct cert do novo `CheckpointIndexStateV1` no slot alternado,
   preservando o slot que casa com o Q ainda publicado;
7. publicação atômica de `commit.state.checkpoint_lsn=Q` e direct read conjunto dos dois estados;
8. revalidação do reader floor, system floors e três projeções. Com active pending, Q não passa do
   predecessor COMMIT completo e portanto BEGIN..MARK COMMIT/body permanecem integrais. Terminal não
   limita Q, mas seu system floor mantém MARK..CLEAR até action14;
9. se existe `CUTOVER_PHASE_RETENTION_PIN`, limitar recycle ao
   `effective_recycle_ceiling`, emitir `DeferredCutoverRecycleV1` e **não** liberar o phase pin; o
   marker sucessor precisa ser publicado/direct-certified fora desta passagem;
10. se `terminal_release_set` é nonempty, emitir `DeferredTerminalFloorReleaseV1` e não reciclar esse
    prefixo: action13 e as N action14 sequenciais publicam/direct-certificam a remoção das keys; só uma
    passagem posterior, depois do `TerminalFloorReleasePendingGateV1`, pode formar
    `WalMutationDraftV1(RECYCLE_PREFIX)`. Se o release set é vazio e não há cutover pin, ou em passagem
    posterior que já revalidou marker/system-map successor, remover somente o prefixo autorizado e
    liberar explicitamente os pins. CLEAR elimina o **active** ceiling assim que vira terminal; map/
    retention floor só desaparecem após checkpoint incorporation + action14, nunca por exigir mapa
    vazio antes da ação que o produz.

O prefixo reciclável termina estritamente antes/na boundary autorizada pelo checkpoint Q e nunca
viola um `WAL_SCAN_PIN`, reader registration ou floor pin. Qualquer tentativa de escolher Q acima do
oldest active snapshot, ou de reconciliar/remover entry cuja visibility upper bound seja maior/igual
ao cutoff, é malformed pre-WAL. O floor pin é liberado somente depois de publication/recycle ou de
rollback integral do attempt.

Falha em qualquer etapa não avança checkpoint nem autoriza reciclagem. Dirty unrelated é drenado
por design nesta porta, diferentemente de commit/recovery. Depois da publicação/reciclagem, sai de
COMMIT, libera EXCLUSIVE em ordem inversa e libera lease confirmado; callback/métrica ocorre depois.
Checkpoint não revoga ACTIVE nem persiste seu remainder, conforme seção 6.6.
Se target Q é igual ao checkpoint já publicado, a operação é no-op certificado e não cria segunda
generation/slot com o mesmo Q.

Publicar um checkpoint que avança Q invalida imediatamente, por mismatch de
`checkpoint_state_sha256/checkpoint_lsn`, todo par frontier/device usado para **novas** operações; os
tokens históricos de readers já registrados em S permanecem apenas para esses readers. Antes de
retornar sucesso normal, o owner sai de COMMIT/guards/lease/claim, mantém o mesmo envelope/deadline e
abre um participant separado com admission `FRONTIER_SCAN_ONLY`: action15 kind4, scan físico desde o
novo Q até P fora de COMMIT e precomputa todas as preimages/hashes grandes fora de sections. No segundo
COMMIT breve, seal/revalidation bit-identical finaliza a
`WalFrontierCertificationV1(FULL_SCAN,new_Q,P)` fresh e combina somente seus fixed digests, o
`CheckpointDeviceApplyManifestV1`/checkpoint state novos e a
`RuntimeDeviceReplayProofV1` predecessora live para emitir os 500 bytes de
`RuntimeDeviceReplayProofV1(CHECKPOINT_REBASE)`. Então action17 termina a pin em toda saída, sai de
COMMIT/participant e abre o gate local somente se ambas as proofs frozen foram produzidas **e** a
transition terminal action17 foi confirmada; release incerto revoga ambas. Nenhum raw
scan, hash de manifest, clock ou allocation proporcional a target count ocorre sob COMMIT. Action16 e
wrapper público são proibidos nesta rota.

Antes de action15, a admission externa recertifica headroom para install+terminate e se recusa com
zero registration se ambos não cabem. O seed/seal exige o `BoundaryProofV1` terminal/baseline que
publicou new Q, os mesmos active ceilings/system floors/pending/release gates, range `(new_Q,P]`
integral ainda retido e o prefix-recycle result do manifest direct-valid. O scan pin nasce antes de
ler o primeiro byte dessa range e impede recycle concorrente. Extensão no ACTIVE tail que não muda P,
ou outro drift somente de seal, termina action17 e repete o scan. Se outro processo já concluiu seu
rebind e publicou um grouped commit, `P'>P` torna a predecessor local insuficiente: action17 termina,
o rebind é abandonado e a porta write-capable entra no catch-up/replay exato `new_Q→P'` pelo mesmo
budget/deadline; read-only recusa typed. Novo Q/checkpoint, floor/map change, candidate/control adoption
pendente ou inventory/history incompatível segue o gate current apropriado e nunca é tratado como
simples retry do rebase. Logo rebase não esconde checkpoint no meio de grouped transaction, reader
floor violado, tail já perdido ou liability sem slot terminal.

Entre a publication do checkpoint e o rebase local, `LiveCheckpointProofRebindGateV1` bloqueia begin,
append, DDL, maintenance, próximo checkpoint e reconcile naquele processo; em outro processo o
mismatch Q também impede que sua proof antiga autorize uma operação. Um processo que possua a mesma
predecessora live pode executar o mesmo rebind; sem ela segue o cold gate. Drift de P/WAL/inventory
durante o scan consome somente a tentativa PROCESS e reinicia pelo budget original, sem desfazer nem
renumerar o checkpoint já durável. Exhaustion depois da publication retorna
`CHECKPOINT_PUBLISHED_PROOF_REBIND_REQUIRED`, preserva o checkpoint e mantém operações normais
bloqueadas até rebind/cold replay. Crash em qualquer ponto simplesmente perde as proofs process-local:
reopen usa CHECKPOINT_DIRECT se P==Q ou REPLAY integral Q→P; nunca adota um rebase incompleto.

O primeiro rebind recebe o mesmo `OperationBudget` envelope e o mesmo `OperationAttempt` que publicou
o checkpoint; helper/participant não cria attempt nem reseta deadline. Se o seal driftar, somente o
outer loop cria `OperationAttempt(n+1)` e entra em continuation mode
`CHECKPOINT_PROOF_REBIND_ONLY`, bound ao raw SHA/generation/Q do checkpoint já publicado. Esse mode não
forma candidate, não chama whole-pool checkpoint, action12/13/14/24, commit.state ou recycle de novo;
apenas action15→scan→seal→action17→rebase. Ele pode tail-call somente o recovery/catch-up já fechado
quando a recertificação demonstra `P'>P` ou gap, depois de liberar sua pin/sections; não pode continuar
com predecessor curta nem criar outro checkpoint. Max attempts/deadline/cancel seguem a precedence
universal, sem renovar admission/budget nem desfazer a publication durável.

### 10.5 Cold-open device materialization gate — SUBSTITUI V6

`commit.state` e WAL podem sobreviver a power loss enquanto pages ordinárias não: commit normal faz
apply antes de publicar, mas não data barrier. Portanto `WalFrontierCertification(P,Q,...)` não é
`DeviceMaterializationProof`. Igualdade de frontier/P só prova quais efeitos são autoritativos; não
prova que heap/catalog/index/overflow/pages no device chegaram a P.

Uma runtime é **cold** no primeiro open de `(database_uuid, process_birth_identity,
database_runtime_generation)`, depois de fork, owner-death/recovery, pool rebuild ou perda/poison de
uma proof anterior. Antes de expor `Database`, iniciar background worker, callback ou permitir
`TransactionManager.begin`, a porta interna `ColdOpenDeviceGate` classifica o bootstrap/fence e
produz exatamente uma destas saídas:

- `LEGACY_UNFENCED`: somente a semântica compatible M1 da seção 3.1.1, sem proof V7;
- `V7_READ_ONLY_CERTIFIED`: exige **`P == Q`**, marker/slot BOUND, checkpoint control state direto e
  todos os heap/index headers exigidos pelo manifest diretamente certificados; zero replay/write;
- `V7_WRITE_CAPABLE_CERTIFIED`: `P == Q` usa a mesma certificação; `P > Q` exige o catch-up abaixo;
- `V7_MAINTENANCE_ONLY`: PREPARED/FENCED ou binding-required cria apenas um contexto interno sob a
  `OfflineMaintenanceSession`, com replay/proof limitado à próxima transição fechada; nenhuma API
  Database/query/generic writer é exposta;
- `REFUSED/CORRUPT/INCONCLUSIVE`: nenhum handle público é criado.

Nas duas saídas `P==Q`, cold-open primeiro produz/revalida
`WalFrontierCertificationV1(FULL_SCAN)` a partir do baseline+tail direto usando admission
`FRONTIER_SCAN_ONLY`, action15 kind4 e action17 em toda saída; nunca action16. Só depois produz
`RuntimeDeviceReplayProofV1(CHECKPOINT_DIRECT)` que referencia seu SHA; essa construção não executa
lifecycle adicional e cold-open não emite `WalPublicationProofV1`. O wrapper pertence ao begin
posterior. Genesis `P==Q==0` segue a
mesma ordem com boundary BASELINE e result bytes vazios. Nenhum fixed point ou placeholder é usado.

Read-only jamais usa a exceção “bytes parecem aplicados”: se `P > Q`, inclusive apenas o fence C ou
uma única transação ordinária, recusa
`GrafxUnsupportedOperation(field="read_only_consistency", reason="device_replay_required")`.
Checkpoint Q é a única base cuja data barrier e direct certification sobrevivem a cold restart. Uma
emenda futura pode introduzir sidecar durável equivalente, mas V7 não o infere de page LSN, OS cache,
mtime, header isolado ou sucesso de uma execução anterior.

Write-capable com `P > Q` executa `ColdReplayPlan` mesmo quando a frontier certification já está perfeita:

1. participant + sandwich COMMIT/WAL-scan congela `WalFrontierCertification`, checkpoint slot/root,
   catálogo baseline, todas as `CommittedTransaction` em `(Q,P]`, final file inventory e o conjunto
   bijetivo de page/header/catalog/directory effects. O plan contém target final e fingerprint de cada
   page/address/path, create/drop/allocate/truncate topology, manifest scopes e `IndexFencePlan`;
2. sai de COMMIT e de toda pin de page/frame, adquire writer lease **fresh**, opcional
   `INDEX_NAMESPACE_CATALOG(EXCLUSIVE)` e todos os `INDEX_VIEW(EXCLUSIVE)` do plan em ordem;
3. reentra em COMMIT, repete o seal do WAL e revalida bit a bit meta/marker/slots/commit.state,
   catálogo, file identities/sizes, history root, target P e carrier generations. Drift libera tudo e
   reinicia pre-mutation pelo mesmo `OperationBudget`;
4. antes do primeiro device byte, aplica `checked_increment_u64` à
   `device_replay_generation` no carrier `WAL_COORDINATION`, revoga proofs/authorities da runtime
   anterior e executa full preflight. Depois reaplica **todos** os efeitos agrupados `(Q,P]` em ordem;
   APPLIED/IDENTICAL/SUPERSEDED só são aceitos pela mesma prova de imagem posterior cometida, nunca
   por `page_lsn >= target` ou máximo heurístico;
5. faz write-back exato de every touched page, data/directory barriers de todos os devices/paths
   tocados e cold direct cert de file inventory, raw final pages, heap/catalog headers, IndexHeaderV2,
   catalog/history projection e carrier state. Dirty unrelated nunca é flushed por esta porta;
6. ainda sob COMMIT, congela `RuntimeDeviceReplayProof` abaixo, sai, libera guards/namespace/lease e
   participant, e só então publica o handle/métricas. Falha ou release incerto invalida a proof e
   mantém recovery-required.

```text
RuntimeDeviceReplayProofV1 = (
  magic=MAGIC_RUNTIME_DEVICE_REPLAY_PROOF_V1, version u16=1, reserved_zero u16, record_length u32,
  database_uuid[16], process_birth_identity[16], database_runtime_generation u64,
  device_replay_generation u64, wal_cutover_lsn u64,
  wal_feature_fence_generation u64, wal_feature_bits u64,
  materialization_kind u8, reserved_zero[3], commit_redo_result_length u32,
  bootstrap_marker_sha256[32], checkpoint_state_sha256[32],
  checkpoint_lsn u64, materialized_through_lsn u64,
  wal_frontier_certification_sha256[32], committed_history_root[32],
  predecessor_runtime_device_replay_proof_sha256[32],
  final_file_inventory_sha256[32], device_apply_target_set_sha256[32],
  commit_redo_result_sha256[32],
  heap0_expected_page_lsn u64, heap0_floor_f u64, heap0_transition_lsn_t u64,
  heap0_generation u64, heap0_raw_sha256[32], heap0_semantic_sha256[32],
  final_catalog_heap_index_headers_sha256[32],
  commit_redo_result_bytes[commit_redo_result_length], crc32c u32
)
runtime_device_replay_proof_sha256 = SHA256(DOMAIN_RUNTIME_DEVICE_REPLAY_PROOF_V1 || canonical proof bytes)
record_length = 500 + commit_redo_result_length
```

O validation bundle inclui o `WalFrontierEvidenceEnvelopeV1` integral e, para kinds 1/4, o
`CheckpointDirectProofV1`; seus projected frontier/checkpoint SHAs precisam casar. Os três receipts
finais são os codecs genéricos fechados da seção 7: `final_file_inventory_sha256` kind 11,
`heap0_semantic_sha256` kind 13 e `final_catalog_heap_index_headers_sha256` kind 12. Eles são
reconstruídos de `CommitRedoResult`/checkpoint target set + direct postimages e não podem ser
preenchidos por tuple host/cache. Bundle/receipt ausente, locator unreadable ou digest opaco recusa a
proof; nenhum desses companions entra na preimage da frontier predecessora.

`RuntimeDeviceMaterializationKindV1` é `INVALID=0`, `CHECKPOINT_DIRECT=1`, `REPLAY=2`,
`OWN_COMMIT=3`, `CHECKPOINT_REBASE=4`; 5..255 recusam. `CHECKPOINT_DIRECT` exige `P==Q`, result length 0 e result SHA ZERO;
seu target-set SHA é exatamente `CheckpointDeviceApplyManifestV1.target_set_sha256` do checkpoint
diretamente certificado (ZERO somente quando target_count=0) e predecessor runtime SHA ZERO. `REPLAY` também exige predecessor runtime SHA
ZERO: sua source authority é o checkpoint state/Q integral no `ColdReplayPlan`, não uma proof local
que poderia ter morrido no crash. `OWN_COMMIT` exige predecessor runtime SHA nonzero, preimage integral
live com `materialized_through_lsn==CommitRedoResult.source_materialized_lsn`, mesma runtime/fence/Q e
frontier predecessor exato. `REPLAY` e `OWN_COMMIT`
exigem result length 60..67108864, nested bytes que decodificam como `CommitRedoResultV1` da seção
8.1, digest nonzero bit-identical ao field, source/target iguais à predecessor materializada/P e target
set nonzero igual à projection `DOMAIN_RUNTIME_DEVICE_TARGET_SET_V1` e ao plan executado. `OWN_COMMIT` exige ainda que o frontier producer seja
`DERIVED_OWN_COMMIT` e que seu producer evidence carregue esse mesmo result SHA; `REPLAY` exige
frontier `FULL_SCAN` current depois da publicação/recovery. Kind/result zero rules cruzados,
truncation/trailing bytes ou result que omita touched target/barrier recusam.

| Kind | predecessor runtime | `CommitRedoResult` | horizons/producer obrigatórios |
|---|---|---|---|
| `CHECKPOINT_DIRECT=1` | ZERO | length 0/SHA ZERO | cold `P=Q`; frontier FULL_SCAN; checkpoint target set |
| `REPLAY=2` | ZERO | integral/nonzero | `Q<P`, result Q→P; frontier FULL_SCAN; runtime target projection |
| `OWN_COMMIT=3` | integral/current | integral/nonzero | source predecessor Pold, target `P>Pold`, Q igual; frontier DERIVED_OWN_COMMIT |
| `CHECKPOINT_REBASE=4` | integral/rebind-only | length 0/SHA ZERO | `old_Q<new_Q<=P` com P igual; frontier FULL_SCAN nova; checkpoint target set novo |

`CHECKPOINT_REBASE` é a única otimização live para a publicação de um checkpoint novo enquanto o
device já está materializado em `P`. Exige predecessor runtime proof integral/nonzero, retida em state
module-private `REBIND_PREDECESSOR_ONLY` no mesmo
process birth/runtime/device-replay generation, bootstrap/fence e `materialized_through_lsn=P`; exige
`old_Q < new_Q <= P`, result length 0/result SHA ZERO e uma frontier **nova** `FULL_SCAN` em
`(new_Q,P]`. O manifest do checkpoint novo precisa declarar exatamente
`source_q=old_Q,target_q=new_Q,published_p=P`; seu history root de origem casa ao frontier predecessor,
e o verifier deriva bijetivamente o suffix history root `(new_Q,P]` que aparece na frontier nova.
Target-set SHA é exatamente o `CheckpointDeviceApplyManifestV1.target_set_sha256` novo, e cada target,
barrier, final inventory/header e checkpoint state é direct-certified depois do whole-pool flush. A
preimage dessa alegação é a `DeviceApplyTargetSetAuthorityV1(DURABLE_DIRECT)` integral embutida no
control core do CHECKPOINT_CANDIDATE, com exact direct-result set obtido após os barriers e antes da
publication de commit.state; o rebase redecodifica essa authority pelo checkpoint-state payload, não
aceita um cache booleano ou SHA solto. Seu final seal exige iguais o `device_replay_generation`, cada
index mutation generation e file identity que protegiam a direct target certification. Writer lease/
attempt não é stable; WAL/system-floor carrier só pode diferir pelas arestas exactas de
recycle/action14 já embutidas no mesmo checkpoint manifest/recipe e direct-adopted na ordem. A
action13 PROCESS é validada separadamente por sua transition proof terminal. Qualquer outra diferença
recusa. A
proof predecessor e o manifest integral são inputs obrigatórios do validator; seus SHAs isolados não
bastam. O rebase não incrementa `device_replay_generation`, não contém redo, não serve em cold-open e
não converte control/WAL authority em materialização: ele conserva a materialização P já autenticada
pela predecessor e liga a ela o flush/barriers/direct cert do checkpoint sucessor.

`REBIND_PREDECESSOR_ONLY` não é proof current, enum persistido, snapshot authority nem permissão para
begin/mutator. Ela nasce somente quando um checkpoint successor direct-valid prova
`source_q=proof.checkpoint_lsn`, mesmo P/runtime/device generation e o gate fecha antes de expor o novo
Q; retém os bytes frozen até sucesso/refusal do rebind. Outro Q drift, P diferente naquele instante,
fork/close/poison/generation change ou perda do enclosing checkpoint plan destrói essa retenção. Crash
perde-a e portanto força a rota cold. Nenhuma API aceita esse state onde exige
`RuntimeDeviceReplayProof` current.

`CHECKPOINT_DIRECT` é canônico somente em cold `P==Q` e predecessor ZERO. Em checkpoint live que
avança Q, inclusive quando `new_Q==P`, usa-se `CHECKPOINT_REBASE`; misturar predecessor ZERO/nonzero,
reutilizar o frontier antigo/Q antigo, aceitar `old_Q==new_Q`, omitir um target do manifest, mudar
device generation ou inserir `CommitRedoResultV1` recusa. Se não há predecessor live bit-identical,
`P==Q` pode voltar pelo cold direct gate, enquanto `P>Q` exige `REPLAY` integral Q→P; nunca se fabrica
rebase a partir de checkpoint/control state.
Todas as somas são checked antes de allocation e `record_length<=67109364`; golden records length 500,
560 e máximo, truncation ±1 e nested length divergente são parte do byte gate.

O nome curto `RuntimeDeviceReplayProof` é alias desse codec. A proof é frozen, process-local e não serializada como substituto de checkpoint; fork/close/poison,
mudança de database/device replay generation, fence, inventory não-MVCC ou history
que não seja extensão committed a revoga. Mudança válida de Q/P apenas a torna **não current** para
novo begin/mutator. Há exatamente duas retenções estreitas dos mesmos bytes imutáveis: (a) o
`REBIND_PREDECESSOR_ONLY` acima, recusado por toda API normal, e (b) a cópia já ligada a um
`WalPublicationProof`/reader key live, válida somente para aquele snapshot/floor histórico. Avançar current P por grouped commits válidos **não** apaga
o objeto histórico P de uma `WalPublicationProof`/reader key já live: a reader floor conserva as
versions/tombstones necessárias e impede reconcile/recycle incompatível; checkpoint só pode subsumir
o prefixo até um Q terminal `<=S`, depois de seus barriers/direct cert. O WAL anterior a esse Q pode
ser reciclado e não é falsamente prometido pela key. Em cada statement posterior,
`HistoricalReaderContinuationGate` relê own-key, runtime generation e checkpoint atual, exige
`Q<=S`, exact checkpoint subsumption, current history como extensão committed e nenhum reconcile
que remova versão com upper bound `>=S` (`reconciled_through_lsn<=S`); ele não atualiza S nem
transforma a proof antiga em current. Q>S, prefixo removido sem
checkpoint authority ou falta de version floor revoga/poison. Todas as pages/entries são
snapshot-aware. Esse objeto fica bound àquele wrapper/S e jamais
serve a novo begin ou mutator, que exigem frontier/device current no novo P. Recovery generation,
non-MVCC DDL incompatible, poison ou own-key termination o revogam. Um commit
local só estende `materialized_through_lsn` depois de exact apply, `CommitRedoResultV1`, publicação
bem-sucedida e construção do frontier own-commit predecessor. Se outro
processo elevar P, o próximo begin write-capable executa o mesmo catch-up/revalidate antes do snapshot;
read-only recusa até checkpoint tornar `P == Q`. Assim custo/conservadorismo multiprocess são
explícitos, não uma janela de false-clean.

Se há COMMIT completo acima do P inicial, recovery combina `RecoveryApplyPlan` e `ColdReplayPlan`: a
base continua Q, reaplica/certifica todos os efeitos até o novo H, faz as barriers acima, só então
publica H, executa um FULL_SCAN sem reader lifecycle, emite `WalFrontierCertificationV1(H)` e depois
proof com `materialized_through_lsn=H`. Um recovery live que já possuía proof ainda
revoga/incrementa generation. Sufixo incomplete segue quarantine/truncate; unknown REQUIRED continua
bloqueador. Nenhuma publicação de gap ocorre antes da materialização cold integral.

### 10.6 Read-only

O caminho atual em `engine/recovery_manager.py:590-663` já prova consistência sob COMMIT e, se
incompleto, lança `GrafxUnsupportedOperation(field="read_only_consistency")` em `:650-663`.
V7 preserva essa taxonomia pública, em vez de inventar `GrafxRecoveryRefused`.

Read-only cold-open só aceita quando header/checkpoint/tail/fase são auto-consistentes, existe
`WalFrontierCertification` no published, **`P == Q`** e uma `RuntimeDeviceReplayProof` derivada sem escrita
do checkpoint BOUND diretamente certificado; não pode haver incomplete effects, transition pendente,
floor pendente, gap, bootstrap parcial ou dano.
Isso autoriza o handle frio, não um read-view: cada begin ainda executa action15→16 e produz seu
`WalPublicationProofV1` wrapper fresh antes de expor Snapshot(P).
Também lê index headers diretamente e exige stale/relevant-horizon consistentes; nunca aceita cache
porque o published token permaneceu igual. Caso contrário recusa typed e byte-identical. Um harness captura bytes, nomes, tamanhos, mtimes,
write/allocate/truncate/recycle/barrier calls antes/depois tanto do sucesso quanto da recusa. Locks e
registrations efêmeras do coordinator são permitidos; nenhum byte/mtime do database `StorageDevice`
ou control file muda.

### 10.7 Ordem operacional fechada

| Fluxo | Passagem certificadora | Passagem mutadora |
|---|---|---|
| cold open V7, `P == Q` | participant → frontier certification + direct checkpoint/device headers → exit | nenhuma; em RO zero database write |
| cold/live catch-up write-capable, `P > Q` | participant → COMMIT sandwich → `ColdReplayPlan` → exit COMMIT | GENERATION_ADMISSION → fresh LEASE → optional NAMESPACE → EXCLUSIVE* → COMMIT revalidate/replay/barriers/cert/proof → exit → release claim/participant |
| begin sem gap/device drift | participant → COMMIT → ambas proofs + reader pin → exit | nenhuma |
| begin write-capable com gap | participant → COMMIT → `RecoveryApplyPlan` → exit COMMIT | GENERATION_ADMISSION → fresh LEASE → EXCLUSIVE* → COMMIT revalidate/apply/publish → exit → release claim/participant |
| ordinary Transaction commit/DDL/index maintenance | detached preflight → participant → GENERATION_ADMISSION → fresh LEASE → EXCLUSIVE* → COMMIT → WriteSnapshotProof(current P) | mesmo COMMIT freeze/append/apply/successor; proof stale libera tudo/claim para catch-up ou abort+recovery, nunca salta intervalo |
| recovery explícita | participant → COMMIT → plan/history fingerprint → exit COMMIT | GENERATION_ADMISSION → fresh LEASE → EXCLUSIVE* → COMMIT revalidate/apply/barriers/direct cert/publish/device proof → exit → release claim/participant |
| checkpoint | participant → COMMIT → proofs + preparation → exit COMMIT | GENERATION_ADMISSION → fresh LEASE → EXCLUSIVE(all) → COMMIT revalidate/manifest/redo/flush/cert/publish/recycle → exit/release → participant fresh + action15 kind4 → scan new Q→P fora de COMMIT → seal + action17 → CHECKPOINT_REBASE; operation gate permanece fechado até a proof |
| reservation | participant → GENERATION_ADMISSION → fresh LEASE → COMMIT proof/revalidate/append/apply/publish → exit | release lease/claim confirmado → activation CAS fora das sections |
| repair/upgrade/cutover | participant → COMMIT → full detached proof → exit COMMIT | GENERATION_ADMISSION → fresh LEASE → EXCLUSIVE* quando houver índice → COMMIT revalidate/apply → exit → release claim/participant |

`EXCLUSIVE*` é o conjunto canônico, vazio somente quando o manifest/proof demonstra zero index
identity/effect. Em nenhuma linha se adquire lease/guard dentro de COMMIT; em nenhuma passagem
certificadora há page/frame pin, graph guard, host callback, clock ou métrica; `WAL_SCAN_PIN` detached
é a única registration provisória permitida entre os dois COMMITs. A release order é sempre COMMIT,
guards/NAMESPACE inversos, lease, GENERATION_ADMISSION claim e participant; activation/telemetria vêm
depois.

## 11. Sections, leases, PID/fork e ordem de locks

### 11.1 Ordem única

Os únicos caminhos autorizados são:

```text
PID -> participant -> COMMIT(certification-only) -> exit COMMIT
    -> install GENERATION_ADMISSION claim -> acquire fresh LEASE
    -> optional INDEX_NAMESPACE_CATALOG(EXCLUSIVE)
    -> sorted INDEX_VIEW(EXCLUSIVE)*
    -> COMMIT(revalidate/apply) -> exit COMMIT
    -> release EXCLUSIVE* -> release NAMESPACE -> release LEASE
    -> release GENERATION_ADMISSION claim -> exit participant
PID -> participant -> install GENERATION_ADMISSION claim -> acquire fresh LEASE
    -> optional INDEX_NAMESPACE_CATALOG(EXCLUSIVE)
    -> sorted INDEX_VIEW(EXCLUSIVE)* -> COMMIT
    -> exit COMMIT -> release EXCLUSIVE* -> release NAMESPACE -> release LEASE
    -> release GENERATION_ADMISSION claim -> exit participant -> activation
PID -> participant -> sorted INDEX_VIEW(SHARED)*
    -> precheck/traversal/materialize/postcheck/context-release/result-detach
    -> zero pins/graph guards -> release SHARED* -> exit participant
```

`INDEX_VIEW(EXCLUSIVE)*` é vazio apenas quando manifest/proof demonstra zero índice, como reservation
pura. Readers nunca tentam lease ou COMMIT enquanto possuem SHARED; DDL toma old/new identities e
checkpoint toma todos em ordem canônica. Recovery/checkpoint saem da certification pass, adquirem
lease fresh e EXCLUSIVE, e só então reentram. A exceção histórica COMMIT→LEASE de retirement não pode
adquirir/mutar índice. Todo apply preflighta o conjunto e toma EXCLUSIVE antes de COMMIT. Nenhum
caminho adquire participant, manager lock ou digest menor enquanto já possui digest maior.
`INDEX_NAMESPACE_CATALOG(EXCLUSIVE)` só aparece antes dos guards per-index quando CREATE/DROP muda o
conjunto de carriers; nunca é adquirido depois de um `INDEX_VIEW` nem por reader ordinário. Release
é a ordem inversa. Missing carrier descoberto depois dessa fase recusa, não tenta repair in-place.

`GENERATION_ADMISSION` acima significa record/claim live, não lock interno retido: seu registry lock
lexical é solto antes de LEASE. Mesmo assim seu lifetime fica estritamente dentro do participant e
`close/drain` o inclui; unwind libera lease/guards antes do claim e o claim antes de participant.
Fluxo read-only/SHARED não instala claim.

O manager lock nunca fica retido **atravessando** acquire/release de participant ou lease, entrada/
saída de COMMIT, device, clock, metrics ou callback. Ele pode ser tomado brevemente já dentro de
participant apenas para revalidar ticket/close/generation, mas é liberado antes de tentar lease ou
COMMIT. Há somente três encontros breves e sem nesting com lease/COMMIT:

```text
PID -> manager lock -> install REFILLING -> unlock
participant -> manager lock -> revalidate ticket -> unlock -> GENERATION_ADMISSION
    -> LEASE -> COMMIT -> release COMMIT/LEASE/claim -> exit participant
manager lock -> activation CAS / consume / burn / revoke -> unlock -> metrics-callback
```

Status de identity manager usa apenas `PID -> manager lock`. Status agregado que lê header de índice
solta esse lock e usa o proof sandwich de 8.4.7: `PID -> participant -> COMMIT proof -> exit ->
sorted INDEX_VIEW(SHARED) -> direct header -> release -> COMMIT post-proof -> exit`; nunca aninha
manager lock com essas sections nem COMMIT com SHARED. Close faz PID-first, marca o Database terminal, entra em
participant, toma manager lock, revoga grant/ticket/handles, notifica conditions, libera o lock e sai.
Um refill que instalou ticket mas ainda esperava participant acorda depois do close, falha na
revalidação pre-lease e faz zero device call. Um waiter de condition libera o manager lock enquanto
dorme e reinicia no PID/generation check; spurious wakeup nunca consome nem cria segundo refill.

Esse close descreve revogação local. Se a composição pretende drenar dirty index frames, ela executa
antes um checkpoint/explicit-flush plan com fresh lease + EXCLUSIVE conforme seção 10; não chama
whole-file flush depois de revogar guards. Close que encontra dirty/pinned index sem esse plan recusa/
poison e nunca escreve silenciosamente.

Nunca: adquirir LEASE ou `INDEX_VIEW` segurando COMMIT; certificar repair/upgrade já com lease; fazer
upgrade SHARED→EXCLUSIVE; usar a exceção COMMIT→LEASE de retirement para index apply; manter page pin,
HNSW graph guard, clock, metric ou host callback sob COMMIT; chamar host code sob `INDEX_VIEW`; ativar
antes de release; manter lease entre operações identity.

O commit atual já segue participant, lease, COMMIT em `engine/txn_manager.py:1416-1432`, e sua ordem
é documentada em `:2678-2716`. Recovery possui uma exceção deliberada COMMIT→LEASE somente para
retirement em `engine/recovery_manager.py:843-858`; identity não a chama.

Fences mínimos por reserva/consumer: PID antes de tudo; lease validado após aquisição; novamente
após completion; antes de append; após WAL barrier antes de apply; antes de publication; release
confirmado antes de activation. Mudança de generation invalida plan, proof, permit e grant.

`retain_lease` existe em `engine/txn_manager.py:233-346` e defaulta false, mas o coordinator pode
retornar lease já retido (`adapters/coordination_local.py:917+`). Composição v2 deve recusar
`retain_lease=True` e a porta identity deve provar que recebeu guard fresh, nunca apenas confiar no
default.

### 11.2 PID como primeira instrução

Toda autoridade captura `origin_pid`: Database, coordinator, LeaseGuard, transaction/recovery/
identity managers, TransactionContext, permit, WalAppendPlan, grant, range handle, registry e
cleanup/close. O check de PID/generation é literalmente a primeira instrução antes de lock, clock,
metric, fd, I/O ou dispatch externo.

No SHA-base, `engine/coordination.py:83-220`, `LeaseGuard`, não possui PID; seu `release` marca
`_released` antes do release do coordinator em `:208-213`. `LocalProcessCoordinator`
(`adapters/coordination_local.py:765-842`) só inclui `os.getpid()` na string do owner em `:800`, o
que não constitui fence. Esses objetos precisam de poison permanente e release state que distingue
`RELEASING`, `RELEASED` e `UNCERTAIN`.

`os.register_at_fork(after_in_child=...)` apenas substitui registries/sections process-globais por
novos objetos; nunca adquire `_SHARED_SECTIONS_GUARD`, `_SHARED_SECTIONS` ou qualquer lock herdado.
Objetos herdados recusam imediatamente e child close nunca libera lease, reader ou range do pai.
Fresh `connect()` no filho cria registry/adapters próprios e funciona tanto em `file:` quanto
`:memory:`.

## 12. API, configuração, status e métricas

### 12.1 Superfície

Além das configs da seção 3 e dos budgets da seção 12.4, a API pública adiciona somente:

- uma porta operator-only `enable_wal_semantics_v2()` e, depois do fence, uma porta separada
  `upgrade_identity_leasing()`. Nenhum booleano `confirm_quiescent` prova quiescência; ambas exigem
  `OfflineMaintenanceSession` autenticada pelo coordinator, com database exclusivo e backup/runbook.
  A primeira só se torna pública no Gate 5; antes disso existe apenas no harness internal allowlisted.
  A segunda só se torna pública no enable atômico do Slice 7;
- campos detached/immutable em status: formato da identity, WAL semantics/C, formato heap, T,
  generation, F, leasing ativo, capacity, exhaustion, bootstrap phase/marker hash, P/Q,
  WAL frontier/public-reader-proof states, device materialized-through/runtime generation, oldest active snapshot,
  durable-gap state, index shared-stale/local-poison e recovery-required;
- métricas registradas na composição antes do primeiro uso.

Nenhum coordinator, store, manager, range handle, permit, callback ou container mutável é exposto
por public views. Configuração aceita tipos built-in exatos após canonicalização e persiste descriptor
compatível; v1 compatible não simula v2.

### 12.2 Semântica exata das métricas

- `identity_reservations_total{outcome}`: fecha um evento exatamente uma vez quando uma tentativa
  chega a outcome terminal (`committed`, `conflict`, `exhausted`, `failed_pre_wal`,
  `recovery_required`).
- `identity_ids_reserved_total`: fecha um evento `stop-start` no primeiro WAL barrier bem-sucedido,
  mesmo se o range nunca for ativado; o sink o recebe depois da saída dos locks, se o processo
  sobreviver.
- `identity_ids_handed_out_total`: evento com `delta=n` depois de `pos += n` e antes da exposição da
  tuple integral;
- `identity_ids_burned_total`: contabiliza apenas remainder local conhecido e descartado por refill,
  close/poison/release uncertainty ou nonactivation conhecida. Perda em hard crash é incognoscível e
  explicitamente excluída; não é inferida por scan.
- `identity_range_remaining`: gauge exato `0` ou `stop-pos` do único range ativo do manager.
- `identity_refill_seconds` e `identity_lease_wait_seconds`: histogramas observados fora de locks e
  sem executar sink hostil sob participant/COMMIT/manager lock.

“Exatamente uma vez” vale para a vida do processo, não é garantia de telemetria durável após hard
crash. Sob o lock, a operação apenas fecha um evento contábil built-in e avança o estado; depois de
liberar manager/participant/COMMIT/lease, entrega esse evento uma vez ao sink. Para handout, o lock
é liberado após `pos += n`; a métrica `delta=n` é emitida antes de qualquer elemento da tuple chegar
ao caller. Falha do sink preserva
o ID consumido e a exceção original. Callbacks de métrica nunca decidem correctness nem substituem
prova.

### 12.3 Claim de performance honesta

Reserva faz zero scan de data pages, catálogo completo, owners ou rows. Faz leituras/escritas de
controle e uma leitura direta/write-back de heap0. Entretanto, localizar/copiar/validar a identity
entry percorre slots da page0: CPU é `O(numero_de_slots_materializados)`, limitada pela capacidade
fixa do formato da page0. Portanto V7 **não** chama o caminho de O(1) computacional.

Critérios estruturais:

- zero data-page read/allocate/write no refill;
- número de device reads não cresce com rows/data pages;
- uma imagem heap0 e um control record por reservation;
- commits unitários sem refill não tocam heap0 por identidade;
- benchmark mede, não promete, p50/p90/p99, conflitos, barriers, refills e writes para size 1, 64 e
  multiprocess.

`INDEX_VIEW` é um RW guard interprocesso. Readers SHARED do mesmo índice progridem em paralelo;
mutadores EXCLUSIVE esperam todos os readers. Um attempt indexado estável faz duas direct reads de
page0 por índice usado (pre e post); attempt recusado faz no máximo duas e a chamada inteira no máximo
`2 * max_attempts` por índice planejado, independente de rows, candidatos, `k` ou frontier.
O custo de aquisição/certificação é `O(indices_usados_no_statement)`. Verify global e checkpoint são
explicitamente `O(indices_conhecidos)`.

Isso **não** é o custo total de begin/query. Cada attempt de begin lê/decodifica fora de COMMIT
`O(bytes_e_records_WAL_retidos_desde_Q)` e deriva relevant horizons em
`O(commits_desde_Q + scopes_de_manifest)`; o sandwich sob COMMIT lê
`O(segmentos_retidos + control_slots)`. Portanto o total é
`O(retained_WAL + commits + indices_usados + trabalho_do_resultado)`, não apenas O(indices). O
checkpoint limita o sufixo, mas não apaga esse termo. Slice 8 mede bytes/records de proof scan,
`wal_proof_scan_seconds`, hold time das duas certification sections COMMIT e drift retries; a
implementação não pode voltar a mover o scan inteiro para dentro de COMMIT para melhorar uma métrica.

O guard permanece retido durante a materialização, portanto uma busca/verify longa pode elevar a
latência de writers. Esse é um trade-off correctness/performance explícito, não regressão escondida.
Slice 8 mede `index_view_hold_seconds`, wait p50/p90/p99, throughput de readers concorrentes, writer
starvation e buscas HNSW longas. V7 proíbe o modo detached optimistic apesar da shared mutation
generation; ele exige emenda/prova própria de cópia completa. Hash de page0 e `Page.seq` 32-bit não
bastam para ABA.

Cada `WalMutationDraftV1` que alcança o CAS custa exatamente uma publicação do slot inativo do
`WAL_COORDINATION`: uma imagem fixed-size `WAL_COORDINATION_SLOT_BYTES_V1=204`, uma durable/visibility barrier
do carrier e direct decode/cert dos dois slots fixed-size antes do primeiro WAL byte/remove/rename.
Falha antes do CAS custa zero publication; falha depois do CAS e antes do efeito custa uma publication
burn extra. Um sucesso custa uma. Segment create/roll embutido no mesmo APPEND draft usa essa única
publication; records/batch do mesmo draft também. É proibido amortizar/batchear generations entre
drafts, grouped transactions, operation kinds ou permits independentes. Logo **somente o WAL carrier**
tem upper bound de uma publication por `WalMutationDraftV1`/attempt que chega ao CAS; isso não é claim
sobre o commit inteiro.

Essa slot write/barrier/direct-cert ocorre segurando COMMIT porque lineariza current g→g+1; V7 **não**
alega custo equivalente ao append baseline. Slice 8 mede bytes escritos/lidos, barrier/fsync count,
`wal_coordination_publish_seconds`, `commit_hold_wal_coordination_seconds`, burns e p50/p90/p99 por
APPEND/TRUNCATE/RECYCLE/QUARANTINE/RENAME/DELETE, além do throughput antes/depois. Backend que precisa
de mais de uma primitive física reporta todas; otimização futura só pode reduzir custo preservando uma
publication durable exact por draft e o mesmo ponto de linearização.

Antes disso, todo mutador comum já adquiriu fresh writer lease: action 6 publica outro
`WalCoordinationSlotV1` de 204 bytes, barrier e direct two-slot cert sob participant/lease coordinator,
antes de COMMIT. Essa publication não é a WAL-mutation publication e não pode ser coalescida sob uma
generation. Seu tempo não segura COMMIT ainda, mas entra no deadline/latência total e no lease hold.

Um commit que toca K index identities publica ainda K per-index successor slots pós-WAL, cada um com
visibility barrier e direct two-slot cert sob EXCLUSIVE+COMMIT, antes do primeiro effect de índice/
catálogo. CREATE/DROP soma uma publication do carrier global `INDEX_NAMESPACE`; todo commit soma a
publication permit-bound de commit.state, embora ela não possua counter próprio. Assim o vetor
estrutural mínimo de publications de um commit comum é `1 WRITER_LEASE + 1 WAL_MUTATION + K
INDEX_MUTATION + 1 COMMIT_STATE`; CREATE/DROP acrescenta `1 INDEX_NAMESPACE`. Refill/checkpoint/
recovery acrescentam suas generations/targets explicitamente enumeradas; adquirir nova lease em retry
repete o primeiro termo. Data/page writes não desaparecem dessa conta.

Depois do apply, o custo process-local de prova é linear no exact touched set:
`CommitRedoResultV1 = 60 + 188*T + 72*B` bytes, frontier = 404 bytes,
frontier evidence envelope = `228 +` seus cinco nested records (máximo 268435456),
`RuntimeDeviceReplayProofV1 = 500 + commit_redo_result_length` bytes e reader wrapper = 308 bytes.
Esses encodes/hashes não acrescentam database barrier, mas acrescentam CPU/memória/latência e são
medidos separadamente. Commit/recovery não pagam action15/16; cada begin paga exatamente seu lifecycle
15→16→17. Implementação pode fazer hash streaming, mas precisa conservar a preimage nested integral
durante a vida da proof e respeitar os maxima específicos de scan/history/envelope; não existe claim
genérico de 64 MiB para o bundle inteiro.

Lifecycle PROCESS é I/O/coordenação de correctness, mas preserva zero-write no database StorageDevice.
BEGIN_READ executa exatamente scan-pin install→convert→terminate actions 15/16/17, até três CAS; não
executa 10/11 nem cria segunda reader key. Action 12/13 do checkpoint fazem
duas CAS externas. Checkpoint que **avança** Q soma exatamente um lifecycle separado
`FRONTIER_SCAN_ONLY` 15→17 (duas CAS) e encode/hash de frontier 404 + rebase proof 500 bytes; checkpoint
no-op soma zero, e nenhum dos dois executa action16. FULL_SCAN sem read-view em cold/recovery executa
exatamente 15→17, duas CAS, sem 16. Nenhuma dessas actions cria/modifica byte, mtime, temp, fsync ou barrier na root DB;
backend somente process-local/advisory ainda é insuficiente, pois o coordinator precisa ser
interprocesso, owner-death-aware e identificado por incarnation. Seu custo é medido em coordinator
calls/bytes/lock hold/p50-p99, separado de storage bytes/barriers, inclusive em filesystem read-only.
Action15 não é um record fixo de 144 bytes: sua entry custa `144 + (140 + 72*R)` bytes para R ranges
WAL e o transition proof embute os registry states predecessor/successor integrais; action16 remove o
blob na mesma CAS. Logo bytes/hash/copias no coordinator são `O(registry_state + R)` por transition,
limitados pelos bounds da seção 3.1.1, e entram em `process_registry_transition_seconds/bytes` e no
deadline. Implementação pode usar hashing/transport streaming sem omitir a preimage canônica que os
mutadores estrangeiros precisam enumerar.

Se checkpoint instala qualquer SYSTEM floor, action 24 escreve o
`SystemOperationFloorMapSlotV1` inteiro de 655412 bytes, faz map barrier/direct cert e depois publica o
header `WalCoordinationSlotV1` de 204 bytes com barrier/direct cert. Cada uma das N actions 14 custa
**uma nova** map image 655412 + header 204, com as duas certifications/barriers — nunca um único
`g→g+N`. Checkpoint sem novo floor custa zero action24/map publication; MARK→CLEAR pré-checkpoint
também. Candidate checkpoint, commit.state e recycle WAL são custos adicionais. O hold de COMMIT/
lease/INDEX_VIEW inclui exatamente as publications que a ordem normativa mantém dentro dessas
sections; CAS PROCESS fora do StorageDevice continua dentro do mesmo deadline.

Backend pode coalescer uma syscall/fsync físico apenas se cada slot/target mantiver generation,
preimage, tear selection, barrier durability e direct certificate próprios; nunca batcheia uma única
generation/permit para K identities. Slice 8 mede K=0/1/N, bytes/barriers/direct reads por carrier,
`index_mutation_publish_seconds`, `commit_hold_index_mutation_seconds`, namespace/control publication,
`writer_lease_publish_seconds`, `process_registry_transition_seconds`, coordinator calls/bytes para
actions 10–13/15–17, `system_floor_map_publish_seconds`, storage bytes/barriers de action24 e N
releases action14, partial post-WAL recovery e o total `commit_hold_seconds`/
`operation_seconds`. O(K+N) e seus p50/p90/p99 são parte do ACCEPT,
não custo oculto sob a frase “uma publication”.

Cold/live device catch-up é deliberadamente
`O(bytes/records/effects_WAL_em_(Q,P] + pages/paths_tocados + indices_tocados)` e pode executar data/
directory barriers; não é incluído no bound de duas page0 reads de um statement já certificado.
Read-only V7 frio fica indisponível quando `P > Q` até checkpoint, por decisão de integridade. Slice 8
mede `cold_replay_seconds`, bytes/records/pages reaplicados, barriers, proof reuse local e taxa de
recusa RO P>Q; nenhuma config permite aceitar frontier/publication wrapper como device proof.

O feasibility pre-fence do legacy cutover é deliberadamente
`O(bytes_e_pages_de_todos_indices_legados + bytes_target_codificados + bytes_WAL_BIND_simulados)` e
faz ao menos um direct source pass; não é incluído no bound de query/refill. O peak de memória e
workspace é o valor exato do `CutoverFeasibilityManifestV1`, limitado pelo resource envelope, e disk
reservation soma target/temp/WAL/directory margin com aritmética checked. A passagem longa mantém
somente a `OfflineMaintenanceSession` que já provou zero participantes: não mantém COMMIT, writer
lease, INDEX_VIEW ou frame pin durante o scan/encoding. Cada BIND pós-fence pode manter seus guards
durante write/barrier/cold cert de `O(bytes_do_indice)`; como nenhuma API pública está exposta nesse
modo, isso é disponibilidade operator explícita. Slice 8 mede feasibility seconds/bytes/peak memory,
reserved/required/available workspace, BIND hold/resume count e injeta pressure/ENOSPC em cada
boundary.

### 12.4 Budgets e deadlines determinísticos — SUBSTITUI V6

As configurações são exact built-in ints; bool, float, subclasse hostil, negativo e overflow recusam
antes de lock/I/O:

```text
index_view_acquire_timeout_ms = 5_000       # [1, 600_000]
index_view_retry_limit        = 2           # [0, 16], retries além da tentativa inicial
operation_deadline_ms           = 30_000    # [1, 3_600_000], universal
integrity_retry_limit         = 3           # [0, 16]
```

`operation_deadline_ms` é o deadline universal de **toda** porta pública, apesar da separação de retry
limits. O budget é escolhido exatamente uma vez na entrada:

- `INDEX_BUDGET`: connect/begin estritamente read-only (nunca recovery), query/read/write statement
  já dentro de Transaction cujo snapshot/device proof foi certificado, `inspect_index`, verify,
  status e open-freshness read-only; usa `index_view_retry_limit`;
- `INTEGRITY_BUDGET`: connect/open/begin write-capable, `Transaction.commit`, DDL,
  MARK/CLEAR_STALE, RESET, rebuild, reconcile, gap completion, cold/live device catch-up, recovery,
  checkpoint, genesis/resume, bootstrap/cutover/BIND, upgrade, repair, refill/reservation, explicit
  flush e close-com-drain; usa `integrity_retry_limit` desde a entrada, mesmo quando o primeiro proof
  parece clean.

Read-only que descobre gap/device-behind recusa e não troca de classe. Write-capable que descobre o
mesmo já trouxe `INTEGRITY_BUDGET`; begin→gap-recovery/catch-up, close→checkpoint e
rebuild→MARK/CLEAR herdam **o mesmo `OperationBudget` envelope object e o mesmo
`OperationAttempt` corrente** sem reset de start/deadline/max_attempts nem renegociação de error
envelope. Statement dentro de transaction certificada usa `INDEX_BUDGET` e não
pode iniciar recovery; commit subsequente é outra porta pública/integrity budget. Porta que não pode
replayar user code repete somente sua fase pre-WAL/pre-staging; depois termina sem retry automático.

O deadline único começa na entrada da porta pública e inclui acquire, condition wait, revalidation e
retries. Cada operação cria exatamente uma vez o envelope frozen:

```text
OperationBudgetV1 = (
  magic=MAGIC_OPERATION_BUDGET_V1, version u16=1, budget_class u8, error_envelope u8,
  record_length u32, outer_operation u16, reserved_zero u16,
  budget_nonce[32], start_ns u64, deadline_ns u64, max_attempts u32,
  reserved_zero u32, crc32c u32
)
operation_budget_sha256 = SHA256(DOMAIN_OPERATION_BUDGET_V1 || canonical budget bytes)

OperationAttemptV1 = (
  magic=MAGIC_OPERATION_ATTEMPT_V1, version u16=1, reserved_zero u16, record_length u32,
  operation_budget_sha256[32], attempt_number u32, reserved_zero u32, crc32c u32
)
operation_attempt_sha256 = SHA256(DOMAIN_OPERATION_ATTEMPT_V1 || canonical attempt bytes)
```

Os nomes curtos `OperationBudget`/`OperationAttempt` significam esses records imutáveis. Seus SHAs são
propriedades derivadas, não campos autorreferentes; helpers recebem a preimage integral e o SHA. Budget
length é fixed, deadline/start/max nunca mudam; cada attempt é criado somente pelo outer loop com
`1<=attempt_number<=max_attempts` e o mesmo budget SHA.

`BudgetClassV1`: `INVALID=0`, `INDEX_BUDGET=1`, `INTEGRITY_BUDGET=2`; 3..255 recusam.
`ErrorEnvelopeV1`: `INVALID=0`, `INDEX_RETRY_EXHAUSTED=1`,
`INTEGRITY_RETRY_EXHAUSTED=2`, `TRANSACTION_WRITE_CONFLICT=3`; 4..255 recusam. Ele seleciona somente
o terminal de deadline/attempts; correctness/I/O/cancel já observado continua propagado sem conversão.

`OuterOperationCodeV1` é o mesmo u16 de `WriteSnapshotProof.outer_operation_code`:

| Valor | Operação pública/outer | Budget / error envelope |
|---:|---|---|
| 1 | QUERY_STATEMENT | INDEX / INDEX_RETRY_EXHAUSTED |
| 2 | VERIFY | INDEX / INDEX_RETRY_EXHAUSTED |
| 3 | INSPECT_INDEX | INDEX / INDEX_RETRY_EXHAUSTED |
| 4 | STATUS_OR_OPEN_FRESHNESS_READ_ONLY | INDEX / INDEX_RETRY_EXHAUSTED |
| 5 | BEGIN_READ | INDEX / INDEX_RETRY_EXHAUSTED |
| 16 | CONNECT_OR_OPEN_WRITE | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 17 | BEGIN_WRITE | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 18 | TRANSACTION_COMMIT | INTEGRITY / TRANSACTION_WRITE_CONFLICT |
| 19 | DDL_CREATE | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 20 | DDL_DROP | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 21 | DDL_ALTER | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 22 | MARK_STALE | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 23 | CLEAR_STALE | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 24 | RESET_INDEX | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 25 | REBUILD_INDEX | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 26 | RECONCILE_INDEX | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 27 | GAP_COMPLETION | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 28 | DEVICE_CATCH_UP | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 29 | RECOVERY | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 30 | CHECKPOINT | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 31 | GENESIS_CREATE | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 32 | GENESIS_RESUME | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 33 | LEGACY_CUTOVER | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 34 | BIND_IDENTITY_RESUME | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 35 | UPGRADE | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 36 | REPAIR | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 37 | REFILL_OR_RESERVATION | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 38 | EXPLICIT_FLUSH | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |
| 39 | CLOSE_DRAIN | INTEGRITY / INTEGRITY_RETRY_EXHAUSTED |

Zero, 6..15 e 40..65535 são reserved/unknown e recusam. `BEGIN_READ` nasce na entrada pública de
`begin(read_only=True)` e cobre proof scan, participant, COMMIT sandwich, reader registration e
snapshot handout; não herda QUERY_STATEMENT futuro nem usa código de helper. Connect/open read-only
usa row 4; um begin posterior cria envelope/nonce/deadline novos pela row 5. Gap/device-behind nesse
begin recusa sob o mesmo INDEX envelope, sem mudar para recovery/integrity. Nested helper preserva o código/envelope da
entrada pública: close→checkpoint continua 39, begin→catch-up continua 17; ele não troca para o código
do helper. Porta pública direta usa sua própria row. Qualquer combinação class/code/envelope fora da
tabela recusa antes de clock/lock/I/O.

`OperationBudget` nunca contém attempt mutável. `budget_nonce` é CSPRNG nonzero exact bytes e nasce
uma vez na entrada pública antes de sections;
`operation_budget_sha256` é exclusivamente a fórmula de record integral sob
`DOMAIN_OPERATION_BUDGET_V1` da seção 0.1.1, incluindo magic/version/record length/reserved/CRC; não
existe digest alternativo de fields-only.
`max_attempts = 1 + retry_limit_selecionado`, portanto vale `[1,17]`. `deadline_ns` usa soma checked
e um provider interno de `monotonic_ns`, nunca wall clock. Uma `_OuterAttemptFactory` module-private,
exact-type e possuída somente pelo loop da porta pública cria o primeiro `OperationAttempt(1)` e,
depois de cleanup+decisão, no máximo o próximo; seu `operation_attempt_sha256` é exclusivamente a
fórmula de record integral sob `DOMAIN_OPERATION_ATTEMPT_V1` da seção 0.1.1; não existe tuple digest
alternativo. Contextos são frozen,
sequenciais, single-current e nunca editados/reutilizados. Helpers recebem referências read-only ao
mesmo envelope e ao contexto corrente; não recebem a factory. O acquire recebe
`min(index_view_acquire_timeout, remaining_deadline)` como valor built-in e não reinicia o deadline.

O clock é consultado somente antes de adquirir sections e depois de liberar todas; nunca roda
provider de clock, callback, métrica ou sleep sob COMMIT, lease, INDEX_VIEW, manager lock ou page pin.
Aquisição de várias identities usa uma única primitive
`acquire_many(sorted_identities, mode, absolute_deadline_ns)`: o backend enfileira/adquire tudo ou
libera qualquer parcial antes de retornar, usando o mesmo deadline absoluto. Ele não devolve controle
ao caller entre guards e não pede que o caller consulte clock segurando o primeiro. O coordinator
aplica o timeout internamente. Drift pre-WAL libera/revoga tudo, faz rollback ao staging mark e
somente então aplica a decisão fechada abaixo; se outra tentativa foi admitida, o outer factory cria
um novo contexto `attempt_number + 1`, preservando o mesmo envelope object. Não há polling por `sleep`; testes usam
`multiprocessing.Event`/fake monotonic.

Helpers não possuem retry loop próprio. Eles devolvem sucesso, erro terminal ou
`RESTART_OUTER_ATTEMPT(reason)`; somente a porta externa pode pedir o próximo `OperationAttempt` à
factory. Assim N helpers
nested não multiplicam para `max_attempts^N`. O error type/`outer_operation` é o envelope escolhido na
entrada; um acquire INDEX_VIEW dentro de catch-up integrity não troca para `GrafxIndexError` nem ganha
contador index separado.

O wait da condition de refill recebe `absolute_deadline_ns`; timeout não rouba/reset o attempt do
owner e termina o waiter com erro typed. Todo owner fecha/notifica seu ticket em `finally` para
qualquer `BaseException`; hard process death elimina o manager inteiro. O single-flight local formado
por `POOL_DERIVED_EPOCH` → `INDEX_CACHE_REFRESH` também recebe o mesmo deadline e usa
owner/attempt/PID/generation + `finally`; timeout esperando o primeiro nunca tenta tomar o segundo,
timeout esperando o segundo libera o global, e todo caminho que adquiriu ambos libera per-index e
depois global.
Owner stalled faz waiters terminarem em timeout; owner thread terminado sem fechamento instala local
poison, nunca força unlock/reusa cache. Nenhum desses waits é ilimitado.
Reader-registry snapshot/floor pin, WAL scan pin e WAL/device carrier acquisition recebem o mesmo
deadline absoluto. Registration viva não é TTL-evicted ao expirar o waiter: a operação que aguardava
termina, sem revogar o owner; release/owner-death seguem o protocolo conservador.

O deadline limita admissão, espera e início de **nova tentativa**; não é cancelamento assíncrono de
uma page read/apply já iniciada. Uma tentativa que termina depois do deadline não inicia outra. Timeout
de I/O bloqueante pertence ao StorageDevice/coordinator e recebe o mesmo absolute deadline quando a
porta suporta isso; V7 não cria thread killer nem abandona pin/guard para simular cancelamento.

Exaustão da chamada é terminal e typed; `reason` é exatamente `"deadline"` quando não resta tempo e
`"attempts"` quando `current_attempt.attempt_number == budget.max_attempts`:

Depois de liberar tudo, a decisão usa uma única leitura `now_ns` e ordem fechada: (1) propagar
corruption/unknown-required/I/O inconclusive/poison já observado; (2) propagar sem conversão o
`KeyboardInterrupt`/`SystemExit`/cancel exception capturado, após cleanup; (3) se
`now_ns >= deadline_ns`, terminar com `reason="deadline"`; (4) senão, se
`current_attempt.attempt_number >= budget.max_attempts`, terminar com `reason="attempts"`; (5) senão
criar o contexto frozen seguinte e repetir. O contexto anterior já está revoked e nunca é mutado.
Logo cancellation vence timeout, e deadline vence attempts quando coincidem; erro de correctness já
observado permanece primário. Nenhum erro depende de segunda leitura do clock.

Nos erros abaixo, `attempt` é somente a cópia built-in de
`current_attempt.attempt_number`; nunca é campo mutável do budget/context:

- acquire cujo outer é `INDEX_BUDGET`: `GrafxIndexError(field="index_view_timeout", reason, attempt, max_attempts,
  deadline_ms, retryable=True)`;
- token/identity mudou continuamente sob `INDEX_BUDGET`: `GrafxIndexError(field="index_view_epoch", reason, attempt,
  max_attempts, deadline_ms, retryable=True)`;
- commit/DDL/MARK/CLEAR/RESET/rebuild/reconcile com drift pre-WAL:
  `GrafxWriteConflict(field="integrity_budget", operation, reason, attempt, max_attempts,
  deadline_ms, retryable=True)`;
- gap completion, cold device catch-up, recovery, checkpoint, genesis/resume, bootstrap/cutover/BIND,
  upgrade ou repair:
  `GrafxRecoveryRefused(field="integrity_budget", reason, attempt, max_attempts, deadline_ms,
  retryable=True)`;
- explicit flush/close-com-drain: `GrafxRecoveryRefused(field="integrity_budget", operation,
  reason, attempt, max_attempts, deadline_ms, retryable=True)`;
- refill/reservation: `GrafxLeaseTimeout(field="integrity_budget", reason, attempt, max_attempts,
  deadline_ms, retryable=True)`;
- read-only com gap, `P > Q` ou device proof ausente:
  `GrafxUnsupportedOperation(field="read_only_consistency", reason, attempt, max_attempts,
  deadline_ms)`, nunca retry que muta;
- write-capable connect/begin cujo budget termina durante proof/recovery/catch-up:
  `GrafxRecoveryRefused(field="integrity_budget", operation=outer_operation, reason, attempt,
  max_attempts, deadline_ms, retryable=True)`; nested acquire não troca o tipo;
- corruption, unknown-required, I/O inconclusive e local poison não são convertidos em timeout.

`retryable=True` informa que **uma nova chamada** pode ter sucesso; a chamada atual terminou. Depois
de WAL barrier, budget não autoriza abandonar/retentar como nova transação: o estado fica
`recovery_required` até completion exata, mesmo se o deadline expirar.

## 13. Compatibilidade profunda com o SHA 539 — B3/B4/B6/B7/B8/B9

| Blocker | Evidência real | Mecanismo normativo | Mutante de aceite |
|---|---|---|---|
| B3 — LSN previsto/retarget tardio | `wal_manager.py:812-986`; `txn_manager.py:1477-1497,1789-1973` | migrar commit genérico para `WalAppendPlan` frozen, tail proof completa e append de blob exato | clock/roll/tail muda entre freeze/append; zero byte ou plano exato, nunca image com LSN previsto |
| B4 — replay achatado | `decision.py:57-72,183-234`; effects tipos `:48-54` | `CommittedHistory` preserva epoch/txn/terminal/payload/order; control só por shape fechado | txn id repetido entre epochs, terminal duplo, record pós-terminal, marker solto, payload/touches falso |
| B6 — verifier pode false-clean | oráculo `40a4201`, `verifier.py:471-682` | preservar v1 bit-for-bit; branch identity antes de TableExtent; estado inconclusive separado | page_count/read failure nunca gera clean/repair; decoder trap; orphan com CSN nunca removido |
| B7 — namespace/OCC forjável | `partitions.py:83-125`; `context.py:283-302`; `_find_conflict` `txn_manager.py:1726-1757` | IDs reservados, revalidação dos sets crus, `_IdentityOccPermit`, grouped reservation e `RebasedPage0Proof` | note/direct-set forjado, foreign/own reservation, page0 genérica disfarçada, transition nunca exempt |
| B8 — lease/fork sem PID | `coordination.py:83-220`; `coordination_local.py:765-942`; txn order `:1416-1432` | PID-first, poison, fresh lease, release-before-activation, child reset sem locks, no reclaim | fork em cada fence; child use/close; release incerto; stalled owner/takeover; retained guard recusado |
| B9 — recovery/checkpoint/RO | `recovery_manager.py:590-663,1350-1478`; `txn_manager.py:996-1069,1154-1250`; `segment.py:119-150` | grouped phase, exact recovery, checkpoint global legítimo, RO byte-identical, prefix-only | kill em cada boundary; transition abaixo/acima checkpoint; interior gap; stale state; RO zero calls/bytes |

Dependências adicionais descobertas na auditoria:

- B5: `BufferPool.apply_page_image` hoje silencia equal-different/higher-unproven; enum+preflight é
  pré-requisito de recovery v2.
- COW: allocator/extent atuais mutam pool/device durante planning; toda porta v2 deve usar clones e
  endereçamento virtual antes de writers.
- Capability: rejeitar `note_*` não basta porque os sets são mutáveis; provenance module-sealed é
  condição de commit.
- Checkpoint: o flush global é intencional e não deve ser confundido com o flush amplo acidental de
  commit/recovery.

### 13.1 Novos blockers de implementação encontrados no SHA-base

Eles não alteram o objetivo, mas impedem iniciar leasing diretamente:

| ID | Blocker novo | Evidência/fechamento |
|---|---|---|
| N1 | corrigir só o append identity deixaria o commit genérico com LSN preview/retarget | `txn_manager.py:1477-1497,1789-1973`; slice 2 migra o planner interno, mas porta pública só habilita no Gate 5 pós-COW, antes de tipo 14 |
| N2 | control/phase não é inferível de `CommittedReplay` achatado | `decision.py:57-72,183-234`; grouped history é pré-requisito, não adapter opcional |
| N3 | checks em `note_*` são burláveis pelos sets crus | `context.py:210-211,283-302`; commit revalida provenance e capability, antes de lease/device |
| N4 | owner string com PID não impede uso pós-fork | `coordination_local.py:800`; todas as authorities precisam de PID-first/poison/at-fork reset |
| N5 | apply atual confunde idempotência, conflito e futuro sem prova | `buffer_pool.py:1299`; enum/fingerprint/supersession deve entrar antes de recovery v2 |
| N6 | trocar taxonomia RO quebraria API aceita | `recovery_manager.py:650-663`; preservar `GrafxUnsupportedOperation(read_only_consistency)` |
| N7 | exigir exact-write do checkpoint quebraria a semântica explícita de drain | `txn_manager.py:1043`; checkpoint whole-pool permanece e é separado de redo exact |
| N8 | allocator/extent atual pode alocar e dirty antes do WAL | `heap_store.py:538-578,1105-1125`; COW/virtual allocation integral é gate do slice 5 |
| N9 | published global torna reservation/foreign commit falsamente relevante para todo índice | `index_manager.py:623-680,1807-1872,2106-2147`; relevant-horizon + fresh direct header entram antes de control writer |
| N10 | unknown WAL não distingue trabalho obrigatório de extensão pulável, e flags v1 não estão reservados | `wal/codec.py:14-17`; `wal/record.py:168-180,224-271`; WAL format v2 + DatabaseIdentity V2/C entram antes de required/manifest/tipo 14; v1 fica opaco |
| N11 | participant section não fecha a janela pre-refresh→mutação entre processos | `txn_manager.py:163-168,2679-2703`; RW `INDEX_VIEW` estável, SHARED durante toda leitura e EXCLUSIVE em todo mutador entram no slice 3 |

### 13.2 Fechamento dos sete blockers da revisão V5

| ID | Blocker V5 | Resolução normativa V7 | Mutante decisivo |
|---|---|---|---|
| R1 | unicidade global incompatível com legado | identidade é `(table_id,record_id)`; F só separa números novos; zero renumeração | duas tabelas v1 com row 1 fazem upgrade e cold verify clean; duplicata intra-table recusa |
| R2 | compatible FE/FF indefinido | leitura permitida; DML/DDL FE/FF e novo DDL que os criaria recusam typed pre-staging/pre-lock/pre-device | remover cada fence ou classificar legado como corrupt |
| R3 | upgrade certificava fora do lease sem digest completo | `UpgradeDigest` catalog+heap+inventory+tail recertificado integralmente sob fresh lease+COMMIT, restart pre-WAL em drift | DDL concorrente sem extent muda só catálogo |
| R4 | cursor possuía `pos`/revogação e ordem local incompletas | manager owns pos; handle frozen nonce-bound; single-flight attempt/CAS/condition; lock nunca atravessa participant/lease/COMMIT | stale/copied handle, close-vs-refill, spurious wakeup, activation CAS removido |
| R5 | counter consumer/remainder multi-table ambíguos | grant database-bound; reservation interna antes do planner; um range serve todos os intents; `COUNTER_CONSUMER` removido | DDL+rows em 3 tabelas, reorder intent depois do consume, table switch sem burn |
| R6 | superseded podia escapar de exact write-back | APPLIED/IDENTICAL/SUPERSEDED entram sempre em touched pages | higher committed dirty no pool, retry e cold reopen |
| R7 | control-only avançava published e staleava todos os índices; pre-refresh deixava writer cross-process mutar | relevant horizon pelo manifest total; checkpoint certifica headers; cada statement usa RW `INDEX_VIEW` SHARED e todo mutador usa EXCLUSIVE, com token raw+derived epoch em generic/exact/HNSW | reservation/foreign table, index-only omitted, pause pós-precheck + writer cross-process; readers coexistem e writer espera |

## 14. Matriz explícita dos 11 blockers V3

| # | Blocker | Mecanismo V7 | Teste/mutante obrigatório |
|---|---|---|---|
| B1 | v2 não era kind-aware nem tinha limites/checksum semântico | heap-only v2; T/generation; identity codec/slot único; fingerprint canônico; capacity v2 | non-heap v2; missing/duplicate/moved slot; no-room upgrade; equal LSN com seq/CRC diferente mas semântica igual |
| B2 | RESERVED per-table contradizia DDL+primeira row | piso global F; grant database-bound; reservation interna anterior ao planner; nenhuma entry placeholder ou counter consumer | create table+node/edge/index multi-table na mesma txn; kill em todas as fronteiras; nenhum sentinel extent publicado |
| B3 | transition LSN previsto/circular e plan mutável | `WalAppendPlan` frozen genérico, tail proof completa, exact encoded blob | roll, foreign append, clock hostil, mudança de record length; mutantes `last_lsn+n`, retarget após freeze e append reencodando |
| B4 | replay perdia epoch/txn/terminal/CommitPayload | `CommittedHistory` agrupado e gramática fechada | reused txn ID cross-epoch, duplicate/missing terminal, post-terminal, marker solto, wrong payload/order/partitions |
| B5 | write/dirty/path pre-WAL, whole-file flush e skip de LSN sem prova | detached COW/virtual paths, zero data temp, `_IrrevocableWalPermit` pós-barrier, full preflight, apply enum, exact touched pages | zero data calls/frames/temp pre-WAL; bootstrap/coordinator permit não serve CREATE/BIND; dirty unrelated intacto; equal-conflict e higher-unproven recusam |
| B6 | repair podia apagar resíduo e false-clean | verifier `40a4201` preservado; classifier fechado; repair só counter raise | unreadable/inventory refusal/orphan/duplicate/cross-owner/CSN real: byte-identical refusal; v1 findings idênticos |
| B7 | namespace público forjável e reservation self-conflict | reserved IDs, format-aware catalog ceiling, module-sealed permit e rebase proof | note/bulk/direct-set forge; generic page0 disfarçada; own/foreign reservation; transition nunca isenta |
| B8 | locks, release, stale owner e takeover ambíguos | ordem única, fresh guard, multi-fence, activation pós-release, remainder nunca reclaimed | lease stolen antes/depois de cada fence; release uncertain; stale owner; retained lease; timeout/generation restart |
| B9 | phase/checkpoint/RO/transition recycle ambíguos | T exact, prefix-only, grouped simulation, checkpoint cert, RO byte-identical | T=0/T>0 antes/depois checkpoint; interior gap; marker pretransition; v1+transition pending; RO zero writes/barriers/mtime |
| B10 | overflow, rewind, abort e fork podiam reutilizar | half-open checked ranges, manager-owned pos, nonce revogável, burn semantics, increment-before-exposure e PID poison | near MAX/u64 wrap; size1; insufficient remainder; conflict/abort/KI/SE; child consume/close/release do pai |
| B11 | O(1), capacity e ordem dos slices não sustentados | claim CPU bounded por slots; zero data scan; capacity pública; generic writer só no Gate 5 pós-COW e identity writer depois | read/write counters invariantes a rows; capacity edge; mutante enable Slice3/4 falha; benchmarks não substituem provas |

## 15. Blockers Claude L1–L4/O1/O2

| Finding Claude | Resolução V7 | Evidência/teste de fechamento |
|---|---|---|
| L1 — identity slot chegaria a `TableExtent.decode` | enumeração completa dos dois callsites produtivos do SHA 539, verifier aceito e ancestrais; dispatcher bruto antes do decoder; writer separado | `heap_store.py:1168,1214`; verifier `40a4201:635`; decoder trap mantém zero em todas as rotas |
| L2 — monotonicidade global falsa com ranges simultâneos | removida garantia cross-process; um range database-bound por manager; só remainder insuficiente queima antes de refill `max(size,n)`; identidade lógica continua table-scoped; métricas fechadas | multiprocess força inversão de ordem mas prova disjunção pós-transition e unicidade de pares; troca de tabela não refill; restart não reclama |
| L3 — rota de flush indefinida | inventário de 12 callsites diretos e cinco callers de checkpoint; commit/recovery/index apply usam touched pages; global só explicit flush/close/checkpoint/bootstrap isolado | monkeypatch whole-file flush nas rotas proibidas; dirty sentinel; cada header/index/new/tail page exigida no resultado |
| L4 — continuidade assumida sem política | normal recycle declarado e provado prefix-only; gap interior é dano/inconclusive, não estado limpo | `segment.py:119-150`; `wal_manager.py:1708-1787`; mutantes break/continue, newest e horizon |
| O1 — O(1) não sustentado | claim substituído por CPU O(slots), bounded por capacidade page0; zero data/catalog/owner scan | page-count/read-count independe de rows; slots variam até capacity e CPU é medida honestamente |
| O2 — capability abstrata | `_IdentityOccPermit` adapta `_RecoveryPermit` module-sealed, manager/txn/attempt/PID/generation-bound e revogado | `recovery_manager.py:284-338`; forge/copy/leak/reuse/cross-manager/cross-attempt recusados |

### 15.1 Blockers adversariais específicos da V6 fechados pela V7

| ID | Evidência/falha V6 | Mecanismo normativo V7 | Gate decisivo |
|---|---|---|---|
| A1 — flags v1 reinterpretados | `record.py:168-180`; tests congelam `0x1234` e `9`; V6 415–432 atribuía bits | DatabaseIdentity V2/C + WAL format v2; prefix CRC compatível; v1 inteiro opaco; old build recusa meta V2 | round-trip de todo valor amostrado v1; V2 sob meta V1 e V1 pós-C recusam; crash em cada fronteira do cutover |
| A2 — read-view sobre COMMIT não publicado ou device não materializado | WAL barrier/apply precedem commit.state e commit comum não faz data fsync | `WalFrontierCertification` exige frontier=P/zero incomplete-required; `RuntimeDeviceReplayProof` exige exact Q→P; `WalPublicationProof` liga ambas a reader key; recovery/catch-up two-pass, RO exige P==Q | kill/power loss pós-barrier/em cada apply/pré-publish; begin nunca devolve snapshot até completion/materialização |
| A3 — manifest index-only incompleto | apenas CREATE/DROP/ALTER; REMOVE/INDEX_RECONCILE, RESET e rebuild podiam não mover horizon | enum total exato 1–13: COMMIT 1–12, incluindo REMOVE e BIND; checkpoint somente op 13; digest/identity/operation bijetivos; manutenção normal sempre logged | omitir/trocar cada operation; index-only no mesmo table/LSN precisa alterar horizon/token |
| A4 — stale compartilhado apagava poison local | `_stale_reason` único; clear estrangeiro podia false-heal processo falho | `_shared_stale_reason` refreshable + `_local_poison_reason` sticky; limpeza local só por reopen/repair certificado | partial apply local + rebuild estrangeiro clean continua recusando até nova generation |
| A5 — ABA/cache HNSW | header sem generation; Page.seq 32-bit; graph só `_graph_mark == built_through_lsn` | RW guard SHARED/EXCLUSIVE + shared mutation generation checked/fail-closed; raw page0 hash; token completo + pool.derived_epoch + graph SHA | stale→rebuild→clean mesmo LSN; seq wrap/raw ABA; generation força descarte e graph/cache antigos não sobrevivem; MAX recusa antes do efeito |
| A6 — post-fence depois de staging | QueryEngine materializa e chama `context.release()` em 859–860 | pending result; post-cert antes de release; outer mark cobre rows/catalog/index/attachments/partitions; rollback LIFO | falha no terceiro staged item e drift no post-token deixam txn exatamente no mark; IDs não rewound |
| A7 — admin fora do fence | `Database.verify` 1520–1538, `inspect_index` 1735–1753, status/reconcile não estavam inventariados | tabela 8.4.7: verify/inspect/status SHARED; reconcile/reset/rebuild/recovery/checkpoint EXCLUSIVE | monkeypatch permit/guard por rota; nenhuma lê/muta live index sem modo correto |
| A8 — retry/deadline indefinidos | “reinicia até deadline/budget” sem nomes, limites, clock ou terminal | quatro configs fechadas; `OperationBudget` envelope imutável + `OperationAttempt` outer-only, deadline/max e erro typed; sem sleep/callback sob locks | fake monotonic no limite, bool/overflow, drift contínuo, nested factory mutant e timeout de acquire produzem sequência exata 1..N |
| A9 — F/reservation contraditórios | V6 178 sugeria consumer avançando F; recovery/checkpoint não fechavam grant | só reservation avança F; consumer só copia F; recovery materializa stop sem grant, revoga generation; checkpoint não persiste remainder | crash antes/depois release/activation/checkpoint; restart jamais reclama range e novo start é F |
| A10 — lease/guard e performance | two-pass recovery/checkpoint omitia fresh lease; INDEX_VIEW exclusivo serializaria readers | certify → exit → fresh lease → EXCLUSIVE* → COMMIT revalidate/apply; RW guard permite readers SHARED paralelos; custo 2 page0/identity/attempt, bounded por budget | dois readers coexistem, writer espera; retained lease/COMMIT→guard mutant recusa; benchmark hold/wait/starvation |

Essa matriz é parte do ACCEPT: marcar V7 aceita sem cada gate verde é violação do contrato, mesmo que
os testes antigos permaneçam verdes.

## 16. Matriz de crash, fences e publicação

Cada boundary recebe soft fault (`Exception`), process-control (`KeyboardInterrupt`/`SystemExit`) e
hard kill em processo separado. Reopen usa pool frio e verifier completo.

| Boundary | Estado obrigatório após falha/reopen |
|---|---|
| antes/depois do PID check | zero lock/I/O/métrica no objeto herdado; child incapaz de cleanup do pai |
| genesis antes/depois do primeiro marker root-level, cada create/data+parent-dir barrier, PREPARED/FENCED/BOUND | antes do marker qualquer root atualmente vazia/certificada no final component pode iniciar sessão nova, sem alegar mesma identity; o único temp reserved segue `FirstMarkerOrphanPartialTempProofV1`/resume/disposition exact, enquanto qualquer outra entry/temp/slot parcial recusa. PREPARING válido binda root+parent identities; depois, replacement vazio recusa e `V7_GENESIS_INCOMPLETE` resume bit-identical; nenhum handle antes do segundo cold reopen BOUND; artefato sem marker nunca é guessed/overwritten |
| antes/depois de marker PREPARING/PREPARED, cada create/parent-dir barrier de sidecar/carrier | LEGACY_UNFENCED nunca ganha artefato lazy; parcial entra maintenance-incomplete retomável; PREPARED prova baseline `(0,0,Q=B)` e todos os carriers antes do fence |
| antes/depois do meta/WAL cutover freeze, WAL v1 barrier, meta apply/barrier/direct cert/publish, checkpoint C/marker FENCED, cada BIND e checkpoint BOUND/marker BOUND | antes do marker sucessor phase pin retém fence/BIND raw preimages e recycle-first não cruza B/C; depois dele identity V2/C é irrevogável/restrita; old writer recusa; nenhum record v2 aparece antes de C; generic writer só após todos HeaderV2/path/carriers + terceiro cold reconnect BOUND |
| antes/depois de manager ticket/CAS/condition wake | um refill single-flight; stale attempt não ativa; close revoga sem deadlock |
| gap/device-behind descoberto durante refill com waiter/close | gap revoga ticket e recovery generation força outer restart desde PID; device-only conserva ticket só generation-equal uma vez; waiter/close nunca ativa CAS antigo |
| antes/depois de participant entry | nenhuma authority/range; cleanup idempotente |
| antes/depois de fresh lease acquire | sem COMMIT durante acquire; lease não retido; falha libera ou marca uncertain |
| antes/depois de frontier/device/wrapper e reader pin | COMMIT acima de published/incomplete/required ou device proof atrás impede snapshot; pin/wrapper atômicos sob COMMIT; RO P==Q zero-write refusal |
| cold open/catch-up em cada effect/barrier/direct cert Q→P | frontier sem device proof nunca basta; RW reaplica/certifica integralmente antes do handle; RO P>Q recusa byte-identical; kill perde proof local e repete |
| recovery/gap/catch-up antes/depois de authority/apply/proof emission | `WriteSnapshotProof` é inconstructível/recusada nessa porta; exact Recovery/Cold authority não autoriza novo COMMIT e só emite predecessor proof após barriers/direct cert/publication na ordem |
| long txn S, foreign P>S, local pre-append/successor P' | todo mutador exige predecessor device=P; device-only catch-up pode recertificar pre-WAL; gap recovery aborta txn por generation; successor cobre P→P' sem pular S→P |
| depois de lease validate/gap completion/read view | mudança de generation/tail reinicia antes de write; completion usa fresh lease + EXCLUSIVE e nova proof |
| durante clone/virtual allocation/first ou second OCC | zero allocate/write/dirty/resident mutation; range consumido localmente permanece burn-only |
| antes de `WalAppendDraft.freeze` | nada persistido; relógio/roll não escapam |
| entre freeze e CAS/permit/`append_planned(mutation_draft,append_draft,permit)` | tail ou predecessor divergente recusa retryable com zero bytes; a projection Q→post-tail com 65535 segmentos ainda produz pin/tail canônicos, enquanto 65536 recusa antes do CAS; kill pós-CAS só deixa generation extra |
| antes/depois de manifest validation | affected tables e COMMIT operations 1–12 correspondem a todos os effects, inclusive REMOVE/INDEX_RECONCILE, BIND e index-only; checkpoint op 13 cobre todas identities |
| CREATE/BIND antes/depois de WAL barrier, permit, temp/data+parent-dir barrier | pre-barrier só path/images virtuais e carrier coordinator; zero target/temp data; pós-barrier permit exact-type cria imagem bit-identical; kill repete grouped redo; bootstrap permit nunca serve data path |
| antes/depois de `IndexFencePlan`, cada SHARED/EXCLUSIVE acquire e pre-refresh | conjunto canônico/bijetivo; readers coexistem; mutador espera; timeout recusa; nenhum lock local substitui fence cross-process |
| após cada byte/record de append e antes da WAL barrier | preimage restaurado ou uncertainty latch; nunca publica/aplica |
| imediatamente após WAL barrier | transaction irrevogável; recovery completa; evento contábil fecha uma vez e só é emitido se o processo sobreviver |
| após phase-0 CAS e antes/depois de cada quarantine temp/create/publish/barrier, cada byte do **ledger COW temp**, data barrier/atomic replace/parent barrier/direct cert; após phase-1 CAS antes do primeiro syscall e depois de cada recycle/create/write/rename/release/barrier/cold cert | o slot da CAS grava HELD_OPEN/g; kill antes do primeiro syscall e kills repetidos após fresh lease reconstroem o mesmo event `g→g+1` e usam somente `_TailPhase0ContinuationPermit`, sem novo generation/draft/event. Phase 1 só limpa pending junto de `g+1→g+2` após event direct-certified. Identity seed deriva o mesmo quarantine/event/temp sem CSPRNG de attempt; phase 0 aceita somente os cinco states totais do COW progress, nunca active-ledger partial/append kind1. Child permit ordinal não escapa do parent kind5. Depois do event durável, phase 1 aceita toda e somente a cadeia `WalMonotonicProgressProofV1`: k=0 pós-CAS, prefixos newest-first, temp strict prefix, barrier-uncertain e terminal; child target6 não é permit top-level cruzado. Syscall target-exact não se repete; barrier incerta sempre se repete; gap/reorder/terceira image recusa. Evidência fica durável antes do corte e somente sufixo terminal sem COMMIT/unknown-required é removido |
| antes/depois de cada apply de new/overflow/link/catalog/index/heap0 | full preflight impede conflito parcial; kill exige grouped redo exato |
| antes/depois do COMMIT MARK_STALE e primeira mutação de maintenance | antes do WAL/barrier não há página alterada; depois, gap ou durable STALE bloqueia reader até rebuild/CLEAR certificado |
| antes/depois de cada exact write-back | redo idempotente; dirty unrelated não chega ao device |
| antes/depois do heap data barrier | format/reservation não publica até read direto; ordinary consumer usa WAL authority |
| antes/depois do direct heap0 verify | mismatch recusa e mantém recovery-required; não ativa range |
| antes/depois de commit.state publication | reopen completa ou observa publicado exato, nunca meio estado |
| antes/depois de COMMIT exit | lease ainda válido até exit; nenhuma activation |
| antes/durante/depois de lease release | release confirmado permite activation; failure/uncertain queima e envenena |
| antes/depois de range assignment | crash antes perde range como gap; depois usa só no mesmo manager/PID |
| antes/depois de manager increment e callback | valor nunca retorna ao manager; KI/SE preservada |
| antes/depois de statement mark, post-token e `release_to_transaction` | falha restaura rows/catalog/index/attachments/partitions ao mark; nenhum resultado; IDs consumidos ficam queimados |
| shared stale refresh/local poison/HNSW token | foreign clean pode limpar só shared; poison local continua; graph exige token completo + derived epoch |
| checkpoint/reconcile com transaction S pausada entre statements | registry snapshot é revalidado; Q nunca excede S; REMOVE só abaixo estrito de S; retomada vê exatamente o snapshot antigo |
| checkpoint antes/depois de global flush/barrier/direct cert/slot+parent-dir manifest/commit.state/recycle | slot antigo sobrevive até novo durable; Q só avança com manifest/reader floor correspondente; recycle só prefixo; tail contínuo |
| read-view antes/depois de pre-header, traversal/materialization/post-header/release | SHARED cobre tudo; writer EXCLUSIVE lineariza depois; generic/exact/HNSW não retornam parcial |
| antes/depois de cada retry/deadline edge | número de attempts exato; todas sections liberadas antes do clock; terminal typed sem loop/sleep infinito |
| read-only em toda boundary equivalente | zero write/allocate/truncate/recycle/barrier/mtime; erro público typed atual |

Propriedade pós-kill para todo caso em que um ID foi ou pode ter sido exposto:

```text
all_physical_and_visible_(table_id,record_id)_pairs_are_unique
and direct_heap0_floor > max(exposed_or_stored_post_transition_record_id)
and last_complete_durable_commit_lsn == published_lsn_at_every_returned_read_view
and runtime_device_materialized_through_lsn == published_lsn_at_every_returned_read_view
and verifier has no false clean
```

Duplicatas numéricas cross-table herdadas de v1 são válidas e não violam a propriedade; duas rows da
mesma tabela com o mesmo `record_id` continuam sendo dano. Não se tenta reconstruir exatamente
quantos IDs um processo morto queimou; isso seria scan e ainda
não distinguiria IDs entregues de IDs nunca observados.

## 17. Matriz adversarial e multiprocess

### 17.1 Formato/verifier

- genesis hard-kill antes/depois do primeiro marker, de cada file/parent-directory barrier e de cada
  phase: retry/resume só aceita image manifest bit-identical; BOUND tem C=T=P=Q=0 e segundo cold
  reopen; recognized artifact sem marker recusa sem cleanup; mutante que inclui slot/temp bootstrap
  no image/carrier manifest ou quebra predecessor chain falha no codec/preflight;
- namespace gates usam Event, sem sleep: POSIX symlink/hardlink/mount e Windows symlink/junction/
  reparse/hardlink são injetados em root, ancestral, parent, target e temp, inclusive swap depois do
  preflight e antes de create/replace/rename/delete/quarantine/recycle. Capability handle-relative
  recusa/poison sem tocar alvo externo; link_count>1, duas names/DBs com mesmo file identity e parent
  identity trocada falham. Cross-directory injeta swap/reparse separadamente no source e target e
  exige source+target handle identities/barriers; mutante resolve um lado por string ou faz só uma
  barrier. Genesis com root ausente mata após mkdir/parent barrier e troca a root por outra vazia antes
  do marker: retry adota a identity atual em sessão/DB UUID novos e PREPARING a binda; repetir o swap
  depois do PREPARING recusa. Qualquer link/reparse/alias/content/temp/slot parcial pré-marker recusa.
  Backend sem no-follow/handle-relative/barrier segura recusa V7 pre-marker;
- legacy cutover com cada BIND exatamente no record/batch/segment/u32/memory/workspace limite passa;
  um byte/count acima e target grande demais recusam com zero marker/arquivo/mtime alterado. Mutantes
  omitem um índice, usam estimate em vez do encoder real ou fenceiam antes do feasibility digest;
- `GenerationCapacityPlanV1` parametriza genesis/cutover com N=0/1/N e cada domínio em MAX-k: se
  worst-case burns de todos attempts + minimum completion path não cabe, recusa com zero marker. Resume
  após CAS g+1 pre-byte aceita o mesmo initial plan, deriva burn/current maior e não exige equality ao
  initial; novo budget não renova folga. Caso que cabe exatamente o cutover mas deixa reserve zero
  pós-BOUND também recusa. Mutantes omitem domínio/per-index step, contam só uma increment
  por attempt, gastam o último reserved slot, resetam consumed no resume ou usam marker/file identity
  no `domain_identity` falham;
  ENOSPC/OOM/EDQUOT depois do fence em cada WAL/target/barrier pausa e resume no mesmo digest, sem
  rollback/adoption de target parcial;
- recomputar o DAG base→bind-plan-source→core→outer→full-plan(base SHAs)→normalized prefix/L[0]→
  (bound context/projection/L[i+1])*→normalized BOUND checkpoint/L_terminal→envelope→marker
  produz cada byte esperado. Mutantes inserem final digest/marker SHA no core, fazem full-plan hashear
  bound context, mantêm placeholder, trocam uma edge ou alteram um base byte sem propagar: todos
  recusam pre-marker, sem tentativa de ponto fixo/self-hash. Mutantes colocam final feasibility/
  marker SHA no BIND trailer/template/projection ou fazem `bind_plan_source_digest` depender do core:
  também recusam antes do marker. Mutantes injetam PREPARED/BOUND marker/raw slot SHA,
  LegacyFenceWriteProof, image/carrier final digest ou ColdOpen/proof nonce nas prefix/suffix
  projections: recusam; runtime demonstra checkpoint durable/direct cert anterior ao marker sucessor;
- dois attempts BIND comprovadamente pre-WAL usam clock/UUID/attempt/raw blobs distintos mas a mesma
  normalized projection; ambos cabem, enquanto mudar um byte target/manifest/LSN/length falha.
  Recovery pós-barrier conserva UUID/BEGIN/COMMIT raw committed e nunca gera seed novo;
- retry pre-byte de Transaction contendo CREATE usa seed/clock/attempt novos, mas conserva exatamente
  `transaction_uuid`, PersistedIndexIdentity/path/carrier plan; mutante que aloca UUID novo ou altera
  identity entre retries recusa antes do WAL;
- com N>1, fence/checkpoint-C e BINDs anteriores usam epoch/txn/UUID/CRC diferentes entre simulação e
  execução, e raw committed-history roots antes de BIND0/i e no checkpoint BOUND também diferem; cada
  actual certification ainda projeta para L[i]/L_terminal. Mutantes que preservam raw history no
  stable context, pulam prefix/suffix, taggeiam output SHA/CRC em vez de recalculá-lo ou aceitam chain
  divergente falham;
- kill após publicar/direct-certificar checkpoint C e antes de marker FENCED conserva BEGIN..fence
  COMMIT; kill após checkpoint final e antes de marker BOUND conserva todos BIND/trailer/body bytes.
  Tentativa de recycle-first fica limitada a B/C pelo `CUTOVER_PHASE_RETENTION_PIN`; somente marker
  sucessor durable + fresh revalidation libera Q. Mutantes usam history SHA opaco ou TTL/owner release;
- N BINDs canônicos dentro do mesmo `OperationAttempt` recebem `plan_ordinal` 0..N-1 e UUIDs todos
  distintos; MARK→rebuild→CLEAR no mesmo attempt recebe três ordinals/UUIDs distintos. Em cada grouped
  txn BEGIN==COMMIT UUID; mutantes reuse entre ordinals, UUID novo depois do primeiro WAL byte,
  ordinal repetido ou seed de outro operation context falham;
- kill/`CUTOVER_RESOURCE_REQUIRED` depois de BIND k e resume em nova chamada pula os committed mas
  começa em ordinal k+1 original, nunca 0; retry de rebuild/CLEAR conserva operation identity e
  ordinals fixos 1/2. Mutantes que derivam ordinal da lista remaining ou do attempt falham;
- fault depois de **cada capability pre-draft**/generation certificada (namespace, index,
  runtime/device,
  lease/COMMIT) e antes do WAL byte 0 deixa invalidação extra segura; retry usa fencing generations/
  nonces/budget/attempt/epoch/txn diferentes e ainda casa o mesmo stable feasibility context. Reusar
  permit antigo ou preservar fencing field na projection falha; normalizar identity generation,
  source/target/path/manifest/ordinal ou outro stable field também falha. `_IrrevocableWalPermit` é
  indisponível nesse gate e só nasce após append+barrier+direct terminal proof; mutante que o antecipa
  ou o inclui no context/proof/seed morre;
- dois processos congelam drafts sobre WAL generation `g`; exatamente um CAS `g→g+1` obtém
  `_WalMutationPermit` e pode chamar `append_planned(mutation_draft,append_draft,permit)`, o outro
  recusa/replana sem byte.
  Kill após slot `g+1` certificado e antes do byte deixa somente incremento extra; retry usa
  predecessor `g+1`/successor `g+2`. Mutantes current==predecessor no append, successor off-by-one,
  permit de outro fingerprint/range, nonce no draft ou permit anterior ao fingerprint falham;
- para cada kind non-append, dois `WalMutationDraftV1` usam o mesmo range/g mas source bytes,
  cut boundary, target, quarantine/ledger ou barrier plan diferentes; permit adquirido para A não
  executa B. Dois processos/CAS deixam um vencedor e um stale sem byte/remove/rename. Mutantes omitem
  full fingerprint, trocam kind, aceitam pin snapshot antigo ou chamam low-level com args soltos falham;
- compounds tail kinds 5/3 derivam child permits somente na matriz/ordinal: phase0 exercita temp+
  publish target8 e ledger COW target7; phase1 exercita recycle/temp/fill/publish target6/release.
  Parent permit passado direto, child target trocado/reusado, standalone generation omitida ou child
  usado fora do executor falha antes de cada syscall, enquanto as duas edges parent permanecem exatas;
- forensic ledger usa somente COW kind2. Event grande é escrito no temp em prefixes k=0/1/middle/full;
  kill em cada byte/data barrier/atomic replace/parent barrier/direct cert classifica exatamente os
  states 1..5 e retoma sem tocar parcialmente o active ledger. Mutante APPEND_EXISTING, terceiro
  prefixo, temp identity/path novo ou adoption sem progress proof 340 bytes falha;
- provider hostil comprova que clock/CSPRNG só rodam ao capturar `WalEntropySeed` fora de participant/
  lease/guards/COMMIT. Sob COMMIT, `WalPlanSeedFinal` combina entropy já capturada com fences current;
  mutantes que capturam fencing/permit no entropy, chamam provider sob section, conservam final seed
  após cleanup ou montam draft antes do fresh lease falham;
- mutantes adicionam RECOVERY/CATCHUP ao enum `WriteSnapshotProof`, fabricam predecessor proof ausente
  ou deixam `RecoveryApplyAuthorityV1` emitir novo grouped COMMIT/refill: falham. Kill em cada apply/
  barrier/publication mostra que WalPublication/RuntimeDevice proofs só nascem completas e depois a
  primeira escrita ordinária constrói nova WriteSnapshotProof;
- golden receipts exercitam kinds 1..14 e cada consumer mapeado: JSON/tuple host, SHA sem locator/
  direct read, kind/body swap, lista parcial/reordenada, result SUPERSEDED colapsado, barrier não
  repetida, failed-temp de outro target e inventory cache-only recusam. Kind 9 congela subject
  `80+path`, parent `112+path` e receipt único; incluir receipt no subject, zerar o field para obter
  outra preimage ou trocar ordinal/class/policy/path/capability recusa. Fence/scan/checkpoint/frontier
  usam somente seus codecs dedicados/envelope integral; max+1/0xffffffff recusa pre-allocation;
- cada MULTI_STEP persiste e recomputa `BoundOperationSubstepContextV1` body+SHA. Mutantes usam
  ZERO32/body ausente, hash opaco, body/locator/horizon/source inventory/target effect/apply-plan
  divergente ou a fórmula BIND em INDEX_REBUILD; UNITARY com qualquer byte/campo nonzero recusa.
  Mutantes incluem BEGIN/COMMIT/trailer/context/CRC/proof/fingerprint nos effect/apply hashes e criam
  self-cycle; todos recusam antes do WAL;
- hard kill depois de MARK WAL barrier/COMMIT, depois do MARK apply, depois do REBUILD COMMIT e antes
  do CLEAR reinicia em processo sem RAM e reconstrói o plano completo: BIND usa marker/core pelo
  `bind_plan_source_digest`; rebuild usa `OperationSubstepManifestV1` inline do MARK. Missing/corrupt
  source bytes, digest sem preimage, UUID/LSN source errado, body acima de u32/record/batch/segment ou
  checkpoint/recycle atravessando o pending floor recusam. Mutantes omitem/trocam database UUID,
  index identity, rebuild nonce ou base-plan digest e não conseguem recomputar outer/full-plan. Opaque
  SHA jamais autoriza replan. Backend sem recipe bijetiva e mutante que inclui future raw page/WAL,
  LSN/page_lsn/epoch/txn/UUID/CRC no MARK recusam pre-MARK;
- checkpoint com MARK multi-record NONTERMINAL escolhe o terminal COMMIT anterior inteiro como active
  ceiling, nunca `MARK_lsn-1`; map/projeção divergente entre passes reinicia, e system floor impede
  recycle/CLEAR concorrente. Após CLEAR válido, a entry vira TERMINAL/CLEAN, deixa de limitar Q, entra
  no release set integral do checkpoint e somente N actions14 sequenciais removem keys/floors antes do
  recycle. Kill C1→action14 impede C2 pelo pending gate. Mutantes omitem o min, tratam terminal como
  STALE, saltam g→g+N, checkpointam no interior do MARK, omitem carry-forward, persistem SHA errado ou
  TTL-evictam o pin falham;
- parametrizar requested_Q em baseline, SEGMENT_HEADER, BEGIN, primeiro/meio/último effect,
  `COMMIT-1`, COMMIT e além de P: somente a maior boundary autenticada ≤ request entra no min e no
  `RequestedCheckpointBoundaryProofV1`. Mutantes usam request raw, locator de outro txn, cacheiam a
  boundary entre passes ou publicam Q interior; todos recusam/reiniciam sem checkpoint/recycle;
- checkpoint-C do cutover produz `V1_COMMIT` com UUID ZERO16 e fence transaction/raw terminal exact;
  checkpoint posterior V2 produz `V2_COMMIT` com UUID nonzero BEGIN=COMMIT. Mutantes inventam UUID no
  V1, zeram UUID V2 ou trocam record format/kind e recusam;
- dois processos tentam MARK/rebuild/DROP/ALTER/CLEAR na mesma identity e somente o exact next
  ordinal do `PendingOuterOperationMap` avança; segundo pending ou branch conflitante é damage/refusal.
  Mutação row/index da mesma identity fica bloqueada; commit estrangeiro fora do scope entre MARK e
  REBUILD — e entre base freeze e MARK — pode avançar P/raw target, força novo bound actual/draft e
  passa sem mudar outer/full/base. CLEAR antigo,
  adiantado ou sem MARK→REBUILD compatível falha; cold open com dois pendings conflitantes não escolhe;
- resume BIND em novo processo/fresh lease usa `(epoch,txn_id)` diferente e passa a projection somente
  se o raw validator provar equality do par em todos os records BEGIN..COMMIT e distinctness na history; mutante que
  normaliza antes de validar grouping/mismatch morre;
- BIND exatamente no segment roll emite `SEGMENT_HEADER(txn_id=0)` antes do BEGIN; sua projection
  separada preserva roll/type/flags/order/length/LSN, enquanto somente BEGIN..COMMIT compartilha o
  grouped pair. Mutantes que incluem header no equality, usam txn_id nãozero ou o intercalam recusam;
- config omitida escolhe mode `compatible`/lease size 64; `require`, unknown/case/whitespace e tipos
  não-exatos cobrem success/refusal pre-I/O sem coerção;
- DatabaseIdentity V2 missing/truncated/trailing extension, prefix/full CRC, unknown feature bit,
  C/generation errado; decoder v1 chega a version mismatch pelo CRC legado válido;
- WAL format v1 com flags 0, 1, 2, 9, 0x1234 e 0xFFFF preserva bytes/semântica opaca; V2 sob meta V1,
  V1 pós-C, transaction/roll cruzando C e old reconnect após cutover recusam;
- tipo unknown V2 com flags `0,1,2,3,4,5,0xFFFF` classifica, nessa ordem, SKIPPABLE,
  UNSUPPORTED_REQUIRED e MALFORMED para todos os demais; mutante que mascara REQUIRED ou testa bits
  antes da igualdade exata falha;
- heap v2 missing/duplicate identity, identity fora do primeiro slot, slot real com reserved ID;
- META/CATALOG/INDEX v2, header downgrade, T/generation mudando, T errado por um LSN;
- capacity exatamente no limite e um acima; legado v1 com IDs reservados readable-compatible mas
  DML/DDL typed-refused pre-mutation e upgrade recusado;
- duas tabelas v1 com o mesmo número de record ID fazem upgrade sem renumeração; duplicata intra-table
  continua dano;
- corrupt checksum, equal semantic com seq diferente, equal LSN/different semantic, higher unproven;
- inventory I/O refusal, orphan com `NO_CSN`, provisional ou CSN real, duplicates/cross-owner/refs;
- oráculo v1 `40a4201`: identidades de findings, ordering, accounting e zero maxima incompletos.

### 17.2 WAL/recovery/checkpoint

- begin multiprocess com `Q<P` publica action15 com `snapshot_lsn=P` e
  `ProtectedWalRangeSetV1.checkpoint_q=Q`; action16 preserva P e zera length/SHA/bytes. Fixtures
  0/1/N ranges, ACTIVE_PREFIX com append disjunto, owner-death e codec truncado/extra/CRC/file-id/hash/
  gap/overlap provam que outro processo enumera a preimage; mutante que grava snapshot Q, aceita SHA
  opaco ou permite truncate/recycle/rename/delete intersectante falha antes do WAL/handout. Todo
  mutation draft embute o `ProcessRegistryStateV1` current e usa o único domain de pin snapshot;
  ZERO, raw-state SHA sem domain, ReaderRegistry digest ou Prefix payload divergente falha pre-CAS;
- hard kill após WAL barrier e após cada apply antes de commit.state: `begin` detecta gap e somente
  completion exata publica; read-only recusa byte-identical;
- power-loss fixture preserva WAL+commit.state P mas restaura heap/catalog/index pages a Q/estado
  parcial: frontier existe, porém RO P>Q recusa e RW exact-redo/certifica Q→P antes do handle;
- mesma fixture termina em `RANGE_RESERVATION` publicada com heap0 em Q: refill nunca usa F antigo,
  exige device catch-up e `Heap0MaterializationCertificate` exato antes de calcular start;
- incomplete terminal antes/depois de BEGIN/cada effect/fragment: write-capable trunca somente da
  boundary provada, faz WAL barrier+cold rescan e então publica; incomplete interior e unknown
  REQUIRED nunca são truncados;
- segment roll exatamente no transition/control/COMMIT; append estrangeiro entre plan/append;
- txn ID repetido em epochs, missing/duplicate terminal, record pós-terminal, control pretransition;
- FORMAT/RESERVATION aceitam somente
  `BEGIN_V2→WRITE_PAGE(heap0)→IDENTITY_CONTROL→COMMIT_V2`; permutar page/control, omitir BEGIN,
  intercalar effect/SEGMENT_HEADER ou acrescentar record recusa em live/recovery/verify. CONSUMER
  exige BEGIN/COMMIT, zero control e zero heap0-counter target;
- unknown v2 required/skippable, bit inválido; flags v1 nunca consultados; CommitPayloadV2 manifest
  omitido/extra/digest divergente;
- CommitPayload com touches/partitions/terminal inconsistentes;
- checkpoint antes, em e depois de T; transition marker já reciclado somente após horizon certificado;
- crash antes/depois do slot alternado de `CheckpointIndexStateV1`, sua barrier, commit.state e
  recycle, inclusive temp/replace/parent-directory barrier: sempre existe exatamente um slot que casa
  com Q; missing/divergent manifest recusa;
- action24 é exercitada como substep code13 antes do checkpoint code2: kill após map-slot write,
  map barrier, header write/barrier/direct cert e antes da planning pass recompõe
  `SystemFloorInstallBatchPlanV1` do source/target slots+history e nunca perde o floor. Mutantes que executam
  action24 sem base/transition/admission, a colocam no plan code2, armazenam o base SHA em sua própria
  preimage ou instalam reader floor antes dela falham com zero publication não autorizada;
- carrier alterna install A, install B e release de C até sobrescrever os dois source maps antigos;
  cold reopen ainda recomputa `SystemFloorInstallEntryBaseV1` de A/B diretamente de cada record. Um
  record com batch/global-historical-map SHA opaco, pending-entry projection/entry-base field
  divergente ou gate que permite overwrite durante uma
  publication pendente falha; nenhum record live depende de source slot histórico;
- um full plan BIND/rebuild com duas persisted identities e o mesmo outer/full instala duas floor keys
  distintas ordenadas pela entry-base SHA; mutante que deduplica por `(outer,full)`, usa raw-record SHA
  como key ou omite a pending-entry projection perde uma entry e falha antes da map publication;
- truncate multissegmento mata o processo depois de **cada** newest-first recycle, empty-number
  reservation, roll-temp create, cada partial write, data barrier, publish/rename, old-cut release e
  cada parent barrier. Reopen classifica exatamente um prefixo do `WalMonotonicProgressPlan`, repete
  somente barrier incerto/remaining steps e chega ao mesmo target; no fill-temp length 0 é kind1,
  strict interior é kind2 e full pós-write é kind3 da step 4; target integral do plan é kind4 cold
  ou kind5 apenas pós-cert live, nunca dois kinds. Segment missing fora da ordem, partial bytes não canônicos,
  temp+final simultâneos e identity/hash trocado recusam;
- RECYCLE_PREFIX com [A,B,C] mata depois de cada unlink, retorno Windows deferred e parent barrier:
  latest checkpoint manifest preserva o plan; somente absent `[0,k)` + current `[k,N)` retoma.
  Novo checkpoint antes do terminal direct cert recusa, e mutante que agrega sem progress parent ou
  aceita subset com gap falha;
- fixtures de namespace incluem PLANNED/CURRENT regular empty com `raw_length=0` e SHA256(empty)
  nonzero para reservation/temp; ZERO32, ABSENT disfarçado ou tree fields nonzero recusam;
- transaction em S fica pausada entre statements enquanto checkpoint/reconcile tentam avançar: Q é
  limitado a S, REMOVE conserva boundary igual a S e a próxima statement devolve resultado exato;
- first retained segment com número >1 é prefixo histórico, gap entre retained segments é dano;
- page0 à frente do checkpoint sem committed exact image;
- replay idempotente que não aplica ainda retorna touched page para write-back requerido;
- `SUPERSEDED_COMMITTED` dirty retorna touched page; cold reopen vê a imagem posterior;
- `CommitRedoResultV1` golden N=0/1/max-combined congela `60+188*N+72*B`, order/CRC e os tags 1/2/3;
  RuntimeDeviceReplayProof REPLAY/OWN embute os bytes+SHA exatos. Omitir touched/barrier, trocar
  target kind/result, aceitar 4/5, usar history ZERO em superseded, truncar ±1 ou anexar bytes falha;
  OWN exige predecessor runtime proof integral/source exato, enquanto CHECKPOINT_DIRECT/REPLAY exigem
  predecessor ZERO e checkpoint Q como source. CHECKPOINT_REBASE exige predecessor integral retida
  exclusivamente para rebind,
  `old_Q<new_Q<=P`, manifest source/target/P exato, frontier FULL_SCAN nova e result vazio; trocar as
  zero rules, aceitar a predecessor como proof current depois de Q mudar, omitir o state
  REBIND_PREDECESSOR_ONLY, usar frontier/Q antigos, saltar P anterior ou inserir redo falha;
- long reader fixa S e força checkpoint `old_Q<new_Q<P`: kill antes/depois de candidate/commit.state,
  action13, action15, scan, seal e action17 deixa checkpoint durável mas nenhum token novo até rebind.
  Owner live termina com CHECKPOINT_REBASE sem replay/action16; cold reopen faz REPLAY new_Q→P. O caso
  `new_Q=P` continua rebase live e CHECKPOINT_DIRECT somente depois de perda cold da predecessor;
- failure em list/read WAL preserva `INCONCLUSIVE` vs `CORRUPT` sem writes.
- recovery/checkpoint certification drift depois do primeiro COMMIT exige release/retry; fresh lease
  é observado, retained/COMMIT→lease mutants falham; recovery nunca recria grant.
- scanner fica pausado por Event fora de COMMIT com `WAL_SCAN_PIN`: append disjunto força retry do
  seal; checkpoint/recycle limitam/deferem; recovery/truncate overlap saem de COMMIT e retornam
  `WAL_PIN_CONFLICT` pelo budget. Nenhum path espera condition sob COMMIT, deadlocka ou altera range;
  mutante wait-while-COMMIT falha pelo gate sem sleep.
- BEGIN_READ instala exatamente uma key e executa 15→16→17. Proof pre-action16 não decodifica como
  `WalPublicationProofV1`; proof final embute CAS pre G→post G+1 e converted entry P. A própria
  conversion não invalida o token, outra reader key pode registrar/fechar sem invalidá-lo, mas remover/
  alterar a own-key o revoga. Mutantes action10/11, segunda registration, proof pre-CAS, snapshot G
  usado como current G+1 e close diferente de action17 falham por cardinalidade/codec/liability;
- FULL_SCAN de cold/recovery/checkpoint-rebind instala exatamente uma key kind4 com liability 1 e executa 15→17; action16,
  CONVERTED state, wrapper público ou liability 2 nessa rota recusa. Gates MAX-1/MAX-2 provam reserva
  terminal antes do install e kill/owner-death conclui action17 sem tocar bytes da database;
- DAG de proof é exercitado em genesis P=Q=0, cold P=Q, commit P→P' e recovery H: frontier nasce antes
  de device; somente begin posterior executa action16 e cria wrapper. Commit/recovery com zero new
  reader registration não podem emitir o codec action16-shaped; RuntimeDeviceReplayProof que aponta ao
  wrapper, wrapper calculado antes do device, placeholder/fixed-point ou frontier que contenha successor
  SHA falham. DERIVED frontier seguida de begin reutiliza a mesma frontier current em vez de forçar
  FULL_SCAN SHA incompatível;
- `GenerationTransitionPlanV1` golden N=1/N=2 congela entry 156 e record lengths 296/452 com EOF/CRC;
  entry/record length 152, truncation −1,
  trailing +1, semantic target kind trocado e checkpoint source apontando ao composite core recusam.
  O DAG base→transition→core é recomputado e mutante self-reference não encontra fixed point aceito;
- semantic code11 com duas tail phases congela exatamente
  `(outer=composite_identity,full=source=tail_mutation_composite_plan_sha256)`. Trocar outer por event/
  quarantine/publication identity, zerar full, usar digest do transition como full/source ou inserir
  transition/successor bytes no tail plan falha no DAG antes do primeiro WAL mutation permit;
- Terminal floor release sem MARK+BOUND/CLEAR+checkpoint incorporation, reason diferente de 1 ou
  tentativa abort/pre-effect recusa com zero action14/map/header write.
- checkpoint com três release keys persiste recipe/M0 integral; kill após action14 step 2, quando os
  dois map slots já são M1/M2 e M0 não existe no carrier, reabre a recipe do manifest, recompõe M0→M1
  →M2→M3 e conclui somente step 3. Manifest com apenas record SHA, recipe ausente/truncada, outro
  checkpoint antes de M3 ou subset fora do prefixo falha antes de action14/recycle;
- phase 0 tail morre imediatamente depois da stable CAS e antes do primeiro syscall, depois fresh lease
  e novamente antes do syscall: em ambos reabre HELD_OPEN/g, recompõe event/plan bit-identical e usa a
  continuation sem novo CAS; pending ausente/trocado, event com current fresh ou limpeza antes do
  direct cert falha. SUFFIX_CUT phase 1 e PREFIX_RECYCLE morrem depois de tornar visível sua stable CAS e
  antes do primeiro syscall: reopen classifica kind1/k=0 + stable adopted e usa auxiliary resume, sem
  INCONCLUSIVE. Para cada step com barrier, kills imediatamente antes/depois da barrier classificam a
  mesma kind3 predecessora e repetem a barrier; boundary terminal cold é sempre kind4, kind5 só live
  pós-direct-cert. Mutantes que retornam kind1 na mesma boundary ou sobrepõem kind3/4 falham;

### 17.3 OCC/DDL/rows/índices

- DDL+node, DDL+edge com refs, DDL+index+row e overflow na mesma txn;
- tabela catalogada sem extent; primeira materialização sem placeholder;
- two-OCC com reservation própria incorporada, reservation alheia válida e page0 genérica alheia;
- forged identity por todas as portas públicas e por mutação direta dos sets;
- drift no post-token antes de `context.release`, falha no terceiro stage e schema root early-return:
  todas as superfícies voltam ao mark e zero resultado escapa;
- conflict/retry/KI/SE depois de IDs consumidos: gaps somente, nenhum reuse;
- exact touched pages inclui catálogo page0, chain pages, freed/relinked pages, heap0, index header,
  tail e new pages; omitir qualquer uma falha reopen/verify.
- range database-bound alimenta intents de três tabelas sem refill por troca; reorder/add/remove depois
  do consume recusa e queima; manifest affected cobre todas;
- reservations e commits de outra tabela não avançam relevant horizon; omitir cada uma de INSERT,
  TOMBSTONE, REMOVE/INDEX_RECONCILE, RESET, REBUILD, RECONCILE_ADVANCE, MARK/CLEAR_STALE ou
  BIND_IDENTITY deixa
  manifest inválido; missing row index effect deixa header curto;
- generic, exact, endpoints, uniqueness, verify, inspect, status, vector exact/HNSW e visible_count
  usam SHARED do PRE efetivo ao resultado; reconcile/reset/rebuild/checkpoint/recovery usam EXCLUSIVE.
- a matriz de `WriteSnapshotProof` cobre TRANSACTION e cada kind NO_TRANSACTION permitido (REFILL,
  CHECKPOINT, INDEX_MAINTENANCE, REPAIR, UPGRADE e BIND_IDENTITY): tag/kind cruzado ou desconhecido,
  S/staging não canônicos, digest de plan/ticket/context incompleto, envelope SHA/outer
  operation/attempt divergente, attempt revoked ou nonce reutilizado recusam **antes** do primeiro
  byte WAL; mutantes que removem qualquer check falham.
- max-edge parametriza marker/checkpoint, registry, database/runtime, namespace/carrier, WAL mutation,
  device replay, index mutation e `pool.derived_epoch`: em MAX, o primeiro intento de incremento
  recusa typed e os contadores de registration/slot/device/page/frame/cache writes permanecem zero;
  attempts de DROP/ALTER/CREATE compensatório, rekey, coordinator rebootstrap ou runtime rotation
  in-place recusam com os mesmos zero counters; mutantes clamp-and-continue, wrap/reset ou
  efeito-before-check falham.
  Persisted/shared MAX só admite restore anterior ou cold export byte-identical da source para novo
  DB UUID; apenas epoch process-local admite pool novo, sempre descartando tokens do pool anterior.

### 17.4 Multiprocess/fork

- 3+ writers, mesma tabela e tabelas disjuntas; size 1, 64, batch maior que remainder e near MAX;
- barrier sincroniza reservas concorrentes; intervals persistidos são disjuntos apesar de ordem de
  retorno invertida;
- transaction local abre em S; filho publica P>S numa página disjunta; local tenta commit P'. Antes
  do append, WriteSnapshotProof exige catch-up/materialized=P e fresh revalidation; successor encadeia
  P→P', nunca S→P'. Variante com publication gap aborta a transaction após recovery generation;
- processo morre após reserve, após publish, antes/depois activation e no meio de `consume`;
- gap é injetado depois de REFILLING com waiter bloqueado e close concorrente: recovery revoga ticket,
  muda generation e somente novo outer attempt pode reservar; mutantes stale-ticket/passo4/CAS antigo
  falham. Device-only catch-up generation-equal preserva exatamente um retorno com lease fresh;
- owner stalled/takeover: novo piso começa em F e nunca em remainder antigo;
- dois índices adquiridos em ordem inversa por mutante detectam deadlock; implementação canônica
  progride; dois readers cross-process coexistem; writer espera; owner death/revocation recusa token,
  timeout nunca cai para participant-local;
- fork em cada fence; child não usa/fecha/release objetos do pai; fresh child connect funciona
  `file:` e `:memory:` sem deadlock em locks herdados;
- fake clock avança além de qualquer lease TTL com reader registration, WAL scan pin e INDEX_VIEW
  SHARED ainda vivos: upgrade/cutover permanece recusado até release explícito ou owner-death OS;
  mutante que TTL-evicta qualquer desses objetos ou aceita inventário não vazio falha pre-mutation;
- PortRegistry/sections substituídos no child por assignment e sem tocar segredo/FD herdado.

### 17.5 Performance estrutural

- refill toca heap0/control, não data/catalog/owners; instrumentar device por página;
- reads/writes não crescem com quantidade de rows; CPU cresce no máximo com slots da page0;
- commits unitários uninterrupted medem refills próximos de `ceil(n/size)`; batches/restarts reportam
  burn e refills reais, sem assertiva matemática falsa;
- comparar `tools/measure_concurrency.py` antes/depois: conflitos, p50/p90/p99, throughput, barriers,
  page0 writes e fairness; melhoria não relaxa nenhum gate de integridade.
- afirmar exatamente duas direct page0 reads por identity num attempt estável que retorna, no máximo
  duas num attempt recusado e `2*max_attempts` por chamada, independente de candidates, rows, `k` e
  frontier; stale cache ainda usa a PRE=1 para formar `IndexRefreshCandidateV1` e POST=2 para selar,
  sem read dentro do refresh. Quando o CAS publica g→g+1, successor evidence substitui o token
  provisório por PRE efetivo g+1 e somente esse compara ao POST g+1. Mutantes que comparam PRE
  provisório, fazem terceira read, reutilizam candidate de outro attempt/guard ou renegociam contador
  falham; medir hold/wait/starvation do RW guard e HNSW longa.

## 18. Conflitos normativos com Round 7, W6 e CONTRACT

### 18.1 `ROUND7-PLAN.md` §4

O texto em `:392-449` propõe `_identity_leases` por tabela, `durable_end/speculative_end`, renewal na
própria txn e reuse de IDs do attempt abortado. V7 o substitui porque:

- reuse, mesmo de row abortada, viola a política “uma vez exposto/consumido, nunca reutilizar”;
- advance na mesma txn mantém page0 no consumer e não resolve o piso global DDL+primeira row;
- pool dirty pre-WAL contraria COW e recovery strict grammar já adotados no M1;
- conflito whole-page não basta para provar rebase de reservations agrupadas;
- per-table in-memory state não fecha restart, release uncertainty, fork/PID e format transition.

Também fica substituído `IDENTITY_LEASE_BLOCK=1024`: V7 usa config validada, default proposto 64, e
nenhum número é aceito por argumento apenas qualitativo. O baseline de performance de
`ROUND7-PLAN.md:365-367` é conservado como comparação, não como promessa.

### 18.2 `W6-WRITE-CEILING.md`

Option 1 em `:19-38` diz “redo unchanged” e “no on-disk format change”. Essas premissas são
incompatíveis com o piso global, DDL+row atômico, recuperação verificável, non-reuse e fork safety;
V7 é uma emenda de maior blast radius. Permanecem verdadeiros:

- a seção COMMIT exclusiva continua sendo teto de throughput (`:6-15`);
- leasing reduz conflito/waste, não remove sozinho o teto;
- Windows publication e group commit continuam complementos separados (`:66-79`).

Nenhuma métrica V7 deve anunciar que leasing por si só elevou throughput máximo.

### 18.3 `CONTRACT.md` §8.5 e §8.6

O protocolo frozen em `CONTRACT.md:657-672` é emendado apenas nos pontos necessários:

- DatabaseIdentity/WAL ganham o fence v2/C da seção 5.3; flags WAL v1 permanecem opacos;
- begin só publica snapshot após frontier + `RuntimeDeviceReplayProof` + wrapper `WalPublicationProof`; gap ou device
  atrás força recovery/catch-up/refusal;
- step 3.3 usa grouped history e rebase proof fechado;
- steps 3.4–3.5 usam `WalAppendPlan` exato e preservam WAL barrier;
- step 3.6 usa preflight/apply enum e exact touched pages;
- todo index reader/mutator usa RW `INDEX_VIEW` e manifest total; participant-local não é fence;
- statement só transfere staging depois do post-token e sob rollback mark externo;
- data files continuam sem fsync para commit ordinário (`:669-670`), mas format/reservation fazem heap
  barrier antes de autorizar range/formato; successor device proof é apenas runtime-local e cold open
  sempre a perde/reconstrói desde Q;
- step 3.7 permanece publication depois de aplicação válida.

Recovery de `:690-702` continua WAL-authoritative e no-undo, porém passa a preservar transações
agrupadas e controls v2, usa fresh lease/two-pass e nunca ressuscita grant. Refuse/read-only continuam
non-mutating. Qualquer mudança além disso exige nova emenda explícita.

## 19. Critérios de ACCEPT conjunto

O design só está aceito quando Codex e Claude, em revisão independente sobre o mesmo SHA-256 do
documento, e depois sobre o mesmo `P_integrated` registrado no Gate 0, confirmarem todos os itens abaixo:

1. os 11 blockers, L1–L4/O1/O2, R1–R7/N9–N11 e A1–A10 têm mecanismo único, sem
   contradição entre seções;
2. `WalAppendPlan` é implementável sem preview/retarget/clock depois do freeze e migra commit genérico;
3. DatabaseIdentity V2/C e WAL format v2 fecham o cutover; todo flag WAL v1 permanece opaco e seus
   fixtures `0x1234`/`9` ficam byte-identical; nenhum record v2 aparece antes do cold fence;
4. required/skippable, manifest total operations 1–13 (COMMIT 1–12; checkpoint-only 13) e `CommittedHistory` conservam informação
   suficiente para phase, OCC, supersession, relevant horizons e recovery;
5. todos os callsites identity/decoder, readers/mutadores de índice e flush produtivos estão
   enumerados e testáveis, incluindo verify/inspect/status/reconcile/recovery/checkpoint;
6. verifier v1 aceito em `40a4201` permanece oracle e v2 não false-cleana estado incompleto;
7. COW puro cobre data, overflow, relink, hints, first extent, catálogo e todos os índices;
8. `WalFrontierCertification` impede snapshot sobre COMMIT/incomplete durável acima de commit.state,
   `RuntimeDeviceReplayProof` impede snapshot sobre pages atrás de P e `WalPublicationProof` liga as
   duas a uma reader key fresh; write-capable completa Q→P e
   read-only exige P==Q sem bytes alterados; todo mutador consome `WriteSnapshotProof` com predecessor
   materialized exatamente no P pre-append e successor nunca pula `(S,P]`; recovery/gap/catch-up usam
   authorities separadas, jamais uma WriteSnapshotProof circular, e só produzem predecessor proofs ao
   final; checkpoint live que muda Q fecha novas operações até frontier fresh + CHECKPOINT_REBASE,
   enquanto cold-open jamais usa esse atalho process-local;
9. genesis, legacy bootstrap, prefix-only/checkpoint/T/read-only formam máquinas totais, inclusive
   marker-first/resume, parent-directory durability, damage e I/O inconclusive; root vazia substituída
   pré-marker pode ser adotada sem continuidade inventada, enquanto PREPARING binda root+parent
   identities e torna qualquer replacement posterior inconclusive;
10. capability, raw set validation, manager-owned pos/nonce/CAS, leases, sections e fork/PID não deixam
   caminho alternativo;
11. overflow, insufficient remainder, abort, close, hard crash e takeover nunca reutilizam ID;
    consumer nunca avança F e recovery/checkpoint nunca recriam grant;
12. `StatementStagingMark` cobre todas as superfícies; post-cert precede release e qualquer falha
    restaura o mark sem rewind de identity;
13. shared stale/local poison são independentes; HNSW exige token completo + derived epoch; header
    clean estrangeiro não false-heala cache/graph local; o epoch global ao pool usa ordem
    `POOL_DERIVED_EPOCH`→`INDEX_CACHE_REFRESH`, CAS successor antes do primeiro discard e loser/MAX
    com zero discard/cache mutation;
14. RW `INDEX_VIEW` é interprocesso: readers SHARED coexistem, todo mutador EXCLUSIVE, guard cobre a
    materialização inteira e mutation generation checked/fail-closed fecha ABA entre scopes mesmo com
    LSN/bytes repetidos;
15. recovery/checkpoint/repair/upgrade usam certify → exit → fresh lease → EXCLUSIVE → COMMIT
    revalidate/apply, sem retained lease, pin/graph/callback/métrica sob COMMIT;
16. `operation_deadline_ms` universal e budgets INDEX/INTEGRITY têm nomes, defaults, limites,
    mapping por porta, monotonic clock, attempts exatos e cancellation precedence; o único
    `OperationBudget` envelope fica imutável e só o outer cria `OperationAttempt(1..N)` frozen, sem
    mutation/reset/amplificação em nesting;
17. performance é descrita como bounded page0 CPU/zero data scan e 2 direct reads por índice/attempt
    estável, bounded por `2*max_attempts` por chamada, não O(1) nem ordem global; hold/wait/starvation
    do RW guard, contenção do epoch global process-local e o slot/barrier/direct-cert por
    `WalMutationDraftV1` irrevogável são medidos;
18. generic row/DDL writer WAL V2 permanece publicamente tombstoned nos Slices 1–4 e só é habilitado
    no Gate 5 após COW/virtual allocator/zero-data-temp/permits/rollback marks completos; writer
    identity v2 permanece posterior aos gates 1–4/5 conforme slices 6–7;
19. upgrade preserva IDs cross-table, revalida `UpgradeDigest` sob lease e só é executado pelo runbook
    offline reconhecidamente não provado por booleano;
20. feasibility integral prova a normalized projection de todos os full BINDs antes do primeiro
    marker/fence sem congelar seed volátil; excesso estrutural tem zero efeito, e resource failure
    pós-fence só pausa/resume pelo digest/space plan durável e UUID committed; cada grouped
    NO_TRANSACTION substep possui operation identity/ordinal estável no full plan e UUID próprio;
    resume nunca renumera ordinal, campos logical-stable nunca são normalizados, enquanto raw history/
    actual certification e attempt fencing são tagged+recalculados e nunca comparados como target
    feasibility; `bind_plan_source_digest` é predecessor acíclico e nenhum
    final feasibility/marker SHA entra em trailer/template/projection;
21. toda safety generation/derived epoch recusa no MAX antes de qualquer efeito, sem clamp/wrap/reset,
    persisted/shared exhaustion não possui DDL/rekey/runtime bypass in-place e só admite restore/cold
    export para novo DB UUID; genesis/cutover pre-marker provam `GenerationCapacityPlanV1` total e
    cada attempt preserva worst-case burns + minimum completion path sem renovar allowance; conflito
    de WAL_SCAN_PIN nunca espera sob COMMIT e
    OfflineMaintenanceSession nunca trata TTL como release de reader/pin/guard;
22. flags unknown V2 e a ordem FORMAT/RESERVATION/CONSUMER têm classifiers totais idênticos em live,
    recovery, verifier e feasibility;
23. Gate 0 prova por ancestry que o parent integrado contém M1/M2/M3/C13/lint/oracle; nenhuma slice
    parte do audit SHA isolado nem re-cherry-picka milestone ancestral;
24. todo multi-step carrega preimage recomputável do bound actual e source durável completa dos
    substeps restantes; pending por identity serializa MARK→REBUILD→CLEAR e bloqueia qualquer mutação
    incompatível até o terminal;
25. entropy seed pre-section/final seed sob COMMIT e handshake draft `g→g+1`→permit→append são
    acíclicos, exact, single-use e toleram kill/contenda sem byte sob generation errada; toda mutação
    non-append usa o mesmo `WalMutationDraftV1` fingerprint-bound sem cross-kind/cross-plan permit;
26. full tests, mutation battery, hard-kill multiprocess e cold reopen verifier passam sem xfail novo.

## 20. Sequência de implementação — oito slices V7

Cada slice é um ou mais commits mínimos coerentes, revisado por builder + critic independente sobre
o mesmo parent SHA. Nenhum commit mistura docs/infra alheia, nenhum gate é marcado por xfail e nenhum
writer posterior é cherry-picked antes de seus pré-requisitos. Os oito slices permanecem, mas a ordem
agora torna o fence WAL e a estabilidade de leitura pré-condições reais.

### Gate 0 — parent integrado e ancestry (bloqueia todas as oito slices)

Antes de criar a branch da Slice 1, o coordenador registra o SHA completo de `P_integrated` e exige
sucesso de `git merge-base --is-ancestor <required_sha> P_integrated` separadamente para M1, M2, M3,
C13, lint e o oráculo `40a42014913394d00f3446316edba68569f26d44`. `git status --porcelain` deve
estar vazio, não pode haver unmerged path, e suites/lint aceitas desses milestones precisam continuar
verdes no mesmo parent. Falha/missing ancestry é blocker de coordenação: faz-se o merge no milestone
integrado e repete-se Gate 0; uma slice V7 nunca “corrige” isso por cherry-pick, cópia de arquivo ou
branch a partir do audit SHA M1.

O acceptance record fixa `(V7_document_sha256, P_integrated_full_sha, six ancestry results,
toolchain)` antes do primeiro commit. Como as line refs V7 foram auditadas em M1, Gate 0 também gera
um delta inventory read-only M1→`P_integrated` para cada callsite citado e confirma o símbolo corrente;
linha movida não altera a regra nem autoriza aplicar patch pelo número antigo. Todas as slices formam
uma cadeia descendente desse parent e da slice anterior.

### Slice 1 — codecs/fences de leitura, proof e coordenação (zero emissão nova)

- DatabaseIdentity V2/WalFeatureFence codecs, prefix/full CRC, C/generation/bits e direct-first open;
- WAL format v2 decoder/header grammar total (`unknown flags == 0/1/outro`) e transaction order,
  mantendo **todos** os flags v1 opacos e round-trip antigos;
- `CommittedTransaction/History` grouped, scan evidence e `WalFrontierCertification` read-only;
- begin recusa gap/unknown-required/incomplete sem mutar; writer de records continua exclusivamente v1;
- RW `INDEX_VIEW` backend/capability, shared mutation generation checked/fail-closed, canonical identity/order,
  owner-death/timeout, ainda sem migrar mutadores;
- `WAL_SCAN_PIN` carrier/probe nonblocking, checked generations e max-edge refusal antes de efeito;
- `OperationBudget`, configs/status, PID/generation-first e public writer tombstones.

Commits mínimos: (1a) identity/WAL codecs + frozen v1 compatibility; (1b) grouped history/proof;
(1c) RW coordinator/permit; (1d) configs/PID/status/tombstones.

**Gate 1:** old decoder recusa identity V2; new decoder preserva v1 bytes; meta V1 + record V2 e
snapshot sobre gap são recusados. Nenhum código produtivo consegue emitir WAL v2.

### Slice 2 — planner, bootstrap/cutover e binding duráveis (heap ainda v1; writer genérico fechado)

- `WalEntropySeed` pre-section, `WalPlanSeedFinal`, `WalAppendDraft`/`WalMutationDraftV1` e handshake
  generation `g→g+1`→`_WalMutationPermit`→`append_planned(mutation,append,permit)` substituem
  preview+retarget no commit
  genérico;
- apply enum/fingerprint/supersession, full preflight e exact touched pages;
- `CommitPayloadV2` + `OperationSubstepTrailerV1`/bound context/source manifest, COMMIT operations 1–12,
  `CheckpointIndexManifest` op 13 e validators bijetivos;
- codecs de `V7BootstrapStateV1`, `PersistedIndexIdentity`, IndexHeaderV2/path imutável e
  `_publish_control_state_durable`/control permits/pending temps, `bootstrap_create_entry_durable`, coordinator carrier create e
  `create_entry_durable_after_wal` com parent-directory barrier/permits disjuntos;
- scanner/recovery/checkpoint/RO compreendem v1/v2 e unknown required/skippable antes de tipo 14;
- harness/porta operator-only ainda internal-feature-gated executa bootstrap offline completo: marker PREPARING/PREPARED, baseline legado
  `(0,0,Q=B)`, `WAL_COORDINATION`, namespace global e todos os carriers per-index duráveis antes do
  primeiro append de fence;
- feasibility integral read-only de todos os full BINDs e `CutoverSpacePlanV1` são certificados antes
  de PREPARED/fence; oversized recusa pre-marker e pressure pós-fence pausa/resume idempotente;
- `enable_wal_semantics_v2`: transaction fence v1, meta barrier/direct cert/publish, terminal close,
  cold reconnect restrito, checkpoint C e marker FENCED;
- para todo índice legado, porta allowlisted offline `BIND_IDENTITY=12` toma carriers/EXCLUSIVE,
  congela path/HeaderV2/pages COW detached, faz WAL barrier e só então cria path durable com
  `_IrrevocableWalPermit`; checkpoint final grava `BOUND_V2`,
  direct-certifica L_terminal ainda em FENCED, publica marker BOUND, fecha maintenance e então o
  terceiro cold reconnect prova marker/slot/catalog/path/header/carriers sem mutar phase;
- generic writer WAL v2 permanece tombstoned mesmo em BOUND durante Slices 1–4; somente fence,
  checkpoint e BIND shapes fechados são emitíveis nesta slice;
- tipo 14 continua ausente/não emitível.

Commits mínimos: (2a) frozen planner; (2b) apply/touched pages; (2c) manifest total/projection parity;
(2d) bootstrap/atomic-directory durability e identity/header/path codecs; (2e) fence/checkpoint C;
(2f) BIND/checkpoint final/BOUND.

**Gate 2:** kill em toda boundary de feasibility/bootstrap/fence/BIND/final checkpoint e parent-directory barrier;
nenhum transaction cruza C; flags v1 0..0xFFFF continuam opacos; nenhum generic writer é chamável;
manifest omission mutants de RESET/RECONCILE/rebuild/BIND morrem; database com 0/1/N índices só
termina no gate após cold reconnect `BOUND_V2` + marker BOUND. Kill pre-barrier de BIND deixa zero
target/temp; near-limit/oversized/resource-resume e permit/bootstrap-confusion mutants morrem.

### Slice 3 — read/write integrity total, staging e índices (ainda sem identity writer)

- begin usa frontier/device e só então cria o wrapper público; gap completion e device catch-up/recovery two-pass com fresh lease;
- migração de **todos** os readers e mutadores da tabela 8.4.7 para SHARED/EXCLUSIVE;
- relevant horizons pelo manifest total; remove published global/advance de índice alheio;
- raw direct page0 tokens, clean-frame discard/derived_epoch, shared stale/local poison;
- HNSW certificate completo; remove `_graph_mark == built_through_lsn`;
- `StatementPreflight`, pending result, outer staging mark e rollback de todas as superfícies;
- maintenance index-only logged, STALE-before-mutation, RESET/rebuild/reconcile/clear fenced;
- `PendingOuterOperationMap` serializa MARK→REBUILD→CLEAR por identity e retém source/manifest até o
  terminal, inclusive cold recovery;
- recovery/checkpoint/repair/close/explicit flush usam fresh lease + EXCLUSIVE e ordem da seção 10;
- verify/inspect/status/read-only entram no mesmo inventário.
- `ColdOpenDeviceGate`, catch-up Q→P, P==Q read-only, reader-registry floor de checkpoint/reconcile e
  device/WAL carriers anti-ABA passam a ser obrigatórios;
- harnesses internos podem emitir somente shapes allowlisted de recovery/checkpoint/index maintenance
  necessários aos gates; row/DDL generic public continua typed-unsupported e tombstoned.

Commits mínimos: (3a) gap begin/recovery; (3b) relevant horizon/manifest projection;
(3c) RW read scopes + cache/HNSW/poison; (3d) mutation permits/admin routes;
(3e) statement staging atomicity; (3f) checkpoint/RO/cold-device/fresh-lease closure;
(3g) allowlist interna + tombstones públicos estruturais.

**Gate 3:** hard-kill pre-publish, ABA same-LSN, seq-wrap mutant, dois readers + writer, dirty/pinned
foreign cache, post-token antes de release, admin inventory, cold Q→P, paused-reader floor e
deterministic budgets passam. Mutante que habilita generic writer público no Slice 3 falha porque
planner heap/index ainda não possui COW completo; zero public writer V2 está habilitado neste ponto.

### Slice 4 — verifier/repair v1 e certification total (ainda sem leasing)

- validar/preservar o oráculo `40a4201` **já ancestral por Gate 0** e sua identidade de
  findings/accounting; zero cherry-pick/reincorporação do commit;
- classifier total v1/v2 fixtures, branch-before-decoder e no-false-clean sobre gap/inventory I/O;
- repair exclusivamente counter raise COW/WAL/exact durable/direct cert, pelo fluxo fresh lease;
- upgrade dry-run produz `UpgradeDigest` completo, mas format/reservation/consumer continuam tombstone;
- full mutation suites de WAL fence, manifest, apply, read-view, INDEX_VIEW e staging.

Commits mínimos: (4a) regression/parity sobre o oracle ancestral, somente mudanças V7 necessárias;
(4b) classifier; (4c) counter-only repair; (4d) dry-run/gates.

**Gate duro 1–4:** ACCEPT independente Codex/Claude no mesmo SHA, incluindo A1–A10. Até lá,
generic row/DDL writer V2, genesis heap v2, upgrade heap, IDENTITY_CONTROL, reservation, grant e
consumer lançam typed unsupported antes de qualquer write. Shapes internos são exact allowlist, não
porta pública. Benchmark não libera feature.

### Slice 5 — planner COW puro completo em paridade heap v1

- detached heap/catalog/index state, virtual allocator e dependency-ordered apply;
- insert/update/delete/overflow/relink/hints/first extent/DDL+row/vector/index maintenance;
- structural zero-write/zero-frame-new/zero-data-temp assertion até WAL barrier; paths novos usam
  somente `_IrrevocableWalPermit` pós-barrier;
- two-OCC + staging marks/rollback + fault injection em cada boundary;
- comportamento/resultados públicos v1 permanecem iguais.
- no commit final da slice, depois de paridade COW de **todos** os componentes e gates zero-device/
  zero-frame/zero-temp pre-WAL, habilitar atomicamente o generic row/DDL writer heap v1 em WAL
  v2/manifest; rollback do feature bit/tombstone no mesmo commit é proibido.

Commits mínimos: um por componente COW, depois integração DDL+rows/índices, mutation battery e um
commit final separado de enable atômico.

**Gate 5:** public generic commit permanece typed-unsupported até o último parent; mutantes que
habilitam no Slice 3/4, alocam/dirty/criam temp durante planning ou contornam permits morrem. No commit
final, row/DDL generic v1-behavior usa WAL V2/manifest e prova zero device/frame/temp antes da barrier
para heap/catalog/overflow/index/path; fault injection e rollback marks passam integralmente.

### Slice 6 — heap identity v2 genesis/upgrade, sem grants públicos

- implementar/exercitar sob gate internal/operator o `V7_GENESIS` completo da seção 3.1.2: detached image manifest, marker primeiro,
  meta/WAL/heap/catalog/control/carriers C=T=P=Q=0, parent-dir barriers, resume e BOUND por segundo
  cold reopen; nenhum handle em phase parcial;
- upgrade offline exige `OfflineMaintenanceSession`, C/checkpoint/proof e `UpgradeDigest` recertificado;
- habilitar tipo 14 somente para `FORMAT_TRANSITION`, magic V7, WAL format v2 REQUIRED;
- direct heap barriers/cert, grouped recovery/checkpoint/read-only da transição;
- nenhum range/handle/consumer e nenhum `RANGE_RESERVATION` emitível;
- bootstrap/upgrade ficam internal/operator gate até slice 7 habilitar allocator atomicamente.

Commits mínimos: (6a) heap codecs + genesis image builder; (6b) genesis resume/crash gate;
(6c) format control/upgrade; (6d) transition crash/recovery matrix.

### Slice 7 — reservation, manager, handles e consumers

- `_IdentityOccPermit`, `RebasedPage0Proof`, database-bound single active range e fresh lease;
- `RANGE_RESERVATION` interno; somente ele aumenta F e extents espelho na mesma heap0;
- manager-owned pos, CAS/condition/nonce, activation-after-release, burn/exhaustion/takeover/PID;
- recovery revoga runtime generation e nunca recria grant; checkpoint não persiste remainder;
- consumers multi-table não tocam F, associam IDs depois de reservation e preservam staging marks;
- genesis/upgrade/APIs/config são habilitados atomicamente somente no commit final do slice.

Commits mínimos: (7a) reservation/recovery semantics; (7b) manager/handle;
(7c) consumer/OCC/COW; (7d) atomic enable.

### Slice 8 — fechamento adversarial, operação e performance

- full crash matrix, flags v1/cutover C, manifest operations 1–13 (COMMIT 1–12; checkpoint 13),
  A1–A10 e mutantes históricos;
- multiprocess/fork/owner death/file+memory, reader-reader/writer fairness e hard-kill cold verifier;
- budgets no limite, clock fake, zero sleep, local poison/foreign repair e HNSW graph cache;
- benchmarks: refill, page0 writes, 2 reads/identity/attempt e bound por chamada,
  RW hold/wait/starvation, p50/p90/p99,
  throughput e HNSW longa; sem relaxar integrity;
- CONTRACT/ROUND7/W6 amendments, status/operator docs, backup/cutover/upgrade/restore runbook;
- somente após ACCEPT final: decisão coordenada separada de commit/push/milestone.

Não se antecipa writer identity para medir performance. Database/WAL fence, gap-proof, recovery,
checkpoint, manifest total, staging atomicity, all-mutator RW guard e verifier precisam estar provados
antes do primeiro range persistível.
