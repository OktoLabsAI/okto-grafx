# Plano de evolução do Okto Grafx

**Data da análise:** 2026-08-23

**Estado analisado:** `main@092a60f`

**Ambiente principal:** Windows, Python 3.13.1
**Escopo:** integridade, recuperação, concorrência, estabilidade, performance, API, configuração e novas capacidades.

## Estado de execução — 2026-08-24

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

## 9. Roadmap priorizado

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

## 10. Estratégia de testes recomendada

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

## 11. Pontos fortes a preservar

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

## 12. Avaliação final

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
