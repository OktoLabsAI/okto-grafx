# DESIGN V6 — Identity-range leasing

> **Status:** PROPOSTA / BLOCKED — requer revisão conjunta Codex/Claude antes de implementação.
>
> **Base normativa e código-alvo exclusiva:** milestone M1
> `539e94e8fa8c084c8322d4d23c6105610333b684`, no worktree de auditoria
> `D:\Projetos\Techridy\okto_grafx-m1`. As referências de linhas deste documento foram
> conferidas nessa árvore limpa. A revisão Claude anterior usou o `main` `23794daf...`; suas
> conclusões L1–L4/O1/O2 foram preservadas, mas suas linhas não são evidência para este desenho.
>
> **Oráculo de integridade que deve ser preservado:**
> `m2/record-id-counter-verifier@40a42014913394d00f3446316edba68569f26d44`.
>
> **Rastreabilidade:** V6 preserva, mas não altera,
> `IDENTITY-LEASING-V4.md` (SHA-256
> `8301053a57e7c3e76ee15da515e76945b22aeaf8a0728d6c504f95426b154726`).
> Preserva também `IDENTITY-LEASING-V5.md` byte-identical (SHA-256
> `02018552bfcd427b5ec72a653284ab6da9e5664ea1a41724f0d9e1e8968e6488`). V6 incorpora integralmente
> a V5, corrige seus seis blockers independentes e adiciona o gate de horizonte relevante de índices.
> Após ACCEPT conjunto, V6 substitui as propostas V4/V5 e constitui emenda substitutiva ao
> `ROUND7-PLAN.md` §4 e ao Option 1 de `W6-WRITE-CEILING.md`. Até esse ACCEPT, nenhum writer,
> upgrade, migração ou leasing v2 pode ser implementado ou habilitado.

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

1. Para todo `record_id` que já tenha sido consumido pelo manager/exposto por callback ou tornado visível em row, o
   heap v2 persistido deve ter `F > record_id` antes da exposição/publicação.
2. `F` é monotônico e só aumenta por batch `RANGE_RESERVATION` cometido.
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
8. Um batch cujo WAL cruzou a barreira é irrevogável. Falha posterior não autoriza rollback em
   memória; ela marca `recovery_required` e bloqueia nova escrita/publicação.
9. Imagem com LSN igual e fingerprint canônico igual é no-op idempotente. LSN igual e fingerprint
   diferente é corrupção. LSN maior só subsume uma imagem se uma transação cometida posterior
   prova exatamente aquela imagem.
10. `format_transition_lsn`, `identity_generation` e `F` nunca diminuem. Heap v2 nunca faz
    downgrade in-place para v1.
11. Read-only nunca repara, migra, completa gap, aplica redo, trunca, recicla, atualiza mtime ou
    ativa grants.
12. O cache nunca é sua própria prova de durabilidade; toda certificação final lê `heap:0`
    diretamente do device ou por uma instância fria independente.

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

No SHA-base, `domain/page/file_header.py:44` fixa versão `1`, e
`FileHeader.decode` (`:146-152`) recusa qualquer versão maior globalmente. Slice 1 deve tornar essa
decisão kind-aware sem ampliar silenciosamente os outros kinds.

Configuração:

```text
identity_leasing_mode = "compatible" | "require"
identity_lease_size   = exact built-in int em [1, MAX_RECORD_ID]
```

`identity_lease_size` tem default `64`. Definem-se
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
redundante. Reserva/consumer que avança F clona page0 e eleva, na mesma imagem, a identity entry e
todos os extents reais. Tabela ABSENT não possui extent. Primeira materialização cria o extent já
com o novo F. Qualquer drift entre identity entry e extent real é `CORRUPT` ou redo pendente
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

- `ABSENT_HEAP`: arquivo inexistente/zero bytes somente em bootstrap comprovado;
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
binário V6 aplica o mesmo ceiling preventivo. v1 que já use qualquer ID reservado permanece legível
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

V6 substitui isso, primeiro no caminho genérico, por um `WalAppendPlan` frozen e single-use criado
sob COMMIT. Ele contém:

- identidade do `WalManager`, PID/generation e nonce de uso;
- prova do tail: epoch, `last_lsn`, `max_epoch`, segmento/número/tamanho atuais e próximo número;
- decisão de roll e eventual `SEGMENT_HEADER`;
- clock capturado exatamente uma vez;
- sequência completa de records tipados com todos os LSNs;
- imagens já retargeted para o terminal exato;
- bytes codificados, CRCs, offsets, placements e terminal COMMIT;
- fingerprint e comprimento do blob total.

`append_planned(plan)` atualiza a visão do WAL e revalida **todos** os campos do tail antes do
primeiro byte. Divergência produz conflito retryable `wal_plan_stale`, ainda sem write. Depois da
validação, não consulta clock, não aloca LSN, não reencoda, não retargeta e não materializa; escreve
o blob exato. Falha que não restaura byte a byte o preimage aciona uncertainty latch e recovery.
Uso repetido do nonce é recusado pelo manager; o dataclass frozen não carrega flag mutável.

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

Todo commit novo após o cutover genérico usa `CommitPayloadV2`, selecionado pelo flag de header WAL
`COMMIT_FLAG_MANIFEST_V2`. Ele preserva o prefixo V1 e acrescenta um trailer canônico autenticado pelo
CRC do COMMIT:

```text
magic = b"OKCMV2\0\0"; manifest_version = 1
affected_table_ids = tuple[u32] sorted/unique
affected_index_scopes = tuple[(table_id u32, operation u8, definition_sha256[32])] sorted/unique
```

Operações de schema são `CREATE=1`, `DROP=2`, `ALTER=3`; outros códigos recusam. Counts e bytes têm
limites checked antes do freeze. `CommittedTransaction.decoded_commit_payload` expõe o manifest
detached; o CRC do record, grouped terminal e `WalAppendPlan` autenticam-no.

Antes de append e novamente em recovery, um validator total reconstrói o manifest obrigatório a
partir de row intents canonicalizados, ownership/table descriptors das page images, efeitos lógicos
de índice e catalog/schema diff. `affected_table_ids` precisa ser exatamente o conjunto de tabelas
cujo conteúdo lógico pode mudar; schema scopes precisam corresponder bijetivamente ao diff. Omissão,
extra, digest divergente ou page touch sem owner provado recusa antes do append/apply. A ausência de
um `INDEX_WRITE` esperado não permite omitir a tabela: ou a preparação recusa, ou a tabela permanece
affected e o relevant horizon avança, deixando o header curto detectável.

`RANGE_RESERVATION` e `FORMAT_TRANSITION` carregam manifest vazio, provado por exatamente heap0,
partições identity/format e ausência de row/catalog/index effect. Antes de habilitar tipo 14, um
checkpoint certificado cria o baseline e garante que o sufixo necessário para relevant horizons é
inteiramente `CommitPayloadV2`; payload V1 retido é validado conservadoramente e nunca recebe manifest
vazio inventado.

### 5.3 Compatibilidade WAL required/skippable — gate anterior ao tipo 14

Antes de adicionar `IDENTITY_CONTROL`, o header WAL v1 passa a interpretar o bit 0 do `flags` u16
já congelado (`domain/wal/record.py:88,168-180`) como `WAL_FLAG_REQUIRED`; zero significa
`SKIPPABLE`. Bit 1 é `COMMIT_FLAG_MANIFEST_V2` e só é válido em COMMIT junto de REQUIRED; bits
restantes são recusados até emenda futura. A assimetria atual — decoder aceita
tipo desconhecido (`domain/wal/codec.py:14-17`) mas writer só emite tipo conhecido
(`domain/wal/record.py:224-271`, `engine/wal_manager.py:1008-1030`) — fica fechada assim:

- tipo desconhecido + SKIPPABLE: framing/CRC/LSN continuam obrigatórios; o record é preservado no
  `CommittedTransaction.records_in_wal_order`, mas não produz efeito;
- tipo desconhecido + REQUIRED: `UNSUPPORTED_REQUIRED_RECORD`, zero apply/publication/repair/recycle;
- tipo conhecido: flags precisam pertencer à gramática daquele tipo; não se degrada conhecido
  malformed a skippable;
- `IDENTITY_CONTROL` é sempre REQUIRED no header WAL. Seu `flags` interno de payload continua zero e
  é outro campo; confundir os dois é corrupção/refusal.

O cutover required/skippable migra writer, scanner, `CommittedHistory`, recovery, checkpoint e
read-only **antes** de o enum ganhar um tipo emitível. Mutantes retiram o bit, pulam unknown-required,
classificam known-malformed como skippable ou reciclam seu segmento; todos recusam pre-mutation.

### 5.4 Record type e gramática fechada

Adicionar um único `WalRecordType.IDENTITY_CONTROL = 14`, sempre REQUIRED; o WAL continua format
version 1. O payload é fixo de 80 bytes, little-endian e auto-delimitado pelo record WAL:

```text
magic[8]          = b"OKIDV6\0\0"
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
reservado e recusado — V6 não possui `COUNTER_CONSUMER`. Demais códigos/flags/version são recusados
em fase v2. O heap é implícito e único no database/WAL; não se serializa path ou
provider-controlled file ID.

Para format, `start == stop == F` e `transition_lsn == commit_lsn == terminal`. Para reservation,
`start == old F`, `stop == new F`, `start < stop`, `transition_lsn == header.T` e
`commit_lsn == terminal`. O CRC do próprio WAL cobre todos os 80 bytes; `page0_sha256` cobre a
imagem semântica alvo definida na seção 3.5.

Gramática de transação:

- `FORMAT_TRANSITION`: exatamente uma `WRITE_PAGE(heap,0)`, um control format e um COMMIT;
- `RANGE_RESERVATION`: exatamente uma `WRITE_PAGE(heap,0)`, um control reservation e um COMMIT;
- `CONSUMER`: sem control, sem avanço de `F` e sem imagem de counter; pode conter outras imagens
  legítimas.

Cada control exige `new_floor > old_floor`, salvo format sem range (`old == new`), fingerprint e
partições exatas. Marker solto, duplicado, fora de ordem ou com payload divergente é inválido.
Antes de uma transição exata, um tipo 14 REQUIRED só é aceito se formar exatamente
`FORMAT_TRANSITION`; outro shape conhecido é corrupção/refusal e nunca é tratado como legado opaco.
Unknown SKIPPABLE de outros números continua opaco. A transição só é reconhecida se seu COMMIT LSN é
igual a `T` codificado na imagem de heap0. Depois de v2, qualquer shape inválido é corrupção/refusal.

O scan base valida framing/comprimento/CRC/required sem decodificar eager o payload identity. Em fase
v1, somente uma transação com shape completo, imagem heap0 v2 e `T == commit_lsn` é decodificada como
transition; outro tipo 14 é required-malformed e recusa. Isso impede que lixo pretransition eleve F
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
   manager lock e só então adquirir writer lease **fresh**, sem segurar COMMIT;
6. entrar em COMMIT;
7. validar lease/epoch, completar qualquer COMMIT durável pendente e revalidar;
8. abrir `begin_read_view` com tail/epoch atual e executar OCC identity/page0;
9. ler `heap:0` diretamente, validar v2/T/generation/fingerprint e `page_lsn <= current`;
10. calcular `start = F`, `stop` com aritmética checked e recusar wrap/exaustão;
11. construir clone privado de heap0 e `WalAppendPlan` `RANGE_RESERVATION` estrito;
12. provar novamente PID/lease/tail e `append_planned`;
13. `wal.barrier()`; o runner marca seu resultado detached como `PENDING_IRREVOCABLE(attempt)` e
    fecha o evento built-in sem tomar manager lock; nenhum ID é consumível;
14. preflight/apply exato de heap0, `write_back(heap,0)`, data barrier de heap;
15. read direto: header, T, generation, fingerprint, page_lsn e `F >= stop`;
16. publicar `commit.state`/published LSN;
17. sair de COMMIT;
18. liberar writer lease; confirmação é obrigatória;
19. sair da participant section;
20. sob manager lock, PID/generation + CAS do mesmo attempt ativam o grant e notificam waiters.

É proibido adquirir lease segurando COMMIT, certificar migração com writer lease, reutilizar lease
retido, publicar antes de data barrier/prova direta, ou ativar antes de release. `BaseException`
depois da barreira marca recovery; falha/ambiguidade de release queima o range pendente e envenena o
manager.

Falha comprovadamente pre-WAL sai de todas as sections, toma manager lock, faz CAS do mesmo attempt
para `NONE`/`EXHAUSTED` e notifica waiters; o remainder anterior continua queimado. Falha após barrier
faz CAS para `RECOVERY_REQUIRED`, revoga nonce/ticket e notifica. Se o CAS falhar por close/generation,
nenhum estado é ressuscitado. Cleanup nunca chama provider nem métrica sob manager lock.

### 6.3 Exaustão e overflow

Todas as contas são u64 checked em exact built-in ints. Se `F == MAX_RECORD_ID + 1`, allocator
retorna erro typed de identity exhaustion antes de qualquer write. Se o grant pedido cruza a
sentinela, reserva somente `[F, MAX_RECORD_ID + 1)` se isso satisfaz `n`; caso contrário recusa sem
write. Nunca há wrap para `0`, truncamento, modulo ou entrega da sentinela.

### 6.4 Stale owner e takeover

O estado durável não contém owner nem remainder: contém somente `F`. Por isso takeover nunca
reclama IDs. Um manager que morre perde seu remainder, já coberto por `F`, como gap autorizado.

Um processo antigo só pode continuar seu próprio range se for o mesmo manager object, mesmo PID, mesmo DB
UUID/generation e, sob fresh lease+COMMIT, uma leitura direta provar `F >= stop`. Restart do
manager, fork, mismatch ou qualquer incerteza queima a autoridade. Outro processo pode reservar a
partir de `F`, jamais a partir de `pos` alheio.

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

## 7. Consumer e DDL+primeira row em COW puro

Todo batch consumidor executa primeiro o prepass de demanda. Se necessário, conclui uma
`RANGE_RESERVATION` interna independente antes do planejamento; em seguida consome do range ACTIVE
todos os `n` números sob o manager e associa cada um ao respectivo row intent, mesmo quando o batch
mistura tabelas existentes, várias tabelas novas, nodes, edges e índices. A reservation posterior ao
snapshot é incorporada por `RebasedPage0Proof`; o batch consumidor não avança `F` e não contém
identity control.

Esse prepass/refill/consume ocorre antes de entrar na participant section do **commit consumidor** e
sem main lease/COMMIT retidos. A reservation usa e libera sua própria participant/lease/COMMIT. Depois
do consume, o commit consumidor entra normalmente em participant e revalida txn/close/generation;
se close ou abort vencer no intervalo, os IDs consumidos viram gap e nada é reutilizado.

O commit consumidor, já dentro de participant, segue a ordem global de locks e só então chama o planner:

1. adquire main lease fresh, lê diretamente catálogo/identidades dos índices e, a partir dos intents
   canonicalizados, congela o `IndexFencePlan` sem tocar pool/device;
2. adquire `INDEX_VIEW*` em ordem canônica e só então entra em COMMIT;
3. revalida lease, catálogo/identity set, completa gap, captura current/read view e executa primeiro OCC;
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
14. publica, sai de COMMIT, libera `INDEX_VIEW*` em ordem inversa e libera lease;
15. o remainder já estava ACTIVE e permanece database-bound.

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

`CommitRedoResult` passa a carregar `touched_pages: tuple[(file, page_index), ...]`, ordenada e sem
duplicatas, e `files_requiring_barrier: tuple[file, ...]`, não somente `touched_files`. Todo resultado
bem-sucedido — `APPLIED`, `IDENTICAL` ou `SUPERSEDED_COMMITTED` — devolve a página e exige write-back
exato. Tanto identical quanto uma imagem posterior comprovada podem existir apenas dirty no pool por
uma tentativa anterior; retry idempotente ainda deve colocá-las no device antes da publicação.
`CONFLICTING_EQUAL_LSN` e `UNPROVEN_FUTURE` devolvem zero touched e recusam a transação inteira.
Logical index apply retorna
explicitamente header, tail relinkado e cada página nova; nunca se infere touched set pelo delta
global de frames modified.

`apply_page_image` retorna:

```text
APPLIED
IDENTICAL
SUPERSEDED_COMMITTED
CONFLICTING_EQUAL_LSN
UNPROVEN_FUTURE
```

O SHA-base em `engine/buffer_pool.py:1242-1307` trata todo `page_lsn >= target` como skip em
`:1299`; isso mascara equal-LSN diferente e higher-LSN sem prova. V6 decodifica/fingerprinta antes
da decisão. `SUPERSEDED_COMMITTED` requer lookup no `CommittedHistory` por uma transação posterior
que contenha a imagem canônica exata. Os dois estados de conflito recusam publicação. Mutantes que
omitem `IDENTICAL` ou `SUPERSEDED_COMMITTED` de `touched_pages`, ou escrevem somente `APPLIED`, morrem
num retry com frame superior/igual dirty e cold reopen.

Toda transação é preflighted por inteiro antes do primeiro apply. `durable_exact` ordena/deduplica
e chama `write_back(file,page)` somente nas touched pages. Frames dirty não relacionados continuam
dirty e não podem ser tornados duráveis por acidente.

### 8.2 Classificação completa dos sites `BufferPool.flush` no SHA 539

Esta tabela é normativa para L3. Ela inclui a primitive, os 12 callsites diretos produtivos e as
duas rotas indiretas críticas. “Migrar” significa substituir o whole-file flush do caminho por
`durable_exact(touched_pages)`/write-back exato; “manter” exige o motivo independente do WAL.

| Evidência no SHA-base | Símbolo/uso | Decisão V6 |
|---|---|---|
| `engine/buffer_pool.py:547-563` | `BufferPool.flush` | manter como primitive explícita whole-file |
| `engine/buffer_pool.py:669-685` (`:679`) | `BufferPool.checkpoint -> flush` | manter; checkpoint explícito drena o pool inteiro |
| `engine/commit_redo.py:185-199` | `CommitRedo.flush` | migrar para touched pages |
| `engine/database.py:1592-1603` | `Database.flush` público | manter; pedido explícito do operador |
| `engine/database.py:1998` | `Database._flush_pages`/close | manter; courtesy close, salvo estado recovery-required |
| `engine/index_manager.py:352-378` | `IndexStore.create` | manter somente como inicialização isolada/quiescente de arquivo novo; exact pages é preferível |
| `engine/index_manager.py:544-578` | `IndexStore.mark_stale` | migrar para write-back exato do header não-WAL |
| `engine/index_manager.py:602-621` | `_lift_short_commit_mark` | migrar; ocorre dentro de index apply e whole-file publica estado parcial |
| `engine/index_manager.py:871-892` | `advance_built_through` | migrar para header exato; recovery usa `_advance` e touched pages |
| `engine/index_manager.py:894-915` | `clear_stale` | boundary de rebuild legítimo, mas escrever exatamente o header |
| `engine/index_manager.py:1098-1117` | `note_reconciled` | manutenção legítima, porém write-back exato do header |
| `engine/index_manager.py:2110-2147` | `IndexManager.commit` | migrar para touched pages do commit |
| `engine/txn_manager.py:2500-2528` | `_apply_images` | migrar para touched pages do commit |
| `engine/txn_manager.py:1246-1247` | redo via `CommitRedo.flush` | migra indiretamente com `CommitRedo` |
| `engine/recovery_manager.py:1454-1455` | recovery via `CommitRedo.flush` | migra indiretamente com `CommitRedo` |

Não se deve converter `create`, `mark_stale`, `_lift_short_commit_mark`, `clear_stale` ou
`note_reconciled` em writes **sem durabilidade**: `LESSONS.md:560-578` exige persistência
independente para estado não coberto por WAL, e `PUNCHLIST.md:805-826` documenta especificamente os
headers de índice. A correção é `write_back` exato do header + barrier quando requerido, não
whole-file flush. A porta pública `advance_built_through` continua durável; a aplicação
recovery-derived não chama essa porta e reporta sua page 0 entre touched pages.

Os cinco callers de `BufferPool.checkpoint` — `api/assembly.py:436,1595,1604`,
`database.py:MetaStore.create:521` e `txn_manager.py:checkpoint:1043` — são bootstrap/checkpoint
deliberados e mantêm semântica global. Não contam como commit/index apply acidental.

Mutante obrigatório: `BufferPool.flush` lança se chamado de commit, redo, recovery ou index apply;
todos esses fluxos devem passar. O mesmo mutante registra que explicit `Database.flush`, close,
checkpoint e create isolado continuam chamando flush, enquanto os cinco headers não-WAL chamam
somente write-back exato + barrier requerido. Um frame dirty sentinela em outro arquivo/página
permanece byte-identical e dirty após durable exact.

### 8.3 Barreiras

Commit ordinário continua com WAL como autoridade e não faz data fsync, como
`CONTRACT.md:667-671`. Exceções identity/format fazem data barrier de `heap.dat` antes da publicação,
pois ativação de range e interpretação de formato dependem da prova física imediata. Recovery e
checkpoint seguem as regras da seção 10.

### 8.4 Horizonte relevante por índice — gate anterior ao writer v2

No SHA 539, `IndexStore.check_freshness` compara `built_through_lsn` diretamente com o published
global (`engine/index_manager.py:623-680`). `IndexManager.commit` avança índices de tabelas não
escritas até todo CSN global (`:2106-2147`), recovery chama `mark_built_through` global
(`engine/txn_manager.py:1240-1249`) e `IndexManager.open/mark_built_through` mantém um único
`_published_lsn` (`index_manager.py:1807-1872`). Uma `RANGE_RESERVATION` eleva WAL/published sem tocar
índice: manter a regra atual exigiria reescrever todos os headers a cada refill, contradizendo “uma
imagem heap0”, ou tornaria todos stale. O conserto é genérico, anterior ao tipo 14, e não uma exceção
hard-coded para identity.

`CommittedHistory` fornece:

```text
relevant_horizon(index_definition, checkpoint_lsn, published_lsn) -> Lsn
```

O horizonte é exatamente `max(checkpoint_lsn certificado, commit_lsn das transações retidas cujo
CommitPayloadV2 manifest afeta table_id ou definition digest do índice)` no intervalo
`(checkpoint, published]`. `affected_table_ids` cobre mudança de conteúdo; `affected_index_scopes`
cobre CREATE/DROP/ALTER. O manifest é validado contra row/page/catalog/index effects como na seção
5.2; ele é a prova autenticada, não uma inferência oportunista durante lookup.

`CommitPayload.page_touches` precisa corresponder bijetivamente às `WRITE_PAGE`; write partitions e
page owners precisam justificar `affected_table_ids`, e efeitos/schema diff precisam justificar os
scopes. Não se infere relevância de descriptor textual, CSN global, page0 genérica, quantidade de
records ou ausência de efeito. `FORMAT_TRANSITION` e `RANGE_RESERVATION` têm manifest vazio;
control-only e commits apenas de outras tabelas não avançam o horizonte. Um commit com row/page effect
da tabela não pode omiti-la do manifest. Se ele omite um efeito de índice esperado, a preparação pode
recusar; se o batch chega validamente sem esse efeito, o table ID ainda está affected, o relevant
horizon avança e o header fica atrás, preservando o alarme contra índice curto.

Writer/redo avançam e retornam touched pages somente dos índices relevantes que foram aplicados com
sucesso. O caminho `elif table not written -> advance_built_through(csn)` em
`index_manager.py:2122-2141` é removido; commit alheio simplesmente não muda aquele header. Recovery
deriva o mesmo mapa pela história agrupada, nunca chama global `mark_built_through(published)` fora de
checkpoint.

`IndexStore.check_freshness` deixa de receber um único published e passa a exigir valores built-in
detached `relevant_horizon`, `published_lsn` e o header direto já validado. `IndexManager` não conserva
`_published_lsn` como prova; guarda no máximo cache derivado keyed pela tail proof exata, nunca header.

Em **todo novo read-view público** — uma vez por operação/view, nunca por row — o engine:

1. captura checkpoint, published e tail proof sob o fence de leitura apropriado;
2. deriva ou reutiliza somente o mapa de relevant horizons autenticado por essa tupla exata;
3. lê **diretamente do device** o header de cada índice relevante, validando checksum, DB/definition,
   stale flag, `built_through_lsn` e `reconciled_through_lsn`;
4. aceita apenas header não stale com `relevant_horizon <= built_through_lsn <= published_lsn` e
   coerência de checkpoint;
5. forma um token detached `(checkpoint, published, tail_fingerprint,
   tuple(index_name, direct_header_sha256))` e o entrega a `BufferPool.begin_read_view`. Mudança de
   qualquer header invalida/recarrega frames do índice antes do lookup, mesmo com LSN igual.

A leitura direta do header ocorre mesmo se checkpoint/published/tail forem numericamente idênticos ao
view anterior. `mark_stale()` é unlogged (`index_manager.py:544-578`) e outro processo pode persistir
o bit sem mover qualquer LSN; cache ou token global sozinho false-cleanaria essa mudança. Essa leitura
fresh é obrigatória até existir um shared freshness epoch persistido e autenticado por protocolo
posterior. O fingerprint direto também mata cache após clear/rebuild cross-process no mesmo LSN.
Falha/I/O é refusal/inconclusive; nunca usa header cached como fallback.

Essa leitura fresh de entrada, sozinha, ainda deixa uma janela TOCTOU: um writer de outro processo
pode começar a alterar páginas derivadas depois do passo 3 e persistir `STALE` somente depois de o
reader já ter atravessado parte do índice. A `participant_section` existente é deliberadamente local
ao participante — seu nome inclui a identidade do owner e outro processo nunca contende nela
(`engine/txn_manager.py:163-168,2679-2703`) — portanto ela não fecha essa corrida. V6 introduz uma
section cross-process estável `INDEX_VIEW/<sha256(database_uuid, index_identity)>`, obtida pelo mesmo
coordinator e com o mesmo nome em todos os participantes. `index_identity` é a identidade persistida
do índice, não o nome mutável; CREATE/DROP/ALTER adquirem, em ordem lexicográfica de digest, as
identidades antiga e nova que existirem. A section usa o mecanismo interprocess com timeout e
owner-death release do coordinator; falha/timeout/namespace inconsistente é refusal, nunca fallback
para lock de thread. No SHA 539, `coordination_local.py:1713-1744` escolhe lock process-wide quando
`lock_directory is None` e file lock caso contrário: um database/device compartilhável entre
processos precisa obrigatoriamente do segundo modo para `INDEX_VIEW`; ausência dessa capacidade
recusa open/write indexado. Apenas database realmente process-private (por exemplo, memória não
compartilhada) pode usar a section local, pois não existe segundo processo capaz de acessar seus bytes.

Todo caminho público que usa páginas derivadas — lookup generic, índice exact e traversal HNSW —
obedece a um dos dois modos abaixo; misturá-los ou ler página live fora da section é dano de contrato:

1. **fence-held:** adquire todas as `INDEX_VIEW` relevantes em ordem canônica, faz o pre-refresh
   direto e valida horizon/header, atravessa o índice e materializa resultado detached enquanto ainda
   possui as sections; nenhum callback, sink, métrica, codec ou vector provider hostil roda retido;
2. **detached optimistic:** sob as mesmas sections, faz o pre-refresh e copia para estado detached
   imutável **todas** as páginas/bytes necessários; libera, calcula sem consultar frame/device live,
   readquire o mesmo conjunto e executa post-validation direta antes de expor o resultado.

A post-validation relê device header, checkpoint, published, tail proof, relevant horizon, catálogo/
definition set e forma novamente o token completo. Ela exige igualdade com o token prévio, header
clean e limites válidos; qualquer mudança, I/O, index set diferente ou section revogada descarta o
resultado inteiro e recusa/reinicia segundo budget explícito. Nenhuma row, iterator, generator,
callback ou resultado parcial é observável antes de a pós-validação terminar e todas as sections
serem liberadas. Assim generic/exact/HNSW possuem o mesmo fail-closed; validar só o HNSW é proibido.

Todo writer, redo, recovery, rebuild e checkpoint que possa alterar header ou página derivada adquire
as mesmas sections canônicas **antes da primeira alteração** e as retém até exact write-back/barrier e
publicação final do header clean/stale. Para alteração coberta por WAL, manifest e `WalAppendPlan` são
congelados/validados sob as sections antes do append; o append durável precede apply e relevant horizon
torna um crash com header curto não-legível. Para
manutenção sem WAL, a ordem é ainda mais forte: `STALE` precisa ser exact-persisted, barriered e
direct-verified sob a section **antes** da primeira mutação de página; só depois de todas as páginas
duráveis e verificadas `clear_stale`/`note_reconciled` pode publicar clean. O padrão atual “muta e
depois chama `mark_stale`” é removido, não apenas cercado. Kill entre esses passos deixa STALE ou um
header atrás do relevant horizon, nunca um índice parcialmente novo aceito como clean.

Antes de adquirir as sections, o writer constrói um `IndexFencePlan` frozen a partir dos row intents,
catálogo direto e schema diff detached. Já sob COMMIT, o validator do `CommitPayloadV2` exige igualdade
bijetiva entre esse plano, manifest e efeitos reais **antes do append**; índice ausente/extra ou troca
de identity reinicia pre-WAL. Isso torna o conjunto de locks conhecível sem planejar páginas live e
impede adquirir um novo fence depois de COMMIT. Reservation/format produzem plano vazio.

Checkpoint continua global. Sob COMMIT ele aplica redo até published, calcula todos os relevant
horizons e exige cada índice conhecido clean e completo. Então avança cada header certificado para o
novo `checkpoint_lsn`, retorna todas essas page0 em touched pages, executa whole-pool flush+barriers,
relê headers diretamente e só depois publica checkpoint e recicla prefixo. Índice stale/incompleto ou
unknown-required impede avanço/reciclagem; reconstrução explícita precisa ocorrer antes. Assim o
checkpoint cria a nova base mesmo quando o WAL relevante antigo será removido.

Mutantes obrigatórios: usar published global; esvaziar/forjar manifest; contar control-only ou outra
tabela; ignorar affected table sem index effect; avançar o antigo `elif`; usar header cached quando token não mudou;
remover direct read ou header fingerprint do token no mesmo LSN; `mark_built_through` global em recovery; reciclar antes de
certificar todos os headers; substituir `INDEX_VIEW` por participant-local; remover post-validation;
fazer post-check antes do `mark_stale`; inverter a ordem de dois fences; expor iterator/callback antes
da liberação; ou permitir página live no modo detached. Fixtures incluem dois índices/tabelas, várias
reservations, foreign commits, missing index effect, `mark_stale` cross-process no mesmo published e
cold reopen pós-recycle. Um harness pausa cada reader generic/exact/HNSW logo após o pre-refresh,
deixa outro processo alterar página e somente então tentar `mark_stale`: o reader devolve integralmente
o snapshot detached anterior ou recusa/reinicia, mas nunca observa mistura nem resultado parcial.

## 9. Verifier, saneamento e upgrade

### 9.1 Oráculo preservado

O verifier aceito `40a4201` não pode regredir. Sua `_physical_heap_inventory`
(`engine/verifier.py:471-568`) inventaria owners e máximos fisicamente; falha de `page_count` produz
`FILE_UNREADABLE` e nenhum máximo. `_verify_record_id_counters` (`:571-682`) só acusa contador
quando o inventário é completo. V6 adiciona branch v2 antes do decoder, sem alterar os resultados
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

O repair usa fresh writer lease, COMMIT, revalidação de digest, imagens COW, `WalAppendPlan`, WAL
barrier, apply/write-back exatos, heap barrier e leitura direta final. Qualquer generation/tail/digest
mudou: descarta plano e reinicia até deadline. `CORRUPT`/`INCONCLUSIVE`: zero write.

### 9.3 Upgrade explícito

`db.upgrade_identity_leasing(confirm_quiescent=True)` é a única porta. O booleano é somente uma
**atestação operacional explícita**, não prova quiescência e não transforma upgrade em operação
online segura. Read-only e lazy open não migram. Pré-condições:

- writer binário antigo encerrado; direct header check existe no início de toda operação pública de
  escrita e novamente dentro de COMMIT após `begin_read_view`;
- CLEAN_V1 recertificado sem lease, ou counter-only repair terminado e recertificado. A certificação
  produz `UpgradeDigest` frozen com DB UUID/generation, identidade+tamanho+SHA-256 direto de catalog e
  heap, catálogo canônico completo, inventário físico/ownership/maxima, heap0 fingerprint/page_lsn,
  checkpoint e tail proof/CommittedHistory;
- tabela IDs/capacidade v2 válidos; nenhum reserved ID legado;
- duplicatas numéricas cross-table são preservadas e não participam da recusa; duplicata dentro da
  mesma tabela continua `CORRUPT`;
- `F_initial = max(FIRST_RECORD_ID, todos os next_record_id v1 certificados,
  max_record_id_físico + 1)` com aritmética checked; a imagem eleva todos os extents reais para
  esse mesmo F e nunca reduz um contador legado;
- fresh lease, COMMIT e gap completion; ainda pre-WAL, reler diretamente catalog+heap e revalidar
  **todo** o `UpgradeDigest`, current checkpoint/history/tail e classifier. Qualquer drift, inclusive
  catálogo alterado sem extent/heap0 alterado, descarta o plan, sai de COMMIT, libera lease e reinicia
  a certificação até deadline; não existe comparação apenas de heap0;
- `WalAppendPlan` frozen fornece `T` exato; imagem v2 codifica esse mesmo T;
- batch `FORMAT_TRANSITION`, WAL barrier, apply/write-back heap0, heap barrier, direct exact verify,
  publish, release;
- nenhum range é reservado ou ativado no upgrade.

Runbook offline mínimo, normativo:

1. parar e desabilitar restart automático de todos os processos/binários pre-fence;
2. fechar handles V6 e adquirir o upgrade fence exclusivo do diretório; a API adquirirá seu próprio
   writer lease fresh e nunca aceita guard retido pelo runbook;
3. esperar expirar o TTL máximo de lease/readers/participants e provar inventário de coordenação vazio;
4. registrar backup cold, DB UUID, hashes de todos os arquivos e tail; reabrir cold read-only e verify;
5. executar a API uma única vez mantendo o fence externo até cold reopen v2 + verify;
6. só então iniciar binários V6. Qualquer processo antigo observado invalida o procedimento e exige
   restore do backup, não tentativa de downgrade.

O upgrade fence bloqueia participantes V6, mas **não pode ensinar protocolo novo a um binário
pre-fence**. Portanto nenhuma API/CLI afirma provar quiescência apenas por `confirm_quiescent=True`;
violar o runbook é precondição operacional quebrada e risco explícito de overwrite v1 tardio.

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

Assim, V6 não “completa” ou tolera buraco arbitrário. O conjunto retido é um sufixo lógico contíguo;
deletion deferral pode preservar fisicamente um nome, mas não legitima um interior ausente. Falha de
I/O que impede provar continuidade é `INCONCLUSIVE`; checksum, overlap ou descontinuidade confirmada
é `CORRUPT`. Nenhum dos dois ativa range ou publica recovery.

O item conhecido em `PUNCHLIST.md:258` sobre `_unflushed` de segmento deferred deve ser corrigido ou
formalmente excluído antes do gate v2: perder um segmento retido não pode ser reclassificado como
prefix recycling saudável.

### 10.2 Determinação de fase

Recovery lê header físico, checkpoint e `CommittedHistory` sob COMMIT:

- heap v2, `T == 0`: `V2_GENESIS`;
- heap v2, `T > 0`, `checkpoint < T`: exige batch transition exato ainda retido;
- heap v2, `T > 0`, `checkpoint >= T`: header+checkpoint são autoridade; marker pode ter sido
  reciclado apenas como prefixo;
- heap v1 com transition exato retido/commitido: crash após WAL barrier e antes de aplicar; recovery
  deve aplicar a transição;
- heap v1 cujo suposto transition estaria abaixo de checkpoint é estado impossível: checkpoint não
  poderia ultrapassar T antes de heap0 v2 durável e certificado.

Antes do exact transition, tipo 14 REQUIRED só pode ser a transition exata; outro shape recusa.
Unknown SKIPPABLE continua opaco. Depois, a gramática v2 é obrigatória.

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
mutação, adquire `INDEX_VIEW*` canônico e reentra em COMMIT. A segunda relê tail/checkpoint/history e
exige fingerprint idêntico antes do full preflight/apply; drift libera tudo e reinicia pre-mutation.
Jamais adquire `INDEX_VIEW` dentro do primeiro COMMIT nem aplica com a certificação antiga.

No SHA-base, `domain/recovery/decision.py:48-54` nem inclui control records nos efeitos e
`engine/recovery_manager.py:1350-1478` opera sobre replay achatado. Essas portas devem migrar no
slice 2/3 antes de qualquer WAL v2 existir.

### 10.4 Checkpoint

`TransactionManager.checkpoint` em `engine/txn_manager.py:996-1069` entra em COMMIT, faz redo até o
published e chama `BufferPool.checkpoint()` em `:1043`. Isso é uma operação explicitamente global:
ela pode e deve drenar todos os frames dirty, ordenar writes e executar a barreira requerida. V4
estava amplo demais ao exigir exact-write do próprio checkpoint; V6 restringe exact-write a
commit/recovery/index apply e preserva o flush global explícito do checkpoint.

V6 divide também checkpoint em certify/revalidate: um COMMIT inicial read-only enumera história e
todos os index identities, sai, adquire `INDEX_VIEW*` em ordem, reentra em COMMIT e recertifica
checkpoint/published/tail/history/index set antes de redo. Mudança reinicia sem apply; só a segunda
passagem executa os passos abaixo. Assim checkpoint nunca adquire fence enquanto possui COMMIT.

Antes de publicar `checkpoint_lsn >= T` ou reciclar prefixo, checkpoint exige:

1. grouped history íntegro e redo exato até o horizon;
2. relevant horizons calculados; cada índice clean é certificado/avançado exatamente até o novo
   checkpoint, e índice stale/incompleto recusa;
3. whole-pool checkpoint concluído;
4. data barrier dos arquivos drenados;
5. direct heap0 cert para v2 (T/generation/F/fingerprint/LSN) e direct fresh cert de todos os index
   headers;
6. publicação atômica de checkpoint;
7. só então cálculo e remoção do prefixo reciclável.

Falha em qualquer etapa não avança checkpoint nem autoriza reciclagem. Dirty unrelated é drenado
por design nesta porta, diferentemente de commit/recovery.

### 10.5 Read-only

O caminho atual em `engine/recovery_manager.py:590-663` já prova consistência sob COMMIT e, se
incompleto, lança `GrafxUnsupportedOperation(field="read_only_consistency")` em `:650-663`.
V6 preserva essa taxonomia pública, em vez de inventar `GrafxRecoveryRefused`.

Read-only só aceita quando header/checkpoint/tail/fase são auto-consistentes e não há committed
transaction após checkpoint, incomplete effects, transition pendente, floor pendente, gap ou dano.
Também lê index headers diretamente e exige stale/relevant-horizon consistentes; nunca aceita cache
porque o published token permaneceu igual. Caso contrário recusa typed e byte-identical. Um harness captura bytes, nomes, tamanhos, mtimes,
write/allocate/truncate/recycle/barrier calls antes/depois tanto do sucesso quanto da recusa.

## 11. Sections, leases, PID/fork e ordem de locks

### 11.1 Ordem única

Os únicos caminhos autorizados são:

```text
PID -> participant -> COMMIT(certification-only) -> exit COMMIT
    -> sorted INDEX_VIEW* -> COMMIT(revalidate/apply)
PID -> participant -> acquire fresh LEASE -> sorted INDEX_VIEW* -> COMMIT
    -> exit COMMIT -> release INDEX_VIEW* -> release LEASE -> activation
PID -> participant -> sorted INDEX_VIEW* -> read/precheck/traversal-or-detach/postcheck -> release
```

`INDEX_VIEW*` é vazio para reservation/control manifest sem índice. Readers nunca tentam lease ou
COMMIT enquanto possuem `INDEX_VIEW`; DDL toma old/new identities e checkpoint toma todos em ordem
canônica. Recovery separa retirement de apply: a exceção histórica COMMIT→LEASE não pode adquirir nem
mutar índice; todo apply de índice preflighta o conjunto e toma `INDEX_VIEW*` antes de COMMIT. Nenhum
caminho adquire participant, manager lock ou fence de digest menor enquanto já possui digest maior.

O manager lock nunca fica retido **atravessando** acquire/release de participant ou lease, entrada/
saída de COMMIT, device, clock, metrics ou callback. Ele pode ser tomado brevemente já dentro de
participant apenas para revalidar ticket/close/generation, mas é liberado antes de tentar lease ou
COMMIT. Há somente três encontros breves e sem nesting com lease/COMMIT:

```text
PID -> manager lock -> install REFILLING -> unlock
participant -> manager lock -> revalidate ticket -> unlock -> LEASE -> COMMIT -> release -> exit
manager lock -> activation CAS / consume / burn / revoke -> unlock -> metrics-callback
```

Status usa apenas `PID -> manager lock`. Close faz PID-first, marca o Database terminal, entra em
participant, toma manager lock, revoga grant/ticket/handles, notifica conditions, libera o lock e sai.
Um refill que instalou ticket mas ainda esperava participant acorda depois do close, falha na
revalidação pre-lease e faz zero device call. Um waiter de condition libera o manager lock enquanto
dorme e reinicia no PID/generation check; spurious wakeup nunca consome nem cria segundo refill.

Nunca: adquirir LEASE ou `INDEX_VIEW` segurando COMMIT; certificar repair/upgrade já com lease; usar a
exceção COMMIT→LEASE de retirement para index apply; chamar host code sob `INDEX_VIEW`; ativar antes
de release; manter lease entre operações identity.

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

Além das duas configs da seção 3, a API pública adiciona somente:

- `Database.upgrade_identity_leasing(confirm_quiescent=True)`;
- campos detached/immutable em status: formato heap, T, generation, F, leasing ativo, capacity,
  exhaustion e recovery-required;
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
- `identity_ids_handed_out_total`: incrementa depois de `pos += 1` e antes da exposição.
- `identity_ids_burned_total`: contabiliza apenas remainder local conhecido e descartado por refill,
  close/poison/release uncertainty ou nonactivation conhecida. Perda em hard crash é incognoscível e
  explicitamente excluída; não é inferida por scan.
- `identity_range_remaining`: gauge exato `0` ou `stop-pos` do único range ativo do manager.
- `identity_refill_seconds` e `identity_lease_wait_seconds`: histogramas observados fora de locks e
  sem executar sink hostil sob participant/COMMIT/manager lock.

“Exatamente uma vez” vale para a vida do processo, não é garantia de telemetria durável após hard
crash. Sob o lock, a operação apenas fecha um evento contábil built-in e avança o estado; depois de
liberar manager/participant/COMMIT/lease, entrega esse evento uma vez ao sink. Para handout, o lock
é liberado após `pos += 1`; a métrica é emitida antes do ID chegar ao caller. Falha do sink preserva
o ID consumido e a exceção original. Callbacks de métrica nunca decidem correctness nem substituem
prova.

### 12.3 Claim de performance honesta

Reserva faz zero scan de data pages, catálogo completo, owners ou rows. Faz leituras/escritas de
controle e uma leitura direta/write-back de heap0. Entretanto, localizar/copiar/validar a identity
entry percorre slots da page0: CPU é `O(numero_de_slots_materializados)`, limitada pela capacidade
fixa do formato da page0. Portanto V6 **não** chama o caminho de O(1) computacional.

Critérios estruturais:

- zero data-page read/allocate/write no refill;
- número de device reads não cresce com rows/data pages;
- uma imagem heap0 e um control record por reservation;
- commits unitários sem refill não tocam heap0 por identidade;
- benchmark mede, não promete, p50/p90/p99, conflitos, barriers, refills e writes para size 1, 64 e
  multiprocess.

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
| N1 | corrigir só o append identity deixaria o commit genérico com LSN preview/retarget | `txn_manager.py:1477-1497,1789-1973`; slice 2 migra o caminho genérico antes de emitir tipo 14 |
| N2 | control/phase não é inferível de `CommittedReplay` achatado | `decision.py:57-72,183-234`; grouped history é pré-requisito, não adapter opcional |
| N3 | checks em `note_*` são burláveis pelos sets crus | `context.py:210-211,283-302`; commit revalida provenance e capability, antes de lease/device |
| N4 | owner string com PID não impede uso pós-fork | `coordination_local.py:800`; todas as authorities precisam de PID-first/poison/at-fork reset |
| N5 | apply atual confunde idempotência, conflito e futuro sem prova | `buffer_pool.py:1299`; enum/fingerprint/supersession deve entrar antes de recovery v2 |
| N6 | trocar taxonomia RO quebraria API aceita | `recovery_manager.py:650-663`; preservar `GrafxUnsupportedOperation(read_only_consistency)` |
| N7 | exigir exact-write do checkpoint quebraria a semântica explícita de drain | `txn_manager.py:1043`; checkpoint whole-pool permanece e é separado de redo exact |
| N8 | allocator/extent atual pode alocar e dirty antes do WAL | `heap_store.py:538-578,1105-1125`; COW/virtual allocation integral é gate do slice 5 |
| N9 | published global torna reservation/foreign commit falsamente relevante para todo índice | `index_manager.py:623-680,1807-1872,2106-2147`; relevant-horizon + fresh direct header entram antes de control writer |
| N10 | unknown WAL não distingue trabalho obrigatório de extensão pulável | `wal/codec.py:14-17`; `wal/record.py:168-180,224-271`; required/skippable migra scanner/history/recovery/checkpoint/RO antes do tipo 14 |
| N11 | participant section não fecha a janela pre-refresh→delayed `mark_stale` entre processos | `txn_manager.py:163-168,2679-2703`; `INDEX_VIEW` estável, stale-before-unlogged-mutation e pós-validação fresh em generic/exact/HNSW entram no slice 3 |

### 13.2 Fechamento dos sete blockers da revisão V5

| ID | Blocker V5 | Resolução normativa V6 | Mutante decisivo |
|---|---|---|---|
| R1 | unicidade global incompatível com legado | identidade é `(table_id,record_id)`; F só separa números novos; zero renumeração | duas tabelas v1 com row 1 fazem upgrade e cold verify clean; duplicata intra-table recusa |
| R2 | compatible FE/FF indefinido | leitura permitida; DML/DDL FE/FF e novo DDL que os criaria recusam typed pre-staging/pre-lock/pre-device | remover cada fence ou classificar legado como corrupt |
| R3 | upgrade certificava fora do lease sem digest completo | `UpgradeDigest` catalog+heap+inventory+tail recertificado integralmente sob fresh lease+COMMIT, restart pre-WAL em drift | DDL concorrente sem extent muda só catálogo |
| R4 | cursor possuía `pos`/revogação e ordem local incompletas | manager owns pos; handle frozen nonce-bound; single-flight attempt/CAS/condition; lock nunca atravessa participant/lease/COMMIT | stale/copied handle, close-vs-refill, spurious wakeup, activation CAS removido |
| R5 | counter consumer/remainder multi-table ambíguos | grant database-bound; reservation interna antes do planner; um range serve todos os intents; `COUNTER_CONSUMER` removido | DDL+rows em 3 tabelas, reorder intent depois do consume, table switch sem burn |
| R6 | superseded podia escapar de exact write-back | APPLIED/IDENTICAL/SUPERSEDED entram sempre em touched pages | higher committed dirty no pool, retry e cold reopen |
| R7 | control-only avançava published e staleava todos os índices; pre-refresh deixava writer cross-process mutar antes de `STALE` | relevant horizon por CommitPayload/history; checkpoint certifica headers; cada read-view lê headers direct, usa `INDEX_VIEW` compartilhada e pós-valida generic/exact/HNSW | reservation/foreign table, missing index effect; pause pós-precheck + writer cross-process + delayed `mark_stale` |

## 14. Matriz explícita dos 11 blockers V3

| # | Blocker | Mecanismo V6 | Teste/mutante obrigatório |
|---|---|---|---|
| B1 | v2 não era kind-aware nem tinha limites/checksum semântico | heap-only v2; T/generation; identity codec/slot único; fingerprint canônico; capacity v2 | non-heap v2; missing/duplicate/moved slot; no-room upgrade; equal LSN com seq/CRC diferente mas semântica igual |
| B2 | RESERVED per-table contradizia DDL+primeira row | piso global F; grant database-bound; reservation interna anterior ao planner; nenhuma entry placeholder ou counter consumer | create table+node/edge/index multi-table na mesma txn; kill em todas as fronteiras; nenhum sentinel extent publicado |
| B3 | transition LSN previsto/circular e plan mutável | `WalAppendPlan` frozen genérico, tail proof completa, exact encoded blob | roll, foreign append, clock hostil, mudança de record length; mutantes `last_lsn+n`, retarget após freeze e append reencodando |
| B4 | replay perdia epoch/txn/terminal/CommitPayload | `CommittedHistory` agrupado e gramática fechada | reused txn ID cross-epoch, duplicate/missing terminal, post-terminal, marker solto, wrong payload/order/partitions |
| B5 | write/dirty pre-WAL, whole-file flush e skip de LSN sem prova | detached COW, virtual pages, full preflight, apply enum, exact touched pages | zero calls/frames pre-WAL; dirty unrelated intacto; equal-conflict e higher-unproven recusam; later exact commit subsume |
| B6 | repair podia apagar resíduo e false-clean | verifier `40a4201` preservado; classifier fechado; repair só counter raise | unreadable/inventory refusal/orphan/duplicate/cross-owner/CSN real: byte-identical refusal; v1 findings idênticos |
| B7 | namespace público forjável e reservation self-conflict | reserved IDs, format-aware catalog ceiling, module-sealed permit e rebase proof | note/bulk/direct-set forge; generic page0 disfarçada; own/foreign reservation; transition nunca isenta |
| B8 | locks, release, stale owner e takeover ambíguos | ordem única, fresh guard, multi-fence, activation pós-release, remainder nunca reclaimed | lease stolen antes/depois de cada fence; release uncertain; stale owner; retained lease; timeout/generation restart |
| B9 | phase/checkpoint/RO/transition recycle ambíguos | T exact, prefix-only, grouped simulation, checkpoint cert, RO byte-identical | T=0/T>0 antes/depois checkpoint; interior gap; marker pretransition; v1+transition pending; RO zero writes/barriers/mtime |
| B10 | overflow, rewind, abort e fork podiam reutilizar | half-open checked ranges, manager-owned pos, nonce revogável, burn semantics, increment-before-exposure e PID poison | near MAX/u64 wrap; size1; insufficient remainder; conflict/abort/KI/SE; child consume/close/release do pai |
| B11 | O(1), capacity e ordem dos slices não sustentados | claim CPU bounded por slots; zero data scan; capacity pública; writers só após recovery gate | read/write counters invariantes a rows; capacity edge; benchmarks não substituem provas; tentativa de habilitar writer antes do gate falha |

## 15. Blockers Claude L1–L4/O1/O2

| Finding Claude | Resolução V6 | Evidência/teste de fechamento |
|---|---|---|
| L1 — identity slot chegaria a `TableExtent.decode` | enumeração completa dos dois callsites produtivos do SHA 539, verifier aceito e ancestrais; dispatcher bruto antes do decoder; writer separado | `heap_store.py:1168,1214`; verifier `40a4201:635`; decoder trap mantém zero em todas as rotas |
| L2 — monotonicidade global falsa com ranges simultâneos | removida garantia cross-process; um range database-bound por manager; só remainder insuficiente queima antes de refill `max(size,n)`; identidade lógica continua table-scoped; métricas fechadas | multiprocess força inversão de ordem mas prova disjunção pós-transition e unicidade de pares; troca de tabela não refill; restart não reclama |
| L3 — rota de flush indefinida | inventário de 12 callsites diretos e cinco callers de checkpoint; commit/recovery/index apply usam touched pages; global só explicit flush/close/checkpoint/bootstrap isolado | monkeypatch whole-file flush nas rotas proibidas; dirty sentinel; cada header/index/new/tail page exigida no resultado |
| L4 — continuidade assumida sem política | normal recycle declarado e provado prefix-only; gap interior é dano/inconclusive, não estado limpo | `segment.py:119-150`; `wal_manager.py:1708-1787`; mutantes break/continue, newest e horizon |
| O1 — O(1) não sustentado | claim substituído por CPU O(slots), bounded por capacidade page0; zero data/catalog/owner scan | page-count/read-count independe de rows; slots variam até capacity e CPU é medida honestamente |
| O2 — capability abstrata | `_IdentityOccPermit` adapta `_RecoveryPermit` module-sealed, manager/txn/attempt/PID/generation-bound e revogado | `recovery_manager.py:284-338`; forge/copy/leak/reuse/cross-manager/cross-attempt recusados |

## 16. Matriz de crash, fences e publicação

Cada boundary recebe soft fault (`Exception`), process-control (`KeyboardInterrupt`/`SystemExit`) e
hard kill em processo separado. Reopen usa pool frio e verifier completo.

| Boundary | Estado obrigatório após falha/reopen |
|---|---|
| antes/depois do PID check | zero lock/I/O/métrica no objeto herdado; child incapaz de cleanup do pai |
| antes/depois do format fence público | v1 writer antigo recusa v2; bytes iguais |
| antes/depois de manager ticket/CAS/condition wake | um refill single-flight; stale attempt não ativa; close revoga sem deadlock |
| antes/depois de participant entry | nenhuma authority/range; cleanup idempotente |
| antes/depois de fresh lease acquire | sem COMMIT durante acquire; lease não retido; falha libera ou marca uncertain |
| depois de lease validate/gap completion/read view | mudança de generation/tail reinicia antes de write |
| durante clone/virtual allocation/first ou second OCC | zero allocate/write/dirty/resident mutation; range consumido localmente permanece burn-only |
| antes de `WalAppendPlan.freeze` | nada persistido; relógio/roll não escapam |
| entre freeze e `append_planned` | tail divergente recusa retryable com zero bytes |
| antes/depois de manifest validation | affected tables/scopes correspondem a todos os effects; required omission nunca vira vazio |
| antes/depois de `IndexFencePlan`, cada acquire e pre-refresh | conjunto canônico/bijetivo; timeout recusa; nenhum lock local substitui fence cross-process |
| após cada byte/record de append e antes da WAL barrier | preimage restaurado ou uncertainty latch; nunca publica/aplica |
| imediatamente após WAL barrier | transaction irrevogável; recovery completa; evento contábil fecha uma vez e só é emitido se o processo sobreviver |
| antes/depois de cada apply de new/overflow/link/catalog/index/heap0 | full preflight impede conflito parcial; kill exige grouped redo exato |
| antes/depois de STALE barrier e primeira mutação unlogged | antes do STALE não há página alterada; depois dele todo reader recusa até rebuild/clear certificado |
| antes/depois de cada exact write-back | redo idempotente; dirty unrelated não chega ao device |
| antes/depois do heap data barrier | format/reservation não publica até read direto; ordinary consumer usa WAL authority |
| antes/depois do direct heap0 verify | mismatch recusa e mantém recovery-required; não ativa range |
| antes/depois de commit.state publication | reopen completa ou observa publicado exato, nunca meio estado |
| antes/depois de COMMIT exit | lease ainda válido até exit; nenhuma activation |
| antes/durante/depois de lease release | release confirmado permite activation; failure/uncertain queima e envenena |
| antes/depois de range assignment | crash antes perde range como gap; depois usa só no mesmo manager/PID |
| antes/depois de manager increment e callback | valor nunca retorna ao manager; KI/SE preservada |
| checkpoint antes/depois de global flush/barrier/direct cert/publish/recycle | horizon só avança após cert; recycle só prefixo; tail contínuo |
| read-view antes/depois de pre-header, detach/traversal e post-header | control/foreign commit não stalea; writer cross-process entre checks serializa ou invalida generic/exact/HNSW sem resultado parcial |
| read-only em toda boundary equivalente | zero write/allocate/truncate/recycle/barrier/mtime; erro público typed atual |

Propriedade pós-kill para todo caso em que um ID foi ou pode ter sido exposto:

```text
all_physical_and_visible_(table_id,record_id)_pairs_are_unique
and direct_heap0_floor > max(exposed_or_stored_post_transition_record_id)
and verifier has no false clean
```

Duplicatas numéricas cross-table herdadas de v1 são válidas e não violam a propriedade; duas rows da
mesma tabela com o mesmo `record_id` continuam sendo dano. Não se tenta reconstruir exatamente
quantos IDs um processo morto queimou; isso seria scan e ainda
não distinguiria IDs entregues de IDs nunca observados.

## 17. Matriz adversarial e multiprocess

### 17.1 Formato/verifier

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

- segment roll exatamente no transition/control/COMMIT; append estrangeiro entre plan/append;
- txn ID repetido em epochs, missing/duplicate terminal, record pós-terminal, control pretransition;
- unknown required/skippable, bit inválido, CommitPayloadV2 manifest omitido/extra/digest divergente;
- CommitPayload com touches/partitions/terminal inconsistentes;
- checkpoint antes, em e depois de T; transition marker já reciclado somente após horizon certificado;
- first retained segment com número >1 é prefixo histórico, gap entre retained segments é dano;
- page0 à frente do checkpoint sem committed exact image;
- replay idempotente que não aplica ainda retorna touched page para write-back requerido;
- `SUPERSEDED_COMMITTED` dirty retorna touched page; cold reopen vê a imagem posterior;
- failure em list/read WAL preserva `INCONCLUSIVE` vs `CORRUPT` sem writes.

### 17.3 OCC/DDL/rows/índices

- DDL+node, DDL+edge com refs, DDL+index+row e overflow na mesma txn;
- tabela catalogada sem extent; primeira materialização sem placeholder;
- two-OCC com reservation própria incorporada, reservation alheia válida e page0 genérica alheia;
- forged identity por todas as portas públicas e por mutação direta dos sets;
- conflict/retry/KI/SE depois de IDs consumidos: gaps somente, nenhum reuse;
- exact touched pages inclui catálogo page0, chain pages, freed/relinked pages, heap0, index header,
  tail e new pages; omitir qualquer uma falha reopen/verify.
- range database-bound alimenta intents de três tabelas sem refill por troca; reorder/add/remove depois
  do consume recusa e queima; manifest affected cobre todas;
- reservations e commits de outra tabela não avançam relevant horizon; missing index effect deixa
  header curto; `mark_stale` por outro processo no mesmo published recusa no próximo read-view;
- para generic, exact e HNSW, pause após pre-refresh + writer em outro processo que altera derivadas e
  atrasa `mark_stale`: fence bloqueia a mutação ou post-validation descarta o resultado; nunca mistura.

### 17.4 Multiprocess/fork

- 3+ writers, mesma tabela e tabelas disjuntas; size 1, 64, batch maior que remainder e near MAX;
- barrier sincroniza reservas concorrentes; intervals persistidos são disjuntos apesar de ordem de
  retorno invertida;
- processo morre após reserve, após publish, antes/depois activation e no meio de `consume`;
- owner stalled/takeover: novo piso começa em F e nunca em remainder antigo;
- dois índices adquiridos em ordem inversa por mutante detectam deadlock; implementação canônica
  progride, owner death libera `INDEX_VIEW`, timeout nunca cai para participant-local;
- fork em cada fence; child não usa/fecha/release objetos do pai; fresh child connect funciona
  `file:` e `:memory:` sem deadlock em locks herdados;
- PortRegistry/sections substituídos no child por assignment e sem tocar segredo/FD herdado.

### 17.5 Performance estrutural

- refill toca heap0/control, não data/catalog/owners; instrumentar device por página;
- reads/writes não crescem com quantidade de rows; CPU cresce no máximo com slots da page0;
- commits unitários uninterrupted medem refills próximos de `ceil(n/size)`; batches/restarts reportam
  burn e refills reais, sem assertiva matemática falsa;
- comparar `tools/measure_concurrency.py` antes/depois: conflitos, p50/p90/p99, throughput, barriers,
  page0 writes e fairness; melhoria não relaxa nenhum gate de integridade.

## 18. Conflitos normativos com Round 7, W6 e CONTRACT

### 18.1 `ROUND7-PLAN.md` §4

O texto em `:392-449` propõe `_identity_leases` por tabela, `durable_end/speculative_end`, renewal na
própria txn e reuse de IDs do attempt abortado. V6 o substitui porque:

- reuse, mesmo de row abortada, viola a política “uma vez exposto/consumido, nunca reutilizar”;
- advance na mesma txn mantém page0 no consumer e não resolve o piso global DDL+primeira row;
- pool dirty pre-WAL contraria COW e recovery strict grammar já adotados no M1;
- conflito whole-page não basta para provar rebase de reservations agrupadas;
- per-table in-memory state não fecha restart, release uncertainty, fork/PID e format transition.

Também fica substituído `IDENTITY_LEASE_BLOCK=1024`: V6 usa config validada, default proposto 64, e
nenhum número é aceito por argumento apenas qualitativo. O baseline de performance de
`ROUND7-PLAN.md:365-367` é conservado como comparação, não como promessa.

### 18.2 `W6-WRITE-CEILING.md`

Option 1 em `:19-38` diz “redo unchanged” e “no on-disk format change”. Essas premissas são
incompatíveis com o piso global, DDL+row atômico, recuperação verificável, non-reuse e fork safety;
V6 é uma emenda de maior blast radius. Permanecem verdadeiros:

- a seção COMMIT exclusiva continua sendo teto de throughput (`:6-15`);
- leasing reduz conflito/waste, não remove sozinho o teto;
- Windows publication e group commit continuam complementos separados (`:66-79`).

Nenhuma métrica V6 deve anunciar que leasing por si só elevou throughput máximo.

### 18.3 `CONTRACT.md` §8.5 e §8.6

O protocolo frozen em `CONTRACT.md:657-672` é emendado apenas nos pontos necessários:

- step 3.3 usa grouped history e rebase proof fechado;
- steps 3.4–3.5 usam `WalAppendPlan` exato e preservam WAL barrier;
- step 3.6 usa preflight/apply enum e exact touched pages;
- data files continuam sem fsync para commit ordinário (`:669-670`), mas format/reservation fazem heap
  barrier antes de autorizar range/formato;
- step 3.7 permanece publication depois de aplicação válida.

Recovery de `:690-702` continua WAL-authoritative e no-undo, porém passa a preservar transações
agrupadas e controls v2. Refuse/read-only continuam non-mutating. Qualquer mudança além disso exige
nova emenda explícita.

## 19. Critérios de ACCEPT conjunto

O design só está aceito quando Codex e Claude, em revisão independente sobre o mesmo SHA-base,
confirmarem todos os itens abaixo:

1. os 11 blockers, L1–L4/O1/O2 e R1–R7/N9–N11 têm mecanismo único, sem contradição entre seções;
2. `WalAppendPlan` é implementável sem preview/retarget/clock depois do freeze e migra commit genérico;
3. required/skippable, `CommitPayloadV2` manifest e `CommittedHistory` conservam informação suficiente
   para phase, OCC, supersession, relevant horizons e recovery;
4. todos os callsites identity/decoder e todos os flush produtivos estão enumerados e testáveis;
5. verifier v1 aceito em `40a4201` permanece oracle e v2 não false-cleana estado incompleto;
6. COW puro cobre data, overflow, relink, hints, first extent, catálogo e todos os índices;
7. prefix-only/checkpoint/T/read-only formam uma máquina total, inclusive damage e I/O inconclusive;
8. capability, raw set validation, manager-owned pos/nonce/CAS, leases, sections e fork/PID não deixam
   caminho alternativo;
9. overflow, insufficient remainder, abort, close, hard crash e takeover nunca reutilizam ID;
10. performance é descrita como bounded page0 CPU/zero data scan, não O(1) nem ordem global;
11. nenhuma implementação writer v2 aparece antes dos gates de slices 1–4;
12. index header direct fresh + `INDEX_VIEW` cross-process + post-validation fecham a janela entre
    pre-refresh e `mark_stale` em generic/exact/HNSW, mesmo com LSN estável, e checkpoint certifica
    todos os headers antes de recycle;
13. upgrade preserva IDs cross-table, revalida `UpgradeDigest` sob lease e só é executado pelo runbook
    offline reconhecidamente não provado por booleano;
14. full tests, mutation battery, hard-kill multiprocess e cold reopen verifier passam sem xfail novo.

## 20. Sequência de implementação — oito slices revisados

Os oito slices são preservados em quantidade, mas sua ordem e conteúdo são corrigidos. Cada slice é
um ou mais commits mínimos coerentes; nenhum commit mistura docs/infra irrelevante. Todo slice fecha
builder tests + critic independente antes do próximo gate.

### Slice 1 — formato de leitura, namespaces, fences e PID (sem writer v2)

- kind-aware FileHeader v2 heap-only; `IdentityEntry` e iterator central com branch-before-decode;
- ceiling format-aware em catalog/schema/index/DDL; reserved OCC namespaces e raw-set rejection;
- PID/generation-first em todas as autoridades, fork child reset e poison;
- config/status/API tombstones e direct v2 fence em toda operação pública de escrita;
- verifier consegue classificar v2 fixture read-only, mas não migrar/reparar/escrever.

Commits mínimos: (1a) codecs/fixtures; (1b) namespaces/capability skeleton; (1c) PID/fork/fences.

### Slice 2 — WAL plan e histórico agrupado genéricos (ainda sem writer v2)

- required/skippable flags e `CommitPayloadV2` affected manifest entram no commit v1 genérico;
- `WalAppendPlan`/`append_planned` substituem preview+retarget no commit v1 existente;
- append uncertainty e single-use; segment-roll/tail/clock mutants;
- `CommittedTransaction/History` substitui replay achatado em commit/recovery/checkpoint;
- type 14 ainda não pertence ao enum; unknown-required recusa, unknown-skippable é preservado.

Commits mínimos: (2a) required/skippable + manifest; (2b) grouped model/projection parity; (2c) frozen
planner; (2d) generic commit cutover.

### Slice 3 — apply/durabilidade/recovery/checkpoint/RO (ainda sem writer v2)

- apply enum/fingerprint/supersession proof e full preflight;
- touched pages em heap/catalog/index; migração de todos os flushes classificados;
- relevant horizon por manifest, direct fresh index header em cada read-view e remoção do avanço global;
- `INDEX_VIEW` cross-process, pre/post-validation generic/exact/HNSW e stale-before-unlogged-mutation;
- checkpoint global certifica/avança todos os headers, prefix-only proof, read-only byte identity;
- phase/control grammar e floor simulator testados apenas com fixtures WAL.

Commits mínimos: (3a) apply enum; (3b) touched pages/flush inventory; (3c) relevant horizons/fresh
headers; (3d) shared index fences + adversarial reader/writer harness; (3e) recovery/checkpoint/RO.

### Slice 4 — certification e repair v1 + verifier v2 (ainda sem leasing)

- incorporar/preservar `40a4201`, branch v2 e state classifier total;
- repair exclusivamente counter raise COW/WAL/exact durable/direct cert;
- no false clean, no orphan deletion e upgrade preflight dry-run.

Commits mínimos: (4a) oracle merge/parity; (4b) classifier; (4c) counter-only repair.

**Gate duro:** slices 1–4 precisam de ACCEPT independente conjunto, incluindo manifest parity,
unknown-required mutants, relevant-horizon/index freshness e checkpoint baseline certificado. Até lá,
code paths de format, reservation, upgrade e consumer v2 lançam typed unsupported e não escrevem.

### Slice 5 — planner COW puro completo, ainda em paridade v1

- detached heap/catalog/index state, virtual allocator e dependency-ordered apply;
- insert/update/delete/overflow/relink/hints/first extent/DDL+row;
- structural zero-write assertion e fault injection em toda fronteira;
- v1 behavior/public results permanecem iguais.

Commits mínimos: por componente COW, depois integração DDL+rows/índices.

### Slice 6 — genesis e upgrade v2, sem grants públicos

- bootstrap novo v2/T=0; upgrade explícito T exato e format control;
- direct certification/barriers/recovery/checkpoint/read-only de transição;
- ainda não existe handle/range público e nenhum reservation é emitido; bootstrap/upgrade v2 ficam
  atrás de gate internal/test-only, e a API tombstone continua typed unsupported para não criar um
  database v2 sem allocator habilitado.

Commits mínimos: (6a) genesis; (6b) upgrade; (6c) transition recovery matrix.

### Slice 7 — reservation, manager, handle e consumers

- `_IdentityOccPermit`, `RebasedPage0Proof`, fresh leases e multi-fence;
- one-active-range/burn/refill/exhaustion/takeover/metrics;
- RANGE_RESERVATION interna anterior ao planner e consumers multi-table sem counter;
- activation-after-release e genesis/upgrade/APIs/config finally enabled atomicamente.

Commits mínimos: (7a) internal reservation; (7b) local manager/handle; (7c) consumer/OCC; (7d) enable.

### Slice 8 — fechamento adversarial e performance

- full crash matrix, mutation batteries L1/L3/B3–B11/R1–R7/N9–N11, near-MAX e hostile BaseException;
- multiprocess/fork/owner takeover/file+memory; cold verifier e zero-write RO;
- benchmark antes/depois, docs/contract amendments e operator migration/rollback runbook;
- somente após ACCEPT final: decisão separada de commit/push/milestone.

Não se antecipa writer leasing para medir performance. Recovery/checkpoint/OCC/apply precisam estar
provados antes de existir o primeiro control batch persistível.
