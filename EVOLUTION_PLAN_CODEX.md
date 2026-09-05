# Plano de evolução do Okto Grafx

**Data da análise:** 2026-08-23

**Estado analisado:** `main@092a60f`

**Ambiente principal:** Windows, Python 3.13.1
**Escopo:** integridade, recuperação, concorrência, estabilidade, performance, API, configuração e novas capacidades.

## Estado de execução — 2026-09-03

- **Milestone de hot paths publicado em `9603115` (2026-09-04).** Foram concluídos os recortes
  limitados de heap/catalog bootstrap, projeção e sincronização de índices, segunda leitura de
  controle sob pin válido, caches de query por `Database`, packing/decode de slots, retenção de
  catalog read view somente na composição WAL-only e a lane vetorial VEC-1..VEC-5. Não houve
  mudança de formato, WAL, OCC, multiwriter ou multireader. Os ganhos vetoriais medidos variam de
  `1,47x` a `3,19x` conforme o componente/workload; os ganhos de query continuam estimativas, não
  números end-to-end. A busca vetorial exata header-first valida integralmente toda linha admitida,
  mas deixa a corrupção de payload invisível/filtrado para `verify`/scan, registrando honestamente
  a mudança de momento de detecção. Evidências completas estão em
  `docs/PERFORMANCE_ROUND_0_0_2.md` e no handoff Nexus
  `hof_47afe28f5f004696a473718eeca9d980`.

- **Linha `0.0.2` iniciada sob o plano de performance congelado.** A branch canônica de trabalho é
  `feature/v0.0.2`; o bump de versão, o ambiente P0.0 e o diagnóstico/correção multiprocesso P0.1
  estão publicados, e o marco de código integrado mais recente é `72694bd`. A auditoria do Pulse Community pinado em `d50c034`
  (Core `f602c7c`) confirmou que o backfill
  não chama `rebuild_vector_index`, portanto a cerca process-local de um rebuild manual não é um
  blocker do fluxo real. D-26, D-01 e D-04 foram promovidos depois dos testes focados: D-26 mede
  janelas/fases sem callbacks sob lock e separa completion estrangeiro de OCC; D-01 elimina a
  construção de headers rejeitados; D-04 reutiliza a versão validada somente no acerto de PK,
  mantendo índices de alta cardinalidade no modo lazy e limitado. O protótipo D-02 cauda-primeiro
  foi descartado porque poderia ocultar duplicata corrupta; a substituição estrita por prefixo
  canônico entrou em `49a9b03` + `e26af74` e teve o comentário de quota alinhado em `fb984a7`.
  Ela mantém `HeapStore.lookup` e a ordem pública intactos, amortiza resoluções repetidas para
  `O(N+E)` enquanto o working set cabe na quota e cai no lookup canônico quando a quota satura.
  Memo, tabela, identidade, referência e páginas visitadas são cobrados antes da retenção por um
  teto global por handle; registro e accounting usam `RLock` injetado sem manter o guard durante
  I/O. O mesmo reader multiprocesso preservou seu snapshot antes da reconciliação e invalidou para
  fallback canônico depois dela. D-03 foi promovido em `1e2997e` + `13a5bda`: os três consumidores
  de pousos agora resolvem apenas as identidades efetivamente nomeadas pelas arestas e reutilizam
  hits/misses sob uma quota por handle de 32 MiB/131.072 entradas. A primeira revisão bloqueou o
  candidato por reter o fingerprint bruto e o `_Context` sem cobrança completa; o hardening cobra
  memo, slots, overlays, todo o histórico do fingerprint e resultados antes da publicação, passa o
  contexto somente por lookup e mantém I/O fora do guard. `DELETE` entra no fingerprint e paga a
  carga fixa por entrada, mas seu tuple vazio é marcador de ausência de payload e não é reencodado;
  a view continua admitida/reutilizável e o settlement devolve a quota. R1 foi mantido e o
  limiar/R2 não foi selecionado: dentro das quotas D-02 + D-03 o custo é `O(N+E)`, enquanto a
  saturação de ambas conserva explicitamente o fallback canônico de até `O(N)` por identidade.
  D-05 foi promovido em `bd12a5c` após 250 testes integrados e duas revisões adversariais: o scan
  de incidentes valida integralmente cada payload visível, inclusive overflow, propriedades e
  linhas não incidentes, mas materializa somente os endpoints. Seu microbench integrado mediu
  `1,365x` no componente sintético de decode; isso não é ganho end-to-end e o scan permanece
  `O(|R| + bytes dos payloads visíveis)`. D-12 também foi promovido em
  `df09c2e` + `6f6b410` + `16fbc0a`: depois da primeira contagem canônica, o planejador vetorial
  reutiliza cardinalidade exata cercada por page 0, invalida em rebase estrangeiro e mantém os
  deltas locais pelo identificador durável `(key, ref)`. Uma revisão adversarial encontrou e
  bloqueou a versão que identificava apenas por `ref`; o hardening final também garante que uma
  inconsistência de cache derivado nunca transforma um commit já durável em falha. P0.2 está concluído: as primitivas fail-closed entraram em `c276dec` e
  o driver Pulse autenticado em `be286fa` + `2d43d75`, com 46 testes focados e smokes sintéticos.
  O ferramental P0.3 foi integrado em `8ab6a8b`: perfil py-spy one-shot protegido por READY/GO,
  localidade dinâmica de endpoints, atividade vetorial e censo agregado read-only; `89cb893`
  removeu o teto de 100 mil hits por agregação exata por página e acrescentou o preflight de
  interpretador direto. A regressão combinada passou 98/98 antes desse hardening, e os dois
  arquivos focados passaram 28/28 depois dele. A execução real de P0.3 e as séries P0.4 continuam
  aguardando o término do backfill vivo e usarão somente cópias autenticadas; por decisão explícita
  do usuário, elas são evidência e não bloqueiam promoções que passem os gates de qualidade. A
  autoridade detalhada e a rastreabilidade por commit estão em `GRAFX_PERFORMANCE_ROUND_FINAL.md`
  e `docs/PERFORMANCE_ROUND_0_0_2.md`.

- **D-09 promovido sem alterar o protocolo de durabilidade.** `c3ef29f` gera a imagem WAL de uma
  página materializada localmente por `copy → stamp → encode`, eliminando o antigo
  `encode → decode(verify) → stamp → encode`; bytes externos pré-estagiados continuam no caminho
  integral `decode_page(verify=True)`. `69368c3` vincula a cópia reutilizável ao par
  `(txn_id, csn)` da tentativa e preserva todos os campos de `Page`, impedindo reuso cruzado em
  retarget. O diferencial contra o gerador antigo é byte-idêntico antes do flush, corrupção externa
  continua recusada antes do WAL, e um microbench sintético na máquina carregada observou razão
  p50 de `2,47x` por imagem; isso não é estimativa do ganho total do commit.

- **P1 fechado por composição explícita; P2 selecionado como “nenhuma”.** A regressão ampla
  terminou com `9.628 passed, 17 skipped, 1 failed`; a única falha foi uma docstring ausente no
  callback aninhado `count`, corrigida sem mudança de comportamento em `8f0af84`, seguida de 66/66
  testes do lote afetado. A suíte completa não foi repetida após essa correção documental. O gate
  multiprocesso passou 500/500 operações em 46,6 s, absorveu 44 conflitos retryable e terminou sem
  perda, duplicata, phantom ou torn read, com `verify("all")` limpo em live e reopen. O cold
  read-only real e descartável preservou todos os bytes duráveis e adicionou somente o lock vazio
  da participant section autenticada; `b445736` torna essa prova fail-closed, publica o inventário
  bruto e passou 43/43 focados mais revisão Nexus
  `hof_b0100393646349f095544abdd6137a58`. P2-ID, P2-DIRTY e P2-VAC não atingiram seus gatilhos
  congelados, portanto nenhuma alteração estrutural foi iniciada por hipótese. A limpeza futura dos
  `control/txn-*.lock` acumulados permanece dívida explícita e exige ADR mais prova multiprocesso
  contra split-lock antes de qualquer remoção automática.

- **Fila pós-P1 congelada e autorizada em 2026-09-03.** O fechamento da rodada P1/P2 acima não
  encerra por colisão de numeração os débitos históricos P1.5--P2.3 deste plano. A execução seguinte
  tem exatamente estes nove itens antes do próximo checkpoint: (1) acumuladores O(1), incluindo a
  correção da ordem não total de `NaN`; (2) memoização process-local da validação do provider CRC-32C
  e alinhamento contratual D-29(c/d); (3) top-N para `ORDER BY ... LIMIT`; (4) budgets de caminhos e
  expansões de traversal; (5) accounting/telemetria honesta de memória residente e métricas bounded
  do cache de descriptors; (6) HNSW como access path ponta a ponta, mantendo fallback canônico e sem
  cache persistido; (7) single-flight de cold miss e I/O fora do lock global sem estreitar as
  garantias multiwriter/multireader; (8) cursor/streaming, budget em bytes e spill; e (9)
  `executemany`/bulk ingest atômico sobre o protocolo WAL/OCC existente. Os itens 1--9 não podem
  alterar bytes duráveis, recovery ou as premissas de concorrência. A exceção descoberta durante o
  item 1 foi autorizada como correção: `DOUBLE` aceita `NaN`, mas `_sort_key` o entregava ao TimSort
  sem ordem total; a nova regra deve ser determinística, compartilhada por `ORDER BY` e `MIN/MAX` e
  coberta por diferencial.

  Estado final desta fila: o item 1 está integrado em `da48d6a`; `COUNT`, `SUM`, `AVG`, `MIN` e
  `MAX` mantêm estado O(1) por grupo, `COLLECT` continua materializado e `DISTINCT` retém apenas
  seu conjunto de unicidade. A exceção `NaN` foi fechada com ordem total compartilhada (depois dos
  demais números em ASC, antes em DESC), preservando estabilidade. O item 2 está integrado em
  `658760b` + `401e838`: as duas portas de instalação de checksum memoizam a prova fechada do
  corpus CRC-32C por processo sem confiar em igualdade nem em um `id` reutilizável; a identidade do
  provider é autenticada por `is`, as entradas são limitadas, validações concorrentes são
  serializadas e falhas nunca são memorizadas como sucesso. O item 3 está integrado em `b71846d` +
  `841127a`: `ORDER BY ... SKIP ... LIMIT` consome o child integral, mas retém no máximo
  `K = SKIP + LIMIT` em heap estável, com memória O(K) e tempo O(N log K); planos com bound físico
  sem a janela semântica exata são recusados. O item 4 está integrado em `8447ded`:
  `max_traversal_expansions` e `max_traversal_paths` são limites positivos opt-in, cumulativos por
  query e aplicados a traversal tipado, sem tipo e scan de relações; N+1 é recusado antes de
  retenção/retorno. O contrato explicita que a construção do fallback agrupado e o HNSW interno
  não são cobrados por esses dois contadores.

  O item 5 está integrado em `283cffa`: `used_bytes` preserva o budget nominal compatível e uma
  métrica/health view separada estima os objetos Python retidos sem alegar RSS nem governar
  eviction. Hits, misses e evictions do LRU de descriptors são cumulativos por device, sem labels
  de arquivo/path, e callbacks compostos deixam os guards de storage/buffer antes de alcançar o
  host. O item 7, integrado em `59af285` + `6e8d261` + `b49f297`, avança esse estimador para
  `python-v2` e executa um único cold load por `(file, page)`: callers da mesma chave compartilham
  o resultado; leituras/decode distintos e a publicação da vítima dirty ficam fora do guard global;
  tickets limitados e epochs de estrutura/drop recusam publicação stale. Testes determinísticos de
  alocação fecharam três janelas: a vítima dirty mantém autoridade enquanto tickets são criados,
  toda falha de publicação de frame acorda waiters, e um write físico bem-sucedido cuja
  pós-publicação falha preserva a evidência da página modificada e libera ambos os flights. A
  revisão focada de buffer, concorrência pública, telemetria e import boundary passou 396 testes.

  O item 6 está integrado em `2642750` + `1299ad8` + `980ac52`: planos HNSW top-k elegíveis
  consomem hits já ordenados em vez de revarrer a label inteira, capturam `ref`, `record_id` e
  score uma vez, revalidam identidade/visibilidade no snapshot exato e retornam ao caminho canônico
  em formas stale, ambíguas, nullable ou não suportadas. O planner nunca funde a janela de retorno
  quando há `DELETE`/`SET`; seis combinações adversariais com LIMIT/SKIP provaram que todos os
  matches são mutados e apenas o retorno é limitado. A auditoria independente final deu PASS.

  O cursor do item 8 está integrado em `c4cba57`: `QueryCursor` mantém uma transação read-only e o
  snapshot até EOF/close, destaca lotes limitados e preserva `max_result_rows` cumulativo. O budget
  em bytes e spill estão integrados em `192a497`: `query_memory_budget_bytes` opt-in dá a cada sort,
  result-DISTINCT ou aggregate bloqueante um contador lógico determinístico e um external merge
  workspace isolado fora do namespace do banco. Ordem estável/primeira ocorrência, aggregate
  DISTINCT, mixed values, identidade de NaN, equivalência de zero com sinal inclusive em vetores,
  cancelamento por cursor e preservação da exceção primária têm diferenciais. Níveis binários de
  runs e remoção imediata de sorters fechados por grupo evitam metadados O(N). Não há pickle nem
  mudança de formato; um `COLLECT` cujo próprio resultado excede o limite falha fechado, e `None`
  preserva os caminhos in-memory/top-N anteriores.

  O item 9 está integrado em `ae5c89f`: `Transaction.executemany` faz parse único,
  consome/canonicaliza um mapping por vez fora do page access, replana cada DML sem `RETURN` e
  reverte todo o lote ao mark externo em qualquer falha, sem mudar commit/WAL/OCC. A composição
  entre o AST `Query` e o novo tipo público homônimo foi corrigida em `f1ab724`. O microbenchmark
  indicativo de 200 inserts observou `2,77x` sem PK e `1,74x` com PK, sem caráter de SLO; validação
  de unicidade sobre muitos intents com PK e reconciliação repetida do budget em bytes permanecem
  débitos superlineares explícitos, não escondidos como ganhos de `executemany`.

  **Checkpoint dos itens 1--9 aprovado em 2026-09-03.** Após integração serial, um gate agrupado
  sobre todo o subsistema de query, bulk/public boundaries, buffer/single-flight, checksum,
  descriptor telemetry, metric catalog, configuração, packaging e import boundaries passou
  **2.916/2.916 testes**. A única falha no run composto anterior revelou que a telemetria de buffer
  congelava um sink habilitado no assembly; `60fc96b` agora respeita sua desativação posterior sem
  permitir opt-in tardio inseguro, e o caso falho mais a suíte focada passaram antes do gate final.
  Ruff lint, `compileall` e `git diff --check` estão verdes. O baseline histórico de Ruff format do
  repo inteiro não é verde e não foi reformatado mecanicamente; todos os arquivos novos do item 8
  e cada hot path editado diretamente nesta etapa passaram seu format check escopado.

  O checkpoint aprovado acima libera o segundo grupo já autorizado: (10) P2-ID com sizing,
  rehash e índices de identidade/secundários; (11) vacuum/compaction MVCC; (12) WAL
  delta/chunked/fisiológico; e (13) codec nativo além do CRC. Cada item 10--13 ainda exige seu ADR,
  migração/compatibilidade quando aplicável e gates próprios de recovery, mas não nova decisão de
  escopo do usuário após o checkpoint. Sharding/layout físico por tabela era o item 14 da lista e
  permanece fora desta autorização. Group commit continua fora da fila porque a medição histórica
  foi aproximadamente `1,002x`; plan cache continua somente hipótese a medir.

  **Item 10 / P2-ID iniciado com contrato finito em 2026-09-03.** O ADR aceito
  [`P2_IDENTITY_SECONDARY_INDEXES_V1.md`](docs/architecture/P2_IDENTITY_SECONDARY_INDEXES_V1.md)
  congela o catálogo v2 como capability/fence de frota, a migração explícita e idempotente (sem
  upgrade por mero `connect()`), gerações físicas shadow sem republish do mesmo path, sizing e
  rehash growth-only sob o `COMMIT_SECTION`, índices exatos customizados/compostos e o fallback
  versus fail-closed. B+tree, range/full-text, rehash online, sharding e itens 11--13 não entram
  nesse alvo. O primeiro milestone `fa0c298` implementa e testa o codec canônico de nove bytes
  `record_id_u64_v1` sobre todo o domínio utilizável `1..2**64-2` e generaliza a derivação interna
  para receber a identidade completa da versão, sem ainda tornar o novo access path elegível.
  A auditoria do downgrade fence acrescentou uma exigência necessária, sem ampliar a feature: a
  ativação do catálogo v2 co-publicará `control/commit.state` v2 no mesmo layout de 36 bytes. Isso
  impede inclusive um processo `0.0.1` já aberto de mutar recovery/checkpoint depois que o WAL da
  ativação tiver sido reciclado; publishers posteriores preservarão monotonicamente a versão 2.
  O milestone `4feec76` conclui o formato antes de torná-lo elegível no runtime: catálogo v1
  continua byte a byte idêntico e é o default; catálogo v2 persiste apenas access paths exatos
  compatíveis, com capability obrigatória, definições lógicas, gerações físicas, ordem canônica,
  unicidade global de nonce e cross-references fail-closed. Vetores/proximity continuam derivados
  pelo tipo especializado do schema. O mesmo milestone fixa o payload `commit.state` v2 de 36
  bytes e preserva monotonicamente uma versão já publicada. Gates agrupados: `132 passed` no codec
  e contratos adjacentes, mais `207 passed` no `CatalogStore`, publicação e visões públicas. A
  porta transacional de promoção/coativação foi concluída no milestone `12414d0`: somente um
  commit que efetivamente materializou `catalog.dat` decodifica a autoridade durável após
  `WAL barrier -> apply/flush`, e então publica `commit.state` v2 como último ato; commits comuns,
  checkpoints e gap completion preservam monotonicamente o fence sem confundir catálogo LIVE não
  salvo com estado durável. A primeira promoção substitui os dois slots com preflight do budget de
  gerações; crash entre as escritas é convergido pelo recovery. Todos os payloads outer-valid são
  inspecionados antes de replay/publicação: fallback v2, payload futuro, geração ambígua, binding
  estrangeiro e dano de header que esconda um fence mais forte recusam sem mutação. Dano local
  reconstruível só usa WAL completo e, após validar checksums independentes e bindings, restaura
  duas cópias v2. O gate agrupado passou **309/309 testes** (`281` em transação/recovery/API e `28`
  no formato de controle), com auditoria adversarial adicional em **77/77**, Ruff, format check
  escopado, `compileall` e `git diff --check` verdes. A próxima entrega finita do item 10 é projetar
  a autoridade ACTIVE do catálogo v2 sobre registry/planner/redo/staging/verifier/inventory; nenhum
  índice novo está elegível no runtime antes dessa projeção.

  Essa entrega foi concluída em `99622af`. Catálogo e registry agora mantêm projeções estruturais
  por identidade completa de tabela; resolução por nome no v2 consulta diretamente a definição
  lógica e sua geração ACTIVE, e count/staging por linha visitam somente `K_t` índices da tabela
  mais as observações especulativas da própria transação, sem materializar `O(total_indexes)`.
  Catálogo v1 preserva sua semântica anterior: qualquer registro process-local válido de uma
  tabela committed continua público, verificável e utilizável, inclusive quando o caller fornece
  uma foto explícita do catálogo. Cada acelerador automático passa a declinar isoladamente, de
  modo que um nome vetorial inexpressível não esconda PK/endpoints válidos. No v2, apenas a
  definição física exata da geração ACTIVE alcança planner, DML, lookup, redo, freshness,
  reconciliation, rebuild, verifier e inventário; BUILDING, STALE, nonce antigo e registros rogue
  permanecem apenas sob ownership físico/compensação. `verify()` recusa cobertura incompleta se
  uma geração exact ACTIVE estiver ausente ou divergente, em vez de produzir relatório falsamente
  limpo. O fallback de colaboradores legados mantém a validação exact do manager e nunca desce
  para lookup bruto do store. O gate agrupado dos 15 módulos afetados passou **242/242 testes**;
  testes discriminantes adicionais, Ruff, `compileall` e `git diff --check` também passaram, e as
  duas revisões adversariais terminaram sem blocker deste milestone.

  O lifecycle record-aware foi concluído em `0d353ae`: quota/count, INSERT, UPDATE, DELETE,
  lookup validado, rebuild e verifier derivam `record_id_u64_v1` da identidade durável completa;
  UPDATE conserva o mesmo ID entre referências físicas e DELETE usa ID/valores lidos do heap,
  sem reencodar o tuple vazio do intent para quota. O catálogo v1 recuperou apenas sua superfície
  diagnóstica histórica para reportar registros inválidos; catálogo v2 continua estritamente
  ACTIVE-only. O gate agrupado passou 435/435 testes, com Ruff lint, `compileall`, diff-check e
  revisão adversarial sem blocker. O WAL permanece deliberadamente lógico por nome, como
  congelado no ADR: o significado de uma definição não pode mudar, o shadow de rehash cobre
  integralmente o horizonte cercado e redo anterior é idempotente sobre a nova geração. A matriz
  de rehash/recovery deverá provar essas premissas, sem adicionar nonce ou novo formato WAL.

  O roteamento de identidade de endpoint foi concluído em `01c496d`. Cada statement fixa uma
  única decisão por identidade completa `(table_id, table_name)`: catálogo v1, ausência de geração
  ACTIVE ou store já stale escolhem o caminho canônico; uma geração ACTIVE íntegra escolhe o
  índice e não pode depois cair silenciosamente para scan. A consulta usa a projeção estrutural
  `O(K_t)`, a chave u64 do `RecordId` e versões já validadas contra heap/snapshot; miss é definitivo,
  duplicidade visível é corrupção e falha posterior propaga fail-closed. Os testes discriminam
  store ausente, definição física divergente e framework sem validação, além de identidade acima
  de `2**63`; rota + locator passaram 26/26, a regressão relacional focada permaneceu verde e a
  revisão adversarial não encontrou blocker. Cold reopen com `IndexManager` real permanece
  corretamente vinculado ao próximo milestone de ativação física, que cria o artefato necessário.

  A ativação física e o crescimento automático do schema v2 foram concluídos em `ba8ca9a`.
  `ensure_identity_indexes()` é explícito/idempotente, migra v1 em um único commit e constrói como
  shadows nonced todas as gerações automáticas exatas mais os índices u64 das tabelas de endpoint.
  NODE/REL criados depois da migração publicam suas gerações ACTIVE no mesmo commit do schema; um
  REL também cria ou substitui identidades ausentes/stale dos endpoints antes de ficar visível.
  Builds sobre heap committed mantêm todas as partições na OCC até o commit, enquanto gerações
  vazias de tabela nova cruzam durability barrier antes de o catálogo poder referenciá-las.
  `max_index_build_entries`, keyword-only e opt-in, soma entradas finais exatas do lote e recusa em
  N+1 antes de `catalog.stage` ou do primeiro `g_*`; rollback limpa claims/cache e retry usa nonce
  novo. Colisões case-fold continuam scan-only e índices RID não entram no planner genérico. O gate
  agrupado activation/DDL/quota/planner/config passou 367 testes; o slice DDL pós-formatação passou
  5/5, Ruff lint, compile e diff-check ficaram verdes, e duas revisões adversariais encerraram sem
  blocker residual.

  A criação de índices secundários exatos customizados foi concluída em `2fa81b1`. A gramática
  `CREATE INDEX` e a porta `Database.create_index()` compartilham análise, planner, sizing e o
  mesmo protocolo transacional: migração/reparo automático v2 e shadow custom são admitidos como
  um lote, construídos sob writer lease + `COMMIT_SECTION`, barrierados antes do catálogo e
  publicados por um único commit WAL/OCC. Índices compostos preservam a ordem declarada;
  `bucket_count` e `expected_cardinality` são exclusivos; o default continua 64 e o sizing por
  cardinalidade usa `next_pow2(ceil(N/64))` dentro de `1..4096` buckets. A porta Python exige uma
  `Sequence` ordenada, recusa set/dict/generator antes de abrir transação e retorna `IndexView`
  ACTIVE destacado com nonce, metadados e horizons recém-certificados. O executor preserva a
  igualdade da linguagem: `NULL` não casa, e representações que a query considera equivalentes
  mas o codec distingue (INT64/DOUBLE, zero com sinal e valores aninhados) escolhem scan canônico
  antes de consumir o índice. A fronteira pública canonicaliza definições lógicas sem callbacks
  hostis e mantém índices vector/proximity derivados do schema no catálogo v2.

  Gates do marco: 394/394 no slice grammar/planner/query/txn/API e 281/281 no slice público,
  concorrência de fronteira, activation/v2/reopen; `verify("all")` live e cold passou, além de
  Ruff, `compileall` e `diff --check`. A revisão adversarial encontrou quatro blockers reais
  (callback lógico hostil, perda de vector no inventário v2, receipt sem horizons e coleções não
  ordenadas) e confirmou todos fechados, sem blocker residual.

  **Item 10 / P2-ID concluído em `72694bd`.** `Database.rehash_index()` e a facade de manutenção
  exigem exatamente um hint e crescimento estrito, constroem uma geração shadow imutável sob o
  writer lease + `COMMIT_SECTION`, preservam as duas OCC e só publicam o novo ACTIVE após a
  barreira física. No catálogo v1, a coativação v2 redimensiona a geração planejada e constrói o
  alvo uma única vez; custom v1 sem autoridade durável é recusado. No v2, somente o predecessor
  imediato fica descrito como STALE; arquivos históricos permanecem órfãos não reutilizáveis até
  existir reclamador seguro. O WAL continua lógico por nome e recovery converge para autoridade
  antiga completa ou nova completa. Handles long-lived adotam mudança de catálogo antes do próximo
  statement/`verify()`, sem rescan de inventário para DML comum, checkpoint ou delta CE-3 grande
  quando os bytes persistidos do catálogo não mudaram. Geração ACTIVE ausente/malformada mantém um
  latch e faz toda nova fronteira falhar fechada até reparo. O gate focal integrado passou 163/163,
  com recovery/fault injection, multiprocesso `strict`/`generation`, read-only, cold reopen e
  `verify("all")`; Ruff, `compileall`, diff-check e duas revisões adversariais ficaram verdes.
  O gate adjacente também revelou um drift test-only anterior: a métrica
  `oktografx_buffer_retained_estimate_bytes` era emitida e constava do contrato desde `283cffa`,
  mas não do roster executável do storage-core; `0374dc9` alinhou o teste e o lote focal passou.
  Assim, o próximo item autorizado é exatamente o **11 — vacuum/compaction MVCC**; itens 12--13 e
  sharding não entram nele.

  **Item 11 / P2-VAC iniciou pelo marco read-only de medição.**
  `db.maintenance.bloat(table=None)` faz um censo header-only numa observação sem pruning do
  horizonte reciclável limitado pelo checkpoint; reader records parados por TTL continuam pins.
  A porta retorna DTOs imutáveis por tabela e agregados. A nomenclatura ficou fechada:
  `horizon_eligible + horizon_retained == ended`; versões vivas/provisórias são
  `stored - ended`, e bytes de overflow/diretório não são atribuídos ao potencial reclaim. O
  relatório mantém `vacuum_safety_established=False`, não escreve WAL/páginas, recusa estado dirty
  em vez de fazer flush e não constitui permissão para exclusão. O gate focal passou 196 testes,
  somado aos testes de buffer/read-view/coordenação, Ruff, `compileall` e diff-check.

  A análise adversarial do protocolo mutante encontrou uma fronteira real, não um novo alvo: o
  TTL do registry não prova quiescência de um processo antigo já dentro de um statement, e
  `RecordRef(page, slot)` sem incarnation impede reuso durável seguro por ABA. O usuário autorizou
  explicitamente o menor contrato v1: manual/foreground, process-quiescent, com floor global
  monotônico no header do heap e capability obrigatória de catálogo v2; sem vacuum online,
  truncagem, overflow reclamation nem reuso de `RecordRef` nesta etapa.

  **Item 11 / P2-VAC concluído em `75e799f`.** `maintenance.vacuum(...)` exige a asserção exata
  `confirm_quiescent=True`, catálogo v2 já ativo e nenhum outro processo/handle Grafx ou transação
  local durante toda a chamada. A primeira execução publica `heap_reclaim_v1`; a remoção física,
  floor durável, relink de sucessoras retidas e reconcile de todos os índices ACTIVE selecionados
  são co-publicados pelo WAL/commit comum. `max_versions` limita deterministicamente apenas slots
  inline; overflow é contado e retido. Snapshot abaixo do floor falha com
  `GrafxSnapshotReclaimed`, retryable, e build antigo recusa o capability bit. O segundo passe sem
  trabalho é zero-write. A matriz de crash atravessou barreira WAL, heap, índice e commit.state;
  query live/cold, vector, verifier e retry de rehash interrompido ficaram limpos. Gates agrupados:
  490/490 diretamente afetados e 736/736 transacionais/fronteira, além de Ruff, format,
  `compileall` e diff-check. Quatro falhas do comando transacional sem filtro foram reproduzidas no
  baseline `6d3e62c` e excluídas por node id, sem serem reclassificadas como sucesso. A revisão
  Nexus `hof_df837d11c96a430786856895c7f7c744` foi corrigida após contraditório e verificada PASS.
  O ganho é remover payload inline morto e evitar seu decode futuro; páginas históricas, diretórios
  de slot e tamanho do arquivo permanecem, portanto ainda existem percursos `O(history pages)`.

  **Item 12 / WAL — recorte definitivo concluído e publicado.** O commit `24f2f63` implementa o
  contrato convergido na revisão adversarial: compressão zlib nível 1 da imagem completa de
  `WRITE_PAGE`, sob `format_version=2` e capability persistente `wal_record_v2`. A ativação é
  explícita, one-way e
  ocorre em transação anterior inteiramente v1; somente commits posteriores podem emitir v2, e
  apenas quando a codificação completa fica estritamente menor. O prefixo `(file, page_index)`
  permanece legível sem inflate, a descompressão é limitada a `MAX_PAGE_SIZE`, o CRC do record
  cobre bytes comprimidos e o page codec valida a imagem expandida antes de mutação.
  `max_wal_batch_bytes` cobra os records finais comprimidos. Para eliminar feedback entre tamanho,
  roll, `SEGMENT_HEADER` e CSN materializado, todo lote raw que já exigiria roll permanece v1;
  compressão só encurta lotes cujo terminal já foi provado no segmento atual. Tipos/flags v2
  obrigatórios desconhecidos recusam read/append/recycle/recovery sem alterar o WAL, enquanto
  capability de catálogo mantém o downgrade fechado mesmo depois de reciclar os records v2.

  O escopo não se move: block-delta fica futuro e exige pre-image/base LSN, FPW pós-checkpoint e
  redo de quatro vias; chunk físico é **NO-GO** neste protocolo porque kill entre `append_log`s
  deixa effects completos sem COMMIT, estado que recovery corretamente trata como ambíguo;
  WAL fisiológico também é **NO-GO** por duplicar semântica de mutação no redo. O contrato e seus
  prós/contras estão em
  [`WAL_PAGE_COMPRESSION_V1.md`](docs/architecture/WAL_PAGE_COMPRESSION_V1.md). O parecer Nexus
  `hof_44e3743219b2476a95ecc02d00ac0eb6` foi corrigido duas vezes no contraditório e verificado
  PASS na revisão 3. A revisão da implementação
  `hof_b192c61ffcea4297852c7b2b8f00064b` também foi verificada PASS depois de fechar a gramática
  explícita de records desconhecidos `SKIPPABLE`; não há gate de performance nem etapa de
  pre-image adicionada a este marco. O gate agrupado terminou em 1.496/1.496 casos verdes após
  excluir nominalmente quatro falhas históricas reproduzidas fora do caminho alterado; os gates
  focais, Ruff, `compileall` e `git diff --check` também ficaram verdes. Uma
  amostra indicativa de uma transação de 50 linhas/3 páginas de 8 KiB mediu 31.510 bytes v1 contra
  7.966 bytes finais (`-74,72%`, razão `3,96x`), sem elevar esse número a SLO ou critério de aceite.

  **Item 13 / codec NumPy v1 concluído em `d644dc3`.** `DatabaseConfig(codec="numpy")`, opt-in e
  por instância, liga um adapter híbrido que preserva byte a byte o page format v1. Encode usa
  packing vetorizado a partir de 16 slots; decode usa validação vetorizada a partir de 96; abaixo
  desses limiares e em toda entrada inválida, `PageCodecV1` continua o oráculo único. Não há
  `auto`, capability, migração, cache global ou mudança de WAL/locks/multiwriter/multireader.
  `database.codec` distingue o implementation por handle do
  `process_checksum_implementation`, global por desenho preexistente. O diferencial cobre bytes,
  freed/compacted pages, corrupções explícitas, 1.000 mutações semeadas em cada page size
  4/8/32 KiB, cold reopen e dois handles no mesmo processo. A revisão adversarial inicial retirou
  seu NO-GO após separar CRC puro do custo do diretório; a revisão final
  `hof_19b2cca000214564832d7ff15cf77618` fechou os seis blockers e foi verificada PASS. O
  microbenchmark final de 200 slots/8 KiB com CRC nativo mediu encode `164,85→53,87 us` (`3,06x`)
  e decode `150,48→82,12 us` (`1,83x`), somente como evidência de componente. O commit
  `b720f7e` fechou as duas dívidas arquiteturais reveladas pelo gate ampliado: tabelas constantes
  de catálogo agora são imutáveis e `zlib` está explicitamente admitido como algoritmo
  determinístico da gramática WAL v2; o regex de nome físico foi removido sem perder `g_`/`G_`.

- **Run real do Pulse 0.3.3 na pasta padrão — reconstrução Grafx em andamento, SQLite preservado.**
  Antes da troca foi criado backup consistente do SQLite (`quick_check=ok`, zero violações de FK)
  e os artefatos Ladybug foram movidos, sem exclusão, para quarentena operacional. Os bindings de
  Board e Global foram materializados com `backend=grafx` e `descriptor_revalidation=generation`.
  O run real revelou e fechou dois defeitos no Pulse Community: o `kg backfill --apply` standalone
  não registrava o provider de coordenação antes de adquirir o writer lease, e o fechamento terminal
  da transação tentava resetar em worker thread um token `ContextVar` criado no contexto async. As
  correções estão em `okto-pulse@d50c034`; 54 testes focados passaram. O limite textual abaixo está
  em `okto-grafx@58e2e7d`; 367 testes focados e Ruff passaram. A fila interrompida por reboot é
  retomada pelo protocolo público de expiração/recuperação de claim, preservando at-least-once e a
  serialização por board. Auditoria final e liberação para uso só ocorrem após a fila chegar a zero.

- **Hotfix de integração Pulse — limite textual corrigido localmente; I64 permanece gap explícito.**
  A criação do refinement `876eb7b0-189c-4499-8bb8-9ca8c6568d82` expôs que o limite léxico de
  16.384 caracteres estava sendo reutilizado indevidamente para dados parametrizados: no SQLite
  preservado, `screen_mockups` tem 27.825 e `description` 18.718 caracteres. A correção separa as
  superfícies: literais na query continuam em 16.384, enquanto `max_query_value_characters` passa a
  governar parâmetros/resultados com default 65.536 e hard cap configurável de 1.048.576, sem mudar
  formato ou tocar WAL/durabilidade. A outra mensagem observada, `kg.scoring.fetch_failed`, vem da
  consulta `_fetch_node_inputs` com três `OPTIONAL MATCH` encadeados e dois `WITH` agregadores; ela é
  exatamente a família I64 já congelada como `generic_gap/runtime_current`, não uma regressão do
  subconjunto prometido. Portanto I64 continua dívida funcional declarada e não foi mascarada por
  widening parcial do parser neste hotfix.

- **Decisão normativa de 2026-09-01 — gates de performance aposentados.** Throughput, latências,
  RSS, CPU, contadores de syscall, o antigo piso `same-10 >= 7,5/s`, D5 e paridade temporal com
  Ladybug continuam registrados como observações, mas não bloqueiam M-PULSE-7, compatibilidade
  Pulse, auditoria integrada ou `0.0.1`. Toda passagem histórica deste documento que ainda diga
  “bloqueado” por uma dessas métricas está supersedida por esta decisão. Permanecem vinculantes
  somente os gates de qualidade: zero divergência sem explicação, corrupção, falha de WAL/
  durabilidade, recovery/reopen, segurança de concorrência, timeout da operação semântica,
  `verify("all")`, identidade/proveniência de fonte ou regressão funcional. O `same-10` autenticado
  pós-ST-2 foi aceito como prova de qualidade: A `60/60`, B `194/194`, zero conflito, retry, recusa
  ou reopen, verify live+cold limpo e geração/fonte/identidade estáveis; `4,421593/s` e CPU são
  apenas informativos. Artefato
  `D:\GrafxBenchEvidence\st2-same10-20260901-final-a01\ce3-same10.json`, SHA-256
  `419d60d64747f1676210b2772b8d0efbb732d72283039aeebb4c89a59f75834c`.
- **ST-2 autorizado, implementado e certificado; PF5 e `same-10` fecharam em qualidade, e o próximo
  passo finito é o M-PULSE-7 sob gates exclusivamente funcionais.**
  A autorização posterior do usuário congelou dois modos públicos de
  `descriptor_revalidation`: `"strict"` permanece o padrão e revalida cada hit; `"generation"`
  é opt-in para o diretório exclusivamente gerido por Grafx/Pulse e amortiza a prova somente para
  `heap.dat`, `catalog.dat`, índices canônicos e segmentos WAL canônicos. Controle, metadata,
  temporários, órfãos, nomes malformados e desconhecidos continuam estritos. A geração é local ao
  adapter e não substitui OCC, WAL, lease, page-0 CAS ou BR-10; refreshes CE-3 parciais invalidam
  apenas os nomes comprovadamente alterados, e `read_fresh_page`/CAS page 0 revalidam o alvo antes
  da leitura física. O primeiro F4 `generation` encontrou um descritor de índice movido para
  `index_orphan` ainda certificado; a invalidação dirigida fechou-o antes do WAL. O F4 final passou
  `strict` e `generation`: DDL estrangeiro com recusa pre-WAL e WAL/LSN imutáveis, mais
  checkpoint/recycle com reader pin, horizonte preservado, kill do writer sem flush e
  `verify("all")` limpo após cold reopen. A suíte integral autenticada com os baselines Pulse
  pinados percorreu **11.357 nodeids** até 100%/exit 0; Ruff, compileall e diff-check passaram, e
  uma revisão independente concluiu `GO` sem defeito alto/médio diferencial. O contrato, prós,
  contras e critérios explícitos de quando usar/não usar cada modo estão em
  `docs/architecture/ST2_DESCRIPTOR_REVALIDATION.md` e CONTRACT A96. Qualquer medição futura será
  rotulada por modo: o Pulse usa `generation`, configuração opt-in efetivamente destinada a esse
  deployment; nenhum número será atribuído ao default `strict`. Para que essa
  proveniência não seja apenas declaratória, o handle expõe o modo efetivo process-local pela
  propriedade read-only `Database.descriptor_revalidation`, fora da identidade persistida; o
  provider Pulse deve comparar solicitado e observado antes de admitir o handle e o runner deve
  recusar artefatos sem essa prova. O fechamento estrutural foi medido no conjunto imutável
  `c994255b0bf695040c972ce339cc5d580ec253d2146674664e7722cf6b5a7f81`, em modo `continuous`,
  `per_family=5`, com Grafx `f0b55b7b6facc916118f342c774cb06e56bf17e3`, Community
  `050ced9b79533d50efed453d53ed450984f75cf3` e Core
  `ccc1f345ece1db89a274cfdd634bd4da27028f63`. Nas 12 famílias, a soma das medianas manteve
  `_still_names=424` (`<500`) e `os.lstat=4.137` (`<8.000`), enquanto `os.stat` caiu
  `2.393 -> 1.727` e passou o limite `<2.000`. `_read_page=323`, `read_fresh_page=146`,
  `write_page=96`, 123 aquisições autenticadas de binding e 148 statements ficaram idênticos ao
  controle. O fast path é somente Board/Grafx com o database exato já pinado: cada fence continua
  lendo/autenticando o binding, recusando cutover CAS visível, comparando o snapshot completo,
  exigindo o diretório físico canônico e readmitindo path/page size; Global e revalidações genéricas
  mantêm a caminhada integral. Um alias físico real (symlink/junction) é recusado por regressão.
  Artefato `D:\GrafxBenchEvidence\st2-pinned-route-20260901-final-a01\st2-pinned-route-generation-profile-pf5.json`,
  969.630 bytes, SHA-256
  `384a7722ff6772a2e89ca95225ab759ec5c7cab5af9939f405ef7b5ec2802aae`. A recomputação
  independente do handoff Nexus `hof_566e4a5333b54b55946b5dc7ad416b36` confirmou hash, pins,
  gates e os cinco contadores invariantes, e foi verificada/PASS pelo Codex. Como
  `machine_idle_asserted=false`, esta evidência aprova somente contagens estruturais e não publica
  throughput temporal.
- **M-PULSE-7 — certificação funcional concluída; falso timeout e lifecycle do harness fechados.**
  O run `D:\GrafxBenchEvidence\mpulse7-quality-20260901-a01` completou os dois traces de 10.000
  mutações e os 11 cenários de crash/recovery; auditoria independente confirmou os 11 PASS, os
  fingerprints contra o oracle, autoridade dos 22 workers, ausência física nos dois casos de
  privacidade e nenhum processo/lock ativo residual. O run falhou fail-closed antes do primeiro
  caso Board e, corretamente, não gravou receipt. A causa não era consulta nem backend: no primeiro
  worker Ladybug, importação e duas validações de autoridade ocorriam dentro do mesmo `join(30)`;
  o preâmbulo medido levou `54,074 s`, enquanto `find_by_topic` levou `84,6 ms` no Ladybug e
  `59,4 ms` no Grafx, com resultado e fingerprints bilaterais idênticos. Os tempos são diagnóstico,
  não SLO. Community `perf/st2-pulse-generation@1d2a25d` introduziu um handshake fail-closed: spawn,
  autoridade integral, open, identidade e fingerprint precedem a prontidão; somente a operação
  semântica usa os `30 s`; identidade/fingerprint finais, close e receipt permanecem obrigatórios
  sob contenção operacional separada. Timestamps monotônicos publicados pelo filho fecham a corrida
  de fronteira, e terminate/kill precisa comprovar a morte. Setup/finalização acima do timeout da
  operação passam; operações lentas em Board/Pulse são encerradas. A regressão pinada desse primeiro
  patch terminou **73/73**, com py_compile, Ruff (somente E402 histórico ignorado) e diff-check
  limpos. O run `D:\GrafxBenchEvidence\mpulse7-quality-20260901-a02` concluiu o trace Ladybug e
  entrou no trace Grafx sem divergência, mas foi interrompido deliberadamente, sem receipt e sem
  processo órfão, quando a revisão adversarial Nexus `hof_cb1563e7aeed47c9a13eff733334ef66`
  encontrou dois defeitos estruturais do supervisor: um filho que saísse `0` sem publicar `ready`
  podia saltar o watchdog, e uma exceção rara do supervisor pós-`start()` podia deixar o filho vivo.
  O milestone Community `perf/st2-pulse-generation@4086ad732249709013e108728af5afea09610954`
  torna `ready` obrigatório em todos os caminhos, recolhe/termina o filho em toda exceção pós-start e
  acrescenta regressões para recibo `ok` sem prontidão e falha do supervisor com operação bloqueada.
  Gate focado **7/7**, suíte M-PULSE-7 integral **75/75** em `454,75 s`, py_compile, Ruff e
  diff-check passaram; commit e remoto coincidem. O run
  `D:\\GrafxBenchEvidence\\mpulse7-quality-20260901-a03` voltou a provar os dois traces e 11/11
  crash/recovery, passou pelo antigo ponto de falso timeout e concluiu todos os 19 casos Board
  Ladybug mais os oito primeiros Grafx. Ele então falhou fail-closed, sem receipt e sem órfão, no
  caso congelado `vector-api-contract-inclusive`: a fixture passava `min_similarity=-1.0`, embora o
  contrato público Pulse normalize scores e thresholds no intervalo `[0,1]`. A revisão independente
  confirmou que `0.0` preserva exatamente a intenção inclusiva — inclusive cossenos brutos negativos,
  que são clampados para zero — e que o Grafx já traduz esse valor para o threshold bruto `-1.0`.
  Não foi criada exceção mágica nem ampliado o contrato do adapter. O milestone Community
  `perf/st2-pulse-generation@18478ede9528556ef5e59fa7f91c72f4ccba2a5e` publicou a revisão de
  conteúdo `m_pulse_7_acceptance_gate_v2.json`: o formato do manifesto continua
  `okto-pulse-community-m-pulse-7-acceptance-gate/1`, enquanto o suplemento passa a `/2`. A revisão
  fixa `min_similarity=0.0`, pina o candidato Grafx
  `8cee82b9b92529f2ba767519c01c161f20876dce` e valida fail-closed que todo argumento vetorial é
  numérico, não booleano, finito e pertence a `[0,1]`; o teste negativo recompõe o digest antes de
  provar a recusa de `-1.0`. Autoridades recompostas: manifesto físico
  `e5c9ef4432b2c550e8ba22fc57ee607a0eabdb8cf6e49a87ae080d50c726ee66`, canônico
  `ea7b070b8e98b3a53b12bfe89cc4ff4355c5c433da03b8bcf681733398bd199e` e queries
  `f0f50de41464a112147d1d13552fcb4b964022799959f5b64c64ca68df3ac2cd`. Passaram **38/38**
  focados, **76/76** na suíte M-PULSE-7 integral e **49/49** nas matrizes vetoriais dedicadas com
  resultados materiais, ranking, score e desempate. Os dois casos vetoriais do suplemento ainda
  produzem `[]` bilateralmente: é dívida preexistente, não regressão nem blocker desta revisão. Uma
  futura revisão explícita deverá semear embeddings no bootstrap real e exigir IDs esperados não
  vazios nos dois backends, sem alterar silenciosamente fingerprints congelados agora. O run
  `D:\GrafxBenchEvidence\mpulse7-quality-20260902-a04`, nos pins Community `18478ede`, Core
  `ccc1f345` e Grafx `8cee82b9`, voltou a concluir os dois traces de 10.000 mutações e os 11/11
  cenários de crash/recovery, mas recusou corretamente o receipt ao encontrar duas divergências no
  suplemento Board. Em `topic-api-contract-inclusive`, as mesmas 68 linhas divergiam apenas na
  forma observável de `created_at`: Ladybug entrega `datetime` UTC-naive, enquanto o adapter Grafx
  entregava texto com sufixo `Z`; como o Core serializa `datetime` por `isoformat()` e preserva
  strings, isso era incompatibilidade real de produto, não diferença a esconder no gate. Em
  `contradictions-board`, os dois backends entregavam exatamente as mesmas duas linhas em ordem
  inversa; essa API não promete ranking, e o limite 100 é maior que o censo final de 70 arestas,
  portanto não há escolha de subconjunto por truncamento. A correção híbrida mínima foi publicada
  em Community `perf/st2-pulse-generation@23f9927`: timestamps Grafx passam a reproduzir o
  `datetime` UTC-naive do Ladybug, com precisão inteira de microssegundos, e somente
  `contradictions-board` passa a comparação de multiconjunto preservando duplicatas.
  `contradictions-node` continua ordenado; Core `ccc1f345` e as consultas de produção permanecem
  inalterados, sem introduzir `ORDER BY` ou uma nova semântica de ranking. A fixture
  `m_pulse_7_acceptance_gate_v3.json` ratcheta o suplemento para `/3`; hashes físico, canônico e das
  queries são, respectivamente,
  `f4caf1236104e9bb410462b6e5ef3541bec04f4fd591b8e6870b4bb0939a70b1`,
  `3160a8cdde56feab41b425182e1c41f1b323aeb5b1e91a00cadffb2ff94d6a83` e
  `02c3b06dd71a0f66e04afbf64d33c203ef53d985d8912edb738e6519a6a29a7d`. Replay exato dos 19 casos
  ficou 19/19 bilateral, a suíte focada terminou **97/97**, o gate real dos adapters ficou **12/12**,
  Ruff e diff-check passaram, e as revisões independentes local e Nexus
  `hof_a411cbd75a984035b1d09d14f6cbb546` aprovaram sem achados. O run definitivo
  `D:\GrafxBenchEvidence\mpulse7-quality-20260902-a05`, nos pins Community
  `23f9927ba5ac84424821b42aeac37528996b12e7`, Core
  `ccc1f345ece1db89a274cfdd634bd4da27028f63` e Grafx
  `8cee82b9b92529f2ba767519c01c161f20876dce`, terminou com exit `0` e
  `M-PULSE-7 acceptance gate PASSED`. O receipt canônico único
  `D:\GrafxBenchEvidence\mpulse7-quality-20260902-a05\receipt.json` tem SHA-256 autenticado
  `f08c8be63ea2cd7abf6c6ffbb0deef5203169ed22c5b42a2c34ac290d90b77f1` e autoridade de processo
  `34605c59f8d99894f40ffe7bb988eccedef8fea6d7cf3951d28f2126eaf859b4`. A auditoria independente
  recompôs o hash e passou 27 verificações: dois traces de 10.000 operações com fingerprints
  idênticos, três reopen/recovery por backend, 11/11 pontos de crash, 19/19 comparações Board, 97
  casos Pulse, suplementos de 21 famílias raw e quatro cenários receipt-bound por backend e zero
  falha de crash, timeout, `verify` ou divergência sem explicação. As únicas três diferenças Pulse
  são os casos já classificados `generic_gap` `I64`, `EXPLAIN_CONSTRAINT_ORIGINS` e
  `GET_RELATED_CONTEXT`; todos os demais resultados comparáveis são iguais. O modo Grafx observado
  é `descriptor_revalidation="generation"`; fontes carregadas, worktrees e pins foram autenticados
  no início e no fim. Após o run, os três worktrees continuavam limpos e não havia processo órfão.
  M-PULSE-7 está encerrado por qualidade; a regressão ampla Community, a auditoria dos artefatos,
  a publicação conjunta e a reinstalação pública também foram fechadas pelas evidências compostas
  descritas abaixo. A linha `0.0.1` está concluída. Não há matriz nem piso de performance
  intermediário; os roadmaps `0.0.2` estão liberados pelo gate, mas ainda não foram iniciados.
- **Pré-release `0.0.1` — regressão Community fechada; artefatos auditados no marco seguinte.** A
  primeira execução Community pós-M-PULSE-7 terminou em `5 failed, 5183
  passed, 3 skipped, 13 deselected`: dois números documentais estavam cinco linhas atrás do oráculo
  executável (`1236 -> 1241`), dois subprocessos herdavam checkouts não pinados ou contenção curta
  demais, e o teste de wheel offline não encontrava as wheels nativas de `ladybug==0.16.0` e
  `google-crc32c` no cache local. Não houve falha funcional de Grafx/Pulse nem contradição do receipt
  `a05`. Community `perf/st2-pulse-generation@ae94845` torna os subprocessos de autoridade e da suíte
  semântica herméticos aos checkouts configurados, preserva todas as asserções e usa `600 s` somente
  como contenção do processo supervisor — não como SLO nem como alteração do watchdog semântico de
  `30 s`. Core `milestone/grafx-mpulse6-logical-transfer-manifest@341bccd` e os documentos Community
  registram o inventário executável `1241`. Após pré-carga operacional das duas wheels para Python
  3.13, os cinco casos originalmente falhos passaram; também passou o segundo cenário do helper de
  autoridade, totalizando documentação/F16 `2/2`, M7 subprocessos `2/2`, supervisor semântico `1/1`
  e launcher instalado `1/1`. O diagnóstico independente Nexus
  `hof_1b1010756f4e49d59a5985b98992d4dc` foi verificado/PASS. A segunda execução ampla terminou em
  `1 failed, 5187 passed, 3 skipped, 13 deselected`: o único vermelho era a asserção que comparava o
  HEAD Core certificado `ccc1f345` ao sucessor `341bccd`, cujo único delta é a linha documental do
  README exigida pelo F16. Não houve falha de produto, e a suíte semântica antes limitada a 240 s
  passou sob a contenção de 600 s.
  Community `perf/st2-pulse-generation@159ef75328dc68493379d95b9f72826b660b8d84`
  fecha essa única ocorrência sem tornar o runner permissivo: o teste prova que o runner de produção
  continua rejeitando `341bccd` contra o pin `ccc1f345` e admite o carry-forward da release somente
  se `ccc1f345` for ancestral imediato, o diff for exatamente `M README.md`, ambas as árvores `src`
  forem `a83336036c1276f6582261579cf60d45883cb084` e o catálogo produtivo completo continuar idêntico
  ao receipt `a05` — hash geral `689dee55bef6b28cae87400aa168a33fc0b88a07c6c050e608999ed04544f6ac`,
  com `309/757/129` fontes Community/Core/Grafx. O happy path exato e as recusas de pins Core/Grafx
  forjados permanecem testados; runner, manifesto v3 e código produtivo não mudaram. O fechamento
  focado persistente passou **10/10** em `123,74 s`; JUnit
  `D:\GrafxBenchEvidence\mpulse7-release-carry-forward\focused-regression.xml`, SHA-256
  `9fdfa522c70202d93e03c04af51f1f0810fc331b22402206597d293be4bdd1f6`. A revisão Nexus
  `hof_e31712f8b255489aa1834b5454be10b9` foi verificada/PASS. A conclusão é por composição explícita
  da regressão ampla `5187 + 1` com a repetição direcionada do único caso alterado, não por alegação
  de uma terceira execução integral. A construção e a auditoria instalável foram concluídas no
  marco seguinte, sem upload automático ao PyPI.
- **Pré-release `0.0.1` — candidata instalável auditada; pronta para publicação conjunta.** O SHA
  candidato Grafx `744d450f5c199d06fa34e2f831dd443fbd15375d` altera, desde o pin certificado
  `8cee82b9b92529f2ba767519c01c161f20876dce`, somente o allowlist estático de teste da propriedade
  pública imutável `descriptor_revalidation` e este documento; a árvore produtiva `src` permanece
  bit-idêntica, `a00e55461daf93e3ab639790899079ad30a699b8`. A suíte integral no pin produtivo terminou em
  `1 failed, 11381 passed, 17 skipped`; a única falha era exatamente esse allowlist desatualizado.
  Após a correção test-only, o arquivo de fronteira e as suítes de bootstrap/configuração e
  descriptor passaram **485/485**. O fechamento é, portanto, a composição explícita `11381 + 1`,
  sem alegar uma segunda execução integral; JUnits SHA-256
  `db4ef018a6f190e98e03470b2228f2d3628bc8e2216f4b0a91dc4dd07719213d` e
  `07d51835d20fd339cce66b14961876dab19e7aa30bde2f764f95a794c9fed1a8`.
  O build isolado produziu wheel `okto_grafx-0.0.1-py3-none-any.whl`, SHA-256
  `3acc1bbc17bb4629caea3cbca60ff503c5cea1f8891cd829227a08eb5bc75070`, e sdist
  `okto_grafx-0.0.1.tar.gz`, SHA-256
  `ccdc368cb73781f04512023f4cfa54aaf89f6e7dab0881a86112ad8bdff9359e`; ambos passaram
  `twine check --strict`. Conteúdo e metadata foram auditados: licença e `py.typed` presentes,
  129 módulos Python do wheel bit-idênticos à fonte, nenhum teste/tool/bench, bytecode, cache ou
  caminho local indevido, `Requires-Python >=3.11`, entry point `oktografx` e extras esperados.
  Instalações limpas core-only e `[accel]` passaram `pip check`, versão/branding/CLI, create/query,
  `verify`, checkpoint, cold read-only reopen e, no extra, checksum nativo e busca vetorial NumPy.
  A auditoria independente Nexus `hof_a8c2280dc3be489792d4f51b54ba8a69` foi verificada/PASS.
  Por fim, o gate Pulse contra **esse wheel exato**, offline e fora dos checkouts, passou **1/1 em
  277,82 s**; JUnit SHA-256
  `0f2e6f7a170833a4b80de20d54ea7db3c0cd055cf2898ec9e4a3faade020a9ff` e evidência de release
  SHA-256 `821e42136ed9c8ea04671cb2bc93fbc6dc2668c14dc33e3c7d0b393bc7d27b3c`. A evidência autentica
  Community `159ef75328dc68493379d95b9f72826b660b8d84`, Core
  `341bccdbb1232ee7ed6a9bcce380fdf9616c1600`, Grafx `0.0.1`, payloads byte-idênticos, matriz
  instalada, concorrência/crash-resume SQLite, paridade de projeção e MCP HTTP. Não há blocker
  objetivo aberto nem upload realizado naquele checkpoint; a depreciação futura da forma TOML de
  `project.license` e a consolidação das duas fontes de versão ficam como manutenção pós-`0.0.1`,
  sem ampliar este gate.
- **Release `0.0.1` — publicada no PyPI e reinstalada com sucesso.** O checkpoint conjunto de
  2026-09-02 criou o projeto público `okto-grafx` e publicou exatamente os dois artefatos auditados:
  wheel SHA-256 `3acc1bbc17bb4629caea3cbca60ff503c5cea1f8891cd829227a08eb5bc75070`
  e sdist SHA-256 `ccdc368cb73781f04512023f4cfa54aaf89f6e7dab0881a86112ad8bdff9359e`.
  A API pública do PyPI confirma nome `okto-grafx`, versão `0.0.1`, `Requires-Python >=3.11`, nomes,
  tamanhos e ambos os digests. Uma venv nova baixou sem cache `okto-grafx[accel]==0.0.1` diretamente
  de `https://pypi.org/simple/`; o wheel obtido repetiu o SHA auditado, `pip check` ficou limpo e a
  origem foi `site-packages`, não qualquer checkout. Versão, CLI/branding, CRC-32C nativo, NumPy,
  criação e consulta vetorial, `verify`, checkpoint e cold read-only reopen passaram. O token foi
  mantido somente na memória do processo de upload e removido do ambiente ao final; nenhum segredo
  foi gravado no repositório ou nos artefatos. O `uv.lock` Community, que antes não podia resolver
  um projeto ainda inexistente, passou a registrar o wheel/sdist Grafx e `google-crc32c` com URLs e
  hashes públicos em `perf/st2-pulse-generation@92ece5d`; `uv lock --check` passou sem alterar as
  demais 152 resoluções. A instalação operacional do usuário também foi atualizada de Pulse/Core
  `0.3.1` para `0.3.3`, com Grafx `0.0.1` público e `[accel]`; `uv pip check` validou 121 pacotes.
  O diretório de dados preexistente não foi tocado nem teve backend alterado implicitamente. O gate
  de publicação `0.0.1` está concluído.
- **Passo futuro pós-`0.0.1` — remover o acoplamento documental F16 entre Community e Core.** O
  inventário `Community-to-Core import rows` é fato calculado no Community, mas hoje também integra
  o bloco gerado no README do Core. Isso obriga um commit documental Core sempre que imports do
  Community mudam, enquanto a certificação M-PULSE-7 pina o Core por HEAD. A solução estrutural deve
  ser desenhada com ADR e nova certificação na linha `0.0.2`, pois o gerador/validador reside em
  `okto_pulse.core.application.boundary.saas_closure_report` e uma correção honesta altera `src/` do
  Core. Até lá, o carry-forward acima é deliberadamente exato, autoexpirável e fail-closed; qualquer
  novo commit ou mudança em catálogo produtivo exige nova decisão, nunca atualização automática.
- **Passo futuro — cache/bundle autenticado da autoridade do harness M-PULSE-7 (não bloqueante para
  `0.0.1`).** Decisão de implementação congelada para o gate atual: embora seja tecnicamente
  possível, não será introduzido agora um cache/bundle do catálogo de autoridade, pois ele não é
  necessário para desbloquear o gate e ampliaria a superfície de segurança. A correção corrente
  fica limitada ao protocolo de fases do subprocesso: pré-validação integral de autoridade,
  abertura, identidade e fingerprint; operação semântica sob os `30 s` reais; e pós-validação,
  fingerprint e fechamento. Cada fase continua fail-closed. Em evolução futura,
  pode-se evitar que cada `spawn` releia e recompile o catálogo produtivo completo por meio de um
  bundle canônico, imutável e content-addressed produzido pelo supervisor. Qualquer implementação
  deverá autenticar bytes e forma canônica, vincular o digest ao `process_authority` esperado,
  recomputar hashes/contagens do catálogo, validar raízes, revisions e worktrees, ativar o audit hook
  para imports anteriores e posteriores, falhar sem fallback diante de chave/cache divergente e
  executar rebuild integral no supervisor ao final. Os testes mínimos incluem adulteração de byte,
  catálogo, resumo, raiz e HEAD; entradas ausentes, duplicadas ou com escape de caminho; import
  divergente antes/depois da ativação; modificação TOCTOU persistente; e preservação dos PIDs
  isolados e do digest de autoridade em Board/Pulse. Essa otimização é de custo operacional do
  harness, não gate de performance, não muda o watchdog semântico e não condiciona a compatibilidade
  Pulse, a auditoria integrada ou a publicação conjunta de `0.0.1`. Uma futura revisão do formato
  do **schema** do manifesto deverá explicitar separadamente o isolamento por processo, a janela de
  `30 s` da operação semântica e as contenções fail-closed de pré/pós-validação. A revisão `v3` da
  fixture continua deliberadamente no schema `/1`: ela ratcheta conteúdo, suplemento e autoridades,
  sem antecipar esse aumento de superfície nem bloquear a certificação corrente.
- **Freeze de publicação `0.0.1` — atribuição e histórico corrigidos antes do próximo pin.** A
  seção III da licença exige que versão, help e usage do CLI preservem `Okto Grafx`, `Okto Labs`,
  copyright e identificação da licença. O banner antigo mostrava apenas `oktografx 0.0.1`; todas as
  superfícies textuais agora carregam os quatro elementos e têm regressões de processo real. O
  `CHANGELOG` também foi consolidado em uma única seção `0.0.1` datada de 2026-09-01, sem manter uma
  falsa publicação em 2026-08-23 nem deixar mudanças já integrantes da release sob `Unreleased`.
  Essas são correções de distribuição/CLI; nenhum arquivo do engine, storage, WAL ou query foi
  alterado. O SHA resultante `8cee82b9b92529f2ba767519c01c161f20876dce` está pinado pela fixture
  v3 e preservado em worktree detached como candidato exato do próximo run e dos artefatos de release.
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
- **Pivot de performance autorizado em 2026-08-30.** Por ordem explícita do usuário, o gate
  M-PULSE-7 em `Community@d44c821` foi interrompido sem receipt e sem alterar os repositórios; o
  workspace foi preservado. O relatório
  `D:\Projetos\Techridy\claude-scratch\GRAFX_PERFORMANCE_NEXT_STEPS.md` (564 linhas, SHA-256
  `367a5f3abe6adcf4907a7e639558e844bdca8473181667edfaafdc7dcaf044c7`) foi lido integralmente e
  criticado por Codex e Claude. O consenso verificável do handoff Nexus
  `hof_13e2e4033b704687a1cb4051bc31988a` separa: (A) otimizações que preservam integralmente
  multi-writer/multi-reader; (B) mudanças que preservam os invariantes, mas exigem emenda numerada;
  e (C) hipóteses que estreitam contrato ou podem degradar concorrência e não serão iniciadas sem
  decisão do usuário. ST-2, CM-1/2/3 como então redigidas ficaram fora daquela execução; a
  autorização explícita posterior de ST-2, registrada acima, supersede somente esse veto histórico.
  CM-1/2/3 continuam fora e CM-4 fica deferida.
  CE-1 exige, antes do código de produção, spike NTFS, emenda de formato/porta e matriz de crash.
  Todo ganho inferido serve apenas para ordenar: promoção exige delta RAW patch-a-patch mais os
  contadores discriminantes. A onda 0 versiona o harness H1-H8; QW-1 abriu a primeira frente segura
  em `perf/qw1-frontier-aware`, com suíte `tests/query` e Ruff verdes antes da medição oficial.
- **Onda 0/H1-H8.1 concluída e publicada.** O harness está em Pulse Community
  `perf/w0-harness-h1-h8@0dfb5269dd8531fd4db2679fc80b649c64bd9b09`, SHA-256 normalizado
  `a50e9713c224424e706eae56dd72c2c27e5e8393c78ac0ef658571d3e6052c35`. A correção H3 usa
  proxy apenas no harness para medir o dispatch real de scopes slotted, sem alterar classes de
  produção. Os 13 testes dedicados, Ruff e um smoke real com timers de begin/execute/commit
  disponíveis passaram; o digest lógico certificado permaneceu
  `c994255b0bf695040c972ce339cc5d580ec253d2146674664e7722cf6b5a7f81`.
- **QW-1 medido e aprovado para promoção.** O par sequencial, mesma máquina/janela, mesmo harness,
  mesmo Community/Core, mesma forense e mesmo digest comparou
  `4c474b56ac35cd3169f18ef416bc3d2ae906083c` com
  `dd40c66ed1572f70ea8dd00c34785dedf1bcd224`. A soma das 12 medianas RAW caiu de
  `14.284,03` para `7.375,84 ms` (`-48,4%`, razão contra Ladybug `14,72x -> 7,60x`). Os maiores
  ganhos foram `delete_edges_by_session -63,7%`, `replace_node_payload -57,7%`,
  `replace_with_source_deleted_tombstone -54,2%` e `reconcile_projection_active_set -53,5%`;
  leituras frescas de `delete_edges` caíram de `8.834` para `2`, sem mudar page writes ou o
  protocolo de commit. Artefatos: baseline SHA-256
  `d4149c1234b22e18db31b902645c3511b28b476d0a46edaa7f6fb554bbbcad86`; QW-1 SHA-256
  `86a5eaed615cb0febc5ea122a796be6ccde410879f7f10af7b7ef1dff038a540`. A revisão independente
  Nexus `hof_bfa99be5471e4874871624e37ed7d137` foi verificada/PASS e concluiu `PROMOVER`; o parecer
  `D:\Projetos\Techridy\claude-scratch\QW1-RAW-GATE-REVIEW.md` tem SHA-256
  `3de0f0d47ab202abac5e0bd77280303ac2d5f3e61da7eb157cd4bd25c4b1c041`. A suíte completa chegou
  a 100% com somente duas falhas AST de docstrings já existentes e sem falha funcional; as três
  docstrings faltantes foram adicionadas em commit de higiene, o teste de documentação integral,
  `tests/query + tests/test_language_surface.py` e Ruff terminaram com código `0`.
- **CQ-3/QW-2 medido e aprovado para promoção.** O carry one-shot reutiliza somente o certificado
  obtido no pós-read durável da leitura exata anterior; a prova otimista pós-leitura continua
  obrigatória, qualquer recusa cai no caminho fresco e todo publicador local de page 0 invalida o
  carry. O F4 in-process e com processo spawnado prova que uma marca stale estrangeira entre
  lookups continua sendo observada e recusada fail-closed. Origem revisada/pushed:
  `perf/w1-cq3-certificate-carry@e68a15098345a5d68e8d2e44e78a3a49caf6aef2`; integração sobre
  o main corrente: `ce1c747`. O gate Nexus `hof_79e50ac8a243463ab8d3d3778dd9557f` foi
  verificado/PASS. O par com QW-1 manteve digest/harness/Community/Core e page writes idênticos;
  a soma RAW caiu de `7.375,84` para `6.801,17 ms` (`-7,8%`). `read_fresh_page` caiu, por exemplo,
  de `32 -> 17` em projection, `18 -> 10` em lineage e `12 -> 7` em replace; o JSON CQ-3 tem
  SHA-256 `acc672c32d7d91d05e4b4bcf13c16c329fb0f66ae5e752f44565df8e767c5c6f`. Red-first 4/4,
  mutantes do pós-read/fallback/invalidação mortos, 262 testes focados e multiprocesso reproduzidos
  pelo Codex e Ruff passaram; a mesma bateria passou novamente no SHA integrado.
- **CQ-1/QW-7 aprovado por ganho causal e regressão, com wall temporal inconclusivo nesta
  rodada.** A cauda do WAL agora é descoberta uma única vez dentro da seção exclusiva de commit;
  refresh explícito, recovery e a barreira continuam forçando uma redescoberta, e a próxima seção
  volta a observar anexos de outro processo. Origem revisada/pushed:
  `perf/w2-cq1-wal-tail-hold@d46c46aea447e6f813b8fbf57d22d5181b3b1642`; integração sobre o
  main corrente: `7d990bb`. Em todas as 12 famílias do perfil H1-H8.1, as medianas instrumentadas
  de `WalManager._refresh_tail` e `WalManager._discover` caíram de `8 -> 1` por operação e
  `LocalStorageDevice.list_files` de `9 -> 2`, enquanto page writes permaneceram idênticos. O
  perfil concluiu 110 commits preparatórios e 180 operações medidas, 12/12 famílias, exit `0`,
  com o mesmo digest lógico/harness dos perfis anteriores; artefato
  `cq1-d46c46a-pf5-h1h8_1.json`, SHA-256
  `f316c097df2096bb8148b54029bebbee63ef90a8308052ffed29b6365f661cd2`. A soma RAW foi
  `7.376,78 ms`, contra `6.801,17 ms` na rodada CQ-3, mas a carga do host subiu de
  `12,8%/8,5%` para `17,7%/22,1%` nas amostras antes/depois; como a alavanca prevista é de apenas
  `75-160 ms`, esse wall não resolve o delta e não é apresentado como ganho temporal. A promoção
  se apoia na remoção discriminante de 7 redescobertas/listagens por commit, sem page-write extra,
  nos quatro mutantes mortos (hold ausente, barreira não-forçada, vazamento do hold por exceção e
  releitura de `commit.state`), nos testes de append estrangeiro multiprocesso e na bateria
  integrada WAL/commit/multiprocesso/API em 100%, além de Ruff e diff-check verdes. A premissa
  multi-writer/multi-reader permanece intacta: o reuso existe somente sob `COMMIT_SECTION`, não
  atravessa seções e não muda lease, época, publicação ou horizonte de leitores.
- **CQ-4/QW-6 medido e aprovado para promoção.**
  O memo por handle reutiliza a `CatalogStoreView` imutável apenas quando a imagem persistida, a
  identidade do objeto vivo e cópias rasas do conteúdo de tabelas/espaços continuam iguais. A
  prova otimista de época sob a seção participante ainda ocorre em todo acesso; o memo só remove
  reconstruções repetidas, é publicado depois da prova e nunca guarda uma observação não adotada.
  Origem entregue/pushed: `perf/w2-cq4-catalog-view-memo@ae2f443697cb8e22fe53c16b16137477a6090719`;
  composição sobre CQ-1 no branch de integração: `63dd813`. O handoff Nexus
  `hof_2029bdda6c5b48d69c0c36ea55be6303` foi verificado/PASS pelo Codex. A entrega do Claude
  passou 2.578 testes de foundation/API, 1.671 de query, 191 de memo+fronteiras hostis, 9 do
  consumidor Pulse pinado e Ruff. A reprodução independente confirmou hashes, passou 270 testes
  focados, matou os dois mutantes (`no_content_check`: 1 falha discriminante;
  `no_invalidation`: 3), passou 9/9 no Pulse com origens pinadas e aprovou em 100% a bateria
  composta CQ-1+CQ-4, além de Ruff/diff-check. Duas micro-medições de 400 acessos com 17 tabelas
  mostraram `5,2x` na entrega (`232,1 us` contra `1.213,0 us`) e `3,1x` na reprodução
  (`208,8 us` contra `641,6 us`); page writes ficaram em zero e a reconstrução caiu para uma por
  geração. O gate H1-H8.1/pf5 composto concluiu 110 commits preparatórios e 180 operações
  medidas, 12/12 famílias e exit `0`; contra o CQ-1 já publicado, a soma RAW caiu de
  `7.376,78` para `7.016,97 ms` (`-359,80 ms`, `-4,88%`) com host em `9,6%/7,7%` antes/depois.
  Digest lógico e harness permaneceram idênticos; page writes totais/de índice ficaram em
  `72/43 -> 72/43`. No passe instrumentado, o custo inclusivo de `_catalog_snapshot` caiu
  `224,0 ms` nas aberturas e `44,93 ms` nas operações, mantendo a mesma quantidade de provas de
  época. Artefato `cq4-959e911-pf5-h1h8_1.json`, SHA-256
  `c9b4add6296bf8ca140c80ab7f77b657785799d9b64fb50a1d00cef2ea028200`. Multi-writer/
  multi-reader permanece intacto: nenhuma lease, cerca, época, publicação ou regra D7 mudou.
- **QW-3 medido, aprovado e integrado.** O storage local deixa de repetir `realpath` nas portas
  de identidade do namespace depois que a raiz física já foi validada, substituindo-o por uma
  prova segmentada de identidade: diretório pai e raiz são conferidos antes/depois da listagem,
  o nome exato é exigido, cada segmento é inspecionado por `lstat` e redirects/reparse points e
  entradas especiais continuam recusados. A abertura de descriptor ganhou prova pós-open e retry
  limitado; criação, remoção, `atomic_replace`, recycle e cold miss usam a mesma fronteira. A prova
  física completa continua obrigatória na construção da raiz, em `_walk` e na barreira durável de
  diretório POSIX. Origem publicada:
  `perf/w2-qw3-storage-identity@6a2b374a36965d9adaeb1a3ff4cc5c7984cbc268`; composição sobre
  CQ-4: `0433d17`. O red-first falhou nos quatro casos esperados (dois skips de plataforma); as
  regressões cobrem custo zero de `realpath` nas portas de identidade/cold miss, troca concorrente
  de pai e raiz na listagem e no open, junction Windows/symlink POSIX e a prova física preservada
  no namespace do device. Três mutantes discriminantes foram mortos: remover a prova pós-listagem,
  confiar apenas na prova pré-open e restaurar `realpath` no caminho quente. As suítes de storage
  adapters/core, Ruff e `diff --check` passaram no SHA integrado. A suíte global não encontrou
  falha funcional; os únicos 11 erros iniciais eram precondições do corpus apontando para HEADs
  Pulse diferentes dos pins declarados, e o corpus passou integralmente em worktrees detached nos
  pins exatos, sem mudança de código entre as execuções. No gate H1-H8.1/pf5, 110 commits de setup
  e 180 operações medidas concluíram 12/12 famílias, exit `0`; contra CQ-4, a soma RAW caiu de
  `7.016,97` para `4.985,75 ms` (`-2.031,22 ms`, `-28,95%`) e todas as famílias melhoraram
  (`-11,8%` a `-45,8%`). A mediana de `nt._getfinalpathname` caiu de `774-2.568` chamadas por
  operação para `38-42` na maioria das famílias e `310` no caso pesado de delete de edges;
  bytes persistidos, page writes e index writes permaneceram idênticos em cada família. Artefato
  `qw3-6a2b374-pf5-h1h8_1.json`, SHA-256
  `1e29cf39638e5ec1e974308fb888139106761e2dd1c876133785e41158d85990`, mesmo digest lógico,
  harness e checkouts Pulse do CQ-4. Multi-writer/multi-reader permanece intacto: não houve mudança
  de lease, época, publicação, recovery, visibilidade ou protocolo de commit.
- **CQ-2/QW-4 aprovado por remoção causal discriminante e fases; wall RAW inconclusivo.** O
  `TransactionManager` arma `_own_published_lsn` somente depois de publicar com sucesso o próprio
  commit e o invalida antes de qualquer publicação genérica ou observação sem proveniência. A
  igualdade do token — nunca ordenação — permite ao `BufferPool` preservar apenas frames limpos
  quando a view moveu pelo commit deste mesmo manager; token estrangeiro, frame sujo ou proveniência
  ambígua mantém o drop integral. `catalog.dat` continua sempre descartado com bump de época no ramo
  próprio, cobrindo DDL/espaços fora do movimento de LSN. Origem publicada:
  `perf/w3-cq2-own-read-view@8ee02c7ff6dc135709bb6b6f8c58dd422f987a17`; integração sobre QW-3:
  `99107a7` (`2c0b242`, `458f760`, `1df4bc1` e a correção documental `99107a7`). A revisão
  independente reproduziu 19 testes discriminantes e verificou/PASS o handoff CQ-2
  `hof_20d1e762b320467cb805815033cc7aa9`; os cenários spawnados cobrem commit estrangeiro,
  `create_space`, checkpoint+redo+recycle, reopen/recovery e `mark_stale`. A primeira suíte global
  composta QW-3+CQ-2 passou todo o código funcional e expôs sete falhas de contrato de
  observabilidade: a métrica nova estava no catálogo, mas não no contrato/dashboard/pins. O
  follow-up `hof_bd7edcd11a4346b285669eb01533c7c7` foi verificado/PASS: contrato e painel M1 agora
  congelam `oktografx_read_view_drops_total{view_origin=own|foreign}` sem expor os contadores
  internos do harness; 661 testes de observabilidade/foundation passaram na entrega e na reprodução,
  além de Ruff e `diff --check`. Como esse follow-up só alterou contrato, dashboard e transcrições de
  teste, o restante já verde da suíte global não foi repetido. No gate H1-H8.1/pf5, 110 commits de
  setup e 180 operações concluíram 12/12 famílias, exit `0`, com host em `9,2%/9,0%`. Somadas as
  12 famílias instrumentadas, `_read_page` caiu `1.228 -> 124` e `_still_names` `2.631 -> 1.528`;
  exemplos: create `55 -> 5`, replace `159 -> 10`, projection `231 -> 25` e delete_edges
  `287 -> 6` leituras. O custo inclusivo de `_read_page` caiu `486,9 ms`; `flush` manteve 55
  chamadas e variou só `+1,4 ms`. O passe de fases caiu `5.613,44 -> 4.721,70 ms` (`-15,89%`),
  concentrado em execute (`-20,45%`), e o instrumentado ficou em `+0,12%`. O RAW isolado ficou
  `4.985,75 -> 5.324,61 ms` (`+6,8%`), mas é inconclusivo nesta janela: os três passes discordam
  de sinal, o delta cabe na resolução pf5 e o diff não oferece mecanismo compatível com `+338,85`
  ms enquanto elimina leituras e mantém bytes persistidos/page/index writes idênticos. Os alvos
  absolutos antigos (~2.500 reads e `_still_names` 20-40) usavam a base anterior a QW-1/QW-3; no
  denominador integrado, CQ-2 removeu praticamente todo o residual de delete_edges (`287 -> 6`).
  Por consenso Codex/Claude e pelo critério pré-fixado do relatório (`aceitar por contagens`), não
  foi feita repetição pf5 incapaz de arbitrar o ruído; uma reancoragem A/B/A ou pf10 fica para o fim
  da onda, não como novo gate deste patch. Artefato `cq2-99107a7-pf5-h1h8_1.json`, SHA-256
  `155663b67f3662119057de3fffcc648ef2b6ad63195d978dfcf51ed60e170c85`, mesmo digest lógico,
  harness e checkouts Pulse. Multi-writer/multi-reader permanece intacto: qualquer publicação
  estrangeira ou duvidosa conserva a invalidação fail-closed e nenhuma lease, época, horizonte de
  leitores, recovery ou protocolo de commit foi removido.
- **QW-5 medido, aprovado e integrado.** O pacote remove apenas overhead de materialização e
  interpretação: o heap reutiliza o `RecordHeader` já validado, lê slots por `memoryview` enquanto
  pinados e copia somente payloads aceitos; o índice valida os 27 bytes de toda entrada, mas só
  copia/constrói/atribui localização quando chave e referência correspondem; `decode_value` usa uma
  tabela fechada dos 12 tags, `decode_tuple` compara o tag persistido no caminho válido,
  `TableDef` pré-computa posições em um slot derivado imutável fora dos fields públicos e os três
  produtores aplicáveis não copiam os bindings vazios de `SingleRow`. A forma literal de campo
  `init=False` do relatório foi corrigida durante o gate: embora o plan walker pudesse ignorá-lo,
  o `mappingproxy` ainda vazava pelo `CatalogStoreView`; o slot herdado não-dataclass mantém
  igualdade/hash, codec, plan view e grafo público byte/shape-idênticos. Integração:
  `7df6e17` (a/c/f), `132e3f0` (d/e/g) e `5de6c95` (pins finais). As suítes completas de query,
  storage core, índice/API e fronteiras públicas hostis passaram, além de Ruff e `diff --check`.
  A auditoria read-only do Claude comparou o diff independente com o integrado e foi
  verificada/PASS no handoff `hof_2282f896e995432686bd92e332e09c0d`; os três únicos reforços
  apontados — layout relacional normalizado, reconstrução no round-trip do catálogo e
  `value/offset` da recusa de tag — estão pinados em `5de6c95`.
  O gate H1-H8.1/pf5 concluiu 110 commits preparatórios em `614,8 s`, 180 operações, 12/12
  famílias e exit `0`. Contra CQ-2, a soma RAW caiu `5.324,61 -> 5.269,96 ms` (`-54,65 ms`,
  `-1,03%`); o passe instrumentado caiu `14.406,72 -> 12.130,81 ms` (`-15,80%`), o wall das
  fases `4.721,70 -> 4.664,64 ms` (`-1,21%`) e execute `3.284,61 -> 3.183,66 ms` (`-3,07%`).
  Na amostra 0 de delete_edges com exatamente as mesmas chamadas lógicas, as 529.208 construções
  `Enum.__call__/__new__` desapareceram do top-25, `decode_value` cumulativo caiu
  `1,916 -> 1,481 s`, `decode_tuple` `2,738 -> 2,137 s` e `RecordHeader.decode` caiu exatamente
  uma vez por linha visível (`80.576 -> 68.636` chamadas). As 12 famílias mantiveram idênticos
  bytes persistidos/WAL, page writes totais/de índice e contagens de `_read_page` (`124`),
  `read_fresh_page` (`76`), `_still_names` (`1.528`) e `os.lstat` (`11.356`): nenhuma economia
  protocolar foi creditada ao pacote. Artefato `qw5-132e3f0-pf5-h1h8_1.json`, SHA-256
  `59232c0f485aa696fc0e4593b666bb20de0e9efca8ebf5e8f9f4fda21fe8a9fb`, mesmo digest lógico e
  checkouts Pulse. Multi-writer/multi-reader permanece intacto: formato, WAL, lease, cerca,
  visibilidade, publicação, recovery e coordenação não mudaram.
- **Onda QW-8/QW-9/QW-10 + ST-1 implementada e perfilada; QW-8/QW-10 passou o gate causal.**
  No Pulse Community, `perf/w5-qw8-qw10@64a9da6` troca as revalidações completas intermediárias
  do roteamento por um probe fail-closed de identidade do JSON de binding e do diretório físico;
  `begin`/`commit`, qualquer diferença e qualquer `OSError` continuam fazendo a prova completa.
  A mesma abertura passa um único catálogo já provado ao bootstrap/validação de schema. No Grafx,
  `perf/w5-qw9-open@75d956e` torna o limite de descriptors configurável e reutiliza uma listagem
  provada do diretório de índices durante o attach, sem remover prova de tamanho, cabeçalho,
  digest, frescor ou corrupção; a composição é `5896c12`. O default final foi corrigido de
  `256` para `128` após confirmar `_getmaxstdio()=512` no UCRT do Python suportado: duas instâncias
  Grafx podem assim coexistir deixando metade da tabela CRT para WAL, coordenação, Pulse e host,
  enquanto um processo que controla seu orçamento continua podendo elevar o parâmetro. ST-1 foi entregue em
  `perf/w5-st1-planner@20dd9b1` e integrado como `f01b659`: hops de uma aresta partem do lado
  seekable quando existe e consultas cujo predicado lê somente a relação usam `RelationshipScan`,
  mantendo overlay da transação, `ended`, limites de admissão e validação dos dois rótulos. As
  baterias focadas, query completa (1.684 casos após o reforço), API (92), fronteiras públicas,
  Ruff e `diff --check` passaram. O perfil H1-H8.1 dessa composição concluiu 110 commits de setup
  em `490,6 s` e 180/180 operações, 12/12 famílias, mesmo digest e page writes por família. Contra
  QW-5, a soma RAW caiu `5.269,96 -> 3.337,76 ms` (`-36,66%`), o instrumentado
  `12.130,81 -> 7.724,27 ms` (`-36,33%`) e as fases `4.664,64 -> 3.385,62 ms` (`-27,42%`);
  `delete_edges_by_session` caiu `1.916,61 -> 483,06 ms` (`-74,79%`). O aparente aumento de
  `delete_nodes_by_session` (`184,81 -> 265,58 ms`) não veio acompanhado de mudança em consultas,
  escritas ou chamadas de storage e atingiu begin/execute/commit juntos; fica como variância
  temporal a repetir no gate final, não como regressão aceita nem como alvo móvel. Artefato Grafx:
  `w5-f01b659-64a9da6-pf5-h1h8_1.json`, SHA-256
  `e5108fee403b6055a2902bc7b9524dc30313882538c4369cc77a4d3dd20f1ad8`.
  O candidato Community `64a9da6` passou 122 focados e mais de 529 casos da suíte larga, com
  Ruff/diff-check e worktree limpos. Falhas vetoriais observadas foram idênticas no Community
  base `0dfb526` e no candidato, e desapareceram ao trocar somente o Grafx de `ef0de63` para o
  pré-ST-7 `72671c0`; portanto QW-8/QW-10 não é causal. Handoff
  `hof_167a02e301534ede9b3e5ba8c9d2b650` verificado pelo Codex; registro completo em
  `claude-scratch/PULSE-QW8QW10-GATE.md`.
- **Referência Ladybug reancorada após QW-8.** Como QW-8 altera código comum do adaptador, a
  referência anterior de `970,62 ms` foi invalidada e não é reutilizada. O novo perfil sequencial
  em `Community@64a9da6`, com o mesmo digest e 180/180 operações, mediu `787,00 ms`; portanto a
  razão atual correta é `3.337,76 / 787,00 = 4,24x`, e não `3,44x` contra a referência antiga.
  `delete_edges_by_session` está em `1,84x`; o maior desvio isolado passa a ser
  `delete_nodes_by_session`, em `14,21x`. Artefato
  `ladybug-64a9da6-pf5-h1h8_1.json`, SHA-256
  `23314a1b2609e57518166ed81bdc6d8bc0a8a7f9a8b0b4f541d9ec27a3f0b653`.
- **ST-6 implementado, auditado e integrado.** O memo de pouso vive somente na transação e na
  tabela que o produziu; sua chave de validade compara por conteúdo a versão do schema, intents e
  linhas staged da tabela, além da identidade do snapshot. Assim, savepoint que retorna à mesma
  contagem, segundo update sem crescimento e update/delete no mesmo fluxo invalidam corretamente;
  `require_endpoints` e `_incident_edges` continuam fora do memo. Origem publicada:
  `perf/w6-st6-owner-node-memo@5daa502`; integração: `72671c0`. O red-first falhou nos 5 casos de
  reutilização/invalidação esperados; depois, 9/9 focados, 1.693 testes de query, 99 de API/limites,
  Ruff e `diff --check` passaram. A reprodução independente adicionou a família C10 de regressões
  e terminou verde. O handoff Nexus `hof_60d0dad3d3164074a546532713f400f8` foi verificado/PASS.
  O memo não atravessa transações, não altera WAL/storage/coordenação e não é usado no caminho de
  verificação de endpoints; multi-writer/multi-reader permanece intacto.
- **Gate global pós-ST-1/ST-6 concluído sem falhas.** A primeira execução coletou 10.756 casos e
  isolou cinco bloqueios determinísticos: duas docstrings locais, a fronteira pura para
  `types.MappingProxyType` e o materializador do corpus escolhendo o par inválido
  `Decision-[:relates_to]->Decision` para a consulta I12. A correção manteve a validação estrita de
  endpoints do Grafx e passou a tentar somente membros do domínio fechado de labels do Pulse
  quando o representante padrão falha exclusivamente no planner; o corpus voltou a ser
  reproduzido byte a byte, sem regeneração nem reclassificação. Após a higiene mínima, o gate
  global completo terminou em 100%/exit `0`, além dos testes focados de imports, documentação,
  ST-1, ST-6, corpus, Ruff e `diff --check`. Nenhuma regra de leitura, escrita ou concorrência foi
  afrouxada para obter o resultado.
- **ST-7 implementado, integrado e corrigido no gate cross-repo vetorial.** A abertura/recovery fotografa uma vez os
  watermarks por tabela após aplicar páginas e adotar o catálogo; a foto só é reutilizada dentro
  da mesma seção e qualquer tabela ausente é relida, enquanto a montagem final continua fazendo
  uma foto própria. O marcador de replay deixa intacto um header somente com certificado page-0
  fresco e saudável, ausência de páginas dirty e de `stale`/claim/rebuild local, igualdade do
  `built_through_lsn` residente e cobertura do high-water da tabela; qualquer desconhecido usa
  integralmente o caminho anterior. Uma gravação `STALE` por
  processo spawnado continua observada no open seguinte e a consulta usa o scan correto.
  Origem publicada: `perf/w7-st7-open-freshness@61ca9f5`; integração: `ef2d16e`; handoff Nexus
  `hof_dcd0953d2fe345fcb76e2a0e0098723a` verificado/PASS. Medição discriminante:
  `advance_built_through 4 -> 0` numa reabertura limpa, com as seis leituras de high-water
  preservadas; o caso representativo remove flush/fsync de quatro headers, e o M7 de 161 índices
  passa a escrever somente os realmente atrasados. Claude executou 7/7 ST-7, 33/33 staleness,
  637 index+txn, 1.612 recovery+WAL+storage, 2.390 API/foundation e 1.693 query; a reprodução
  independente passou os 40 testes críticos, inclusive o subprocesso real, Ruff e `diff --check`.
  Nenhuma lease, WAL, publicação, seção de commit ou premissa multi-writer/multi-reader mudou.
  O primeiro gate global integrado revelou duas premissas que os focados iniciais não cobriam:
  uma aplicação direta pode deixar page 0 dirty sem mover o watermark, e uma foto usada para o
  skip também precisa ser o floor de leitura por tabela. O follow-up adiciona a sonda read-only
  `BufferPool.has_dirty_pages`, vincula a foto ao manager/índices e mantém o fence de rebuild no
  snapshot global original; o fixture vetorial passou a declarar explicitamente seu teto
  sintético. A reprodução red-first falhou nos dois representantes, e depois toda a bateria
  vetorial/rebuild, 41 testes ST-7/staleness e 162 focados de buffer/index/vector passaram, com
  Ruff e `diff --check`. O gate global final do candidato corrigido coletou 10.831 casos,
  atravessou novamente toda a cauda vetorial e terminou em 100%/exit `0`, sem falhas; esse é o
  gate válido para promoção.
  O gate posterior do Pulse revelou uma lacuna que a suíte local não cobria: o skip por tabela
  também alcançou índices vetoriais/proximity, mas o contrato Pulse exige que seu
  `built_through_lsn` acompanhe a posição global. Com o mesmo Community/Core, a matriz vetorial
  teve 6 erros, discovery 5 falhas e composição routed 2 falhas em `ef0de63`, contra
  **8/8, 8/8 e 12/12** no pré-ST-7 `72671c0`. A correção mínima entregue em `75eac97` preserva o
  skip e o ganho para exact/hash e restaura o avanço global durável para vector/proximity. O
  handoff `hof_c23df4f3e7124d98a2409b23a6354e3f` foi verificado/PASS: 43/43 locais, 958 de
  index+recovery+vector e **28/28** no Pulse pinado. Nenhuma lease, WAL, seção ou premissa
  multi-writer/multi-reader mudou.
- **Gate final ST-6/ST-7 concluído e aprovado com a limitação temporal registrada.** O perfil
  H1-H8.1 em `Grafx@ef0de63`, `Community@64a9da6` e `Core@ccc1f345` concluiu 180/180 operações,
  12/12 famílias e manteve exatamente o digest lógico certificado `c994255b...`. O setup de 110
  commits caiu de `490,6 -> 397,7 s` (`-18,93%`), evidência do ganho no caminho de
  abertura/replay ao qual ST-7 se destina. No hot path contínuo, que exclui da mediana a primeira
  abertura de cada família, não houve ganho temporal nesta janela: RAW
  `3.337,76 -> 3.401,74 ms` (`+1,92%`), fases `3.385,62 -> 3.456,28 ms` (`+2,09%`) e passe
  instrumentado `7.724,27 -> 8.341,64 ms` (`+7,99%`). A promoção não credita esses números como
  melhora nem cria novo alvo: apoia-se no ganho causal `advance_built_through 4 -> 0`, na queda
  material do setup e no gate global de 10.831 casos; a variação marginal do RAW permanece
  explicitamente registrada. A razão temporal desta janela contra a referência Ladybug é
  `3.401,74 / 787,00 = 4,32x` (antes `4,24x`). Artefato
  `w7-ef0de63-64a9da6-pf5-h1h8_1.json`, SHA-256
  `2ce10d4200a9a420c75c081da9ae804001223cfb5f833aacdb91b854b68415c9`; check-only anterior,
  mesmo conjunto lógico e checkouts limpos: `w7-ef0de63-64a9da6-check.json`.
- **CE-1 aceito: G0–G7 e correção ST-7 × Pulse concluídos; candidato apto à promoção.** O spike
  `perf/w1-ce1-spike@fe9977d` foi executado em três ordens, 20 warmups + 200 amostras por caso.
  `atomic_replace` mediu `13,37-18,51 ms` de mediana; o slot quente `0,095-0,133 ms`
  (`108,85x-158,01x`), o caso frio `1,00-7,16 ms` (`2,58x-14,95x`) e a LRU real com evicção
  `3,00-4,37 ms` (`4,24x-4,98x`). Portanto o ganho se sustenta no cenário conservador de
  descriptor evicto, sem extrapolá-lo ainda para commits reais. Artefatos v2: `run-10` SHA-256
  `a41b645cf8d40abce0f46ff9cb3c8eb9d25c22c89f519bf6df4e1e33dd89af3c`, `run-11`
  `63fc77475a39697924d3b8ff8613c458a579ae5fe2ed9a02e7cc2986bc350262` e `run-12`
  `2e8e4e062734eb668b15ab74b350f7a5d20eb4b39ef18647354909b341118ff8`. O candidato implementa
  o envelope de três páginas (header imutável + dois slots), migração crash-safe v1→v2,
  `writer.lease` e `commit.state` in-place, reservas best-effort dos dois descritores dentro do
  orçamento, retirement v2 e rollback offline `oktografx control downgrade`. Unidade (11), matriz
  de crash/partial-write (30, inclusive todas as fronteiras de 512 B e 10.000 publicações),
  storage adapters, coordination, txn, recovery, API CE-1 e CLI passaram, assim como Ruff e
  `diff --check`. O gate experimental corrigiu uma premissa do plano: um prefixo setorial pode
  conter o registro inteiro, portanto o aceite é geração antiga ou nova **inteira**, nunca híbrida,
  e não “sempre fallback”. O round-trip v2→v1 preservou os payloads lógicos byte a byte. A proposta de
  formato/migração/crash matrix está em
  `perf/w1-ce1-contract@7b83dcd8c89c991bac35273d099a7a79e982d227`, documento
  `docs/architecture/CE1_TWO_SLOT_CONTROL_RECORD.md` SHA-256
  `5d07dcebaeda8c33a5220846985b80b8a4353e2294659eb352e076ffbf973dd9`; o ADR agora está
  `IMPLEMENTED CANDIDATE`, preservando multi-writer/multi-reader. O G6 H1-H8.1 concluiu 180/180
  amostras e 12/12 famílias com digest `c994255b...`: rename `4 → 1`, fsync `9 → 6`, listagens
  `9 → 2` e dois descriptors aquecidos. As sete famílias simples reduziram RAW
  `1.586,60 → 1.482,21 ms` (`-6,58%`) e commit phase `674,81 → 455,90 ms` (`-32,44%`); o agregado
  RAW das 12 variou `3.401,74 → 3.539,92 ms` (`+4,06%`) e não é creditado como ganho. Artefato
  `ce1-93a3ee3-64a9da6-pf5-h1h8_1.json`, SHA-256
  `e031cb4c7ede4859e6513d8614cb625db9fef38ffa0c30960a8852d1b0f2d2ae`.
  O smoke pareado pré-CE-1/CE-1 preservou a leitura pontual (`4,15/15,31 → 4,34/16,05 ms`,
  mediana/p99) e passou 4 writers + 3 readers com 500/500 linhas e `verify()` limpo. Após adaptar
  três expectativas históricas ao novo formato, a primeira suíte global terminou em **10.909
  passed, 17 skipped, 0 failed** em `98e52dd`. A revisão cega G7 passou com 222 testes e zero
  blocker; o gate final após `75eac97` terminou em **10.911 passed, 17 skipped, 0 failed** em
  `1.628,96 s`, com Ruff check e `diff --check` verdes. O H1-H8 final concluiu 180/180 amostras,
  12/12 famílias e o mesmo digest: rename/fsync/listagens/descriptors `1/6/2/2`, RAW agregado
  `3.078,01 ms` (`-9,52%` vs pré-CE-1), simples `1.289,48 ms` (`-18,73%`) e commit simples
  `406,52 ms` (`-39,76%`). Artefato `ce1-final-75eac97-64a9da6-pf5-h1h8_1.json`, SHA-256
  `4f943009c93ff8c85061519e578b08f0da85e677aa158e4b649f8701029c8d31`.
- **CE-2 concluída e certificada sem alterar a premissa multi-writer/multi-reader.** O produto está
  em `565ac37` (commit autoral `3be6659`) e substitui o registro durável por transação por um pin
  conservador por participante: o primeiro publish ocorre antes da escolha final do snapshot; o pin
  avança somente para frente até o menor snapshot vivo ou piso publicado quando ocioso; commit e
  rollback não removem o registro; `close()` remove uma vez; checkpoint avança o pin próprio antes
  do horizonte/recycle. A suíte global terminou em **10.910 passed, 19 skipped, 0 failed** em
  `1.912,75 s`, com Ruff e `diff --check` verdes. O instrumento multiprocesso
  `tools/measure_reader_pin.py` v8 certificou **47/47** critérios nos cenários long-reader,
  close-advance e kill-hold/TTL, com o leitor longo vivo por `90,00 s`, 19 scans idênticos,
  retenção exercitada até `2,94 s` antes do TTL e `verify("all")` limpo vivo e após reabertura;
  artefato `reader-pin-ce2-official-v8-565ac37.json`, SHA-256
  `d0285b5197a1ac05133cac3c90ab02ae1bd021f86d2785ad6ad6ffa9c3d29b3e`, instrumento SHA-256
  `815cdebe1802649aab19765ad106c2187404bb9bb3250340e0c2eea46b8d19da`. O H1-H8.1 manteve
  180/180 amostras, 12/12 famílias, digest lógico `c994255b...` e forense `6dd6cf05...`; rename,
  fsync, listagens e opens caíram de `1/6/2/2` para `0/4/2/0`, o RAW agregado de
  `3.078,01 -> 2.363,90 ms` (`-23,20%`), as sete famílias simples de
  `1.289,48 -> 975,79 ms` (`-24,33%`) e o commit simples de `406,52 -> 319,20 ms`
  (`-21,48%`). Contra a referência Ladybug congelada de `787,00 ms`, a razão desta janela é
  **3,00x**. O smoke histórico inalterado de 4 writers + 3 readers passou em `46,6 s`, com 500/500
  rows, 10,7 rows/s, leitura pontual `3,16/12,97 ms` mediana/p99, 173,9 statements/s e zero perda,
  duplicação, phantom, leitura rasgada, escape ou finding. Documento de decisão e reprodução:
  `docs/architecture/CE2_READER_PARTICIPANT_PIN.md`. Naquele checkpoint, ST-2 e CN-1 ainda estavam
  bloqueados sem autorização; não eram parte da CE-2 nem condição retroativa daquele gate. A
  autorização posterior e estritamente delimitada de CN-1 está registrada no resultado CE-3 abaixo.
- **M-7 WAL-only re-medido e aprovado; seu escopo não foi inflado.** Em `origin/main@530df34`, o
  runner D5 congelado (`30` amostras, `5` warm-ups, `2.000` registros, LadybugDB `0.16.0`, CRC
  puro e source pin explícito) mediu abertura/replay Grafx em `232,351 ms` contra `123,633 ms` no
  Ladybug: **`1,88x`**, dentro do teto `<=3x` e abaixo do histórico `3,51x/3,55x`. O exit global
  foi `1` exclusivamente porque o mesmo runner também mede durable commit, ainda fora de seu teto;
  M-7 passou. O instrumento constrói `WalManager` diretamente e faz `open()+scan_all()`: não chama
  `Database._open`, não exercita QW-9/ST-7 e não substitui a Etapa 5. Artefato principal
  `m7-open-replay-530df34-pure/calibration.json`, SHA-256
  `317091b280922e66a68e87dc39c9352b421ceb1e83ad26b641ab8f8431c0d37f`. Como o schema v1 não
  embute commit/source/comando/estado dirty, o sidecar retrospectivo da mesma sessão registra esses
  dados e ancora os hashes sem alegar autoautenticação (`provenance.json`, SHA-256
  `ec1ec8ecb09673f01b19a085f0efef67d7b3c2cbeb12b7b6e0a4254f373614e5`). O próximo discriminante
  congelado era F1 de dois processos para preparar a medição CE-3. Esse gate instrumental foi
  concluído no objeto auditado `d87a0c6520683b2d22929165136f718d14a1d795` e integrado/publicado em
  `main@2b8e9006218b9ccf013d914dfe98e32abaa5bdd3`: suíte focada **61/61**, Ruff, AST e diff-check
  verdes; auditoria independente PASS confirmou os seis guards congelados e a expansão pura dos
  eixos em **312 células**. O instrumento exige exatamente N+M métricas reais, oráculos live/cold/
  serial com cobertura, janela long oficial de 60 s, mesma base pós-bootstrap e provenance source+
  script recapturada/fail-closed. Naquele checkpoint, a matriz oficial longa ainda não havia sido
  executada; sua execução posterior está registrada no item seguinte. `600d17b`, `1eec9e4` e os
  demais commits intermediários
  permanecem apenas históricos; o squash aceito é `2b8e900`. O instrumento CE-3 literal também foi
  concluído no objeto auditado `da7f5e41e851a4cb8bc14c404b2f19df3ec9efc7` e integrado/publicado em
  `main@4f6201a1e2f520bfe747a9636af10b036984732d`. A raiz reproduziu **70/70** testes focados e o
  branch integrado fechou **136/136** em `tests/tools`, com Ruff/AST/diff-check verdes. O check-only
  real autenticou environment, checkouts, fonte e tool sem executar a matriz; artefato
  `ce3-checkonly-da7f5e4/ce3-check-only.json`, SHA-256
  `c2a643652caf6bd83748624f5a2808396c40c1467ae39bc190e9021242be1324`, com único shortfall esperado
  `check_only_has_no_measurements`. Isso certifica o instrumento, não um resultado. A ordem
  vinculante naquele checkpoint era **medição oficial CE-3 -> decisão CE-3 -> M-PULSE-7 10k**; o
  resultado oficial e a decisão finita estão registrados abaixo.
  Os instrumentos atuais de 4 writers + 3 readers e reader-pin são apenas evidência parcial. NT-1
  permanece condicional/bloqueado até o gate skip-decode e a decisão RC8-B. Nenhuma mudança que
  estreite multi-writer/multi-reader foi iniciada. O inventário residual corrigido foi concluído e
  verificado/PASS no Nexus `hof_ec7f56977c2a4a1593ab9753406ba6b2`; o relatório
  `PERF-RESIDUAL-POS-CE2.md` tem SHA-256
  `ffd7298831fdaaf3066eda99b869de2581e178937a2eed273752017ee7272fb4`.
- **CE-3 isolou e fechou a contenção de heap/OCC; a invalidação bounded foi publicada, a regressão
  de header esparso foi corrigida em `5b73890`, e o gate finito corrente é o gargalo de taxa
  residual comprovado pelo mesmo `same-10`.** No candidato Grafx
  `c2ca648b3388dbc969fccac7b35a85c84111a44f`, o artefato
  `D:\Projetos\Techridy\grafx-ce3-official-c2ca648\ce3-official.json` tem SHA-256
  `319ac5a3966c7fd096bdab8800e37e6beb4ea8e0a255db719f981b5a3c1f71af`. `RAW/idle-0` e
  `RAW/same-1` passaram; `RAW/same-10` falhou fail-fast na operação A 19/60, a quarta
  `replace_node_payload(Criterion)`, após 60 conflitos OCC tipados antes da durabilidade. Em paralelo,
  B concluiu 864/864 `create_node(Decision)`, recuperou 13 conflitos e não esgotou retry. A partição
  final `18446744071110879591` é exatamente `page_partition("heap.dat", 0)`: extents de tabelas
  logicamente distintas compartilham o contador `next_record_id` da página física 0 e, portanto,
  conflitam no grão de página. `verify("all")` terminou estruturalmente limpo. O observador lógico
  recusou a duplicidade deliberada do estado intermediário da fixture porque A parou antes das
  operações de limpeza 51–55; isso não é corrupção, perda, phantom nem vazamento de retry.
  A correção instrumental v3 está em `d80b438a4544ebb08be513739edfc3734cdd898e`: sempre preserva
  `verify("all")`, só aplica o fingerprint do perfil completo quando as 60 operações e suas
  pós-condições foram certificadas, representa amostras ausentes como progresso desconhecido e
  mantém cenários incompletos fail-closed. O gate focado passou 165/165, Ruff e `diff --check`
  passaram, e a revisão Nexus `hof_655c090ee6f547d98e249aeff95c367f` foi concluída/verificada/PASS.
  O check-only autenticado contra as mesmas fontes e os três checkouts fixos confirmou schema v3,
  identidade estável, tool blob `9637251d469248a015d93af6163c1120e2a85154` e somente o shortfall
  esperado `check_only_has_no_measurements`; artefato
  `D:\Projetos\Techridy\grafx-ce3-v3-checkonly-e311be8\ce3-check-only.json`, SHA-256
  `cec6f2d0b6671515502c85fda9d60988fa504a851c86252a01528ab2b020ee8c`. Ele não altera taxa,
  workload, tolerância ou política de retry.

  O usuário autorizou CN-1 com a resposta explícita `aceito. pode seguir`, registrada na sessão
  Claude `9699f9ef-9534-43db-9888-7031689e86f5`, mensagem Nexus
  `msg_a4a139748d0942f3a6df40cb23af0ed1` e trace
  `trc_38e40d10c9ec412fa45eb1b449c7e26e`. A autorização cobre somente identity-range leasing que
  preserve multi-writer/multi-reader e heap/WAL v1: reserva durable-before-use por
  `WRITE_PAGE+COMMIT`, imagem COW da page 0, cursor local burn-only, refill concorrente protegido por
  OCC, descarte de sobra em close/restart/fork e IDs explícitos fail-closed. Não autoriza ST-2,
  single-writer, group commit, relaxamento de consistência ou a proposta expansiva heap/WAL v2 de
  `IDENTITY-LEASING-V7.md`. O próximo gate é implementar e provar esse recorte, repetir primeiro
  `RAW/same-10` e somente então repetir a matriz CE-3 completa e o M-PULSE-7 10k.

  Esse gate foi executado no candidato autenticado
  `origin/perf/w8-ce1-production@6fd26f9e62b88eb852a4aa59aef2d5c35913752b` (código CN-1
  `40b2b43`). O artefato
  `D:\Projetos\Techridy\grafx-ce3-same10-cn1-6fd26f9\ce3-same10.json`, SHA-256
  `bbdc9dd52d1ebfb703246c5aa8e52beed8688d42bfce0d7b8f6b2e74cc4d27d3`, falhou fail-fast
  em `RAW/same-10`; por isso a variante instrumentada e a matriz oficial não foram executadas. O
  processo A avançou de 19 para **46 operações concluídas** antes de esgotar 60 tentativas na 47ª,
  `reconcile_projection_active_set`; a partição terminal mudou de `heap.dat/0` para exatamente
  `heap.dat/394`. B concluiu 807/807 operações em 819 tentativas, com 12 conflitos recuperados; 11
  dos 12 foram exclusivamente nas páginas de dados 350--353 e somente um incluiu a page 0. O cold
  `verify("all")` passou com 10.865 páginas, 9.467 registros e 12.178 entradas de índice. Portanto,
  CN-1 está fechado como eficaz e sem corrupção: removeu o gargalo de identidade; o blocker
  residual é starvation por colisão física na cauda de append da mesma tabela.

  A auditoria conjunta local/Nexus (`hof_c783f3f0cbb3411788d8c3145fd58db3`, concluído,
  verificado/PASS) registrou o diagnóstico em
  `D:\Projetos\Techridy\claude-scratch\CE3-SAME10-POSTCN1-RESIDUAL.md`. O candidato mínimo é
  preservar a primeira OCC integral desde o snapshot original e usar, somente para partições
  físicas descobertas durante a materialização posterior a `begin_read_view(current)`, esse
  `current` como baseline da segunda OCC. Páginas pré-staged, interesses lógicos, WAL, durabilidade
  e multi-writer/multi-reader permanecem inalterados. O usuário autorizou explicitamente essa
  emenda em 2026-08-31 com: `Autorizo ajustar o baseline da segunda OCC apenas para páginas físicas
  materializadas a partir da visão durável atual, mantendo a primeira OCC integral, páginas
  pré-staged no snapshot original e todas as garantias de WAL/durabilidade.` A implementação fica
  limitada a esse texto: o conjunto antigo é congelado e revalidado inclusive sobre eventual
  subcommit CN-1; somente localizações físicas medidas, novas e não pré-staged recebem o baseline
  atual. Não aumentar retries, reduzir a carga, alterar o harness ou introduzir page/tail leasing:
  isso esconderia ou ampliaria o blocker sem necessidade.

  **OCC-MB1 concluída, publicada e auditada em 2026-09-01.** O commit path congela a união de
  interesses e as localizações pré-staged antes da primeira OCC, revalida o conjunto antigo sem
  bypass se um refill CN-1 avançar o WAL e deriva o conjunto elegível exclusivamente das
  localizações `(file, page)` medidas após a visão durável. Drift tardio, overlap pré-staged e
  baseline anterior ao snapshot falham fechado; o payload COMMIT continua carregando o conjunto
  completo. Gates locais verdes: 100/100 focados nos cinco arquivos diretamente afetados,
  454/454 transacionais não-multiprocesso (14 deselected), 17/17 multiprocesso, 8/8 de falha e
  concorrência CN-1, 9/9 de cleanup/relink, Ruff, compileall e `git diff --check`. A auditoria
  independente local encerrou sem blocker e matou o mutante `_attempt_pages -> ()`. O parecer
  adversarial Nexus `hof_cb653c8a25fd479098fc31a5ac8415f9` foi concluído/PASS; sua ressalva
  inicial de manter bypass no refill foi posteriormente retirada pelo próprio auditor após
  conferir que o código revalida o conjunto antigo sem perdão e coloca o refill no baseline das
  páginas frescas. O candidato imutável é
  `origin/perf/w8-ce1-production@9498554e1c59f49a946571e9f281e5533ab2904b`; a auditoria final do
  próprio SHA (`hof_d8b8484183d24ee882daf12151854f37`) terminou PASS, sem blocker, sobre árvore
  limpa e ponta remota conferida.

  O `RAW/same-10` autenticado pós-OCC-MB1 passou funcionalmente: A 60/60, B 187/187, zero conflito,
  zero retry e verificações live+cold limpas. O único shortfall do cenário foi a taxa `4,1287/s`,
  abaixo do piso congelado `7,5/s`; a execução não é oficial porque a amostra inicial de CPU falhou
  o limite. Artefato
  `D:\Projetos\Techridy\grafx-ce3-same10-occmb1-9498554\ce3-same10.json`, SHA-256
  `3e5e7a74cb83163f5669260a04b6041b38b360de7daa9ebc740c0e0b8cbf44b0`. Isso encerra starvation e
  corrupção como blockers e torna a taxa o único gate vermelho.

  O diagnóstico H8 repetiu exatamente o `same-10` no mesmo SHA, sem alterar workload, retries ou
  tolerância. Ele também não é oficial (`CPU inicial 27,5%`), mas concluiu A/B e cold verify sem
  retry/refusal: RAW `4,1403/s`, instrumentado `4,6319/s`. Foram observadas 82 invalidações, todas
  full-pool; `_invalidate` consumiu somente `23,16 ms`, enquanto a reconstrução subsequente apareceu
  em `_read_page` (9.653 chamadas/`5.990,48 ms`) e `_still_names` (20.665/`8.564,34 ms`), com
  correlação wall de aproximadamente `0,7315` e `0,9845`. Logo o custo causal é reconstruir o
  working set depois do drop, não executar o drop. Artefato
  `D:\Projetos\Techridy\grafx-ce3-same10-occmb1-h8-9498554\ce3-same10-h8.json`, SHA-256
  `5e2d2544fdb3dc1f409c002f57c9404378561c682ad55229de8f764b0a445d0a`.

  **CE-3 bounded implementada e publicada sobre `9498554`.** `WalManager.read_bounded` só entrega
  um intervalo contíguo e terminal dentro de 512 registros e orçamento físico total de 2 MiB,
  compartilhado pela atualização da cauda e pela leitura do intervalo; recycle, dano conhecido,
  append incerto, lacuna, tipo desconhecido, checkpoint movido ou orçamento excedido declinam a otimização.
  `TransactionManager` classifica somente efeitos físicos conhecidos e liga a prova ao token
  anterior. `BufferPool` pré-valida todos os alvos e descarta sem escrita apenas páginas/arquivos
  provados; pin limpo fica discard-only. Sem prova, o fallback continua sendo o full refresh antigo,
  inclusive o write-back legítimo de frames sujos. A primeira OCC, páginas pré-staged, WAL,
  durabilidade e multiwriter/multireader não mudam. O escopo decorre da ordem geral de executar os
  ganhos do relatório de performance sem sacrificar funcionalidade; não usa nem amplia a autorização
  estreita do OCC-MB1.

  O audit read-only Nexus `hof_75a8a1e43bc24f1790b9a79d75d3749d` retornou PASS. A única pergunta
  prioritária foi fechada pelo próprio auditor no sweep `2026-09-01T01:37:18Z`: o caminho
  conservador normal permanece `_invalidate(None, doom_pinned=True)`; somente uma prova parcial
  atrasada contra token divergente recusa zero-write, como exige a atomicidade. O acesso WAL sem
  guarda também foi endurecido e colaborador malformado agora apenas declina CE-3. Uma revisão
  local independente reproduziu a leitura física de aproximadamente 3 MiB diante do limite de 2 MiB
  na versão intermediária; o candidato atual recusa esse caso antes de ler qualquer byte e debita
  atualização+intervalo do mesmo orçamento, com teste discriminante. A bateria multiprocesso
  diretamente afetada (visibilidade, read-view própria, índice exato e vetor) passou 10/10. Suítes completas
  `tests/storage_core`, `tests/wal`, `tests/recovery` e `tests/txn`, regressões focadas, o teste da
  fotografia única do tail, Ruff e `git diff --check` estão verdes. O produto foi publicado em
  `2feb579dded9e06c669d3c62c584e5f7964a7113`; o sucessor exclusivamente de teste
  `61485d92a2f2e5fcea54835f81c30107f3286cbd` acrescentou a prova discriminante de que refresh da
  cauda e leitura do intervalo compartilham o mesmo orçamento físico. O audit sucessor Nexus
  `hof_7c7f3a4b3a9c470ea1d5886445cf233b` retornou PASS: M1, M1b, M2 e M3 morreram e o teste intacto
  passou. M4 — neutralização simultânea dos quatro guardas de rebuild sob orçamento — permanece
  dívida de cobertura não bloqueante, sem defeito de produto reproduzido.

  A primeira repetição do `same-10` pós-CE-3, no SHA `61485d9`, não chegou à taxa. RAW e H8
  falharam na operação A `m7-profile-00002`, página 0 de
  `index/vector_Decision_decision_embedding_idx.idx`, com `cached_seq=19082` e dispositivo
  `19084`/`19086`. A recusa ocorreu depois da barreira WAL, quando o COMMIT já era durável; B não
  reportou conflito, retry ou recusa, e os verificadores cold-open ficaram limpos. O instrumento
  finalizou e removeu o scratch, mas isso não tornou o workload verde. Artefato
  `D:\Projetos\Techridy\grafx-ce3-same10-postce3-61485d9\ce3-same10-h8.json`, 60.074 bytes,
  SHA-256 `5b8df4d089a34ca91403a7c636c916474bb970e69a95b4754fe68ffd38f5a2a9`.

  A causa concreta não era OCC nem a emenda OCC-MB1. `stage_empty_observation()` não emite
  `INDEX_WRITE` para vetor `NULL`, mas `_commit_staged()` ainda avança e publica a página 0 do
  índice. O classificador bounded via o `WRITE_PAGE` de `heap.dat`, porém omitia esse efeito físico
  implícito; A conservava um header limpo antigo e só descobria o conflito CAS depois de seu próprio
  COMMIT durável. O diagnóstico conjunto está em
  `D:\Projetos\Techridy\claude-scratch\CE3-PAGE0-INDEX-FENCE-DIAGNOSIS.md`, SHA-256
  `c7bfaf2a669afd1f2862c3ae093845797b07ca77a19198e0fd953a9423b2db24`.

  O fix foi publicado em
  `origin/perf/ce3-bounded-invalidation@5b73890bdfddf9763c2b46514fc2c41c07ee30a4`.
  Quando o delta contém escrita de heap, ele acrescenta somente a página 0 de cada índice
  registrado; um `INDEX_WRITE/INDEX_RECONCILE` explícito continua dominando como alvo de arquivo
  inteiro. Inventário ausente, malformado ou acima do budget declina CE-3 e usa o refresh completo.
  A premissa falsificável ficou no docstring: todo movimento suportado de header sem registro lógico
  é hoje co-publicado com um `WRITE_PAGE` de heap; um movedor futuro precisa nomear seu alvo ou fazer
  a prova declinar. OCC, WAL, barreira, durabilidade e multiwriter/multireader não mudaram.

  A regressão pública multiprocesso foi executada red-first e reproduziu o mesmo CAS pós-barreira;
  com o fix, prova commit durável sem `recovery_required`, busca correta e `verify("all")` limpo
  também depois de reopen frio. Remover a regra mata o teste. Gates: 15/15 focados antes da
  formatação, 7/7 diretamente afetados depois dela, `tests/txn` 476/476, `tests/index tests/vector`
  em 100%/exit 0, Ruff check/format-check e `git diff --check` verdes. Execuções que resolveram
  `okto_grafx` pelo checkout primário devido à instalação editável foram descartadas; os gates
  creditados fixaram e imprimiram a origem `okto_grafx-ce3-bounded\src`. A auditoria local final
  retornou PASS e a auditoria read-only Nexus do SHA imutável
  `hof_e890ffe523ac4023aee333a8b094c8e8` foi concluída/verificada/PASS, sem blocker. O relatório
  `D:\Projetos\Techridy\claude-scratch\CE3-SPARSE-HEADER-AUDIT-5B73890.md` tem SHA-256
  `84bd4c36179df5f6e5930a556beddad76cbe4716a9715d4bbbe91d7ebe26c90a`. As observações sobre um
  header dirty improvável, gestor de índices nulo e peso menor do verify live ficaram registradas
  como não bloqueantes; o reopen frio e o benchmark real fornecem a prova forte.

  O check-only autenticado em
  `D:\Projetos\Techridy\grafx-ce3-same10-sparsefix-5b73890\preflight.json`, SHA-256
  `4bcd84e600e1ce888992e3c740424a1b5373a26f2e195198353c80d80e978ada`, confirmou Grafx
  `5b73890`, harness `0dfb526`, Core `ccc1f34`, fixture e tool blob imutáveis. A repetição comparável
  RAW+H8 terminou funcionalmente verde: A 60/60 nos dois passes; B 198/198 RAW e 192/192 H8; zero
  conflitos, retries ou recusas; efeitos, postconditions, imports, exits A/B/verifier e verificações
  live+cold passaram. A taxa RAW foi `5,0875/s` e a H8 `5,1507/s`: ganhos de `22,88%` e `11,20%`
  sobre `4,1403/s`/`4,6319/s`, mas ainda `32,17%`/`31,32%` abaixo do piso congelado `7,5/s`.
  A amostra inicial de CPU foi `99,9%`, portanto o run não é oficial; o shortfall H8 de ordem da
  evidência por operação já existia no artefato histórico e não é regressão do produto. Artefato
  `D:\Projetos\Techridy\grafx-ce3-same10-sparsefix-5b73890\ce3-same10-h8.json`, SHA-256
  `73fd46b85f3e7a72ddec99603144ad9cc344286724819959384b610b94b38983`.
  `finalization.status=passed`, zero erros, scratch removido, fonte inalterada e identidade estável.
  Como o gate de taxa falhou, a matriz CE-3 completa não foi iniciada; o próximo passo é somente o
  gargalo residual medido no H8, sem alterar workload, retry, tolerância ou garantias de produto.

  **Probe finito F2/CN-2 implementado, corrigido e publicado em 2026-09-01, sem mudança de
  produto.** A reconciliação local e com Claude concluiu que o artefato anterior não separava o
  wall normal do commit das duas janelas globais compatíveis com checkpoint; portanto ainda não
  justificava CN-2 nem ST-2. O consenso imutável está em
  `D:\Projetos\Techridy\claude-scratch\CE3-NEXT-GATE-RECOMMENDATION.md`. O instrumento v4 foi
  publicado em `321b2f02051849806d734759395d80140845da35`, mas o primeiro run real revelou que os
  eventos delimitadores `_coordinator_section` eram indevidamente incluídos na união de cobertura:
  508 reconciliações pareciam conclusivas, enquanto apenas 84 permaneciam conclusivas sem esses
  contêineres. O artefato
  `D:\Projetos\Techridy\grafx-ce3-same10-phaseprobe-r1-60a0323\ce3-same10-phase-r1.json`
  (SHA-256 `a37a3c3d42f2096d30b340a70fa21a8290c42c0788ee65555828b04ead7a501f`) continua válido para
  funcionalidade e throughput, mas está invalidado para atribuição de fases e não conta como uma
  das duas repetições do gate.

  A correção fail-closed schema v5 está em
  `origin/perf/ce3-bounded-invalidation@74e4cf77fe07ede5ee9f7dfc0939fdb33b392731` (tool blob
  `28a03217f7de9d4c62447164f0b24210b2f957ed`, test blob
  `b38d16acccdd854067d160c5d2d18a876e1f52d8`). Os delimitadores continuam medindo a posse da
  seção, mas não contam como trabalho coberto; snapshot e validador aplicam a mesma regra. O único
  hook adicional, `IndexManager.open`, cobre a transição causal de `452,309/545,582 ms` observada
  após `WalManager.recycle`; `BufferPool.begin_read_view` foi rejeitado por medir apenas
  `0,314/0,647 ms`. RAW não instala hooks; H8 usa 26 hooks em A e B e restaura as identidades
  originais. O v5 rejeita as 252 capturas v4, captura commit+checkpoint no motor real, passou
  171/171 testes, Ruff, compile, format-check e diff-check, e recebeu PASS independente nos blobs
  finais. Os dois commits tocam somente `tools/` e `tests/tools/`; o diff em `src/` é vazio. Não
  houve alteração em OCC, WAL, durabilidade, multiwriter ou multireader.

  **Gate schema v5 encerrado em duas repetições autenticadas, sem desconto H8→RAW.** No R1,
  preflight SHA-256 `0505461c14a0ac105f6f8fdb25143978b3e54b7bd1467f277c1b59be5a8c5373` e
  artefato
  `D:\Projetos\Techridy\grafx-ce3-same10-phaseprobe-v5-r1-7b28a45\ce3-same10-phase-v5-r1.json`
  SHA-256 `231c4eaf8eb57190dbdf76032726296133be6ec24a430bbdc6ef9d3df0953561`: RAW
  concluiu A `60`, B `194`, um retry OCC pré-durável em A e taxa `5,367894/s`; H8 concluiu A `60`,
  B `193`, zero retry e taxa `4,801222/s`. No R2, preflight SHA-256
  `0e2c730ee3d0e8533becc6e7aeeba67b058374afb786b1a0bf80572e4ab4d411` e artefato
  `D:\Projetos\Techridy\grafx-ce3-same10-phaseprobe-v5-r2-7b28a45\ce3-same10-phase-v5-r2.json`
  SHA-256 `977c44aee1d26906f29476a7a336af732e941e4779dea3c93defd09bfe1a6911`: RAW
  concluiu A `60`, B `189`, zero retry e taxa `4,481830/s`; H8 concluiu A `60`, B `147`, zero retry
  e taxa intrusiva `2,116703/s`. Todos os processos saíram com código zero, não houve recusa,
  reopen ou perda de efeito, e postconditions, verificação viva e cold-open `verify(all)` passaram
  limpas; identidade, fontes e checkouts permaneceram estáveis.

  A recomputação independente validou `253/253` capturas e 3 checkpoints no R1 e `207/207`
  capturas e 1 checkpoint no R2. Todos os checkpoints ficaram conclusivos pelo limite congelado:
  resíduo máximo `1,968%` na raiz e `3,252%` na seção. Nos checkpoints B, a fase dominante
  reproduzida foi `_redo_onto_device` (`2.424–2.473 ms`), seguida pelos walks de índice e
  `BufferPool.checkpoint`; as fases são inclusivas e não aditivas. O bloqueio cross-writer também
  reproduziu em `_hold_lease`: B65 aguardou `1.870,6 ms` o checkpoint A29 e A57 aguardou
  `3.278,5 ms` o checkpoint B182 no R1; A29 aguardou `3.800,9 ms` o checkpoint B64 no R2. Já o
  commit normal permaneceu inconclusivo e sem dominância reproduzida: roots/seções abaixo de 15%
  foram `68/253` e `20/253` no R1, `92/207` e `57/207` no R2. O R2 H8 ainda teve 24 outliers B
  acima de 500 ms, dos quais somente um sobrepôs checkpoint; portanto checkpoint não explica a
  cauda global. Os p50 B RAW de `145,7938/165,8788 ms` também provam que eliminar apenas
  checkpoints não alcança sozinho o piso `7,5/s` (`133,3333 ms`).

  O falso shortfall `instrumented_per_operation_hook_evidence_incomplete` era somente a ordem das
  chaves após serialização `sort_keys`; os conjuntos, contagens e durações estavam íntegros. A
  correção canônica fail-closed foi publicada em
  `origin/perf/ce3-bounded-invalidation@b77876296bc65a7de3aaa574ff77433b80ed1632` (tool blob
  `23234d75d91696f44675871d2e3589bd09b7498a`, test blob
  `1ca8c2bb0ebc96bf73959063bd0588f854daff25`), passou 172/172 testes e auditoria adversarial; ao
  revalidar R1/R2, resta somente o shortfall real de taxa. O diff continua restrito a `tools/` e
  `tests/tools/`, sem alteração de produto.

  **Decisão finita:** o gate autoriza CN-2 apenas como otimização da cauda causal de checkpoint,
  sujeita antes de promoção à matriz crash+multiwriter já prevista. A fase B precisa liberar tanto
  `COMMIT_SECTION` quanto o writer lease, e a fase C deve readquirir/revalidar autoridade e estado
  atual; caminhos de claim/clear de rebuild permanecem monolíticos ou usam fallback fail-closed.
  OCC1/OCC2, páginas pré-staged, ordem WAL `append→barrier→apply→publish`, horizonte de leitores,
  durabilidade, multiwriter e multireader permanecem invariantes. O gate não autoriza ST-2, mover
  redo para fora da fase A, nem escolher uma otimização semântica do commit normal. A matriz CE-3
  completa e o M-PULSE-7 10k continuam bloqueados até `same-10 >= 7,5/s`.

  **CN-2 implementado, certificado e medido no candidato integrado
  `7acb9d869a7a6b9a533033309a3126f4de704f5b` em 2026-09-01.** O P0 de proveniência física/DDL veio de
  `8ec5322751632c37f81fe74044efd090abeff344` e foi reaplicado sem diferença de árvore em
  `53fa576`; o split CN-2 está em `e202324`, o instrumento de medição em `c739733`, a expectativa
  estrita de uma derivação da cauda do WAL por seção A/C em `f872fbe` e duas docstrings exigidas
  pelo gate público em `afb95b3`. No checkpoint normal, A congela target e inventário sob
  lease+`COMMIT_SECTION`+cauda do WAL; B executa somente barreiras de dados sem esses fences; C
  readquire autoridade, recusa regressão, refaz o sufixo, publica no máximo o target efetivamente
  barrierado e só então recicla. Claim/clear de rebuild permanecem monolíticos. No commit, a
  primeira OCC continua integral no snapshot original; sync/proveniência ocorre somente depois
  dela; a segunda OCC recebe exclusivamente o delta de páginas físicas materializadas da visão
  durável atual, nunca páginas pré-staged ou interesses lógicos tardios; WAL
  `append→barrier→apply→publish` não mudou.

  A certificação integrada passou 298/298 testes focados, mais 50/50 de checkpoint/rebuild em
  revisão independente. Três auditorias read-only locais e o handoff Nexus
  `hof_3781b51dbb77417cb899c50d1775cd29` com Claude concluíram PASS sem blocker. A regressão global
  coletou 11.306 nodeids: o prefixo até o único gate documental percorreu 9.308 nodeids sem falha
  funcional; depois das duas docstrings, `tests/test_language_surface.py` completo e os 1.998
  nodeids da fronteira restante passaram. A única outra interrupção foi ambiental — venv `uv` sem
  `pip` para o teste hermético de wheel — e o mesmo teste passou após completar a venv ignorada.
  `ruff check src tests tools`, `compileall src tools` e `git diff --check` passaram; a árvore ficou
  limpa. O `ruff format --check` global continua apontando a dívida preexistente de 254 arquivos e
  não foi convertido em alvo desta etapa.

  O preflight autenticado passou sobre o corpus físico
  `6dd6cf05316b38b01bade965b5fc5e4b118b1853f28c7aef705a8d283e0f0f0c` e a proveniência
  `655c0ec0a10d4ee272fe6e2e6eea0b584d165b8ae3645148a77cd16bd1c428c8`. Uma recomputação local
  independente e o handoff Nexus `hof_8d1bcdb495ab497c91869f300d4688e2` com Claude convergiram
  nesses números depois de corrigir uma subcontagem inicial dos checkpoints do processo B. O
  runner não aceita o rótulo `official` para `scenario=same-10`; por isso as duas repetições válidas são evidência
  autenticada não oficial, sem confundi-las com a matriz completa. R1b, SHA-256
  `b4d1fa1cff1de76f5ea9a5b21452e5f07ea4dea0897b73ed0274aa583d227e95`, começou/terminou com CPU
  `14,8%/9,9%` e mediu RAW `5,3178/s` e H8 `4,1577/s`. R2, SHA-256
  `fdf28373c49e11fa4b19420fee8e170c2ebc5a1a2e8bdfe22a49ffd08b4612b6`, começou/terminou com CPU
  `16,9%/21,0%` e mediu RAW `1,3218/s` e H8 `4,5368/s`. Todas as quatro passagens completaram A
  60/60; B registrou 173/180 efeitos em R1b e 161/171 em R2, sem recusa terminal, com verify live+cold e
  fonte imutável. O único retry tipado ocorreu no RAW R2 e sua cauda B (`p99 7.392,2 ms`, máximo
  `11.489,2 ms`) é variabilidade real, não descartada. Uma tentativa anterior, SHA-256
  `a270b74b5efdee630059b2d2211a387ce3a3752202debaa16fe31e8183d84dba`, ficou apenas diagnóstica:
  funcionalmente passou, mas CPU inicial `43,2%` invalidou seu uso como evidência de performance.

  Nos seis checkpoints H8 válidos, as fases cercadas A/C duraram `1.086,6–2.059,2 ms`; as 978
  chamadas de barreira ficaram integralmente fora do lease e do `COMMIT_SECTION`, somando
  `497,8–622,5 ms` por checkpoint, e três commits estrangeiros normais concluíram nessas janelas.
  Contra quatro fences monolíticos históricos, a maior exclusão contínua caiu `49,26%` na mediana e
  `45,80%` no máximo. Isso não tornou o checkpoint global mais rápido — sua duração total mediana
  cresceu cerca de `38%` — nem produziu ganho de throughput reproduzível; o RAW variou `4,02x`
  entre R1b e R2 no mesmo SHA. A atribuição do commit normal também permaneceu inconclusiva pelo
  limite do próprio instrumento (`2/62` seções conclusivas). Assim, CN-2 provou somente o efeito
  local pretendido, sem enfraquecer a concorrência: nenhuma passagem atingiu o piso congelado
  `7,5/s`. A matriz CE-3 completa e o M-PULSE-7 10k permanecem bloqueados.
  O próximo survivor preexistente é ST-2, porém ele continua fora desta autorização: exige decisão
  explícita para a emenda A66.1/CF-12, prova F4 multiprocesso e nova repetição do mesmo `same-10`.
  Essa restrição era o estado daquele gate CN-2 e foi superada apenas pela autorização explícita
  posterior registrada no item ST-2 do topo desta seção; não há autorização retroativa implícita.
- **O ratchet de entrada do M-PULSE-7 está certificado; ele não é o run de 10.000 operações.** No
  Community `6595abdcfa788dfa2cc8da1a53ff96c378790531` (base
  `d44c82155e9884c556813ea96dec829be567c236`, branch
  `origin/milestone/grafx-mpulse7-ratchet-530df34`), o pin Grafx é
  `d39e27435171574ab6f03bc1d17672b26bf163b2`. O manifesto tem SHA-256 físico
  `d1777bb26aee2feae5c8d5f4593840c08bdc37474ad6be4bdfe5334daedd0192` e canônico
  `1e6e92fc3bae3b54d3052ca9055b7682a9d518927573e0ffcbfcbb4568cf9f93`; o corpus tem SHA-256
  físico `0997747ed8bb9172d05781a62e5f81e7694630b173aaa152ac9ea28daec9d13f` e lógico
  `b29334edf6e7c1e6b9419a4f3add84ede4baad94fdeaecb0c679261a78f241cc`. O gate focado passou
  12/12 e a auditoria independente passou 39/39, sem alterar critérios, backends ou contagens. O
  run de 10.000 operações ainda não foi iniciado.
- **Compatibilidade Okto Pulse: M-PULSE-1 a M-PULSE-6 concluídos e certificados conforme o quadro
  9.7; M-PULSE-7 tem suas entradas certificadas e permanece o gate serial operacional.** O primeiro trace representativo
  expôs um blocker de performance, não de semântica: no mesmo workload, Ladybug concluiu em
  aproximadamente `8m44s` (`~19,1 ops/s`) e Grafx em `16h10m58s` (`~0,172 ops/s`). A causa
  dominante reproduzida foi a atualização/flush de page 0 de índices de tabelas não relacionadas
  em cada commit, agravada por varreduras globais de namespace no Windows. O RUN #4 posterior foi
  interrompido deliberadamente em 2026-08-29 e não produz verdict; ele será substituído por
  medições limpas após a estabilização. A frente A1 foi congelada, publicada e medida em
  `origin/perf/a-index-freshness@5002a77c8d4e59e137890a45bcf61874398d2dcc`: troca o piso global
  pelo high-water físico da tabela coberta, preserva o teto publicado e a marca stale durável para
  qualquer row intent sem observação do índice, inclusive para readers estrangeiros longevos. No
  micro-trace limpo `per_family=1`, o baseline `6d9b7a1` em modo diagnóstico `reopen` e o A1 em modo
  `continuous` executaram as mesmas 12 famílias e o mesmo conjunto lógico
  (`operation_set_sha256=61e21ccf…`), ambos 12/12. O wall RAW agregado caiu de `97,6 s` para
  `68,5 s` (`~1,43x`) e as páginas de índice instrumentadas de `1.949` para `43` (`~45,3x` menos),
  com delta de WAL idêntico em cada família. Os relatórios autenticados são
  `codex-base-reopen-pf1.json` (`4f828baf…`) e `codex-a1-continuous-pf1-v2.json` (`a749c2ee…`);
  `per_family=5` ainda estava pendente naquele checkpoint e foi concluído no candidato integrado,
  conforme o registro abaixo. A
  frente B foi certificada no handoff Nexus `hof_7a5be05b5b644b609b92b5e8c09fdeea`, publicada em
  `origin/perf/b-storage-namespace@420ca4886e5e226aa10bf3a28a1396f98471faca` e integrada
  serialmente sobre A1 em `main@91a59c6806659cdd75d1b33500885f0c7354de6b`. Ela restringe
  `list_files(prefix)` ao namespace pertinente e remove a re-prova por `realpath` em hits quentes,
  mas mantém a identidade de todos os descriptors e fixa cada diretório antes/depois do `scandir`;
  uma regressão intermediária que seguia uma junction NTFS injetada entre prova e varredura foi
  reproduzida, recusada na revisão e fechada antes da certificação. O teste pós-integração dos
  adapters de storage, o foco namespace/junction, Ruff e diff-check passaram. A medição limpa de
  A1+B em `continuous/per_family=1` repetiu as mesmas 12 famílias, o digest lógico e a imagem
  forense do A1 isolado, com WAL idêntico por família. O RAW agregado caiu de `68,5 s` para
  `19,3 s` (`3,55x`), as páginas de índice permaneceram em 43 e o tempo instrumentado acumulado em
  `list_files` caiu de `21.798,5 ms` para `195,3 ms` (`111,62x`). O relatório autenticado é
  `codex-main-a1b-continuous-pf1.json`
  (`0c5ee72fc42c6cb1ad74b18387bc74e6e274c897d01de582f281786d5f7bde27`). O relatório Codex
  anterior está congelado no commit `aa3de79f`. Na rodada oficial `per_family=5`, o candidato
  `main@fb1c7c9` e a referência Ladybug completaram 12/12 sobre o mesmo digest lógico
  `c994255b...`, a mesma forense e as mesmas pós-condições. A soma das medianas RAW foi
  `18.561,15 ms` no Grafx e `970,62 ms` no Ladybug (`19,12x`); portanto a estabilidade e a
  correção estão preservadas, mas nem o milestone intermediário D5 (`<=10x`) nem a meta final
  `Grafx <= Ladybug` do plano de performance foram atingidos. O manifesto M-PULSE-7 registra os
  benchmarks sem impor SLO, mas o plano de performance continua aberto. A evidência atribuiu
  `5.606,6 ms` instrumentados a 94 reconstruções da view pública de catálogo. A frente
  C-community foi implementada em `65d07b50a945493c08e3a79bb50a562b47a624c6` e endurecida em
  `c05d89d33f671e700d3fd1c8f485db5a1d9d9fb3`: uma view pública por scope, índices locais por
  nome, memo de layouts e invalidação fail-closed antes de DDL. A revisão encontrou e fechou antes
  da integração tanto o furo de `CREATE INDEX`/`CREATE` desconhecido quanto a mudança indevida do
  campo `operation` em erros de catálogo. O handoff `hof_d301014276c148c59c4709a2c2a87950`
  entregou o primeiro commit e os mutantes; foi cancelado após o lease expirar para que o Codex
  concluísse o follow-up sem corrida. Os cinco mutantes foram mortos, a validação independente
  passou 98/98, os checks estáticos ficaram verdes e o candidato foi publicado por fast-forward em
  `origin/milestone/grafx-mpulse7-rollout@c05d89d`. No perfil causal correto
  `continuous/per_family=1`, 12/12, o catálogo caiu de 94 chamadas/`5.591,6–5.891,2 ms` para
  12/`705,5 ms`; o RAW agregado caiu de `19.266,1–19.564,1 ms` para `17.219,4 ms` (`1,119–1,136x`)
  e o instrumentado de `40.017,2–42.218,0 ms` para `34.540,5 ms` (`1,159–1,222x`), mantendo 68
  page writes, 43 de índice, o digest lógico `61e21ccf…` e a forense `6dd6cf05…`. O relatório
  correto é `codex-main-a1b-c05d89d-continuous-pf1.json` (SHA-256 `94c692af…`). Uma primeira
  execução nomeada como C importou comprovadamente o candidato antigo `a6b74cd`; ela serve apenas
  como repetição de baseline/controle de ruído e não é evidência do patch. Na rodada oficial C
  `continuous/per_family=5`, 60/60 amostras e 12/12 famílias passaram; o RAW agregado caiu de
  `18.561,15` para `17.196,86 ms` (`1,079x`), o instrumentado de `40.072,74` para `37.073,95 ms`
  (`1,081x`) e o catálogo de 94/`5.606,59 ms` para 12/`718,69 ms`, mantendo 72/43 page/index
  writes, digest `c994255b…` e forense. O artefato de 526.344 B tem SHA-256 `655c0ec0…`; contra a
  referência Ladybug imutável, a razão caiu de `19,12x` para `17,72x`. D5 `<=10x` permanece dívida
  do plano de performance, mas não é SLO do manifesto M-PULSE-7. Como o Patch 2 aumenta a superfície
  cross-process e não é necessário para o gate congelado, nenhuma nova otimização será aberta antes
  do run completo Pulse+Grafx, que passa a ser o próximo passo finito. O plano/harness conjunto foi
  certificado no handoff
  `hof_1ad44f9d92d04f58abbcfe4a6e316ba6`; a revisão final do harness é `0cb60d5e…`. O primeiro smoke
  com a revisão anterior foi corretamente abortado na fixture de projeção e está em quarentena; a
  causa e a correção foram provadas no handoff `hof_42a66b0033f0454ca5e12b3f27668141`, e a repetição
  completa terminou 12/12 com read-back no mesmo handle. Isso
  não cria novo gate: é a correção necessária para que o gate M-PULSE-7 já congelado termine no
  limite externo de 30 segundos por operação sem enfraquecer durabilidade, recovery ou recusa de
  respostas curtas. O fechamento M-PULSE-6 está publicado em
  Pulse Community `milestone/grafx-mpulse6-assembly@d1e988a`, Pulse Core
  `milestone/grafx-mpulse6-logical-transfer-manifest@ccc1f34` e Grafx `main@1bbb839`. A regressão
  oficial congelada passou `4890 passed, 3 skipped, 13 deselected` em `8150.11 s`, sem repetir
  nenhuma das 36 falhas do run diagnóstico anterior. M-PULSE-4 foi
  aceito e promovido no Pulse Community em
  `feature/v0.3.3@d3ef4afdf263e7b6da70b6705b31950cfe07986e`. O gate final passou 56/56; o recall público
  foi `0.9546875` e a evidência direcional sem SLO dos espaços não públicos registrou build/ingest
  de `1279.6239901 s`/`1415.9925527 s` e footprint persistido de `794624 B`/`802816 B` para
  `Alternative`/`Assumption`, ambos com `verify("all")` limpo e cold reopen 10/10. Em M-PULSE-5,
  o scan público bounded-memory do Grafx foi integrado por fast-forward na `main` pelo código
  `f132190207b562eb9794e7e4d752c41c9fdb7504` e revalidado em 254/254 testes focados. O formato
  lógico, codec, fingerprint, ports e orquestração neutra do Pulse Core foram concluídos,
  revisados no Nexus
  `hof_2373b761002c42aeb519e53617e56ec8` e integrados por fast-forward em
  `okto-pulse-core/feature/v0.3.3@098a346b0988d7b39e417e7de7ed8d57d06b9795`; a validação
  independente passou 318/318, Ruff, compileall, diff-check e a auditoria de fronteiras 4/4. No
  Pulse Community, o arquivo lógico atômico foi publicado em `cb74da0f135cf8429cd9c35f399cd10ed0bd7ed6`,
  o source físico Grafx em `cb75ac60b74310a6af2de7f151286af1bfa307bd` e o sink candidato Grafx em
  `445043666530862300221783ece480e63c7086ac`; os três passaram revisão independente. As operações
  neutras streaming de backup/restore foram publicadas no milestone de integração em
  `6fe93a6e2586decc94081778f4f2bbb7425c07a0`, com revisão independente e 5/5 testes focados. A
  fundação Board/Global e os adapters Ladybug foram aceitos no handoff Nexus
  `hof_2a6e059c010049a9b218345e8db663b7` pelo código
  `b7b76acd3912041b973cc5691c946ed20d887ab6` e integrados no milestone Community em
  `c980737126118f44115fe199e17cec1ea6371c0e`; a composição passou 111/111 testes focados. As
  factories compartilhadas foram publicadas em `f73cdf4e1e5a64c48e285f514f27d16a2d913853` e
  integradas em `a4a5ec11bc20490fef7395066647a2152651291f`. A matriz final foi commitada em
  `d61f3a875952cf6fbf9bb4be177c9a98a98d83d0`, integrada e publicada no milestone
  `08e4fa71dce08d3d3a99929ed0c68a44fc260631` e promovida por fast-forward para Pulse Community
  `feature/v0.3.3@e73a446a954e039fb038f8fa329236b51104504a`. O gate congelado passou 32/32 e a
  regressão consolidada M5 passou 189/189, com Ruff, Black, compileall e diff-check verdes; a árvore
  promovida é idêntica à árvore testada. M-PULSE-6 começa somente a partir desse fechamento.
  A integração corrente de M-PULSE-6 está em
  `milestone/grafx-mpulse6-integration@c524813` (publicada como milestone, ainda não promovida): bindings
  imutáveis Board/Global, providers operacionais Board/Global, Cypher, transaction, schema,
  lifecycle, recovery/restore, erasure física, inspeção non-opening e pool Grafx compartilhado já
  foram integrados em pacotes revisados. O hardening Windows recusa symlink/junction/reparse point
  estático sob a boundary de permissões do processo/filesystem e fences cooperativas; não declara
  proteção absoluta contra um ator externo que troque paths concorrentemente. O privacy erase
  remove o binding por último, não tenta reacquirir essa autoridade depois do unlink terminal e
  confirma a ausência física antes de emitir o recibo. A janela pública de operação de board usa o
  mesmo guard de close/mutação, e os leitores canônicos de Bug e a preparação de rebuild deixaram
  de instanciar/probar Ladybug diretamente: ambos resolvem/delegam pelo registry composto. O gate
  focado desse lote passou 41/41 e a janela passou validação independente 6/6. O resolver imutável
  Board/Global agora também recusa resíduos Ladybug antes de criar/adotar Grafx e valida aliases,
  inclusive junctions Windows reais em Python 3.11, antes de `mkdir` ou `FileLock`. O pool ganhou
  limite opcional, eviction LRU somente de handles não pinados, leases idempotentes e fechamento
  fail-closed; o provider transacional mantém o pin até o engine confirmar `active=False`, sem
  transformar uma falha de release posterior a commit durável em commit retryable. No SHA integrado,
  resolver/foundation/pool passaram 103/103 e os finais transacionais discriminantes 15/15; as
  revisões independentes repetiram 52 testes de routing com junctions reais e 51+16 testes de
  pool/transação. O quarto checkpoint acrescentou facades Board explícitas e a transação roteada,
  quarantine/restore da geração Grafx completa, recusa do recovery offline em board Grafx e o
  diagnóstico backend-neutral do `okto-pulse init`. O restore só aceita retries `done` ou em
  reconciliação quando o inventário terminal autenticado é exatamente igual ao live. A validação
  integrada desse lote passou 127/127; a execução ampla adicional passou 272 testes e teve dois
  failures reproduzidos no baseline do harness — Core legado hardcoded num subprocesso e ausência
  do extra de desenvolvimento `build` — antes de alcançar o delta M-PULSE-6. As revisões
  independentes passaram 23 testes de diretório + 17 de WAL, 13 focados + 172 de regressão da
  transação e 44/44 do `init`. Permanece registrado para o fechamento M-PULSE-6/auditoria 0.0.1 um
  residual MEDIUM: crash depois do rename `pending -> final` do snapshot de quarantine e antes do
  retorno pode perder o recibo normal de purge no retry; os bytes permanecem seguros, mas a API
  ainda não possui chave de idempotência ponta a ponta. O quinto checkpoint acrescentou o
  lifecycle Board roteado, com uma única aquisição de snapshot por operação, callbacks físicos
  `*_unguarded`, revalidação da fence do writer antes de mutações e nenhuma inicialização implícita;
  a regressão independente desse pacote passou 130/130. Esse era o estado do quinto checkpoint.
  O fechamento posterior, iniciado em `milestone/grafx-mpulse6-assembly@09d46ae`, foi completado e
  publicado em `milestone/grafx-mpulse6-assembly@d1e988a`, com Core
  `milestone/grafx-mpulse6-logical-transfer-manifest@ccc1f34` e engine Grafx
  `main@1bbb839`: composição integral Board+Global, startup, CLI, restore, shutdown, rebuild,
  provenance, conformance diferencial, wheel instalado e recovery offline backend-neutral estão
  implementados e revisados. A cobertura de interface confirmou 91/91 métodos nas 11 superfícies; a
  regressão roteada passou 232/232; o mesmo fluxo Core-facing passou com os bundles Ladybug e
  Grafx reais; o smoke do wheel candidato `0.0.1` passou em venv isolado com `[accel]`,
  `uv pip check`, bindings Board/Global e catálogos 81/11. O recovery-only passou 220/220 casos
  aplicáveis e uma reauditoria fechou os blockers de compensação binding-aware Ladybug/Grafx e dos
  sidecars `.wal`, `.shadow` e `.wal.checkpoint`. F13/AF21 passou 25/25, o gate curto
  foundation/provenance/bypass passou 32/32 e o Pulse Core permanece com zero import de Grafx. A
  regressão longa final passou `4890/4890`, com três skips esperados, 13 deselected e cinco warnings
  conhecidos; o JUnit tem SHA-256
  `d4d5bfc33cb9aa5d22a03e8f078dbd5df97dc94f6a7821a89b15737a33f14498`. M-PULSE-6 está
  certificado e autoriza o início de M-PULSE-7, mas ainda não constitui release `0.0.1`.
  O gap factual do inventário F13 também foi fechado no milestone do Core
  `milestone/grafx-mpulse6-logical-transfer-manifest@8d2dbcb`: o pacote público
  `okto_pulse.core.kg.logical_transfer` passou a constar no manifesto, com 311/311 testes, e o
  handshake Community em `c524813` eliminou os 230 bridges antes reportados; os dois testes de
  auditoria cruzada passaram 2/2.
  A dependência candidata do Pulse foi fixada como `okto-grafx[accel]==0.0.1`; o extra
  `accel` instala NumPy e CRC-32C nativo, sem mudar a semântica pública do engine.
  M-PULSE-0 foi concluído em
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
  Lineage, active-set, preservação exata durante cleanup e a superfície estruturada completa do
  `GraphTransactionScope` foram integrados no Pulse Community
  `feature/v0.3.3@befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595` (código em `36c2fc6`,
  documentação de fechamento em `befaf1e`) e no Pulse Core
  `feature/v0.3.3@ab61b9a785f2018312fc91541a580877fd068bbb`. O gate final passou com
  Grafx 123/123, Kuzu/Spec 8/8, Core 61/61 e auditoria independente sem blocker. M-PULSE-1 está
  concluído; o `execute()` genérico foi fechado em M-PULSE-2A..O e a compatibilidade total
  permanece aberta nos M-PULSE-3 a 7.
  O corpus fail-closed M-PULSE-2A foi congelado em
  `milestone/pulse-query-corpus@5b05b4f`: 97 entradas, 68 famílias internas, 28 templates
  públicos sobre 27 textos, 87 probes e 36 behaviours, com digest integral `54c3d757...` e zero
  `runtime_fragment`. O gate final passou 45/45, o rebuild ficou em 25,6 s e as revisões
  independente e Nexus `hof_ac705be83fb845ba8193c8f76a477ac8` concluíram sem blocker. Em
  M-PULSE-2B, `coalesce`, `string_split` e `size` foram publicados na
  `main@af2e21e678e60eb135967169598f82549157c504` após auditorias local e Nexus
  `hof_e224557b3aba4ef8baed3bddc1c27de1` PASS. O corpus passou a digest `5ad93542...`,
  com 61/26 probes aceitos/recusados e 18 débitos públicos. M-PULSE-2C acrescentou `CASE`
  searched/simple, subscrito de listas e acesso composto a parâmetros no código final
  `milestone/pulse-query-case-subscript@bf88462`; o corpus combinado tem digest `d9095a0f...`,
  64/23 probes aceitos/recusados e 15 débitos públicos. A revisão Nexus
  `hof_e3b5f54f3e2344949286b9768456df3e` encontrou duas composições defeituosas, fechadas
  sem ampliar a linguagem por `045a8c2` e `bf88462`, com os handoffs
  `hof_a1b17d76f49940dea1d0b00b609b985a` e
  `hof_01b133a255454f4c8ed84026093e8d10`. O gate agrupado final manteve a suíte query,
  o corpus, as fronteiras públicas, Ruff e diff-check verdes. M-PULSE-2D acrescentou
  `label` e `timestamp` no código final `milestone/pulse-query-label-timestamp@a1846c3`, incluindo
  validação pré-stream de composições conhecíveis sem antecipar agregados. O corpus passou ao
  digest `75622dfe...`, com 66/21 probes aceitos/recusados e 13 débitos públicos; a suíte query
  final passou 996/996, as fronteiras públicas selecionadas 147/147 e o corpus 45/45. A auditoria
  independente final passou sem blocker; a revisão cruzada Nexus
  `hof_7b8a913b11644a4c98ee129ce7ae2b85` foi concluída e verificada/PASS antes da publicação.
  M-PULSE-2E acrescentou `UNWIND` como source streaming no código `4fc8ca5`, limitado às formas
  públicas `UNWIND ... RETURN` e internas I67/I68 com um `MATCH` correlacionado e um `SET`.
  Carrier, resolução tipada, atomicidade, budget contado uma vez, seek por PK e fallbacks
  dirty/stale têm regressões. O corpus passou ao digest `836d55ad...`, raw 69/18, dez débitos
  públicos e entries 71/24. A suíte query final passou 1.019/1.019; o gate focado final somou
  68/68 em `test_unwind.py` + corpus; Ruff, diff-check e fronteiras públicas passaram. A auditoria
  independente e a revisão Nexus `hof_9e5978356c68454ca7303e13b62b61c0` foram PASS. Em
  M-PULSE-2F, `WITH` não agregante foi concluído no código final `5b32e8e`: o probe público e as
  mutations I06/I07 executam projeções sequenciais com troca real de escopo, `WHERE` por estágio,
  snapshot pré-`SET`, piso zero, fallback nulo e atomicidade. O corpus passou ao digest
  `33254881effa...`, raw 70/17, nove débitos e entries 73/22. A suíte query final passou
  1.060/1.060, corpus 45/45, fronteiras públicas selecionadas e Ruff/diff-check passaram, sem
  aumentar a dívida de formatter (250 arquivos na base e no candidato). Duas auditorias
  independentes e o handoff Nexus `hof_89b6fa0a5aa243d7b314b71a72782ebc` concluíram/PASS após
  corrigir compatibilidade posicional, bypass por AST e reutilização cíclica de alias. Em
  M-PULSE-2G, `MATCH (n)` polimórfico read-only foi concluído no código
  `6e4f1d5e91878395f9736a95b855296f69e2e248`: um único `AllNodesScan` une somente node tables,
  preserva a visão owner-only, aplica filtro/agregação/ordem/janela globalmente e destaca o node
  público como `{label, properties}`, sem identidade interna. Propriedade ausente lê `NULL` e
  tipos homônimos incompatíveis recusam antes do stream. O corpus passou ao digest
  `ac19e6735a90e5fe9831fdca67a80de1a3f4fffadd54b81151d9e343a7bd0d7a`, raw 71/16 com oito
  débitos e entries 80/15; exatamente um probe e sete entries mudaram, enquanto I01/I02 e todos
  os demais gaps permaneceram integralmente iguais. A suíte query final passou 1.088/1.088, o
  lote combinado dedicado+corpus 73/73 e as fronteiras públicas selecionadas 158/158; três
  auditorias independentes, corpus `--check`, Ruff e diff-check passaram, e o handoff Nexus
  `hof_a28be7ad2f584fd693c3ef9cbaec75e8` foi concluído/verificado/PASS, sem aumentar a dívida
  de formatter (250 arquivos na base e no candidato). Os oito constructs restantes permanecem
  congelados para os sublotes seguintes do próprio M-PULSE-2. Em M-PULSE-2H, a inferência de
  endpoints exclusivamente para I01/I02 na forma read-only `MATCH (a)-[r:TYPE]->(b)` foi concluída
  nos commits `a516c64` e `42078ca`. O planner resolve a relação primeiro, infere o source por
  `from_table` e mantém o target em `to_table`, reutilizando `NodeScan -> TraverseRelationship`.
  O corpus ficou no digest `2fec52e0f033c3674aa8558fc5cca4aec05dacc7eae82e111bc2819864873e36`:
  raw permaneceu 71/16 com oito débitos, somente I01/I02 mudaram e entries passou a 82/13. Query
  passou 1.141/1.141, corpus 45/45, gate focado pós-formatação 388/388 e fronteiras públicas
  selecionadas 158/158. Três auditorias independentes e o handoff Nexus
  `hof_8ceb3eb19622472bbd50a83c81921015` concluíram/PASS; corpus `--check`, Ruff e diff-check
  passaram, sem aumentar a dívida de formatter (250 arquivos na base e no candidato). Incoming,
  undirected, relação sem tipo, ranges escritos, `OPTIONAL`, `WITH`, writes, paths e multi-endpoint
  permanecem fora.
  M-PULSE-2I concluiu o named path decorativo, não projetado e não lido, sobre um único hop
  tipado, nos commits `1c728cc` e `edf6efd`, publicados no branch
  `milestone/pulse-query-named-path` e integrados à `main`. O corpus fechou exatamente em raw
  72/15 com sete débitos e 97 entries em 82/13, digest
  `e792ded751eeffbe597a4e37d9110b30943e3d0fa69bd22027ad09778fc24f1c`; somente os dois objetos
  congelados mudaram. Duas auditorias independentes e o handoff Nexus
  `hof_a3e5645c9d4b437bbf75841a20872c13` concluíram/PASS. M-PULSE-3B também foi concluído e
  publicado no Pulse Community `feature/v0.3.3@c4b1f37ad3a4cd08a1e2f5249db25c33ddbecd45`:
  o adapter materializa o manifesto fechado de 16 tipos/69 pares em tabelas físicas distintas,
  sem alterar o formato Grafx, executar DDL, ativar o provider ou reclassificar o corpus.
  Em paralelo, a primeira capacidade M-PULSE-3A de propriedades de node foi integrada no Pulse
  Community `feature/v0.3.3@4aae27eca9c0a2d1d14a3334b03e6c57976dea75`, ainda inativa até a
  composição do provider completo.
  M-PULSE-2J a 2L fecharam, respectivamente, upper bound implícito, `OPTIONAL MATCH` root e a
  convergência da autoridade pública. M-PULSE-2M foi concluído no código
  `e71672f5445bed5ce40066e4a1e286ad5ad9fbe1` e integrado por merge em
  `main@e90ea642f28261589ba47045cfc82d7f53faceb2`: exatamente duas branches read-only com
  `RETURN`, nomes da esquerda, coerção tipada antes de um distinct global e um único
  bind/transação/snapshot/context. O corpus fechou em 97 entries, engine raw 77/10, dois débitos e
  digest `8963b64ab073d13f84d216cd58ce7f1c683c74b1a1e8dfdcd4897c3c6fd8002f`; somente o probe
  `UNION` mudou. O handoff Nexus `hof_14d5d5fd05fc42efbb1746995ec0c9f0` foi
  concluído/verificado/PASS, assim como três auditorias independentes. M-PULSE-2N foi concluído
  no código `8a917395092b7d9c593460f63c2e1ea91e7f64cd` e integrado por merge em
  `main@21060edbf3682ae92d76cd7e5c37e6775e14f0d0`: admite somente
  `MATCH (a:Decision)-[r]->(b) RETURN a.id`, enumera todas as tabelas relacionais compatíveis por
  `table_id` em um único operador/contexto/snapshot e preserva multiplicidade. O corpus fechou em
  97 entries, engine raw 78/9, um único débito (`path projection`) e digest
  `905aa0baeb0d78d61858a1d4b5dc2da44003b966a065cbae2994340f9e83bb1a`; somente o probe raw
  `untyped relationship` mudou. O gate congelado passou em 71 testes dedicados, 46 de corpus e
  1.559 de query/fronteira pública, além de Ruff e diff-check; três auditorias independentes e o
  handoff Nexus `hof_9698ae9ce6574df881871be6bf98ede4` concluíram/PASS. M-PULSE-2O foi concluído
  no código final `9118b7115329a3d9218b7a5532c461ab0d2ccdc4`, publicado no branch
  `milestone/pulse-query-path-projection` e integrado por merge em
  `main@7248caf6747cab82f4c0da173abe36b94a7860a7`. O oracle Ladybug/Kuzu foi congelado antes do
  código em `9458d6f00cc6a6cfcd5bc718fc58eeb45382786d` e verificado no handoff Nexus
  `hof_baca7724b827446ebc78c4a1f27758ab`. O gate admite somente a forma literal de 9.7, fecha o
  corpus em 97 entries, raw 79/8, 73/14 no contrato, dívida zero e digest
  `b29334edf6e7c1e6b9419a4f3add84ede4baad94fdeaecb0c679261a78f241cc`. O candidato funcional
  passou 1.950 regressões antes do hardening final; o SHA final passou 140 testes dedicados, 600
  regressões relacionadas e o gate independente Claude de 187/187, além de corpus `--check`,
  Ruff, format, compileall e diff-check. A primeira revisão Nexus
  `hof_99fa41ce71fe49f6b0a5830d16826cfc` encontrou uma colisão tardia de chaves estruturais; ela
  foi corrigida como recusa tipada pré-stream e o delta final foi concluído/verificado/PASS em
  `hof_0ddbaa8ea5214a3a9ada8163c98ddbd4`, sem ampliar a gramática. M-PULSE-3D foi concluído no
  código final `237f3bf7fa2a65a108db4f558932d429ec6696ce`, publicado tanto em
  `milestone/grafx-mpulse3-schema-evolution-impl` quanto em `feature/v0.3.3`; seu contrato fora do
  lugar havia sido congelado antes do código em
  `milestone/grafx-mpulse3-schema-evolution@703ad83c43b286e7c90fe2b0a29de0982929d4de`. O próximo
  alvo já existente é M-PULSE-4, sem nova designação nem alteração retroativa dos gates concluídos.
  O registro verificável está em 9.7.

## Governança dos roadmaps complementares pós-Pulse

Os dois documentos abaixo ficam incorporados por referência, em sua íntegra, a este plano. Eles
devem permanecer versionados no repositório; a referência inclui todos os capítulos, decisões,
contratos, APIs propostas, milestones, gates, matrizes de teste, instruções de execução, itens
adiados e non-goals — não apenas seus resumos executivos:

- [`GRAFX_COMPLEMENTARY_EVOLUTION_PLAN_CODEX.md`](GRAFX_COMPLEMENTARY_EVOLUTION_PLAN_CODEX.md):
  trilha database-first `GX-CAP-0` a `GX-CAP-11` e integrações opcionais `GX-AGENT-0/1`;
- [`AGENT_FIRST_EVOLUTION_PLAN_CODEX.md`](AGENT_FIRST_EVOLUTION_PLAN_CODEX.md): detalhamento
  obrigatório da camada agent-first `AGENT-0` a `AGENT-8`, incluindo workspace/scopes,
  identidade, proveniência, idempotência, memory/claims/evidence, MCP, policies, segurança,
  observabilidade e conformance.

Esta incorporação obedece às seguintes regras vinculantes:

1. primeiro são fechados e publicados os gaps de integração com o Pulse, incluindo os gates
   M-PULSE-2 a M-PULSE-7; em seguida ocorre o run integrado real, a auditoria completa e a
   publicação conjunta do Grafx `0.0.1` no PyPI. Nenhum item dos roadmaps complementares amplia
   retroativamente um sublote Pulse já congelado;
2. somente depois de `0.0.1` estar publicado e reinstalado com sucesso a partir do PyPI, o roadmap
   técnico deste documento continua como linha `0.0.2` e autoridade para
   integridade, recovery, concorrência, performance, lifecycle, backup/migração e capacidades já
   planejadas; os dois anexos entram como backlog obrigatório adicional, respeitando suas
   dependências explícitas;
3. em capacidades sobrepostas, o plano database-first define ownership e ordem no core, enquanto
   o plano agent-first preserva os requisitos detalhados e os gates da camada opcional. Deve ser
   atendido o conjunto compatível mais estrito; nenhum requisito exclusivo de qualquer um dos
   arquivos pode ser descartado silenciosamente;
4. a declaração interna do plano database-first de que ele substitui a proposta isolada
   agent-first não retira o segundo documento do roadmap: por decisão posterior registrada aqui,
   ambos permanecem autoridades. Ela vale apenas para evitar que a semântica agentic contamine o
   core ou duplique uma capability genérica;
5. conflito material entre os anexos exige ADR/decisão explícita antes do código. Não é permitido
   resolver conflito por omissão, reinterpretar um gate depois da implementação ou elevar a barra
   exploratória de um milestone em andamento;
6. o registro de execução de 9.7 deve mapear cada milestone pós-Pulse ao documento e seção de
   origem, SHA imutável, testes, auditoria e débitos aceitos. Alterações futuras nesses anexos são
   diffs versionados e não movem retroativamente um gate já congelado.

Assim, manter os documentos separados não perde informação: o texto integral de ambos é parte
normativa deste plano, enquanto esta seção fixa precedência, momento de execução e resolução de
sobreposição sem criar uma terceira cópia divergente.

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

### P2.10 — Mesma marca certificada não implica mesma figura HNSW

A abertura fria constrói a figura em ordem canônica: `_build` insere
`sorted(walk(), key=(born_csn, ref.encode()))` em
[`vector_engine.py`](src/okto_grafx/engine/vector_engine.py#L1041). O caminho quente **não**
reconstrói: `commit` ([`vector_engine.py`](src/okto_grafx/engine/vector_engine.py#L1230)) e `apply`
([`vector_engine.py`](src/okto_grafx/engine/vector_engine.py#L1260)) chamam `_note` por mudança
encenada, inserindo na figura **viva em ordem de encenação**, e só então `_certify` avança a marca.

Como o HNSW é sensível à ordem de inserção, **dois handles na mesma marca certificada podem manter
grafos diferentes** e responder rankings diferentes para a mesma consulta no mesmo snapshot. Isso
já é verdade hoje, sem parametrizar nada, e é a premissa que mata a opção C de P1.16.

Não é necessariamente defeito. O docstring de `HnswGraph` promete determinismo para "a mesma
semente e as mesmas inserções", e "as mesmas inserções" inclui a ordem, portanto é literalmente
correto; e o contrato já recusa prometer recall para uma consulta individual. **A lacuna é de
documentação no nível do engine**, e é ali que precisa ser escrita: mesma marca e mesmo conjunto
não implicam mesma figura.

Distinto de P1.2: lá o problema é frescor, um processo que não percebe que outro marcou o índice
stale. Aqui nada está stale — as duas figuras estão corretas e atualizadas, e mesmo assim divergem.

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

**Estado em 2026-08-30:** a frente B implementou e publicou o primeiro lote sem enfraquecer a
contenção contra redirects. `list_files(prefix)` passa a descer somente pelo prefixo solicitado, o
hit quente deixa de repetir `realpath` e cada diretório listado é fixado e revalidado imediatamente
antes/depois do `scandir`. A corrida com junction NTFS foi reproduzida e recusada antes do aceite.
No micro-trace causal `reopen/per_family=1`, B completou 12/12 com mesmo digest lógico, imagem
forense e WAL do baseline; o RAW agregado caiu de `97.623,1 ms` para `38.732,6 ms` (`2,52x`). Na
composição A1+B definitiva em `main@91a59c6`, `continuous/per_family=1`, o custo instrumentado
acumulado de `list_files` caiu de `21.798,5 ms` no A1 isolado para `195,3 ms` (`111,62x`). Os
contadores públicos e os demais itens desta seção continuam como evoluções separadas, não como
condições retroativas para M-PULSE-7.

Na rodada `continuous/per_family=5`, o mesmo candidato registrou `211,4 ms` acumulados em
`list_files`, confirmando que a varredura global foi eliminada. O residual dominante do storage é
agora `_still_names`/revalidação de descriptors (`7.713,3 ms` inclusivos, com sobreposição de
`lstat/stat/fstat`); a classificação de namespaces do Patch 2 permanece condicionada à prova
cross-process já prevista. C-community reduziu o catálogo de 94 para 12 snapshots tanto no perfil
curto quanto no oficial; a rodada `per_family=5` melhorou o RAW agregado em `1,079x` e reduziu a
razão Grafx/Ladybug para `17,72x`, ainda fora de D5 `<=10x`. O residual fica explicitamente aberto
no plano de performance, mas o Patch 2 não é promovido antes do run completo M-PULSE-7: não existe
SLO no manifesto e ampliar agora a superfície cross-process transformaria um débito medido em alvo
móvel do gate de compatibilidade.

### P1.11 — Heap page 0 elimina concorrência efetiva entre writers

Todo insert avança `next_record_id`, fazendo writers disjuntos conflitarem na página 0. A medição oficial registra 10,9 rows/s, 254 conflitos e caudas de segundos em [`PERFORMANCE.md`](docs/PERFORMANCE.md#L62).

O registro W6 recomenda corretamente identity-range leasing em [`W6-WRITE-CEILING.md`](docs/architecture/W6-WRITE-CEILING.md#L19).

**Estado em 2026-08-31:** CN-1 foi implementado, auditado e publicado em `40b2b43`, com
`identity_lease_size=64` parametrizável e fallback operacional `1`, preservando heap/WAL v1 e
multi-writer/multi-reader. Cada refill reserva e avança duravelmente o piso da extensão por uma
transação interna COW normal antes de qualquer ID ser entregue; o participante consome localmente o
intervalo de forma monotônica/burn-only e nunca devolve IDs após conflito, aborto, crash, close ou
fork. Refill stale perde por OCC e reconstrói a partir da page 0 fresca. Commits que consomem um ID
já reservado deixam de declarar page 0 sem tê-la modificado; refill e crescimento de tail continuam
declarando-a. IDs explícitos abaixo do piso durável são recusados enquanto leasing estiver ativo, e
IDs explícitos acima do piso exigem avanço durável antes do uso.

A repetição autenticada de `RAW/same-10` no candidato `6fd26f9` mediu o efeito: page 0 caiu de
partição fatal para uma ocorrência em 807 commits de B, e A avançou de 19 para 46 operações. O cold
verifier permaneceu limpo. O gate ainda não passou porque a cauda de dados da mesma tabela se tornou
o blocker seguinte (`heap.dat/394` terminal; páginas 350--353 nos retries de B). Isso já era a
limitação prevista para writers na mesma tabela depois de remover a identidade compartilhada; não é
um novo requisito nem evidência de corrupção. O próximo passo está limitado à correção dessa colisão
física, sem elevar retries, mudar workload ou executar a matriz oficial antes de `same-10` passar.

Sequência recomendada:

1. leasing de blocos de IDs por participante;
2. medir novamente conflitos e fairness;
3. corrigir publicação de control files no Windows;
4. implementar group commit;
5. considerar páginas por tabela apenas se o caso multi-tenant justificar;
6. evitar redo lógico/mergeável até haver necessidade demonstrada.

### P1.12 — Hash indexes têm escala fixa

**Parcial estrutural concluída em 0.0.2:** índices automáticos exatos recém-materializados podem
ser dimensionados por `connect(..., automatic_index_expected_cardinality=N)`. A dica vale por
índice, é persistida na nova geração v2 e não redimensiona artefatos existentes. PK e `ef_`/`et_`
usam o mesmo número porque cada um contém uma entrada por linha; o índice `record_id` usa a dica
somente como piso sobre `max(4096, 2 * visible_rows)`. Um catálogo vazio e gravável é ativado em
v2 no open; catálogo v1 não vazio mantém migração explícita e recusa novo DDL em vez de ignorar a
dica. `None` preserva 64 buckets.

Índices automáticos recebem 64 buckets por padrão em [`keys.py`](src/okto_grafx/domain/index/keys.py#L49). O lookup percorre toda a cadeia daquele bucket em [`index_manager.py`](src/okto_grafx/engine/index_manager.py#L942).

São necessários:

- `expected_cardinality` ou `bucket_count`;
- rehash/rebuild online;
- eventualmente hash extensível;
- `CREATE INDEX`;
- índices compostos;
- B+tree para range, prefix e `ORDER BY`.


Ressalva medida na rodada de escala de 2026-09-04, a considerar antes de aumentar buckets por
padrão: `walk()` custa O(buckets + entradas) e **piora com diretório esparso** — 73,5 ms com 1.024
buckets para apenas 1.000 entradas. `walk()` é o caminho de `verify('indexes')`, de `reconcile` e
da reconstrução, então dimensionar por cardinalidade esperada precisa vir junto de um percurso que
não pague pelos buckets vazios. O `CREATE INDEX` já aceita as duas opções de dimensionamento
([`parser.py`](src/okto_grafx/domain/query/parser.py#L384)) e o valor é persistido. Os índices de
identidade v2 também são uma exceção já entregue: `identity_index_sizing(visible_rows)` escolhe o
diretório a partir da cardinalidade cercada. O gap de configuração dos índices automáticos foi
fechado sem alterar `TableDef` ou a projeção lógica de `automatic_index_definitions`: o sizing é
aplicado somente quando QueryEngine/TransactionManager planejam uma nova geração física. Continuam
como evoluções separadas o crescimento automático/rehash sob carga e um diretório extensível ou
esparso; não são pré-condição para usar a dica entregue.

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

### P1.16 — Os três parâmetros de construção do HNSW são inalcançáveis

Levantamento de 2026-09-04 (base `de24b7b`, reconferido em `22d9694`). `DEFAULT_NEIGHBOURS = 16` e
`DEFAULT_EF_CONSTRUCTION = 200` estão em [`hnsw.py`](src/okto_grafx/domain/vector/hnsw.py#L61) e
[`hnsw.py`](src/okto_grafx/domain/vector/hnsw.py#L70); `DEFAULT_INDEX_SEED` está em
[`vector_engine.py`](src/okto_grafx/engine/vector_engine.py#L154). O `VectorEngine` aceita os três
na assinatura ([`vector_engine.py`](src/okto_grafx/engine/vector_engine.py#L583)), mas
[`assembly.py`](src/okto_grafx/api/assembly.py#L378) nunca os passa: são alcançados por omissão e
não existe caminho público para nenhum deles. Ao lado, `vector_ef_search` e
`vector_exact_scan_threshold` são configuráveis em
[`config.py`](src/okto_grafx/runtime/config.py#L290).

Quatro desenhos foram avaliados com adversário dedicado e os quatro morreram:

| Opção | Por que não sobrevive |
|---|---|
| A — campo imutável no catálogo, com bit de capacidade | O default de compatibilidade referencia a própria constante, então todo espaço já existente continua preso a ela; e o bit só existe em catálogo v2, enquanto um banco nasce em v1. Só se justifica como pré-requisito de **persistir o grafo**, não como botão. |
| B — derivar de campos já presentes no catálogo | Não dá alavanca ao usuário e transforma a fórmula em parte do build: duas versões do Grafx divergiriam sobre o mesmo banco sem bit de capacidade que detecte. |
| C — configuração por processo com prova de acordo | O conjunto de entradas **não é função** de `built_through_lsn` (ver P2.10), então a prova certificaria a proposição errada. Nenhum dos três mecanismos existentes — registro de leitores, lease de escritor, digest de cabeçalho — serve. |
| D — heurística na criação, gravada uma vez | No `CREATE` a cardinalidade é zero, e o espaço existe antes de qualquer tabela declarar coluna nele. A metade que resolve é a opção A; a metade exclusiva de D quebra a compatibilidade preguiçosa. |

**Recomendação registrada.** Expor `ef_construction` e `neighbours` como controle **operacional do
processo**, exatamente como `vector_ef_search` já é exposto — o que a §7 deste plano já prevê na
linha `VectorOptions` —, acrescentando teto superior (hoje a validação só exige `>= 1`) e mantendo
o `HNSW_FROZEN` do arnês de recall independente da configuração pública. Custo de formato: zero.
O fundamento é duplo: o contrato já recusa prometer recall por consulta
([`CONTRACT.md`](docs/architecture/CONTRACT.md#L576)), e a concordância entre processos que
justificaria travar o valor **já não existe** na forma que se supunha (P2.10).

**Condição de entrada acordada com o Codex, antes de expor qualquer botão:** perfil completo de
recall em 8.192 × 384 medido nos dois valores candidatos e recongelamento deliberado do
`HNSW_FROZEN` em `bench/harness/recall_worker.py`. A divergência entre as duas posições é de
**sequência**, não de mérito: um parâmetro exposto sem perfil convida a ser girado, e o custo cai
em recall, que é o que menos se percebe quando degrada.

**Retirado por medição cega, não repropor sem experimento novo:** a medição que sugeria
`ef_construction = 64` equivalente a 200 (recall@10 de 0,915 contra 0,910) usou 2.000 vetores
uniformes em dimensão 64, faixa em que a travessia visita 97,6% do grafo. Com a busca praticamente
exaustiva a qualidade das vizinhanças não influencia o recall, então o experimento estava cego
para o efeito que deveria medir.

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


A linha `VectorOptions` carrega hoje um item bloqueado: `neighbours` e `ef_construction` não têm
caminho público nenhum, e a decisão de expô-los está registrada em P1.16 com condição de entrada
explícita (perfil de recall 8.192 × 384 antes do botão). A semente do índice fica fora dessa lista:
mudá-la muda a figura, e P2.10 mostra que a figura já não é única entre processos.

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

**Compatibilidade analisada em:** 2026-08-26

**Baseline funcional observado:**

- Okto Grafx `main@4bcd6034a235f86dfd26bc31df0fac848dccc267`;
- Okto Pulse Community `feature/v0.3.3@befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595`;
- Okto Pulse Core `feature/v0.3.3@ab61b9a785f2018312fc91541a580877fd068bbb`;
- contrato público de consulta `KG_QUERY_CONTRACT_VERSION = "1.0"`, em
  `../okto-pulse-core/src/okto_pulse/core/kg/query_contract.py`;
- schema de board `SCHEMA_VERSION = "0.5.0"` e Global Discovery
  `GLOBAL_SCHEMA_VERSION = "0.1.2"`.

Os hashes acima formam o baseline limpo e reproduzível que fechou M-PULSE-1. Os dois worktrees
originais do Pulse continuam com suas alterações locais preservadas, mas não participam dos gates:
as integrações, testes e pushes foram executados em worktrees dedicados e limpos. Esse fechamento
não antecipa M-PULSE-2 a 7 nem declara compatibilidade total.

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
| CRUD básico de nós/arestas | Overlay owner-only combinado de nós e relações concluído, incluindo insert/update/delete staged | superfície estruturada, tombstone, lineage e active-set integrados em `36c2fc6`; provider ainda não composto até o bundle integral do M-PULSE-6 | P0 |
| `DELETE`/`DETACH DELETE` | relationship delete e detach físico cobrem estado committed e cancelamento de relações staged de statements anteriores | falta mapear a exclusão destrutiva do port Pulse sempre para essa primitive | P0 |
| Transação | commit/rollback, read-your-own-writes combinado, resolução pré-write de endpoints, crash all-or-none e `execute()` genérico do corpus Pulse concluídos no engine | superfície estruturada completa do port e compensações integradas em Community `36c2fc6`/Core `ab61b9a`; composição do provider permanece deliberadamente no bundle integral M-PULSE-6 | P0 |
| Substituição de payload | um `MATCH ... SET` único já substitui o payload e preserva identidade/arestas sob isolamento, rollback, conflito e reopen | wrapper Grafx e contratos Core/Kuzu integrados no bundle final `befaf1e`/`ab61b9a`; provider permanece deliberadamente inativo até o bundle integral do M-PULSE-6 | P1 |
| Cypher read-only 1.0 | escalares, `CASE`, subscritos, `label`, `timestamp`, `UNWIND`, `WITH` não agregante, `MATCH (n)` polimórfico, endpoint inference tipada de I01/I02, named path decorativo, upper bound implícito, `OPTIONAL MATCH` root, convergência da autoridade pública, `UNION` binário, relação sem tipo de um hop e projeção exata de path concluídos até M-PULSE-2O | nenhuma consulta do corpus raw permitida pelo contrato permanece recusada pelo engine; rewrite de camada/limite, envelope e tuple→list pertencem ao provider M-PULSE-6 | P1 |
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
  `unsafe_cypher`/`unsupported_operation`, `auto-LIMIT`, auto-bound em 20 apenas quando o upper
  bound está ausente, rewrite de layer canônica, `execute_read_only_pair`,
  columns/row_count/truncation e contagem de linhas omitidas. O pin atual admite e preserva um
  limite explícito como `*1..21`; M-PULSE-2 não pode assumir que ele foi clampado e deve manter a
  recusa canônica `canonical_filter_unenforceable` ou versionar outra política antes de executar.

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

**Estado em 2026-08-26:** concluído no Grafx `main@4bcd6034a235f86dfd26bc31df0fac848dccc267`,
Pulse Community `feature/v0.3.3@befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595` e Pulse Core
`feature/v0.3.3@ab61b9a785f2018312fc91541a580877fd068bbb`. Fundação, overlays
owner-only de nós e relações, resolução pré-write de endpoints, cancelamento staged, guardas OCC,
substituição integral de payload, tombstone, lineage, active-set e compensação/cleanup exatos estão
fechados. O `execute()` genérico então deliberadamente reservado para M-PULSE-2 foi concluído nos
sublotes M-PULSE-2A..O; a composição do provider continua no M-PULSE-6.

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

**Estado em 2026-08-26:** o sublote M-PULSE-2A de corpus está concluído em
`milestone/pulse-query-corpus@5b05b4f`. O descriptor `pulse-1` fixa os baselines Community
`befaf1e4...` e Core `ab61b9a...`, as 68 famílias internas e a superfície pública 1.0. O corpus
possui oráculos estruturados de erro, tipo, nulidade, cardinalidade, ordenação e efeito; o digest
cobre o payload inteiro. M-PULSE-2B foi publicado na `main@af2e21e`: `coalesce`,
`string_split` e `size` reduziram a dívida pública de 21 para 18 constructs, com digest
`5ad93542...`. M-PULSE-2C está congelado no código final `bf88462`: `CASE`
searched/simple e subscritos de lista reduziram a dívida a 15 constructs, com digest
`d9095a0f...`; `map access` permanece
corretamente devido porque seu probe público depende do `UNWIND` ainda ausente. M-PULSE-2D está
concluído e publicado com o código final `a1846c3`: `label` e `timestamp` eliminaram os dois últimos
débitos de função, com digest `75622dfe...`, raw 66/21 e 13 débitos públicos. A revisão Nexus
`hof_7b8a913b11644a4c98ee129ce7ae2b85` foi concluída/PASS. M-PULSE-2E está concluído no código
`4fc8ca5`: `UNWIND` leading, acesso a mapas, execução streaming e lookup correlacionado cobrem
I67/I68 sem liberar escrita parcial em erro tardio ou budget. O corpus ficou no digest
`836d55ad41bb617f3e71cf788ca164b0eca9c038964c63ec6542e87184e6f1e9`, raw 69/18, dez débitos
públicos e entries 71/24; a revisão Nexus `hof_9e5978356c68454ca7303e13b62b61c0` foi concluída,
verificada e PASS. M-PULSE-2F está concluído no código final `5b32e8e`: `WITH` não agregante cobre
o probe público `WITH 1 AS value RETURN value` e as mutations I06/I07 com duas projeções
sequenciais e `WHERE` pós-projeção. A troca real de escopo, o snapshot pré-`SET`, o piso zero e o
fallback de restauração nulo têm regressões; AST construída à mão não contorna as recusas e um
nome descartado não pode reaparecer como outro alias. O corpus ficou no digest
`33254881effa0b8325d351c0732ec4e23a260e0d9cd78ed83de75dc9f6ba1b04`, raw 70/17, nove débitos e
entries 73/22. A revisão Nexus `hof_89b6fa0a5aa243d7b314b71a72782ebc` foi concluída/verificada e
PASS, assim como duas auditorias independentes.

M-PULSE-2G está concluído no código `6e4f1d5e91878395f9736a95b855296f69e2e248`. Ele cobre somente
o node scan polimórfico read-only do probe `MATCH (n) RETURN n`, I19 e os seis templates públicos
`COUNT_ALL_NODES`, `COUNT_ALL_NODES_BY_TYPE`, `GET_ALL_NODES`,
`GET_ALL_NODES_AFTER_CURSOR`, `GET_ALL_NODES_BY_TYPE` e
`GET_ALL_NODES_BY_TYPE_AFTER_CURSOR`. `OPTIONAL MATCH`, endpoints ou relações sem tipo,
named/path projection, `UNION`, writes polimórficos e agregação/ordenação em `WITH` continuam
fora. O gate diferencial fechou exatamente como congelado: somente esse probe mudou para
`planned`, somente esses sete IDs mudaram para `already_supported`, raw passou a 71/16 com oito
débitos e entries a 80/15; I01/I02 e todos os demais gaps preservaram seus objetos completos. O
corpus final tem digest `ac19e6735a90e5fe9831fdca67a80de1a3f4fffadd54b81151d9e343a7bd0d7a`.
Os demais constructs, mutations internas, DTO/erros, perfil `pulse-1` e o ratchet diferencial
total permanecem nos sublotes seguintes do próprio M-PULSE-2.

O sublote fixo M-PULSE-2H é a inferência de endpoints exclusivamente para o path read-only
`MATCH (a)-[r:TYPE]->(b)` usado por I01/I02: exatamente um `MATCH`, um pattern, um hop fixo e
outgoing, relação e endpoints nomeados, um único tipo, nodes sem label ou mapa inline, `WHERE`
opcional e `RETURN` obrigatório. O planner resolve primeiro a tabela da relação, infere o source
por `from_table` e deixa o target seguir o `to_table` já suportado, formando os operadores atuais
`NodeScan -> TraverseRelationship`; não há operador, DTO ou superfície pública nova. Incoming,
undirected, variável-length, relação sem tipo, `OPTIONAL MATCH`, `WITH`, writes, named/path
projection, `UNION`, schema/provider e tipos lógicos multi-endpoint ficam explicitamente fora.
O gate diferencial é exato: nenhum dos 87 probes muda, raw permanece 71/16 com oito débitos,
somente I01/I02 passam de `generic_gap/plan_error` para `already_supported/planned`, entries passa
a 82/13 e os treze gaps restantes preservam seus objetos completos. I01/I02 mantêm duas colunas
`r.layer`/`r.rule_id`, ambas `STRING` nullable, cardinalidade many-rows e comparação por multiset,
sem `ORDER BY` ou `LIMIT`. A matriz dos 16 tipos, zero/uma/paralelas, `NULL`, filtro I02,
owner-only/rollback, shapes excluídos, corpus byte a byte, Ruff e diff-check formam o gate.

M-PULSE-2H está concluído nos commits `a516c64` (capacidade e regressões) e `42078ca` (formatação do
código novo). O gate diferencial fechou exatamente como previsto acima: nenhum dos 87 objetos raw
mudou, raw permanece 71/16 com oito débitos, somente I01 e I02 passaram de
`generic_gap/plan_error` para `already_supported/planned`, entries passou a 82/13 e os treze gaps
restantes preservaram seus objetos completos. Nos dois oracles alterados o bloco de linhas é
idêntico byte a byte; o que sai é apenas o `error` da recusa anterior, que era o registro do gap.
O corpus regenerado tem digest
`2fec52e0f033c3674aa8558fc5cca4aec05dacc7eae82e111bc2819864873e36`. Uma exclusão que o texto
congelado já previa exigiu uma correção de proveniência: `min_hops`/`max_hops` não distinguiam
`-[r:T]->` de `-[r:T*1..1]->`, então o pattern passou a registrar se um `*` foi escrito, com
default compatível e sem mudar o que qualquer pattern casa; `*1..1` e `*1..2` são recusados. A
resolução centralizada da relação também preserva a recusa tipada quando `TYPE` nomeia uma node
table. A suíte query passou 1.141/1.141, corpus 45/45, o gate focado pós-formatação 388/388 e as
fronteiras públicas selecionadas 158/158. Três auditorias independentes reproduziram o contrato e
o handoff Nexus `hof_8ceb3eb19622472bbd50a83c81921015` foi concluído/verificado/PASS. Corpus
`--check`, Ruff e diff-check passaram; a dívida de formatter permaneceu em 250 arquivos tanto na
base quanto no candidato.

O próximo sublote fixo M-PULSE-2I aceita somente um named path decorativo e não lido na forma
read-only `MATCH path = (a:A)-[r:TYPE]->(b:B) [WHERE ...] RETURN ...`: um `Query`, um `MATCH`, um
pattern, um hop fixo outgoing e tipado, path/relação/endpoints nomeados, ambos os endpoints com
exatamente um label, sem mapas inline ou range escrito e com `RETURN` obrigatório. `PatternPath`
ganha `variable` no final dos campos, com default `None`, preservando construtores posicionais; o
parser reconhece `name =` somente dentro de `MATCH`, `describe()` preserva o texto e o plano ignora
o nome apenas depois de analyzer e planner comprovarem que ele não é lido. O planner repete o gate
diretamente sobre o `Statement`, inclusive com AST/análise fornecida, e o gate de M-PULSE-2H passa
a exigir path sem nome.

Path projection ou qualquer referência ao nome em `WHERE`, `WITH`, `RETURN`, `ORDER BY`, `SKIP` ou
`LIMIT`; colisão com nome de node/relação; múltiplos `MATCH`/patterns/named paths; `OPTIONAL`,
`UNION`, `UNWIND`, `WITH`, writes, `CREATE`/`MERGE` nomeado; relação sem tipo, incoming/undirected,
multi-hop, qualquer `*`, maps, endpoint anônimo/sem label; DTO/path runtime, schema/provider e
multi-endpoint ficam fora. O ratchet é exato: os 87 probes e o contrato raw 74/13 permanecem no
inventário; engine raw passa de 71/16 para 72/15 e o débito de oito para sete; somente `named path`
passa a `planned/accepted`, enquanto `path projection` continua recusado pre-stream e pode mudar de
`parse_error` para `analysis_error` por a sintaxe agora ser reconhecida. Os outros 85 objetos raw e
todas as 97 entries permanecem integrais em 82/13. Parser/AST/`describe`, zero/uma/paralelas,
owner-only/rollback, colisões, exclusões, AST/análise forjada, diferencial full-object, corpus
`--check`, Ruff e diff-check formam o gate; o digest novo só é registrado depois da regeneração.

M-PULSE-2I está concluído nos commits `1c728cc` (capacidade e regressões) e `edf6efd`
(hardening fail-closed), publicados no branch `milestone/pulse-query-named-path` e integrados à
`main`. O corpus regenerado e o gate diferencial fecharam exatamente como o parágrafo previa:
mudaram DOIS objetos raw e nenhum outro — `named path` de refused/parse_error para
accepted/planned, e `path projection` continuando refused com a fase migrando de `parse_error`
para `analysis_error`, porque com a sintaxe reconhecida quem recusa `RETURN path` passa a ser a
análise. Os outros 85 objetos raw ficam íntegros, as 97 entries ficam íntegras em 82/13, engine
raw passa a 72/15 com sete débitos, e o contrato permanece invariável em 74/13 sobre 87 probes. O
corpus regenerado tem digest
`e792ded751eeffbe597a4e37d9110b30943e3d0fa69bd22027ad09778fc24f1c`. O teste dedicado passou
60/60, nove suítes relacionadas passaram 539/539, o corpus passou 45/45 com `--check`, a suíte
completa `tests/query` terminou com exit 0, Ruff/diff-check passaram e a dívida de formatter
permaneceu 250/250 contra a base. Duas auditorias finais independentes reproduziram o contrato,
incluindo AST/análise forjada, colisões e cardinalidades malformadas, e o handoff Nexus
`hof_a3e5645c9d4b437bbf75841a20872c13` foi concluído/verificado/PASS.

O próximo sublote fixo M-PULSE-2J aceita somente um relationship traversal com upper bound
omitido nas três grafias que o contrato Pulse já normaliza: `*`, `*..` e `*n..`. A forma bare e
`*..` tornam-se `1..20`; `*n..` preserva o lower escrito e recebe upper 20. O teto 20 vem de
`MAX_TRAVERSAL_DEPTH` do Pulse e é distinto de `MAX_TRAVERSAL_HOPS=30`, que continua sendo apenas
o maior upper explícito aceito pelo Grafx. O AST permanece o mesmo e registra
`hop_range_written=True`; `describe()` pode canonicalizar a forma aceita para o range explícito.
Planner e executor existentes continuam recebendo um traversal finito.

Ranges com upper explícito, inclusive `*1..21` até o máximo 30, não mudam; `*n` continua sendo
range exato; lower zero, lower maior que 20 depois do default, upper explícito maior que 30,
valores não inteiros e shapes malformados continuam recusados. `OPTIONAL MATCH`, `UNION`, relação
sem tipo, homoglyph na raiz, trailing clause, path projection, novos operadores/DTOs/APIs,
schema/provider, catálogo e formato persistido ficam fora. Analyzer e planner repetem sobre AST
fornecida a validação estrutural `type(min_hops) is int`, `type(max_hops) is int`,
`1 <= min_hops <= max_hops <= 30` e `type(hop_range_written) is bool`, antes de qualquer plano ou
stream; isso impede que uma árvore forjada transforme o novo default finito em trabalho sem teto.

O ratchet diferencial é exato: contrato raw permanece 74/13 sobre 87 probes; engine raw passa de
72/15 para 73/14 e o débito de sete para seis; somente o objeto `unbounded variable length` passa
de refused/parse_error para accepted/planned. Os outros 86 objetos raw e todas as 97 entries
permanecem byte a byte iguais, com entries em 82/13. Parser/`describe`, análise/planner com AST
forjada, equivalência com o range explícito, ciclos, relationship isomorphism, budget/cancelamento,
corpus completo + `--check`, suíte query, Ruff, formatter e diff-check formam o gate. O novo digest
só é registrado depois da regeneração e do diferencial full-object.

M-PULSE-2J está implementado e entregue para verificação, ainda NÃO concluído. O corpus foi
regenerado e o diferencial fechou exatamente como o parágrafo previa: mudou UM objeto raw,
`unbounded variable length`, de refused/parse_error para accepted/planned com error nulo; os
outros 86 objetos raw e todas as 97 entries ficaram byte a byte iguais em 82/13; engine raw
passou de 72/15 para 73/14 e o débito de sete para seis; o contrato permaneceu 74/13 sobre 87
probes. O digest regenerado é
`ded258c21bb0d58b93f36e3b641a13ee421f90a3b234951c0ebe871cb59811c4`, idêntico à sentinela
derivada independentemente pelo Codex antes da implementação. `*` e `*..` passam a 1..20, `*n..`
preserva o lower e recebe 20, e a forma aceita é escrita de volta como o range explícito; Pulse
Core materializa `*..20`, que é a mesma travessia que 1..20. Ranges com upper escrito não mudam e
continuam limitados a 30. Analyzer e planner validam a mesma árvore fornecida antes de qualquer
plano: contagens inteiras, 1 <= min <= max <= 30, flag booleana, e range não escrito significa
exatamente um hop.

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

O sublote fixo M-PULSE-3B estabelece somente o layout lógico de relações no adapter Community. A
autoridade fechada do Core gera um manifesto de 16 tipos lógicos e 69 pares e um codec bijetivo
`(logical_type, from_type, to_type) -> logical__From__To`; a resolução reversa vem do manifesto,
nunca de inferir tipos quebrando um nome físico. A introspecção read-only valida no catálogo Grafx
que cada tabela é `rel` e possui exatamente o par declarado, agrupa por nome lógico e não expõe o
nome físico nos DTOs Pulse. O mesmo resolvedor passa a ser usado pelo
`CommunityGrafxGraphTransaction`; desconhecido, colisão ou endpoint divergente falha tipado.

M-PULSE-3B não executa DDL/bootstrap/evolve, não ativa provider, não reescreve queries lógicas, não
reclassifica corpus, não altera `TableDef`, catálogo, `CATALOG_FORMAT_VERSION`, gramática ou
`label(r)` e não corrige `EXPLAIN_CONSTRAINT_ORIGINS`. Os dez templates de supersedence continuam
gaps até o sublote de bootstrap/rewrite; `EXPLAIN_CONSTRAINT_ORIGINS` exige antes resolver a
inconsistência normativa, pois a query usa `derives_from(Decision->Constraint)` e a autoridade atual
declara apenas `Decision->Requirement` e `Entity->Entity`. O gate é 16 tipos, 69 pares, 69 nomes
únicos/reversíveis; unknown/collision/mismatch fail-closed; dois pares do mesmo tipo em tabelas
distintas; visão lógica idêntica após reopen; nomes físicos ausentes da introspecção; regressões do
provider transacional e prova de zero delta no formato/gramática Grafx.

M-PULSE-3B está concluído no Pulse Community pelos commits `067b82c` (layout lógico e regressões)
e `c4b1f37` (hardening do manifesto), publicados no branch
`milestone/grafx-mpulse3-logical-relationships` e integrados em
`feature/v0.3.3@c4b1f37ad3a4cd08a1e2f5249db25c33ddbecd45`. O manifesto é autoridade imutável de 16 tipos,
69 pares e 69 nomes físicos; a resolução reversa não interpreta nomes, a introspecção valida kind
e endpoints no catálogo e expõe somente definições lógicas. Iteráveis malformados, representação
hostil, unknown, collision e mismatch falham tipados; configurações customizadas mantêm o
resolvedor lógico anterior salvo injeção explícita. O gate focado passou 15/15 e a regressão
selecionada completa passou 146/146 contra um checkout limpo do Pulse Core
`feature/v0.3.3@ab61b9a785f2018312fc91541a580877fd068bbb`; Ruff, format e diff-check passaram. Duas
auditorias independentes deram PASS. Permanecem fora, conforme o freeze, DDL/bootstrap/evolve,
ativação do provider, rewrite de queries, dez gaps de supersedence e a divergência normativa de
`EXPLAIN_CONSTRAINT_ORIGINS`.

O próximo sublote fixo M-PULSE-3C cobre **somente** o manifesto completo do schema Pulse atual
`0.5.0`, seu bootstrap Grafx idempotente e a validação/fingerprint lógico. A autoridade permanece
no Core (`NODE_TYPES`, `STABLE_NODE_PROPERTIES`, `REL_TYPES`, `MULTI_REL_TYPES`,
`EDGE_METADATA_COLUMNS`, `VECTOR_INDEX_TYPES`, `SCHEMA_VERSION`) e no DDL Community já usado em
produção. O manifesto Grafx materializa exatamente 11 node tables com 44 propriedades ordenadas,
`id STRING` como primary key não nullable e `embedding VECTOR(...)` nullable; uma `BoardMeta` com
`board_id STRING` primary key, `schema_version STRING`, `bootstrapped_at TIMESTAMP`,
`embedding_model STRING` e `embedding_dimension INT64`; e as 69 tabelas físicas de relação do
M-PULSE-3B, cada uma com os dois endpoints não nullable e as propriedades ordenadas
`confidence DOUBLE`, `created_by_session_id STRING`, `created_at TIMESTAMP`, `layer STRING`, `rule_id STRING`,
`created_by STRING` e `fallback_reason STRING`. Tipos, ordem, nullability, primary key e endpoints
são parte do contrato e não podem ser inferidos por presença nominal apenas.

Cada um dos 11 node types recebe um space físico próprio, em ordem de `NODE_TYPES`, nomeado pela
autoridade estável `vector_index_name(node_type)`, com dimensão 384, métrica cosine,
`normalized=false` e storage `float64`. Spaces distintos são necessários porque o Grafx mantém a
resolução vetorial por `space_name`; compartilhar um space entre tabelas sobrescreveria o índice
resolvido. O DDL atual do Grafx anexa um índice derivado a cada coluna `VECTOR`, portanto surgem
11 índices físicos. Isso não declara paridade vetorial: o fingerprint M-PULSE-3C exclui índices
derivados e a capability Pulse continua expondo somente os nove tipos de `VECTOR_INDEX_TYPES`.
M-PULSE-4 deve medir o overhead dos dois aceleradores não expostos e decidir mantê-los ou evitá-los
antes da ativação, sem mudar coluna, space, fingerprint ou dados persistidos.

O bootstrap recebe explicitamente `database`, `board_id`, `bootstrapped_at`,
`embedding_model | None` e `embedding_dimension | None`. Metadata de embedding é um par: ambos
`None`, ou model não vazio com dimensão exatamente 384. Antes de qualquer write ele captura um único
catálogo público e valida todo objeto esperado já existente e qualquer objeto inesperado; kind,
primary key, colunas completas, ordem, tipo, nullability, vector-space, endpoints e configuração
do space divergentes falham na taxonomia Core com `backend=okto_grafx` e sem DDL/WAL. Em catálogo
vazio ou parcial contendo somente um subconjunto correto, cria somente os objetos ausentes em uma
única transação write; após o commit, recaptura e valida o schema completo. Somente depois dessa
validação grava a row `BoardMeta` do board em uma transação separada. Falha de criação ou validação
deixa a versão ausente; retry converge. Uma row já presente precisa ter `SCHEMA_VERSION`; metadata
persistida é preservada quando o caller não oferece par, deve ser idêntica quando oferece, e pode
ser preenchida uma única vez se ainda estiver ausente. Schema completo + row compatível retorna
sem DDL, mutation ou avanço de WAL/LSN.

O fingerprint usa JSON canônico e SHA-256 sobre a visão lógica validada: versão, 11 nodes e suas
colunas, 16 relações e 69 pares, propriedades de relação, `BoardMeta` e os 11 spaces. Ele exclui
table/space IDs, LSN/CSN, paths, timestamps de criação, nomes físicos das 69 relações e nomes de
arquivos/índices; o reopen precisa produzir o mesmo valor. M-PULSE-3C não implementa `ALTER`,
evolve ou upgrade de schema anterior, não ativa provider, não altera `kg.py`/`composition.py`, não
reescreve queries lógicas, não fecha os dez gaps de supersedence, não muda gramática/formato Grafx
e não reivindica os gates de M-PULSE-4.

O gate fixo de M-PULSE-3C é: bootstrap vazio produz 11 nodes + `BoardMeta`, 69 relações e 11
spaces exatos; segunda execução é no-op comprovado por catálogo, LSN e WAL; catálogo parcial
correto converge criando somente ausentes; cada divergência de kind/PK/ordem/nome/tipo/nullability,
endpoint, dimensão/métrica/normalização/dtype/state e objeto inesperado falha antes de write;
falha durante bootstrap nunca grava versão; metadata/version incompatível não é sobrescrita;
cold reopen mantém fingerprint e `database.verify("all")` sem findings; os builders Kuzu e
`kg.py`/`composition.py` permanecem byte a byte invariantes. Upgrade aditivo continua um sublote
posterior de M-PULSE-3, sem mover este gate retroativamente.

O sublote fixo M-PULSE-3D cobre **somente** a evolução do predecessor histórico Pulse `0.3.12`
para o manifesto atual `0.5.0` por reconstrução em uma geração Grafx nova e inicialmente não
vinculada. O contrato normativo foi congelado antes do código em
`okto-pulse/docs/grafx-schema-evolution-0.3.12-to-0.5.0.md`, commit Community
`703ad83c43b286e7c90fe2b0a29de0982929d4de`. Ele recusa evolução física in-place, `ALTER`, destino
parcial/incompatível e qualquer ativação do provider; M-PULSE-7 continua sendo a autoridade para
fencing e cutover. A fonte é aberta em snapshot read-only, o candidato usa path durável distinto e
lock de processo, e schema mais marcador `building:0.3.12->0.5.0` nascem atomicamente. Nós, relações
paralelas, propriedades nulas, direção, self-loops e vetores são copiados sem deduplicação; os onze
campos aditivos nascem `NULL` e as dez novas tabelas relacionais nascem vazias.

O fingerprint lógico de dados possui codec binário/canônico fechado e golden digest
`9d1123371dc1ed6009737f24bdf19a2a293fa7598585c3e45ba598ff64b6b175`. A validação terminal exige
catálogo/fingerprint/contagens/vetores/índices exatos, `verify("all")`, checkpoint, recaptura sob o
lock, cold reopen read-only e repetição integral; somente então o marcador muda para `0.5.0`. Uma
reexecução sobre candidato completo e logicamente idêntico é no-op (`changed=False`); marcador de
build abandonado, drift ou catálogo inesperado recusam. O gate fechado de M-PULSE-3D é a igualdade
do fingerprint de schema entre bootstrap vazio, bootstrap repetido e rebuild do predecessor, mais
as provas de invariância da fonte e de ausência de escrita no candidato completo. Paginação por
offset pode repetir scans/sorts completos: `batch_size` limita linhas devolvidas e intents de write,
enquanto os budgets reais de query são `max_result_rows`/`max_intermediate_rows` e os de bytes de
write são `max_transaction_bytes`/`max_wal_batch_bytes`; cursor/bulk permanece em M-PULSE-5.

A implementação final está em
`237f3bf7fa2a65a108db4f558932d429ec6696ce`: `aece7d1` implementou o rebuild, `121e264` fechou a
fronteira de erro de `Path.exists()` e a matriz finita de falhas, e `237f3bf` completou a prova do
checkpoint de recovery ambíguo sem mudança adicional em produção. O gate rápido terminou com
`122 passed, 1 deselected`; a suíte completa com o fixture real terminou com `122 passed` em
`121e264`, cujo código de
produção é byte a byte idêntico ao SHA final. A regressão M-PULSE-3A/B/C passou 56/56; Ruff,
format, `py_compile` e diff-check passaram. A revisão Nexus
`hof_3df4987f6eef4611bfd9486409b91227` foi concluída, verificada e PASS no SHA final, incluindo
prova discriminante de que o teste de `PermissionError` falha no commit defeituoso e passa no
corrigido. Fonte inerte, cópia de duplicatas/paralelas/self-loop/NULL/vetores, catálogo e 161
índices exatos, no-op zero-write, commit ambíguo, close/lock e cold reopen estão cobertos.

#### M-PULSE-4 — paridade vetorial

1. mapear os nove espaços de board e os quatro de Global Discovery para espaços Grafx;
2. normalizar score e filtros;
3. suportar create/rebuild/status dos índices;
4. executar gates exact/ANN, cold/warm, churn e reopen.

**Gate:** top-k exato retorna os mesmos IDs elegíveis e ordenação; empates têm política
determinística e scores usam tolerâncias absoluta/relativa congeladas no fixture, não igualdade
bitwise entre backends. ANN atende o recall congelado pelo harness e nunca retorna item inelegível
por board/layer/supersedence.

O contrato pré-código finito desta etapa foi congelado no Pulse Community em
`docs/grafx-vector-parity.md`, branch `milestone/grafx-mpulse4-vector-parity`, SHA
`6a2ec1a2283b95cb3455b70212f9081b7cd1a515` (publicado no `origin` em 2026-08-27). Ele fixa os
nove espaços públicos de board, os quatro espaços de Global Discovery, normalização de score,
ordenação/tolerâncias, rebuild público, matriz V1–V7 e não objetivos. Achados posteriores só podem
corrigir defeito reproduzível desse contrato; não ampliam o milestone.

Fechamento em 2026-08-27/28: o milestone aceito e `feature/v0.3.3` convergiram em
`d3ef4afdf263e7b6da70b6705b31950cfe07986e`. A matriz final passou 56/56 em 267,06 s, além de
Ruff, compileall, diff-check e format-check dos dez arquivos Python alterados. O recall@10 público
foi `0.9546875` no fixture congelado 8192x384/256 queries/k=10. A medição não normativa dos índices
`Alternative` e `Assumption` foi publicada em
`milestone/grafx-mpulse4-vector-recall@fcfbf215b151aac4603978e02900ef323cb1b898`, artefato SHA-256
`e010ef6fb4a46b9e7a7bb770c9d4f6007b5b1af6415efb9952e3b7c369a0bede`: 8192 linhas e 128
commits por índice, build/ingest de `1279.6239901 s` e `1415.9925527 s`, tamanhos persistidos de
`794624 B` e `802816 B`, `verify_findings=0` e cold/reopen 10/10. A auditoria independente conferiu
branch/remoto, pins, JSON, metadata e relatório; a evidência permanece explicitamente sem SLO.

#### M-PULSE-5 — export/import, backup e recovery portável

1. congelar o formato lógico `okto-pulse-logical-graph/1`, com manifesto de schema, features
   obrigatórias, contagens, checksums e manifesto terminal;
2. exportar Board e Global Discovery de Ladybug sob freeze/snapshot único e importar em batches
   limitados para geração Grafx nova, vazia e não vinculada;
3. exportar Grafx sob uma única transação MVCC read-only e importar em batches limitados para
   geração Ladybug nova, vazia e não vinculada; este caminho bidirecional é a opção M-PULSE-5, não
   um journal;
4. preservar tipo lógico, chave, todas as propriedades, string vazia, timestamps, direção,
   self-loop e uma entrada por ocorrência de relação, inclusive paralelas idênticas. O formato
   Core preserva `absent` versus `NULL`; os adapters físicos Grafx/Ladybug, ambos de schema fixo,
   projetam todas as colunas declaradas e convertem o único estado físico nulo em `LOGICAL_NULL`.
   Eles não inventam ausência histórica: uma entrada com propriedade ausente que o destino físico
   não represente deve ser recusada de forma tipada e o candidato abortado, nunca canonicalizada
   silenciosamente como sucesso;
5. serializar vetores pelo `space_name` lógico e remapeá-los no destino; IDs físicos de space,
   RecordIds, páginas, filenames, WAL/LSN e topologia HNSW não pertencem ao formato;
6. calcular fingerprint canônico sobre schema e multiconjunto completo, reabrir o candidato a frio
   e exigir schema, contagens, vetores, fingerprint e `verify()` iguais antes de emitir sucesso;
7. publicar backup por temp + flush/fsync + verificação + replace atômico e restaurar sempre fora
   do lugar, preservando a geração anterior;
8. adaptar as operações públicas necessárias de lifecycle/recovery para nomes neutros;
   aliases/configurações internos Ladybug/Kuzu existentes não são renomeados neste milestone.

O escopo de Board é exatamente `BoardMeta`, 11 tipos de nó com 44 propriedades, 69 layouts
relacionais com sete propriedades e 11 spaces. O escopo Global Discovery é exatamente quatro tipos
de nó/spaces e sete relações. Nós cognitivos canônicos sem origem SQL fazem parte da mesma
transferência, sem rebuild derivado.

A correção de representabilidade congelada em 2026-08-27 não altera o wire nem reduz a prova do
codec. Probes independentes após commit, close e cold reopen demonstraram que tanto Grafx quanto o
`ladybug==0.16.0` pinado devolvem a mesma coluna `NULL` para propriedade omitida e para `NULL`
explícito; nenhum deles persiste um bit de presença. Assim, o golden Core continua provando
`absent != LOGICAL_NULL`, enquanto o round-trip físico prova a visão canônica efetivamente
armazenável e a recusa fail-closed do ramo inalcançável. Um sidecar durável foi excluído deste
milestone porque seria apenas prospectivo, não recuperaria a intenção dos bancos Pulse existentes
e exigiria mudar todos os writers/runtime, ampliando M-PULSE-5 sem melhorar a migração atual. A
auditoria Nexus `hof_2a9c3574a2e041e68ff2b137cf345afb` foi concluída/verificada/PASS após medir os
dois backends e corrigir explicitamente a dependência/runtime usada no primeiro probe.

O `graph_export.py` atual permanece uma exportação JSON-LD de exposição/proveniência e não é entrada
válida do importador portátil: ele omite 35 propriedades de nó, seis propriedades de relação e
embeddings, colapsa `NULL`/ausente e deduplica por tipo/endpoints, perdendo multiplicidade.

No Grafx, a única superfície nova é `Transaction.scan_rows_v1(table, limit, cursor=None)`: ela usa o
snapshot da transação read-only existente, devolve DTOs destacados na ordem física estável, inclui
endpoints das relações e preserva uma row por ocorrência. O cursor é opaco, preso à identidade do
banco, transação/snapshot e tabela, e não pode ser reutilizado. A implementação deve manter memória
`O(limit + valores do lote)` e avançar a posição de storage sem `QueryResult`, sort ou traversal.
Não há novo `Database.logical_snapshot()`, bulk import, archive, backup ou semântica Pulse no core.

Como `_from/_to` são RecordIds físicos cujo domínio é a tabela de nó, o source Community Grafx
constrói em streaming um mapa temporário disk-backed
`(node_table, record_id) -> logical_key` antes de emitir relações. O mapa usa RAM `O(limit)`, disco
`O(nós no escopo)`, lookup indexado por lote e cleanup em `finally` tanto no sucesso quanto na
falha. RecordId sem tipo é inválido porque IDs colidem entre tabelas. Dict completo em RAM, rescan
quadrático e nova API de lookup no Grafx ficam excluídos.

O Core Pulse possui apenas DTOs/codec/fingerprint, snapshot source, candidate sink e orquestração
neutra. O Community possui adapters Ladybug/Grafx, arquivo atômico e operações de backup/restore.
Nenhum tipo, path ou erro de backend entra no Core.

**Gate finito:** codec golden e recusa de versão/feature desconhecida; Board Ladybug→Grafx; Board
Grafx→Ladybug; Global Discovery nos dois sentidos; snapshot consistente sob writer concorrente;
corrupção/falhas em write, import, checkpoint e reopen; memória limitada, nomes neutros e geração
anterior intacta. Cada round-trip preserva 100% dos nós, relações, propriedades, vetores e
multiplicidade da visão física canônica. O codec prova separadamente `absent` versus `NULL`; o
adapter de schema fixo prova `NULL -> LOGICAL_NULL` e recusa tipada, sem sucesso ou cutover, para
ausência que o destino não consiga persistir. O scan Grafx prova continuação/snapshot, relações
paralelas, bounded-memory, tokens inválidos e relação ainda física cujo endpoint foi removido; o
source Community prova também o mapa temporário de endpoints sob RAM limitada e seu cleanup.

M-PULSE-5 não implementa provider/router, binding/CAS, shadow, canário, dual write, journal/outbox,
troca de tráfego, migração in-place ou retomada de candidato parcial. Binding pertence a M-PULSE-6;
journal e cutover pertencem a M-PULSE-7.

O congelamento deste recorte foi revisado no Nexus em
`hof_aa27cde687bd41bbaf57e0e74b09e7ef`, concluído, verificado e PASS: contagens Board/Global,
perdas do exportador JSON-LD, necessidade do scan bounded-memory e fronteiras M6/M7 foram
conferidas diretamente nas árvores Grafx/Pulse, sem blocker.

**Fechamento executado em 2026-08-28:** as factories Board/Global dos dois backends foram
publicadas em `f73cdf4e1e5a64c48e285f514f27d16a2d913853` e integradas em
`a4a5ec11bc20490fef7395066647a2152651291f`. A matriz final permaneceu exatamente com oito testes
parametrizados e 32 casos — A1 2, A2 3, B1 4, C1 4, C2 2, D1 8, D2 5 e D3 4 — no commit
`d61f3a875952cf6fbf9bb4be177c9a98a98d83d0`. A primeira execução encontrou cinco falhas Grafx de
um único baseline temporal: o digest incluía `control/` antes de o caller abrir a conexão
read-only, cujo contrato admite criar apenas `control/txn-*.lock` vazio. A prova por arquivo mostrou
zero alteração em catálogo, heap, metadata, WAL ou índices; mover os dois baselines para depois do
open do caller e antes da factory/transfer preservou a exigência byte a byte dentro da fronteira M5.
Os cinco casos afetados passaram 5/5, a matriz integrada passou 32/32 em 1.239,02 s e a regressão
consolidada dos nove módulos M5 passou 189/189 em 1.341,70 s, sempre com o Pulse Core explicitamente
pinado no worktree `098a346b0988d7b39e417e7de7ed8d57d06b9795`. Ruff, Black nos 19 arquivos,
compileall e diff-check passaram. A auditoria independente Nexus
`hof_2b2ca8a48e4b472abe9f51e92c765138` executou novamente os 32 casos no commit exato, terminou
32/32 e foi concluída/verificada/PASS, sem mutação nos worktrees. O milestone Community imutável
`08e4fa71dce08d3d3a99929ed0c68a44fc260631` foi promovido para
`feature/v0.3.3@e73a446a954e039fb038f8fa329236b51104504a`; os tree hashes da integração testada e da
promoção são idênticos (`76b9bd8979f9345aa05d9c613716291eeb2ee95b`).

#### M-PULSE-6 — providers Grafx e conformance end-to-end

1. criar o bundle coerente `CommunityGrafx*` no Pulse;
2. criar um routing bundle estável que selecione backend+geração por board; cada scope usa um único
   backend coerente, e Global Discovery possui binding global separado;
3. manter nomes de configuração legados como aliases durante a transição;
4. fixar versão exata do Grafx;
5. normalizar resultados, timestamps, mapas, vetores e erros nos DTOs/taxonomia Pulse. Para o
   path M-PULSE-2O, o provider converte somente as sequências `_NODES`/`_RELS` de tuple Grafx
   para list Ladybug e preserva chaves/ordem/correlações; a camada também traduz o `LIMIT 1000`
   e o filtro canônico que o endpoint injeta, mantendo distintos os envelopes
   `canonical_only` e `canonical_and_working`, sem ampliar a gramática do engine;
6. revalidar fencing em toda mutação e imediatamente antes do commit/cutover;
7. adicionar suíte diferencial por port e por fluxo de negócio;
8. validar no admission do provider a geometria persistida antes de bootstrap/rebuild: o manifesto
   Pulse atual precisa de capacidade para pelo menos 81 extents no heap único. No formato físico
   vigente isso exige `page_size >= 4096`; a configuração padrão de 8192 é segura e valores menores
   devem ser recusados de forma tipada, sem tentar ativação parcial.

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

O ratchet que autentica as entradas deste gate está certificado no Community
`6595abdcfa788dfa2cc8da1a53ff96c378790531` (base
`d44c82155e9884c556813ea96dec829be567c236`, branch
`origin/milestone/grafx-mpulse7-ratchet-530df34`) com pin Grafx
`d39e27435171574ab6f03bc1d17672b26bf163b2`. Manifesto físico/canônico:
`d1777bb26aee2feae5c8d5f4593840c08bdc37474ad6be4bdfe5334daedd0192` /
`1e6e92fc3bae3b54d3052ca9055b7682a9d518927573e0ffcbfcbb4568cf9f93`; corpus físico/lógico:
`0997747ed8bb9172d05781a62e5f81e7694630b173aaa152ac9ea28daec9d13f` /
`b29334edf6e7c1e6b9419a4f3add84ede4baad94fdeaecb0c679261a78f241cc`. O gate focado passou
12/12 e a auditoria independente 39/39. Naquele checkpoint, essa certificação autenticava as
entradas e o run 10k ainda não havia sido iniciado. A autoridade corrente é a fixture de conteúdo
v2 no schema `/1`, Community `18478ede9528556ef5e59fa7f91c72f4ccba2a5e`, Grafx
`8cee82b9b92529f2ba767519c01c161f20876dce` e Core
`ccc1f345ece1db89a274cfdd634bd4da27028f63`, com manifesto físico/canônico
`e5c9ef4432b2c550e8ba22fc57ee607a0eabdb8cf6e49a87ae080d50c726ee66` /
`ea7b070b8e98b3a53b12bfe89cc4ff4355c5c433da03b8bcf681733398bd199e` e queries
`f0f50de41464a112147d1d13552fcb4b964022799959f5b64c64ca68df3ac2cd`. Os runs `a01` e
`a03` já executaram bilateralmente o trace de 10.000; `a04` é a repetição definitiva nesses pins.

Antes da execução, o trace de 10.000 operações é congelado com fixture, distribuição, seed,
fingerprints esperados, cobertura de cada família de mutação e pontos de crash. “Representativo”
sozinho não pode ser usado para mudar o gate durante a rodada.

#### RELEASE-0.0.1 — run integrado, auditoria e publicação conjunta

1. executar o Pulse com o bundle Grafx efetivamente ativo, cobrindo os fluxos de board e Global
   Discovery certificados em M-PULSE-6 e o roteiro congelado de M-PULSE-7;
2. executar as suítes completas do Grafx e as suítes Core/Community aplicáveis ao bundle, além dos
   gates de crash/reopen, conformance, diferencial e integridade já congelados — sem criar novos
   critérios durante a auditoria;
3. auditar o SHA candidato completo contra os contratos versionados, registrando somente blockers
   reproduzíveis de correção, corrupção, segurança, estabilidade ou incompatibilidade;
4. construir `sdist` e wheel de `0.0.1`, validar metadata/conteúdo e instalar o wheel em ambiente
   limpo para repetir o smoke integrado;
5. pausar com SHA e hashes dos artefatos aprovados. A publicação no PyPI será feita em checkpoint
   interativo com o usuário; depois dela, reinstalar do PyPI e repetir import, versão e smoke.

**Gate de release:** SHA e artefatos imutáveis, todas as provas acima verdes, zero blocker aberto e
`okto-grafx[accel]==0.0.1` instalável do PyPI. Nenhum item `GX-CAP-*`, `GX-AGENT-*` ou `AGENT-*` começa
antes desse gate. A evolução complementar subsequente pertence à versão `0.0.2`.

**Estado do gate em 2026-09-02:** concluído. Os dois artefatos auditados foram publicados sem
alteração, reinstalados do índice público com `[accel]` e submetidos ao smoke previsto. A linha
`0.0.2` está liberada, mas não é iniciada implicitamente por este registro.

#### PULSE-GRAFX-ONLY — retirada futura de Ladybug/Kuzu

Esta é uma direção pós-`0.0.1`, não um atalho para M-PULSE-6/7. A dependência e os providers
Ladybug/Kuzu só podem ser removidos do Pulse depois de a janela de rollback do M-PULSE-7 encerrar e
de todas as instalações suportadas terem migrado seus bindings/dados para Grafx com fingerprint
lógico, cold reopen e `verify()` aprovados. O gate mínimo é: censo zero de bindings Ladybug ainda
ativos; nenhuma tentativa/journal de cutover ou rollback pendente; retenção/backup concluídos;
startup fail-closed diante de storage Ladybug legado não migrado; remoção de imports, composição,
configuração e dependência `ladybug`; wheel instalado contendo apenas `okto-grafx[accel]` como
backend físico; documentação de migração e versão Pulse explicitamente breaking. Nada desta seção
amplia o gate corrente nem autoriza apagar dados Ladybug antes dessas provas.

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
- M-PULSE-1 conclui a superfície estruturada do `GraphTransactionScope`; o `execute()` genérico
  permanece em M-PULSE-2.
- Depois de M-PULSE-1, M-PULSE-2, M-PULSE-3 e M-PULSE-4 podem avançar em paralelo em arquivos e
  branches isolados.
- Com os contratos M-PULSE-3/4 fechados, M-PULSE-5 é o próximo gate serial de compatibilidade.
- O scaffold de M-PULSE-6 pode existir, mas sua certificação final depende de M-PULSE-0 a
  M-PULSE-5.
- M-PULSE-7 começa somente depois da certificação M-PULSE-6 e é exclusivamente rollout/cutover;
  não pode descobrir semântica básica faltante.
- A antiga precedência temporal F1 → matriz CE-3 → M-PULSE-7 foi aposentada em 2026-09-01. F1 e
  CE-3 permanecem reproduzíveis como instrumentos, mas nenhuma taxa condiciona o run de 10.000
  operações; M-PULSE-7 avança diretamente sob os gates de qualidade congelados.
- A sequência vinculante é M-PULSE-5 → M-PULSE-6 → M-PULSE-7 → run integrado e auditoria →
  build/install limpo → checkpoint interativo, publicação e reinstalação de
  `okto-grafx[accel]==0.0.1` no PyPI → somente então linha `0.0.2`.
- Nenhum `GX-CAP-*`, `GX-AGENT-*` ou `AGENT-*` começa antes desse gate de release.

Cada milestone deve ter branch, commit e push próprios, suíte direcionada, suíte global verde,
revisão independente por um segundo agente e SHA imutável antes do merge serial em `main`.

Por orientação posterior do usuário em 2026-08-29, Claude voltou a participar da execução pelo
Nexus. A divisão corrente mantém implementação, revisão e medição em responsabilidades separadas;
nenhum resultado delegado é integrado sem validação final do Codex e sem o gate do milestone.

### 9.7 Registro de execução e evidências

**Leitura normativa do quadro:** estados temporais antigos nas linhas M-PULSE-7, CE-3, CN-2 e ST-2
foram preservados como histórico, mas seus pisos/razões de performance não são mais blockers. O
estado vigente é o do topo deste documento: `same-10` aceito em qualidade, harness de timeout
endurecido e correção mínima das duas divergências Board publicada na fixture v3 em Community
`23f9927`. O run integral `a05` passou no Grafx `8cee82b` e Core `ccc1f345`, com receipt canônico
`f08c8be63ea2cd7abf6c6ffbb0deef5203169ed22c5b42a2c34ac290d90b77f1`, dois traces de 10k,
11/11 crash/recovery, 19/19 Board e 97 casos Pulse sem divergência inexplicada. M-PULSE-7, a
regressão ampla Community, a construção/auditoria instalável, a promoção a `main`, a publicação de
`okto-grafx==0.0.1` e a reinstalação pública com `[accel]` estão concluídos. A linha `0.0.2` pode
começar sob seus roadmaps versionados; a matriz CE-3 temporal tornou-se evidência opcional.

| Marco | Estado | Evidência integrada | Validação registrada |
|---|---|---|---|
| M-PULSE-0 | concluído | `main@5b7551b40dba2facb28c46770f166ab3ac9daecc` | suites query/API do marco: 1.398 passes e 1 skip; chunks globais: 3.841 + 1.205 + 2.021 passes, 10 skips; Ruff limpo; nove mutantes mortos; revisão cruzada sem blocker |
| M-PULSE-1A — identidade pendente | concluído | `main@d487ac9312229e0376ad8e65625213af111d9c9c` | regressões de `PendingRowRef`, redução de intents, prevalidation e bloqueios passaram; Ruff e `git diff --check` limpos após rebase |
| M-PULSE-1A — overlay de nós | concluído | `main@3f354d9b4973086421500654fe74982606767df3` + hardening `main@aef1df734582cd04097fcc0393ecda587ee5409f` | `tests/query`, `tests/txn` e `tests/api` com exit 0; 51 regressões focadas pós-auditoria com exit 0; Ruff limpo; revisão independente encontrou dois casos, ambos reproduzidos e fechados antes do merge |
| M-PULSE-1A — update de relações committed | concluído | `main@a3bb8cb44cf86f5c151a232406c703f54f9a1316` | 6 regressões públicas e 814 testes de query passaram; Ruff global e diff-check limpos; validação independente focada 6/6; preservação de endpoints, isolamento, rollback, conflito, cold reopen e `verify()` cobertos |
| M-PULSE-1B — resolução de endpoints | concluído | `main@d2b73bacd861982a67d4ac217a8997ed332b0aca` (origem revisada `1b5a4266658b186014c8159c2861755a86320b0c`; handoff Nexus `hof_5836d33c98d04c27b152dc7f56902749`) | 31 regressões novas; 1.203 testes txn/query e 612 API (1 skip) passaram antes do rebase; 178 gates de integração passaram depois do rebase; Ruff/diff-check limpos; 15 mutantes mortos; auditoria independente: 139/139 e zero blocker |
| M-PULSE-1 — capacidade `replace_node_payload` | concluído no engine e integrado no Pulse | Grafx `main@959bb6e313433b489211d1cb3a8c6c1bb10587c0`; wrapper no Community `feature/v0.3.3@befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595` | 4 regressões públicas provam substituição de 5 campos em um único `MATCH ... SET`, identidade imutável, multiconjunto exato de incoming/outgoing/self-loop/paralelas, owner/outsider, no-op, rollback, conflito, cold reopen e `verify()`; Ruff/diff-check limpos |
| M-PULSE-1C — overlay relacional (engine) | concluído | `main@512e2f8656bd14c9f1a7cecd3fe18caabe32cb2a`; branch publicado `m1/pulse-rel-overlay-hardening`; origem Claude `06869c1` + rework `3358453`; handoff Nexus verificado `hof_bbccc4744e7943e09109845d387a1a8e` | 9 regressões congeladas verdes; 1.245 testes query/txn e 536 crash/index/vector passaram no candidato integrado; docs 3/3, Ruff global e diff-check limpos; auditoria independente PASS. Owner/outsider, pending start, source+target OCC, `_write_rows == 0` no conflito, unwind de read guards, crash pré-WAL/pós-barreira, cold reopen, endpoint token fail-closed e payload+arestas estão cobertos |
| M-PULSE-1 — valores públicos para adapters | concluído | `main@f3683e3dbca731a7f027e0df30e406d805244fac` | `Timestamp` e `VectorValue` exportados pela raiz suportada, contrato e packaging atualizados; import boundary/public surface passaram; auditoria independente PASS; nenhum import privado do domínio é necessário no provider Pulse |
| M-PULSE-1 — índice vetorial nullable/esparso | concluído e integrado | `main@ad38ed080c1359d347c5e0399d49d2c46eb60820`; branch `m1/pulse-nullable-vector-index` | matriz NULL→NULL/valor, WAL, empty marker, rebuild, verifier, HNSW/exact, rollback, retarget e cold reopen cobertos; 20 testes focados e lotes amplos passaram; auditoria independente executou 180 updates NULL→NULL em 29 segmentos e mutantes críticos. A suíte global chegou a 100% com uma única expectativa CLI preexistente, reproduzida sem o commit em `main@f3683e3`; o teste obsoleto foi alinhado separadamente ao contrato owner-only em `main@6e63345a24a30f97b817decd19e46c346da99002` e o arquivo completo passou 40/40 |
| M-PULSE-1 — freeze de `replace_node_payload` no Core | concluído e integrado no branch de release atual | Pulse Core `feature/v0.3.3@9f6f37da0c19371781ec86abd2cae2ae8fb400d3`; milestone preservado em `milestone/grafx-transaction-contract`; handoff Nexus `hof_25a808d44abb4fc4a97cdf15d0fc8b91` concluído/PASS | 19/19 testes independentes; Protocol, delegação fail-closed e memory provider confirmam payload exato, identidade estrutural, multiconjunto de incoming/outgoing/self-loop/paralelas e restauração quando o publisher aplica e lança. O push ocorreu somente depois do provider Kuzu compatível |
| M-PULSE-1 — provider Grafx, primitives básicas | concluído no branch; propositalmente inativo | Pulse Community `milestone/grafx-graph-transaction@50cd190dc18aab199bbadeb62a9f44f4be626e03` | create/update/snapshot/restore, replace exato baseado no catálogo físico, supersedence, edges, cleanup, attestation, timestamps, fencing por mutação+commit, rollback após falha de commit, resultado pós-durabilidade e taxonomia/redaction cobertos; 36/36, Ruff/format/diff-check e auditoria independente PASS. `execute()`, lineage e active-set permanecem fail-closed/deferidos |
| M-PULSE-1 — tombstone source-deleted no provider Grafx | concluído e integrado; provider ainda inativo | milestone Pulse Community `f8769d17fff5f74b9cdd2ee220813d811b7ed9da`, incorporado ao bundle `feature/v0.3.3@c12f4d9db662c7ba42f0f3689c30c1afab8ba620` | swap `DETACH DELETE + CREATE` em uma única statement, payload erasure fail-closed por schema, vetores nullable, remoção de todas as relações catalogadas, retry idempotente, fencing e rollback/poison após apply-then-raise ou confirmação divergente; 46/46 no arquivo completo, 4/4 revalidados pelo Codex, Ruff/format/diff-check limpos e revisão independente sem blocker |
| M-PULSE-1 — provider Kuzu compatível com `replace_node_payload` | concluído e integrado no branch de release atual | origem `milestone/kuzu-atomic-payload-contract@3a5a499f7d2addda98f0b37ce8b9d8ed36d4025d`; cherry-pick validado no bundle Community `feature/v0.3.3@c12f4d9db662c7ba42f0f3689c30c1afab8ba620`; handoff Nexus `hof_24969ea99ef247248fd3d127138b20f7` concluído/PASS | 22/22 contra Kuzu real passaram duas vezes pelo Codex com os paths Community/Core fixados e resolução de módulos comprovada; identidade, payload integral, arestas paralelas idênticas, incoming/outgoing/same-label/self-loop, lease loss e compensação pós-COMMIT cobertos; cinco mutantes mortos; `git diff --check` limpo e Ruff TRY/I sem delta contra o baseline |
| M-PULSE-1 — bundle transacional Core/Community | concluído para este lote | Community `feature/v0.3.3@c12f4d9db662c7ba42f0f3689c30c1afab8ba620` publicado antes do Core `feature/v0.3.3@9f6f37da0c19371781ec86abd2cae2ae8fb400d3` | gate conjunto contra o mesmo contrato: Grafx 46/46, Kuzu 22/22 e Core versionado 14/14; ambos os updates foram fast-forward. Os 12 arquivos sujos do Community original e os 13 entries do Core original permaneceram exatamente no worktree local, sem reset, checkout ou sobrescrita |
| M-PULSE-1 — Spec lineage no provider Grafx | concluído e integrado | origem Pulse Community `milestone/grafx-lineage-primitives@73b65dafb432b35ff3dea8edde8c6aa07b58c393`; bundle final `feature/v0.3.3@befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595` | `reconcile`, `clear` e compensação restore-first usam uma única resolução física por operação, before-images completos, transação Grafx real e rollback/poison após qualquer falha pós-mutation. 27/27 regressões próprias e 73/73 no gate combinado passaram; quatro provas adversariais cobriram múltiplos pais, self-loop, incoming/paralelas, metadata drift e cold reopen; Ruff/format/diff-check limpos e duas validações independentes sem blocker |
| M-PULSE-1 — active-set e cleanup pós-compensação | concluído e integrado | Community `feature/v0.3.3@befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595` (código `36c2fc6`); Core `feature/v0.3.3@ab61b9a785f2018312fc91541a580877fd068bbb`; handoffs Nexus `hof_9649f2bc0a974519a99b98e7350a1c7e` e `hof_bf680c016aae4c9ca3a14d21d889660f` concluídos/PASS | before-images de nó/relação completos, vetores não nulos reconstituídos no tipo declarado, paralelas idênticas preservadas por multiset, NaN recusado antes de delete, múltiplos receipts netados e `rule_id` de Spec incluído na identidade. Provider legado com apenas `**kwargs` falha tipado antes do sweep |
| M-PULSE-1 — primitives completas `GraphTransactionScope` | concluído e publicado | Community `milestone/grafx-mpulse1-conformance@befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595` → `feature/v0.3.3`; Core `milestone/grafx-transaction-contract@ab61b9a785f2018312fc91541a580877fd068bbb` → `feature/v0.3.3` | gate final: Grafx 123/123 em 14m25s, Kuzu/Spec 8/8, Core 61/61; auditoria independente focada 10/10 + 3/3 + 2/2 sem blocker; Ruff/format/diff-check limpos. Os worktrees originais permaneceram em `0401e412` com 12 mudanças e `985f6a88` com 13 entries. `execute()` genérico continua deliberadamente em M-PULSE-2 |
| M-PULSE-2A — corpus query contract 1.0 | concluído no milestone | `milestone/pulse-query-corpus@5b05b4f`; digest `54c3d75797eb9c92c58bbfb796a3e772b77b4c0307f971af88e8cc4978927554`; revisão Nexus `hof_ac705be83fb845ba8193c8f76a477ac8` concluída/PASS | 97 entradas; 68 famílias internas = 47 read/21 write e 66 current/2 preventive; 17 templates nomeados + 11 gerados sobre 27 textos; 87 probes/36 behaviours; authorities 11/16/69; zero `runtime_fragment`; 45/45, `--check` 25,6 s, Ruff default/TRY/I/BLE, format e diff-check PASS; nenhuma mudança de engine neste sublote |
| M-PULSE-2B — escalares Pulse | concluído e publicado | `milestone/pulse-query-coalesce@af2e21e678e60eb135967169598f82549157c504` → `main`; código `a48d87c`/`e5063ea`, ratchet `af2e21e`; revisão Nexus `hof_e224557b3aba4ef8baed3bddc1c27de1` concluída/PASS | `coalesce`, `string_split` e `size` tipados; parâmetros e incompatibilidades recusados antes de stream/efeitos; eager errors, falsey, null, promoção numérica e Unicode cobertos. Corpus digest `5ad93542b86cf5cc335d9334f05d1d6c8f5beddbcb15e812c1ba7dc0c224020d`, 69/26 entries, raw 61/26 e 18 débitos. Auditoria local 226/226, corpus 45/45, revisão Nexus query 373/373; Ruff configurado PASS e dívida TRY/I/BLE/format reduziu sem novo arquivo |
| M-PULSE-2C — CASE e subscritos | concluído e publicado | `milestone/pulse-query-case-subscript@bf88462` → `main` (`863ec04` linguagem + `9787d6e` ratchet + `045a8c2`/`bf88462` hardening); revisão Nexus `hof_e3b5f54f3e2344949286b9768456df3e`; correções `hof_a1b17d76f49940dea1d0b00b609b985a` e `hof_01b133a255454f4c8ed84026093e8d10` concluídas/PASS | CASE searched/simple eager e com promoção determinística; lista 1-based/negativa, map-dot e composições nested; parâmetros nested são bindados antes do stream; colisões case-insensitive `a`/`A` recusadas e cache distingue `true` de `1`. Range conhecido é recusado mesmo em plano zero-row e CASE não recusa falsamente subscript de chamada escalar. Corpus digest `d9095a0fec35f834605c274f94bf1b2bad9ce8b6df144d22f58d3f73a5706bae`, raw 64/23 e 15 débitos. Suítes query/corpus/API saíram em `exit 0`; corpus `--check`, seis probes independentes, Ruff default e diff-check PASS |
| M-PULSE-2D — `label` e `timestamp` | concluído e publicado | `milestone/pulse-query-label-timestamp@a1846c3`; `0a7509f` label, `e74e89f` timestamp, `eaf413e`/`a33b09a`/`89e3494`/`a1846c3` hardening e `9fe3cee`/`cd7b7c1` ratchet; revisão Nexus `hof_7b8a913b11644a4c98ee129ce7ae2b85` concluída/verificada/PASS | `label` devolve o nome físico exato de node/relação e NULL, com recusas pré-stream; `timestamp` cobre T/espaço, Z/offset/naive/date-only/micros, identidade/NULL e composições conhecíveis, sem antecipar agregados. Auditorias independentes: label PASS e candidato final PASS. Query 996/996, corpus 45/45, API pública selecionada 147/147, revisão Claude API 615/615 + 1 skip, `--check` digest `75622dfe057ca446d200c91ad1041718bd15058d99e222948f3aae339901dfa0`, entries 69/26, raw 66/21 e 13 débitos; Ruff/diff-check verdes |
| M-PULSE-2E — `UNWIND` e map batch | concluído e publicado | código `4fc8ca5`; branch `milestone/pulse-query-unwind-map`; revisão Nexus `hof_9e5978356c68454ca7303e13b62b61c0` concluída/verificada/PASS | Source leading streaming com carrier fail-closed, mapa destacado, alias read-only, resolução tipada e duas formas estritas: `RETURN` ou um `MATCH` + um `SET`. I67/I68 usam seek correlacionado em PK limpa e fallback correto em índice ausente/dirty/stale; budget é contado uma vez e recusa tardia não libera escrita parcial. Corpus digest `836d55ad41bb617f3e71cf788ca164b0eca9c038964c63ec6542e87184e6f1e9`, raw 69/18, dez débitos e entries 71/24; query 1.019/1.019, gate focado final 68/68, API pública selecionada, Ruff e diff-check PASS; formatter sem dívida nova (250 arquivos contra 251 na base). Auditoria independente final PASS |
| M-PULSE-2F — `WITH` não agregante | concluído e publicado | código final `5b32e8e`; branch `milestone/pulse-query-with`; revisão Nexus `hof_89b6fa0a5aa243d7b314b71a72782ebc` concluída/verificada/PASS | Projeções sequenciais substituem o escopo e aplicam cada `WHERE` depois do estágio; I06/I07 preservam snapshot pré-write, piso zero, fallback nulo e atomicidade. Compatibilidade posicional do AST e portas analyzer/planner são fail-closed; alias descartado não pode ser reutilizado. Corpus digest `33254881effa0b8325d351c0732ec4e23a260e0d9cd78ed83de75dc9f6ba1b04`, raw 70/17, nove débitos e entries 73/22; query 1.060/1.060, `test_with.py` 41/41, corpus 45/45, fronteiras públicas selecionadas, Ruff e diff-check PASS; formatter 250/250 contra a base; duas auditorias independentes PASS |
| M-PULSE-2G — node scan polimórfico | concluído e publicado | código final `6e4f1d5e91878395f9736a95b855296f69e2e248`; branch `milestone/pulse-query-polymorphic-node`; handoff Nexus `hof_a28be7ad2f584fd693c3ef9cbaec75e8` concluído/verificado/PASS | Um único `AllNodesScan` une somente node tables e mantém filtro, agregação, `DISTINCT`, ordem e janela globais. A visão transacional é owner-only; propriedade ausente lê `NULL`, conflito de tipo recusa pré-stream e o DTO destacado `{label, properties}` não expõe identidade. Corpus digest `ac19e6735a90e5fe9831fdca67a80de1a3f4fffadd54b81151d9e343a7bd0d7a`, raw 71/16, oito débitos e entries 80/15; exatamente 1 probe/7 entries mudaram, com I01/I02 e os demais gaps intactos. Query 1.088/1.088, dedicado+corpus 73/73, fronteiras públicas 158/158, `--check`, Ruff e diff-check PASS; formatter 250/250 contra a base; três auditorias independentes PASS |
| M-PULSE-2H — endpoint inference tipada | concluído e publicado | código `a516c64`; formatação do código novo `42078ca`; branch `milestone/pulse-query-typed-endpoints`; handoff Nexus `hof_8ceb3eb19622472bbd50a83c81921015` concluído/verificado/PASS | I01/I02 planejam somente a forma bounded `MATCH (a)-[r:TYPE]->(b)` por `NodeScan -> TraverseRelationship`, inferindo source/target do `from_table`/`to_table` declarado. Relações paralelas, `NULL`, filtro I02, owner-only, rollback, wrong-kind, AST/análise injetada e os 16 tipos têm regressões; ranges escritos e demais shapes excluídos recusam antes do stream. Corpus digest `2fec52e0f033c3674aa8558fc5cca4aec05dacc7eae82e111bc2819864873e36`, raw 71/16, oito débitos e entries 82/13; somente I01/I02 mudaram. Query 1.141/1.141, corpus 45/45, focado pós-formatação 388/388 e fronteiras públicas 158/158; `--check`, Ruff e diff-check PASS; formatter 250/250 contra a base; três auditorias independentes PASS |
| M-PULSE-2I — named path decorativo | concluído e publicado | código `1c728cc` + hardening `edf6efd`; branch `milestone/pulse-query-named-path`; integrado à `main`; handoff Nexus `hof_a3e5645c9d4b437bbf75841a20872c13` concluído/verificado/PASS | Aceita somente `MATCH path = (a:A)-[r:TYPE]->(b:B) ... RETURN ...` com um hop tipado e nome jamais lido. Analyzer e planner repetem o gate sobre AST/análise fornecida; path projection permanece recusado pre-stream. Corpus digest `e792ded751eeffbe597a4e37d9110b30943e3d0fa69bd22027ad09778fc24f1c`, raw 72/15, sete débitos e entries 82/13; exatamente dois objetos raw e nenhuma entry mudaram. Dedicado 60/60, relacionadas 539/539, corpus 45/45, query completa exit 0, `--check`, Ruff e diff-check PASS; formatter 250/250; duas auditorias independentes PASS |
| M-PULSE-2J — upper bound implícito | concluído, verificado e integrado | código `4d1f62b`; branch `milestone/pulse-query-implicit-bounds`; handoff Nexus `hof_30fc02bfaca34762a46d90ffe143e162` concluído/verificado/PASS | `*`, `*..` e `*n..` recebem upper 20, como o contrato Pulse; máximo explícito 30 permanece. Analyzer e planner recusam AST/análise forjada, inclusive range oculto atrás de `hop_range_written=false`. Corpus digest `ded258c21bb0d58b93f36e3b641a13ee421f90a3b234951c0ebe871cb59811c4`, raw 73/14, seis débitos e entries 82/13; somente `unbounded variable length` mudou. Parser/limits/dedicado 201/201, corpus 45/45, `--check`, Ruff, format e diff-check PASS. A regressão global reproduziu somente as mesmas duas falhas de boundary já presentes na `main`, ambas no import `datetime` de `query_engine.py`, arquivo não tocado por M-PULSE-2J; a correção fica registrada como estabilização baseline separada, sem atribuí-la ao milestone |
| STAB-GRAFX-Q1 — boundary pura de `timestamp()` | concluído, verificado e integrado antes de M-PULSE-2K | código `e997a90cb4cc9969d06e133d7e5ea3eca949e266`; merge `main@6b2ef08c6555a6b6441de931012d67ea1ed3a263`; branch `milestone/stabilize-timestamp-boundary-final`; auditoria Nexus `hof_023c811279a045a780cf496417bdb3ac` concluída/verificada/PASS | O import de `datetime` e as duas constantes de mecanismo saíram do core puro; a conversão privada agora usa somente inteiros, calendário Gregoriano e semana ISO, sem alterar allowlist, API, Clock/cancelamento, corpus ou taxonomia `GrafxPlanError`. A superfície efetiva do baseline foi preservada, inclusive semana compacta de dez caracteres, fração vazia antes de zona e normalizações de offset. Boundary 187/187, timestamp 93/93 e arquivo completo do engine 278/278; duas auditorias independentes, 5.238.843 comparações diferenciais, 359.964 datas e 249.975 semanas ISO tiveram zero divergência/exceção crua. Ruff e diff-check passam; o delta tem zero linha tocada pelo formatter, enquanto o check de arquivo inteiro reproduz exatamente a dívida anterior (42/42 e 45/45 hunks). A suíte global adicional não era gate deste lote e foi interrompida sem falha observada em 19% para ser acumulada com os próximos incrementos, conforme a estratégia de regressão longa acordada |
| M-PULSE-2K — `OPTIONAL MATCH` root de um nó | concluído, verificado e integrado | código `495fa412a12d79d8600df63d0ebe239cd1da551b` + correção estritamente cosmética `f26952a7ae5c1e78b9f44b205cf19bf5e91fce12`; merge `main@1d3f2df52bd4ee7f84318018f8ed76f1785baa74`; branch `milestone/pulse-query-optional-match`; auditoria funcional Nexus `hof_6f9336dc8960498db0e426a65a6f45a2` e auditoria delta final `hof_ad58aaf72f3b492682a4b5c1a833a86b` concluída/verificada/PASS | A única forma admitida é a primeira cláusula `OPTIONAL MATCH (v:Label)`, com variável nomeada, um label, nenhum mapa inline, relação, path ou pattern adicional, seguida apenas do `WHERE` e subconjunto de `RETURN` já suportados. Sem match produz exatamente uma linha com `v = NULL`; com match não acrescenta linha nula; `v.prop`/`label(v)` propagam `NULL`, `count(v)=0` e `count(*)=1`. Parser, analyzer e planner repetem o gate inclusive para AST/análise fornecida; snapshot, RYOW, rollback e budgets permanecem cobertos. Query completa 1.326/1.326, fronteiras públicas 1.439/1.439, dedicado 42/42, revisão funcional adicional 229/229 + 544/544 e validação delta 317/317. Corpus raw 74/13, cinco débitos, entries 82/13 e digest `db3802aa449dc6a0204680c822027f8a8c6b0eeddef71afc350528b3c2d89e05`, com exatamente um objeto raw alterado. Ruff e diff-check passam; o único hunk novo de formatter encontrado pela revisão foi fechado antes do merge, deixando cada arquivo com dívida exatamente igual à base. A regressão global longa segue acumulada para o checkpoint combinado, sem falha observada atribuível ao lote |
| M-PULSE-2L — convergência da autoridade pública | concluído, verificado e integrado | Pulse Core `milestone/grafx-query-authority@f602c7cc2f6a9f5ef446d4c991309196bd4667c7`; Grafx `milestone/pulse-query-authority@6ae77177368207cabeeafaaa7d434a7eefd70640`; merge `main@c5ba88985d93d42fda64e6becc95bda920571fc1`; handoffs Nexus `hof_04152636362247d4b8b95a004ede89d1` e `hof_2183d449ad444ab9961f1ff3ef3ab795` concluídos/verificados/PASS | O Core recusa `CALL`/`YIELD` pós-root como `unsupported_operation`, preserva blacklist como `unsafe_cypher`, taxonomia de `CALL` root, masking e allowlists internas. O freezer aplica NFKC somente aos probes raw públicos e prova por AST que o validador consulta a autoridade nomeada; mutantes equivalentes que a substituem ou esvaziam são recusados. Corpus 46/46 e `--check` PASS; 97/97 entries idênticas, exatamente três probes alterados, engine 76/11, contrato 73/14, erros 10/4, entries 82/13 e apenas `UNION`, relação sem tipo e path projection em débito. Digest `905b29bdb503d77c9ad56878d50586866e1c013e31a00dfbd8793bf9293c484c`; Core 65/65, Ruff/format/diff-check PASS e zero dívida nova. O único blocker da auditoria local — digest antigo na especificação — foi corrigido antes do commit imutável e revalidado |
| M-PULSE-2M — `UNION` binário simples | concluído, verificado, publicado e integrado | código `e71672f5445bed5ce40066e4a1e286ad5ad9fbe1`; merge `main@e90ea642f28261589ba47045cfc82d7f53faceb2`; branch `milestone/pulse-query-union`; handoff Nexus `hof_14d5d5fd05fc42efbb1746995ec0c9f0` concluído/verificado/PASS | Exatamente duas branches top-level read-only terminadas em `RETURN`, mesma aridade, nomes da esquerda e distinct global. Tipos iguais, `NULL+T` e `INT64+DOUBLE -> DOUBLE` são resolvidos antes do stream; entidades/path e tipos incompatíveis recusam. Um bind/transação/snapshot/context serve às duas pipelines; `UnionRows` conta intermediários e o result budget é aplicado após o distinct. `UNION ALL`, chaining, nesting, writes/DDL e `OPTIONAL+UNION` permanecem recusados em parser/analyzer/planner, inclusive em árvores/análises fornecidas. Dedicado 107/107, regressão ampla query/API 100%/exit 0, gate Claude 349/349, corpus e `--check` PASS, Ruff/format/compileall/diff-check PASS; três auditorias independentes sem blocker. Corpus final 97 entries, raw 77/10, contrato 73/14, dois débitos, entries 82/13 e digest `8963b64ab073d13f84d216cd58ce7f1c683c74b1a1e8dfdcd4897c3c6fd8002f`; exatamente o probe `UNION` mudou |
| M-PULSE-2N — relação sem tipo de um hop | concluído, verificado, publicado e integrado | código final `8a917395092b7d9c593460f63c2e1ea91e7f64cd`; merge `main@21060edbf3682ae92d76cd7e5c37e6775e14f0d0`; branch `milestone/pulse-query-untyped-relationship`; handoff Nexus `hof_9698ae9ce6574df881871be6bf98ede4` concluído/verificado/PASS | Admite exatamente `MATCH (a:Decision)-[r]->(b) RETURN a.id`. `TraverseAnyRelationship` enumera por `table_id` todas e somente as tabelas relacionais com `from_table == Decision`, valida cada endpoint no planner, preserva relações paralelas e multiplicidade entre tabelas e usa um único child/contexto/transação/snapshot/admission budget. Zero tabela compatível retorna vazio; range escrito, maps, path, `OPTIONAL`, `WITH`, writes, múltiplos patterns, outras direções e a forma dentro de `UNION` continuam recusados, inclusive com AST/análise fornecida. Corpus final: 97 entries, raw 78/9, contrato 73/14, um débito (`path projection`), digest `905aa0baeb0d78d61858a1d4b5dc2da44003b966a065cbae2994340f9e83bb1`; somente o probe raw de relação sem tipo mudou. Dedicado 71/71, corpus 46/46, query/fronteira pública 1.559/1.559, Ruff e diff-check PASS; três auditorias independentes e a revisão final Claude sem blocker |
| M-PULSE-2O — projeção exata de path | concluído, verificado, publicado e integrado | código final `9118b7115329a3d9218b7a5532c461ab0d2ccdc4`; merge `main@7248caf6747cab82f4c0da173abe36b94a7860a7`; branch `milestone/pulse-query-path-projection`; spec pré-código `9458d6f00cc6a6cfcd5bc718fc58eeb45382786d`; oracle `hof_baca7724b827446ebc78c4a1f27758ab` e revisão delta final `hof_0ddbaa8ea5214a3a9ada8163c98ddbd4` concluídos/verificados/PASS | Admite exatamente `MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN path`. O valor nativo destacado preserva `_NODES`/`_RELS` como tuples, ordem de schema, identidades inteiras opacas, correlações, zero/uma/paralelas, owner-only, rollback e budgets; o provider M-PULSE-6 fará apenas tuple→list e os rewrites/envelopes Pulse. Path genérico, multi-hop, filtros, aliases, clauses extras, `WITH`, agregação, mutação e `UNION` continuam recusados. Colisões `Decision._ID/_LABEL` e `supersedes._SRC/_DST/_LABEL/_ID` recusam no planner antes do stream, enquanto `_from/_to` permanecem endpoints físicos. Corpus final: 97 entries, raw 79/8, contrato 73/14, zero dívida, classificações 82/13 e digest `b29334edf6e7c1e6b9419a4f3add84ede4baad94fdeaecb0c679261a78f241cc`; somente o probe raw de path projection mudou. Candidato funcional 1.950/1.950, SHA final 140/140 dedicado, 600/600 regressão relacionada e gate independente 187/187; corpus `--check`, Ruff, format, compileall e diff-check PASS. A revisão inicial `hof_99fa41ce71fe49f6b0a5830d16826cfc` encontrou o blocker das chaves e o hardening final o fechou sem widening |
| M-PULSE-2O — compatibilidade do limite terminal do Pulse | concluído, integrado e revalidado | Grafx `main@1bbb839`; código `55e025e` + hardening `1bbb839`; origem preservada em `milestone/grafx-mpulse6-path-limit-claude@b3fd6e4` | A projeção literal de path admite opcionalmente apenas `LIMIT` literal inteiro não negativo. `SKIP`, bool, negativos, parâmetros, expressões, aliases, filtros, `DISTINCT` e `ORDER BY` continuam recusados. `LimitRows` permanece no plano e é aplicado pelo engine antes do orçamento público; 166/166 testes do arquivo dedicado passaram após integração. O mesmo código participou do gate Pulse final de 4890/4890 |
| M-PULSE-3A — propriedades de node | concluído e publicado no branch Pulse atual; helper ainda inativo | Pulse Community `milestone/grafx-mpulse3-node-properties@4aae27eca9c0a2d1d14a3334b03e6c57976dea75` → `feature/v0.3.3`; revisão Nexus `hof_cad849ee19e542eb862930f64054724d` concluída/PASS | labels desconhecidas retornam vazio sem tocar backend; label conhecida ausente/wrong-kind e DB fechado falham tipado; ordem de catálogo, snapshot, reopen e fronteiras públicas cobertos; 8/8, Ruff default/TRY/I/BLE e format PASS. A resolução `board_id → Database` fica no provider/composição, sem ativação parcial |
| M-PULSE-3B — layout lógico de relações | concluído e publicado; provider ainda inativo | Pulse Community `milestone/grafx-mpulse3-logical-relationships@c4b1f37ad3a4cd08a1e2f5249db25c33ddbecd45` → `feature/v0.3.3`; código `067b82c` + hardening `c4b1f37` | Manifesto fechado e imutável de 16 tipos/69 pares/69 nomes, codec bijetivo e reverse pelo manifesto; introspecção valida kind/endpoints e oculta nomes físicos. Unknown/collision/mismatch, shapes malformados e representação hostil falham tipados. Gate focado 15/15; regressão selecionada completa 146/146 contra Core limpo `ab61b9a`; Ruff/format/diff-check e duas auditorias independentes PASS. DDL/bootstrap, ativação, query rewrite e os gaps de supersedence continuam explicitamente fora |
| M-PULSE-3C — manifesto e bootstrap do schema atual | concluído, verificado e publicado; provider ainda inativo | Pulse Community `milestone/grafx-mpulse3-schema-bootstrap@7e126a7130090c00891f8d1d35bd44819afe7a7a` → `feature/v0.3.3`; Core pinado `ab61b9a785f2018312fc91541a580877fd068bbb`; auditoria Nexus `hof_7240f7ad38f64538adc5b500bd3ea1a7` concluída/verificada/PASS | Schema `0.5.0` materializado em 11 nodes de 44 propriedades, `BoardMeta`, 69 relações e 11 spaces únicos 384/cosine/normalized=false/float64. Preflight fail-closed precede qualquer write; ausentes são criados numa única transação, o catálogo é recapturado/validado e somente então `BoardMeta` é gravado em transação separada. Fingerprint lógico canônico `4a7b425bf4b8c4864be633c1a87f034e5f7f641019dc029015b7d3ca786deb81`; no-op preserva catálogo/txn/LSN/WAL e bytes. Gates M3C 33/33 e regressão selecionada 73/73 no Core correto; Ruff/format/diff-check focados PASS; outputs Kuzu têm digest `18a8b1a1b9459d92d61670d734087a4212af29fa4039b0825d6e966ffa181e0e`, `kg.py`/`composition.py` mantêm os blobs congelados. Staging dos 92 DDLs mediu aproximadamente 101 s neste ambiente: risco de performance registrado para otimização posterior, sem alterar o gate funcional. ALTER/upgrade, provider, rewrite e paridade vetorial permanecem fora |
| M-PULSE-3D — rebuild do predecessor `0.3.12` | concluído, verificado, publicado e integrado; provider ainda inativo | spec Pulse Community `milestone/grafx-mpulse3-schema-evolution@703ad83c43b286e7c90fe2b0a29de0982929d4de`; código final `237f3bf7fa2a65a108db4f558932d429ec6696ce` em `milestone/grafx-mpulse3-schema-evolution-impl` e `feature/v0.3.3`; revisão Nexus final `hof_3df4987f6eef4611bfd9486409b91227` concluída/verificada/PASS | Reconstrução fora do lugar para candidato durável não vinculado, sem mudança no formato físico Grafx, `CATALOG_FORMAT_VERSION` ou upgrade in-place. Fonte em snapshot inerte; 11 nodes, 59 relações predecessoras, duplicatas/paralelas/self-loop, NULLs e vetores são preservados; dez relações novas nascem vazias. Catálogo, fingerprints, contagens e 161 índices são provados hot+cold; marker/commit ambíguo, checkpoint/recovery, close/lock, matriz finita de falhas e no-op zero-write são fail-closed. Gate rápido `122 passed, 1 deselected`, suíte completa real `122 passed` no mesmo código de produção, regressão M3A/B/C 56/56 e checks estáticos PASS. O único blocker factual da primeira revisão — `PermissionError` cru em `Path.exists()` — foi reproduzido no SHA antigo, corrigido e provado por teste discriminante no final |
| M-PULSE-4 — paridade vetorial | concluído, verificado, publicado e integrado | Pulse Community `milestone/grafx-mpulse4-accepted@d3ef4afdf263e7b6da70b6705b31950cfe07986e` → `feature/v0.3.3`; evidência não pública `milestone/grafx-mpulse4-vector-recall@fcfbf215b151aac4603978e02900ef323cb1b898` | Matriz V1–V7, exact/ANN, filtros, ordenação, rebuild/churn e cold reopen fechados. Gate final 56/56; recall@10 público `0.9546875`. `Alternative`/`Assumption`: 8192 linhas e 128 commits cada, build/ingest `1279.6239901 s`/`1415.9925527 s`, persistido `794624 B`/`802816 B`, verify limpo e cold/reopen 10/10. Artefato SHA-256 `e010ef6fb4a46b9e7a7bb770c9d4f6007b5b1af6415efb9952e3b7c369a0bede`; sem SLO; auditoria independente PASS |
| M-PULSE-5 — scan físico bounded-memory no Grafx | concluído, revisado, integrado e revalidado | Grafx `milestone/mpulse5-scan-v1@f132190207b562eb9794e7e4d752c41c9fdb7504` → `main`; validação pós-integração 254/254, Ruff, compileall e diff-check PASS | `Transaction.scan_rows_v1` percorre o snapshot read-only na ordem física estável, devolve DTOs destacados, preserva uma ocorrência por relação e usa cursor opaco, preso ao banco/transação/snapshot/tabela e de uso único. Continuação, writer concorrente, relações paralelas, token inválido e bounded-memory foram revisados sem blocker; nenhuma semântica Pulse, arquivo ou backup entrou no core Grafx |
| M-PULSE-5 — formato e transferência lógica no Pulse Core | concluído, verificado e integrado | Pulse Core `milestone/grafx-mpulse5-logical-transfer-core@098a346b0988d7b39e417e7de7ed8d57d06b9795` → `feature/v0.3.3`; revisão Nexus `hof_2373b761002c42aeb519e53617e56ec8` concluída/verificada/PASS | Formato `okto-pulse-logical-graph/1`, DTOs frozen, schema Board/Global representável, mapping propriedade→space, geometria vetorial, identidade tripla de layouts, codec canônico incremental, fingerprint multiconjunto, ports e transferência candidata neutra. Corrupção, não-finitos, wire/features, schema-record, batches e certificação cold-reopen falham tipados na matriz finita. Validação independente final: 318/318, Ruff, compileall, diff-check, remoto exato e auditoria de fronteiras 4/4 PASS. Adapters, arquivo atômico e round-trips físicos permanecem no Community, como já congelado |
| M-PULSE-5 — arquivo lógico atômico no Community | concluído, publicado, auditado e integrado no milestone M5 | Pulse Community `milestone/grafx-mpulse5-logical-artifact@cb74da0f135cf8429cd9c35f399cd10ed0bd7ed6` → integração `c980737126118f44115fe199e17cec1ea6371c0e` | Publicação por arquivo temporário, flush/fsync, verificação e replace atômico; matriz congelada `success/write/fsync/verify/replace` 5/5, geração anterior preservada nas falhas e nenhuma operação posterior ao replace; auditoria PASS |
| M-PULSE-5 — source físico Grafx | concluído, publicado, auditado e integrado no milestone M5 | Pulse Community `milestone/grafx-mpulse5-grafx-adapter@cb75ac60b74310a6af2de7f151286af1bfa307bd` → integração `c980737126118f44115fe199e17cec1ea6371c0e` | Snapshot Grafx único, projeção integral do schema fixo, `None → LOGICAL_NULL`, mapa temporário SQLite `(node_table, record_id) → logical_key`, scan bounded-memory e cleanup em sucesso/falha; contrato de endpoints `success/scan_failure/dangling_endpoint` 3/3 e checks estáticos PASS |
| M-PULSE-5 — sink candidato Grafx | concluído, publicado, auditado e integrado no milestone M5 | Pulse Community `milestone/grafx-mpulse5-grafx-sink@445043666530862300221783ece480e63c7086ac` → integração `c980737126118f44115fe199e17cec1ea6371c0e` | Geração nova e não vinculada, schema esperado exato, recusa de propriedade ausente, batches transacionais, checkpoint, close, cold reopen, `verify("all")`, contagens/fingerprint e abort seguro sem tocar a geração anterior; falhas de import/checkpoint/reopen e checks estáticos cobertos |
| M-PULSE-5 — backup/restore neutro streaming | concluído, publicado, auditado e aceito na matriz final | Pulse Community `milestone/grafx-mpulse5-integration@6fe93a6e2586decc94081778f4f2bbb7425c07a0` → integração final `08e4fa71dce08d3d3a99929ed0c68a44fc260631` | Backup consome um único snapshot em batches, fecha-o antes do manifesto/verificação/replace atômico e não materializa o grafo; restore lê um único handle verificado para candidato novo e não vinculado. Revisão independente e 5/5 testes focados PASS; D3 `[ladybug,grafx] × [clean,corrupt]` fechou 4/4 na matriz final, preservando a geração anterior |
| M-PULSE-5 — adapters Ladybug Board/Global | concluídos, publicados, auditados e integrados no milestone M5 | código `b7b76acd3912041b973cc5691c946ed20d887ab6`; handoff Nexus `hof_2a6e059c010049a9b218345e8db663b7` concluído/verificado/PASS; integração `c980737126118f44115fe199e17cec1ea6371c0e` | Schema e endpoints físicos exatos; Board 11 spaces/9 HNSW e Global 4/4; tipo/coluna/métrica/índice extra fail-closed; cold RO byte-idêntico; `graph.lbug`/`discovery.lbug` sem rename; snapshots e handles cold têm cleanup retentável. Gate Ladybug 92/92 e composição M5 111/111, sem blocker independente |
| M-PULSE-5 — integração e aceite final | concluído, verificado, publicado e promovido | factories `f73cdf4e1e5a64c48e285f514f27d16a2d913853` → `a4a5ec11bc20490fef7395066647a2152651291f`; matriz `d61f3a875952cf6fbf9bb4be177c9a98a98d83d0`; integração `milestone/grafx-mpulse5-integration@08e4fa71dce08d3d3a99929ed0c68a44fc260631`; Pulse Community `feature/v0.3.3@e73a446a954e039fb038f8fa329236b51104504a`; auditoria Nexus `hof_2b2ca8a48e4b472abe9f51e92c765138` concluída/verificada/PASS | Factories fail-closed compartilham a mesma autoridade Board 69/`graph.lbug` e Global 7/`discovery.lbug`. A matriz congelada A1/A2/B1/C1/C2/D1/D2/D3 passou 32/32, foi repetida 32/32 pelo Claude e a regressão consolidada passou 189/189; Ruff, Black, compileall e diff-check verdes. Core `098a346` foi pinado explicitamente para impedir import acidental do pacote instalado; árvore promovida e testada têm o mesmo tree hash `76b9bd8979f9345aa05d9c613716291eeb2ee95b` |
| M-PULSE-6 — fundação, resolver, pinning e primeiro lote roteado | checkpoint histórico concluído; supersedido pelo fechamento abaixo | Pulse Community `origin/milestone/grafx-mpulse6-integration@c524813`; commits integrados `00247fc..c524813`; resolver `559647b` + hardening P0 `6d0dc2` (origem revisada `b346ddc`); pool/pinning `fb21702` + correção terminal `8f54dc1`; Board facades `789c07c`, directory quarantine/restore `0092e6b` + inventário terminal `87641f3`, recovery offline `3231eba`, `init` neutro `03e96da`, transação roteada `f154cb9`, pin `okto-grafx[accel]==0.0.1` em `fb18d53`, lifecycle `171c6d2` e handshake `c524813`; Core inicial `8d2dbcb`; engine LIMIT originado em `b3fd6e4` e integrado em Grafx `main@1bbb839` | Board store/Cypher/schema/runtime/transação/lifecycle têm facades explícitas com snapshot imutável e revalidação física; o lifecycle não cria binding implicitamente e passou regressão independente 130/130. O manifesto Core/Community reconhece `logical_transfer`, eliminando os bridges do baseline. Quarantine/restore autentica a geração completa. Os gaps então abertos de Global, composição, provenance, conformance e regressão longa foram fechados no marco seguinte. O residual não corruptivo do receipt pós-rename permanece explicitamente rastreado para a auditoria de release; os bytes e o snapshot final permanecem seguros |
| M-PULSE-6 — bundle integral, recovery e certificação instalável | concluído, certificado e publicado nos milestones; M-PULSE-7 autorizado | Pulse Community `milestone/grafx-mpulse6-assembly@d1e988a` (`b4e27ff`, `f42d2e9`, `d1e988a`); Pulse Core `milestone/grafx-mpulse6-logical-transfer-manifest@ccc1f34` (`a9cf33d`, `ccc1f34`); Grafx `main@1bbb839` (`55e025e`, `1bbb839`) | Bundle único Board+Global compartilha binding store, resolver e pool; startup, CLI, restore, shutdown e rebuild não fazem fallback silencioso. Auditoria de interface 91/91; regressão roteada 232/232; conformance real Ladybug/Grafx 1/1; wheel Grafx `0.0.1` isolado com `[accel]`, `uv pip check`, bindings Board/Global e catálogos 81/11; recovery-only 220/220 aplicáveis; F13/AF21 25/25; gate curto 32/32; checks estáticos verdes e zero import Grafx no Core. A execução oficial completa no estado commitado fechou as 36 falhas diagnósticas anteriores e terminou em `4890 passed, 3 skipped, 13 deselected, 5 warnings` em `8150.11 s`; JUnit SHA-256 `d4d5bfc33cb9aa5d22a03e8f078dbd5df97dc94f6a7821a89b15737a33f14498`. A freeze pré-publicação do `uv.lock` Community ainda não é resolvível pelo índice enquanto `okto-grafx==0.0.1` não existir no PyPI; regenerar o lock a partir do índice imediatamente após a publicação conjunta, sem inserir URL/path local fictício |
| M-PULSE-7 — estabilização de performance A1/B/C | ratchet de entrada e F1 certificados; `same-10` CE-3 funcionalmente verde em `5b73890`, mas taxa `5,0875/s` ainda bloqueia matriz completa e run 10k | base Grafx `6d9b7a1`; A1 `5002a77c8d4e59e137890a45bcf61874398d2dcc`; B original `420ca4886e5e226aa10bf3a28a1396f98471faca`; código Grafx integrado `91a59c6806659cdd75d1b33500885f0c7354de6b`; candidato Grafx `origin/main@7b5a2ace36d0a300dc71dbdf97f8b9686d55fcba`; C-community `65d07b5` + hardening `c05d89d`, publicado em `origin/milestone/grafx-mpulse7-rollout@c05d89d`; ratchet Community `6595abdcfa788dfa2cc8da1a53ff96c378790531` sobre base `d44c82155e9884c556813ea96dec829be567c236`, branch `origin/milestone/grafx-mpulse7-ratchet-530df34`, pin Grafx `d39e27435171574ab6f03bc1d17672b26bf163b2`; manifesto físico/canônico `d1777bb26aee2feae5c8d5f4593840c08bdc37474ad6be4bdfe5334daedd0192` / `1e6e92fc3bae3b54d3052ca9055b7682a9d518927573e0ffcbfcbb4568cf9f93`; corpus físico/lógico `0997747ed8bb9172d05781a62e5f81e7694630b173aaa152ac9ea28daec9d13f` / `b29334edf6e7c1e6b9419a4f3add84ede4baad94fdeaecb0c679261a78f241cc`; ratchet 12/12 e auditoria independente 39/39; F1 autoral `d87a0c6520683b2d22929165136f718d14a1d795`, squash/integrado `main@2b8e9006218b9ccf013d914dfe98e32abaa5bdd3`, 61/61, matriz pura 312 células, auditoria independente PASS e handoff Nexus `hof_81cf7d9bc9d94fc7b76ad9639519cd2e` concluído/verificado; handoffs B `hof_7a5be05b5b644b609b92b5e8c09fdeea`, harness `hof_1ad44f9d92d04f58abbcfe4a6e316ba6`, fixture `hof_42a66b0033f0454ca5e12b3f27668141`, runbook `hof_93dbf437246040a3a83cb900f3a488ea` concluídos/PASS; C `hof_d301014276c148c59c4709a2c2a87950` cancelado após entregar o commit/mutantes, follow-up concluído pelo Codex | A1 remove o fan-out de page 0 entre tabelas com freshness fail-closed; B restringe o namespace e preserva contenção contra redirects; C usa uma view pública de catálogo por scope com invalidação DDL fail-closed e preserva operações de erro. Gates C: cinco mutantes mortos, 213 testes relacionados no autor, 98/98 independentes, 9/9 pós-fast-forward e checks estáticos verdes. Evidência curta (`94c692af…`): catálogo 94→12 e RAW `19.266,1–19.564,1`→`17.219,4 ms`, com mesmo digest/forense e 68/43 writes. Evidência oficial C (`655c0ec0a10d4ee272fe6e2e6eea0b584d165b8ae3645148a77cd16bd1c428c8`, 526.344 B): 60/60, 12/12, catálogo 94/`5.606,59`→12/`718,69 ms`, RAW `18.561,15`→`17.196,86 ms`, instrumentado `40.072,74`→`37.073,95 ms`, mesmas 72/43 writes, digest `c994255b…` e forense; referência Ladybug reutilizada porque o delta toca somente o adapter Grafx, razão `19,12x`→`17,72x`. O F1 agora autentica o instrumento, não publica ainda números da matriz longa. D5 `<=10x` e meta final `<=1x` permanecem dívidas do plano, não SLO do manifesto. Patch 2/A2/plan cache não são promovidos antes do gate completo; quarentenas e RUN #4 permanecem inalterados. O ratchet autentica somente as entradas; `same-10` precisa atingir o piso congelado antes da matriz CE-3 e do run 10k |
| CE-2 — reader pin por participante | concluída, certificada e publicada em `main` neste milestone | produto integrado `565ac37`; global 10.910/19; H1-H8.1 `bc305be2…`; reader-pin v8 47/47 `d0285b51…`; concorrência histórica PASS | Um único registro conservador por participante elimina publication/unregister por transação sem estreitar multi-writer/multi-reader. Rename/fsync/open `1/6/2 -> 0/4/0`; RAW 12 famílias `3.078,01 -> 2.363,90 ms`; razão contra Ladybug congelada `3,00x`. Long reader de 90 s, avanço após close, processo morto/TTL, recycle do WAL e verify hot+cold têm prova multiprocesso. ST-2 e CN-1 estavam fora naquele checkpoint; somente CN-1 recebeu autorização posterior, após o resultado CE-3 abaixo |
| CE-3 — matriz multiprocesso e diagnóstico `same-10` | contenção heap/OCC e regressão de header esparso fechadas; único gate vermelho é a taxa residual medida | candidato original `c2ca648`; CN-1 `40b2b43`; OCC-MB1 `9498554`; CE-3 `2feb579` + teste `61485d9`; falha pós-CE-3 SHA-256 `5b8df4d089a34ca91403a7c636c916474bb970e69a95b4754fe68ffd38f5a2a9`; fix `5b73890`; diagnóstico Nexus `hof_4b48e58b0e4b490bb235760f49daf02d`; audit final `hof_e890ffe523ac4023aee333a8b094c8e8`; novo artefato SHA-256 `73fd46b85f3e7a72ddec99603144ad9cc344286724819959384b610b94b38983` | As taxas `4,1287/s`, RAW `4,1403/s` e H8 `4,6319/s` permanecem históricas de `9498554`. `61485d9` falhou pós-barreira na operação 2 por header vetorial antigo, com cold verify limpo. Em `5b73890`, RAW/H8 passaram A 60/60, B 198/192, zero conflito/retry/refusal e verify live+cold; taxas `5,0875/s`/`5,1507/s`, ainda abaixo do piso `7,5/s`. Finalização passou e removeu scratch. Matriz completa bloqueada somente pelo gate de taxa |
| CN-1 — leasing durável de identidades | concluído, publicado, auditado e medido; não é o blocker residual | commits `40b2b43` + evidência documental `6fd26f9` em `origin/perf/w8-ce1-production`; desenho final `docs/architecture/CN1_IDENTITY_RANGE_LEASING.md`; auditorias Nexus `hof_8867688a5b9649d4a718fbf6eec91a63`, `hof_9e0acf09542a47bd9873a04fea7b0d51` e `hof_c03087626717449ea68b8fc595345fdf` concluídas/verificadas/PASS | `identity_lease_size=64` parametrizável; piso multi-tabela COW em `heap.dat/0`; subcommit privado `WRITE_PAGE+COMMIT` sob participant/lease/`COMMIT_SECTION`/WAL-tail; barrier/apply/publish antes do uso; primeiro OCC antes de consumo; a emenda OCC posterior remove o bypass do LSN e revalida interesses antigos incrementalmente; cache local burn-only; explicit `< floor` recusa e `>= floor` exige avanço durável; primeira extensão permanece atômica no commit do usuário; manager herdado após fork falha fechado. Gates: 8/8 identidade, 4/4 falhas, 4/4 multiprocesso, 449/449 transacionais não-multiprocesso e 523/523 de composição; Ruff/compileall/diff-check verdes. Medição autenticada: A 19→46 operações e page 0 fatal→1/807 commits de B; residual deslocou-se para a tail data page, como previsto |
| OCC-MB1 — baseline de materialização da segunda OCC | concluído, publicado, auditado e medido | `origin/perf/w8-ce1-production@9498554e1c59f49a946571e9f281e5533ab2904b`; autorização explícita do usuário; auditorias Nexus `hof_cb653c8a25fd479098fc31a5ac8415f9` e `hof_d8b8484183d24ee882daf12151854f37` PASS; gates 100/100 focados, 454/454 txn não-multiprocesso, 17/17 multiprocesso, 8/8 CN-1 failure/concurrency e 9/9 cleanup/relink | Primeira OCC usa conjunto congelado desde o snapshot; avanço CN-1 revalida esse conjunto sem bypass; somente novas localizações físicas medidas e não pré-staged usam o LSN durável atual. Interesses lógicos tardios e overlap/drift de página falham fechado. WAL/payload, durabilidade, readers e multiwriter não mudam. O `same-10` passou funcionalmente 60/60 + 187/187 sem conflitos/retries; deixou apenas a taxa como shortfall |
| CE-3 — invalidação bounded por delta WAL (produto) | publicada; correção de header esparso publicada, auditada e validada funcionalmente no Pulse | base `9498554`; produto `2feb579`; sucessor test-only `61485d9`; fix `origin/perf/ce3-bounded-invalidation@5b73890bdfddf9763c2b46514fc2c41c07ee30a4`; auditorias Nexus `hof_75a8a1e43bc24f1790b9a79d75d3749d`, `hof_7c7f3a4b3a9c470ea1d5886445cf233b` e `hof_e890ffe523ac4023aee333a8b094c8e8` PASS | Intervalo WAL limitado, classificação fail-closed e invalidação seletiva permanecem intactos. O fix acrescenta somente headers registrados quando há efeito de heap e declina para refresh completo se o inventário não puder ser provado. Red-first e mutation kill confirmados; 15/15 combinados, 7/7 pós-formatação, txn 476/476, index+vector 100%/exit 0 e checks estáticos verdes. M4 permanece dívida de cobertura não bloqueante. Primeira OCC, páginas pré-staged, WAL/durabilidade e multiwriter/multireader não mudaram. Próximo gate fixo: gargalo residual medido no H8; sem matriz completa antes de `same-10 >= 7,5/s` |
| CN-2 — barreiras de checkpoint fora do fence | implementado, auditado e medido; efeito local aprovado, gate agregado ainda vermelho | candidato medido `7acb9d869a7a6b9a533033309a3126f4de704f5b`; produto `e202324`; instrumento `c739733`; contrato de cauda `f872fbe`; R1b `b4d1fa1cff1de76f5ea9a5b21452e5f07ea4dea0897b73ed0274aa583d227e95`; R2 `fdf28373c49e11fa4b19420fee8e170c2ebc5a1a2e8bdfe22a49ffd08b4612b6`; auditorias Nexus `hof_3781b51dbb77417cb899c50d1775cd29` e `hof_8d1bcdb495ab497c91869f300d4688e2` PASS | A/B/C preserva autoridade, WAL e horizonte; 978 barriers ficaram fora do fence e três commits estrangeiros progrediram em seis checkpoints. A exclusão contínua caiu ~49% na mediana, mas o checkpoint total mediano cresceu ~38% e não houve ganho reproduzível de throughput. RAW/H8 foram `5,3178/4,1577/s` em R1b e `1,3218/4,5368/s` em R2, todos abaixo de `7,5/s`; verify live+cold e finalização passaram. Esta é a disposição atual e substitui o status de taxa histórico das linhas M-PULSE-7/CE-3 acima. Matriz completa/10k seguem bloqueados pelo piso `7,5/s`; a indicação histórica de que ST-2 ainda exigia autorização/F4 foi supersedida pela autorização e pelo fechamento registrados na linha seguinte. |
| ST-2 — dual descriptor revalidation + pinned-route fence | concluído, publicado nos branches de milestone e aceito no gate estrutural; `same-10` temporal permanece o próximo gate finito | Grafx `perf/st2-descriptor-revalidation@f0b55b7b6facc916118f342c774cb06e56bf17e3`; Community `perf/st2-pulse-generation@050ced9b79533d50efed453d53ed450984f75cf3` (produção `cea13b8`, alias test `050ced9`); Core `ccc1f345ece1db89a274cfdd634bd4da27028f63`; artefato PF5 SHA-256 `384a7722ff6772a2e89ca95225ab759ec5c7cab5af9939f405ef7b5ec2802aae`, 969.630 bytes; operação `c994255b...`; auditoria de segurança Nexus `hof_b69cf41505824beda52a25b29b37a8aa` PASS | `strict` continua default e `generation` é opt-in exclusivo Grafx/Pulse. PF5 `continuous`, 12 famílias x 5: `_still_names 424<500`, `os.lstat 4.137<8.000`, `os.stat 2.393->1.727<2.000`; `_read_page/read_fresh_page/write_page=323/146/96`, 123 bindings e 148 statements inalterados. Nenhum fence foi removido: Board/Grafx reutiliza a prova física do binding autenticado com o handle exato pinado/readmitido; genérico/Global preservam caminhada completa; CAS visível, path/page size, missing e alias real permanecem fail-closed. 59 testes focados finais e suíte relevante 253/253 passaram. `machine_idle_asserted=false`: aceite estrutural, sem alegação de throughput. |
| M-PULSE-7 — certificação somente por qualidade | concluído e auditado; `a05` PASS com receipt canônico, sem blocker funcional | Community `perf/st2-pulse-generation@23f9927ba5ac84424821b42aeac37528996b12e7`; Grafx `8cee82b9b92529f2ba767519c01c161f20876dce`; Core `ccc1f345ece1db89a274cfdd634bd4da27028f63`; manifesto físico/canônico `f4caf1236104e9bb410462b6e5ef3541bec04f4fd591b8e6870b4bb0939a70b1` / `3160a8cdde56feab41b425182e1c41f1b323aeb5b1e91a00cadffb2ff94d6a83`; queries `02c3b06dd71a0f66e04afbf64d33c203ef53d985d8912edb738e6519a6a29a7d`; receipt `D:\GrafxBenchEvidence\mpulse7-quality-20260902-a05\receipt.json` SHA-256 `f08c8be63ea2cd7abf6c6ffbb0deef5203169ed22c5b42a2c34ac290d90b77f1`; autoridade `34605c59f8d99894f40ffe7bb988eccedef8fea6d7cf3951d28f2126eaf859b4`; revisões Nexus `hof_cb1563e7aeed47c9a13eff733334ef66` e `hof_a411cbd75a984035b1d09d14f6cbb546` verificadas/PASS | `a01`–`a03` fecharam falso timeout, lifecycle do supervisor e threshold vetorial; `a04` isolou as duas divergências Board e a fixture v3 aplicou a correção híbrida mínima sem mudar Core/queries nem inventar ranking. `a05` terminou exit 0: dois traces de 10.000, três reopen/recovery por backend, 11/11 crash points, 19/19 Board, 97 Pulse, 21 famílias raw e quatro cenários receipt-bound por backend. Auditoria independente 27/27: JSON canônico + LF, hash recomposto, pins/worktrees e fontes carregadas autenticados, `descriptor_revalidation=generation`, fingerprints finais idênticos e zero crash/timeout/verify/divergência inexplicada. As três diferenças são somente os `generic_gap` congelados `I64`, `EXPLAIN_CONSTRAINT_ORIGINS` e `GET_RELATED_CONTEXT`. Após o run, worktrees limpos e zero processo órfão. Métricas temporais e os dois casos vetoriais vazios permanecem observações/dívida futura, não blockers de `0.0.1`. |
| RELEASE-0.0.1 — regressão Community pós-M-PULSE-7 | fechada por regressão ampla + repetição focada do único caso residual; auditoria instalável fechada na linha seguinte | Community `perf/st2-pulse-generation@159ef75328dc68493379d95b9f72826b660b8d84`; Core `milestone/grafx-mpulse6-logical-transfer-manifest@341bccdbb1232ee7ed6a9bcce380fdf9616c1600`; Grafx preservado em `8cee82b9b92529f2ba767519c01c161f20876dce`; diagnósticos Nexus `hof_1b1010756f4e49d59a5985b98992d4dc` e `hof_e31712f8b255489aa1834b5454be10b9` verificados/PASS; JUnit focado SHA-256 `9fdfa522c70202d93e03c04af51f1f0810fc331b22402206597d293be4bdd1f6` | O primeiro run amplo teve 5 falhas e 5.183 passes; todas foram fechadas sem remover asserções. O segundo teve 5.187 passes e uma única incompatibilidade de HEAD provocada pelo commit Core documental exigido pelo F16. O teste final mantém o runner fail-closed, prova ancestralidade imediata, diff exclusivo em README, `src` Core bit-idêntico e catálogo produtivo integral idêntico ao receipt a05; happy path e pins forjados continuam cobertos. O lote final passou 10/10. A conclusão `5187 + 1` é composição explícita, não uma terceira corrida ampla. Código produtivo, manifesto, watchdog semântico de 30 s e receipt não mudaram; upload PyPI permanece manual/conjunto. |
| RELEASE-0.0.1 — auditoria dos artefatos e smoke instalado | concluída; artefatos posteriormente publicados sem alteração | Grafx `perf/st2-descriptor-revalidation@744d450f5c199d06fa34e2f831dd443fbd15375d`; árvore produtiva `a00e55461daf93e3ab639790899079ad30a699b8`; wheel SHA-256 `3acc1bbc17bb4629caea3cbca60ff503c5cea1f8891cd829227a08eb5bc75070`; sdist SHA-256 `ccdc368cb73781f04512023f4cfa54aaf89f6e7dab0881a86112ad8bdff9359e`; JUnit Pulse instalado `0f2e6f7a170833a4b80de20d54ea7db3c0cd055cf2898ec9e4a3faade020a9ff`; evidência `821e42136ed9c8ea04671cb2bc93fbc6dc2668c14dc33e3c7d0b393bc7d27b3c`; auditoria Nexus `hof_a8c2280dc3be489792d4f51b54ba8a69` PASS | Regressão Grafx fechada honestamente por `11381 + 1`, com 485/485 no lote afetado após correção exclusiva do allowlist de teste. Build isolado, `twine check --strict`, inventário/metadata/licença e smokes core-only/`[accel]` verdes. O gate Pulse offline instalou os três wheels fora dos checkouts e passou 1/1 em 277,82 s, autenticando versões, origens, payloads, runtime Python 3.11, MCP, paridade de projeção, concorrência e crash-resume. Zero blocker objetivo aberto. |
| RELEASE-0.0.1 — publicação e reinstalação do PyPI | concluída em 2026-09-02; release pública verificada | `https://pypi.org/project/okto-grafx/0.0.1/`; wheel SHA-256 `3acc1bbc17bb4629caea3cbca60ff503c5cea1f8891cd829227a08eb5bc75070`; sdist SHA-256 `ccdc368cb73781f04512023f4cfa54aaf89f6e7dab0881a86112ad8bdff9359e`; venv `D:\GrafxBenchEvidence\grafx-0.0.1-release-audit-20260902\venv-pypi-a01`; lock Community `92ece5d` | A API pública confirma `okto-grafx==0.0.1` e os dois artefatos exatos. Download sem cache repetiu o SHA do wheel; instalação pública `[accel]`, `pip check`, origem/version/CLI, checksum nativo, NumPy, busca vetorial, verify, checkpoint e cold reopen passaram. O lock passou a conter URLs/hashes públicos sem mudar as demais resoluções. A ferramenta do usuário foi atualizada para Pulse/Core `0.3.3` + Grafx `0.0.1`; o diretório de dados existente permaneceu intocado. Nenhum segredo foi persistido. |
| Roadmaps complementares pós-Pulse | incorporados por referência; gate `0.0.1` cumprido e linha `0.0.2` executada pela rodada delimitada em `GRAFX_PERFORMANCE_ROUND_FINAL.md` | marco de código `feature/v0.0.2@b445736`; P0.2 originado em `perf/v002-p0-instruments-claude@9635146` e `perf/v002-p0-card-driver@e8a6be0`; P0.3 originado em `perf/v002-p0-census@7cdb204`, integrado em `8ab6a8b`, endurecido em `89cb893` e corrigido no censo frio em `b445736`; D-26 integrado em `7aa410d` + `59020a4`; D-01 em `b23bcbc` + `07dfb02`; D-04 em `2e6bbf9` + `f6e7531` + `873f419` + `bc3ede4`; D-02 em `49a9b03` + `e26af74` + `fb984a7`; D-03 originado em `e977285` + `7b152a9` e integrado em `1e2997e` + `13a5bda`; D-05 originado em `7751ea1` e integrado em `bd12a5c`; D-12 em `df09c2e` + `6f6b410` + `16fbc0a`; D-09 em `c3ef29f` + `69368c3`; `GRAFX_COMPLEMENTARY_EVOLUTION_PLAN_CODEX.md` (`GX-CAP-0..11`, `GX-AGENT-0/1`) e `AGENT_FIRST_EVOLUTION_PLAN_CODEX.md` (`AGENT-0..8`) | P0.0/P0.1/P0.2 e o ferramental P0.3 estão concluídos; sua execução real e P0.4 continuam apenas como evidência pós-backfill por decisão explícita do usuário. D-26, D-01, D-04, D-02, o R1 de D-03, D-05, D-09 e a contagem vetorial D-12 foram promovidos com gates focados verdes e sem mudar formato, WAL ou multiwriter/multireader; o limiar/R2 de D-03 foi explicitamente não selecionado. D-02 + D-03 amortizam pousos repetidos para `O(N+E)` dentro das quotas e preservam fallback canônico quando saturadas. Em D-03, `DELETE` paga a entrada fixa do fingerprint sem reencodar seu tuple vazio. D-05 reduz materialização no scan de incidentes, mas continua honestamente `O(|R|)`. D-12 elimina o walk `O(N)` repetido do planejador após o primeiro count cercado e preserva a atualização incremental do HNSW; D-09 reduz materialização redundante mantendo o caminho de verificação de imagens externas. P1 foi fechado pela composição documentada de regressão ampla, correção documental focada, 500/500 multiprocesso e cold read-only; P2 foi selecionado finitamente como `nenhuma`, pois nenhum gatilho congelado foi atingido. Ambos os roadmaps integrais continuam autoridades versionadas depois desta rodada: database-first governa ownership/ordem no core; agent-first preserva todos os requisitos e gates detalhados da camada opcional. Nenhum item amplia retroativamente o lote de performance; sobreposição usa o conjunto compatível mais estrito e conflito exige ADR explícita |
| 0.0.2 / checkpoint 1–9 — item 6, access path vetorial ponta a ponta | integrado, auditado e aprovado no checkpoint | `2642750` + `1299ad8` + `980ac52`; origem `perf/v002-ann-query`; código em `query_engine.py`/`vector_engine.py`; contrato, README, performance e regressões dedicadas atualizados | `VectorSearch` usa `VectorHit.ref` somente no shape bounded/unfiltered certificado no frontier exato do snapshot e no par físico tabela/coluna planejado; exact e approximate retornam o mesmo ranking do fallback e a camada de query materializa 3 hits contra 8 rows do `NodeScan` no discriminante. O oracle exact mantém sua validação exaustiva própria; o ganho ali é remover o segundo scan redundante. Nullable pequeno/`k` amplo, filtro, snapshot histórico/custom, budget intermediário e índice stale declinam para o child canônico; dirty owner mantém a recusa existente; refs ausentes/estrangeiras falham fechado. Hits capturam identidade e score uma vez, e LIMIT/SKIP nunca reduz o conjunto mutado por `DELETE`/`SET`. Cold reopen, `verify(all)` e a auditoria independente estão verdes. Sem mudança de beam/recall, formato, WAL, locks ou multiwriter/multireader |
| 0.0.2 / item 11 — vacuum MVCC v1 | concluído, auditado e publicado na branch | `75e799f`; ADR `docs/architecture/MVCC_VACUUM_V1.md`; revisão Nexus `hof_df837d11c96a430786856895c7f7c744` corrigida e verificada PASS | Vacuum manual/foreground/process-quiescent com capability requerida, floor heap-global monotônico, recusa retryable de snapshot reclamado, relink de cadeia e reconcile ACTIVE atômicos. Remove somente versões inline; overflow, truncagem e reuso físico continuam fora. Fault injection e 1.226 testes agrupados verdes no delta; quatro falhas históricas foram reproduzidas no baseline e nominadamente excluídas |
| 0.0.2 / item 12 — full-page WAL zlib v2 | concluído, auditado e publicado na branch | `24f2f63` + `d806652`; ADR `docs/architecture/WAL_PAGE_COMPRESSION_V1.md`; revisões `hof_44e3743219b2476a95ecc02d00ac0eb6` e `hof_b192c61ffcea4297852c7b2b8f00064b` verificadas PASS | Ativação explícita/one-way por capability `wal_record_v2`, transação de ativação integralmente v1, inflate limitado e semântica unknown-v2 fail-closed. Lotes com segment roll e imagens incompressíveis preservam v1. Amostra 50 linhas/3 páginas reduziu `31.510→7.966` bytes (`-74,72%`); gate agrupado 1.496/1.496 no delta |
| 0.0.2 / item 13 — codec NumPy byte-idêntico | concluído, auditado e publicado na branch | `b720f7e` + `d644dc3`; ADR `docs/architecture/NATIVE_PAGE_CODEC_V1.md`; parecer inicial `hof_fd26b1d96ce74dac80c9671e00a55999` e validação final `hof_19b2cca000214564832d7ff15cf77618` verificados PASS | Selector explícito `codec="numpy"`, por banco, sem `auto`; page format v1, WAL e concorrência inalterados. Caminho híbrido 16/96 slots, montagem canônica de `Page`, oracle único de recusa, paridade `u16` em NumPy 1.x/2.x, 3.000 mutações diferenciais e receipt processo/instância. Micro final: encode `3,06x`, decode `1,83x` em 200 slots/8 KiB; sem alegação end-to-end. Focado 556/556; gate ampliado fechou uma dívida imutável via 661/661 e reteve as mesmas quatro falhas transacionais históricas fora do delta |
| 0.0.2 / item 14 — hot paths limitados de query/heap/txn/vetor | concluído, auditado e publicado na branch | `9603115`; revisão Nexus `hof_47afe28f5f004696a473718eeca9d980` verificada PASS | Caches por `Database` limitados a 256/128/128 e sem autoridade de storage; bootstrap elimina device probes redundantes mas continua validando o header; CE-3 evita sync sem mudança; uma projeção ACTIVE por statement; retenção de catálogo só sob composição WAL-only; packing/decode e VEC-1..VEC-5. Sem mudança de formato/WAL/OCC/concorrência. Evidência vetorial: `1,47–3,19x` conforme componente/workload e ranking idêntico; ganhos de query permanecem estimados. Payload vetorial invisível/filtrado passa a ser auditado por verifier/scan, mantendo fail-closed para linhas admitidas. 21/21 mutantes, gates focais e checks estáticos verdes; 29 arquivos históricos seguem fora do baseline de formatação, sem reformat amplo |
| 0.0.2 / lote de escala 1 — recovery, storage e heap | concluído e publicado na branch | `c1d537e`, `9ee51f3`, `2be59c9`, `2afd876`, `210b685`, `c12e67c`; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Coalescência permanece exclusiva de replay originalmente page-only e inequívoco; replay misto é sequencial. Preflight passage/content-bound reduz decode sem pular validação; barreiras de dados precedem `commit.state`. Leitura control fundida preserva identidade strict; dirty candidates eliminam scan de frames e revalidam cada candidato. Diretório de extents usa hint defensivo e fallback canônico; default lazy de descritores passa a 256 com override. Evidência de componente: flush 8.192 frames cai a microssegundos, extent 200 tabelas `6,19x`, e cache de 192 artefatos elimina evicções. Nenhuma premissa multiwriter/multireader, OCC, WAL ou formato foi alterada |
| 0.0.2 / lote de escala 1 — índice incremental | concluído e publicado na branch | `8604661`; testes `test_registry_and_format.py` e `test_provisional_csn.py` | Gauge de tombstones deixa de varrer o índice por commit após uma semeadura exata; reopen/rebase/falha invalidam, métricas off não pagam. Comparação de efeitos preserva multiconjunto com `Counter` em O(K), incluindo recusa de duplicada e ausente. Micro de 1.000 mudanças `~33x`; 73 testes independentes e Ruff verdes. O Pulse 0.3.3 instalado usa o sink noop por default, então o ganho do gauge é estrutural para observabilidade, não alegação de throughput Pulse atual |
| 0.0.2 / lote de escala 1 — inventário ACTIVE | concluído e publicado na branch | `592fd22`; teste `test_index_manager_authoritative_facade.py` | O fast path com todos os ACTIVE relevantes registrados e definição integral exata não executa mais `list_files("index/")`; qualquer unresolved/mismatch usa um único inventário. A fresh certificate posterior continua a prova física pré-WAL. V1 opcional, v2 obrigatório, nonce/definição divergentes e catálogo estrangeiro permanecem cobertos; 9/9 focados e Ruff verdes |
| 0.0.2 / lote de escala 2 — PK, planos, catálogo e LIMIT | concluído e publicado na branch | `02e6f41`, `e0e18f8`, `855c9cf`, `3b72d4a`, `82bd176`; detalhes e evidências em `docs/PERFORMANCE_ROUND_0_0_2.md` | Fold incremental remove O(K²) de unicidade e reconstrói pelo redutor canônico em rewrite/rollback; plano preparado usa imagem imutável em vez de identidade Python; projeção ACTIVE vive somente na autoridade corrente; catálogo residente só atravessa same-token/own/CE-3 sem DDL; travessia 1-hop sob LIMIT usa endpoint index e short-circuit, enquanto operadores bloqueantes, shapes amplos e índice stale mantêm scan. Evidência de componente: PK K=1.000 `67,16x`, CAT-1 N=64 `~3,02x`, QUERY-2 E=2.000 `2,56x`. Sem alteração de multiwriter/multireader, WAL, OCC ou formato |
| 0.0.2 / lote de escala 3 — build, recovery, commit e checkpoint | concluído e publicado na branch | `c1f1a1f`, `b8eff8a`, `26baa86`, `46fa9fe`, `aaf72f2`; detalhes e evidências em `docs/PERFORMANCE_ROUND_0_0_2.md` | Diretório first-fit efêmero acelera geração vazia mantendo imagem byte-idêntica (`2,05x–9,43x`); recovery nativo faz um único WAL walk; stamps MVCC deixam `O(P×R)` por `O(R+A)`; commits somente de conteúdo preservam caches estruturais; checkpoint compartilha uma observação exata de reader horizon. Gates focais agrupados verdes, sem alteração de multiwriter/multireader, WAL, OCC, formato, visibilidade ou durabilidade. A alegação antiga de `1,11x–1,35x` foi retirada de `e0e18f8`: o ganho era do churn CAT-1 resolvido por `3b72d4a`, enquanto `e0e18f8` permanece hardening estrutural |
| 0.0.2 / lote de escala 4 — vetor seletivo, identidade registrada e estabilidade de seek | VEC-4/CAT-5/seek concluídos; EDGE transparente não selecionado após dois NO-GO | `c77d414`, `40552df`; pushdown `b236697` retirado por `030b39d`; replay `bf8ed4e` retirado por `4bcdad0`; seek `f5152de` + `5938a03`; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | VEC-4 reduz 64→4 leituras no caso focal sem alegar O(K). CAT-5 mede `5,8x` no allocator N=64 e `1,37x` no DDL. O pushdown EDGE podia suprimir erro/budget; o replay media `~3,6x`, mas violou A63 ao ocultar corrupção de uma passagem física posterior e permitir commit, portanto ambos foram removidos e o cliff permanece. A correção de seek elimina semântica dependente da presença de índice e preserva padrões posteriores seguros. Multiwriter/multireader, WAL, OCC e formato não mudaram; HNSW continua deferido até perfil/recall válidos |
| 0.0.2 / lote de escala 5 — observação WAL e inventário CAT-4 | dois NO-GO fechados; nenhum código promovido | base `22d9694`; revisão TXN-1 `hof_0dc74badf8504e09af643bd9ae4357bd`; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | TXN-1 media `2,71x` no componente, mas autenticava só o delta novo, não todo o plano físico de `read_from`, ocultava corrupção antiga/quente e mudava o budget físico de `read_bounded`; foi removido integralmente. CAT-4 por lifetime seria invisível a mutações de namespace de outro processo; o único recorte seguro, por uma `COMMIT_SECTION`, mantém O(N), tem esforço médio e ganho baixo no porte Pulse, portanto não foi implementado. As decisões encerram os alvos sem enfraquecer multiwriter/multireader, WAL/OCC, durabilidade ou fail-closed |
| 0.0.2 / lote de escala 6 — D-17 walks de índice sem DTO | concluído e publicado | `b7fb52d`, `82ea61b`; revisão Nexus `hof_1d4c091b7f1a49a99a8e7c44282eeabb` verificada PASS | Contagem/refs header-only compartilham a validação fail-closed integral; full walk permanece em verifier/reconcile/build. Micro final full/count/refs `166,87/33,20/65,98 ms` (`5,03x`/`2,53x`) e diferencial de 40.010 imagens sem divergência |
| 0.0.2 / lote de escala 7 — D-13H residência HNSW compacta | concluído e publicado | `eebc497`; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Bytes f32/f64 imutáveis por nó somente no HNSW+NumPy; Pure/APIs públicas seguem tuple e formato/WAL/OCC/concorrência não mudam. Micro 128×384: memória `7,59x` menor, build `1,48x` e busca `1,22x`; 121 focais + 18 NumPy e revisão adversarial verdes |
| 0.0.2 / lote de escala 8 — D-14 scoring em lote | NO-GO fechado; nenhum código promovido | revisão Nexus `hof_1b20013e41154a00b21d65af4153af42` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | O exact alcançável mede `1,26–2,04x` porque materializar tuples consome `99,5%` do lote relevante. HNSW precisa score escalar exato antes de qualquer entrada em beam/results; lote serve apenas para rejeições provadas. O teto otimista real ficou `1,70–2,18x`, somente em DOT e antes do custo de intervalos; cosine/Euclidean exigiriam provas próprias. Pela relação risco/ganho e ausência de benefício ao Pulse cosine, o alvo foi encerrado sem criar capability, alterar ranking ou tocar formato/WAL/OCC/multiwriter |
| 0.0.2 / lote de escala 9 — D-15(b) delta HNSW estrangeiro | NO-GO fechado; draft removido antes de commit | revisão Nexus `hof_52f3f82fe7f34175ab704d40a9bbd1e8` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Delta lógico vazio não prova ausência de efeito vetorial esparso. Aplicar `_note/_install` alteraria um HNSW já publicado enquanto leitores o percorrem; a cópia atômica segura do grafo mutável custa `O(N)` e consome o ganho `O(i)`. Reentrada do `VectorMath` pelo `begin` e topologia permanentemente dependente do histórico de cada processo completam o NO-GO. Nenhuma mudança em formato, WAL/OCC, durabilidade ou multiwriter/multireader foi aceita |
| 0.0.2 / lote de escala 10 — fronteira HNSW larga | concluído e auditado | `f1d1a75`; revisão Nexus `hof_cad8d1a0ae734da19db0903334cc5c3c` verificada PASS; evidência completa em `docs/PERFORMANCE_ROUND_0_0_2.md` | `_trim` usa score único + sort/slice; somente filtro ou exaustão com `N>=4096` recebe heap total-order, mantendo o caminho aproximado comum legado. A fronteira seletiva melhora `5,94x` em 4.096 e `33,09x` em 50 mil; build 512×64 melhora `1,074x`. Ranking, signed zero, callback order, stats e shape são diferenciais exatos; nenhum formato, WAL/OCC, lock ou princípio multiwriter/multireader mudou |
| 0.0.2 / lote de escala 11 — D-30 decode vetorial | NO-GO fechado; nenhum código promovido | análise de endpoint real registrada em `docs/PERFORMANCE_ROUND_0_0_2.md` | O corpo do vetor isolado acelera `20,98x`, mas ocupa só `6,4–12,1%` dos endpoints alcançáveis: teto total `~1,07–1,11x`, e `~0,07%` no build HNSW. A otimização local foi recusada por ser marginal; ganho material fica condicionado a um desenho contíguo de ownership/cursor, sem criar API paralela nem afrouxar validação |
| 0.0.2 / lote de escala 12 — projeção vetorial e escopo NumPy | concluído e publicado na branch | `cd32623`; revisão Nexus `hof_c5ee0c49b2ec43beb0406f77e6f8eebf` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | A projeção de resultados decodificados elimina validação Python por dimensão somente para tupla exata de floats exatos; consulta externa e qualquer tipo estrangeiro mantêm o caminho integral. Ganho medido: componente `6,71x`, `scan_rows_v1` `2,11–2,87x` e build HNSW NumPy `1,153x`. Os dois atalhos preservam erros, callbacks, warnings, ownership, formato, WAL/OCC e as premissas multiwriter/multireader |
| 0.0.2 / lote de escala 13 — hot paths imutáveis compostos | concluído na branch | `69f9cea`, `40e7514`, `60850b5`, `0fa3406`; detalhes e testes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Quatro ganhos simples precedem o próximo redesenho de recovery: finitude escalar sem redispatch NumPy; header walk de build `2,503x`; normalização automática por `TableDef` e memo bounded de proveniência (`~44%` no matcher); norma cosine HNSW exata por geração imutável (`1,350x` em buscas repetidas). Cache vetorial publica somente após score+norm atômicos, valida backing antes/depois e não retém órfãos sob remove/reuse concorrente. Nenhuma API existente, formato, WAL/OCC, durabilidade ou premissa multiwriter/multireader foi reduzida |
| 0.0.2 / lote de escala 14 — replay lógico comum em batch | concluído, auditado e publicado na branch | `ae8d01e`; revisão Nexus `hof_e04db78c505a4ea0922fc19e8b278d5b` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Page 0 é semeada e publicada no máximo uma vez por índice comum, mantendo `_apply_change` na ordem WAL e compondo os mesmos watermarks monotônicos. Mixed/RESET/rebuild/stale/unknown/vector preservam fallback integral. O draft reutilizável que podia limpar STALE foi rejeitado; a capability final é atômica, privada entre preparação/aplicação e coberta por regressão. Micro 500 efeitos `2,14x`; perfil público aponta checkpoint `~1,69–1,82x` e redo `~2,57x`; 13 focais + 136 regressões + 14 multiprocesso verdes. A tentativa seguinte de compartilhar exact-view em `executemany` foi fechada como NO-GO sem código: teto inseguro `26,6%`, mas a forma segura mudaria lazy/callback/freshness. Formato, WAL/OCC, durabilidade e multiwriter/multireader seguem intactos |
| 0.0.2 / lote de escala 15 — extent reservado, buckets quentes e ordem de publicação | concluído e auditado na branch | `da58471`, `d50eab2`, `90f0560`; revisão Nexus `hof_0b518924b36444a595988f6e0151a117` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | `insert_reserved` elimina a segunda leitura do extent com prova restrita à chamada/derived epoch (`1,362x`). O replay comum troca walks repetidos `O(K*N)` de buckets com ≥8 efeitos por preparação `O(N)` e lookups/first-fit bounded, sob tetos 16.384/65.536/16.384 e o mesmo `COMMIT_SECTION`; micros `7,88x` e `38,75x` em 250/1.000 efeitos no mesmo bucket. A regressão corrigiu ainda a perda de page-0-last introduzida pelo dirty-candidate set, mantendo flush `O(D)` e impedindo certificado à frente dos dados. Perfil público direcional: total `2,478 s`, checkpoint `0,477 s`, redo `0,228 s`. Diferenciais, fault injection, fences e multiprocesso verdes; formato, WAL/OCC, durabilidade e multiwriter/multireader permanecem intactos |
| 0.0.2 / lote de escala 16 — materialização seletiva no preflight de replay | concluído e auditado na branch | `28dd5d4`; 24 focais, 125 agrupados, Ruff/diff-check e revisão adversarial independente PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Todos os slots continuam estruturalmente validados e com `RecordRef` autenticado, mas somente alvos `(key, ref)` constroem DTO completo. O índice efêmero `encoded_ref -> key` evita alocação comum e busca interna linear mesmo sob refs compartilhadas; nenhuma view de página escapa do pin. Scanner focal 4.000 entradas/400 páginas/8 alvos: `2,76x`; formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 17 — metadados e buckets vivos | concluído e auditado na branch | `d47eaec`, `3657c47`, `5fc25f0`; revisões Nexus `hof_f9015f5ba1264be69404ed2427698444` e `hof_70a7620b5b35472aa889cde8faa22d52` PASS; 19 focais, 590 agrupados e 15 multiprocesso; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Derivados imutáveis são retidos por store, a travessia física funde walk/match sem perder validação estrutural e o pipeline transacional interno pode preparar um diretório efêmero limitado para buckets com ≥2 efeitos. Dois blockers adversariais foram fechados: override em instância e autoridade sobrevivente em contexto copiado. Chamadas diretas, customizações, RESET/rebuild/stale/retry e saturação usam o escalar; HNSW preserva o `commit` externo e reutiliza apenas os hooks físicos herdados. Estimativa focal `~1,70x` distribuído e `~9,16–21,04x` em colisões; baseline público anterior até `26df492` já mediu `1,53x` agregado, sem incluir este lote. Formato, WAL/OCC, durabilidade, writer lease e multiwriter/multireader não mudaram |
| 0.0.2 / lote de escala 18 — dirty tables e inputs float | concluído e auditado na branch | `3c847ea`, `00299ed`, `787a464`, `3888830`; correção de test drift `ea11621`; revisões Nexus `hof_a2d2ceffff124c77a48fa9e5c9287cfa` e `hof_c1cfada9144b44d7a7d764fe2482b131` PASS; 371 regressões agrupadas + 81 focais pós-correção | Intents append-only deixam de ser rescaneadas a cada operação: o cursor revisionado consome somente o sufixo e rebuilda em qualquer rewrite/rollback/replacement, com fallback integral para iteráveis não rastreáveis. Tuples exatos de floats exatos são snapshots imutáveis prontos; lists exatas são destacadas uma vez, e todo tipo estrangeiro/limite mantém o canonicalizador. A auditoria encontrou e fechou um bypass de profundidade exatamente no limite 64. O caso em memória de 500 itens mediu `1,49x`, mas o perfil em storage limita a contribuição desse trecho a `~2,2%`; o componente de snapshot mediu `17,96x`/`9,58x` em tuple/list e a família representava `~7%` da escrita. Nenhuma autoridade durável, formato, WAL/OCC ou regra multiwriter/multireader mudou. A alternativa de prova de identidade por intervalo foi posteriormente encerrada como NO-GO no lote 21 e não virou gate móvel |
| 0.0.2 / lote de escala 19 — decisões heap sob pin único | concluído e auditado na branch | `08c0197`, `526e882`; revisões Nexus `hof_627aebc2a59a4e7981052ef87eb8c1ae` e `hof_942f47dd6b06455fa4614b892587c777` PASS/GO; 369 regressões agrupadas | O append comum mantém o pin entre capacidade e inserção quando nenhum extent hint exige reparo; drift/growth continuam na ordem directory-before-row/relink. Extent read/write e reclaim-floor unem validação e uso de page 0, mas mudança de epoch ainda executa bootstrap e uma segunda validação operacional; planners device-fresh não foram tocados. No commit público focal de 100 linhas, pins totais caíram `1.176→876` e page-0 `606→306`; o componente reservado foi direcionalmente `~1,42x`, sem alegar ganho wall-clock estável no endpoint. Não existe cache/bundle de autoridade novo, e formato, WAL/OCC, writer lease, durabilidade e multiwriter/multireader permanecem intactos |
| 0.0.2 / lote de escala 20 — extent observado e exact hit sem releitura | concluído e auditado na branch | `51543a7`, `b9cfa10`; revisões independentes PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | O insert comum elimina a segunda resolução do extent com uma prova local revogada por mudança do derived epoch; criação inicial, stale-tail repair e growth preservam o floor já avançado. A unicidade PK reutiliza a versão imutável produzida no certificado exact somente com hooks canônicos, sem cache entre chamadas e sem contornar customizações. Commit focal de 100 linhas: pins `876→776`, page-0 `306→206`, `_find_extent 201→101` e `_extent_for 200→100`; duplicata focal lê cada hit uma vez (`100→50` em 50 recusas, `~1,09x`). O batch físico maior foi recusado por teto agregado `~1,034x` frente à superfície de risco. Nenhum formato, WAL/OCC, durabilidade, writer lease ou princípio multiwriter/multireader mudou |
| 0.0.2 / lote de escala 21 — identidade final no-follow | concluído e auditado na branch | `b5cff4a`; revisão Nexus `hof_a701a6b052134462be310aeb1bc78b0e` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | A prova obrigatória de namespace passa a devolver o `lstat` fresco do componente final para a comparação com `fstat`, removendo exatamente um `stat(path)` redundante por revalidação. O walk no-follow integral, o fallback com containment, os boundaries strict/generation, a detecção de replacement e recusas de redirect/junction/reparse permanecem. Teste estrutural `stat 1→0` por warm hit e nove discriminantes POSIX passaram no Ubuntu/WSL. STOR-1 foi encerrado por medição: máximo `1,65x` na contagem de provas e `~1,02x` agregado não justifica nova autoridade. Formato, WAL/OCC, durabilidade, writer lease e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 22 — quota única e scans quentes sem alocação redundante | concluído e auditado na branch | `7b5ce1f`; revisão adversarial independente PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | A contagem canônica que determina o COMMIT LSN é reutilizada pela verificação da quantidade efetivamente produzida dentro da mesma seção; qualquer manager/hook customizado conserva a dupla chamada anterior. Scans de bucket usam diretamente views readonly sob pin e o diretório quente deixa `page/slot` somente na tupla efêmera, sem cópias de DTO. Ganhos são pequenos e cumulativos (`~1,015x` e `~1,01x` como tetos dos trechos medidos); 80 testes selecionados e checks estáticos verdes. Formato, WAL/OCC, durabilidade, writer lease e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 23 — contexto de pin sem gerador | concluído e auditado na branch | `b964155`; revisão adversarial independente PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | O context manager privado/slotted reduz custo fixo de cada pin preservando aquisição lazy, dirty/identidade no unpin, BaseException/chaining, nesting, uso único, decorator e liberação de toda autoridade após sucesso/falha. Com todos os contratos, o micro mede `1,316x` no componente e tetos públicos modestos `~1,006x` read/`~1,003x` write; por isso nenhum contexto adicional vira alvo. 521 testes selecionados e checks estáticos verdes; formato, WAL/OCC, durabilidade, writer lease e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 24 — scores transitórios no cold-build HNSW | concluído e auditado na branch | `cdcf8c4`; revisões Nexus `hof_1fbc7385740a457cbed30c71ab3b6cd4` e `hof_7df9f6701ce04867aaeb773949f8c356` PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Reutiliza scores exatos de peers inalterados durante um único build e descarta todo o cache antes da publicação. Opt-in é da classe concreta Pure/NumPy; customizações não herdam. Caminho público padrão forçado mediu `1,55x`, com topologia, scores, stats e respostas idênticos sob churn; 165 testes agrupados passaram. O ganho só se aplica ao regime HNSW acima do threshold padrão, que não foi reduzido; HNSW durável continua fora sem protocolo de geração/LSN/atomicidade. WAL/OCC, formato, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 25 — descritor participant desbloqueado no `executemany` | concluído e auditado na branch | `429d192`; revisão adversarial independente PASS após blocker corrigido; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Reusa apenas o descritor aberto do lock file dentro de um batch; cada acesso continua adquirindo/liberando o lock do SO, e nenhum commit section, lease ou autoridade durável é amortizado. Scope thread-local, falhas/timeout/unlock incerto/BaseException fecham o fd e coordenadores sem capability mantêm o legado. A regressão pós-acquire impede vazamento de lock. Em três itens: um open e cinco acquires/releases, com interleaving real; 572 regressões passaram. Ganho conservador `~1,06x`, cumulativo e sem gate; WAL/OCC, formato, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 26 — scores HNSW alinhados sob adjacency underfull | concluído e auditado na branch | `8602abf`; revisões independente e Nexus `hof_157e6798ef814a778409b1aa57b72549` PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Mantém scores transitórios alinhados por posição através de unlink/reinsert, usa sentinela apenas para peer ainda não pontuado e resolve faltantes na ordem canônica somente no overflow. Falha intermediária não publica refresh parcial e mismatch cai no scoring integral. Em N=256/d64 Pure, `71.173→48.332` chamadas (`-32,1%`) sobre o lote 24 e `~1,20x` wall conservador; 94% dos trims de overflow usaram o cache. Pure/NumPy com churn preservaram topologia, scores, respostas e stats bit a bit; 167 testes agrupados passaram. Cache permanece efêmero/bounded e cai antes de publicação/falha; formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 27 — decode direto após tag VECTOR provada | concluído e auditado na branch | `bed6846`; revisão adversarial independente PASS; 485 regressões adjacentes e diferencial adicional de 40 mil buffers; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Depois de `_decode_tuple` provar a tag contra o schema, F32/F64 entram no mesmo decoder de corpo sem reler e redistribuir a tag. LIST/MAP, truncation, header/body bounds, offset e trailing-payload mantêm o caminho/erro canônico. Tuplas mistas d64/d384 mediram `1,042x`/`1,057x`: melhoria simples e transversal, registrada sem novo gate e sem reabrir o redesenho amplo recusado no lote 11. Formato, política de corrupção, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 28 — descritor participant revalidado entre statements | concluído e auditado na branch | `764c546`; auditorias local e Nexus `hof_8bdda5e6dda34172bf688bd1aa54f279` PASS; correção de fixtures stale em `674e420`; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Reusa apenas o descritor aberto e desbloqueado durante a vida da transação; cada statement/commit/settle ainda adquire e libera o lock real. Revalidação física por borrow, fallback frio fail-closed, opt-in por classe concreta e drenagem em commit/rollback/retry/OCC/pós-barreira/close preservam concorrência e durabilidade. Quatro statements reduzem opens participant `6→3` sem reduzir `6/6` acquires/releases; amostra de 1.000 PK seeks observou `1,124x`, sem criar gate temporal. 203 testes passaram e um skip de plataforma foi declarado. Formato, WAL/OCC, writer lease, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 29 — clonagem defensiva de planos compilada | concluído e auditado na branch | `c06463f`; revisão Nexus `hof_93cce5a9688049988570694a4588ecea` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Planos provados como internos compilam, somente após snapshot e validação completos, uma receita limitada pelo LRU existente de 128 entradas. Cada resultado continua dono de uma árvore nova; `TableDef`/`ColumnDef` refazem seus caches e `Literal` mutável recebe novo snapshot. Externos/subclasses nunca usam o atalho. O componente mediu `1,983x`, enquanto o endpoint curto mostrou apenas `~1,02x` ruidoso, sem gate nem extrapolação. Auditoria encontrou zero nós/mutáveis compartilhados; 62 focais e slices de 180/85 testes passaram. Formato, WAL/OCC, locks, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 30 — publicação confiável de resultados e Protocols concretos exatos | concluído e auditado na branch | `e572227`; revisão Nexus `hof_9016dfc426f5439c9d204f9792dea69d` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | O engine exato e a fronteira pública pós-snapshot não repetem a guarda hostil de `QueryResult`; o construtor público e toda entrada colaboradora mantêm a validação integral. `Snapshot`, `TransactionContext` e `WalRecord` exatos evitam reflexão estrutural, mas subclasses, doubles e incompletos continuam pelo Protocol e pelas mesmas recusas. Um quarto atalho foi descartado por ciclo real de importação e ganho pontual. O auditor reconstruiu oito resultados publicados pela porta pública e confirmou tipos/imports em processos novos. Foram aprovados 422 testes focais/adjacentes e checks estáticos; a leitura trivial indicou `~1,08x`, enquanto a escrita mostrou apenas ganho pequeno e ruidoso, sem gate. Formato, checks de corrupção, WAL/OCC, locks, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 31 — witnesses bounded da gramática de nomes exatos | concluído e auditado na branch | `c228b0a`; auditoria independente GO sem gate; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Validações bem-sucedidas de strings built-in exatas usam LRU process-local de 512 entradas, com chave `(label,value,limit)` no controle e `file` integral no storage. Falhas nunca entram no cache; limites distintos, subclasses e objetos hostis preservam o caminho e a taxonomia, e o retorno mantém a identidade do argumento corrente. Nenhum cache extra por segmento foi criado. Ganhos de componente foram `~4,6x`/`~13,7x`, mas o teto plausível em PK seek NTFS é `<1%`: melhoria cumulativa, sem gate. Suítes 26/26 e 147/147 e checks estáticos verdes. Autoridade de path/descriptor, formato, WAL/OCC, locks, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 32 — um descritor desbloqueado por autocommit read | concluído e auditado na branch | `1038ddb`; revisão Nexus `hof_6b1f9e6b31b54c5a97ebed76a2214a32` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | O ciclo público `begin→execute→commit/rollback` fica numa transição externa e na capability de descritor já auditada. `os.open` do participant cai `4→1`, mantendo exatamente quatro acquires/releases reais. Falha de commit reverte somente contexto ACTIVE; rollback que deixa contexto inalcançável sela e drena o facade. Scope custom não suprime falha primária nem deixa exit substituí-la. API 80/80, coordination 29 + 1 skip de plataforma e multiprocesso 5/5; checks estáticos verdes. Amostra indicou `~1,11x`, sem gate; a contagem estrutural é a evidência promovida. Lock duration, formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 33 — dimensionamento de novas gerações automáticas | concluído e auditado na branch | `2dbc367`; revisão Nexus `hof_37ec8f454ada4a668678ebd943b5df6d` verificada PASS | `automatic_index_expected_cardinality` dimensiona somente novas gerações v2; banco v1 vazio e gravável é ativado antes do primeiro DDL, enquanto v1 não vazio não sofre migração implícita e recusa novo DDL com remediação explícita. O índice `record_id` usa a estimativa apenas como piso; reabertura preserva gerações existentes. A sonda física confirmou `64→4096` buckets no máximo configurável sem alterar artefatos legados; o custo máximo é `4097` páginas por artefato automático e multiplica pelo número de PKs/identidades/endpoints. O padrão permanece 64 para evitar custo de buckets vazios em grafos pequenos. Formato existente, WAL/OCC, durabilidade e multiwriter/multireader permanecem intactos |
| 0.0.2 / lote de escala 34 — reaproveitamento da prova imediata de raiz | concluído e auditado na branch | `bce884c`; revisão Nexus `hof_8995dc6efa8848f0be519389b1a35a84` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | A resolução de nome exato e a validação de pais reutilizam no primeiro componente a prova no-follow da raiz feita imediatamente antes, eliminando uma observação literalmente duplicada. Postcheck após listagem e checks before/after dos pais intermediários continuam integrais. O fast path exige raiz, identidade capturada e prefixo vazio, com regressão que impede uso indevido em diretório intermediário. Um `exists` de dois componentes cai `7→6` `lstat`; a sonda curta de cinco writes caiu `956→874` (`-8,6%`), sem elevar isso a gate de parede. 55 casos coletados, sete skips de plataforma declarados, Ruff e diff-check verdes. Case exato, redirect refusal, formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 35 — rehash assistido bounded | concluído e auditado na branch | `8c13d0f`; revisão adversarial GO e Nexus `hof_0c6e83b53d60466397ea63cd6c453019` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | `rehash_index_if_needed` resolve e prova a geração física antes de qualquer `None`; abaixo do teto, conta slots somente nos `B<=4096` head pages e usa `page_count` como sinal secundário, sem decodificar entradas, seguir overflow ou fazer censo O(N). Pressão `64*B` ou ratio físico solicita exatamente um degrau `B→2B` no protocolo shadow/OCC/WAL existente. O teto pula o scan após provar identidade. Não existe automação em commit/background, `None` não é certificado de saúde e corrida exige reavaliação sem garantia universal de retryable. Ensaio direcional alinhou o trigger entre 4.000 e 5.000 linhas para 64 buckets; slices 31+67 e revisão independente 107 verdes. Formato, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 36 — witness revogável de replay local no checkpoint | concluído e auditado na branch | `2792701`; Nexus `hof_fbf973af72084c928938aa64dcb522d4` verificada PASS após corrigir um blocker de eficácia; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Checkpoint completo semeia um witness O(1), não persistido, que cobre apenas o prefixo exato já aplicado/flushado/publicado por este processo. DML comum e reserva CN-1 só o estendem após as respectivas garantias; qualquer writer estrangeiro, DDL/generation/RESET, página suja, recovery/falha/close ou colaborador custom revoga/recusa. A leitura, checksum, continuidade e preflight integral do WAL continuam obrigatórios; apenas o apply/flush estrutural redundante é omitido. Carga de 4.000 linhas ativou 7/16 atalhos e reduziu chamadas `CommitRedo.apply` `32→18`; micro isolado mediu `1,47x`, sem promessa ponta a ponta. Slices 101+11 e checks estáticos verdes; WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 0.0.2 / lote de escala 37 — preflight lógico estrito sem duplicação | concluído e auditado na branch | `75b0fdc`; Nexus `hof_6a091147d30e460eb85518398c096ee0` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Como o shortcut exige catálogo intocado, o full preflight já é estrito e percorre as mesmas validações de nome/generation/versioned/key limit; o segundo preflight do subplano lógico apenas repetia decode sem mutação intermediária possível. A sequência focal passou `[3,2,0]→[3,0]`, e a auditoria de 2.000 linhas confirmou um único passe estrito com efeitos por shortcut. Slice 62 e checks estáticos verdes; sem claim temporal, formato ou mudança nas garantias |
| 0.0.2 / lote de escala 38 — fatos RESET reaproveitados das validações obrigatórias | concluído e auditado na branch | `6b142ba`; Nexus `hof_a85992ce987045a7ac73b2bb42ac822c` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | O preflight integral e o validator de staging passam a transportar o fato `contains_index_reset` já decodificado, removendo dois rescans lineares. O checkpoint só consome proof verificado por seal/owner/replay/passage/signature e cai no replay canônico em qualquer dúvida. O commit só confia no manager/validator built-in exatos e continua revalidando após retarget. RESET é identificado pela operação, não pelo tipo WAL. As medianas A/B de 2.000/8/8 foram `11,397→11,166 s`, mas os ranges sobrepostos não sustentam claim temporal; o ganho promovido é estrutural. Payload corrompido, provas incompatíveis e validator substituído foram cobertos. Sem gate ou alteração de WAL/OCC, durabilidade e concorrência |
| 0.0.2 / lote de escala 39 — certificado fresco byte-idêntico | concluído e auditado na branch | `f28c686`; Nexus `hof_0f5a8046fe2844d79b0c3bbd5a90ae72` verificada PASS; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | A leitura física e a invalidação de descriptor permanecem obrigatórias; somente igualdade byte-a-byte reutiliza o witness e certificado semântico pareados. Instrumentação `1.015→12` decodes genéricos e `1.007→4` semânticos, sem claim baseado nos timings ruidosos. Retém uma raw page por IndexStore acessado (~8 KiB default; ~1,1 MiB/141), não contabilizada pelo retained estimate do BufferPool. A cobertura adversarial inclui mudança estrangeira, interleaving, customização e corrupção exclusiva do último byte; 17 focais e 298 agrupados passaram. Zero alteração em OCC, WAL, durabilidade ou multiwriter/multireader. |
| 0.0.2 / lote de escala 40 — piso atômico inicial e cursor de extent selado | concluído e auditado na branch | revisão adversarial independente GO; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | O primeiro row de uma tabela vazia instala no mesmo commit o piso final já planejado; os demais usam reserva sem reescrever page zero por identidade. A autoridade frozen/selada fixa tabela, raiz e piso; somente tail/count/epoch são hints, e `_write_extent` preserva monotonamente piso mais novo. Em 500 rows: insert/observe `500→0`, 1 criação inicial, 499 reservados, 1 proof, 3 lookups e 62 rewrites para exatamente 62 crescimentos. Regressões cobrem stale floor, cursor adulterado, raiz adulterada, corrida multiprocesso vazia, falha pré-WAL+retry e lote misto vazio/CN-1. Nenhum gate temporal ou alteração de WAL/OCC, durabilidade e multiwriter/multireader. |
| 0.0.2 / lote de escala 41 — autoridade de índices local ao statement e commit | concluído e auditado na branch | Grafx `613bff6`; consumidor Pulse `e4ac346`; revisões estrutural e de correção independentes GO; regressão agrupada Grafx 321/321 e consumidor Pulse 57/57; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Statements built-in com footprint fechado e o commit canônico consultam somente índices das tabelas tocadas. A projeção transitória nasce após OCC/rebase/sync, dentro do writer lease, WAL-tail e `COMMIT_SECTION`, e é revogada em toda saída; formas desconhecidas/polimórficas, registros pre-staged e colaboradores custom preservam o caminho global. Três scans adjacentes de schema/registry também foram tornados table-local. Com 1 ou 80 tabelas, CREATE Person v1/v2 fez zero `Catalog.tables()`, scans globais de definições ou walks globais do registry; apenas Person foi inspecionada. A integração Pulse ativa v2/identity indexes somente no candidate novo, exclusivo e vazio antes do DDL, sem adotar paths existentes. Quota, artifacts, multiset, RESET, retarget/rebuild, WAL, ambas OCC, durabilidade e multiwriter/multireader permanecem integrais. |
| 0.0.2 / lote de escala 42 — precheck de DELETE em relacionamentos | implementado; gate focal verde, revisão adversarial em andamento | descoberta/reprodução Claude `art_dac31d35a797468ea30dda80cd8c248e`; evidências `art_8eb4af8d3ede4c38916d22203a181a67`; consenso Nexus `msg_0f9531e438074cbfa72feb9341a3ec86`; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | Transações insert-only deixam de reconstruir a view completa de todas as tabelas sujas em cada seek: o precheck passa de `O(T*P)` para uma passagem `O(P)` e delega integralmente ao caminho canônico se houver DELETE/held-delete. Harness Pulse-shaped T=30/1.500 relações mediu `60,17→2,03` views/relação e `-44,7%` na fase, com resultado/verify idênticos. Testes focais cobrem short-circuit, delegação, MERGE após DELETE, relação já encerrada e pending-ref inválida na própria tabela. Sem alteração de formato, WAL/OCC, durabilidade ou multiwriter/multireader. |
| 0.0.2 / lote de escala 43 — descriptor bounded por transação lexical | implementado; gate focal Grafx e consumidor Pulse verdes | revisão independente Codex; A/B `D:\GrafxBenchEvidence\descriptor-conservative-codex-20260905\ab_conservative.py`; consenso Nexus `msg_0f9531e438074cbfa72feb9341a3ec86`; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | `Database.transaction()` reutiliza somente o fd destravado durante sua própria fronteira lexical e reprova identidade física antes de cada aquisição. Contagem independente: `4,0→1,0` opens/txn e `0→3,0` revalidações aprovadas; tempos direcionais melhores, sem promover número exato porque as bandas se sobrepuseram. O Pulse usa autocommit otimizado para leitura simples e a fronteira lexical para o par no mesmo snapshot. Locks reais, cleanup, thread-hop, close e retry/OCC permanecem cobertos; nenhuma retenção entre transações, formato/WAL/OCC ou mudança multiwriter/multireader. |

O hardening `aef1df7` existe por causa de evidência, não por expansão de escopo: a auditoria
reproduziu um `DETACH DELETE` que confirmava sucesso enquanto deixava viva uma relação staged, e um
`DELETE p, p` que recusava o segundo nome do mesmo insert. O primeiro agora falha tipado antes do
handover enquanto M-PULSE-1C ainda não havia chegado; o segundo é idempotente e conta uma
exclusão. O fechamento definitivo de M-PULSE-1 é o par Community `befaf1e`/Core `ab61b9a` registrado
acima, não qualquer checkpoint intermediário.

O provider Grafx final de `befaf1e` não foi registrado na composição. Ativar somente
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
8. adicionar vacuum, índice de identidade e rehash/rebuild de índices;
9. decidir P1.16 (parâmetros de construção do HNSW) depois do perfil de recall exigido.

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
