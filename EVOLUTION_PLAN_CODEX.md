# Plano de evolução do Okto Grafx

**Data da análise:** 2026-08-23

**Estado analisado:** `main@092a60f`

**Ambiente principal:** Windows, Python 3.13.1
**Escopo:** integridade, recuperação, concorrência, estabilidade, performance, API, configuração e novas capacidades.

## Estado de execução — 2026-08-26

- **M0 estabilização: concluído e publicado** em
  `milestone/m0-stabilization@e2d6a22da8ec2571127fc9d1533995d40330c632`. Os cinco P0
  reproduzidos, as fronteiras públicas, o primeiro open durável, read-only observacional, fencing
  de recovery e retirement por geração têm regressões; a suíte global coletou 8.610 testes em 184
  módulos e terminou com 100%/exit 0. Duas auditorias independentes aceitaram o milestone.
- **M1 configuração honesta: em execução.** `checkpoint_interval_records` já está ligado ao caminho
  pós-commit com threshold exato, single-flight, retry após falha tardia e 15 regressões. O C13 de
  recall vetorial continua como gate offline independente; o knob runtime inerte
  `vector_recall_target` foi removido e seu keyword legado produz uma recusa migratória explícita.
- **Próximo gate:** concluir C13 e o censo de retornos públicos tipados, integrar serialmente no
  branch M1, executar a suíte completa e publicar um SHA imutável. M2 inicia identity-range leasing
  somente depois desse gate.
- **Compatibilidade Okto Pulse: em execução serial na `main`.** M-PULSE-0 foi concluído em
  `5b7551b40dba2facb28c46770f166ab3ac9daecc`. A fundação de identidades pendentes do
  M-PULSE-1 entrou em `d487ac9312229e0376ad8e65625213af111d9c9c`; o overlay owner-only de nós
  e suas barreiras fail-closed chegaram até `aef1df7`. O update owner-only de propriedades de
  relações committed foi publicado em `a3bb8cb44cf86f5c151a232406c703f54f9a1316`; a resolução
  pré-write de endpoints pending entrou em `d2b73bacd861982a67d4ac217a8997ed332b0aca` e a capacidade
  atômica necessária a `replace_node_payload` foi provada em
  `959bb6e313433b489211d1cb3a8c6c1bb10587c0`. A visão pública combinada de relações staged foi
  concluída em `512e2f8656bd14c9f1a7cecd3fe18caabe32cb2a`. O bridge público de valores
  temporais/vetoriais entrou na `main@f3683e3dbca731a7f027e0df30e406d805244fac`. O contrato
  esparso para embeddings nullable, necessário ao tombstone do Pulse, foi integrado na
  `main@ad38ed080c1359d347c5e0399d49d2c46eb60820`. O primeiro
  lote inativo do provider transacional Grafx foi publicado no Pulse Community em
  `milestone/grafx-graph-transaction@50cd190dc18aab199bbadeb62a9f44f4be626e03` após 36 testes e
  auditoria independente; o tombstone source-deleted atômico foi publicado sobre esse lote em
  `milestone/grafx-source-deleted-tombstone@f8769d17fff5f74b9cdd2ee220813d811b7ed9da`. O bundle
  Community coerente com o provider Kuzu foi integrado por fast-forward em
  `feature/v0.3.3@c12f4d9db662c7ba42f0f3689c30c1afab8ba620`, seguido do contrato Core em
  `feature/v0.3.3@9f6f37da0c19371781ec86abd2cae2ae8fb400d3`; os worktrees originais sujos
  permaneceram intocados. As três primitives transacionais de Spec lineage foram publicadas em
  `milestone/grafx-lineage-primitives@73b65dafb432b35ff3dea8edde8c6aa07b58c393`.
  Active-set, conformance completa e o bundle coerente ainda estão abertos; portanto
  M-PULSE-1 e a compatibilidade total não estão declarados concluídos. O registro verificável está
  em 9.7.

## 1. Resumo executivo

O Okto Grafx possui fundamentos muito bons para um banco de dados de grafo embutido e local-first:

- arquitetura hexagonal e fronteiras verificadas por testes;
- WAL com checksums, barreiras de durabilidade e replay idempotente;
- MVCC e coordenação entre processos;
- verificação estrutural, ledger forense e quarentena;
- erros tipados;
- extensa suíte de crash, concorrência, fault injection e mutação;
- documentação técnica incomumente transparente para uma versão pre-alpha.

Apesar dessa base, foram reproduzidos cinco defeitos críticos que impedem recomendar a versão atual para dados irrepetíveis:

1. recovery pode deixar um registro `COMMIT` durável invisível e bloquear escritas futuras;
2. `read_only=True` pode retornar dados antigos silenciosamente após power loss;
3. identidade e bootstrap de um banco novo não recebem uma barreira de durabilidade;
4. recovery pode truncar o WAL enquanto outro processo está escrevendo;
5. duas buscas vetoriais concorrentes podem observar um HNSW parcial sem erro.

Esses defeitos devem ser resolvidos antes de otimizações maiores ou da estabilização do formato/API. A ordem recomendada é:

1. corrigir recovery, startup e read-only;
2. corrigir a publicação concorrente do HNSW;
3. introduzir budgets de recursos e manutenção automática;
4. reduzir contenção e amplificação de I/O;
5. adicionar vacuum, backup/migração e novos access paths;
6. somente depois evoluir sincronização e capacidades local-first distribuídas.

## 2. Método e verificações

A auditoria combinou:

- leitura da composição pública, storage, buffer pool, heap, WAL, transactions, recovery, índices, query engine e vetores;
- confronto entre código, README, especificações, documentação de performance e punch list;
- execução completa da suíte existente, concluída com código de saída `0`;
- probes em bancos temporários;
- simulações de power loss com o adapter de fault injection;
- reproduções concorrentes determinísticas com barreiras controladas;
- microbenchmarks direcionais de memória, syscalls e amplificação de WAL.

Os problemas críticos abaixo são lacunas de cobertura da suíte atual, não testes existentes que já estejam falhando.

### Classificação usada

- **P0 — crítico:** pode produzir resposta silenciosamente incorreta, quebrar a recuperação/durabilidade ou deixar o banco sem progresso normal.
- **P1 — alto:** risco operacional relevante, indisponibilidade, OOM ou degradação estrutural com crescimento.
- **P2 — médio:** contrato ambíguo, extensibilidade, observabilidade, compatibilidade ou dívida que aumenta o risco de evolução.
- **P3 — baixo:** ergonomia, documentação ou otimização localizada.

## 3. Achados críticos de integridade

### P0.1 — Um `COMMIT` durável pode ficar permanentemente em limbo

O commit grava o lote no WAL e executa a barreira em [`txn_manager.py`](src/okto_grafx/engine/txn_manager.py#L789). Depois disso ainda precisa aplicar páginas, atualizar índices e publicar `commit.state` em [`txn_manager.py`](src/okto_grafx/engine/txn_manager.py#L803).

Existe, portanto, uma janela legítima de crash entre:

```text
WAL barrier concluída
        ↓
apply de heap/índices
        ↓
publicação de commit.state
```

No recovery atual:

- páginas são refeitas;
- `commit.state` não é republicado;
- registros de índice não são integralmente refeitos;
- `redo_order()` seleciona `WRITE_PAGE`, mas não todos os efeitos do commit, em [`decision.py`](src/okto_grafx/domain/recovery/decision.py#L153);
- o recovery é montado antes do `IndexManager`, em [`assembly.py`](src/okto_grafx/api/assembly.py#L236).

Reprodução após crash imediatamente depois da barreira:

```text
WAL final:                         8
commit.state antes do recovery:   4
commit.state depois do recovery:  4
records_replayed:                 4
linhas visíveis:                  ()
duas novas escritas:              write_conflict
stale_indexes:                    ()
```

As páginas do registro foram refeitas, mas o snapshot publicado permaneceu em 4. O OCC passou a encontrar repetidamente o commit oculto no WAL e nenhuma escrita normal conseguiu avançar.

#### Correção recomendada

O recovery deve ser responsável por uma unidade completa de recuperação de commit:

1. identificar o maior `COMMIT` completo e válido;
2. descartar/quarentenar transações sem `COMMIT` completo;
3. refazer catálogo, heap e todos os tipos de registro de índice;
4. flushar os arquivos afetados;
5. publicar `commit.state` atomicamente;
6. verificar que índices estão alinhados ao LSN publicado;
7. somente então liberar a primeira transação.

O contrato também deve declarar explicitamente que um `COMMIT` completo e barrierado encontrado no WAL será considerado committed mesmo quando o processo tenha morrido antes de responder ao cliente.

#### Testes necessários

- crash em cada instrução entre WAL barrier e publicação;
- reabertura pela API pública `connect()`;
- verificação de visibilidade, índices e possibilidade de novas escritas;
- repetição do recovery para provar idempotência;
- ausência e corrupção de `commit.state` com WAL íntegro.

### P0.2 — `read_only=True` pode responder com dados antigos após power loss

Páginas de heap e índice são escritas sem durability barrier — de forma deliberada, porque o WAL é a autoridade — em [`txn_manager.py`](src/okto_grafx/engine/txn_manager.py#L1538). O WAL e `commit.state`, por outro lado, recebem barriers em [`txn_manager.py`](src/okto_grafx/engine/txn_manager.py#L1593).

O open read-only pula recovery em [`assembly.py`](src/okto_grafx/api/assembly.py#L247), mas cria snapshots a partir do estado publicado em [`txn_manager.py`](src/okto_grafx/engine/txn_manager.py#L394). Não existe uma condição que exija `checkpoint_lsn >= last_committed_lsn`.

Reprodução com o fault adapter:

```text
checkpoint baseline:       LSN 4
insert publicado:          LSN 7
power loss:                heap.dat não-barrierado perdido
WAL e commit.state:        preservados
open read-only:            recovery_report=None
MATCH obtido:              ()
MATCH esperado:            ((1,),)
```

O dado continua no WAL e reaparece após um open writable, mas o leitor read-only responde silenciosamente com páginas anteriores ao snapshot que ele afirma representar.

#### Correção mínima

Recusar open read-only quando houver commits posteriores ao checkpoint e orientar:

```text
open writable → recovery → checkpoint → open read-only
```

#### Correção preferível

Suportar replay em overlay copy-on-write somente em memória:

```python
OpenOptions(
    read_only=True,
    read_only_consistency="wal_overlay",  # ou "checkpointed"
)
```

Também convém separar:

- `read_only`: não modifica dados, mas pode participar da coordenação;
- `immutable`: não realiza absolutamente nenhum write;
- `read_only_consistency="checkpointed"`: recusa se recovery seria necessário;
- `read_only_consistency="wal_overlay"`: aplica redo apenas na visão local.

### P0.3 — Identidade e bootstrap não são duráveis

`MetaStore.create()` termina em flush, não em barrier, em [`database.py`](src/okto_grafx/engine/database.py#L343). O bootstrap do catálogo e heap também executa apenas `pool.flush()` em [`assembly.py`](src/okto_grafx/api/assembly.py#L253). A operação que efetivamente estabelece durabilidade é `BufferPool.checkpoint()` em [`buffer_pool.py`](src/okto_grafx/engine/buffer_pool.py#L669).

No fault adapter, após `connect()` retornar com sucesso:

- `grafx.meta`, `catalog.dat` e `heap.dat` ainda estavam voláteis;
- nenhum barrier havia ocorrido para esses arquivos;
- um power loss removeu todos eles;
- o open seguinte criou silenciosamente outro UUID.

A identidade não é reconstruível pelo WAL. Em um banco já usado, perder apenas `grafx.meta` pode fazer uma árvore existente parecer um novo banco.

#### Correção recomendada

1. criar identidade e headers em nomes temporários ou sob um marcador `initializing`;
2. executar barriers dos arquivos e do namespace necessário;
3. publicar atomicamente um marcador `initialized`;
4. somente então retornar o primeiro `connect()`;
5. recusar ou recuperar explicitamente diretórios cuja inicialização tenha sido interrompida.

### P0.4 — Recovery não usa a exclusão usada pelo commit

Commits seguram writer lease e `COMMIT_SECTION` em [`txn_manager.py`](src/okto_grafx/engine/txn_manager.py#L739). `RecoveryManager.run()` não segura nenhum deles em [`recovery_manager.py`](src/okto_grafx/engine/recovery_manager.py#L291), embora possa preservar evidência e truncar o WAL. O próprio comentário de `Database.recover()` reconhece que a operação ocorre sem writer lease em [`database.py`](src/okto_grafx/engine/database.py#L940).

Reprodução:

1. o processo A manteve writer lease e `COMMIT_SECTION` durante um append parcial controlado;
2. o processo B executou `connect()`;
3. B terminou recovery sem esperar A;
4. B classificou a cauda ativa como dano e a truncou;
5. A terminou com `GrafxCorruptionDetected`.

Não foi demonstrado falso reconhecimento de commit, mas a exclusão prometida foi violada e um writer saudável foi derrubado.

#### Correção recomendada

- recovery deve adquirir writer lease;
- deve entrar no mesmo `COMMIT_SECTION` durante scan, preservação, truncate, redo e publicação;
- `Database.recover()` deve exigir que não existam transações locais abertas;
- alternativa: introduzir um `MAINTENANCE_SECTION` global com ordem de aquisição única e testada em relação ao lease e ao commit section.

### P0.5 — Cold-build concorrente do HNSW retorna resultado parcial

`VectorHnswIndex.graph()` publica `self._graph` antes de instalar as entradas em [`vector_engine.py`](src/okto_grafx/engine/vector_engine.py#L519). Outra busca pode invalidar e substituir esse estado em [`vector_engine.py`](src/okto_grafx/engine/vector_engine.py#L711).

Reprodução determinística com oito vetores e regime aproximado forçado:

```text
busca T2: approximate, achieved_k=5, IDs [1,2,3,4,5]
busca T1: approximate, achieved_k=1, ID  [1]
exceções: nenhuma
grafo final: 8 nós
```

Uma busca retornou silenciosamente menos vizinhos porque terminou sobre o objeto parcial que havia criado, enquanto os mapas já pertenciam ao build concorrente.

#### Correção recomendada

1. construir grafo e mapas completamente em variáveis locais;
2. representar o resultado como um bundle imutável;
3. publicar o bundle apenas depois do build completo;
4. usar lock/`Condition` single-flight;
5. permitir que outra busca aguarde ou use o último bundle completo;
6. nunca publicar estado parcial, mesmo durante falha.

## 4. Outros riscos de integridade e estabilidade

### P1.1 — Read-only ainda cria e altera índices

O assembly registra índices também no modo read-only em [`assembly.py`](src/okto_grafx/api/assembly.py#L269). `IndexManager.register()` pode criar arquivos em [`index_manager.py`](src/okto_grafx/engine/index_manager.py#L1631).

Ao remover temporariamente `index/pk_Person.idx`, um `connect(..., read_only=True)` recriou o arquivo e persistiu a marca stale. O modo precisa de um caminho `open_existing_only`: índice ausente ou atrasado deve ficar indisponível apenas em memória.

### P1.2 — Frescor de índices é parcialmente process-local

O estado stale é consultado principalmente por `_stale_reason` em [`index_manager.py`](src/okto_grafx/engine/index_manager.py#L531), e o header persistente é reavaliado na abertura. Um processo longevo pode não perceber que outro processo marcou um índice stale e continuar planejando seeks.

O frescor deve ser invalidado por um sinal compartilhado, como `built_through_lsn` e schema/index epoch verificados a cada novo read view.

### P1.3 — `INDEX_RECONCILE` não participa de todos os redos

O redo usado por checkpoint e pelo tratamento pós-barreira despacha `INDEX_WRITE`, mas não cobre de forma equivalente `INDEX_RECONCILE`, em [`txn_manager.py`](src/okto_grafx/engine/txn_manager.py#L645). Remoções reconciliadas precisam fazer parte da mesma matriz de crash/replay dos inserts e tombstones.

### P1.4 — Não existe vacuum de versões MVCC

Update anexa uma nova versão e encerra a anterior em [`heap_store.py`](src/okto_grafx/engine/heap_store.py#L717). Delete apenas marca o header e deixa os bytes em [`heap_store.py`](src/okto_grafx/engine/heap_store.py#L730). Scans continuam percorrendo todas as versões em [`heap_store.py`](src/okto_grafx/engine/heap_store.py#L742).

Consequências:

- crescimento permanente dos arquivos;
- scans progressivamente mais caros;
- aumento do custo de verify, recovery e backup;
- tombstones e estruturas derivadas consumindo memória e I/O indefinidamente.

É necessário um vacuum transacional coordenado pelo oldest-reader horizon, seguido de reconciliação de índices e reutilização segura de slots/páginas.

### P2.1 — Evidência incompleta de quarentena fica invisível

O payload da quarentena é escrito antes do manifesto em [`quarantine.py`](src/okto_grafx/engine/quarantine.py#L241). Se houver crash entre essas etapas, o diretório fica sem manifesto e as APIs normais deixam de apresentá-lo.

Recomenda-se:

- estado visível `incomplete` ou `corrupt_manifest`;
- identidade determinística por origem/faixa/digest;
- comando explícito para inspecionar e concluir/retirar entradas incompletas;
- nenhuma remoção automática de evidência.

### P2.2 — Compatibilidade futura do WAL não distingue tipos obrigatórios e ignoráveis

Tipos desconhecidos podem ser tratados como puláveis em [`record.py`](src/okto_grafx/domain/wal/record.py#L224). Isso é aceitável apenas se o tipo realmente for opcional. Um tipo futuro indispensável à correção poderia ser ignorado por uma versão antiga.

Adicionar:

- flag `required/skippable` no record;
- manifesto de capabilities/features;
- recusa fail-closed para tipo obrigatório desconhecido;
- fixtures de compatibilidade n−1/n/n+1.

## 5. Performance e escalabilidade

### P1.5 — A busca vetorial aproximada ainda possui custo O(N)

O operador de query materializa o child inteiro antes da busca em [`query_engine.py`](src/okto_grafx/engine/query_engine.py#L1523). Toda busca também chama `live_count()`, que percorre o índice em [`vector_engine.py`](src/okto_grafx/engine/vector_engine.py#L471).

Assim, o HNSW não é um access path de ponta a ponta: mesmo no regime aproximado existe trabalho linear sobre o conjunto.

Melhorias:

- fazer `VectorSearch` dirigir a leitura das linhas;
- manter cardinalidade incremental e persistida;
- evitar reler no heap candidatos já materializados;
- persistir ou reconstruir o grafo em background;
- cobrar sua memória em `vector_graph_budget_bytes`;
- adicionar `max_visits`, porque filtros seletivos podem desabilitar pruning, como registrado em [`hnsw.py`](src/okto_grafx/domain/vector/hnsw.py#L548).

### P1.6 — Queries não possuem orçamento de memória ou trabalho

O resultado é integralmente materializado e depois projetado em uma segunda tupla em [`query_engine.py`](src/okto_grafx/engine/query_engine.py#L679). `ORDER BY`, `DISTINCT`, agregações, eager rows e buscas vetoriais adicionam outras materializações.

`_Accumulator` guarda todos os valores até para `COUNT`, `SUM`, `AVG`, `MIN` e `MAX` em [`query_engine.py`](src/okto_grafx/engine/query_engine.py#L1726). Apenas `COLLECT` e variantes `DISTINCT` precisam desse comportamento.

Isso permite que um produto cartesiano ou traversal consuma memória sem limite proporcional ao banco, apesar de o modelo de segurança considerar alocação sem limite um risco em [`SECURITY.md`](SECURITY.md#L38).

Prioridades:

- `QueryCursor`/streaming;
- `query_memory_budget_bytes`;
- `max_result_rows` e `max_intermediate_rows`;
- deadline e cancel token;
- `max_traversal_paths` e `max_traversal_expansions`;
- spill para sort/group;
- top-N heap para `ORDER BY ... LIMIT`;
- acumuladores O(1) para agregações simples.

### P1.7 — Transações e lotes também não possuem quotas

`TransactionContext` acumula listas e mapas de operações, enquanto o commit mantém records, page images e um blob WAL completo em memória.

Parâmetros recomendados:

- `max_transaction_rows`;
- `max_transaction_bytes`;
- `max_wal_batch_bytes`;
- `max_statement_writes`.

No longo prazo, transações grandes podem escrever chunks WAL associados ao `txn_id`, mantendo atomicidade por um `COMMIT` final.

### P1.8 — O buffer budget subestima o RSS real

`used_bytes()` cobra apenas `frames × page_size` em [`buffer_pool.py`](src/okto_grafx/engine/buffer_pool.py#L353), ignorando objetos Python, listas de slots, conjuntos de páginas modificadas e estruturas auxiliares.

Microbenchmark com cem páginas de 8 KiB:

| Payload por slot | Orçamento nominal | Memória observada | Razão |
|---|---:|---:|---:|
| 256 B | 819 KB | 1,15 MB | 1,40× |
| 64 B | 819 KB | 1,92 MB | 2,34× |
| 1 B, patológico | 819 KB | 15,9 MB | 19,45× |

O budget deve contabilizar peso estimado dos frames e auxiliares, ou o diretório de slots deve adotar uma representação compacta.

### P1.9 — Locks globais serializam cache miss e I/O

`BufferPool.pin()` realiza eviction, read e decode sob um RLock global em [`buffer_pool.py`](src/okto_grafx/engine/buffer_pool.py#L376). O storage local usa outro RLock para `lseek/read/write` em [`storage_local.py`](src/okto_grafx/adapters/storage_local.py#L826).

Caminho recomendado:

- single-flight por frame;
- I/O fora do lock global;
- `pread`/`pwrite` ou equivalente Windows;
- lock separado para namespace e cache de handles;
- locks por arquivo/descriptor;
- preservar a regra de uma única instância residente por página.

### P1.10 — Amplificação de metadados por commit

`list_files(prefix)` percorre toda a árvore do banco em [`storage_local.py`](src/okto_grafx/adapters/storage_local.py#L649). Cada acesso a descriptor executa `stat+fstat` para verificar se o nome ainda aponta para o mesmo arquivo em [`storage_local.py`](src/okto_grafx/adapters/storage_local.py#L1145).

Cinco commits unitários aquecidos produziram aproximadamente, por commit:

- 72–82 ms;
- 76 `listdir`;
- 59 `stat`;
- 113 `fstat`;
- 8 `os.walk`;
- 9 `fsync`.

São números direcionais, não um gate formal, mas explicam boa parte do custo no Windows.

Melhorias:

- `list_files("wal/")` deve caminhar somente `wal/`;
- arquivos imutáveis não precisam da mesma revalidação de control files substituídos;
- consolidar refreshes do WAL;
- expor e medir `max_open_files`;
- manter contadores de hit/miss/eviction do descriptor cache.

### P1.11 — Heap page 0 elimina concorrência efetiva entre writers

Todo insert avança `next_record_id`, fazendo writers disjuntos conflitarem na página 0. A medição oficial registra 10,9 rows/s, 254 conflitos e caudas de segundos em [`PERFORMANCE.md`](docs/PERFORMANCE.md#L62).

O registro W6 recomenda corretamente identity-range leasing em [`W6-WRITE-CEILING.md`](docs/architecture/W6-WRITE-CEILING.md#L19).

Sequência recomendada:

1. leasing de blocos de IDs por participante;
2. medir novamente conflitos e fairness;
3. corrigir publicação de control files no Windows;
4. implementar group commit;
5. considerar páginas por tabela apenas se o caso multi-tenant justificar;
6. evitar redo lógico/mergeável até haver necessidade demonstrada.

### P1.12 — Hash indexes têm escala fixa

Índices automáticos recebem 64 buckets por padrão em [`keys.py`](src/okto_grafx/domain/index/keys.py#L49). O lookup percorre toda a cadeia daquele bucket em [`index_manager.py`](src/okto_grafx/engine/index_manager.py#L942).

São necessários:

- `expected_cardinality` ou `bucket_count`;
- rehash/rebuild online;
- eventualmente hash extensível;
- `CREATE INDEX`;
- índices compostos;
- B+tree para range, prefix e `ORDER BY`.

### P1.13 — Traversal ainda paga landing scan

Quando o destino é livre, o motor constrói um mapa da tabela de destino. A limitação está registrada em [`PERFORMANCE.md`](docs/PERFORMANCE.md#L127).

As melhorias de maior retorno são:

- índice automático `RecordId → RecordRef`;
- planner capaz de iniciar pelo lado seekable;
- fan limit baseado em cardinalidade e páginas;
- budgets de expansões/caminhos;
- operadores específicos de shortest path quando essa capacidade for adicionada.

### P2.3 — WAL usa full-page images e serializa páginas repetidamente

Cada página tocada vira um `WRITE_PAGE` integral. Em um probe:

- uma linha pequena acrescentou aproximadamente 16,8 KiB ao WAL;
- dez linhas pequenas na mesma transação acrescentaram aproximadamente 18 KiB.

Batching já amortiza muito o custo. Antes de mudar o formato WAL, priorizar:

1. `executemany`/bulk ingest;
2. eliminar encode/decode/re-encode redundante;
3. medir write amplification por tipo de workload;
4. só então avaliar WAL fisiológico/delta, pois ele aumenta substancialmente o risco de recovery.

## 6. Configuração, API e extensibilidade

### P1.14 — Dois parâmetros públicos não tinham efeito (fechado em M1)

No estado originalmente analisado, `checkpoint_interval_records` e `vector_recall_target` eram
aceitos e validados em [`config.py`](src/okto_grafx/runtime/config.py#L141), mas não participavam do
wiring. M1 ligou o primeiro ao caminho pós-commit. O segundo foi removido de `DatabaseConfig`:
recall é um SLO do gate offline, não algo que um valor passado a `connect()` consiga impor. O nome
legado recebe uma recusa tipada apontando para `bench.harness.gate --recall-target`.

Naquele estado, o próprio `Database.checkpoint()` afirmava que o WAL crescia até uma chamada manual;
essa afirmação foi substituída pelo contrato automático e pela regressão de recycle/reopen. O
`VectorEngine` já aceitava `neighbours`, `ef_construction` e `ef_search`, mas eles ainda não chegavam
à API em [`vector_engine.py`](src/okto_grafx/engine/vector_engine.py#L774).

Isso cria garantias aparentes que não existem.

Recomendação:

- implementar uma política real de checkpoint ou remover o campo;
- tratar recall como SLO medido pelo harness;
- expor parâmetros honestos de esforço da busca vetorial;
- adicionar um teste de wiring que prove que todo campo público influencia um componente ou é explicitamente informativo.

### P1.15 — OpenMetrics pode contrariar o contrato de loopback

A validação aceita `0.0.0.0:0` em [`config.py`](src/okto_grafx/runtime/config.py#L242), e o teste considera o endereço válido em [`test_config.py`](tests/foundation/test_config.py#L312). README e SECURITY afirmam que o endpoint é apenas loopback.

O default é seguro, mas um valor customizado pode expor métricas externamente.

Recomenda-se:

- rejeitar non-loopback por padrão;
- exigir `allow_remote_metrics=True` para override;
- emitir aviso de segurança;
- testar binds reais IPv4 e IPv6, não apenas parsing da configuração.

### P2.4 — Contrato de adapters customizados é ambíguo

`PortRegistry` verifica presença e callability, não comportamento. Em um probe, um clock estruturalmente válido cujo `monotonic()` lançou `RuntimeError` fez a exceção crua sair de `db.begin("read")`.

Escolher e documentar uma política:

1. adapters customizados são trusted code e não estão cobertos pela taxonomia; ou
2. toda fronteira converte `Exception` em `GrafxAdapterError`, preservando `__cause__` e sem capturar `BaseException`.

Uma capacidade importante seria `okto_grafx.testing`, com suíte pública de conformidade para storage, clock, coordinator, codec, metrics e vector math.

### P2.5 — API typed ainda retorna muitos `object`

Apesar de o pacote declarar `Typing :: Typed`, `execute`, `verify`, `recover`, `checkpoint` e várias propriedades retornam `object` em [`database.py`](src/okto_grafx/engine/database.py#L885).

Os tipos concretos já existem. Publicar facades estáveis e tipadas para:

- relatórios;
- manutenção;
- índices;
- vetores;
- ledger e quarentena.

### P2.6 — `connect(**options: object)` reduz descoberta e autocomplete

Adicionar uma entrada tipada:

```python
Database.open(config: DatabaseConfig, *, overrides: PortOverrides | None = None)
```

ou usar `TypedDict` + `Unpack` na função existente. Campos de enum devem preferir `Literal`/enum público.

### P2.7 — Exemplos oficiais de extensibilidade não executam

A documentação usa `PortRegistry.replace()` e `PortRegistry.require()`, métodos que não existem. Os exemplos de README e `docs/PORTS.md` devem ser transformados em smoke tests ou doctests executados pela CI.

Também deve haver uma política explícita de ownership: um registry default construído pelo usuário e passado como customizado atualmente pode não ser fechado pelo banco.

### P2.8 — CI não cobre todo o Python declarado

O pacote declara Python 3.11, 3.12 e 3.13 em [`pyproject.toml`](pyproject.toml#L30), mas a CI usa apenas 3.13 em [`ci.yml`](.github/workflows/ci.yml#L79).

Recomendação:

- manter suíte completa Windows/Linux em 3.13;
- executar packaging, import, type-check e smoke tests em 3.11 e 3.12;
- construir wheel e sdist e instalá-los em ambientes limpos;
- incluir um pequeno projeto consumidor verificado por mypy ou pyright.

### P2.9 — Checksum é configuração global do processo

Abrir um segundo banco com outro seletor pode trocar a implementação usada pelo primeiro. Os digests permanecem compatíveis, mas performance e diagnóstico passam a depender da ordem de abertura.

Opções:

- injetar a função CRC por instância/codec;
- instalar a preferência apenas uma vez e recusar conflitos;
- documentar explicitamente que `checksum` é process-wide.

## 7. Modelo recomendado de configuração

Separar configuração persistida da operacional reduz ambiguidades e impede que um parâmetro de runtime pareça alterar o formato em disco.

### Configuração persistida

- page size;
- formato/feature manifest;
- política de IDs e versão de hashing que alterem layout;
- definição de espaços vetoriais;
- definições de índices.

Ao abrir um banco existente, esses valores devem ser descobertos da identidade. `page_size=None`, por exemplo, poderia significar “ler do banco”, evitando exigir que o usuário memorize opções históricas.

### Configuração operacional

| Grupo | Parâmetros sugeridos |
|---|---|
| `OpenOptions` | `create=if_missing\|never\|exclusive`, `read_only`, `immutable`, `read_only_consistency` |
| `MaintenancePolicy` | `checkpoint=manual\|records\|wal_bytes\|time\|on_close`, `wal_max_bytes`, `vacuum_policy` |
| `QueryLimits` | memória, rows finais/intermediários, deadline, expansões e paths |
| `TransactionLimits` | rows, bytes, page images, WAL batch e writes por statement |
| `VectorOptions` | `M/neighbours`, `ef_construction`, `ef_search`, `max_visits`, graph budget |
| `IndexOptions` | bucket count, cardinalidade esperada e rebuild threshold |
| `StorageOptions` | `max_open_files`, retries/backoff e descriptor policy |
| `MetricsOptions` | bind, non-loopback opt-in, intervalo, rotação e timeouts |
| `ConcurrencyOptions` | `identity_lease_size`, group-commit window e batch máximo |

O alvo de recall pertence a `bench.harness.gate --recall-target`, não a `DatabaseConfig`: é um SLO
de benchmark, não uma promessa que cada query possa garantir sem um oracle exato.

## 8. Novas capacidades recomendadas

### 8.1 Backup e restore consistentes

O primeiro formato pode ser offline ou sob uma seção de manutenção:

1. recovery e checkpoint;
2. captura de manifesto, formato e checksums;
3. cópia para destino temporário;
4. verificação da cópia;
5. publicação atômica do backup concluído.

Copiar o diretório vivo diretamente não deve ser apresentado como seguro.

### 8.2 Export/import lógico versionado

É a melhor rota de migração enquanto o formato físico ainda é pre-alpha:

- formato lógico independente das páginas;
- streaming;
- checksums e manifesto de schema/features;
- import para outro diretório;
- `verify()` antes da publicação;
- nunca migrar in-place sem rollback seguro.

### 8.3 Vacuum e relatório de bloat

Adicionar:

- estimativa de versões mortas por tabela;
- bytes recuperáveis;
- oldest reader que bloqueia reclaim;
- vacuum manual primeiro;
- políticas por bytes/percentual/tempo depois.

### 8.4 Facade de manutenção e health

Exemplo:

```python
status = db.maintenance.status()
status.wal_bytes
status.checkpoint_lag_lsn
status.recovery_required
status.stale_indexes
status.heap_bloat_bytes
status.oldest_reader_age
```

Operações:

- checkpoint;
- verify;
- rebuild de índice;
- vacuum;
- backup;
- inspeção do WAL/reader pins;
- publicação explícita de métricas.

### 8.5 Streaming cursor e prepared statements

`execute()` pode continuar sendo a conveniência materializada. Adicionar:

```python
with db.query(...).cursor() as rows:
    for row in rows:
        ...
```

O cursor precisa possuir e fechar seu snapshot. Prepared statements e plan cache devem usar como chave:

- query normalizada;
- schema/catalog epoch;
- versão do planner;
- estado/frescor dos índices.

### 8.6 Bulk ingest

`executemany`, batches transacionais e importador em streaming trazem grande retorno porque as page images e barriers já são fortemente amortizadas por lote.

### 8.7 Índices e planner

- `CREATE INDEX`/`DROP INDEX`;
- índices compostos;
- índices range/prefix;
- B+tree para `ORDER BY`;
- estatísticas persistidas;
- cost model;
- `EXPLAIN` e `PROFILE`;
- índice automático `RecordId → RecordRef`.

### 8.8 Capacidades de grafo faltantes

- relationship delete;
- `DETACH DELETE`;
- read-your-own-writes com IDs provisórios e overlay transacional;
- shortest path com budgets;
- introspecção de schema;
- constraints adicionais além de primary key.

### 8.9 Changefeed local

Depois de backup/export e estabilização da semântica de recovery, adicionar um changefeed lógico e versionado:

- cursor durável;
- política de retenção;
- eventos lógicos independentes do WAL físico;
- IDs estáveis;
- base para sincronização/replicação futura.

O WAL físico não deve virar API pública porque seu formato e granularidade são detalhes internos.

## 9. Compatibilidade total com o Okto Pulse atual

**Compatibilidade analisada em:** 2026-08-25

**Baseline funcional observado:**

- Okto Grafx `main@fc32f87f73efdae10ca8404c18feaf141cacdbff`;
- Okto Pulse Community `feature/v0.3.3@0401e412c5104d7ee0ee98e7b91061f0ce94f6d2`;
- Okto Pulse Core `feature/v0.3.3@985f6a88b526bc16e0c9faa0b7f1d9b2acd27ca9`;
- contrato público de consulta `KG_QUERY_CONTRACT_VERSION = "1.0"`, em
  `../okto-pulse-core/src/okto_pulse/core/kg/query_contract.py`;
- schema de board `SCHEMA_VERSION = "0.5.0"` e Global Discovery
  `GLOBAL_SCHEMA_VERSION = "0.1.2"`.

Os dois worktrees do Pulse possuíam alterações locais durante a análise — inclusive mudanças em
`GraphTransactionScope` —, portanto os hashes acima são coordenadas da base, **não ainda um
baseline reproduzível do estado observado**. M-PULSE-0 e M-PULSE-1 podem avançar porque seus
invariantes de engine independem desse delta. Antes de fechar M-PULSE-2, o Pulse deve publicar um
commit limpo contendo o contrato corrente, e esse SHA substituirá esta nota. Nenhum gate de
compatibilidade total pode ser declarado apenas contra um worktree dirty.

A compatibilidade desta seção fica limitada ao contrato e aos fluxos efetivamente usados por esse
Pulse. Não é uma promessa de compatibilidade com todo o dialeto Kuzu, nem um alvo móvel para
features futuras do Pulse.

### 9.1 Definição de “totalmente compatível”

O Grafx será considerado totalmente compatível quando puder substituir Kuzu/Ladybug em **todo o
bundle de providers de grafo** sem alterar a semântica observada pelo Core do Pulse:

- `SemanticGraphStore`;
- `CypherExecutor` para o subconjunto público read-only 1.0;
- `GraphTransaction` e `GraphTransactionScope`;
- `GraphSchemaManager`;
- `GraphLifecycle`;
- `GraphRuntimeStore`;
- runtime e recovery de Global Discovery;
- `GraphRecovery`;
- `QuarantineRestore`.

As referências canônicas estão em
`../okto-pulse-core/src/okto_pulse/core/kg/interfaces/`, e a composição concreta atual está em
`../okto-pulse/src/okto_pulse/community/adapters/kg.py`. Compatibilidade não significa expor
`Database`, `Transaction`, `RecordId`, resultados, configurações ou exceções do Grafx ao Core.
Esses detalhes devem permanecer confinados aos adapters `grafx_*` da edição Community.

Também fazem parte do contrato:

1. mesmas linhas, colunas, tipos lógicos, ordenação e tratamento de `NULL`;
2. mesmas contagens e decisões de idempotência nas mutações;
3. atomicidade, rollback e visibilidade transacional equivalentes ou mais fortes;
4. preservação exata de nós, arestas, propriedades, layers e vetores após reopen, recovery e
   migração;
5. erros incompatíveis ou operações não implementadas devem falhar de forma tipada e explícita,
   nunca ser aceitos parcialmente;
6. o endpoint público de Cypher continua read-only e obedece ao contrato 1.0; mutações internas
   usam os ports estruturados ou um dialeto versionado e coberto pela suíte de conformidade;
7. cada write e o `COMMIT` final revalidam o fencing token/lease fornecido pelo Pulse; perder a
   autoridade durante a unidade de trabalho impede a publicação.

### 9.2 Situação atual e bloqueadores

| Área | Estado atual do Grafx | Gap para o Pulse | Severidade |
|---|---|---|---|
| CRUD básico de nós/arestas | Overlay owner-only combinado de nós e relações concluído, incluindo insert/update/delete staged | lote básico em `50cd190` e tombstone atômico em `f8769d1`, ambos ainda inativos; lineage e active-set ainda faltam | P0 |
| `DELETE`/`DETACH DELETE` | relationship delete e detach físico cobrem estado committed e cancelamento de relações staged de statements anteriores | falta mapear a exclusão destrutiva do port Pulse sempre para essa primitive | P0 |
| Transação | commit/rollback, read-your-own-writes combinado, resolução pré-write de endpoints e crash all-or-none concluídos no engine | provider básico com fencing/rollback publicado em `50cd190`; falta a superfície completa e a conformance pública do port | P0 |
| Substituição de payload | um `MATCH ... SET` único já substitui o payload e preserva identidade/arestas sob isolamento, rollback, conflito e reopen | wrapper Grafx `50cd190`, freeze Core `9f6f37d` e provider Kuzu `3a5a499` foram integrados coerentemente no bundle `c12f4d9`; o gap desta primitive está fechado, embora o provider Grafx completo continue inativo até a conformance do M-PULSE-1 | P1 |
| Cypher read-only 1.0 | subconjunto menor | faltam `OPTIONAL MATCH`, `WITH`, `UNWIND`, `UNION`, `CASE` e funções usadas | P1 |
| Schema/DDL | criação básica | faltam idempotência, evolução aditiva, múltiplos pares de endpoints e introspecção equivalente | P1 |
| Vetores | espaços e busca existem; índices vetoriais nullable são esparsos e avançam cobertura sem entrada falsa desde `main@ad38ed0` | contrato de criação de índices, filtros, ranking e tipos de retorno ainda difere | P1 |
| Lifecycle/recovery | primitivas fortes do Grafx | o provider Pulse ainda assume arquivos e procedimentos Ladybug | P1 |
| Migração | formato físico pre-alpha, sem migrador | grafo cognitivo contém dados que não podem ser sempre reconstruídos do SQL | P0 para corte |
| Performance | commits duráveis e writer serializado | adapter ingênuo por statement causaria grande regressão | P1 |

O defeito destrutivo original foi fechado em M-PULSE-0: `DELETE r` e `DETACH DELETE` estão na
`main@5b7551b`, com incidência posicional, self-loop, multiplicidade, conflito, crash, reopen e
`verify()` cobertos. A revisão do overlay de nós encontrou uma segunda janela: uma relação criada
em statement anterior da mesma transação não aparecia no heap percorrido pelo detach.
`main@aef1df7` primeiro tornou esse caso fail-closed; M-PULSE-1C o fechou positivamente em
`main@512e2f8`, cancelando a relação staged junto com o nó e preservando o restante do grafo.

O requisito de read-your-own-writes também é estrutural. O contrato do Pulse cria nós, verifica
existência e cria relações dentro do mesmo `GraphTransactionScope`, além de exigir
`replace_node_payload()` atômico preservando o multiconjunto exato de arestas. A referência está em
`../okto-pulse-core/src/okto_pulse/core/kg/interfaces/graph_transaction.py`.

Por fim, a migração não pode assumir rebuild determinístico de tudo. O próprio Pulse registra que
nós cognitivos canônicos podem não ter fonte SQL e seriam silenciosamente perdidos; ver
`../okto-pulse-core/src/okto_pulse/core/kg/canonical_cognitive_preservation.py`.

### 9.3 Contrato funcional fechado

#### 9.3.1 Nós e relacionamentos

O Grafx precisa oferecer, com atomicidade transacional:

- create, match, update e delete de nós por label e chave lógica;
- create, match, update e delete de relações, incluindo propriedades;
- `DELETE r` removendo somente as relações matched;
- `DELETE n` preservando a semântica já estabilizada do Grafx: tombstone do nó e relações incidentes
  não observáveis porque o endpoint deixa de ser visível; o provider do Pulse não usa essa operação
  para exclusão destrutiva de nó;
- `DETACH DELETE n` removendo todas as relações incidentes — incoming, outgoing, self-loop e
  múltiplas relações — e somente depois o nó;
- zero matches como no-op bem-sucedido;
- rollback completo e recovery/reopen sem relações logicamente observáveis cujo endpoint não exista;
  após `DETACH DELETE`, nenhuma relação incidente permanece fisicamente viva;
- direção, label, endpoints, propriedades e multiplicidade preservados;
- cobertura dos 11 node types, 16 relationship names e 69 pares concretos de endpoints do schema
  atual do Pulse;
- operação atômica de substituição integral do payload do nó sem trocar identidade ou arestas;
- remoção por `source_session_id`, incluindo a variante que preserva lineage de Spec;
- reconciliação e compensação de lineage/active-set com recibos completos conforme o port Pulse.

Nenhuma forma de delete pode degradar para “best effort” silencioso. Dentro do engine, uma
capacidade ainda ausente deve responder com `GrafxUnsupportedOperation` antes da primeira mutação;
o provider converte esse erro antes de chegar ao Core em `GraphCapabilityUnavailable` ou no erro
público `unsupported_operation`, conforme a porta chamada.

#### 9.3.2 Visibilidade e unidade de trabalho

A transação write deve possuir um overlay privado consultável pelo mesmo owner:

- create → match;
- create node A/B → create edge A→B;
- create/update → read das novas propriedades;
- delete → ausência nas leituras posteriores;
- unicidade/PK considerando estado committed e staged;
- consultas por scan e por índice produzem a mesma visão; no primeiro corte correto, tabelas com
  overlay podem desabilitar seeks e usar scan+overlay, deixando a otimização para depois;
- vetores staged não vazam para outros readers; enquanto o corpus Pulse não exigir consulta
  vetorial sobre overlay, esse caso recusa de forma tipada e a publicação/indexação ocorre
  atomicamente no commit;
- commit publica toda a unidade ou nada; rollback não deixa heap, índices, vetores ou relações;
- retry de conflito não reutiliza estado provisório inseguro;
- outro processo continua vendo apenas o snapshot committed;
- o fencing token/lease é verificado no início das mutações e novamente imediatamente antes da
  publicação final.

IDs físicos provisórios podem existir, mas não podem escapar pela API. A resolução de endpoints e
índices deve permanecer determinística após commit, abort, conflito e recovery.

#### 9.3.3 Consulta e expressões

O alvo é o subconjunto read-only público 1.0 e o corpus interno efetivamente emitido pelo Pulse:

- roots: `MATCH`, `OPTIONAL MATCH`, `UNWIND`, `WITH` e `RETURN`;
- composição: `WHERE`, `UNION`, `DISTINCT`, `ORDER BY`, `LIMIT` e aliases;
- booleanos e nulos: `AND`, `OR`, `NOT`, `IN`, `IS [NOT] NULL`, `TRUE/FALSE`;
- strings: `CONTAINS`, `STARTS WITH`, `ENDS WITH`;
- agregações: `COUNT`, `COLLECT`, `SUM`, `AVG`, `MIN`, `MAX`;
- expressões: `CASE/WHEN/THEN/ELSE/END`, acesso a listas/mapas e parâmetros em lote;
- funções observadas: `label`, `coalesce`, `string_split`, `size`, conversão de timestamp e as funções
  vetoriais mapeadas pelo adapter;
- `MATCH (n)` polimórfico, relação sem tipo, named paths e retorno serializável de nó, relação e
  path sem vazar `RecordId`;
- padrões directed, incoming, outgoing e undirected, incluindo hops 1 e 2 usados pelo contrato;
- envelope público preservado: normalização NFKC, parsing seguro de comments/literals, distinção
  `unsafe_cypher`/`unsupported_operation`, `auto-LIMIT`, range máximo `*..20`, rewrite de layer
  canônica, `execute_read_only_pair`, columns/row_count/truncation e contagem de linhas omitidas.

O gate não é “aceitar os tokens”. Cada forma deve ter semântica diferencial contra Ladybug para
linhas, colunas, tipos, nulidade, ordem, cardinalidade e erro. Qualquer construção fora desse
corpus deve recusar explicitamente até ser versionada. Além do corpus público read-only, a suíte
inclui as mutations internas que hoje passam por `GraphTransactionScope.execute`, como
`MATCH ... SET`, `UNWIND ... SET`, `CREATE`, relationship create/return e deletes. Elas podem ser
traduzidas pelo provider para operações estruturadas; não precisam ampliar o endpoint raw público.

Ordem só é comparada quando a query possui `ORDER BY`; nos demais casos compara-se multiset. O
Ladybug funciona como detector diferencial, não como oracle quando seu comportamento é indefinido
ou viola o port. O contrato normativo e os invariantes de dados têm precedência.

#### 9.3.4 Schema e introspecção

São requisitos:

- bootstrap repetível e idempotente (`IF NOT EXISTS` ou API equivalente);
- node tables com PK, propriedades obrigatórias/opcionais e evolução aditiva;
- relação lógica com mais de um par válido de tipos de endpoint, ainda que o adapter a materialize
  em tabelas físicas separadas;
- mapeamento estável de `STRING`, `BOOL`, `INT64`, `DOUBLE`, `TIMESTAMP`, listas e vetores de
  dimensão fixa;
- `ALTER ... ADD PROPERTY/COLUMN` idempotente ou operação de schema equivalente;
- enumeração de objetos, labels, propriedades, endpoints, PKs, espaços e índices;
- versão de schema persistida e verificável;
- criação/rebuild/inspeção de índices vetoriais;
- divergência de schema deve falhar antes de aceitar writes.

A compatibilidade não exige reproduzir texto Kuzu de introspecção internamente. Ela exige que o
adapter possa preencher `get_schema_info()`, `list_schema_objects()` e `list_node_properties()`
com os mesmos DTOs do Pulse sem scraping de arquivos privados.

#### 9.3.5 Busca vetorial

O Grafx deve cobrir os nove espaços de board declarados em
`../okto-pulse-core/src/okto_pulse/core/kg/schema_contract.py` e os quatro espaços de Global
Discovery (`Board`, `Topic`, `Entity` e `DecisionDigest`), e garantir:

- dimensão e métrica validadas no schema e em cada write;
- atualização/delete refletidos no índice e após reopen;
- filtros de board, layer e supersedence aplicados antes do resultado final;
- `top_k`, `min_similarity`, empates e under-k com resultado determinístico/documentado;
- score normalizado conforme o contrato Pulse, independentemente do score físico do Grafx;
- busca exata como oracle e ANN com gate de recall;
- nenhum HNSW parcial, stale ou de outra geração pode ser certificado como resultado válido.

#### 9.3.6 Lifecycle, recovery e migração

- cada board e Global Discovery têm binding persistido de backend, geração e versão de formato;
- diretórios Kuzu/Ladybug e Grafx são sempre separados;
- open, close, checkpoint, verify, quarantine, rebuild e recovery são oferecidos pelo provider
  completo, sem caminhos híbridos que ainda assumam `graph.lbug`;
- inspect/restore de quarentena, privacy erase, purge, footprint e budgets preservam os DTOs e as
  recusas dos ports Pulse;
- privacy erase invalida/removerá toda cópia que possa reter o board: Grafx ativo e shadow,
  gerações antigas, quarentena, backups, journal e Ladybug mantido para rollback;
- operações de Global Discovery que constroem candidato/cutover recebem e revalidam o fence antes
  da publicação;
- export/import lógico é streaming, versionado, com manifesto e checksums;
- o formato lógico inclui schema, nós, relações, propriedades, layers, vetores e IDs lógicos;
- o fingerprint lógico canônico inclui type tags e mapas completos de propriedades de nós/arestas,
  distingue propriedade ausente de propriedade presente com `NULL`, preserva relações paralelas,
  direção e multiplicidade e nunca depende de `RecordId`, página, ordem física ou nome de arquivo;
- import só é publicado depois de `verify()` e fingerprint lógico completo;
- upgrade de formato físico ocorre por export/import ou rebuild para nova geração, nunca in-place;
- versão antiga deve recusar formato obrigatório desconhecido de maneira fail-closed;
- backup/restore e rollback são exercitados antes de remover a cópia Ladybug.

### 9.4 Milestones de implementação

Os milestones abaixo são o escopo fechado da compatibilidade Pulse. Primeiro entram correções e
estabilidade; linguagem, schema e evolução vêm depois.

#### M-PULSE-0 — deletes corretos e fail-closed

**Estado em 2026-08-26:** concluído e publicado na `main@5b7551b`.

1. implementar relationship delete;
2. implementar `DETACH DELETE` atômico;
3. preservar e documentar o tombstone lógico de `DELETE n`; o provider deve mapear a exclusão
   destrutiva do Pulse para `DETACH DELETE`, que é a operação realmente usada pelo adapter atual;
4. cobrir rollback, conflito, self-loop, múltiplas direções, reopen e `verify()`;
5. corrigir documentação que hoje diverge da execução.

**Gate:** `DETACH DELETE` não deixa relação incidente física ou lógica após commit, abort, crash ou
reopen; `DELETE n` não torna uma relação observável através de endpoint tombstonado e permanece
`verify()`-clean. As regressões de relationship delete e `DETACH DELETE` devem falhar no SHA anterior.

#### M-PULSE-1 — read-your-own-writes e operações atômicas do scope

**Estado em 2026-08-26:** parcial na `main@512e2f8656bd14c9f1a7cecd3fe18caabe32cb2a`;
fundação, overlays owner-only de nós e relações, resolução pré-write de endpoints, cancelamento
staged, guardas OCC de endpoints e substituição integral de payload concluídos no engine. As
primitives estruturadas e a suíte pública do `GraphTransactionScope` no provider Pulse estão em
execução; `execute()` genérico permanece deliberadamente em M-PULSE-2.

1. overlay transacional único para heap, relações, índices e vetores;
2. endpoint lookup de nós staged;
3. delete/update staged e unicidade considerando a visão combinada;
4. `replace_node_payload` preservando arestas;
5. primitives de reconciliação/compensação necessárias ao `GraphTransactionScope`.

**Gate:** suíte pública do port roda contra Grafx; matrizes de abort/conflito/crash não deixam
efeito parcial e readers externos nunca veem staged state.

**Fechamento de escopo em 2026-08-26:** o inventário read-only de todos os métodos do
`GraphTransactionScope` atual confirmou que, fora o `execute()` genérico já atribuído ao
M-PULSE-2, nenhum método exige outro primitive de engine depois da visão combinada do M-PULSE-1C.
Create/update/snapshot/restore, supersedence, attestation, lookup de tipo e lifecycle já se apoiam
na `main`; replace de payload, lineage, active-set e deletes por sessão usam as mesmas operações de
nó/relação sob o overlay 1C. O provider Grafx deve converter timestamps ISO para o tipo armazenado,
calcular o default de attestation a partir do before-image e chamar `Transaction.commit/rollback`;
não se adicionará `timestamp()`, `coalesce()` ou statements `BEGIN/COMMIT/ROLLBACK` ao dialeto apenas
para copiar a implementação Kuzu. Esse limite impede que M-PULSE-1 se transforme em M-PULSE-2.

#### M-PULSE-2 — contrato de query Pulse 1.0

1. gerar um corpus versionado a partir do contrato e das queries reais read-only e write do Pulse;
2. implementar clauses/expressões/funções ausentes;
3. traduzir mutations internas para primitives estruturados quando isso evitar copiar DDL/procedures
   Kuzu sem benefício;
4. estabilizar DTO de resultado e taxonomia de erros;
5. adicionar o perfil `pulse-1` com `KG_QUERY_CONTRACT_VERSION=1.0`, sem alterar silenciosamente o
   dialeto default.

**Gate:** 100% do corpus read-only público e das mutations internas necessárias passa em teste
diferencial; formas fora do corpus recusam antes de executar.

#### M-PULSE-3 — schema, endpoints e introspecção

1. API idempotente de ensure/evolve schema;
2. relações lógicas multi-endpoint;
3. tipos Pulse, propriedades aditivas e constraints;
4. introspecção estável independente do layout físico;
5. manifest de capabilities e schema version.

**Gate:** bootstrap vazio, bootstrap repetido e upgrade a partir do schema anterior produzem o
mesmo fingerprint de schema esperado pelo Pulse.

#### M-PULSE-4 — paridade vetorial

1. mapear os nove espaços de board e os quatro de Global Discovery para espaços Grafx;
2. normalizar score e filtros;
3. suportar create/rebuild/status dos índices;
4. executar gates exact/ANN, cold/warm, churn e reopen.

**Gate:** top-k exato retorna os mesmos IDs elegíveis e ordenação; empates têm política
determinística e scores usam tolerâncias absoluta/relativa congeladas no fixture, não igualdade
bitwise entre backends. ANN atende o recall congelado pelo harness e nunca retorna item inelegível
por board/layer/supersedence.

#### M-PULSE-5 — export/import, backup e recovery portável

1. formato lógico versionado;
2. exporter Ladybug consistente sob freeze/snapshot e importer Grafx em streaming;
3. exporter Grafx e importer de retorno, ou journal lógico reversível enquanto rollback estiver
   aberto;
4. fingerprint lógico canônico e verificação pós-import;
5. backup/restore consistente;
6. adaptação das operações de lifecycle/recovery do Pulse sem nomes Ladybug.

O `graph_export.py` atual do Pulse não serve como formato de cutover: ele exporta um subconjunto de
campos, omite embeddings e várias propriedades e deduplica arestas por tipo/endpoints, perdendo
propriedades e multiplicidade. Ele deve ser substituído/estendido pelo exporter full-fidelity do
milestone, não reutilizado como prova de paridade.

**Gate:** round-trip completo preserva 100% dos nós, arestas, propriedades e vetores, incluindo os
nós cognitivos sem fonte SQL, e permite rollback para a geração anterior.

#### M-PULSE-6 — providers Grafx e conformance end-to-end

1. criar o bundle coerente `CommunityGrafx*` no Pulse;
2. criar um routing bundle estável que selecione backend+geração por board; cada scope usa um único
   backend coerente, e Global Discovery possui binding global separado;
3. manter nomes de configuração legados como aliases durante a transição;
4. fixar versão exata do Grafx;
5. normalizar resultados, timestamps, mapas, vetores e erros nos DTOs/taxonomia Pulse;
6. revalidar fencing em toda mutação e imediatamente antes do commit/cutover;
7. adicionar suíte diferencial por port e por fluxo de negócio.

Backend+geração são resolvidos uma vez no `begin/open` e ficam imutavelmente pinados até o scope
fechar. Cutover por CAS aguarda scopes ativos ou os invalida por fencing; uma chamada individual
nunca é reroteada no meio da operação.

**Gate:** 100% dos métodos de todos os ports enumerados em 9.1 são exercitados por provider Grafx
real, além da mesma suíte de Core com os dois bundles. Isso inclui os quatro estados de diagnóstico
non-opening do runtime, `GraphRecovery.main_untouched=True`,
`search_decision_digests(exhaustive=True)`, replace de identidade, deletes guarded versus
lifecycle-authorized, normalização de links, verification scope e flush de Global Discovery.
Nenhum import/tipo/erro Grafx aparece no Core e nenhuma operação cai silenciosamente no provider
Kuzu.

#### M-PULSE-7 — shadow, canário e corte

1. construir geração Grafx separada por cópia lógica;
2. alimentar shadow por journal/outbox durável de mutações lógicas — não por dual-write síncrono
   sem reconciliação;
3. comparar fingerprints e resultados continuamente;
4. executar canário por board com freeze curto, delta final e troca atômica do binding;
5. manter Ladybug intacto até o fim da janela de rollback;
6. depois do primeiro write aceito pelo Grafx, rollback só permanece aberto se o journal lógico
   também conseguir aplicar e confirmar o delta reverso no Ladybug; sem isso, a janela encerra
   explicitamente antes de aceitar novos writes;
7. privacy erase atravessa o router e invalida todas as cópias, inclusive a fonte de rollback,
   antes de confirmar sucesso.

**Gate de corte:** pelo menos 10.000 mutações representativas, três ciclos completos de
close/reopen/recovery e zero divergência não explicada; nenhuma falha de `verify()`; cada query do
corpus termina dentro do limite externo atual de 30 segundos do Pulse. Se o port ganhar um timeout
próprio, isso exige nova versão do contrato. O benchmark publicado deve registrar throughput,
p50/p90/p99 e pico de memória de ambos os backends antes da decisão operacional.

Antes da execução, o trace de 10.000 operações é congelado com fixture, distribuição, seed,
fingerprints esperados, cobertura de cada família de mutação e pontos de crash. “Representativo”
sozinho não pode ser usado para mudar o gate durante a rodada.

### 9.5 Política para evitar breaking changes no Pulse

1. o compatibility descriptor `pulse-1` e os DTOs do adapter são a fronteira estável. Esse
   descriptor é artefato de release/conformance, de propriedade conjunta do Grafx e do adapter
   Pulse, e registra query contract 1.0, schemas 0.5.0/0.1.2 e versões dos ports; ele não amplia os
   três booleanos atuais de `GraphCapabilities` sem uma versão própria do port;
2. toda versão consumida pelo Pulse é pinada por SHA/versão exata;
3. mudança de API/query/resultado exige nova capability version, nunca mudança silenciosa;
4. mudança de formato físico exige migrador lógico e fixture n−1/n; o Pulse não abre formato
   incompatível por tentativa;
5. aliases de configuração Kuzu existentes permanecem durante pelo menos uma release de
   transição;
6. o binding por board impede que atualizar a dependência troque o backend automaticamente;
7. workarounds específicos do Grafx não entram no Core: ficam no provider ou são recusados;
8. o endpoint raw Cypher declara backend/capability e mantém o mesmo erro público
   `unsupported_operation`/`unsafe_cypher` quando aplicável;
9. o router pode variar backend entre boards, nunca entre providers de uma mesma operação/scope.

Com essa fronteira, otimizações internas previstas neste plano — vacuum, HNSW, leasing, locks,
budgets e group commit — podem evoluir sem retrabalho no Core do Pulse. O trabalho inevitável de
uma mudança de formato ou semântica fica concentrado no Grafx, no adapter Community e no migrador.

### 9.6 Ordem e paralelismo autorizados

- M-PULSE-0 precede qualquer write em shadow.
- M-PULSE-1 precede a implementação completa do `GraphTransactionScope`.
- Depois de M-PULSE-1, M-PULSE-2, M-PULSE-3 e M-PULSE-4 podem avançar em paralelo em arquivos e
  branches isolados.
- O rascunho do formato lógico de M-PULSE-5 pode iniciar imediatamente; só é congelado depois dos
  contratos de schema e vetor de M-PULSE-3/4.
- O scaffold do provider/router/harness de M-PULSE-6 pode iniciar após M-PULSE-1; sua certificação
  final depende dos gates M-PULSE-0 a M-PULSE-5.
- M-PULSE-7 é apenas rollout; não pode ser usado para descobrir semântica básica faltante.

Cada milestone deve ter branch, commit e push próprios, suíte direcionada, suíte global verde,
revisão cruzada Codex/Claude e SHA imutável antes do merge serial em `main`.

### 9.7 Registro de execução e evidências

| Marco | Estado | Evidência integrada | Validação registrada |
|---|---|---|---|
| M-PULSE-0 | concluído | `main@5b7551b40dba2facb28c46770f166ab3ac9daecc` | suites query/API do marco: 1.398 passes e 1 skip; chunks globais: 3.841 + 1.205 + 2.021 passes, 10 skips; Ruff limpo; nove mutantes mortos; revisão cruzada sem blocker |
| M-PULSE-1A — identidade pendente | concluído | `main@d487ac9312229e0376ad8e65625213af111d9c9c` | regressões de `PendingRowRef`, redução de intents, prevalidation e bloqueios passaram; Ruff e `git diff --check` limpos após rebase |
| M-PULSE-1A — overlay de nós | concluído | `main@3f354d9b4973086421500654fe74982606767df3` + hardening `main@aef1df734582cd04097fcc0393ecda587ee5409f` | `tests/query`, `tests/txn` e `tests/api` com exit 0; 51 regressões focadas pós-auditoria com exit 0; Ruff limpo; revisão independente encontrou dois casos, ambos reproduzidos e fechados antes do merge |
| M-PULSE-1A — update de relações committed | concluído | `main@a3bb8cb44cf86f5c151a232406c703f54f9a1316` | 6 regressões públicas e 814 testes de query passaram; Ruff global e diff-check limpos; validação independente focada 6/6; preservação de endpoints, isolamento, rollback, conflito, cold reopen e `verify()` cobertos |
| M-PULSE-1B — resolução de endpoints | concluído | `main@d2b73bacd861982a67d4ac217a8997ed332b0aca` (origem revisada `1b5a4266658b186014c8159c2861755a86320b0c`; handoff Nexus `hof_5836d33c98d04c27b152dc7f56902749`) | 31 regressões novas; 1.203 testes txn/query e 612 API (1 skip) passaram antes do rebase; 178 gates de integração passaram depois do rebase; Ruff/diff-check limpos; 15 mutantes mortos; auditoria independente: 139/139 e zero blocker |
| M-PULSE-1 — capacidade `replace_node_payload` | concluído no engine; wrapper Pulse pendente | `main@959bb6e313433b489211d1cb3a8c6c1bb10587c0` | 4 regressões públicas provam substituição de 5 campos em um único `MATCH ... SET`, identidade imutável, multiconjunto exato de incoming/outgoing/self-loop/paralelas, owner/outsider, no-op, rollback, conflito, cold reopen e `verify()`; Ruff/diff-check limpos |
| M-PULSE-1C — overlay relacional (engine) | concluído | `main@512e2f8656bd14c9f1a7cecd3fe18caabe32cb2a`; branch publicado `m1/pulse-rel-overlay-hardening`; origem Claude `06869c1` + rework `3358453`; handoff Nexus verificado `hof_bbccc4744e7943e09109845d387a1a8e` | 9 regressões congeladas verdes; 1.245 testes query/txn e 536 crash/index/vector passaram no candidato integrado; docs 3/3, Ruff global e diff-check limpos; auditoria independente PASS. Owner/outsider, pending start, source+target OCC, `_write_rows == 0` no conflito, unwind de read guards, crash pré-WAL/pós-barreira, cold reopen, endpoint token fail-closed e payload+arestas estão cobertos |
| M-PULSE-1 — valores públicos para adapters | concluído | `main@f3683e3dbca731a7f027e0df30e406d805244fac` | `Timestamp` e `VectorValue` exportados pela raiz suportada, contrato e packaging atualizados; import boundary/public surface passaram; auditoria independente PASS; nenhum import privado do domínio é necessário no provider Pulse |
| M-PULSE-1 — índice vetorial nullable/esparso | concluído e integrado | `main@ad38ed080c1359d347c5e0399d49d2c46eb60820`; branch `m1/pulse-nullable-vector-index` | matriz NULL→NULL/valor, WAL, empty marker, rebuild, verifier, HNSW/exact, rollback, retarget e cold reopen cobertos; 20 testes focados e lotes amplos passaram; auditoria independente executou 180 updates NULL→NULL em 29 segmentos e mutantes críticos. A suíte global chegou a 100% com uma única expectativa CLI preexistente, reproduzida sem o commit em `main@f3683e3`; o teste obsoleto foi alinhado separadamente ao contrato owner-only em `main@6e63345a24a30f97b817decd19e46c346da99002` e o arquivo completo passou 40/40 |
| M-PULSE-1 — freeze de `replace_node_payload` no Core | concluído e integrado no branch de release atual | Pulse Core `feature/v0.3.3@9f6f37da0c19371781ec86abd2cae2ae8fb400d3`; milestone preservado em `milestone/grafx-transaction-contract`; handoff Nexus `hof_25a808d44abb4fc4a97cdf15d0fc8b91` concluído/PASS | 19/19 testes independentes; Protocol, delegação fail-closed e memory provider confirmam payload exato, identidade estrutural, multiconjunto de incoming/outgoing/self-loop/paralelas e restauração quando o publisher aplica e lança. O push ocorreu somente depois do provider Kuzu compatível |
| M-PULSE-1 — provider Grafx, primitives básicas | concluído no branch; propositalmente inativo | Pulse Community `milestone/grafx-graph-transaction@50cd190dc18aab199bbadeb62a9f44f4be626e03` | create/update/snapshot/restore, replace exato baseado no catálogo físico, supersedence, edges, cleanup, attestation, timestamps, fencing por mutação+commit, rollback após falha de commit, resultado pós-durabilidade e taxonomia/redaction cobertos; 36/36, Ruff/format/diff-check e auditoria independente PASS. `execute()`, lineage e active-set permanecem fail-closed/deferidos |
| M-PULSE-1 — tombstone source-deleted no provider Grafx | concluído e integrado; provider ainda inativo | milestone Pulse Community `f8769d17fff5f74b9cdd2ee220813d811b7ed9da`, incorporado ao bundle `feature/v0.3.3@c12f4d9db662c7ba42f0f3689c30c1afab8ba620` | swap `DETACH DELETE + CREATE` em uma única statement, payload erasure fail-closed por schema, vetores nullable, remoção de todas as relações catalogadas, retry idempotente, fencing e rollback/poison após apply-then-raise ou confirmação divergente; 46/46 no arquivo completo, 4/4 revalidados pelo Codex, Ruff/format/diff-check limpos e revisão independente sem blocker |
| M-PULSE-1 — provider Kuzu compatível com `replace_node_payload` | concluído e integrado no branch de release atual | origem `milestone/kuzu-atomic-payload-contract@3a5a499f7d2addda98f0b37ce8b9d8ed36d4025d`; cherry-pick validado no bundle Community `feature/v0.3.3@c12f4d9db662c7ba42f0f3689c30c1afab8ba620`; handoff Nexus `hof_24969ea99ef247248fd3d127138b20f7` concluído/PASS | 22/22 contra Kuzu real passaram duas vezes pelo Codex com os paths Community/Core fixados e resolução de módulos comprovada; identidade, payload integral, arestas paralelas idênticas, incoming/outgoing/same-label/self-loop, lease loss e compensação pós-COMMIT cobertos; cinco mutantes mortos; `git diff --check` limpo e Ruff TRY/I sem delta contra o baseline |
| M-PULSE-1 — bundle transacional Core/Community | concluído para este lote | Community `feature/v0.3.3@c12f4d9db662c7ba42f0f3689c30c1afab8ba620` publicado antes do Core `feature/v0.3.3@9f6f37da0c19371781ec86abd2cae2ae8fb400d3` | gate conjunto contra o mesmo contrato: Grafx 46/46, Kuzu 22/22 e Core versionado 14/14; ambos os updates foram fast-forward. Os 12 arquivos sujos do Community original e os 13 entries do Core original permaneceram exatamente no worktree local, sem reset, checkout ou sobrescrita |
| M-PULSE-1 — Spec lineage no provider Grafx | concluído no milestone; integração do bundle pendente | Pulse Community `milestone/grafx-lineage-primitives@73b65dafb432b35ff3dea8edde8c6aa07b58c393` | `reconcile`, `clear` e compensação restore-first usam uma única resolução física por operação, before-images completos, transação Grafx real e rollback/poison após qualquer falha pós-mutation. 27/27 regressões próprias e 73/73 no gate combinado passaram; quatro provas adversariais cobriram múltiplos pais, self-loop, incoming/paralelas, metadata drift e cold reopen; Ruff/format/diff-check limpos e duas validações independentes sem blocker |
| M-PULSE-1 — primitives completas `GraphTransactionScope` | em execução | próximo lote sobre os branches reproduzíveis acima | tombstone e as três primitives de lineage estão fechados; faltam as duas rotas de active-set, a integração coerente e a conformance pública completa do port. `execute()` genérico continua deliberadamente em M-PULSE-2 |

O hardening `aef1df7` existe por causa de evidência, não por expansão de escopo: a auditoria
reproduziu um `DETACH DELETE` que confirmava sucesso enquanto deixava viva uma relação staged, e um
`DELETE p, p` que recusava o segundo nome do mesmo insert. O primeiro agora falha tipado antes do
handover enquanto M-PULSE-1C não chega; o segundo é idempotente e conta uma exclusão. Nenhum
resultado desta tabela fecha M-PULSE-1 por antecipação.

O provider Grafx de `50cd190` não foi registrado na composição. Ativar somente
`graph_transaction` dividiria o board entre writes Grafx e reads/schema/lifecycle/recovery Kuzu.
A troca produtiva fica bloqueada até existir o bundle coerente de 9.1 e um catálogo físico
compartilhado por `(relationship_type, from_type, to_type)` para os 69 pares concretos; essa decisão
evita split-brain e retrabalho no Core do Pulse.

## 10. Roadmap priorizado

### Fase 0 — Correção obrigatória antes de uma release estável

1. completar recovery e publicação de commits duráveis;
2. incluir todos os efeitos de índice no redo;
3. serializar recovery contra commits;
4. tornar bootstrap e identidade duráveis;
5. corrigir semântica read-only e impedir writes de índices;
6. corrigir publicação concorrente do HNSW;
7. adicionar regressions públicas para cada janela reproduzida.

#### Gate de saída

- crash matrix completa através de `connect()`;
- nenhuma resposta silenciosamente curta/antiga;
- recovery repetido é idempotente;
- novas escritas progridem depois de qualquer crash válido;
- read-only recusa ou reproduz exatamente o estado publicado;
- duas buscas HNSW concorrentes retornam `k` correto ou uma limitação explicitamente reportada.

### Fase 1 — Limites e operação segura

1. ativar política real de checkpoint e `wal_max_bytes`;
2. introduzir budgets de query e transação;
3. tornar frescor de índices compartilhado;
4. restringir OpenMetrics a loopback;
5. corrigir contrato de adapters e exemplos;
6. tipar a facade pública;
7. ampliar CI para Python 3.11/3.12;
8. expor health/maintenance.

#### Gate de saída

- nenhum parâmetro público inerte;
- peak RSS dentro do budget definido, com tolerância documentada;
- queries e transações grandes recusam com erro tipado antes de OOM;
- WAL permanece dentro da política configurada;
- documentação pública executada na CI.

### Fase 2 — Performance estrutural

1. identity-range leasing;
2. reduzir `os.walk`, `stat/fstat` e locks globais;
3. transformar HNSW em access path real;
4. corrigir agregadores e top-N;
5. implementar bulk ingest;
6. corrigir publicação Windows;
7. adicionar group commit;
8. adicionar vacuum, índice de identidade e rehash/rebuild de índices.

#### Gate de saída

- benchmark de concorrência sem retry storm na página 0;
- crescimento sublinear ou claramente limitado para point lookup, traversal e ANN;
- gates cold/warm HNSW, filtros seletivos e recall;
- métricas habilitadas não mudam a ordem de complexidade das operações;
- benchmark de pico de memória além de latência.

### Fase 3 — Produto e ecossistema local-first

1. backup/restore;
2. export/import/migração;
3. secondary indexes e `EXPLAIN`/`PROFILE`;
4. changefeed lógico;
5. sincronização/replicação opcional;
6. política explícita de conflitos distribuídos;
7. criptografia em repouso somente após threat model completo.

## 11. Estratégia de testes recomendada

### Recovery e power loss

- crash em cada write/barrier/replace do bootstrap;
- crash em cada etapa entre append WAL e publicação;
- `connect(read_only=True)` após cada ponto de falha;
- índice ausente, stale ou parcialmente reconciliado;
- recovery concorrente com append pausado;
- processo morto e power loss tratados como cenários diferentes.

### Concorrência

- HNSW cold-build com duas e N threads;
- reader longevo contra índice marcado stale por outro processo;
- DDL concorrente;
- checkpoint/recovery/close concorrentes com commit;
- leasing de IDs com kill entre lease e uso.

### Recursos

- peak RSS por query e transação;
- produtos cartesianos;
- `DISTINCT`, `COLLECT`, sort e group com spill;
- traversal exponencial sob budgets;
- vector graph memory;
- bloat/vacuum com reader antigo.

### Compatibilidade

- fixtures on-disk n−1/n/n+1;
- tipos WAL required/skippable;
- export de uma versão e import na seguinte;
- wheel/sdist em 3.11/3.12/3.13;
- exemplos da documentação como testes consumidores.

### Performance

- Windows e POSIX;
- cold e warm cache;
- HNSW cold/warm e filtros 100%, 10%, 1% e 0,1%;
- N crescente, churn e pós-vacuum;
- point lookup com buckets saturados;
- throughput, p50/p90/p99, conflitos, syscalls e peak RSS.

## 12. Pontos fortes a preservar

As correções não devem enfraquecer os seguintes fundamentos:

- WAL como autoridade da durabilidade;
- checksums e torn-read protocol;
- replay idempotente;
- recuperação fail-closed com preservação de evidência;
- erros tipados;
- separação entre domínio, engine, ports e adapters;
- comportamento consistente entre Windows e POSIX;
- reader horizon protegendo retenção;
- verificação estrutural e de índices;
- testes de fault injection e mutation testing;
- documentação explícita de limitações e decisões.

Especialmente em performance, otimizações de formato ou redo lógico não devem ser adotadas antes que as opções de menor risco — batching, leasing, redução de metadados e access paths — sejam medidas.

## 13. Avaliação final

O projeto é arquiteturalmente mais maduro do que o número de versão sugere. O desenho de ports, WAL, verificação, fault injection e documentação fornece uma base muito boa para evolução.

Por outro lado, os defeitos de recovery, read-only e bootstrap atingem exatamente a promessa central de um banco de dados: um estado durablemente reconhecido deve continuar recuperável e toda leitura deve corresponder ao snapshot informado.

Classificação atual:

- **adequado:** pesquisa, desenvolvimento e workloads reconstruíveis;
- **ainda não adequado:** dados irrepetíveis ou operação sem supervisão;
- **perspectiva:** forte, desde que a Fase 0 preceda novas features e otimizações de formato.

A recomendação final é congelar temporariamente mudanças de formato físico e concentrar a próxima etapa na máquina de estados completa:

```text
bootstrap durável
    → commit WAL
    → apply completo
    → publicação
    → recovery serializado
    → read-only consistente
```

Depois que essa cadeia estiver provada por crash matrices públicas, o restante do roadmap — budgets, vacuum, backup, índices e performance — pode evoluir sobre uma fundação confiável.
