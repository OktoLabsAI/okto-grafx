# DESIGN V5 — Identity-range leasing

> **Status:** PROPOSTA — requer revisão conjunta Codex/Claude antes de implementação.
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
> **Rastreabilidade:** V5 preserva, mas não altera,
> `IDENTITY-LEASING-V4.md` (SHA-256
> `8301053a57e7c3e76ee15da515e76945b22aeaf8a0728d6c504f95426b154726`).
> Após ACCEPT conjunto, V5 substitui a proposta V4 e constitui emenda substitutiva ao
> `ROUND7-PLAN.md` §4 e ao Option 1 de `W6-WRITE-CEILING.md`. Até esse ACCEPT, nenhum writer,
> upgrade, migração ou leasing v2 pode ser implementado ou habilitado.

## 1. Decisão global

O contador de identidade deixa de ser um campo mutado em toda inserção e passa a ser um único
**piso global persistido** `F` no diretório do heap. Uma reserva durável transforma
`F := stop`; o intervalo `[start, stop)` torna-se autoridade local apenas depois da barreira WAL,
da aplicação exata, da durabilidade de `heap.dat`, da verificação direta e da liberação confirmada
do writer lease. IDs entregues, abandonados ou perdidos nunca voltam ao conjunto alocável.

O formato v2 é heap-only e usa materialização pura/COW. Insert, update, delete, overflow, relink,
extent hints, criação da primeira página e DDL+primeira row são planejados em imagens privadas.
Antes do primeiro byte WAL:

- zero `StorageDevice.allocate`, `write`, `truncate`, `recycle` ou `barrier`;
- zero frame novo residente, dirty ou aplicado;
- zero mutação em catálogo, heap, índice ou grants vivos;
- páginas novas são endereços virtuais do plano, não alocações físicas;
- o batch completo, seus LSNs e bytes já estão congelados.

Este desenho **não promete ordem global de IDs entre processos**. Promete somente unicidade,
disjunção entre ranges e não reutilização. Dentro de um mesmo manager vivo, IDs efetivamente
entregues crescem; entre managers, um ID maior pode ser observado antes de um menor reservado por
outro processo.

O piso global é necessário para a operação atômica “catalogar tabela + inserir primeira row”. Uma
reserva por tabela exigiria publicar um placeholder persistido antes da row ou circularmente usar
uma tabela ainda não catalogada. V5 não permite placeholder, diretório RESERVED por tabela nem
primeira materialização fora do mesmo batch consumidor.

## 2. Invariantes normativos

1. Para todo `record_id` que já tenha sido exposto por cursor/callback ou tornado visível em row, o
   heap v2 persistido deve ter `F > record_id` antes da exposição/publicação.
2. `F` é monotônico e só aumenta por batch `RANGE_RESERVATION` ou `COUNTER_CONSUMER` cometido.
3. Um ID entregue, queimado, perdido por crash ou pertencente a um manager encerrado jamais é
   reutilizado, mesmo se nenhuma row chegou a commitá-lo.
4. Cada manager possui no máximo um range ativo. Ao precisar de `n` IDs com remainder menor que
   `n`, queima o remainder inteiro **antes** de reservar `max(identity_lease_size, n)`.
5. Nenhum range fica ativo antes de writer lease liberado com sucesso. Liberação incerta queima o
   range pendente, envenena o manager e exige recovery.
6. Nenhum caminho público aceita `record_id` explícito para criação de row.
7. O cursor incrementa sua posição antes de retornar ou invocar código do usuário. `BaseException`
   do usuário queima o valor já consumido.
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

Essas invariantes governam heap v2. `_ProvisionalIdentity` ainda não exposto e pertencente a plano
prewrite descartado não é ID entregue/queimado; a distinção fechada está na seção 7. Heap v1 em
modo compatible preserva sua semântica legada e reporta leasing inativo.

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

Em `compatible`, v1 usa o allocator legado e reporta leasing inativo. Em `require`, abrir v1 para
write falha com erro typed antes de qualquer escrita; read-only v1 continua legível. O default de
formato geral não muda por causa desta feature.

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
mudança de formato/transição. Catálogo v2 recusa IDs acima de `MAX_REAL_TABLE_ID`. v1 que já use
qualquer ID reservado permanece legível em `compatible`, mas upgrade é recusado.

O mapping numérico é fechado:

```text
identity_partition()        = partition_key(0xFFFFFFFE, 0)
identity_format_partition() = partition_key(0xFFFFFFFE, 1)
page_partition(heap, 0)     = regra CRC-32C já existente em partitions.py:104-125
```

`FORMAT_TRANSITION` declara format + page0; `RANGE_RESERVATION` declara counter + page0;
`COUNTER_CONSUMER` declara counter + page0 + interesses reais; `CONSUMER` declara somente seus
interesses reais e qualquer page partition efetivamente alterada.

No SHA-base, `domain/txn/partitions.py:83-88` aceita qualquer table id e usa
`MAX_TABLE_ID` como namespace de página. `domain/txn/context.py:283-302` expõe sets públicos
mutáveis via `note_read(s)`/`note_write(s)`. Portanto:

- as portas públicas recusam os dois namespaces reservados;
- commit valida novamente os sets crus, pois um caller pode mutá-los diretamente;
- apenas `_IdentityOccPermit` pode declarar interesses identity.

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

V5 substitui isso, primeiro no caminho genérico, por um `WalAppendPlan` frozen e single-use criado
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

### 5.3 Record type e gramática fechada

Adicionar um único `WalRecordType.IDENTITY_CONTROL = 14`; o WAL continua format version 1. O
payload é fixo de 80 bytes, little-endian e auto-delimitado pelo record WAL:

```text
magic[8]          = b"OKIDV5\0\0"
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

Operações numéricas: `FORMAT_TRANSITION=1`, `RANGE_RESERVATION=2`,
`COUNTER_CONSUMER=3`; demais códigos/flags/version são recusados em fase v2. O heap é implícito e
único no database/WAL; não se serializa path ou provider-controlled file ID.

Para format, `start == stop == F` e `transition_lsn == commit_lsn == terminal`. Para reservation e
counter consumer, `start == old F`, `stop == new F`, `start < stop`, `transition_lsn == header.T` e
`commit_lsn == terminal`. O CRC do próprio WAL cobre todos os 80 bytes; `page0_sha256` cobre a
imagem semântica alvo definida na seção 3.5.

Gramática de transação:

- `FORMAT_TRANSITION`: exatamente uma `WRITE_PAGE(heap,0)`, um control format e um COMMIT;
- `RANGE_RESERVATION`: exatamente uma `WRITE_PAGE(heap,0)`, um control reservation e um COMMIT;
- `COUNTER_CONSUMER`: imagens de heap/catalog/index/rows, exatamente uma imagem heap0, um control
  consumer e um COMMIT;
- `CONSUMER`: sem control, sem avanço de `F` e sem imagem de counter; pode conter outras imagens
  legítimas.

Cada control exige `new_floor > old_floor`, salvo format sem range (`old == new`), fingerprint e
partições exatas. Marker solto, duplicado, fora de ordem ou com payload divergente é inválido.
Antes de uma transição exata, bytes tipo 14 são legado opaco; não elevam piso. A transição só é
reconhecida se seu COMMIT LSN é igual a `T` codificado na imagem de heap0. Depois de v2, qualquer
shape inválido é corrupção/refusal.

O scan base valida framing/comprimento/CRC sem decodificar eager o payload identity. Em fase v1,
somente uma transação com shape completo, imagem heap0 v2 e `T == commit_lsn` é tentativamente
decodificada como transition; outro tipo 14 permanece opaco. Isso impede que lixo pretransition
eleve F ou que um decoder novo torne WAL legado ilegível antes de determinar a fase.

## 6. Range local, reserva, exaustão e takeover

### 6.1 Estado local

Um grant frozen contém DB UUID, manager ID, origin PID, generation, table ID do consumidor, nonce,
`start`, `stop` e fingerprint da prova. Um range/cursor vivo contém apenas `pos` mutável e referência
ao grant exato. Estados:

```text
NONE -> PENDING_IRREVOCABLE -> ACTIVE -> EXHAUSTED/BURNED
                         \--> RECOVERY_REQUIRED
```

Há no máximo um `ACTIVE` por manager, não uma fila. `PENDING_IRREVOCABLE` nunca é visível a row
planning. `ACTIVE` só ocorre por assignment única depois de release confirmado. `BURNED` e
`EXHAUSTED` são terminais.

O grant é table-bound. Se uma solicitação muda de tabela, ou encontra `stop - pos < n`, o manager:

1. avança `pos := stop` e contabiliza todo remainder como queimado;
2. remove a autoridade ativa;
3. reserva exatamente `grant = max(identity_lease_size, n)` (ou o restante até a sentinela);
4. nunca soma apenas o deficit e nunca reativa o remainder antigo.

Isso fecha a contradição V4/L2: ranges simultâneos de processos diferentes tornam falsa a ordem
global, mas não afetam unicidade. O contrato de quantidade é “uma reservation por refill”. Apenas
para handouts unitários, sequenciais e sem reinício vale `ceil(handouts/lease_size)`; batches que
queimam remainder, close e crash podem elevar o número.

### 6.2 Protocolo de reserva

Reserva é permitida somente para tabela já catalogada duravelmente. A ordem fechada é:

1. PID/generation check como primeira instrução;
2. entrar na participant section;
3. adquirir writer lease **fresh**, sem segurar COMMIT;
4. entrar em COMMIT;
5. validar lease/epoch, completar qualquer COMMIT durável pendente e revalidar;
6. abrir `begin_read_view` com tail/epoch atual;
7. validar catálogo durável e segundo OCC;
8. ler `heap:0` diretamente, validar v2/T/generation/fingerprint e `page_lsn <= current`;
9. calcular `start = F`, `stop` com aritmética checked e recusar wrap/exaustão;
10. construir clone privado de heap0 e `WalAppendPlan` `RANGE_RESERVATION` estrito;
11. provar novamente PID/lease/tail e `append_planned`;
12. `wal.barrier()`; a reserva agora é irrevogável e o evento lógico de IDs reservados fecha uma vez;
13. preflight/apply exato de heap0, `write_back(heap,0)`, data barrier de heap;
14. read direto: header, T, generation, fingerprint, page_lsn e `F >= stop`;
15. publicar `commit.state`/published LSN;
16. sair de COMMIT;
17. liberar writer lease; confirmação é obrigatória;
18. sair da participant section;
19. somente agora ativar o grant por uma assignment atômica.

É proibido adquirir lease segurando COMMIT, certificar migração com writer lease, reutilizar lease
retido, publicar antes de data barrier/prova direta, ou ativar antes de release. `BaseException`
depois da barreira marca recovery; falha/ambiguidade de release queima o range pendente e envenena o
manager.

### 6.3 Exaustão e overflow

Todas as contas são u64 checked em exact built-in ints. Se `F == MAX_RECORD_ID + 1`, allocator
retorna erro typed de identity exhaustion antes de qualquer write. Se o grant pedido cruza a
sentinela, reserva somente `[F, MAX_RECORD_ID + 1)` se isso satisfaz `n`; caso contrário recusa sem
write. Nunca há wrap para `0`, truncamento, modulo ou entrega da sentinela.

### 6.4 Stale owner e takeover

O estado durável não contém owner nem remainder: contém somente `F`. Por isso takeover nunca
reclama IDs. Um manager que morre perde seu remainder, já coberto por `F`, como gap autorizado.

Um processo antigo só pode continuar seu próprio range se for o mesmo objeto, mesmo PID, mesmo DB
UUID/generation/table e, sob fresh lease+COMMIT, uma leitura direta provar `F >= stop`. Restart do
manager, fork, mismatch ou qualquer incerteza queima a autoridade. Outro processo pode reservar a
partir de `F`, jamais a partir de `pos` alheio.

### 6.5 Cursor

PID check é a primeira instrução de `next()` e de qualquer bulk/callback. Sob a serialização local:

```text
require ACTIVE and pos < stop
value = pos
pos += 1
ids_handed_out_total += 1
return/expose(value)
```

Não há rewind, clone compartilhável, pickle, fork-share ou decremento. `KeyboardInterrupt`,
`SystemExit` e falha do callback preservam a exceção e deixam o ID consumido.

## 7. Consumer e DDL+primeira row em COW puro

Tabela já catalogada mas sem range reserva antes do batch consumidor. Tabela criada dentro da
própria transação não pode fazer reserva independente; usa `COUNTER_CONSUMER`, que avança `F` e
materializa catálogo, extent e row no mesmo COMMIT.

O planner sob main lease+COMMIT:

1. completa gap, captura current/read view e executa primeiro OCC;
2. clona catálogo, heap e staging de índice em estruturas detached;
3. usa allocator virtual baseado em `page_count` e reusable-set certificado, sem device growth;
4. para nova tabela calcula `[start, stop)`, vincula IDs apenas nos clones e constrói catálogo,
   heap0, páginas de dados/overflow/refs e índices numa única imagem lógica;
5. para tabela existente consome range local, ou usa counter consumer somente na primeira
   materialização que precisa ser atômica;
6. descobre interesses reais, usa permit privado para identity quando aplicável e executa segundo
   OCC/rebase;
7. assertiva estrutural prova zero write/allocate/dirty/resident mutation desde a entrada;
8. congela `WalAppendPlan`, append exato e WAL barrier;
9. faz preflight de **todas** as imagens antes de mutar uma só;
10. aplica páginas novas/overflow, depois links alterados, depois heap0; catálogo/índices seguem sua
    dependência registrada;
11. write-back somente das touched pages; se há avanço de `F` ou format, data barrier do heap e
    prova direta de heap0;
12. publica, sai de COMMIT, libera lease e só depois ativa eventual remainder.

Antes do COMMIT, bindings públicos de row exibem `record_id = 0`/unbound; nenhum ID pending vaza.
Crash antes do primeiro byte WAL não deixa alocação ou frame. Crash após WAL barrier é resolvido por
recovery. Crash após publicação e antes da ativação queima o remainder, sem reutilização.

Um número calculado por `COUNTER_CONSUMER` antes do append é `_ProvisionalIdentity`, não grant nem
ID entregue: vive somente nos clones detached, não incrementa handout/burn e nunca alcança binding,
callback, índice vivo ou public view. Se o plano é descartado prewrite, uma tentativa posterior pode
calcular o mesmo número porque nenhuma autoridade o expôs e F não mudou. No instante da WAL barrier,
o batch inteiro se torna irrevogável e seus números passam a estar cobertos; qualquer row que não
se torne visível depois disso deixa gap, nunca reuse. IDs obtidos de range já `ACTIVE`, ao contrário,
foram consumidos no avanço do cursor e permanecem queimados mesmo em conflito prewrite.

No SHA-base, `HeapStore._extent_for` (`engine/heap_store.py:1105-1125`) aloca página antes de haver
extent, e `allocate_record_id` (`:538-578`) avança o contador via pool. Essas portas não podem ser
chamadas pelo planner v2. Row intents atualmente começam sem ID em
`domain/txn/context.py:53-71`, uma propriedade que deve ser preservada.

## 8. Apply exato, durabilidade e inventário de `flush`

### 8.1 Resultado e enum de apply

`CommitRedoResult` passa a carregar `touched_pages: tuple[(file, page_index), ...]`, ordenada e sem
duplicatas, e `files_requiring_barrier: tuple[file, ...]`, não somente `touched_files`. Até uma
imagem `IDENTICAL` precisa devolver a página quando um apply anterior pode tê-la deixado apenas no
pool: retry idempotente ainda executa o write-back exigido. Logical index apply retorna
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
`:1299`; isso mascara equal-LSN diferente e higher-LSN sem prova. V5 decodifica/fingerprinta antes
da decisão. `SUPERSEDED_COMMITTED` requer lookup no `CommittedHistory` por uma transação posterior
que contenha a imagem canônica exata. Os dois estados de conflito recusam publicação.

Toda transação é preflighted por inteiro antes do primeiro apply. `durable_exact` ordena/deduplica
e chama `write_back(file,page)` somente nas touched pages. Frames dirty não relacionados continuam
dirty e não podem ser tornados duráveis por acidente.

### 8.2 Classificação completa dos sites `BufferPool.flush` no SHA 539

Esta tabela é normativa para L3. Ela inclui a primitive, os 12 callsites diretos produtivos e as
duas rotas indiretas críticas. “Migrar” significa substituir o whole-file flush do caminho por
`durable_exact(touched_pages)`/write-back exato; “manter” exige o motivo independente do WAL.

| Evidência no SHA-base | Símbolo/uso | Decisão V5 |
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

## 9. Verifier, saneamento e upgrade

### 9.1 Oráculo preservado

O verifier aceito `40a4201` não pode regredir. Sua `_physical_heap_inventory`
(`engine/verifier.py:471-568`) inventaria owners e máximos fisicamente; falha de `page_count` produz
`FILE_UNREADABLE` e nenhum máximo. `_verify_record_id_counters` (`:571-682`) só acusa contador
quando o inventário é completo. V5 adiciona branch v2 antes do decoder, sem alterar os resultados
v1, identidades de findings, accounting, ordenação ou casefold aceitos.

No v2, verifier exige:

- exatamente uma identity entry canônica e zero slots reservados adicionais;
- extents reais únicos, catálogo/ownership coerentes e capacity respeitada;
- `F > max(record_id físico)` para todo ID entregável observado e todo extent real espelha F;
- header/T/generation válidos e page LSN justificável;
- nenhuma row/overflow/ref/index ambígua ou cross-table.

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

`db.upgrade_identity_leasing(confirm_quiescent=True)` é a única porta. Read-only e lazy open não
migram. Pré-condições:

- writer binário antigo encerrado; direct header check existe no início de toda operação pública de
  escrita e novamente dentro de COMMIT após `begin_read_view`;
- CLEAN_V1 recertificado sem lease, ou counter-only repair terminado e recertificado;
- tabela IDs/capacidade v2 válidos; nenhum reserved ID legado;
- `F_initial = max(FIRST_RECORD_ID, todos os next_record_id v1 certificados,
  max_record_id_físico + 1)` com aritmética checked; a imagem eleva todos os extents reais para
  esse mesmo F e nunca reduz um contador legado;
- fresh lease, COMMIT, gap completion e direct heap0 com LSN não maior que published;
- `WalAppendPlan` frozen fornece `T` exato; imagem v2 codifica esse mesmo T;
- batch `FORMAT_TRANSITION`, WAL barrier, apply/write-back heap0, heap barrier, direct exact verify,
  publish, release;
- nenhum range é reservado ou ativado no upgrade.

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

Assim, V5 não “completa” ou tolera buraco arbitrário. O conjunto retido é um sufixo lógico contíguo;
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

Antes do exact transition, records tipo 14 são opacos legados. Depois, a gramática v2 é obrigatória.

### 10.3 Simulação e floors

A base de `F` é confiável se o `page_lsn` de heap0 é `<= checkpoint_lsn`; se for maior, precisa ser a
imagem exata de uma transação cometida posterior. Recovery percorre `CommittedTransaction` em ordem,
valida `old_floor == simulated_floor`, fingerprint e shape, e só então avança para `new_floor`.
Consumers sem counter não avançam F. Reservation stops precisam ser contíguos a partir da base
confiável; qualquer interior ausente é dano/insuficiência, não gap de IDs autorizado.

O floor final publicado é o máximo **provado pela cadeia**, não o máximo de markers soltos. Recovery
aplica todos os efeitos exatos, `durable_exact`, e para format/counter faz heap barrier e read direto
com `F >= floor`, T/generation/fingerprint corretos antes de publicar.

No SHA-base, `domain/recovery/decision.py:48-54` nem inclui control records nos efeitos e
`engine/recovery_manager.py:1350-1478` opera sobre replay achatado. Essas portas devem migrar no
slice 2/3 antes de qualquer WAL v2 existir.

### 10.4 Checkpoint

`TransactionManager.checkpoint` em `engine/txn_manager.py:996-1069` entra em COMMIT, faz redo até o
published e chama `BufferPool.checkpoint()` em `:1043`. Isso é uma operação explicitamente global:
ela pode e deve drenar todos os frames dirty, ordenar writes e executar a barreira requerida. V4
estava amplo demais ao exigir exact-write do próprio checkpoint; V5 restringe exact-write a
commit/recovery/index apply e preserva o flush global explícito do checkpoint.

Antes de publicar `checkpoint_lsn >= T` ou reciclar prefixo, checkpoint exige:

1. grouped history íntegro e redo exato até o horizon;
2. whole-pool checkpoint concluído;
3. data barrier dos arquivos drenados;
4. direct heap0 cert para v2 (T/generation/F/fingerprint/LSN);
5. publicação atômica de checkpoint;
6. só então cálculo e remoção do prefixo reciclável.

Falha em qualquer etapa não avança checkpoint nem autoriza reciclagem. Dirty unrelated é drenado
por design nesta porta, diferentemente de commit/recovery.

### 10.5 Read-only

O caminho atual em `engine/recovery_manager.py:590-663` já prova consistência sob COMMIT e, se
incompleto, lança `GrafxUnsupportedOperation(field="read_only_consistency")` em `:650-663`.
V5 preserva essa taxonomia pública, em vez de inventar `GrafxRecoveryRefused`.

Read-only só aceita quando header/checkpoint/tail/fase são auto-consistentes e não há committed
transaction após checkpoint, incomplete effects, transition pendente, floor pendente, gap ou dano.
Caso contrário recusa typed e byte-identical. Um harness captura bytes, nomes, tamanhos, mtimes,
write/allocate/truncate/recycle/barrier calls antes/depois tanto do sucesso quanto da recusa.

## 11. Sections, leases, PID/fork e ordem de locks

### 11.1 Ordem única

Os únicos caminhos autorizados são:

```text
participant -> (certificação sem lease) -> COMMIT
participant -> acquire fresh LEASE -> COMMIT -> exit COMMIT -> release LEASE -> activation
```

Nunca: adquirir LEASE segurando COMMIT; certificar repair/upgrade já com lease; usar a exceção
COMMIT→LEASE de retirement; ativar antes de release; manter lease entre operações identity.

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
identity managers, TransactionContext, permit, WalAppendPlan, grant, range, cursor, registry e
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

Nenhum coordinator, store, manager, range, cursor, permit, callback ou container mutável é exposto
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
fixa do formato da page0. Portanto V5 **não** chama o caminho de O(1) computacional.

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

## 14. Matriz explícita dos 11 blockers V3

| # | Blocker | Mecanismo V5 | Teste/mutante obrigatório |
|---|---|---|---|
| B1 | v2 não era kind-aware nem tinha limites/checksum semântico | heap-only v2; T/generation; identity codec/slot único; fingerprint canônico; capacity v2 | non-heap v2; missing/duplicate/moved slot; no-room upgrade; equal LSN com seq/CRC diferente mas semântica igual |
| B2 | RESERVED per-table contradizia DDL+primeira row | piso global F; nenhuma entry placeholder; `COUNTER_CONSUMER` atômico | create table+node/edge/index na mesma txn; kill em todas as fronteiras; nenhum sentinel extent publicado |
| B3 | transition LSN previsto/circular e plan mutável | `WalAppendPlan` frozen genérico, tail proof completa, exact encoded blob | roll, foreign append, clock hostil, mudança de record length; mutantes `last_lsn+n`, retarget após freeze e append reencodando |
| B4 | replay perdia epoch/txn/terminal/CommitPayload | `CommittedHistory` agrupado e gramática fechada | reused txn ID cross-epoch, duplicate/missing terminal, post-terminal, marker solto, wrong payload/order/partitions |
| B5 | write/dirty pre-WAL, whole-file flush e skip de LSN sem prova | detached COW, virtual pages, full preflight, apply enum, exact touched pages | zero calls/frames pre-WAL; dirty unrelated intacto; equal-conflict e higher-unproven recusam; later exact commit subsume |
| B6 | repair podia apagar resíduo e false-clean | verifier `40a4201` preservado; classifier fechado; repair só counter raise | unreadable/inventory refusal/orphan/duplicate/cross-owner/CSN real: byte-identical refusal; v1 findings idênticos |
| B7 | namespace público forjável e reservation self-conflict | reserved IDs, format-aware catalog ceiling, module-sealed permit e rebase proof | note/bulk/direct-set forge; generic page0 disfarçada; own/foreign reservation; transition nunca isenta |
| B8 | locks, release, stale owner e takeover ambíguos | ordem única, fresh guard, multi-fence, activation pós-release, remainder nunca reclaimed | lease stolen antes/depois de cada fence; release uncertain; stale owner; retained lease; timeout/generation restart |
| B9 | phase/checkpoint/RO/transition recycle ambíguos | T exact, prefix-only, grouped simulation, checkpoint cert, RO byte-identical | T=0/T>0 antes/depois checkpoint; interior gap; marker pretransition; v1+transition pending; RO zero writes/barriers/mtime |
| B10 | overflow, rewind, abort e fork podiam reutilizar | half-open checked ranges, exhaustion marker, burn semantics, increment-before-exposure e PID poison | near MAX/u64 wrap; size1; insufficient remainder; conflict/abort/KI/SE; child next/close/release do pai |
| B11 | O(1), capacity e ordem dos slices não sustentados | claim CPU bounded por slots; zero data scan; capacity pública; writers só após recovery gate | read/write counters invariantes a rows; capacity edge; benchmarks não substituem provas; tentativa de habilitar writer antes do gate falha |

## 15. Blockers Claude L1–L4/O1/O2

| Finding Claude | Resolução V5 | Evidência/teste de fechamento |
|---|---|---|
| L1 — identity slot chegaria a `TableExtent.decode` | enumeração completa dos dois callsites produtivos do SHA 539, verifier aceito e ancestrais; dispatcher bruto antes do decoder; writer separado | `heap_store.py:1168,1214`; verifier `40a4201:635`; decoder trap mantém zero em todas as rotas |
| L2 — monotonicidade global falsa com ranges simultâneos | removida garantia cross-process; um range table-bound por manager; troca de tabela ou remainder insuficiente queima tudo antes de refill `max(size,n)`; métricas fechadas | multiprocess força inversão de ordem mas prova unicidade/disjunção; restart não reclama; counters métricos exatos |
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
| antes/depois de participant entry | nenhuma authority/range; cleanup idempotente |
| antes/depois de fresh lease acquire | sem COMMIT durante acquire; lease não retido; falha libera ou marca uncertain |
| depois de lease validate/gap completion/read view | mudança de generation/tail reinicia antes de write |
| durante clone/virtual allocation/first ou second OCC | zero allocate/write/dirty/resident mutation; range consumido localmente permanece burn-only |
| antes de `WalAppendPlan.freeze` | nada persistido; relógio/roll não escapam |
| entre freeze e `append_planned` | tail divergente recusa retryable com zero bytes |
| após cada byte/record de append e antes da WAL barrier | preimage restaurado ou uncertainty latch; nunca publica/aplica |
| imediatamente após WAL barrier | transaction irrevogável; recovery completa; evento contábil fecha uma vez e só é emitido se o processo sobreviver |
| antes/depois de cada apply de new/overflow/link/catalog/index/heap0 | full preflight impede conflito parcial; kill exige grouped redo exato |
| antes/depois de cada exact write-back | redo idempotente; dirty unrelated não chega ao device |
| antes/depois do heap data barrier | format/counter não publica até read direto; ordinary consumer usa WAL authority |
| antes/depois do direct heap0 verify | mismatch recusa e mantém recovery-required; não ativa range |
| antes/depois de commit.state publication | reopen completa ou observa publicado exato, nunca meio estado |
| antes/depois de COMMIT exit | lease ainda válido até exit; nenhuma activation |
| antes/durante/depois de lease release | release confirmado permite activation; failure/uncertain queima e envenena |
| antes/depois de range assignment | crash antes perde range como gap; depois usa só no mesmo manager/PID |
| antes/depois de cursor increment e callback | valor nunca retorna ao cursor; KI/SE preservada |
| checkpoint antes/depois de global flush/barrier/direct cert/publish/recycle | horizon só avança após cert; recycle só prefixo; tail contínuo |
| read-only em toda boundary equivalente | zero write/allocate/truncate/recycle/barrier/mtime; erro público typed atual |

Propriedade pós-kill para todo caso em que um ID foi ou pode ter sido exposto:

```text
all_physical_and_visible_ids_are_unique
and direct_heap0_floor > max(exposed_or_stored_id)
and verifier has no false clean
```

Não se tenta reconstruir exatamente quantos IDs um processo morto queimou; isso seria scan e ainda
não distinguiria IDs entregues de IDs nunca observados.

## 17. Matriz adversarial e multiprocess

### 17.1 Formato/verifier

- heap v2 missing/duplicate identity, identity fora do primeiro slot, slot real com reserved ID;
- META/CATALOG/INDEX v2, header downgrade, T/generation mudando, T errado por um LSN;
- capacity exatamente no limite e um acima; legado v1 com IDs reservados readable-compatible mas
  upgrade recusado;
- corrupt checksum, equal semantic com seq diferente, equal LSN/different semantic, higher unproven;
- inventory I/O refusal, orphan com `NO_CSN`, provisional ou CSN real, duplicates/cross-owner/refs;
- oráculo v1 `40a4201`: identidades de findings, ordering, accounting e zero maxima incompletos.

### 17.2 WAL/recovery/checkpoint

- segment roll exatamente no transition/control/COMMIT; append estrangeiro entre plan/append;
- txn ID repetido em epochs, missing/duplicate terminal, record pós-terminal, control pretransition;
- CommitPayload com touches/partitions/terminal inconsistentes;
- checkpoint antes, em e depois de T; transition marker já reciclado somente após horizon certificado;
- first retained segment com número >1 é prefixo histórico, gap entre retained segments é dano;
- page0 à frente do checkpoint sem committed exact image;
- replay idempotente que não aplica ainda retorna touched page para write-back requerido;
- failure em list/read WAL preserva `INCONCLUSIVE` vs `CORRUPT` sem writes.

### 17.3 OCC/DDL/rows/índices

- DDL+node, DDL+edge com refs, DDL+index+row e overflow na mesma txn;
- tabela catalogada sem extent; primeira materialização sem placeholder;
- two-OCC com reservation própria incorporada, reservation alheia válida e page0 genérica alheia;
- forged identity por todas as portas públicas e por mutação direta dos sets;
- conflict/retry/KI/SE depois de IDs consumidos: gaps somente, nenhum reuse;
- exact touched pages inclui catálogo page0, chain pages, freed/relinked pages, heap0, index header,
  tail e new pages; omitir qualquer uma falha reopen/verify.

### 17.4 Multiprocess/fork

- 3+ writers, mesma tabela e tabelas disjuntas; size 1, 64, batch maior que remainder e near MAX;
- barrier sincroniza reservas concorrentes; intervals persistidos são disjuntos apesar de ordem de
  retorno invertida;
- processo morre após reserve, após publish, antes/depois activation e no meio do cursor;
- owner stalled/takeover: novo piso começa em F e nunca em remainder antigo;
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
própria txn e reuse de IDs do attempt abortado. V5 o substitui porque:

- reuse, mesmo de row abortada, viola a política “uma vez exposto/consumido, nunca reutilizar”;
- advance na mesma txn mantém page0 no consumer e não resolve o piso global DDL+primeira row;
- pool dirty pre-WAL contraria COW e recovery strict grammar já adotados no M1;
- conflito whole-page não basta para provar rebase de reservations agrupadas;
- per-table in-memory state não fecha restart, release uncertainty, fork/PID e format transition.

Também fica substituído `IDENTITY_LEASE_BLOCK=1024`: V5 usa config validada, default proposto 64, e
nenhum número é aceito por argumento apenas qualitativo. O baseline de performance de
`ROUND7-PLAN.md:365-367` é conservado como comparação, não como promessa.

### 18.2 `W6-WRITE-CEILING.md`

Option 1 em `:19-38` diz “redo unchanged” e “no on-disk format change”. Essas premissas são
incompatíveis com o piso global, DDL+row atômico, recuperação verificável, non-reuse e fork safety;
V5 é uma emenda de maior blast radius. Permanecem verdadeiros:

- a seção COMMIT exclusiva continua sendo teto de throughput (`:6-15`);
- leasing reduz conflito/waste, não remove sozinho o teto;
- Windows publication e group commit continuam complementos separados (`:66-79`).

Nenhuma métrica V5 deve anunciar que leasing por si só elevou throughput máximo.

### 18.3 `CONTRACT.md` §8.5 e §8.6

O protocolo frozen em `CONTRACT.md:657-672` é emendado apenas nos pontos necessários:

- step 3.3 usa grouped history e rebase proof fechado;
- steps 3.4–3.5 usam `WalAppendPlan` exato e preservam WAL barrier;
- step 3.6 usa preflight/apply enum e exact touched pages;
- data files continuam sem fsync para commit ordinário (`:669-670`), mas format/counter fazem heap
  barrier antes de autorizar range/formato;
- step 3.7 permanece publication depois de aplicação válida.

Recovery de `:690-702` continua WAL-authoritative e no-undo, porém passa a preservar transações
agrupadas e controls v2. Refuse/read-only continuam non-mutating. Qualquer mudança além disso exige
nova emenda explícita.

## 19. Critérios de ACCEPT conjunto

O design só está aceito quando Codex e Claude, em revisão independente sobre o mesmo SHA-base,
confirmarem todos os itens abaixo:

1. os 11 blockers e L1–L4/O1/O2 têm mecanismo único, sem contradição entre seções;
2. `WalAppendPlan` é implementável sem preview/retarget/clock depois do freeze e migra commit genérico;
3. `CommittedHistory` conserva informação suficiente para phase, OCC, supersession e recovery;
4. todos os callsites identity/decoder e todos os flush produtivos estão enumerados e testáveis;
5. verifier v1 aceito em `40a4201` permanece oracle e v2 não false-cleana estado incompleto;
6. COW puro cobre data, overflow, relink, hints, first extent, catálogo e todos os índices;
7. prefix-only/checkpoint/T/read-only formam uma máquina total, inclusive damage e I/O inconclusive;
8. capability, raw set validation, leases, sections e fork/PID não deixam caminho alternativo;
9. overflow, insufficient remainder, abort, close, hard crash e takeover nunca reutilizam ID;
10. performance é descrita como bounded page0 CPU/zero data scan, não O(1) nem ordem global;
11. nenhuma implementação writer v2 aparece antes dos gates de slices 1–4;
12. full tests, mutation battery, hard-kill multiprocess e cold reopen verifier passam sem xfail novo.

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

- `WalAppendPlan`/`append_planned` substituem preview+retarget no commit v1 existente;
- append uncertainty e single-use; segment-roll/tail/clock mutants;
- `CommittedTransaction/History` substitui replay achatado em commit/recovery/checkpoint;
- record type 14 é decodificável mas não emitido; pretransition permanece opaco.

Commits mínimos: (2a) grouped model/projection parity; (2b) frozen planner; (2c) generic commit cutover.

### Slice 3 — apply/durabilidade/recovery/checkpoint/RO (ainda sem writer v2)

- apply enum/fingerprint/supersession proof e full preflight;
- touched pages em heap/catalog/index; migração de todos os flushes classificados;
- checkpoint global legítimo, prefix-only proof, read-only byte identity;
- phase/control grammar e floor simulator testados apenas com fixtures WAL.

Commits mínimos: (3a) apply enum; (3b) touched pages/flush inventory; (3c) recovery/checkpoint/RO.

### Slice 4 — certification e repair v1 + verifier v2 (ainda sem leasing)

- incorporar/preservar `40a4201`, branch v2 e state classifier total;
- repair exclusivamente counter raise COW/WAL/exact durable/direct cert;
- no false clean, no orphan deletion e upgrade preflight dry-run.

Commits mínimos: (4a) oracle merge/parity; (4b) classifier; (4c) counter-only repair.

**Gate duro:** slices 1–4 precisam de ACCEPT independente conjunto. Até lá, code paths de format,
reservation, upgrade e consumer v2 lançam typed unsupported e não escrevem.

### Slice 5 — planner COW puro completo, ainda em paridade v1

- detached heap/catalog/index state, virtual allocator e dependency-ordered apply;
- insert/update/delete/overflow/relink/hints/first extent/DDL+row;
- structural zero-write assertion e fault injection em toda fronteira;
- v1 behavior/public results permanecem iguais.

Commits mínimos: por componente COW, depois integração DDL+rows/índices.

### Slice 6 — genesis e upgrade v2, sem grants públicos

- bootstrap novo v2/T=0; upgrade explícito T exato e format control;
- direct certification/barriers/recovery/checkpoint/read-only de transição;
- ainda não existe cursor/range público e nenhum reservation é emitido.

Commits mínimos: (6a) genesis; (6b) upgrade; (6c) transition recovery matrix.

### Slice 7 — reservation, manager, cursor e consumers

- `_IdentityOccPermit`, `RebasedPage0Proof`, fresh leases e multi-fence;
- one-active-range/burn/refill/exhaustion/takeover/metrics;
- RANGE_RESERVATION, COUNTER_CONSUMER e consumers sem counter;
- activation-after-release e APIs/config finally enabled.

Commits mínimos: (7a) internal reservation; (7b) local manager/cursor; (7c) consumer/OCC; (7d) enable.

### Slice 8 — fechamento adversarial e performance

- full crash matrix, mutation batteries L1/L3/B3–B11, near-MAX e hostile BaseException;
- multiprocess/fork/owner takeover/file+memory; cold verifier e zero-write RO;
- benchmark antes/depois, docs/contract amendments e operator migration/rollback runbook;
- somente após ACCEPT final: decisão separada de commit/push/milestone.

Não se antecipa writer leasing para medir performance. Recovery/checkpoint/OCC/apply precisam estar
provados antes de existir o primeiro control batch persistível.
