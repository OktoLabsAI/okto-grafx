# CE-1 — Registros de controle em dois slots (contrato / ADR executável)

Status: **PROPOSED — produção BLOQUEADA.** Nenhuma linha de `src/` muda com este documento. Ele existe para que a implementação, quando autorizada, seja verificável cláusula a cláusula por testes nomeados, e para que nenhum ganho seja creditado antes de medido no lugar certo.

Origem: `GRAFX_PERFORMANCE_NEXT_STEPS.md` §5b (CE-1 = ST-5 / RC1-C), `GRAFX-CONSENSUS-RESPONSE.md` (ACCEPT-B com portões), handoff `hof_35575263f735440abffb3e14a9fd79fa`. Base de leitura: Grafx `main@4c474b56`. Autor: claude-coder, 2026-08-30. Revisor esperado: codex (criador do handoff), depois o blind critic do processo C13.

Premissa que este documento **não relativiza**: muitos escritores E muitos leitores em processos distintos sobre o mesmo diretório (produto). Nada aqui introduz dono único, lock exclusivo de SO na abertura, bloqueio de leitores por escritores, nem servidor intermediário; a única serialização de escrita continua sendo a que já existe (§8.5 passo 3 = `COMMIT_SECTION`; `LEASE_SECTION` para o read-modify-write da lease).

---

## 0. O que está medido e o que é hipótese (leia antes de qualquer número)

| Afirmação | Estado | Fonte |
|---|---|---|
| Publicação atual (`atomic_replace` = temp + append + barrier + rename POSIX-style + barrier): mediana **12,24–14,86 ms**, p95 17,97–25,65 ms; dentro dela o rename custa 5,5–12,8 ms e o barrier do alvo 2,0–2,5 ms | **[MEDIDO]** quente | `claude-scratch\ce1-spike\run-4..9.json`, ferramenta `tools/measure_control_publication.py` (branch `perf/w1-ce1-spike`, sha256 `4229bf0e…`), 200 amostras + 20 warmups por run, ordens alternadas; verificado por leitura independente 16:19Z |
| Primitivo de dois slots (1 descritor **persistente**, 2 slots de 4 KiB, `lseek+write` posicional + `os.fsync`): mediana **0,099–0,159 ms**, p95 0,144–0,217 ms; speedup mediano por run 93–146x | **[MEDIDO]** quente = **lower bound por desenho** | idem; confirmado pelo autor do spike (msg 16:26Z): os runs 4–9 mantêm o descritor aberto entre publicações |
| Custo do mesmo primitivo com descritor **frio** (open + write + fsync + close por publicação) e sob a política real de LRU (`MAX_OPEN_FILES=64` para ~346 arquivos) | **[A MEDIR]** — em curso pelo codex | ainda sem JSON; o `CreateFileW` de 11,4 ms do W6 item 1 é exatamente este caso |
| Ganho por commit em produção (4 publicações/commit hoje → −50..−60 ms/commit se todas ficarem ≤0,2 ms) | **[INFERIDO]** — **não creditado** | 4 × (12,2–14,9 ms) das medianas acima; depende do caso frio, da política de descritores (§7) e de CE-2 para a 4ª publicação (registro de leitor) |
| Teto de commits cross-process no Windows ~10–13/s → ~30/s | **[INFERIDO]** de W6-WRITE-CEILING.md:9-16 | só se o hold da `COMMIT_SECTION` encolher junto; medir com `measure_concurrency.py` (§10, gate G3) |
| fsync puro neste volume 0,29 ms; barreira do WAL 0,76 ms | [MEDIDO] rig D5 | COMPONENTS.md:774-775 |
| Um `os.fsync` de arquivo de 8 KiB já aberto custa ~0,1–0,16 ms neste NTFS | [MEDIDO] (fase `barrier` do spike) | run-4..9 `two_slot.phases.barrier` |
| Slot de 8 KiB (uma página) vs 4 KiB (spike): diferença de custo | **[A MEDIR]** | expectativa: dominado pelo fsync, não pelos bytes; não assumir |

Regra deste documento: qualquer número sem tag é defeito; qualquer "ganho" citado fora desta tabela é [INFERIDO].

---

## 1. Problema e decisão em uma frase

Hoje cada publicação de registro de controle (`control/writer.lease`, `control/readers/<id>.reader`, `control/commit.state`) é temp + `append_log` + `durable_barrier(temp)` + `atomic_replace` + `durable_barrier(alvo)` (`engine/commit_state_store.py:73-82`; `adapters/coordination_local.py` `_publish…`), e no Windows o rename é o custo (§0). **Decisão proposta:** publicar **in-place**, em um arquivo **paginado** de 3 páginas (0 = cabeçalho do arquivo, 1 = slot A, 2 = slot B), escrevendo **uma página** por publicação com `write_page` seguido de `durable_barrier(arquivo)`; o leitor lê as duas páginas sem lock e escolhe o slot válido de maior geração. Os **nomes** dos arquivos e o **conteúdo lógico** dos registros (v1: `encode_lease_record`, `encode_reader_record`, `CommitState.encode`) **não mudam**; muda o envelope físico e o primitivo de publicação.

Por que páginas e não escrita posicional crua (forma do spike): a porta de storage congelada (CONTRACT.md §4.1, `domain/ports/storage.py:80-83`) **proíbe deliberadamente** um primitivo de escrita posicional ("makes the NTFS beyond-EOF zero-fill signature structurally impossible") e já oferece `allocate`/`write_page`/`read_page`/`durable_barrier`; `write_page` já é um write point rastreado pelo fault twin (`adapters/storage_fault.py:90-100`, `WRITE_POINT_METHODS`), e no `LocalStorageDevice` ele é exatamente `lseek + write` no descritor cacheado (`adapters/storage_local.py:983+`) — o mesmo primitivo que o spike mediu. Assim a emenda fica em §6.1/§6.3/§8.5 (formato e protocolo), **não** em §4.1 (porta), e não há adapter, double ou fault twin novo a escrever. Ver §11 (alternativas).

---

## 2. Invariantes (cada um com o teste que o prova)

Convenção: testes marcados **(a criar)** ainda não existem; testes sem marca existem hoje e devem continuar verdes (alguns reescritos deliberadamente, ver §10).

| # | Invariante | Teste que o prova |
|---|---|---|
| I1 | **Escritores serializam-se exatamente onde hoje**: a publicação de `commit.state` ocorre dentro de `COMMIT_SECTION` (§8.5 passo 3.7; `engine/txn_manager.py:1695-1703`), a da lease dentro de `LEASE_SECTION`; nenhuma seção nova, nenhum lock de SO adicional, nenhum guard que faça escritor esperar leitor. | `tests/txn/test_txn_multiprocess.py` (existente); **(a criar)** `tests/coordination/test_two_slot_publication_sections.py::test_two_publishers_in_two_processes_never_write_the_same_slot_file_concurrently` |
| I2 | **Leitores são lock-free**: ler um registro = 2 `read_page` + validação; nunca toma `COMMIT_SECTION`, `LEASE_SECTION` nem `page0-*`; nunca bloqueia nem é bloqueado por um escritor (FR-2, `coordination_local.py:1173-1178` continuam válidos). | **(a criar)** `tests/coordination/test_two_slot_reader.py::test_reading_takes_no_section_and_never_waits` (instrumenta `exclusive()` e afirma 0 chamadas) |
| I3 | **Sempre há pelo menos um slot com registro válido** (geração ≥ 1; um slot EMPTY é válido mas não é registro) em `writer.lease` e `commit.state` depois do bootstrap (§4.3): o escritor só sobrescreve o slot **inválido, EMPTY ou de geração menor**, nunca o slot válido mais novo. | **(a criar)** `tests/storage_core/test_two_slot_control_record.py::test_writer_never_overwrites_the_newest_valid_slot` (property-based sobre sequências de publicações e falhas) |
| I4 | **Geração monotônica por arquivo** (u64, +1 por publicação); um leitor que já observou a geração g nunca aceita g' < g do mesmo arquivo sem re-leitura; regressão persistente é fail-closed. | **(a criar)** `…::test_generation_is_monotonic_and_regression_is_refused`; `…::test_generation_at_u64_max_refuses_to_publish` |
| I5 | **Visível-antes-de-durável é seguro** (ver §4.4): o conteúdo publicado nunca está à frente da sua autoridade durável — `commit.state` só é publicado após `wal.barrier()` (3.5) e a aplicação das páginas (3.6); a lease só é **usada** pelo dono após o seu próprio `durable_barrier` retornar. | `tests/txn/test_isolation.py::test_no_instant_of_a_commit_offers_a_snapshot_of_half_of_it` (existente; continua verde); **(a criar)** `tests/coordination/test_two_slot_lease.py::test_lease_epoch_is_never_acted_on_before_its_barrier_returns` |
| I6 | **Identidade e nomes inalterados**: `control/writer.lease`, `control/readers/<id>.reader`, `control/commit.state` (§6.1); ids de leitor continuam `owner-nonce-rNNNN` (`coordination_local.py:209-240`); `_still_names` continua a re-provar a identidade do descritor antes de todo uso (§7). | `tests/storage_adapters/test_durability_and_platform.py` (bloco `_still_names`, existente); **(a criar)** `…::test_pinned_control_descriptor_is_reproved_after_a_foreign_retirement` |
| I7 | **Fail-closed em dupla corrupção**: 0 slots válidos em `writer.lease`/`commit.state` = `GrafxCorruptionDetected` (nunca "vazio = reinício de época", cf. `coordination_local.py:1503-1512`); em `readers/*.reader` 0 slots válidos = ABSENT (cf. `empty_is_absent=True`, `:1475-1477`). | **(a criar)** `…::test_two_torn_slots_are_corruption_for_lease_and_commit_state` e `…::test_two_torn_slots_are_absence_for_a_reader_registration` |
| I8 | **Formato declarado, nunca adivinhado**: um build antigo recusa a base antes de tocar os arquivos de controle (`grafx.meta.format_version` 1 → 2), e um build novo lê v1 e v2 (§6). | **(a criar)** `tests/foundation/test_control_record_format_bump.py::test_old_build_refuses_a_v2_database_with_schema_version_mismatch`, `…::test_new_build_reads_v1_and_v2_control_records` |
| I9 | **Recuperação e retirement sabem o que é um arquivo de slots**: os limites `_MAX_CONTROL_RECORD_BYTES = 4096` (`engine/recovery_manager.py:187, 872, 898`) passam a valer por **slot lógico**, não pelo arquivo de 3 páginas; a porta de retirement lê as 3 páginas com limite `3 × page_size`. | `tests/recovery/test_control_record_retirement_generation.py` (existente, estendido); **(a criar)** `…::test_retirement_reads_a_slot_file_as_one_generation_of_three_pages` |
| I10 | **Nenhuma escrita além do EOF**: o arquivo é alocado uma vez (`allocate(3)`) no bootstrap; `write_page` recusa índice não alocado (porta §4.1). | `tests/storage_adapters/*` (existente: `write_page` em página não alocada); **(a criar)** `…::test_slot_file_is_never_grown_after_bootstrap` |

---

## 3. Layout byte a byte

### 3.1 Arquivo de slots (paginado, `page_size` da base — padrão 8192, `domain/page/layout.py:72`)

```
página 0  cabeçalho do arquivo de controle (page_type = 7 control_header, NOVO valor em §6.3)
página 1  slot A
página 2  slot B
```
Tamanho fixo = `3 × page_size` (24 KiB no padrão). `page_size` mínimo 512 (`layout.py:65`) — o maior registro v1 (lease: 64 B de cabeçalho + ≤ 88 B de owner + 4 B de CRC = 156 B; leitor: 48 + ≤ 96 + 4 = 148 B; commit.state: 36 B, `domain/txn/commit_state.py:48-50`) cabe em qualquer `page_size` válido junto com os dois cabeçalhos abaixo (32 B de página + 56 B de slot + 4 B de CRC: 156 + 92 = 248 B < 512). Teste **(a criar)** `…::test_every_v1_record_fits_in_the_smallest_page_size`.

### 3.2 Cabeçalho de página (§6.3, 32 B, inalterado no formato) — como este arquivo o preenche

| off | campo | valor no arquivo de slots |
|---|---|---|
| 0 | `checksum` u32 | CRC-32C sobre bytes[4:page_size] (como toda página) |
| 4 | `page_type` u16 | `7` = control_slot (páginas 1 e 2); `8` = control_header (página 0) — **emenda de §6.3**: hoje o enum vai de 0 a 6 |
| 6 | `flags` u16 | 0 |
| 8 | `page_lsn` u64 | 0 — estas páginas nunca são alvo de redo do WAL (não são `WRITE_PAGE`); teste **(a criar)** `…::test_control_slot_pages_are_never_redone_from_the_wal` |
| 16 | `seq` u32 | `(geração × 2) mod 2^32` — sempre **par** (estável); a detecção de torn read do §6.3 é pelo checksum, não por seq ímpar, porque a página é escrita em **uma** chamada (ver §4.1) |
| 20-27 | `slot_count`, `free_start`, `free_end`, `reserved` | 0, 32, `page_size`, 0 |
| 28 | `next_page` u32 | `NO_PAGE` |

### 3.3 Registro de slot (dentro da página, a partir do offset 32; little-endian)

| off (rel.) | tipo | campo | regra |
|---|---|---|---|
| 0 | 8 B | `magic` | ASCII `OKTOSLOT` |
| 8 | u16 | `slot_format_version` | 1 |
| 10 | u16 | `record_kind` | 1 = lease, 2 = reader, 3 = commit_state; qualquer outro valor = slot inválido; tem de ser igual ao `record_kind` da página 0 do mesmo arquivo |
| 12 | u64 | `generation` | monotônica por arquivo; **0 = slot EMPTY** (página canônica válida, sem registro, `payload_length` = 0 — é o estado do slot B após o bootstrap); ≥ 1 = registro publicado |
| 20 | u32 | `payload_length` | tamanho exato do payload v1 embutido (0 no slot EMPTY); deve ser ≤ `page_size − 32 − 56 − 4` |
| 24 | u32 | `reserved` | 0 |
| 28 | 16 B | `database_uuid` | igual ao `database_uuid` de `grafx.meta` (§6.2) |
| 44 | u64 | `file_nonce` | igual ao `file_nonce` da página 0 **deste** arquivo (cunhado no bootstrap, `os.urandom(8)`) |
| 52 | u32 | `reserved2` | 0 |
| 56 | bytes | `payload` | **bytes v1 inalterados** (vazio no slot EMPTY): `encode_lease_record` (64 B + owner + crc32), `encode_reader_record` (48 B + reader + crc32), `CommitState.encode()` (36 B, magic `OGCS`) — o parser v1 continua a ser a autoridade do conteúdo |
| 56 + len | u32 | `slot_crc32c` | CRC-32C sobre `[0, 56 + payload_length)` do registro de slot — autentica magic, kind, geração, `database_uuid`, `file_nonce` e payload como um todo |
| resto | bytes | preenchimento | zeros até `page_size − 32` |

Por que dois checksums e um vínculo: o CRC da página (§6.3) detecta torn write da página inteira. O CRC do slot, sozinho, **não** protege contra uma página movida intacta de outro arquivo ou de outra base (page CRC e slot CRC continuariam válidos — correção do verificador); o que protege é o **vínculo** `database_uuid` + `file_nonce` + `record_kind`, e o slot CRC existe para que esse vínculo seja autenticado junto com o payload e para validar o registro sem recomputar o CRC da página. O CRC interno do payload v1 (crc32 zlib nos registros do coordinator, CRC-32C no commit.state) permanece e continua a ser verificado pelo decoder v1. Testes **(a criar)** `…::test_slot_layout_offsets_match_the_contract_table` (afirma cada offset desta tabela contra o encoder) e `…::test_an_intact_slot_page_moved_from_another_file_or_database_is_refused_by_nonce_and_uuid` (copia a página 1 de um arquivo v2 para outro arquivo/base: ambos os CRCs válidos, slot recusado).

### 3.4 Cabeçalho do arquivo (página 0, payload a partir do offset 32)

`magic 8 B "OKTOCTRL" | slot_format_version u16 | record_kind u16 | page_size u32 | database_uuid 16 B | file_nonce u64 | created_generation u64 | reserved 16 B | crc32c u32`. `database_uuid` = o de `grafx.meta` (§6.2); `file_nonce` = 8 bytes de `os.urandom` cunhados no bootstrap e copiados para **todo** slot publicado neste arquivo (§3.3). Um arquivo de slots copiado de outra base é recusado pelo `database_uuid`; uma **página** de slot copiada de outro arquivo da mesma base é recusada pelo `file_nonce` (teste `…::test_an_intact_slot_page_moved_from_another_file_or_database_is_refused_by_nonce_and_uuid`). A página 0 é escrita **uma vez** no bootstrap e nunca mais; é lida uma vez por descritor aberto (o par uuid/nonce fica em memória) e re-lida sempre que `_still_names` detectar que o nome mudou de identidade (§7); um `page_type = 8` numa página que não seja a 0 é corrupção.

### 3.5 Wrap / overflow

* `generation` u64: sem wrap por construção; ao atingir `2^64 − 1` o escritor **recusa publicar** (`GrafxCorruptionDetected`, campo `generation`) — 10^19 publicações não acontecem, mas a recusa é mais barata do que um raciocínio sobre wrap. Teste `…::test_generation_at_u64_max_refuses_to_publish`.
* `seq` u32 na página é derivado (`(g×2) mod 2^32`) e **não** participa da seleção do slot; pode dar a volta sem consequência. Teste **(a criar)** `…::test_page_seq_wraparound_does_not_affect_slot_selection`.
* `payload_length` maior que o espaço da página = slot inválido (nunca lido além da página). Teste **(a criar)** `…::test_oversized_payload_length_is_an_invalid_slot_not_a_read_past_the_page`.

---

## 4. Algoritmos

### 4.1 Publicar (escritor, já dentro da seção correspondente)

```
publish(file, kind, payload_v1):
  1. a, b := read_slot(page 1), read_slot(page 2)          # §4.2, nunca levanta por UM slot inválido; EMPTY é válido-sem-registro
  2. current := o slot com registro válido (geração ≥ 1, vínculo uuid/nonce/kind ok) de maior geração (ou nenhum)
  3. exigir que, se existe current, current.generation < 2^64-1
  4. alvo := a página cujo slot é inválido, EMPTY, ou tem a geração menor (se ambos válidos e iguais: corrupção — geração é única)
  5. g := (current.generation se existe senão 0) + 1
  6. página := page_header(seq = (g*2) mod 2^32, type 7) + slot_record(kind, g, uuid, file_nonce, payload_v1) + zeros
  7. storage.write_page(file, alvo, página)                  # UMA chamada: lseek+write no descritor cacheado
  8. storage.durable_barrier(file)                            # UM fsync
  9. retornar g
```
Passos 7–8 são os únicos write points; ambos já constam de `WRITE_POINT_METHODS`. **Nenhum temp, nenhum rename, nenhum `list_files`, nenhum `exists`/`create`/`remove`.** A publicação é "o slot" — não existe estado intermediário de nome. Teste **(a criar)** `…::test_publish_issues_exactly_one_write_page_and_one_barrier` (device double contando chamadas da porta).

Para `commit.state`, `_publish_commit_state` (`txn_manager.py:3112-3137`) continua a ler o estado durável antes (para preservar `checkpoint_lsn`) — essa leitura passa a ser `read_record` (§4.2) e **não** custa `exists + log_size + read_log + _resolve_identity` como hoje (`commit_state_store.py:49-53`).

### 4.2 Ler (qualquer processo, sem seção)

```
read_record(file, kind):
  1. se not exists(file): ABSENT                              # único probe de namespace; ver §7 sobre o descritor
  2. para tentativa em 1..R (R = 3):
       a := read_slot(page 1); b := read_slot(page 2)
       # read_slot: página com checksum válido + magic/versão/kind/length/slot_crc ok -> "válido";
       # geração 0 -> EMPTY (válido, sem registro; NUNCA gasta as releituras do §6.3, pois a página é canônica);
       # checksum/CRC/vínculo falhando -> "inválido" (nunca exceção por UM slot)
       válidos := [s for s in (a, b) if s.valid and s.generation >= 1 and s.kind == kind
                   and s.file_nonce == page0.file_nonce and s.database_uuid == meta.database_uuid]
       se válidos: escolhido := max(válidos, key=generation)
                   se escolhido.generation < último_visto[file]: continuar (regressão: releitura)
                   último_visto[file] := escolhido.generation; retornar decode_v1(escolhido.payload)
  3. 0 válidos após R tentativas: lease/commit_state → GrafxCorruptionDetected(file, field="slots")
                                  reader → ABSENT
  4. regressão persistente após R tentativas → GrafxCorruptionDetected(file, field="generation")
```
`read_slot(página)` = `read_page` + validação de `page_type`, `magic`, `slot_format_version`, `record_kind`, `payload_length`, `slot_crc32c` (e o checksum de página que `read_page` já verifica). **Ponto crítico:** `read_page` no §6.3 re-lê até 8 vezes e depois levanta `GrafxCorruptionDetected` quando o checksum falha; para o arquivo de slots, essa exceção em **um** slot é "slot inválido", não falha — o slot que está sendo reescrito pelo escritor é, por I3, o **mais velho**, e o mais novo continua legível. Teste **(a criar)** `…::test_a_torn_older_slot_is_ignored_while_the_newest_slot_is_served`; e `…::test_reader_under_a_publication_storm_never_sees_a_torn_newest_slot` (1 escritor publicando 10^5 vezes, 3 leitores em processos; 0 falhas de decode, gerações nunca regridem).

### 4.3 Bootstrap (criação do arquivo) e migração v1 → v2

Um arquivo de slots **nunca** aparece com 0 páginas, com páginas de zeros nem com 0 slots válidos sob o seu nome final:
```
bootstrap(file, kind, payload_v1):
  temp := f"{file}.{owner_id}.tmp"          # mesmo esquema de nome de hoje (commit_state_store.py:84-87)
  if exists(temp): remove(temp)
  nonce := os.urandom(8)
  create(temp, exclusive=True); allocate(temp, 3)
  write_page(temp, 0, header_page(kind, meta.database_uuid, nonce))
  write_page(temp, 1, slot(kind, g=1, uuid, nonce, payload_v1))
  write_page(temp, 2, EMPTY_slot(kind, uuid, nonce))        # página CANÔNICA: type 7, checksum válido, geração 0, payload_length 0
  durable_barrier(temp)
  atomic_replace(temp, file); durable_barrier(file)          # ÚNICA vez que este arquivo é renomeado
```
A página 2 **nunca** é escrita como zeros (correção do verificador): uma página de zeros falha o checksum do §6.3 e faria cada `read_page` gastar as 8 releituras e levantar corrupção; o slot EMPTY é uma página válida que o decoder reconhece como "sem registro". Isto preserva, sem regra nova, a semântica atual "0 B" (`coordination_local.py:1503-1512`): um `.reader` de 0 B ou sem registro válido é ABSENT; uma `writer.lease`/`commit.state` que exista sem registro válido é corrupção (I7).

**Migração de uma base v1 (ordem crash-safe — correção do verificador):**
```
migrate(root):
  1. sob COMMIT_SECTION + LEASE_SECTION (ordem auditada em recovery_manager.py:844-853): recusar se houver
     lease de outro dono válida ou leitor estrangeiro vivo (a migração é de participante único)
  2. publicar grafx.meta com format_version = 2 DURÁVEL — temp + write_page + durable_barrier + atomic_replace +
     durable_barrier (operação única; o meta é uma página, §6.2) — ANTES de tocar qualquer arquivo de controle
  3. para cada arquivo de controle v1 presente: ler v1 → bootstrap(kind, bytes v1); leitores v1 (readers/*.reader)
     convertidos ou, se o heartbeat já venceu, podados como hoje
  4. fim (nada a publicar no fim: o meta já é 2)
```
Consequência: depois do passo 2 um build v1 **recusa a base** (`GrafxSchemaVersionMismatch`) e nunca vê um arquivo v2 como corrupção; um build v2 abre uma mistura v1/v2 (arquivo v1 = registro cru ≤ 4096 B com magic `OKTOLEAS`/`OKTORDER`/`OGCS`; v2 = `page_type` 8 na página 0), lê ambas as formas e **retoma** a migração na abertura seguinte — cada passo é idempotente. A antiga ordem "converter e elevar o meta no fim" foi rejeitada: um crash a meio deixaria um build v1 abrir a base e tratar os arquivos v2 como corrupção. Testes **(a criar)** `tests/foundation/test_control_record_migration.py::test_meta_is_bumped_durably_before_the_first_v2_control_file`, `…::test_a_v1_build_refuses_a_database_crashed_mid_migration`, `…::test_migration_is_idempotent_and_survives_a_crash_after_each_file`, `…::test_migration_runs_under_both_sections_and_refuses_with_a_live_foreign_writer`.

`_FIRST_OPEN_SECTION` (`api/assembly.py:120-124, 818, 888`) cobre só bases novas; a migração de bases existentes usa as duas seções acima — **não** a primeira abertura.

### 4.4 Ordem de durabilidade e o que "visível antes de durável" muda

Hoje `commit_state_store.py:3-6` promete "visível só depois de durável" (o rename só acontece após o barrier do temp). Com o slot, outro processo pode ler a geração g+1 entre o passo 7 e o passo 8 de §4.1. Isto é seguro **porque a autoridade do conteúdo já é durável antes da publicação**:

* `commit.state` (3.7) só é escrito depois de `wal.barrier()` (3.5) e da aplicação das páginas (3.6) (`txn_manager.py:1695-1703`): um leitor que escolha o snapshot g+1 antes do fsync do slot escolhe um número cujo `COMMIT` já é durável no WAL; após qualquer crash, a recuperação republica ≥ g+1 a partir do WAL (redo idempotente). Prova: `test_no_instant_of_a_commit_offers_a_snapshot_of_half_of_it` (existente) + **(a criar)** `tests/recovery/test_commit_state_slot_after_crash.py::test_a_snapshot_observed_before_the_slot_barrier_is_recoverable_from_the_wal`.
* `writer.lease`: o dono só **age** com a época e+1 depois do seu próprio `durable_barrier` retornar (I5) — exatamente como hoje (o barrier do alvo precede o retorno de `acquire`). Um observador estrangeiro que veja e+1 antes desse barrier e um crash de **processo** do dono não perde a escrita (a página fica no cache do SO); só uma **perda de energia** perderia o slot e+1 — e ela mata também o observador. Modelo já assumido pelo produto: um host, sistema de arquivos local compartilhado (CONTRACT.md TR-3; NFS fora de escopo). Registrar como pressuposto explícito na emenda; teste **(a criar)** `tests/coordination/test_two_slot_lease.py::test_after_a_simulated_power_loss_the_older_epoch_is_visible_and_no_process_survives_with_the_newer_one` (fault twin: descarta escritas não barradas de todos os participantes).
* Registro de leitor: idem; o pin só protege o leitor depois do barrier (o leitor espera o seu próprio barrier antes de começar a ler), como hoje.

A docstring de `commit_state_store.py:3-6` e CONTRACT §8.5 passo 3.7 ("via `atomic_replace`") são as duas frases congeladas a emendar (§9).

---

## 5. Cobertura por arquivo

| Arquivo | Publica quem / quando | Com CE-1 | Ganho onde | Ressalva |
|---|---|---|---|---|
| `control/writer.lease` | dono da lease sob `LEASE_SECTION`: acquire, renew (heartbeat), release, takeover | slot in-place após bootstrap; takeover = publicação normal (geração +1, época nova) | 2 das 4 publicações por commit (acquire + release) | heartbeat in-place num **nome desvinculado** (arquivo retirado/quarentenado por outro processo) escreveria num inode órfão: `_still_names` **continua obrigatório** antes de cada `write_page` (§7) |
| `control/commit.state` | dono do commit sob `COMMIT_SECTION` (3.7); checkpoint; gap completion; recovery | slot in-place | 1 publicação por commit; leitura em `_publish_commit_state` e em todo `begin` fica 2 `read_page` | `_read_commit_state` (`txn_manager.py:3144-3164`) deixa de precisar do argumento "tamanho fixo + rename" — passa a I3/I4 |
| `control/readers/<id>.reader` | cada `begin` de leitura registra um id **novo** (`coordination_local.py:1181-1183` per report; contador `_reader_counter`), heartbeats no mesmo id, remoção por `recycle` na saída ou poda por TTL alheia | **registro** continua bootstrap (temp+rename) porque o id é novo por begin; só os **heartbeats/pins** do mesmo id ficam in-place | ~0 no harness mono-cliente; o ganho real da 4ª publicação depende de **CE-2** (um registro por manager, pin defasado) | poda estrangeira por TTL (`:1456-1466`) e "0 B = ABSENT" (`:1503-1505`) inalteradas; retirement por slot invalidante **não** é oferecido (mantém a recusa de `recovery_manager.py:836-842`) |

---

## 6. Formato, compatibilidade e rollback

* **Sinalização:** `grafx.meta.format_version` (`engine/database.py:183` `IDENTITY_FORMAT_VERSION = 1`; §6.2) sobe para **2** e é publicado **durável antes do primeiro arquivo de slots** (§4.3, passo 2). Um build v1 recusa a base com `GrafxSchemaVersionMismatch` **antes** de ler qualquer arquivo de controle — é a única forma de impedir o "downgrade silencioso" que a leitura de `CommitState.decode` produziria: hoje `decode` checa **tamanho antes de versão** (`domain/txn/commit_state.py:110-116`), logo um build antigo lendo um `commit.state` de 24 KiB veria `GrafxCorruptionDetected("length")`, e a sua recuperação poderia quarentenar e republicar um v1 por cima. Teste I8.
* **Build v2 lê v1 e v2**: dispatch pelo tamanho/magic (arquivo v1 = registro cru ≤ 4096 B começando por `OKTOLEAS`/`OKTORDER`/`OGCS`; v2 = `page_type` 8 na página 0). Necessário para a migração a meio e para bases só-leitura abertas por um build novo sem direito de escrever. Teste I8.
* **Rollback (v2 → v1):** ferramenta offline `oktografx control downgrade <root>` (CLI, `cli/commands.py`), que (1) toma `COMMIT_SECTION` + `LEASE_SECTION`, (2) recusa se houver lease ativa de outro dono ou leitor vivo, (3) para cada arquivo de slots escreve o payload v1 do slot mais novo via temp+`atomic_replace` (o caminho v1 de sempre), (4) remove os arquivos `.reader` (todos os pins já venceram por (2)), (5) `grafx.meta.format_version := 1`. Round-trip v1 → v2 → v1 byte-idêntico nos registros v1 é o gate G5. Teste **(a criar)** `tests/cli/test_control_downgrade.py::test_v1_v2_v1_round_trip_is_byte_identical_for_every_control_record`.
* **Nada muda** em heap, catálogo, índices, WAL, ledger, quarentena (§6.1 linhas 498-504).

---

## 7. Descritores: quente, frio e a prova de identidade

* Fato: o `LocalStorageDevice` cacheia descritores num LRU de `MAX_OPEN_FILES = 64` (`storage_local.py:129, 649`) para ~346 arquivos numa base M7; `_descriptor(name, intent)` re-prova a identidade do descritor cacheado com `_still_names` **antes de todo uso** (`:1573-1603`) porque outro processo pode publicar por rename sobre qualquer nome (CF-12/A66.1). Com slots in-place, os nomes de controle **só** mudam de inode em bootstrap, migração, downgrade, retirement e restore de quarentena — mas **podem** mudar, logo `_still_names` **não é removida** para eles.
* **Política proposta (a medir antes de fixar):** os 3 descritores de controle do participante (`writer.lease`, `commit.state`, o próprio `.reader`) são **fixados** no cache (isentos de evicção) e **contados** dentro do orçamento (`MAX_OPEN_FILES − 3` para os demais). Custo: 3 handles por processo. Sem fixar, uma evicção entre duas publicações reabre o arquivo (`_open_descriptor`, `CreateFileW`, `:1891-1913`) — o caso **frio**, cujo custo é [A MEDIR] (§0) e que no rig D5 foi de 11,4 ms por `CreateFileW` (COMPONENTS.md:1583). **Nenhum número deste documento pressupõe o pin sem esse teste e essa medição.**
* Testes **(a criar)**: `tests/storage_adapters/test_descriptor_pinning.py::test_pinned_control_descriptors_survive_eviction_pressure_of_400_files`, `…::test_pinned_descriptor_still_runs_still_names_and_reopens_after_a_foreign_rename`, `…::test_cold_publication_path_opens_writes_barriers_and_closes_correctly`, e o hook H7 do harness (`_open_descriptor`) para contar `CreateFileW` por op nas duas políticas.
* Deteção cross-process **não** é sacrificada: fixar o descritor não substitui a prova de identidade; a prova continua (lstat por segmento + stat + fstat, `_still_names`), só que agora sobre um nome que raramente muda — o custo dela é o de hoje (~0,3 ms hooked/chamada, §3.2 #7 do relatório) e fica registrado como parte do custo da publicação até QW-3/ST-2 decidirem o seu futuro.

---

## 8. Fault twin / matriz de crash (cada linha = 1 teste nomeado, todos **(a criar)** em `tests/recovery/test_two_slot_crash_matrix.py`, usando `FaultInjectingStorageDevice` com `partial_write_*` estendido a `write_page`)

| # | Falha injetada | Esperado | Teste |
|---|---|---|---|
| C1 | crash **antes** de `write_page` do slot alvo | nada mudou; slot mais novo anterior continua servido; geração não avança | `test_crash_before_write_page_leaves_the_previous_generation` |
| C2 | crash **depois** de `write_page`, **antes** de `durable_barrier` | (a) escrita chegou ao dispositivo → nova geração servida; (b) escrita perdida (perda de energia simulada = descarte de escritas não barradas) → geração anterior servida; em ambos os casos `commit.state` ≤ último `COMMIT` durável do WAL e a recuperação republica | `test_crash_between_write_and_barrier_serves_either_generation_never_a_torn_one` |
| C3 | crash **depois** de `durable_barrier` | nova geração servida sempre | `test_crash_after_barrier_serves_the_new_generation` |
| C4 | escrita **parcial** da página em cada fronteira de 512 B (k × 512, k = 1..page_size/512 − 1) | checksum de página falha → slot inválido → outro slot servido; próxima publicação sobrescreve o slot rasgado | `test_torn_page_at_every_sector_boundary_is_ignored_and_repaired_by_the_next_publication` (parametrizado) |
| C5 | escrita parcial que preserva o cabeçalho de página mas rasga o registro de slot (offsets 32..) | `slot_crc32c` falha → inválido | `test_torn_slot_record_with_intact_page_header_is_invalid` |
| C6 | slot mais velho já rasgado (C4) **e** crash a meio da publicação seguinte (que reescreve exatamente esse slot) | o slot mais novo nunca foi tocado → servido; 1 slot válido sempre (I3) | `test_the_newest_slot_is_never_the_one_being_rewritten` |
| C7 | **ambos** os slots inválidos (corrupção em repouso injetada após C4) | lease/commit_state → `GrafxCorruptionDetected`; porta de retirement da recuperação quarentena o arquivo de 3 páginas como uma geração (I9); reconstrução: `commit.state` a partir do WAL (último `COMMIT` durável), lease a partir da maior época vista no WAL + 1 (mesma regra de hoje para lease ausente), reader → ABSENT | `test_double_corruption_is_fail_closed_and_retired_as_one_generation` |
| C8 | crash durante o **bootstrap** (temp existe, alvo ausente ou v1) | próxima abertura repete o bootstrap; temp órfão é removido (esquema `.tmp` de hoje) | `test_crash_during_bootstrap_is_repeated_on_the_next_open` |
| C9 | crash durante a **migração** (meta já em 2, alguns arquivos ainda v1) | build v2 lê a mistura e completa a migração na reabertura; build v1 **recusa a base** desde o passo 2 de §4.3 (nunca vê um arquivo v2 como corrupção); crash **antes** do passo 2 deixa a base intacta em v1 | `test_crash_mid_migration_is_completed_on_reopen_and_a_v1_build_refuses` |
| C10 | kill −9 do **leitor** entre observar a geração g+1 e usá-la | sem efeito no arquivo; próximo leitor vê ≥ g+1 | `test_reader_death_between_observation_and_use_changes_nothing` |
| C11 | evicção do descritor entre duas publicações (caminho frio) | publicação reabre o nome (`_open_descriptor`), `_still_names`, escreve, barrier; resultado idêntico ao quente | `test_cold_descriptor_publication_is_equivalent_to_warm` |
| C12 | 2 processos tentam publicar o **mesmo** arquivo | impossível fora da seção: o segundo espera em `LEASE_SECTION`/`COMMIT_SECTION`; nunca dois `write_page` intercalados | `test_two_publishers_in_two_processes_never_write_the_same_slot_file_concurrently` (I1) |
| C13 | leitor lê enquanto o escritor escreve o **outro** slot | leitor vê o slot mais novo válido; se ler o slot em escrita, CRC falha e cai no outro | `test_reader_during_write_of_the_other_slot_is_served_the_newest_valid` |
| C14 | retirement/quarentena estrangeira do arquivo **entre** `_still_names` e `write_page` | janela de hoje (a mesma de qualquer publicação): a escrita cai no inode antigo; a próxima `_still_names` detecta e o próximo leitor vê o arquivo novo; documentar como equivalente ao comportamento atual com rename | `test_foreign_retirement_between_identity_proof_and_write_is_detected_on_the_next_use` |

`partial_write_bytes`/`partial_write_method` já existem no fault twin para `append_log` (`storage_fault.py:160-182`); a extensão a `write_page` é infraestrutura de teste, não código de produção.

---

## 9. Emendas de superfície congelada (numeradas; nenhuma altera a premissa)

| Emenda | Texto congelado | Mudança |
|---|---|---|
| E-CE1-1 | CONTRACT.md §6.1:505-507 — `control/commit.state … (atomic_replace)` | acrescentar: os três registros de controle são publicados **in-place** em arquivos de slots (3 páginas; §3); `atomic_replace` fica reservado ao bootstrap e ao downgrade |
| E-CE1-2 | CONTRACT.md §6.3:521 — `page_type` 0…6 | adicionar `7 control_slot`, `8 control_header`; `page_lsn = 0` e `seq` par derivado da geração para esses tipos |
| E-CE1-3 | CONTRACT.md §8.5 passo 3.7:754 — "publish `control/commit.state` via `atomic_replace`" | "publish `control/commit.state` **into the older/invalid slot** via `write_page` + `durable_barrier(file)`; a visibilidade pode preceder o barrier porque o `COMMIT` já é durável (3.5)" |
| E-CE1-4 | `engine/commit_state_store.py:3-6` (docstring: "visible only after durable") | reescrever conforme §4.4 |
| E-CE1-5 | CONTRACT.md §6.2 — `format_version` | v2 = base com arquivos de slots; regra de recusa por builds v1 |
| E-CE1-6 | `engine/recovery_manager.py:187` `_MAX_CONTROL_RECORD_BYTES` e portas `:860-905` | limite por slot lógico; leitura de 3 páginas para arquivos v2 |
| E-CE1-7 | COMPONENTS.md CF-5 (:98-127) e `test_a_control_file_is_published_over_a_reader_in_another_process` (`tests/storage_adapters/test_durability_and_platform.py:781`) | a propriedade "outro processo vê o conteúdo novo" continua obrigatória; o teste passa a exercitar `write_page` in-place em vez de rename sobre alvo aberto (reescrita deliberada; o caso rename fica para bootstrap/downgrade) |
| E-CE1-8 | `domain/ports/storage.py` §4.1 | **sem mudança** (é o argumento central deste ADR) |
| E-CE1-9 | IDENTITY-LEASING-V7 §11.1 (:8132-8135) | sem mudança: `retain_lease=True` continua recusado; CE-1 não retém lease |

Sem estas 7 emendas aprovadas tecnicamente pelos dois agentes (Codex + Claude) e sem os gates G0–G7 (§10), **não há código de produção**. O usuário já autorizou operacionalizar o roadmap e pediu consulta prévia **apenas** se a premissa multi-writer/multi-reader mudar; CE-1 não a muda (I1, I2), logo nenhuma emenda desta lista é uma consulta ao usuário.

---

## 10. Gates objetivos (ordem obrigatória)

| Gate | O que prova | Aceite |
|---|---|---|
| **G0 medição fria** (codex, em curso) | custo do primitivo com descritor frio e sob LRU realista | JSON no `ce1-spike` com fase `open`; decide §7 |
| **G1 unidade** | §2 I3/I4/I7/I10, §3 layout, §4.1/4.2 | todos os testes **(a criar)** de `tests/storage_core/test_two_slot_control_record.py` verdes; property-based (Hypothesis) sobre sequências de publicação/falha com ≥ 10^4 exemplos |
| **G2 matriz de falhas** | §8 C1–C14 | 14 testes verdes; C4 parametrizado em todas as fronteiras de 512 B do `page_size` padrão e em `MIN_PAGE_SIZE` |
| **G3 multiprocesso** | I1, I2, I5, C12, C13 | 2 e 4 escritores × 1 e 3 leitores em processos reais (`spawn`), 10^5 publicações do mesmo arquivo com fault twin rasgando o slot inativo a cada offset e kill −9 a cada passo; 0 falhas de decode, gerações nunca regridem, época nunca regride; `verify('all')` limpo vivo e após reopen; `measure_concurrency.py` 4w+3r antes/depois: unit commit cai, mediana de 4 escritores move ~1× (não 3×: é a OCC de page 0 que a fixa — CN-1), leitura 1,52/5,74 ms inalterada |
| **G4 regressão** | suites existentes | `tests/coordination`, `tests/txn`, `tests/recovery`, `tests/storage_adapters` verdes; `test_no_instant_of_a_commit_offers_a_snapshot_of_half_of_it` inalterado e verde; E-CE1-7 reescrito e verde; ruff limpo |
| **G5 rollback** | §6 | round-trip v1 → v2 → v1 byte-idêntico; build v1 recusa v2 com `GrafxSchemaVersionMismatch`; build v2 abre v1 só-leitura sem migrar |
| **G6 desempenho same-code** | ganho real, não inferido | harness h1-h8 (`okto-pulse-perf-harness`), base Etapa 0 em `4c474b56`, `--mode continuous --per-family 5`, digest `c994255b…` igual: hooks `_windows_posix_replace` 4 → 1/op (só o registro de leitor, até CE-2), `os.fsync` 9 → ≤ 6, `list_files` 9 → ≤ 7, `_open_descriptor` (H7) ≤ 3/op após aquecimento; RAW das 7 famílias simples e do D5 phase split neste rig com alvo "publicação ≤ 1 ms" **medido**, não assumido; só então a linha de ganho de CE-1 no roadmap é substituída por números [MEDIDO] |
| **G7 crítico cego** | processo C13 | revisão independente de §4.4 (visível-antes-de-durável) e §7 (pin) antes do merge |

---

## 11. Alternativas consideradas e rejeitadas (não repropor sem argumento novo)

| Alternativa | Por que não |
|---|---|
| **A — escrita posicional crua** (forma do spike: `lseek+write` num arquivo não paginado de 2 × 4 KiB) | exige um primitivo que §4.1 proíbe por desenho (zero-fill além do EOF); nova porta em 4 adapters, ≥ 6 doubles e no fault twin; tudo o que ela compra, `write_page` numa página alocada já dá |
| **B — um slot só, com `seq` ímpar/par (§6.3)** | escrita de uma página é uma chamada: não há como marcar "em escrita" sem uma segunda escrita (que duplica o custo) e um único slot rasgado deixa o arquivo sem verdade (viola I3) |
| **C — manter `atomic_replace` e só cortar provas (ST-5)** | fica como **estágio intermediário** se as emendas E-CE1-1/3/5 forem recusadas: ~27 provas restantes, renames inalterados em 4/op; ganho −25..−65 ms/op [INFERIDO], não resolve o denominador do W6 |
| **D — slots para registros de leitor sem CE-2** | registro por `begin` continua a criar um nome novo; o slot só ajuda heartbeats; ganho mono-cliente ≈ 0 — por isso a cobertura de leitores (§5) depende de CE-2 |
| **E — remover `_still_names` para os descritores fixados** | reintroduz "descritor lê o arquivo antigo para sempre" (C9 round-3, `storage_local.py:1587-1594`); proibido |
| **F — publicar sem barrier (visível e durável desacoplados por política)** | a lease **precisa** do barrier antes de ser usada (I5); para `commit.state` o barrier é o que permite reciclar/checkpoint com confiança; o custo do barrier é 0,1–0,16 ms [MEDIDO] — não vale o risco |

---

## 12. Perguntas abertas (bloqueiam a passagem de PROPOSED para ACCEPTED)

1. **[A MEDIR]** custo frio e sob LRU (G0) — decide se o pin (§7) é obrigatório e qual número entra no roadmap.
2. **[A MEDIR]** slot de `page_size` (8 KiB) vs 4 KiB: mesma fase `barrier`?
3. **[A VERIFICAR]** `read_page` sob escrita ativa do outro slot: o orçamento de 8 releituras do §6.3 é suficiente para um leitor nunca ver 2 slots inválidos com 1 escritor a 10^3 publicações/s? (G3 responde.)
4. **[A VERIFICAR]** a poda de leitores estrangeiros (`_reader_alive`, `:1456-1466`) usa `heartbeat_seq` — com heartbeats in-place o `seq` continua a avançar por publicação; confirmar que nenhum observador depende do `mtime`/tamanho do arquivo.
5. **[APROVAÇÃO técnica Codex + Claude]** E-CE1-1/3/5 (formato e passo 3.7): aprovação registrada por ambos + G0–G7 verdes; não é consulta ao usuário (ele autorizou operacionalizar e pediu consulta só se a premissa mudar — CE-1 não a muda).
6. **[DECISÃO codex/claude]** quem escreve cada gate (proposta: G1/G2 claude, G3/G6 codex — dono do instrumento e da janela ociosa —, G4/G5 quem implementar, G7 crítico cego).

---

Registro: documento redigido sem abrir bancos, sem executar benchmarks ou gates, sem tocar `src/` nem `tests/`; único artefato desta branch. Todo número tem tag; os únicos [MEDIDO] são os do spike (quente) e os do rig D5/COMPONENTS citados com linha.

### Revisão 2 (2026-08-30, correções obrigatórias do verificador codex, hof_35575263)
1. **Migração crash-safe** (§4.3, §6, C9): `grafx.meta.format_version = 2` é publicado durável **antes** do primeiro arquivo v2; build v1 recusa desde então; build v2 lê a mistura e retoma. A ordem anterior (converter e elevar no fim) foi rejeitada.
2. **Slot EMPTY canônico** (§3.3, §4.1, §4.2, §4.3, I3): a página 2 do bootstrap é uma página válida (checksum ok, geração 0, `payload_length` 0) reconhecida pelo decoder como "sem registro" — nunca zeros, que gastariam as 8 releituras do §6.3 e levantariam corrupção a cada leitura.
3. **Vínculo autenticado** (§3.3, §3.4): cada slot carrega `database_uuid` + `file_nonce` (cunhado na página 0 no bootstrap) cobertos pelo `slot_crc32c`; a alegação de que o slot CRC protegia contra restore trocado foi retirada — uma página movida intacta preservaria ambos os CRCs; o que a recusa é o vínculo.
4. **Aprovação** (§9, §12): "[DECISÃO do usuário]" substituído por aprovação técnica Codex + Claude + G0–G7; o usuário autorizou operacionalizar e pediu consulta só se a premissa mudar — CE-1 não a muda.
Status permanece **PROPOSED / produção bloqueada**; todos os demais invariantes inalterados.
