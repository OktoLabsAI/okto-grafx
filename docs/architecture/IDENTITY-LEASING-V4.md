# DESIGN V4 — Identity-range leasing

Status: **PROPOSTA — requer revisão conjunta Codex/Claude antes de implementação**

Base de código e prova:

- baseline: `milestone/m1-honest-config@539e94e8fa8c084c8322d4d23c6105610333b684`;
- verifier aceito e preservado: `m2/record-id-counter-verifier@40a42014913394d00f3446316edba68569f26d44`;
- contratos consultados: Round 7 §4, W6 e CONTRACT §8.5/§8.6.

Relação normativa: se aceito pela revisão conjunta, este documento substitui o detalhamento ainda
pendente de identity-range leasing do Round 7 §4/W6 e emenda explicitamente os pontos conflitantes
enumerados na seção 17. Até essa aceitação, não autoriza implementação.

Este V4 exige uma emenda explícita ao Round 7/W6 antes de implementação: ele altera somente o formato
do heap para v2, acrescenta `IDENTITY_CONTROL=14` ao WAL e reserva dois namespaces OCC. Não é
implementável com a afirmação antiga de “nenhuma mudança de formato”.

## 1. Decisão estrutural

A reserva deixa de ser por tabela e passa a ser global ao banco, ainda pertencendo em memória a um
participante.

Motivo: uma reserva per-table durável exige que a tabela já exista. Isso contradiz a transação
pública já suportada que cria tabela e primeira linha atomicamente. O estado `RESERVED` per-table
do V3 teria de persistir uma tabela não catalogada, usar placeholder, ou entregar o ID antes da
durabilidade — todos inválidos.

No v2:

- Existe um único `durable_identity_floor = F` global.
- Cada reserva avança `F` de `start` para `stop`.
- O mesmo page-0 image coloca `next_record_id=stop` em todos os extents reais materializados.
- Uma tabela ainda vazia não possui extent.
- Sua primeira materialização cria o extent com `next_record_id=F` corrente.
- Uma faixa reservada pode fornecer IDs para qualquer tabela.
- IDs novos serão globalmente crescentes, embora continuem semanticamente table-scoped.
- IDs e contadores v1 existentes permanecem válidos; duplicidade numérica entre tabelas legadas
  não é corrupção.
- Gaps e baixa densidade por tabela são parte documentada do contrato.

Isso preserva integralmente o oracle aceito: para todo header físico decodificável de uma tabela, o
counter daquele extent continua estritamente maior que o `record_id`.

## 2. Invariantes normativos

1. `format_version` do heap nunca diminui.
2. Heap v2 nunca volta a v1, não remove o identity entry e não altera `transition_lsn` nem
   `identity_generation`.
3. `F` começa em `FIRST_RECORD_ID=1`, nunca diminui e pode chegar a `MAX_U64`, o marcador de
   exaustão.
4. Todo extent real v2 materializado tem `next_record_id == F`.
5. Toda faixa `[start, stop)` é gravada, WAL-barriered, aplicada, write-backed, data-barriered e
   diretamente certificada antes de ser ativada.
6. Reservas são serializadas sob `COMMIT_SECTION`; portanto seus intervalos são disjuntos.
7. Um cursor incrementa antes de expor o valor. Abort, conflito, `KeyboardInterrupt`,
   `SystemExit`, crash, close, fork ou cleanup incerto nunca diminuem cursor ou `F`.
8. Todo `record_id` físico v2 satisfaz `FIRST_RECORD_ID <= id < F`.
9. Nenhuma preparação v2 anterior ao WAL append chama `allocate`, `write_page`, `append_log`,
   produz frame novo dirty ou modifica frame residente.
10. Nenhum estado é publicado antes de todas as imagens exatas estarem cross-process-visible.
11. Bytes ambíguos nunca são apagados para permitir upgrade; dúvida produz recusa sem escrita.

## 3. Formato persistido

### 3.1 Versões

Constantes:

```text
DEFAULT_FILE_FORMAT = 1
MAX_READABLE_FILE_FORMAT = 2
HEAP_IDENTITY_FORMAT = 2
IDENTITY_GENERATION_V1 = 1
```

Regras de `FileHeader.decode`:

- `META`, `CATALOG` e `INDEX`: apenas formatos legados já aceitos, nunca v2.
- `HEAP`: legado 0/1 continua legível; v2 é aceito com a gramática abaixo.
- O writer padrão dos arquivos não-heap continua v1.
- Banco novo em modo `compatible` ou `require` cria somente o heap como v2.
- Banco legado formato 0 é legível em `compatible`, mas upgrade exige exatamente v1.
- Um binário antigo continua recusando heap v2 por versão maior, antes de mutar dados.

### 3.2 Header do heap v2

O layout `<8sHHIIQ>` não muda:

```text
kind           = HEAP
format_version = 2
root_page      = 1                    # identity_generation; não é page pointer em HEAP v2
payload_length = transition_lsn
```

`transition_lsn`:

- `0`: banco nascido v2.
- `>0`: LSN do `COMMIT` exato que realizou v1→v2.
- Imutável por toda a vida do heap.
- `root_page=1` também é imutável neste formato.

### 3.3 Identity entry

O slot usa os mesmos 24 bytes `<IIIIQ>` do diretório, mas possui decoder próprio; nunca passa por
`TableExtent.decode`:

```text
table_id       = 0xFFFFFFFE
first_page     = NO_PAGE
last_page      = NO_PAGE
page_count     = 0
next_record_id = F
```

Deve existir exatamente uma vez no heap v2.

Estados por tabela real:

- `ABSENT`: tabela catalogada, nenhum extent e nenhuma página física que declare ownership dela.
- `MATERIALIZED`: exatamente um extent, chain real não vazia e counter igual a `F`.
- Qualquer slot sentinel de tabela real, extent sem catálogo, duplicata ou página física sem extent
  é `CORRUPT`, nunca `ABSENT`.
- Não existe estado persistido `RESERVED` per-table.

Capacidade:

- `max_materialized_tables_v2 = max_tables_v1 - 1`.
- Upgrade recusa sem escrita se page 0 não comportar o identity entry.
- O limite deve aparecer em status e na exceção typed.

### 3.4 Checksums e fingerprint

- O CRC-32C existente continua protegendo a página inteira.
- Todo WAL record mantém seu CRC-32C.
- O control record carrega SHA-256 do page 0 canônico.
- A canonicalização exclui apenas checksum físico e `seq`, pois `BufferPool.write_back` avança
  `seq`; inclui `page_lsn`, header, slots, flags, links e todo payload semântico.
- Igualdade de LSN é comparada por esse fingerprint canônico, não pelos bytes crus.

## 4. Namespaces OCC

```text
PAGE_PARTITION_TABLE_ID     = 0xFFFFFFFF
IDENTITY_PARTITION_TABLE_ID = 0xFFFFFFFE
MAX_REAL_TABLE_ID           = 0xFFFFFFFD

identity_counter_partition = 0xFFFFFFFE00000000
identity_format_partition  = 0xFFFFFFFE00000001
```

Regras:

- O catálogo v2 nunca aceita IDs acima de `MAX_REAL_TABLE_ID`.
- Um catálogo v1 com esses IDs continua legível no modo legado, mas não pode ser atualizado para
  v2.
- `TransactionContext.note_read/note_write` públicos recusam o namespace identity.
- Somente capability privada, não construível pelo chamador, insere esses interesses.
- `FORMAT_TRANSITION` escreve page-partition(heap,0), format e counter.
- `RANGE_RESERVATION` escreve page-partition(heap,0) e counter.
- Uma mutação v2 que reconstrói page 0 a partir da leitura direta corrente recebe um
  `RebasedPage0Proof` privado.
- OCC pode retirar do overlap apenas reservas válidas cujo page-0 effect já esteja incorporado
  nesse proof.
- Nunca isenta transição, consumer, mudança de extent ou qualquer user-data commit.

## 5. WAL e LSN exato

### 5.1 Novo record type

```text
WalRecordType.IDENTITY_CONTROL = 14
```

O WAL permanece format version 1: record types desconhecidos já são auto-delimitados e
forward-skippable.

Payload fixo de 80 bytes:

```text
magic[8]          = b"OKIDV4\0\0"
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

Operações:

```text
FORMAT_TRANSITION = 1
RANGE_RESERVATION = 2
```

Transição:

```text
start = stop = F
transition_lsn = commit_lsn = terminal COMMIT LSN
```

Reserva:

```text
start = old F
stop  = new F
transition_lsn = header.transition_lsn
commit_lsn = terminal COMMIT LSN
```

### 5.2 `WalAppendPlan`

`planned_terminal_lsn()` seguido de retarget não é suficiente.

Deve existir um `WalAppendPlan` frozen e single-use, construído sob `COMMIT_SECTION`:

1. Refresca e certifica o tail corrente.
2. Recebe templates com comprimentos invariantes.
3. Decide exatamente segment roll, `SEGMENT_HEADER`, arquivo, offsets e todos os LSNs.
4. Materializa page images, control payload e `COMMIT` usando o terminal LSN atribuído.
5. Confirma que nenhum comprimento mudou.
6. Congela os records já codificados, CRCs, blob exato e precondições do tail.
7. `append_planned(plan)` revalida, antes do primeiro byte:
   - manager/plan identity;
   - `last_lsn`;
   - último segmento, tamanho e generation;
   - próximo número de segmento;
   - epoch e shape.
8. Qualquer divergência é retryable e pre-write.
9. O append grava exatamente o blob congelado; não relê clock, não retargeta e não descobre o LSN
   depois.

Nunca é permitido usar `last_lsn+n`, `planned_terminal_lsn` estimado ou reescrever o header
depois do append.

### 5.3 Gramática de batches

Excluindo `SEGMENT_HEADER`, que pertence ao WAL:

- Transição: exatamente `WRITE_PAGE(heap,0)`, `IDENTITY_CONTROL(FORMAT_TRANSITION)`, `COMMIT`.
- Reserva: exatamente `WRITE_PAGE(heap,0)`, `IDENTITY_CONTROL(RANGE_RESERVATION)`, `COMMIT`.
- Consumer: nenhuma `IDENTITY_CONTROL`; page/index/catalog effects normais e um `COMMIT`.
- Uma transação não pode ter duas imagens finais do mesmo target.
- Page 0 de control batch leva `page_lsn=commit_lsn`.
- `CommitPayload.page_touches` é exatamente heap page 0 para control batches.
- Epoch/txn id são idênticos em todos os records do batch.

`CommittedReplay` passa a preservar:

```text
CommittedTransaction(
    epoch,
    txn_id,
    records_em_ordem,
    terminal_commit,
    decoded_commit_payload,
    commit_lsn,
)
```

A visão flattened existente torna-se derivada. Recovery, OCC e prova de supersession nunca inferem
gramática a partir de records soltos.

## 6. Máquina de estados

### 6.1 Persistente

```text
UNINITIALIZED
  └─ bootstrap compatible/require ─> V2_GENESIS(T=0,F=1)

V1_LEGACY
  ├─ operação legacy ──────────────> V1_LEGACY
  ├─ counter repair seguro ────────> V1_LEGACY clean
  └─ upgrade explícito ────────────> V2_MIGRATED(T=commit_lsn)

V2_GENESIS/V2_MIGRATED
  ├─ reservation ──────────────────> mesmo estado, F maior
  └─ consumer ─────────────────────> mesmo estado

INVALID/INCONCLUSIVE
  └─ nenhuma transição escrita
```

Não existe v2→v1. Repetir upgrade em v2 exato é no-op typed `already_active`; qualquer tentativa
de mudar `T`, generation ou remover o entry é corrupção.

### 6.2 Range local

```text
NONE
 → PLANNED
 → WAL_DURABLE
 → PAGE0_DURABLE_AND_CERTIFIED
 → COMMIT_PUBLISHED
 → LEASE_RELEASED
 → ACTIVE
 → DRAINED
```

Qualquer falha:

- antes de `ACTIVE`: range nunca é entregue e se torna gap;
- depois de `ACTIVE`: valores entregues e remainder abandonado nunca retornam ao allocator;
- falha incerta de release: `BURNED + participant_poisoned`;
- takeover nunca transfere ou recupera remainder.

Um manager pode manter uma pequena fila de ranges ativos. Se uma operação necessita `n` IDs e o
remainder é insuficiente, reserva `max(lease_size, deficit)` antes de entregar qualquer item da
operação.

## 7. Protocolo de reserva

1. PID/generation check é a primeira ação.
2. Sob participant section, calcula `minimum_needed`; não segura COMMIT.
3. Adquire fresh writer lease. A seção `LEASE` é deixada antes de entrar em `COMMIT`.
4. Entra `COMMIT_SECTION`.
5. Valida lease antes do primeiro acesso ao device.
6. Completa durable gap existente.
7. Valida lease novamente.
8. Lê diretamente heap page 0 e published state.
9. Exige:
   - v2 exato;
   - `page_lsn <= published_lsn`;
   - identity entry único;
   - counters reais iguais a `F`;
   - generation e `T` imutáveis.
10. Se `MAX_U64-F < minimum_needed`, recusa typed, sem escrita e sem handout.
11. Calcula:

```text
grant = max(identity_lease_size, minimum_needed)
stop  = min(MAX_U64, F + grant)
```

12. Clona page 0 detached, define identity floor e todos os counters reais como `stop`.
13. Cria batch e `WalAppendPlan` imutável.
14. Revalida lease imediatamente antes de `append_planned`.
15. Append, WAL barrier: o resultado torna-se irrevogável.
16. Preflight de apply; aplica a imagem exata.
17. `write_back(heap,0)` apenas.
18. `storage.durable_barrier(heap_file)`.
19. Reread direto confirma header, canonical fingerprint, `page_lsn`, entry único e counters.
20. Publica `commit.state`.
21. Sai de COMMIT.
22. Libera lease.
23. Somente após retorno confirmado do release faz uma atribuição atômica do range ao manager.

`BaseException` posterior ao WAL barrier marca recovery-required antes de propagar a mesma
instância. Release é tentado, mas nunca autoriza activation incerta.

I/O steady-state: um page-0 read, um batch WAL, um page-0 writeback, um heap barrier e uma
publicação; zero heap data-page/catalog scan. Catch-up de WAL e recuperação não fazem parte do
claim O(1).

## 8. Cursor, takeover e exaustão

Um grant contém:

```text
database_uuid
identity_generation
origin_pid
manager_uuid
range_nonce
start
stop
reservation_commit_lsn
```

Antes de cada consumer:

- PID é conferido primeiro.
- Sob fresh writer lease e COMMIT, page 0 direto deve ter mesmo DB/generation/T e
  `F >= grant.stop`.
- Não se exige que o writer lease epoch seja o mesmo da reserva.
- Um owner antigo pode voltar a usar seu remainder, pois o floor nunca voltou e nenhum sucessor
  recebeu aqueles IDs.
- Se DB/generation mudou, o remainder é queimado.
- Filho de `fork` nunca usa nem libera range do pai.

Cursor:

```text
require pos < stop
value = pos
pos += 1
return value
```

O incremento ocorre antes de binding, retorno ou callback.

`MAX_U64` nunca é entregue. O último ID possível é `MAX_U64-1`; `stop` pode ser `MAX_U64`.
Exaustão impede apenas inserts; update/delete/checkpoint/read permanecem válidos.

## 9. Consumer v2 puro

O refill é uma transação separada. A lease da reserva é liberada; o consumer adquire outra fresh
lease.

Antes de COMMIT, a transação guarda somente intents lógicos. IDs pendentes expostos publicamente são
`None`/sentinel; relações intratransação usam tokens simbólicos.

Dentro de COMMIT:

1. PID, lease validation, gap completion, published current e direct format fence.
2. Primeiro OCC sobre interesses lógicos conhecidos.
3. Garante capacidade de IDs; `take_many(n)` avança todos os cursores antes de retornar a tupla.
4. `HeapMutationPlanner` detached lê storage diretamente, nunca via pin que possa evictar dirty
   frame.
5. Planeja insert/update/delete, overflow, relink, first materialization e extent hints sem mutar
   storage/pool.
6. Catalog e index staging da mesma transação também são detached; staging pré-COMMIT é apenas
   lógico.
7. Alocação virtual usa o `page_count` direto atual sob COMMIT; não reutiliza páginas livres no
   v2.
8. Nova tabela recebe extent real e `next_record_id=F`; não recebe placeholder.
9. Exige todo ID consumido `< F`.
10. Declara pages/interesses exatos.
11. Segundo OCC.
12. Prova mecanicamente zero allocate/write/dirty residente.
13. Constrói `WalAppendPlan`, append e barrier.
14. Aplica em ordem:
    - páginas novas/overflow ainda inalcançáveis;
    - targets modificados;
    - predecessor links/relinks;
    - roots/page 0/hints por último.
15. Write-back apenas dos `touched_pages`.
16. Publica `commit.state`.

Crash pré-barrier deixa somente IDs queimados. Crash pós-barrier é recovery de commit durável; a
transação nunca volta a ACTIVE para retry.

## 10. Apply, flush e prova de supersession

`apply_page_image` torna-se duas fases: preflight completo, depois mutação.

Resultados:

```text
APPLIED
IDENTICAL
SUPERSEDED_COMMITTED
CONFLICTING_EQUAL_LSN
UNPROVEN_FUTURE
```

Regras:

- LSN menor/free: `APPLIED`.
- Mesmo LSN e fingerprint canônico igual: `IDENTICAL`.
- Mesmo LSN e fingerprint diferente: `CONFLICTING_EQUAL_LSN`, corrupção.
- LSN maior: somente `SUPERSEDED_COMMITTED` se uma transação committed posterior contém a imagem
  canônica exata para o mesmo target.
- LSN maior sem prova: `UNPROVEN_FUTURE`, recusa.
- Os dois últimos estados adversos são encontrados no preflight antes de aplicar a primeira
  página.

`CommitRedoResult` carrega:

```text
touched_pages: tuple[(file, page_index), ...]
files_requiring_barrier: tuple[str, ...]
```

Logical index apply deve retornar seus pages exatos. `BufferPool.flush(file)` não é permitido nesta
rota. `write_back` é chamado apenas para `touched_pages`; dirty unrelated permanece dirty.

## 11. Upgrade e saneamento v1

API:

```python
db.upgrade_identity_leasing(
    *,
    confirm_quiescent=True,
    repair="refuse" | "raise_counters",
)
```

Condições:

- somente read-write;
- `confirm_quiescent is True` exato;
- operator deve ter encerrado binários pré-fence;
- heap exatamente v1;
- IDs reservados ausentes do catálogo/extents;
- espaço para identity entry;
- nenhum durable gap.

Certificação independente usa o verifier aceito em `40a4201`:

- `CLEAN_V1`;
- `COUNTER_REPAIRABLE_V1`;
- `INCONCLUSIVE`;
- `CORRUPT`.

Somente `RECORD_ID_COUNTER`, com inventory físico completo, ownership único e nenhum outro finding
é reparável. Repair:

- preserva todo byte de row/page;
- nunca libera/deleta orphan;
- nunca altera CSN;
- eleva counters para `max(counter, physical_max+1)`;
- recusa ID físico `MAX_U64`;
- usa page clone, WAL commit, exact apply e heap barrier;
- recertifica diretamente.

Depois:

1. Certifica sob participant→COMMIT, sem lease, e memoriza digest.
2. Sai de COMMIT.
3. Adquire fresh lease.
4. Entra COMMIT, valida, completa gap e recertifica integralmente.
5. Calcula:

```text
F = max(
    FIRST_RECORD_ID,
    todos os next_record_id legados,
    cada physical_max + 1,
)
```

6. Constrói o transition batch por `WalAppendPlan`.
7. O mesmo page-0 image:
   - muda header para v2;
   - grava `T=commit_lsn`;
   - insere identity entry com `F`;
   - eleva todos os extents a `F`.
8. WAL barrier, exact apply/writeback, heap barrier, direct verify, publish.
9. Nenhum range é ativado.
10. O handle fica `reopen_required`; nenhuma operação posterior usa caches nascidos em v1.

## 12. Recovery, checkpoint e read-only

### 12.1 Determinação de fase

- Header v2/T=0: todo WAL retido pertence à fase v2; não pode haver transition batch.
- Header v2/T>0 e `checkpoint_lsn < T`: deve existir exatamente um transition batch committed em
  `T`.
- Header v2/T>0 e `checkpoint_lsn >= T`: checkpoint + header durable são autoridade; o marker
  pode ter sido reciclado.
- Header v1: replay é legado até um transition batch exato. Seu COMMIT muda a fase; records
  posteriores são v2.
- Identity-looking records anteriores à transição nunca elevam floors. Apenas o batch com
  gramática completa e COMMIT igual a `T` muda a fase.
- Header v1 com transition já reciclada é corrupção: checkpoint não podia passar `T` antes de
  tornar o header v2 durável.
- Múltiplas transições, mudança de `T` ou reservation antes da fase v2 não autorizam IDs.

Recovery calcula o floor efetivo como o máximo entre:

- floor direto que tenha checkpoint/prova committed;
- floor da transição retida;
- todos os `stop` de reservations committed retidas.

A partir do primeiro control retido, `next.start == previous.stop`. O primeiro pode começar abaixo
de uma page 0 já mais nova; o apply aceita isso somente por `SUPERSEDED_COMMITTED`.

Após redo de format/counter:

- exact writeback;
- `durable_barrier(heap_file)`;
- direct proof de `F >= recovery_floor`;
- somente então publication.

### 12.2 Checkpoint

Checkpoint:

- usa grouped committed transactions;
- preflights todo redo antes da primeira mutação;
- write-backs pages exatas;
- toma data barriers requeridos;
- não publica `checkpoint_lsn >= T` sem header/identity entry diretamente certificados;
- não recicla transition enquanto `checkpoint_lsn < T`;
- preserva prova suficiente para qualquer higher-page LSN acima do checkpoint.

### 12.3 Read-only

Read-only nunca:

- migra;
- repara;
- reserva;
- replaya;
- trunca WAL;
- altera mtime/bytes.

Só abre se checkpoint completeness e phase consistency puderem ser provadas sem escrita. Durable
transition/reservation acima do checkpoint produz `GrafxRecoveryRefused`, não tentativa de “ler o
que der”.

## 13. Locks, leases e fork/PID

Ordem única para identity paths:

```text
participant
  → LEASE acquire e saída da LEASE_SECTION
  → writer lease held
  → COMMIT_SECTION
  → saída de COMMIT
  → lease release
  → range activation
```

Regras:

- Nunca adquirir ou renovar lease dentro de COMMIT.
- `validate_epoch` dentro de COMMIT é permitido porque não adquire `LEASE_SECTION`.
- Certification pode fazer participant→COMMIT sem lease.
- Nunca reutilizar uma LeaseGuard entre reservation e consumer.
- A exceção legada COMMIT→LEASE usada por retirement em recovery continua isolada; identity code
  não a chama.

Guardas `origin_pid` e process-generation são obrigatórios em:

- `Database`;
- `TransactionManager` e contexts;
- coordinator;
- `LeaseGuard`;
- identity manager/grant/range/cursor;
- `PortRegistry` e snapshots;
- cleanup/close owners.

A primeira instrução de cada porta pública relevante verifica PID antes de lock, clock, métrica, FD
ou I/O.

`os.register_at_fork(after_in_child=...)` substitui por atribuição, sem adquirir locks herdados:

- `_SHARED_SECTIONS`;
- `_SHARED_SECTIONS_GUARD`;
- generation e caches process-globais.

Objetos herdados ficam permanentemente poison. `close()` no filho não libera lease, reader, range ou
ports do pai. Um `connect()` novo no filho cria registry/adapters/coordinator novos e funciona para
arquivo; `:memory:` é uma cópia independente, nunca storage compartilhado.

## 14. Configuração, API e métricas

Configuração:

```text
identity_leasing_mode = "legacy" | "compatible" | "require"
default = "compatible"

identity_lease_size: exact built-in int
default = 64
1 <= value <= MAX_U64 - 1
```

Semântica:

| Banco | legacy | compatible | require |
|---|---|---|---|
| novo | cria v1 | cria v2 | cria v2 |
| v1 | allocator legado | allocator legado, status sugere upgrade | recusa typed |
| v2 | recusa downgrade | leasing | leasing |

Status público, com inteiros exatos:

```text
format
enabled
transition_lsn
identity_generation
durable_floor | None
lease_size
active_ranges
ids_remaining
exhausted
upgrade_required
reopen_required
```

Não há API pública para reserva manual nem explicit `record_id`.

Métricas registradas no catálogo central e testadas contra sinks reais:

```text
oktografx_identity_range_reservations_total{outcome}
oktografx_identity_ids_reserved_total
oktografx_identity_ids_handed_out_total
oktografx_identity_ids_burned_total
oktografx_identity_reservation_duration_seconds
oktografx_identity_range_remaining
```

`outcome` é closed set: `granted`, `exhausted`, `conflict`, `recovery_required`.

Sem labels de owner, PID, table, record ID, path ou range. `burned_total` mede apenas perdas
observáveis pelo processo; hard-crash loss não é reconstruído. Métricas são emitidas fora de
participant/COMMIT/manager locks.

## 15. Matriz dos 11 blockers reconstruídos

| # | Blocker V3 | Mecanismo V4 | Teste/mutante obrigatório |
|---|---|---|---|
| B1 | Header v2 sem semântica kind-aware, checksum/fingerprint e limites de catálogo | Header heap-only v2, identity slot exato, canonical SHA, capacity e reserved IDs | non-heap v2; duplicate/missing slot; no-room upgrade; equal LSN com `seq` diferente |
| B2 | `RESERVED` per-table contradiz DDL+primeira row e `_require_first_page` | Floor global; sem per-table RESERVED/placeholder; ABSENT→MATERIALIZED no consumer | DDL+rows/edges/index no mesmo commit; nenhum extent sentinel |
| B3 | Transition LSN estimado/circular | `WalAppendPlan` frozen criado e validado sob COMMIT | segment roll; foreign tail; mutant `last_lsn+n`; mudança de comprimento |
| B4 | Replay perde txn/epoch/COMMIT/marker | `CommittedTransaction` e gramática estrita de control batches | reused txn_id em epochs; marker solto; duplicate terminal; control após terminal |
| B5 | Writes pre-WAL, whole-file flush e skip `page_lsn>=` sem prova | Detached COW, exact touched pages, fingerprint e supersession committed | zero allocate/write/dirty; unrelated dirty preservado; equal-conflict/higher-unproven |
| B6 | Repair legado podia apagar resíduo ambíguo e produzir falso clean | Só counter raise com verifier completo; nenhuma deleção/free/CSN edit | orphan/duplicate/unreadable/inventory refusal recusa byte-identical |
| B7 | Colisão OCC e capability pública; reservation conflita consigo | IDs reservados, capability privada e `RebasedPage0Proof` | catalog reserved ID; forged partition; own/foreign reservation rebase; transition nunca isenta |
| B8 | Ordem lease/COMMIT, release e takeover ambíguos | Ordem única; fresh leases; activation após release confirmado; stale ranges nunca reclaimed | lease stolen em cada fence; release incerto; takeover com owner antigo retomando |
| B9 | Fase WAL/checkpoint/read-only e reciclagem de transição ambíguas | Phase machine por `T`, grouped controls, floor proof, recycle fence | T antes/depois checkpoint; v1 header com marker reciclado; RO zero bytes |
| B10 | Overflow/off-by-one, rewind e fork duplicam IDs | `[start,stop)`, MAX marker, increment-before-return e PID poison | near-MAX; size1; KI/SE; fork child usa/fecha cursor/lease do pai |
| B11 | Claims O(1), capacity e slices não sustentados | Um page-0 control path bounded; claim restrito; capacity pública; recovery antes de leasing | read/page counters invariáveis; zero heap scan; benchmarks separados de recovery |

## 16. Critérios de aceite

### Crash/fence matrix

| Fronteira | Resultado obrigatório após kill/reopen |
|---|---|
| antes/depois de PID e format fence | zero nova escrita; inherited child incapaz de cleanup |
| antes/depois de lease acquire/validate | nenhuma reservation; old epoch não escreve byte |
| antes/depois de gap completion | prefixo anterior completo ou recovery-required |
| antes/depois de direct page0/cert/OCC | zero allocate/write; IDs já handed ficam queimados |
| antes/depois de `WalAppendPlan` | zero bytes; plan stale recusa pre-write |
| durante append/rollback | tail exato restaurado ou WAL damaged + recovery latch |
| append retornou, antes do WAL barrier | outcome incerto; nunca activation |
| logo após WAL barrier | commit irrevogável; recovery completa |
| cada apply/writeback | replay idempotente e verifier não dá falso clean |
| antes/depois do heap data barrier | range nunca ativo sem direct durable proof |
| antes/depois de direct verify/publication | recovery publica somente estado completo |
| antes/depois de COMMIT exit/release | crash queima range não ativado |
| antes/depois de activation | range inteiro aparece atomicamente |
| antes/depois de cada handout | cursor nunca retorna mesmo ID duas vezes |
| consumer após barrier, antes de publish | row é recuperada uma vez; ID nunca reutilizado |

Após cada hard kill:

- reopen completo;
- verifier `records/all`;
- `F > max(record_id físico)` para toda tabela;
- counters reais iguais a `F`;
- nenhuma duplicidade de ID por tabela;
- nenhum range sobreposto;
- bytes ambíguos preservados.

### Matriz funcional e adversarial

- v1 legacy/compatible/require e v2 legacy/compatible/require.
- genesis v2 e upgrade v1→v2.
- upgrade repetido, downgrade, transition/generation mutation.
- page sizes 512 até 32768; page 0 no limite de capacity.
- lease sizes 1, 64, maior que batch e near-exhaustion.
- insert/update/delete/MERGE, node/relationship, overflow, chain growth, DDL+row, hash/vector indexes.
- abort, conflito OCC, `KeyboardInterrupt`, `SystemExit`, device full e barrier failure.
- multiprocess same-table e different-table; killed owner, stalled owner e takeover.
- POSIX `fork`, Windows/POSIX spawn e fresh child connect.
- verifier preserva literalmente todos os casos de `40a4201`, inclusive inventory inconclusive.
- read-only compara bytes, sizes e mtimes antes/depois.
- sinks noop/JSON/OpenMetrics registram todos os descriptors.
- Ruff, formatter, compileall, API/recovery/txn/full suite.
- Mutantes para cada fence e para todos os 11 itens da tabela.

### Performance

O aceite estrutural, não uma promessa de throughput:

- refill lê/escreve somente page 0 e control/WAL;
- zero heap data page e zero catalog owner scan;
- número de reads não cresce com rows ou heap pages;
- uma reservation por `ceil(handouts/lease_size)` em um manager ininterrupto, salvo batches que
  pedem déficit maior;
- `measure_concurrency.py` registra before/after de conflitos, p90/p99 e throughput;
- throughput pode permanecer no teto do `COMMIT_SECTION`;
- recovery backlog, tail refresh degradado e consumer chain work ficam explicitamente fora do
  claim O(1).

## 17. Conflitos com o Round 7 antigo

Este V4 substitui formalmente:

1. “Option 1 sem mudança on-disk”: há heap v2 e um slot reservado.
2. Counter/range implicitamente per-table: a faixa é global ao banco.
3. File format version global único: decoder passa a ser kind-aware.
4. WAL codes congelados em 1–13: adiciona `IDENTITY_CONTROL=14`.
5. Table IDs até `0xFFFFFFFF`: máximo real passa a `0xFFFFFFFD`.
6. CONTRACT §8.5 “nenhum data fsync no commit”: format, counter repair e reservation fazem heap-only
   data barrier antes do handout.
7. `page_lsn >= image_lsn` como no-op: igualdade divergente e futuro sem prova passam a recusar.
8. Whole-file flush: publicação usa touched-pages exatos.
9. Claim de near-zero conflict/p90 como sucesso garantido: passa a ser hipótese medida.
10. Ordem antiga dos sete slices: recovery/checkpoint/OCC e exact WAL planning precisam ser aceitos
    antes de existir writer de leasing.

Extent-hint offload continua fora de escopo; hints só mudam quando a chain realmente muda.

## 18. Sequência revisada de implementação

Cada slice é no mínimo um commit atômico, suite-green e sem squash posterior.

1. **Formato, namespaces e fences — leasing desabilitado**
   - Decoder kind-aware, constants, identity entry codec, catalog limits, config/status skeleton,
     direct write fences e PID/fork foundation.
   - Nenhum banco v2 é criado por API ainda.

2. **WAL planning e transaction grouping — leasing desabilitado**
   - `IDENTITY_CONTROL`, `CommittedTransaction`, `WalAppendPlan`, plan/append exato e migração do
     commit genérico para o plano imutável.

3. **Exact redo, recovery, checkpoint e read-only — leasing desabilitado**
   - Apply enum/fingerprint, preflight, touched-pages, logical-index touched pages, phase machine,
     floor simulation, checkpoint/recycle e RO zero-write.

4. **Verifier/certification/repair dry-run — leasing desabilitado**
   - Preservação literal de `40a4201`, branch v2, absent/legacy/corrupt/inconclusive e único repair
     permitido.

   **Gate obrigatório:** critic independente ACCEPT de slices 1–4 antes de qualquer writer produzir
   v2.

5. **Planners detached/COW — ainda sem exposição v2**
   - Heap, catalog e index planning puramente detached; virtual allocation;
     DML/DDL/overflow/relink; paridade funcional v1.

6. **Genesis e transição v1→v2 — sem range consumer público**
   - Bootstrap v2 interno, upgrade, transition exact, recovery e checkpoint de fixtures v2. Default
     público ainda não muda.

7. **Reservation manager, cursor e consumer; ativação pública**
   - Protocolos de range, global floor, OCC rebase, DDL+row, exhaustion, API/config/status/metrics.
     Só aqui `compatible` passa a criar v2.

8. **Fechamento adversarial**
   - Hard-kill matrix, multiprocess/fork, mutants, sinks reais, capacity, full suite, benchmarks e
     atualização formal de CONTRACT/Round7/W6/PUNCHLIST.

Nenhum slice de reservation ou consumer depende de comportamento ainda não provado de
recovery/checkpoint/OCC.

