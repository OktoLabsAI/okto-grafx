# CN-1 — leasing durável de faixas de identidade

## Escopo

Este documento descreve a implementação CN-1 presente no candidato de integração. Ela preserva
o contrato multi-writer/multi-reader e não altera a serialização global que já existia para
commits: a reserva usa a mesma writer lease, `COMMIT_SECTION` e guarda da cauda do WAL do commit
do usuário.

## Problema

O `next_record_id` de cada `TableExtent` reside na página 0 de `heap.dat`. Antes do CN-1, inserir
uma linha avançava esse contador pela porta geral `HeapStore.insert`, reescrevendo a página 0.
Isso fazia inserts independentes compartilharem uma página física e podia criar conflitos sem
relação lógica, inclusive entre tabelas distintas.

Uma cache apenas em memória não seria segura: dois processos poderiam entregar o mesmo id após
ler o mesmo piso. CN-1, portanto, torna o piso exclusivo da faixa uma decisão durável e usa a
cache somente como consumidor local de uma faixa já queimada.

## Invariantes

1. O piso durável de cada tabela é monotônico. Uma faixa é o intervalo semiaberto
   `[old_floor, new_floor)` gravado na página 0 antes de qualquer id desse intervalo ser usado.
2. Faixas de participantes distintos são disjuntas porque a leitura, o avanço e a publicação do
   piso ocorrem sob a ordenação global de commit já existente.
3. Ids reservados são *burn-only*: rollback, falha, close, recuperação, reabertura ou fork podem
   deixar lacunas, mas nunca tornam um id reutilizável.
4. A página 0 durável, e não a cache local, é a autoridade entre processos e após crash.
5. O primeiro OCC ocorre antes de `_prepare_identity_plan`; portanto, um conflito já visível não
   consome cache nem cria um subcommit de reserva.
6. Uma identidade explícita menor que o piso durável é sempre recusada, mesmo sem linha visível
   nesse id: ela pode pertencer a uma faixa aberta em outro participante. Uma identidade explícita
   maior ou igual ao piso só é aceita após um avanço durável que a cubra, junto com eventuais ids
   implícitos do mesmo lote.
7. DDL e a primeira linha de uma tabela permanecem no caminho legado. Sem `TableExtent`, não há
   leasing: `HeapStore.insert` cria a extensão e avança o piso atomicamente com a primeira linha.
8. O subcommit de reserva não recebe bypass. Interesses anteriores são revalidados no intervalo
   que ele criou; somente páginas físicas certificadas como materializadas da visão durável já
   posterior à reserva usam esse novo LSN como baseline. Não é permitido promover um interesse
   lógico, uma imagem pré-staged ou uma localização não medida para esse baseline.

## Protocolo de commit

`TransactionManager._commit_with_writing` mantém todo o fluxo dentro da participant section. Após
validar entradas e adquirir a writer lease, ele entra em `COMMIT_SECTION` e na guarda da cauda do
WAL, atualiza a visão durável e executa o primeiro OCC.

Somente quando esse OCC não encontra conflito, `_prepare_identity_plan` reduz os intents e:

- entrega ids de uma cache local suficiente, avançando `next_id` antes da entrega;
- queima um resto insuficiente em vez de unir duas faixas;
- ou solicita uma nova reserva para tabelas sem capacidade local.

Para uma reserva, `HeapStore.plan_record_id_floors` lê uma imagem fresca e destacada da página 0,
valida todos os avanços e produz uma única imagem copy-on-write. Sob a participant section, writer
lease, `COMMIT_SECTION` e guarda do WAL já detidas, `_commit_identity_floor_plan` cria um
`TransactionContext` privado que não entra em `_open` e não chama o commit público recursivamente.
Esse subcommit executa, nesta ordem:

1. `WRITE_PAGE(heap.dat, 0)` e `COMMIT` no WAL;
2. validação da lease e append do lote;
3. `wal.barrier()`;
4. aplicação da imagem da página 0;
5. publicação de `commit.state`.

Só depois dessa sequência o commit do usuário grava as linhas reservadas por
`HeapStore.insert_reserved`, que exige extensão existente e `record_id < next_record_id` sem
avançar novamente o piso. A emenda de baseline da segunda OCC remove o bypass por
`reservation_lsn`: interesses lógicos e páginas pré-staged congelados antes do primeiro OCC são
revalidados incrementalmente quando a reserva avança o estado durável, inclusive contra a própria
reserva. Assim, uma imagem pré-staged da antiga página 0 conflita em vez de poder rebaixar o piso.
Somente páginas físicas novas, medidas durante a materialização posterior e ausentes do conjunto
pré-staged, são validadas a partir do novo LSN durável; todos os demais interesses permanecem no
snapshot original.

Falha antes da barreira não instala faixa nem linha. Falha depois da barreira trata o subcommit
como durável, tenta completar aplicação/publicação, queima qualquer cache local e recusa o commit
do usuário com erro tipado não retryable; o usuário nunca é informado como committed por causa de
um commit apenas de metadados. O `retryable=False` é deliberado no nível do
`TransactionContext`: um contexto que já causou um efeito durável não pode entrar no retry OCC
ordinário. Uma camada como o Pulse pode inspecionar `metadata_committed=True` e
`user_transaction_committed=False`, fazer a recuperação requerida e restagear a operação em um
novo contexto; isso não torna seguro reutilizar automaticamente o contexto original.

## Cache e ciclo de vida

Cada tabela pode ter uma `_IdentityLease(next_id, stop)` em memória. A faixa é consumida somente
para inserts implícitos e nunca é devolvida ao piso. A cache é descartada ao entrar ou sair de
recuperação, ao detectar estado `recovery_required`, em `close()`, em qualquer incerteza durante a
reserva e ao detectar mudança da identidade do processo; nesse último caso, o manager também é
invalidado de modo irreversível.

Um manager herdado por fork falha fechado antes de reutilizar locks, reader pins ou faixas do
processo criador. A invalidação é irreversível naquele objeto; o processo filho deve abrir uma
nova conexão. A faixa não utilizada continua abaixo do piso durável e, por isso, permanece
queimada.

## Parametrização

`DatabaseConfig.identity_lease_size` define o tamanho da faixa local e tem padrão `64`. O valor
deve ser um inteiro estritamente positivo, é canonicalizado na configuração e é encaminhado pela
assembly ao `TransactionManager`. Um lote implícito maior que a configuração reserva capacidade
suficiente para o próprio lote; o valor não enfraquece nenhuma validação de identidade explícita.

## Interesse residual na página 0

CN-1 remove o avanço incondicional do piso no caminho de um insert já reservado, mas não remove
interesse legítimo na página 0. Primeira extensão, reparo de tail e crescimento que atualiza os
demais campos de `TableExtent` continuam declarando e gravando essa página. Esse interesse real
participa do OCC; apenas o falso compartilhamento causado exclusivamente pelo contador deixa o
caminho quente entre refills.

## Mapa dos testes introduzidos ou ajustados

| Arquivo | Cobertura CN-1 |
|---|---|
| `tests/txn/test_identity_leasing.py` | Primeira extensão no caminho legado; refill e consumo local sem nova escrita da página 0; explícito abaixo do piso com leases `1` e `64`; explícito alto com lote misto; primeiro OCC sem consumo; invalidação por fork/reopen; tabelas distintas; crescimento real da tail. |
| `tests/txn/test_identity_leasing_failures.py` | Falha de append antes da barreira; falhas de apply/publicação depois da barreira sem commit da linha; ordem WAL do subcommit antes do commit do usuário. |
| `tests/txn/test_identity_leasing_concurrency.py` | Dois processos reservando faixas disjuntas na mesma tabela; dois processos em tabelas distintas sem conflito apenas por página 0; kill após reserva durável e antes da linha, com recovery/reopen e prova de faixa queimada; recusa de id invisível dentro de uma faixa queimada. |
| `tests/storage_core/test_heap_store.py` | Planejamento COW multi-tabela e por imagem durável fresca; validação atômica/avanço estrito; `insert_reserved` dentro do piso, fora da faixa e sem primeira extensão. |
| `tests/txn/test_pending_relationship_endpoints.py` | Compatibilidade de ids planejados com endpoints e recusa de qualquer explícito abaixo do piso, inclusive lacuna. |
| `tests/txn/test_row_staging.py` | Regressão de conflito real entre writers sem depender do falso interesse incondicional na página 0. |
| `tests/foundation/test_config.py`, `tests/foundation/test_bootstrap.py`, `tests/txn/test_transaction_limits.py` | Padrão, validação/canonicalização, propagação pela assembly e validação direta de `identity_lease_size`. |

Símbolos centrais: `TransactionManager._prepare_identity_plan`,
`TransactionManager._reserve_identity_plan`, `TransactionManager._commit_identity_floor_plan`,
`TransactionManager._find_conflict`, `HeapStore.plan_record_id_floors` e
`HeapStore.insert_reserved`.
