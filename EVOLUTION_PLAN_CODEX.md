# Plano de evolução do Okto Grafx

**Data da análise:** 2026-08-23

**Estado analisado:** `main@092a60f`

**Ambiente principal:** Windows, Python 3.13.1
**Escopo:** integridade, recuperação, concorrência, estabilidade, performance, API, configuração e novas capacidades.

## Estado de execução — 2026-08-28

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
- **Compatibilidade Okto Pulse: M-PULSE-1 a M-PULSE-4 concluídos; M-PULSE-5 em execução com
  seus componentes intermediários já concluídos, publicados e auditados conforme o quadro 9.7.**
  M-PULSE-4 foi aceito e promovido no Pulse Community em
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
  `445043666530862300221783ece480e63c7086ac`; os três passaram revisão independente. A integração
  serial com a fundação Board/Global e os adapters Ladybug, seguida da matriz congelada de 8 testes
  parametrizados/32 casos, continua pendente e é o único próximo alvo de M-PULSE-5.
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
`okto-grafx==0.0.1` instalável do PyPI. Nenhum item `GX-CAP-*`, `GX-AGENT-*` ou `AGENT-*` começa
antes desse gate. A evolução complementar subsequente pertence à versão `0.0.2`.

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
- A sequência vinculante é M-PULSE-5 → M-PULSE-6 → M-PULSE-7 → run integrado e auditoria →
  build/install limpo → checkpoint interativo, publicação e reinstalação de
  `okto-grafx==0.0.1` no PyPI → somente então linha `0.0.2`.
- Nenhum `GX-CAP-*`, `GX-AGENT-*` ou `AGENT-*` começa antes desse gate de release.

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
| M-PULSE-3A — propriedades de node | concluído e publicado no branch Pulse atual; helper ainda inativo | Pulse Community `milestone/grafx-mpulse3-node-properties@4aae27eca9c0a2d1d14a3334b03e6c57976dea75` → `feature/v0.3.3`; revisão Nexus `hof_cad849ee19e542eb862930f64054724d` concluída/PASS | labels desconhecidas retornam vazio sem tocar backend; label conhecida ausente/wrong-kind e DB fechado falham tipado; ordem de catálogo, snapshot, reopen e fronteiras públicas cobertos; 8/8, Ruff default/TRY/I/BLE e format PASS. A resolução `board_id → Database` fica no provider/composição, sem ativação parcial |
| M-PULSE-3B — layout lógico de relações | concluído e publicado; provider ainda inativo | Pulse Community `milestone/grafx-mpulse3-logical-relationships@c4b1f37ad3a4cd08a1e2f5249db25c33ddbecd45` → `feature/v0.3.3`; código `067b82c` + hardening `c4b1f37` | Manifesto fechado e imutável de 16 tipos/69 pares/69 nomes, codec bijetivo e reverse pelo manifesto; introspecção valida kind/endpoints e oculta nomes físicos. Unknown/collision/mismatch, shapes malformados e representação hostil falham tipados. Gate focado 15/15; regressão selecionada completa 146/146 contra Core limpo `ab61b9a`; Ruff/format/diff-check e duas auditorias independentes PASS. DDL/bootstrap, ativação, query rewrite e os gaps de supersedence continuam explicitamente fora |
| M-PULSE-3C — manifesto e bootstrap do schema atual | concluído, verificado e publicado; provider ainda inativo | Pulse Community `milestone/grafx-mpulse3-schema-bootstrap@7e126a7130090c00891f8d1d35bd44819afe7a7a` → `feature/v0.3.3`; Core pinado `ab61b9a785f2018312fc91541a580877fd068bbb`; auditoria Nexus `hof_7240f7ad38f64538adc5b500bd3ea1a7` concluída/verificada/PASS | Schema `0.5.0` materializado em 11 nodes de 44 propriedades, `BoardMeta`, 69 relações e 11 spaces únicos 384/cosine/normalized=false/float64. Preflight fail-closed precede qualquer write; ausentes são criados numa única transação, o catálogo é recapturado/validado e somente então `BoardMeta` é gravado em transação separada. Fingerprint lógico canônico `4a7b425bf4b8c4864be633c1a87f034e5f7f641019dc029015b7d3ca786deb81`; no-op preserva catálogo/txn/LSN/WAL e bytes. Gates M3C 33/33 e regressão selecionada 73/73 no Core correto; Ruff/format/diff-check focados PASS; outputs Kuzu têm digest `18a8b1a1b9459d92d61670d734087a4212af29fa4039b0825d6e966ffa181e0e`, `kg.py`/`composition.py` mantêm os blobs congelados. Staging dos 92 DDLs mediu aproximadamente 101 s neste ambiente: risco de performance registrado para otimização posterior, sem alterar o gate funcional. ALTER/upgrade, provider, rewrite e paridade vetorial permanecem fora |
| M-PULSE-3D — rebuild do predecessor `0.3.12` | concluído, verificado, publicado e integrado; provider ainda inativo | spec Pulse Community `milestone/grafx-mpulse3-schema-evolution@703ad83c43b286e7c90fe2b0a29de0982929d4de`; código final `237f3bf7fa2a65a108db4f558932d429ec6696ce` em `milestone/grafx-mpulse3-schema-evolution-impl` e `feature/v0.3.3`; revisão Nexus final `hof_3df4987f6eef4611bfd9486409b91227` concluída/verificada/PASS | Reconstrução fora do lugar para candidato durável não vinculado, sem mudança no formato físico Grafx, `CATALOG_FORMAT_VERSION` ou upgrade in-place. Fonte em snapshot inerte; 11 nodes, 59 relações predecessoras, duplicatas/paralelas/self-loop, NULLs e vetores são preservados; dez relações novas nascem vazias. Catálogo, fingerprints, contagens e 161 índices são provados hot+cold; marker/commit ambíguo, checkpoint/recovery, close/lock, matriz finita de falhas e no-op zero-write são fail-closed. Gate rápido `122 passed, 1 deselected`, suíte completa real `122 passed` no mesmo código de produção, regressão M3A/B/C 56/56 e checks estáticos PASS. O único blocker factual da primeira revisão — `PermissionError` cru em `Path.exists()` — foi reproduzido no SHA antigo, corrigido e provado por teste discriminante no final |
| M-PULSE-4 — paridade vetorial | concluído, verificado, publicado e integrado | Pulse Community `milestone/grafx-mpulse4-accepted@d3ef4afdf263e7b6da70b6705b31950cfe07986e` → `feature/v0.3.3`; evidência não pública `milestone/grafx-mpulse4-vector-recall@fcfbf215b151aac4603978e02900ef323cb1b898` | Matriz V1–V7, exact/ANN, filtros, ordenação, rebuild/churn e cold reopen fechados. Gate final 56/56; recall@10 público `0.9546875`. `Alternative`/`Assumption`: 8192 linhas e 128 commits cada, build/ingest `1279.6239901 s`/`1415.9925527 s`, persistido `794624 B`/`802816 B`, verify limpo e cold/reopen 10/10. Artefato SHA-256 `e010ef6fb4a46b9e7a7bb770c9d4f6007b5b1af6415efb9952e3b7c369a0bede`; sem SLO; auditoria independente PASS |
| M-PULSE-5 — scan físico bounded-memory no Grafx | concluído, revisado, integrado e revalidado | Grafx `milestone/mpulse5-scan-v1@f132190207b562eb9794e7e4d752c41c9fdb7504` → `main`; validação pós-integração 254/254, Ruff, compileall e diff-check PASS | `Transaction.scan_rows_v1` percorre o snapshot read-only na ordem física estável, devolve DTOs destacados, preserva uma ocorrência por relação e usa cursor opaco, preso ao banco/transação/snapshot/tabela e de uso único. Continuação, writer concorrente, relações paralelas, token inválido e bounded-memory foram revisados sem blocker; nenhuma semântica Pulse, arquivo ou backup entrou no core Grafx |
| M-PULSE-5 — formato e transferência lógica no Pulse Core | concluído, verificado e integrado | Pulse Core `milestone/grafx-mpulse5-logical-transfer-core@098a346b0988d7b39e417e7de7ed8d57d06b9795` → `feature/v0.3.3`; revisão Nexus `hof_2373b761002c42aeb519e53617e56ec8` concluída/verificada/PASS | Formato `okto-pulse-logical-graph/1`, DTOs frozen, schema Board/Global representável, mapping propriedade→space, geometria vetorial, identidade tripla de layouts, codec canônico incremental, fingerprint multiconjunto, ports e transferência candidata neutra. Corrupção, não-finitos, wire/features, schema-record, batches e certificação cold-reopen falham tipados na matriz finita. Validação independente final: 318/318, Ruff, compileall, diff-check, remoto exato e auditoria de fronteiras 4/4 PASS. Adapters, arquivo atômico e round-trips físicos permanecem no Community, como já congelado |
| M-PULSE-5 — arquivo lógico atômico no Community | concluído, publicado e auditado; integração serial pendente | Pulse Community `milestone/grafx-mpulse5-logical-artifact@cb74da0f135cf8429cd9c35f399cd10ed0bd7ed6` | Publicação por arquivo temporário, flush/fsync, verificação e replace atômico; matriz congelada `success/write/fsync/verify/replace` 5/5, geração anterior preservada nas falhas e nenhuma operação posterior ao replace; auditoria PASS |
| M-PULSE-5 — source físico Grafx | concluído, publicado e auditado; integração serial pendente | Pulse Community `milestone/grafx-mpulse5-grafx-adapter@cb75ac60b74310a6af2de7f151286af1bfa307bd` | Snapshot Grafx único, projeção integral do schema fixo, `None → LOGICAL_NULL`, mapa temporário SQLite `(node_table, record_id) → logical_key`, scan bounded-memory e cleanup em sucesso/falha; contrato de endpoints `success/scan_failure/dangling_endpoint` 3/3 e checks estáticos PASS |
| M-PULSE-5 — sink candidato Grafx | concluído, publicado e auditado; integração serial pendente | Pulse Community `milestone/grafx-mpulse5-grafx-sink@445043666530862300221783ece480e63c7086ac` | Geração nova e não vinculada, schema esperado exato, recusa de propriedade ausente, batches transacionais, checkpoint, close, cold reopen, `verify("all")`, contagens/fingerprint e abort seguro sem tocar a geração anterior; falhas de import/checkpoint/reopen e checks estáticos cobertos |
| M-PULSE-5 — integração e aceite final | em execução | fundação/factories Board e Global e adapters Ladybug ainda pendentes no Pulse Community | Integrar serialmente os componentes concluídos e executar a matriz já congelada de 8 testes parametrizados/32 casos: A1 2, A2 3, B1 4, C1 4, C2 2, D1 8, D2 5 e D3 4; depois publicar o SHA Community imutável em `feature/v0.3.3` |
| Roadmaps complementares pós-Pulse | incorporados por referência; implementação bloqueada até M-PULSE-7 + run/auditoria + publicação verificada de `0.0.1` | `GRAFX_COMPLEMENTARY_EVOLUTION_PLAN_CODEX.md` (`GX-CAP-0..11`, `GX-AGENT-0/1`) e `AGENT_FIRST_EVOLUTION_PLAN_CODEX.md` (`AGENT-0..8`) | Ambos os arquivos integrais são autoridades versionadas da linha `0.0.2`. Database-first governa ownership/ordem no core; agent-first preserva todos os requisitos e gates detalhados da camada opcional. Nenhum item amplia milestones Pulse correntes; sobreposição usa o conjunto compatível mais estrito e conflito exige ADR explícita |

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
