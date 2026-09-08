# Plano complementar de evolução do Okto Grafx — Database-First Capabilities

**Data:** 2026-08-26
**Status:** proposta para refinamento e execução incremental

**GX-CAP-1B / validação de append / 2026-09-08:** transição entre diretório e
stream validada contra predecessor íntegro, com recusa de páginas faltantes,
extras, stamps errados e reescritas históricas com CRC válido. Regra exata de
tempo lógico e registro máximo incluídos. 691 testes agrupados em 21,47 s;
reconstrução da visão após crash, staging/publicação e replay completos ainda
pendentes. [Contrato e evidência](docs/specs/SPEC-GX-CAP-1.md). Pulse/specs intocados.

**GX-CAP-1B / vínculo COMMIT–replay / 2026-09-08:** os COMMITs individuais agora
acompanham os efeitos no selector e nos subplanos de recovery/checkpoint. Preflight
valida ownership/ordem/watermark e recusa mutação durante callbacks antes de
aplicar páginas. 826 testes agrupados em 37,45 s. É pré-requisito para a cobertura
do journal, não sua publicação/replay completos; proteções de integração continuam
ativas. [Evidência e limites](docs/specs/SPEC-GX-CAP-1.md). Pulse/specs intocados.
Checkpoint imutável: `18d53f72442d3620eb84afd1bd61e2326107bfcd`.

**GX-CAP-1B / gramática WAL / 2026-09-08:** framing obrigatório do journal
implementado em v2 (`0x0011` raw, `0x0015` comprimido). Semântica antiga simulada
recusa leitura, append e recycle sem alterar bytes; preflight do recovery recusa
o journal antes de aplicar qualquer prefixo. Alvos de redo e publicação automática
continuam fechados até integração completa. Grupo de 894 testes em 17,63 s, sem
novos erros de tipagem nos módulos verificados. Evidência, restrições e próximos
requisitos: [`SPEC-GX-CAP-1`](docs/specs/SPEC-GX-CAP-1.md). Pulse/specs intocados.
Checkpoint imutável: `0feec917d0ac8d1cb2bfea6543e20cf7de156ddf`.

**GX-CAP-1B / ativação interna / 2026-09-08:** catálogo v2 agora persiste o
horizonte do COMMIT de ativação com capability obrigatória. A preparação privada
usa WAL v1, ajusta o horizonte no roll e recupera o mesmo resultado após falha
pós-durabilidade. Não existe ainda publicação automática dos registros; bancos
experimentais ativados recusam novas escritas até essa integração. API pública,
staging/replay do journal e os demais gates permanecem obrigatórios. 1.195 testes
agrupados em 118,97 s; 50 focados após correção adicional de limpeza de contexto.
Ruff verde e nenhum novo erro nos dois módulos com dívida de tipagem preexistente.
Detalhes: [`SPEC-GX-CAP-1`](docs/specs/SPEC-GX-CAP-1.md). Pulse e specs preservados.
Checkpoint imutável: `3d6ef7f822cbe14293f72ee886f91a1fe365485a`.

**GX-CAP-1B / binding do LSN final / 2026-09-08:** preparação limitada por tentativa
e ajuste da identidade ao COMMIT LSN implementados, sem reler storage ou relógio
durante o ajuste. A preparação exige cobertura igual ao controle durável informado.
471 testes agrupados em 15,72 s, incluindo o planejador real do WAL com/sem roll,
sem anexar registros de catálogo. Integração com staging/OCC/ativação/replay ainda
pendente; a lista fechada de pontos de ligação está em `COMMIT_CATALOG_V1.md`.
Não é cache de autoridade nem capability habilitada no Pulse.
Checkpoint imutável: `2d01f1793b8190afe8a7b2f8c21fb0e41913b337`.

**GX-CAP-1B / armazenamento paginado / 2026-09-08:** implementados o planejador
privado de imagens de páginas, append limitado à cauda, lookup binário por identidade
e verificação completa da cobertura anunciada. 59 testes específicos; grupo final
de 606 testes em 14,79 s. O contrato de páginas e os limites de evidência estão em
[`COMMIT_CATALOG_V1`](docs/architecture/COMMIT_CATALOG_V1.md) e
[`SPEC-GX-CAP-1`](docs/specs/SPEC-GX-CAP-1.md). Ainda **não** é uma capability ativa:
staging transacional/WAL, recovery, autoridade física, API e demais gates continuam
obrigatórios. Nenhuma instalação ou consolidação adicional no Pulse.
Checkpoint imutável: `d5c3ed1aaab9edf1ebd99752fc7cf3d4b6125043`.

**GX-CAP-1B / 2026-09-08:** codec do registro persistente e decoder dos metadados
implementados/validados na mesma branch `feature/gx-cap-1`: 379 testes verdes,
incluindo 3.000 entradas modificadas; tipagem estrita/Ruff verdes. O contrato
`docs/architecture/COMMIT_CATALOG_V1.md` define encoding e gates de crash, mas o
storage paginado, ativação e integração commit/recovery continuam pendentes.
Nenhum formato foi ativado no Pulse; nenhuma spec adicional foi consolidada.
Checkpoint imutável: `817fc8a9020fab12f9483ab7dec6d1411d66afcd`.

**GX-CAP-1 / 2026-09-08:** primeiro slice de domínio implementado em
`feature/gx-cap-1`: CommitId qualificado, admissão limitada/imutável de metadados
e cálculo puro de tempo lógico. 985 testes agrupados passaram, incluindo 87
específicos; tipagem estrita/Ruff verdes. A capability persistente e pública
continua pendente, sem mudança no Pulse ou novas specs consolidadas.
Registro: [`SPEC-GX-CAP-1`](docs/specs/SPEC-GX-CAP-1.md).
Checkpoint imutável: `474335962eeae20a937d6a444bcc3f7b2c2f110f`.

**Execução 2026-09-08:** GX-CAP-0 ganhou seis ADRs, estratégia de manifest,
especificações por milestone e enforcement de fronteiras de source/wheel em
`feature/gx-cap-0` (base `61fc44d`). Registro de testes e limite do checkpoint:
[`docs/specs/SPEC-GX-CAP-0.md`](docs/specs/SPEC-GX-CAP-0.md).
As demais capacidades continuam não certificadas; os contratos não as anunciam
como implementadas. Checkpoint imutável `eb25eccbd42d16a1a3ddf06292f2d69b4eaaedc6`,
296 testes agrupados verdes em 14,98 s. Nenhuma nova feature foi instalada no Pulse.
O plano agent-first continua obrigatório por decisão do plano
principal, independentemente da declaração histórica de substituição abaixo.
**Documento-base obrigatório:** `EVOLUTION_PLAN_CODEX.md`
**Natureza:** roadmap complementar; não substitui, não reabre e não duplica o plano técnico existente
**Objetivo de consumo:** orientar o desenvolvimento pelo Codex com escopo, contratos, dependências, milestones, gates e decisões explícitas
**Substitui:** a proposta isolada `AGENT_FIRST_EVOLUTION_PLAN_CODEX.md`, cujo conteúdo relevante foi incorporado aqui como uma camada opcional de produto

---

## 1. Resumo executivo

O Okto Grafx deve continuar sendo, antes de qualquer outra coisa, um **graph + vector database embedded, local-first, multiprocess e verificável**. A principal identidade do produto permanece no banco e nas suas garantias:

- múltiplos processos e threads lendo e escrevendo o mesmo store local;
- snapshot isolation, OCC, WAL, recovery e durabilidade explícita;
- property graph tipado, Cypher, índices de identidade/endpoints e vetores nativos;
- verificação estrutural, ledger forense, quarentena e falhas tipadas;
- arquitetura hexagonal com ports/adapters e dependências orientadas para dentro.

Este plano adiciona capacidades que fortalecem essa identidade e aproximam o Grafx do conjunto de funcionalidades mais valioso observado em outros bancos, sem tentar transformá-lo em um clone de Neo4j, Ladybug, SurrealDB ou CozoDB.

As prioridades complementares recomendadas são:

1. **grafo temporal e time-travel queries**, com system time, valid time e evolução bitemporal;
2. **metadados genéricos de commit e proveniência**, aproveitáveis por qualquer aplicação;
3. **catálogos/stores nomeados**, permitindo uma convenção segura de store de projeto e store global do usuário;
4. **full-text search e hybrid retrieval**, combinando texto, vetor e estrutura do grafo;
5. **evolução de schema de aplicação**, distinta da migração do formato físico do banco;
6. **SPI de extensões, UDFs e procedures**, sem abrir as invariantes internas do engine;
7. **interoperabilidade com Arrow, DataFrames e formatos externos**;
8. **projeções e algoritmos de grafo** como package opcional sobre uma API pública estável;
9. **views e derived graphs** após a estabilização das capacidades anteriores;
10. **Agent API e MCP `stdio` opcionais**, construídos sobre o banco, nunca dentro do core.

A tese de produto resultante é:

> O Okto Grafx é um banco de grafo e vetor embedded para aplicações local-first que precisam compartilhar estado entre processos com garantias fortes, consultar a evolução histórica do grafo e combinar recuperação textual, vetorial e estrutural. Agentes são um caso de uso de primeira classe da plataforma, não a definição do banco.

---

## 2. Correção de escopo e relação com o plano atual

O `EVOLUTION_PLAN_CODEX.md` já possui um plano amplo e em execução. Este documento não deve virar uma segunda fonte de verdade para os mesmos itens.

### 2.1 Itens que permanecem exclusivamente no plano existente

Não replanejar nem reimplementar aqui:

- correções M0 de recovery, durabilidade, bootstrap, read-only e publicação concorrente do HNSW;
- configuração honesta, budgets, tipagem pública, health e maintenance de M1;
- identity-range leasing;
- redução de locks globais, `os.walk`, `stat/fstat`, metadata amplification e custo de publicação;
- HNSW como access path realmente sublinear;
- correções de agregação, top-N e traversal landing lookup;
- bulk ingest, `executemany`, batching e group commit;
- vacuum físico e relatório de bloat MVCC;
- backup/restore consistente;
- export/import lógico e migração do formato físico;
- índices secundários genéricos, índices compostos/range/prefix, B+tree, estatísticas, cost model, `EXPLAIN` e `PROFILE`;
- streaming cursor e prepared statements;
- operações Cypher e capacidades de grafo já enumeradas no plano atual;
- changefeed lógico;
- sincronização, replicação e política de conflitos distribuídos;
- criptografia em repouso;
- compatibilidade total e migração do Okto Pulse.

Esses itens aparecem neste documento apenas como **dependências** ou **pontos de integração**.

### 2.2 Lacunas complementares confirmadas

O plano atual não fecha explicitamente as seguintes capacidades:

- histórico lógico consultável e time travel;
- system-time, valid-time e bitemporalidade;
- diff entre estados históricos do grafo;
- full-text index e ranking BM25;
- fusão lexical + vetorial + grafo;
- múltiplos stores/catálogos anexados e nomeados;
- convenção genérica de store de projeto e store global do usuário;
- metadados lógicos de commit/ator/origem;
- UDFs, procedures e uma extensão pública suportada;
- projeções nomeadas e biblioteca de algoritmos;
- Arrow/DataFrame/external scan como superfície de interoperabilidade;
- evolução versionada do schema da aplicação;
- views e derived graphs;
- uma camada agentic/MCP opcional e desacoplada.

### 2.3 Dependências principais

| Nova capacidade | Dependência do plano existente |
|---|---|
| ADRs, contracts e package boundaries | pode iniciar imediatamente |
| Commit metadata e identidade lógica de commit | M1 tipado; integração com WAL/recovery existente |
| Catálogos anexados e workspace resolver | facade pública tipada; lifecycle/close confiável |
| Temporal system-time | commit identity estável; maintenance e formato com feature manifest |
| Retenção temporal | vacuum físico separado e maintenance facade |
| Full-text search | index lifecycle/freshness; budgets; planner extensível |
| Hybrid retrieval | full-text + vector search; HNSW real de M2 para performance competitiva |
| External scans/Arrow | streaming cursor; budgets; bulk ingest para materialização eficiente |
| Graph projections/algorithms | streaming, budgets, traversal estável e SPI de extensões |
| Agent/MCP preview | catálogos + commit metadata; não depende de HNSW otimizado para corretude |
| Agent/MCP operacional | backup/restore, export/import, health e performance M2 |

---

## 3. Princípios vinculantes

### D1 — Database-first é a fronteira principal

Toda nova feature deve responder primeiro à pergunta:

> Esta capacidade é útil para um banco de grafo embedded genérico, mesmo que nenhum agente exista?

Se a resposta for não, ela não pertence ao core.

### D2 — Agentes são consumidores especializados, não tipos do storage engine

`Agent`, `Claim`, `Memory`, `Evidence` e `Session` podem ser entidades de um schema opcional e de uma API de produto. Não devem entrar em:

- `domain/model` do core;
- formato universal de node/relationship;
- WAL físico;
- transaction manager;
- query planner genérico;
- ports de storage/coordination.

### D3 — Semântica antes da sintaxe

Capacidades novas devem primeiro possuir:

1. modelo de domínio;
2. invariantes;
3. API typed;
4. testes de recovery/concorrência;
5. somente depois extensões de Cypher.

Isso evita congelar uma sintaxe antes de provar a semântica.

### D4 — Histórico lógico não depende da retenção MVCC

MVCC existe para isolamento. Vacuum existe para remover versões internas que já não são necessárias a readers ativos. Time travel é uma capacidade de produto e precisa de histórico lógico persistido explicitamente.

Nenhuma query histórica pode depender de manter indefinidamente versões físicas internas do heap.

### D5 — Ordem temporal canônica é lógica e monotônica

Wall clock é informativo, não autoritativo. O banco deve atribuir a cada commit publicado um `CommitId` monotônico e durável dentro daquele store. Timestamps são associados ao commit, mas empates, regressões do relógio e ajustes NTP não podem quebrar a ordenação.

### D6 — Full-text e hybrid retrieval pertencem ao banco; geração de embeddings não

O core pode:

- indexar texto;
- calcular BM25;
- receber vetores;
- executar ANN/exact search;
- combinar rankings;
- aplicar filtros e expansão de grafo;
- explicar scores.

O core não deve:

- chamar provedores de embedding;
- guardar API keys de modelos;
- executar LLMs;
- fazer reranking probabilístico obrigatório.

### D7 — `project`, `user/global` e `shared` são convenções sobre catálogos genéricos

O core conhece apenas **stores/catálogos nomeados**. Um package de workspace pode convencionar:

- `project` ou `main` para o store do workspace;
- `user` para o store global do usuário local;
- `shared` para um store explicitamente montado.

Isso mantém a capacidade útil para agentes, IDEs, aplicações desktop, analytics local e bases de referência.

### D8 — Uma transação escreve em um único store

Não implementar commit atômico transparente entre diretórios Grafx independentes.

- writes devem declarar um único catalog alvo;
- attached catalogs são read-only por padrão;
- cópia/promoção entre catalogs é uma operação lógica, explícita, idempotente e auditável;
- uma leitura federada deve declarar que usa snapshots independentes.

### D9 — Relações físicas não atravessam stores

Um edge físico sempre conecta nodes do mesmo store. Cross-store references, quando necessárias, são valores lógicos resolvidos pela aplicação ou por uma camada de federação.

### D10 — Extensibilidade pública não abre páginas, WAL ou mutable internals

A primeira SPI pública deve permitir funções, procedures, analyzers, importers e algoritmos usando DTOs e cursors públicos. Custom storage codecs, access paths ou mutation hooks dentro do commit protocol ficam fora da v1 da SPI.

### D11 — Extensões Python são trusted in-process code

O Grafx não deve prometer sandbox para extensões carregadas no mesmo processo. O carregamento é explícito, allowlisted e versionado. O core não baixa código automaticamente de um extension server.

### D12 — MCP é adapter de protocolo

A dependência deve ser sempre:

```text
okto-grafx-mcp
      ↓
okto-grafx-agent
      ↓
okto-grafx public API
```

Nunca:

```text
okto-grafx core → MCP SDK
```

### D13 — O banco não vira um barramento de coordenação

Inbox, handoff, presença, routing, broadcast, heartbeat, delegation e scheduling pertencem ao Okto Nexus ou a outro coordenador. Grafx guarda estado, conhecimento, relações, histórico e proveniência.

### D14 — Proveniência é genérica

O core pode aceitar metadados limitados de transação como `actor`, `origin`, `correlation_id` e `reason`. A interpretação semântica desses campos pertence ao consumidor.

### D15 — Nenhum daemon ou servidor é necessário para as capacidades deste plano

Um server mode futuro não está bloqueado pela arquitetura, mas não é requisito nem eixo prioritário deste roadmap.

### D16 — Toda feature persistida é negociada por manifest

Temporal history, FTS, novos índices e novos tipos de catálogo precisam de capability/format flags persistidos. Um build que não entende uma capability obrigatória deve recusar o open com erro tipado, nunca ignorá-la.

---

## 4. Seleção de funcionalidades de mercado

| Capacidade observada no mercado | Decisão para Grafx | Prioridade | Justificativa |
|---|---:|---:|---|
| Time travel / temporal query | adotar | P0 complementar | combina com MVCC, forensics, auditabilidade e knowledge graphs |
| System-time + valid-time | adotar em fases | P0/P1 | evita confundir “quando o banco soube” com “quando era verdade” |
| Full-text / BM25 | adotar | P0 complementar | cobre termos exatos, códigos, nomes e texto técnico que vetores perdem |
| Hybrid lexical + vector | adotar | P0/P1 | reforça o diferencial graph+vector e casos de retrieval |
| Graph-aware ranking | adotar | P1 | permite usar proximidade/estrutura sem LLM no core |
| Attached databases/catalogs | adotar com escopo restrito | P0/P1 | materializa local/global sem misturar todos os projetos em um store |
| Graph projections | adotar | P1 | base segura para analytics sem poluir o grafo persistido |
| Graph algorithms | adotar como package opcional | P1 | capacidade esperada de um graph DB, sem inflar o engine transacional |
| UDFs/procedures | adotar | P1 | amplia a linguagem sem modificar o parser para cada função |
| Extension ecosystem | adotar primeiro local/allowlisted | P1 | permite crescer sem acoplar tudo ao core |
| Arrow/DataFrame/Parquet/CSV | adotar incrementalmente | P1/P2 | reduz fricção de ingestão, analytics e integração Python |
| Application schema migrations | adotar | P1 | requisito de produto distinto da migração do formato físico |
| Logical/materialized views | adotar depois | P2 | útil para derived graphs; incremental refresh depende de changefeed |
| Live queries | deferir | P2 futuro | deve nascer sobre o changefeed lógico já planejado |
| Geospatial | backlog exploratório | P3 | útil, mas não reforça a tese central neste momento |
| Multi-language bindings | backlog após API estável | P3 | custo alto antes da estabilização do formato/API |
| WASM/browser | não priorizar | P3 | terreno onde Ladybug já é mais forte; não reforça multiprocess local |
| Cluster distribuído | fora deste plano | — | muda garantias, storage e operação; não é extensão pequena do embedded |
| Autenticação/RBAC no core | fora deste plano | — | filesystem/process boundary é o modelo atual; remote server exigiria outro threat model |
| Document database schemaless | rejeitar como direção | — | descaracteriza o property graph tipado; `MAP` já cobre payloads flexíveis |
| LLM/embedding provider no core | rejeitar | — | dependência externa, secrets, não determinismo e acoplamento de produto |
| Paridade total de Cypher como meta isolada | rejeitar | — | ampliar a linguagem por casos reais e conformance, não por checklist infinito |

---

## 5. Arquitetura-alvo

### 5.1 Camadas

```text
┌──────────────────────────────────────────────────────────────────┐
│ Aplicações Python, Pulse, IDEs, workers, notebooks               │
└─────────────────────────────┬────────────────────────────────────┘
                              │ API pública typed
┌─────────────────────────────▼────────────────────────────────────┐
│ okto-grafx core                                                  │
│                                                                  │
│ storage / WAL / recovery / MVCC / OCC / verify                   │
│ graph query / vector / temporal / full-text / catalogs           │
│ commit metadata / extension SPI                                  │
└───────────────┬──────────────────────┬───────────────────────────┘
                │                      │
       optional packages      optional product layers
                │                      │
┌───────────────▼────────────┐  ┌──────▼───────────────────────────┐
│ okto-grafx-algorithms      │  │ okto-grafx-agent                │
│ okto-grafx-interop         │  │ okto-grafx-workspace            │
│ third-party extensions     │  └──────┬───────────────────────────┘
└────────────────────────────┘         │
                                ┌──────▼───────────────────────────┐
                                │ okto-grafx-mcp                   │
                                │ stdio adapter / resources/tools │
                                └──────────────────────────────────┘
```

### 5.2 Organização recomendada do repositório

Durante a evolução inicial, manter um monorepo facilita testes de compatibilidade e mudanças atômicas, mas separar wheels/import graphs:

```text
src/okto_grafx/                         # core
packages/okto-grafx-algorithms/         # optional wheel
packages/okto-grafx-interop/            # optional wheel
packages/okto-grafx-workspace/          # generic path/catalog conventions
packages/okto-grafx-agent/              # optional agent schema/API
packages/okto-grafx-mcp/                # optional MCP adapter
```

O teste de import boundary deve provar:

- o core não importa nenhum package opcional;
- `agent` não importa MCP;
- `mcp` usa apenas APIs públicas de `agent` e `okto_grafx`;
- `algorithms` e `interop` não acessam `engine.*` ou pages internas;
- nenhuma extensão é necessária para abrir um banco sem aquela capability.

---

## 6. Capability A — Commit identity e metadados genéricos

### 6.1 Objetivo

Criar uma identidade lógica estável para cada commit publicado e permitir metadados limitados, úteis para temporalidade, auditoria, changefeed, replicação futura e integrações agentic.

### 6.2 Contrato proposto

```python
from okto_grafx import CommitMetadata

with db.begin(
    "write",
    metadata=CommitMetadata(
        actor="pulse:indexer",
        origin="okto-pulse",
        correlation_id="run_123",
        reason="refresh-source",
        attributes={"board_id": "board_42"},
    ),
) as txn:
    ...
```

`CommitMetadata` deve ser:

- opcional;
- imutável depois do begin;
- limitado por bytes e número de chaves;
- composto apenas por tipos públicos canônicos e pequenos;
- redigível na observabilidade;
- persistido atomicamente com o commit lógico;
- acessível por APIs de history/changefeed, não pelo WAL físico.

### 6.3 `CommitId`

Requisitos:

- monotônico dentro de um store;
- único entre processos que escrevem aquele store;
- atribuído antes da publicação final, mas nunca reutilizado depois de ficar durable;
- recuperável/idempotente após crash;
- comparável sem usar wall clock;
- serializável em export/import lógico;
- distinto de transaction id efêmero e de page LSN interno.

Pode ser derivado de um commit LSN se, e somente se, o contrato de WAL provar estabilidade e unicidade suficientes. Caso contrário, criar uma sequência própria persistida e coberta pelo mesmo protocolo de commit.

### 6.4 Timestamp de commit e regressão de relógio

Persistir duas noções distintas:

- `observed_at`: wall clock observado pelo writer, útil para diagnóstico e correlação externa;
- `ordered_at`: timestamp lógico monotônico atribuído na etapa serializada de publicação do commit;
- `clock_adjusted`: indica que `ordered_at` precisou ser avançado porque o wall clock empatou ou regrediu.

Regra conceitual:

```text
ordered_at = max(observed_at, previous_ordered_at + minimum_representable_tick)
```

O `CommitId` continua sendo a ordem canônica. `system_as_of(timestamp=...)` resolve para o maior `CommitId` cujo `ordered_at` seja menor ou igual ao instante solicitado. A API pode expor `observed_at` para auditoria, mas nunca deve usá-lo sozinho para ordenar commits ou inferir causalidade entre processos.

### 6.5 Invariantes

- nenhum commit retornado como sucesso existe sem `CommitId` publicado;
- recovery nunca cria dois `CommitId` para o mesmo commit;
- commit metadata não pode fazer um commit anteriormente válido falhar depois da barreira de durabilidade;
- metadata inválida é recusada antes de qualquer persistência;
- secrets e prompts completos não são aceitos por convenção/documentação.

### 6.6 Métricas

- `oktografx_commits_with_metadata_total`;
- `oktografx_commit_metadata_bytes_total`;
- `oktografx_commit_id_high_watermark`;
- rejeições por budget de metadata.

---

## 7. Capability B — Grafo temporal e time travel

### 7.1 Objetivo

Permitir consultar como nodes, relationships e propriedades existiam em outro momento, sem depender das versões internas de MVCC e sem transformar toda tabela em histórico por padrão.

### 7.2 Modelo temporal em fases

#### Fase T1 — System-time

O banco registra automaticamente quando cada versão lógica passou a fazer parte do estado publicado.

Exemplos de perguntas:

- como o grafo estava no `CommitId 18042`?
- quais relações existiam ontem às 18h?
- quais versões de um node existiram entre dois commits?
- o que mudou entre duas execuções?

#### Fase T2 — Application/valid-time

A aplicação declara o período em que a informação é considerada válida no domínio.

Exemplo:

- o banco recebeu hoje uma correção dizendo que um vínculo era válido desde janeiro;
- system-time = hoje;
- valid-time = janeiro até determinada data.

#### Fase T3 — Bitemporal

A query combina as duas dimensões:

> O que o banco acreditava em 15 de agosto sobre o que era válido em 1º de julho?

### 7.3 Opt-in por tabela

Temporalidade precisa ser explícita para controlar storage e write amplification.

API inicial sugerida:

```python
db.schema.create_node_table(
    "Person",
    columns=...,
    primary_key="id",
    temporal="system",
)
```

Evolução de DDL possível, depois da semântica estabilizada:

```cypher
CREATE NODE TABLE Person(
  id UUID,
  name STRING,
  PRIMARY KEY(id)
) WITH SYSTEM VERSIONING
```

Para valid-time:

```cypher
CREATE NODE TABLE Assignment(
  id UUID,
  valid_from TIMESTAMP,
  valid_to TIMESTAMP,
  PRIMARY KEY(id),
  PERIOD FOR VALID_TIME(valid_from, valid_to)
) WITH SYSTEM VERSIONING
```

### 7.4 Representação lógica

Cada versão temporal possui conceitualmente:

```text
entity_id
version_id
system_from_commit
system_to_commit | infinity
system_from_ordered_at
system_to_ordered_at | infinity
valid_from | optional
valid_to | optional
payload
commit_metadata_ref | optional
```

O wall clock bruto (`observed_at`) pertence ao registro do commit. Os intervalos temporais usam `CommitId` e `ordered_at`, evitando gaps ou inversões causados por ajuste NTP, empate de precisão ou regressão do relógio do sistema.

Esses campos podem ser implementados em sidecar history heaps, record kinds próprios ou estruturas equivalentes. A estrutura física não deve aparecer como uma tabela de usuário que possa ser alterada diretamente.

### 7.5 API typed antes da extensão Cypher

```python
snapshot = db.temporal.system_as_of(commit_id=18042)

with db.begin("read", temporal=snapshot) as txn:
    rows = txn.execute("MATCH (p:Person) RETURN p.id, p.name")
```

Também:

```python
snapshot = db.temporal.system_as_of(timestamp=Timestamp(...))
window = db.temporal.system_between(from_commit=17000, to_commit=18042)
valid = db.temporal.valid_as_of(Timestamp(...))
bitemporal = db.temporal.at(system=..., valid=...)
```

A sintaxe Cypher deve ser definida em ADR posterior. Um formato possível é `FOR SYSTEM_TIME`, mas o milestone não deve depender de escolher essa gramática antes da API typed.

### 7.6 Semântica de grafo histórico

Uma relationship version só é retornada se, no contexto temporal consultado:

- a própria relação estiver visível;
- o source node estiver visível;
- o target node estiver visível;
- os endpoint identities corresponderem à mesma linhagem lógica.

Traversal histórico não pode produzir um edge órfão apenas porque a história da relação e a história do node foram lidas separadamente.

### 7.7 Delete, recreate e primary key reuse

- update fecha a versão anterior e abre outra;
- delete fecha a versão corrente;
- `DETACH DELETE` fecha node e relações no mesmo commit temporal;
- recriar a mesma primary key depois de delete cria nova linhagem por padrão;
- “resurrection” da mesma identidade exige operação explícita e policy definida;
- diff deve distinguir `delete + create` de `update`.

### 7.8 Query de versões e diff

API proposta:

```python
versions = db.temporal.versions(
    table="Person",
    identity={"id": person_id},
    between=(commit_a, commit_b),
)

changes = db.temporal.diff(
    from_=db.temporal.commit(commit_a),
    to=db.temporal.commit(commit_b),
    tables=["Person", "KNOWS"],
)
```

Cada mudança retorna:

- `created`, `updated`, `deleted` ou `recreated`;
- identity e table;
- before/after;
- commit id/timestamp;
- metadata/provenance permitida;
- relationship endpoints quando aplicável.

### 7.9 Retenção

Retenção temporal é separada de vacuum MVCC:

```text
none                    # história indefinida
keep_last_versions=N
keep_for=duration
keep_since_commit=X
manual
```

Regras:

- retention nunca apaga current state;
- pruning é transacional, verificável e retomável;
- backup/export informa o menor commit histórico presente;
- query anterior ao horizon retorna erro tipado, não conjunto vazio enganoso;
- pinned historical readers impedem pruning de intervalos ainda em uso;
- `verify()` valida continuidade e não sobreposição de intervalos por identidade.

### 7.10 Índices temporais

Adicionar access paths para:

- `(table_id, entity_id, system_from_commit)`;
- interval containment para system-time;
- interval containment para valid-time;
- `CommitId → timestamp/metadata`;
- diff por commit range.

O planner deve declarar quando uma consulta temporal cai em scan.

### 7.11 Interação com recovery

O history effect faz parte do mesmo commit lógico do current state. É proibido:

- publicar o current row sem fechar/registrar a history version correspondente;
- publicar history sem current effect;
- reconstruir história apenas a partir do WAL físico depois de reciclagem;
- tratar clock wall como fonte de replay.

### 7.12 Gate de aceite temporal

- create/update/delete/recreate consultáveis por commit e timestamp;
- nodes e relationships preservam consistência histórica em traversal;
- crash em cada etapa não cria gaps, overlaps ou história sem current state;
- duas writers concorrentes produzem ordem lógica inequívoca;
- vacuum MVCC não remove história de produto;
- temporal retention não remove versão ainda pinada;
- export/import preserva history e `CommitId` mapping ou documenta remapping explícito;
- `verify()` detecta intervalos impossíveis, referências ausentes e history corruption;
- benchmarks medem write amplification, query p50/p99 e crescimento de history.

---

## 8. Capability C — Catálogos anexados e scopes local/global

### 8.1 Objetivo

Permitir que uma sessão trabalhe com mais de um store Grafx nomeado, preservando o isolamento físico. A convenção project/global passa a ser construída sobre uma capacidade genérica de banco.

### 8.2 Modelo

```text
main/project store
<workspace>/.grafx/store/

user/global store
<user-data-dir>/okto-grafx/stores/user/

optional shared/reference stores
<explicit-path>/
```

O core não conhece esses paths padrão. Ele recebe handles/path resolvidos pelo caller ou pelo package `okto-grafx-workspace`.

### 8.3 API proposta

```python
from okto_grafx import CatalogSession

session = CatalogSession.open("./.grafx/store")
session.attach(
    "~/.local/share/okto-grafx/stores/user",
    alias="user",
    mode="read_only",
)

with session.begin("read", catalog="user") as txn:
    ...

with session.begin("write", catalog="main") as txn:
    ...
```

Operações:

- `attach(path, alias, mode="read_only")`;
- `detach(alias)`;
- `catalogs()`;
- `use(alias)` como default da sessão;
- `begin(..., catalog=alias)`;
- `copy(source, target, selection, idempotency_key)`;
- `search_federated(...)` em milestone posterior.

### 8.4 Regras de resolução

No helper de workspace:

1. caminho explícito em CLI/config;
2. root de projeto explicitamente fornecido pelo harness;
3. subida determinística procurando marker permitido, até limite configurado;
4. diretório atual somente quando policy permitir;
5. store global do usuário apenas quando explicitamente habilitado.

A resolução retorna um objeto imutável:

```python
ResolvedWorkspace(
    root=...,
    project_store=...,
    user_store=...,
    source="explicit|root|marker|cwd",
)
```

### 8.5 Segurança

- paths de attached stores nunca vêm de texto livre enviado por um agente;
- symlink/canonical path policy explícita;
- allowlist de roots;
- attached store read-only por padrão;
- alias reservado `main` não pode ser substituído;
- aliases únicos e ASCII;
- nenhuma busca automática fora do root permitido;
- nenhum remote URL nesta fase.

### 8.6 Semântica transacional

- cada transaction pertence a um único catalog;
- não existe two-phase commit entre stores;
- attached read-only store possui seu próprio snapshot;
- consulta federada retorna um token por store e marca `consistency="independent"`;
- falha em um store não pode ser escondida como resultado parcial sem flag explícita;
- `close()` tenta fechar todos os handles e preserva o primeiro erro com evidência dos demais.

### 8.7 Nome de tabela e query

Primeiro milestone: catalog escolhido no begin/session, sem alterar Cypher.

Segundo milestone opcional:

```cypher
USE CATALOG user
MATCH (n:Skill) RETURN n
```

Cross-catalog query em uma única statement deve ficar fora do primeiro milestone. Uma futura gramática qualificada precisa de ADR e não pode fingir atomicidade global.

### 8.8 Cópia/promoção entre scopes

```python
result = session.copy(
    source="main",
    target="user",
    selection=GraphSelection(nodes=[...], relationships="induced"),
    conflict="fail|skip|merge_explicit",
    idempotency_key="promote-knowledge-123",
    metadata=...,
)
```

A operação:

1. lê um snapshot do source;
2. materializa pacote lógico/checksum;
3. abre write transaction no target;
4. aplica policy explícita;
5. registra source store UUID, source commit id e idempotency key;
6. nunca cria edge sem seus endpoints;
7. pode ser repetida após falha sem duplicar dados.

### 8.9 Casos de uso genéricos

- configuração/knowledge compartilhado entre projetos;
- catálogos de referência read-only;
- dados globais de um aplicativo desktop;
- fixtures/test data montados temporariamente;
- migração gradual entre stores;
- agentes usando projeto + conhecimento global;
- analytics sobre snapshots externos.

### 8.10 Gate de aceite

- dois stores podem ser abertos e fechados com lifecycle correto;
- writes nunca atingem catalog diferente do declarado;
- attached read-only recusa qualquer mutação antes de tocar disco;
- aliases e path policies são fail-closed;
- nenhum cross-store edge físico é criado;
- copy é idempotente após crash antes/depois do commit target;
- federated read identifica snapshots e falhas por store;
- dois MCPs no mesmo workspace resolvem exatamente o mesmo project store.

---

## 9. Capability D — Full-text search

### 9.1 Objetivo

Adicionar recuperação lexical nativa para termos exatos, nomes, IDs, códigos, símbolos, mensagens de erro e linguagem técnica, complementando a busca vetorial.

### 9.2 Escopo inicial

- propriedades `STRING` de node tables;
- múltiplas propriedades por índice;
- índice invertido persistido;
- BM25 como ranking inicial;
- atualização transacional com tombstones/version visibility;
- filtros estruturados;
- analyzers persistidos no schema;
- busca via API typed e procedure query;
- relationship properties em milestone posterior.

### 9.3 DDL proposta

```cypher
CREATE FULLTEXT INDEX document_text
ON Document(title, body)
WITH (
  analyzer = 'standard',
  scorer = 'bm25'
)
```

Analyzer para código:

```cypher
CREATE FULLTEXT INDEX symbol_text
ON Symbol(name, qualified_name, signature)
WITH (
  analyzer = 'code_identifier',
  scorer = 'bm25'
)
```

### 9.4 Analyzer contract

Built-ins iniciais:

- `keyword`: valor inteiro como um token;
- `standard`: Unicode normalization, case folding configurável e tokenização textual;
- `code_identifier`: preserva termo integral e também separa `snake_case`, `camelCase`, `dot.path`, `pkg::symbol` e dígitos;
- `whitespace`: mínimo previsível para debugging.

Configurações persistidas:

- versão do analyzer;
- normalization;
- stemming/stopword profile quando suportado;
- max token length;
- field weights;
- locale explícito ou `und`.

O analyzer version faz parte da identidade do índice. Upgrade incompatível exige rebuild.

### 9.5 API

```python
result = db.search_text(
    reader,
    index="document_text",
    query="identity lease page zero",
    k=20,
    filter=RecordIdFilter(...),
)
```

Cada hit retorna:

```text
record_id
score
matched_fields
matched_terms (bounded)
regime = exact_index | fallback_scan
index_built_through_commit
snapshot_commit
```

### 9.6 Snapshot e freshness

FTS é um access path, não uma fonte paralela da verdade.

- resultado deve respeitar o snapshot do reader;
- stale subset nunca é usado como se fosse completo;
- índice pode ser validado contra heap quando for superset, seguindo o contrato existente;
- tombstones/horizon precisam lidar com updates/deletes;
- cold build é single-flight e publicável apenas quando completo;
- rebuild não bloqueia readers corretos: eles usam fallback ou a geração anterior válida;
- DDL/update/recovery incluem FTS effects no mesmo modelo de redo.

### 9.7 Budgets

- max query tokens;
- max postings visits;
- max candidate rows;
- max highlight bytes;
- deadline/cancellation;
- memory budget por operator;
- recusa tipada antes de OOM.

### 9.8 Gate de aceite

- BM25 determinístico em corpus fixo;
- exact names/IDs encontrados sem embedding;
- updates/deletes não retornam postings stale;
- concorrência de build não retorna índice parcial;
- crash/recovery preserva ou reconstrói sem resposta curta silenciosa;
- seletividade e filtros medidos;
- analyzer versions e rebuild testados;
- p50/p99, index size, write amplification e peak RSS publicados.

---

## 10. Capability E — Hybrid retrieval texto + vetor + grafo

### 10.1 Objetivo

Combinar sinais complementares em uma única API de banco:

- lexical/BM25 para correspondência exata;
- vector similarity para semântica;
- graph structure para contexto, proximidade e restrições;
- filtros tipados para escopo e segurança.

### 10.2 API proposta

```python
result = db.search_hybrid(
    reader,
    text=TextQuery(index="document_text", query="lease contention"),
    vector=VectorQuery(space="docs", query=embedding),
    graph=GraphContext(
        seeds=[record_id],
        relationship_types=["DEPENDS_ON", "IMPLEMENTS"],
        max_hops=2,
        direction="both",
    ),
    fusion=ReciprocalRankFusion(k=60),
    limit=20,
)
```

### 10.3 Estratégias de fusão

V1:

- Reciprocal Rank Fusion (`rrf`), robusta a escalas diferentes;
- weighted RRF com pesos explícitos;
- union/intersection de candidate sets;
- deterministic tie-break por stable identity.

V2:

- score normalization por source;
- graph proximity boost;
- diversity/MMR determinístico opcional;
- query planner escolhendo candidate source primário por seletividade.

Nenhuma estratégia depende de LLM.

### 10.4 Resultado explicável

Cada hit expõe:

```text
record_id
final_score
text_rank / text_score
vector_rank / vector_distance
vector_regime = exact | approximate
graph_distance / paths_considered
filters_applied
fusion_method
candidate_sources
snapshot information
```

O caller deve conseguir distinguir:

- resultado lexical;
- resultado semântico;
- resultado promovido pela estrutura;
- ANN aproximado;
- fallback por índice indisponível;
- partial result autorizado por budget.

### 10.5 Graph-aware retrieval

O sinal de grafo não deve executar traversal exponencial implícito. Requer:

- seed nodes explícitos;
- relationship type allowlist;
- hop bound pequeno;
- expansion budget;
- cycle/relationship isomorphism consistente;
- opção de filtrar candidatos antes ou depois da fusão;
- explicação bounded, sem retornar todos os paths por padrão.

### 10.6 Degradação honesta

- sem vector query: lexical + graph;
- sem FTS index: vector + graph ou erro, conforme policy;
- HNSW indisponível: exact vector se budget permitir, ou erro tipado;
- graph budget excedido: retorna erro ou partial explicitamente marcado;
- nenhum caminho pode rotular um resultado incompleto como completo.

### 10.7 Gate de aceite

- corpus de conformance com termos exatos, paráfrases e relações;
- recall/precision comparados por source e fusion;
- determinismo de RRF;
- filtros não são aplicados depois de vazar candidatos proibidos;
- p99 e peak RSS sob budgets;
- cold/warm FTS e HNSW;
- resultados carregam breakdown completo;
- fallback e partial regimes cobertos por testes públicos.

---

## 11. Capability F — SPI de extensões, UDFs e procedures

### 11.1 Objetivo

Permitir que o Grafx cresça sem incorporar cada função no core e sem dar a extensions acesso às invariantes de storage.

### 11.2 Tipos de extensão v1

- scalar UDF;
- aggregate UDF;
- table function/read-only procedure;
- administrative procedure explicitamente privilegiada;
- FTS analyzer/tokenizer;
- import/export format adapter;
- graph algorithm provider;
- result serializer.

Fora da v1:

- custom WAL records;
- custom page codecs carregados dinamicamente;
- mutation hooks dentro do commit;
- custom index access paths que escrevem páginas internas;
- storage adapters não auditados carregados por um banco existente.

### 11.3 Manifest

```python
ExtensionManifest(
    name="okto-grafx-algorithms",
    version="0.1.0",
    grafx_api="~=0.2",
    capabilities=("procedure", "algorithm"),
    deterministic=True,
    thread_safe=True,
    side_effects="none",
)
```

### 11.4 Registro

- Python entry points ou registro explícito;
- nenhuma instalação automática por query;
- allowlist opcional por `OpenOptions`;
- load por session/process, com definitions persistidas apenas quando necessário;
- engine compatibility validada antes da execução;
- duplicate names recusados ou namespaced.

### 11.5 Contrato de execução

Uma UDF recebe apenas valores públicos imutáveis e contexto limitado:

- parâmetros;
- read-only transaction view quando declarado;
- cancellation/deadline;
- memory/work budget;
- logger/event sink sanitizado;
- nenhuma referência ao BufferPool, HeapStore, WAL ou mutable Catalog.

Functions com side effects são recusadas dentro de expressões. Procedures que escrevem precisam abrir uma operação pública estruturada e são separadas das funções puras.

### 11.6 Falhas

- exception comum vira `GrafxExtensionError` tipado;
- extension não pode converter um erro do engine em sucesso;
- failure antes do commit faz rollback normal;
- failure depois de chamar operação pública segue o contrato dessa operação;
- extension timeout/cancellation é observável;
- traceback pode ir a log local sanitizado, não necessariamente ao query result.

### 11.7 Gate de aceite

- projeto consumidor externo registra UDF e procedure sem importar internals;
- mypy/pyright validam o SPI;
- extension incompatível é recusada no load;
- functions determinísticas possuem conformance fixtures;
- exceptions não envenenam o processo nem deixam transaction em estado ambíguo;
- load/unload concorrente segue lifecycle definido;
- documentação de trust model explícita.

---

## 12. Capability G — Interoperabilidade e external scans

### 12.1 Objetivo

Reduzir o custo de trazer dados para o grafo, analisá-los em Python e exportar resultados, sem transformar o core em um lakehouse connector framework.

### 12.2 Ordem recomendada

1. Arrow result export;
2. Pandas/Polars adapters sobre Arrow;
3. CSV/JSON local scan;
4. Parquet local scan via extra opcional;
5. `COPY`/bulk materialization;
6. NetworkX graph projection/export;
7. ADBC e remote/object-store connectors somente depois.

### 12.3 Arrow

- suporte ao Arrow C Data Interface quando disponível;
- batch streaming, não materialização integral obrigatória;
- schemas tipados e estáveis;
- vectors mapeados para fixed-size list/extension type documentado;
- RecordId/UUID sem perda;
- relationship endpoints exportados explicitamente;
- snapshot pertence ao cursor até o último batch/close.

### 12.4 External scan

API inicial:

```python
source = grafx.scan.csv("nodes.csv", schema=...)
source = grafx.scan.parquet("data/*.parquet")
source = grafx.scan.arrow(table)
```

Integração query futura:

```cypher
LOAD FROM $source
RETURN ...
```

Regras:

- source é read-only;
- projection/filter pushdown quando seguro;
- budgets e cancellation;
- filesystem paths passam por policy;
- nenhum URL remoto por padrão;
- malformed input produz erro localizado com row/batch/column;
- `COPY` para tabelas Grafx usa bulk ingest do plano existente.

### 12.5 Graph exchange

Export lógico graph-aware deve preservar:

- table definitions;
- node identities;
- relationship multiplicity, direction e self-loops;
- vector space metadata;
- temporal metadata quando solicitado;
- source commit id/checksum.

Não criar um segundo formato de backup. Graph exchange é interoperabilidade; export/import lógico do plano existente continua sendo a rota de migração confiável do banco.

### 12.6 Gate de aceite

- Arrow round-trip de todos os ValueTypes;
- streaming bounded por memória;
- CSV/Parquet errors localizados;
- copy idempotente conforme policy;
- graph export preserva multiplicidade e identities;
- optional dependencies não entram no wheel core;
- benchmarks de throughput e peak RSS.

---

## 13. Capability H — Projeções e algoritmos de grafo

### 13.1 Objetivo

Oferecer analytics de grafo sem executar algoritmos iterativos diretamente sobre mutable storage a cada passo e sem colocar uma biblioteca inteira dentro do engine transacional.

### 13.2 Projection API no core

```python
projection = db.graphs.project(
    reader,
    name="dependency_graph",
    nodes="MATCH (n:Symbol) RETURN n",
    relationships="MATCH (a:Symbol)-[r:CALLS]->(b:Symbol) RETURN a, r, b",
    orientation="directed",
    node_properties=["kind"],
    relationship_properties=["weight"],
    memory_budget_bytes=...,
)
```

A projection:

- é criada de um snapshot explícito;
- possui source store UUID e source commit id;
- é imutável para o algoritmo, salvo modo `mutate` próprio;
- tem lifecycle e close explícitos;
- é contabilizada no memory budget;
- pode ser ephemeral em memória primeiro;
- não é confundida com backup ou materialized view persistida.

### 13.3 Modos de algoritmo

- `stream`: retorna resultados sem persistir;
- `stats`: retorna métricas agregadas;
- `mutate`: adiciona propriedades apenas à projection;
- `write`: grava resultados no graph persistido via transação pública explícita.

`write` exige:

- target table/property declarados;
- commit metadata;
- conflict policy;
- idempotency key quando aplicável;
- nenhuma escrita parcial fora da transaction.

### 13.4 Algoritmos iniciais

Primeiro pacote:

- degree/in-degree/out-degree;
- weakly connected components;
- strongly connected components;
- PageRank;
- k-core.

Segundo pacote:

- Louvain/label propagation;
- similarity baseada em vizinhança;
- random walk/sampling;
- centralidades adicionais.

Shortest path pertence às capacidades de grafo já previstas no plano principal e deve ser reutilizado, não duplicado.

### 13.5 Por que package opcional

- dependências numéricas podem ser diferentes do core;
- analytics possui lifecycle e memória próprios;
- permite aceleração nativa futura;
- evita que o engine transacional carregue algoritmos não usados;
- facilita integração com NetworkX/PyG sem acoplar storage.

### 13.6 Gate de aceite

- resultados validados contra corpus de referência/NetworkX em grafos pequenos;
- direction, weights, parallel edges e self-loops definidos por algoritmo;
- budgets/cancellation interrompem sem leak;
- projection pin protege snapshot até materialização;
- `write` é atômico e conflict-aware;
- performance cresce de forma documentada;
- package não importa internals.

---

## 14. Capability I — Evolução de schema da aplicação

### 14.1 Distinção necessária

Há dois problemas diferentes:

1. **migração do formato físico Grafx**, já coberta por export/import no plano existente;
2. **evolução do schema criado pelo usuário**, ainda não fechada.

Este capítulo trata apenas do segundo.

### 14.2 Operações em fases

#### Fase S1 — metadata-safe

- rename de table;
- rename de column;
- add nullable column;
- add column com constant default;
- add/drop index;
- add constraint validável sem rewrite;
- comentários/metadata de schema.

#### Fase S2 — backfill resumível

- add non-null column com backfill;
- type widening compatível;
- computed migration via função determinística;
- rebuild de índice/FTS;
- migration de temporal policy.

#### Fase S3 — destructive/offline

- drop column;
- incompatible type change;
- alteração de primary key;
- alteração de relationship endpoint tables;
- merge/split de tables.

Essas operações podem exigir export/import ou rewrite para novo diretório. Não prometer online migration quando não há rollback seguro.

### 14.3 Migration ledger

```python
Migration(
    id="2026-08-add-symbol-language",
    from_schema=4,
    to_schema=5,
    checksum="...",
    operations=(...),
)
```

O ledger registra:

- id/checksum;
- versão anterior/posterior;
- started/completed commit id;
- state `planned|running|completed|failed`;
- progress/checkpoint para backfill;
- tool/application origin;
- failure evidence.

### 14.4 Regras

- migration idempotente;
- checksum divergente para o mesmo id é erro;
- duas migrations concorrentes do mesmo catalog são serializadas;
- DDL e backfill possuem budget;
- readers antigos veem schema epoch compatível ou falham tipado;
- prepared plans são invalidados por schema epoch;
- temporal/history schema acompanha evolução de forma verificável;
- backup antes de destructive migration recomendado/enforced por policy futura.

### 14.5 Gate de aceite

- S1 crash-safe e transacional;
- backfill retoma sem duplicar/apagar dados;
- schema epoch invalida plans/indexes corretos;
- rollback/compensation documentados por operação;
- history e current payload continuam decodificáveis;
- fixtures n−1/n para schema da aplicação;
- migration dry-run fornece espaço, rows e operações estimadas.

---

## 15. Capability J — Views e derived graphs

### 15.1 Logical views

Salvar uma query typed/nomeada no catalog:

```python
db.views.create(
    "active_dependencies",
    query="MATCH ... RETURN ...",
    parameters_schema=...,
)
```

Requisitos:

- schema/catalog epoch no plan cache;
- dependências de tables/indexes registradas;
- read-only;
- expansão recursiva limitada e cycle-safe;
- `SHOW VIEW`/introspection;
- drop/replace transacional.

### 15.2 Materialized/derived graph

Usar para:

- relações derivadas;
- embeddings/aggregates pré-computados;
- resultados de algoritmo;
- subgrafos de trabalho.

Primeira versão:

- refresh manual e transacional;
- source commit id registrado;
- stale status explícito;
- rebuild total;
- swap atômico de geração.

Incremental refresh só depois do changefeed lógico do plano principal e de prova de idempotência.

### 15.3 Gate de aceite

- logical view nunca escreve;
- dependency invalidation correta;
- materialized view informa source commit/staleness;
- refresh falho preserva geração anterior;
- readers nunca observam geração parcial;
- cycle e parameter schema testados.

---

## 16. Capability K — Agentes como first-class consumers, não core citizens

### 16.1 Definição precisa

“Agentes como first-class citizens” significa que o produto oferece uma camada opcional onde agentes possuem:

- identidade;
- sessões;
- autoria/proveniência;
- operações idempotentes;
- memória;
- claims e evidências;
- acesso a project/user catalogs;
- retrieval textual, vetorial, temporal e estrutural;
- interface Python e MCP.

Não significa que o storage engine conheça LLMs ou protocolos agentic.

### 16.2 Packages

```text
okto-grafx-agent
  - schema e migrations próprios
  - AgentWorkspace
  - AgentIdentityProvider
  - Memory/Claim/Evidence services
  - policies e namespace registry
  - usa CommitMetadata, catalogs, temporal e hybrid search

okto-grafx-mcp
  - transporte stdio
  - tools/resources/prompts opcionais
  - validação MCP
  - mapeamento de erros
  - nenhuma regra de storage própria
```

### 16.3 Schema agentic opcional

Entidades mínimas:

- `AgentProfile`;
- `AgentSession`;
- `AgentOperation`;
- `MemoryItem`;
- `Claim`;
- `Evidence`;
- `SourceReference`;
- `NamespaceDefinition`;
- `ScopeCopyReceipt`.

Relações possíveis:

- `ASSERTED_BY`;
- `OBSERVED_IN`;
- `SUPPORTED_BY`;
- `CONTRADICTS`;
- `CORROBORATES`;
- `SUPERSEDES`;
- `RETRACTS`;
- `DERIVED_FROM`;
- `CREATED_DURING`.

Esse schema é criado/migrado pelo package agent, sob names reservados. Um banco Grafx sem esse package não possui obrigação de criá-lo.

### 16.4 Liberdade de schema do agente

O agente pode criar ou usar schemas de domínio dentro de namespaces autorizados, mas não pode:

- alterar tables reservadas do package;
- declarar-se outro actor;
- gravar fora do catalog permitido;
- criar paths de banco;
- executar DDL arbitrário quando a policy não permitir;
- escrever Cypher raw contornando provenance.

`NamespacePolicy` define:

```text
namespace
owner/application
allowed catalogs
allowed table prefixes
DDL permissions
write operations
retention defaults
schema version
```

### 16.5 Claims não são fatos automáticos

Um claim possui:

- subject/predicate/object ou payload estruturado;
- asserted_by;
- asserted_at commit id;
- valid-time opcional;
- confidence opcional, sempre declarada como opinião do producer;
- evidence links;
- status `active|contested|superseded|retracted|verified`;
- source scope/catalog.

Agentes diferentes podem registrar afirmações contraditórias. A camada preserva a contradição e permite consulta; não escolhe silenciosamente o vencedor.

### 16.6 Project e user/global

Convenção:

- project store é `main` e write default;
- user store é attached como `user`, read-only por padrão;
- merged recall consulta ambos e informa source catalog por hit;
- write em “merged” é inválido;
- promoção para user é `scope_copy`, explícita e auditada;
- relações físicas não atravessam stores;
- claims copiados recebem receipt e source reference.

### 16.7 Workspace resolution

O processo MCP recebe na inicialização:

- project root/path explícito ou resolvido;
- project store;
- user store habilitado ou não;
- default write catalog;
- allowed read catalogs;
- agent identity;
- namespace/policy;
- budgets.

Nenhuma tool aceita `database_path`.

### 16.8 MCP `stdio`

O stdio é apenas transporte. Cada harness inicia seu próprio subprocesso; todos podem abrir o mesmo project store graças às garantias multiprocesso do Grafx.

```text
Claude Code ──> grafx-mcp stdio A ──┐
Codex ────────> grafx-mcp stdio B ──┼──> mesmo ./.grafx/store
IDE worker ───> grafx-mcp stdio C ──┘
```

Superfície inicial recomendada:

Tools:

- `grafx_status`;
- `grafx_schema_describe`;
- `grafx_query_read`;
- `grafx_search`;
- `grafx_remember`;
- `grafx_claim`;
- `grafx_evidence_attach`;
- `grafx_scope_copy`;
- `grafx_history` após temporal;
- `grafx_diff` após temporal.

Resources:

- `grafx://status`;
- `grafx://catalogs`;
- `grafx://schema/{catalog}`;
- `grafx://namespaces`;
- `grafx://agent/session`;
- `grafx://capabilities`.

Raw write Cypher não é tool padrão. Pode existir uma tool administrativa desabilitada por default e protegida por policy explícita.

### 16.9 Identidade e idempotência

A identidade é configurada pelo server process ou `AgentIdentityProvider`. O modelo não passa `asserted_by` livremente.

Toda mutação recebe:

- agent profile id;
- session id;
- operation id;
- idempotency key;
- commit metadata actor/origin/correlation;
- target catalog;
- tool name;
- bounded input hash;
- result receipt.

Retries do harness não podem duplicar memory/claim/evidence.

### 16.10 ContextPack e montagem de contexto

A camada agent pode transformar resultados do banco em um `ContextPack` limitado para consumo por um modelo, sem colocar tokenização ou prompt assembly no core.

```python
pack = workspace.context.build(
    query="onde a decisão de identity leasing foi tomada?",
    catalogs=("main", "user"),
    max_items=20,
    token_budget=6000,
    include_history=True,
    explain=True,
)
```

O pack contém:

- items selecionados;
- source catalog e identities;
- text/vector/graph score breakdown;
- temporal snapshot;
- evidence/source references;
- motivo de inclusão;
- conteúdo truncado de forma explícita;
- contagem estimada de tokens feita por adapter configurável;
- omissions/partial flags quando o budget impedir cobertura completa.

O banco retorna dados e rankings. A camada agent decide serialização, token budget e formato de contexto.

### 16.11 Lifecycle de memória

`MemoryItem` deve possuir lifecycle explícito, não apenas inserts acumulativos:

- `working`: curto prazo e normalmente expirável;
- `episodic`: evento ou execução situada no tempo;
- `semantic`: conhecimento consolidado;
- `procedural`: instrução/processo, quando permitido pela aplicação.

Operações:

- pin/unpin;
- expire;
- supersede;
- retract;
- compact/summarize por provider externo opcional;
- promote entre tipos ou catalogs;
- forget lógico;
- hard erase administrativo sujeito à retention/privacy policy.

Compaction produz um novo item `DERIVED_FROM` os itens anteriores; não deve apagar evidência silenciosamente. Qualquer sumarização probabilística pertence a um provider da camada agent e precisa registrar modelo/configuração/origem, nunca ao core.

### 16.12 Fronteira com o Nexus

Grafx:

- estado e conhecimento;
- memória;
- history;
- claims/evidence;
- search;
- provenance.

Nexus:

- messages;
- inbox;
- handoff;
- routing;
- capability discovery;
- presence;
- coordination.

Integração opcional por `nexus_agent_id`, `message_id` e `correlation_id`, sem dependência obrigatória.

### 16.13 Gate de aceite agentic preview

- dois harnesses iniciam MCPs independentes e veem o mesmo project store;
- write de A é recuperado por B após commit;
- actor/session são injetados, não forjados pelo input;
- retry não duplica mutation;
- project e user não vazam paths nem writes;
- merged recall marca source catalog;
- raw write continua desabilitado por default;
- fechar um MCP não afeta os demais;
- nenhum import de MCP/agent aparece no core.

---

## 17. Roadmap complementar priorizado

### Visão de dependências

```text
EVOLUTION M1
    │
    ▼
GX-CAP-0 Boundaries / ADRs
    │
    ├── GX-CAP-1 CommitId + metadata
    │       ├── GX-CAP-3 Temporal system-time ──► GX-CAP-4 valid-time / diff
    │       └── foundation for optional GX-AGENT-0
    │
    ├── GX-CAP-2 Catalogs / workspace
    │       └── foundation for optional GX-AGENT-0
    │
    ├── GX-CAP-5 Full-text ───────────────┐
    │                                     ├──► GX-CAP-6 Hybrid retrieval
    ├── EVOLUTION M2 HNSW / performance ─┘
    │
    ├── GX-CAP-7 Extension SPI ──┬──► GX-CAP-8 Interop / Arrow
    │                             └──► GX-CAP-9 Projections / algorithms
    │
    └── changefeed + maintenance ───► GX-CAP-10 Schema evolution / views

Database capability track ───────────────────────────► GX-CAP-11 hardening

Optional product-integration track:
GX-CAP-1 + GX-CAP-2 ───► GX-AGENT-0 MCP preview
GX-CAP-3 + GX-CAP-4 + GX-CAP-6 + lifecycle ───► GX-AGENT-1
```

### GX-CAP-0 — Arquitetura e contratos

**Pode iniciar:** imediatamente.

Entregas:

1. ADR database-first e feature ownership;
2. ADR temporal semantics;
3. ADR `CommitId`;
4. ADR attached catalogs/single-store transaction;
5. ADR FTS/hybrid index semantics;
6. ADR extension trust model;
7. package/import graph;
8. capability manifest strategy;
9. lista formal de itens já pertencentes ao plano existente;
10. specs separadas por milestone em `docs/specs/`.

Gate:

- zero ambiguidade sobre o que entra no core;
- testes de import boundary planejados;
- nenhum formato físico congelado sem crash/recovery spec;
- syntax extensions explicitamente posteriores à API typed.

### GX-CAP-1 — Commit identity e provenance foundation

**Depende:** M1 public facade/configuration.

Entregas:

- `CommitId`;
- `CommitMetadata` bounded;
- commit catalog/lookup;
- recovery idempotente;
- public typed views;
- métricas e verify scope;
- export/import hooks coordenados com o plano principal.

Gate:

- ordering multi-process provada;
- crash matrix completa;
- metadata atômica;
- nenhum wall-clock ordering bug;
- full suite e mutation tests das novas decisões.

### GX-CAP-2 — CatalogSession e workspace scopes

**Depende:** M1 lifecycle/facade.

Entregas:

- attach/detach read-only;
- begin pinado a catalog;
- store UUID/identity expostos de forma segura;
- generic workspace resolver package;
- project/user conventions;
- explicit copy/promotion receipts;
- path/security policies;
- CLI status/list catalogs.

Gate:

- single-catalog writes enforced;
- no cross-store edge;
- copy idempotente;
- read snapshots identificados por catalog;
- two-process workspace resolution conformance.

### GX-CAP-3 — Temporal system-time

**Depende:** GX-CAP-1; feature manifest; maintenance baseline.

Entregas:

- temporal opt-in para node/rel tables;
- current/history atomic write;
- as-of commit API;
- as-of timestamp mapping;
- versions API;
- historical traversal;
- retention manual;
- verify temporal.

Gate:

- create/update/delete/recreate conformance;
- crash/recovery sem gaps/overlaps;
- MVCC vacuum independence;
- performance/write amplification publicada.

### GX-CAP-4 — Valid-time, bitemporal e graph diff

**Depende:** GX-CAP-3.

Entregas:

- valid-time periods;
- overlap constraints configuráveis;
- bitemporal context;
- diff nodes/relationships/properties;
- retention policies completas;
- history query syntax/CLI após API estabilizada.

Gate:

- backdated corrections corretas;
- system vs valid semantics inequívocas;
- diff distingue update/recreate/delete;
- temporal indexes e planner evidence.

### GX-CAP-5 — Full-text search

**Depende:** index freshness, budgets e planner do plano existente.

Entregas:

- FTS index definition;
- standard/keyword/code analyzers;
- BM25;
- transactional updates/tombstones;
- search API/procedure;
- rebuild/versioning;
- cold/warm benchmark corpus.

Gate:

- snapshot correctness;
- no partial/stale silent answers;
- deterministic ranking;
- exact technical term corpus;
- memory and work budgets.

### GX-CAP-6 — Hybrid retrieval

**Depende:** GX-CAP-5 + HNSW real de M2.

Entregas:

- RRF/weighted RRF;
- candidate fusion;
- graph context boost/filter;
- score breakdown;
- fallback/partial regimes;
- conformance/recall harness.

Gate:

- gains measured against lexical-only/vector-only;
- no filter leakage;
- deterministic tie-breaking;
- cold/warm p99 e peak RSS;
- explicit exact/approximate semantics.

### GX-CAP-7 — Extension SPI e UDFs

**Depende:** GX-CAP-0; typed values/cursors.

Entregas:

- manifest/registry;
- scalar and aggregate UDF;
- read-only procedure/table function;
- analyzer and algorithm provider contracts;
- allowlist/trust docs;
- external consumer fixture.

Gate:

- no internal imports;
- compatibility refusal;
- extension failure isolation;
- typing/documentation CI.

### GX-CAP-8 — Arrow e external data

**Depende:** GX-CAP-7 + streaming cursor/bulk ingest do plano existente.

Entregas:

- Arrow batch export/import;
- Pandas/Polars adapters;
- CSV/JSON scans;
- Parquet optional extra;
- `COPY` integration;
- graph exchange mapping;
- local filesystem policy.

Gate:

- all ValueTypes round-trip;
- bounded memory;
- malformed input localization;
- optional dependency isolation.

### GX-CAP-9 — Graph projections e algorithms

**Depende:** GX-CAP-7 + query budgets/traversal.

Entregas:

- snapshot-bound projection catalog;
- degree, WCC, SCC, PageRank, k-core;
- stream/stats/mutate/write modes;
- NetworkX conformance;
- algorithm budgets and cancellation.

Gate:

- deterministic small-graph fixtures;
- weighted/directed/parallel/self-loop semantics;
- no snapshot leaks;
- write mode atomic.

### GX-CAP-10 — Application schema evolution e views

**Depende:** Commit metadata; schema/catalog epochs; backup/export; changefeed para incremental refresh.

Entregas:

- S1 migrations;
- migration ledger/dry-run;
- resumable backfill subset;
- logical views;
- manual materialized refresh;
- stale/source commit metadata.

Gate:

- crash-safe migration;
- plan invalidation;
- generation swap atomic;
- no silent incompatible reads.

### GX-CAP-11 — Hardening e release do banco

Entregas:

- cross-capability crash matrix das capacidades database-first;
- format fixtures;
- full suite/mutation/fault battery;
- docs consumidoras executadas;
- compatibility matrix;
- operational runbooks;
- release criteria e benchmark disclosure;
- packages opcionais possuem gates próprios e não bloqueiam o release do core quando não fazem parte do artefato distribuído.

Gate:

- nenhuma capability pública inerte;
- nenhum silent partial answer;
- upgrade/refusal behavior comprovado;
- metrics não alteram ordem de complexidade;
- release notes distinguem preview/experimental/stable.

### Trilha opcional de produto — Agent/MCP

#### GX-AGENT-0 — Agent/MCP preview

**Depende:** GX-CAP-1 + GX-CAP-2. Pode usar graph/vector atuais.

Entregas:

- packages `agent`, `workspace` e `mcp` separados;
- system schema agentic versionado;
- identity/session/idempotency;
- memory/claim/evidence primitives;
- MCP stdio tools/resources mínimos;
- project/user policy;
- two-harness demo.

Gate:

- autoria não forjável;
- shared project state;
- no database path em tools;
- retries idempotentes;
- no core dependency reversal.

#### GX-AGENT-1 — Agent knowledge layer integrada

**Depende:** temporal + full-text/hybrid; production lifecycle do plano principal.

Entregas:

- historical recall/diff;
- hybrid ContextPack;
- contradiction/supersession/retraction;
- scope promotion receipts;
- temporal validity de claims;
- MCP capability negotiation;
- Nexus correlation metadata opcional.

Gate:

- retrieval source-explainable;
- claims contraditórios preservados;
- history independente do MVCC;
- project/global isolation;
- backup/export recovery test end-to-end.

---

## 18. Ordem de implementação recomendada

### 18.1 Ciclo A — Fundação database-first

```text
GX-CAP-0
   ↓
GX-CAP-1 + GX-CAP-2
```

Esse ciclo estabelece capacidades genéricas do banco:

- identidade lógica e metadados limitados de commit;
- catálogos/stores nomeados;
- semântica de uma transação por store;
- manifests e boundaries que suportarão temporalidade, busca e integrações futuras.

Nenhum artefato agentic é necessário para concluir ou publicar esse ciclo.

### 18.2 Ciclo B — Diferenciação do banco

```text
GX-CAP-3 → GX-CAP-4
GX-CAP-5 + M2 HNSW → GX-CAP-6
```

Este é o principal eixo funcional do roadmap complementar. O Grafx passa a oferecer:

> shared local graph state, historical truth and explainable hybrid retrieval.

A prioridade de produto é provar essas capacidades como funcionalidades genéricas do banco, com APIs Python e contratos de recovery, antes de utilizá-las em qualquer camada especializada.

### 18.3 Ciclo C — Ecossistema e analytics

```text
GX-CAP-7 → GX-CAP-8 / GX-CAP-9
GX-CAP-10
GX-CAP-11
```

Extensões, dados externos, projeções, algoritmos, schema lifecycle e views entram depois que as semânticas centrais estiverem confiáveis. O hardening fecha o ciclo database-first independentemente da existência da integração MCP.

### 18.4 Trilha opcional — Agent API e MCP

```text
GX-CAP-1 + GX-CAP-2 ──► GX-AGENT-0
GX-CAP-3 + GX-CAP-4 + GX-CAP-6 + lifecycle ──► GX-AGENT-1
```

`GX-AGENT-0` pode evoluir em paralelo depois das fundações, desde que:

- não altere o formato físico nem o transaction manager para atender conceitos agentic;
- não bloqueie milestones temporais, de busca ou de ecossistema;
- dependa apenas de APIs públicas do Grafx;
- permaneça distribuível como package separado.

`GX-AGENT-1` consome temporalidade e hybrid retrieval já estabilizados. Ele não é condição para declarar o core pronto.

### 18.5 Paralelismo autorizado

Pode ocorrer em paralelo quando não houver overlap de formato/engine:

- MCP adapter versus core temporal;
- interop package versus FTS engine;
- algorithm implementations versus catalog/session work;
- documentação/conformance versus performance work.

Deve ser serializado:

- duas mudanças concorrentes no formato físico;
- CommitId e temporal publication;
- FTS index redo e generic index lifecycle;
- schema migration e catalog format;
- qualquer mudança compartilhada em commit/recovery protocol.

---

## 19. Estratégia de testes transversal

### 19.1 Recovery e fault injection

- crash em cada etapa de commit metadata/history/index publication;
- partial writes em temporal/FTS/catalog manifests;
- recovery repetido;
- read-only open depois de crash;
- kill entre source snapshot e target copy commit;
- extension failure antes/depois de public API calls;
- MCP killed durante mutação/retry.

### 19.2 Concorrência

- N writers temporal em identities distintas e iguais;
- history readers long-lived contra retention;
- FTS rebuild contra writes/searches;
- attach/detach contra active readers;
- two MCP processes usando o mesmo store;
- algorithm projection pin contra commits/vacuum;
- migration lease/takeover.

### 19.3 Semântica

- current vs system-time vs valid-time;
- historical relationship endpoint visibility;
- BM25 fixtures;
- hybrid ranking breakdown;
- cross-catalog isolation;
- UDF determinism/side effects;
- algorithms contra oracle;
- claim contradiction/supersession.

### 19.4 Recursos

- history growth;
- FTS postings/index size;
- hybrid candidate explosion;
- external scan batch memory;
- projection size estimates;
- extension and MCP input budgets;
- temporal diff size/deadline.

### 19.5 Compatibilidade

- feature manifests required/skippable;
- databases sem novas capabilities continuam abrindo;
- builds antigos recusam feature obrigatória;
- export/import com temporal/FTS metadata;
- extension API n−1/n;
- MCP protocol version negotiation;
- schema migration fixtures.

---

## 20. Observabilidade recomendada

### Temporal

- history bytes/versions por table;
- current/history write amplification;
- oldest/newest retained commit;
- pinned historical readers;
- temporal index lag;
- diff rows/bytes/time.

### Full-text/hybrid

- postings/doc count/index bytes;
- analyzer/rebuild generation;
- candidates visited por source;
- text/vector/graph latency;
- fallback/partial/exact/approximate totals;
- fusion result overlap.

### Catalogs

- attached catalogs por session;
- catalog open/close failures;
- federated query partial failures;
- copy rows/bytes/conflicts/retries;
- path-policy rejections.

### Extensions/algorithms

- loaded extensions;
- extension failures/timeouts;
- projections/bytes/pins;
- algorithm iterations/work/cancellation;
- external scan rows/bytes.

### Agent/MCP

- tool calls por nome/status;
- idempotent replay hits;
- write conflicts/retries;
- project/user result counts;
- claim/memory mutations;
- nunca incluir prompt, content ou secret em metric labels.

---

## 21. Segurança e threat boundaries

- Grafx core continua embedded e sem autenticação própria;
- filesystem permissions são a primeira boundary;
- attached paths são allowlisted/canonicalizados;
- extensions são trusted code e requerem consentimento explícito;
- remote connectors ficam fora da primeira fase;
- MCP stdio herda a identidade/configuração do processo, não recebe credenciais por tool input;
- user/global store é read-only por default;
- raw Cypher writes de agente são opt-in administrativo;
- commit metadata é bounded e não deve guardar prompts/secrets completos;
- FTS highlights e error messages são bounded/redacted;
- temporal history pode preservar dados apagados: retention/privacy policy deve ser explícita;
- “delete current” não deve ser documentado como “erase history”; hard erasure requer operação administrativa própria e auditada.

---

## 22. Itens deliberadamente fora deste roadmap

- cluster, consensus, sharding e distributed transactions;
- daemon obrigatório;
- remote HTTP database server como requisito;
- RBAC/authentication no embedded core;
- billing/multi-tenancy SaaS;
- execução de LLM ou generation de embedding no engine;
- agent scheduling/routing/handoff;
- cross-store physical edges;
- transparent cross-store atomic commit;
- extension marketplace com download automático;
- general-purpose document database;
- full lakehouse engine;
- promessa de paridade total com Cypher/Neo4j;
- reimplementação dos itens já pertencentes ao `EVOLUTION_PLAN_CODEX.md`.

---

## 23. Instruções de execução para o Codex

Para cada milestone:

1. criar `docs/specs/SPEC-<MILESTONE>.md` antes de alterar código;
2. declarar invariantes, APIs, error taxonomy, persisted effects e recovery behavior;
3. indicar explicitamente arquivos/módulos tocados;
4. adicionar tests que falham antes da implementação;
5. implementar a menor vertical slice verificável;
6. executar targeted tests, full suite, type check e lint;
7. executar fault/mutation battery quando storage/commit/index/recovery forem tocados;
8. publicar benchmark quando houver claim de performance;
9. atualizar este documento com SHA imutável e evidência;
10. não declarar milestone concluído com provider/feature inativo ou sem conformance pública;
11. não incorporar refactors adjacentes sem evidência de necessidade;
12. manter worktrees/branches existentes intactos e integrar de forma serial quando houver overlap no engine.

### 23.1 ADRs sugeridos

```text
docs/adr/ADR-010-database-first-boundary.md
docs/adr/ADR-011-commit-identity.md
docs/adr/ADR-012-temporal-semantics.md
docs/adr/ADR-013-attached-catalogs.md
docs/adr/ADR-014-fulltext-hybrid.md
docs/adr/ADR-015-extension-spi.md
docs/adr/ADR-016-agent-mcp-layer.md
```

### 23.2 Novos erros públicos prováveis

```text
GrafxTemporalHistoryUnavailable
GrafxTemporalRetentionViolation
GrafxTemporalConsistencyError
GrafxCatalogNotFound
GrafxCatalogReadOnly
GrafxCrossCatalogWriteRefused
GrafxFullTextIndexError
GrafxSearchBudgetExceeded
GrafxExtensionError
GrafxExtensionCompatibilityError
GrafxMigrationError
GrafxMigrationChecksumMismatch
GrafxWorkspaceResolutionError        # package workspace
GrafxAgentPolicyError                # package agent
```

Os nomes finais devem seguir a taxonomia atual e evitar classes duplicadas que respondam à mesma condição.

---

## 24. Critério de sucesso do roadmap

O plano é bem-sucedido quando o Grafx puder demonstrar, sem alterar sua identidade de banco:

1. vários processos escrevendo o mesmo store local com as garantias já planejadas;
2. consulta do estado atual e de estados históricos coerentes do grafo;
3. distinção entre transaction/system time e domain/valid time;
4. recuperação lexical, vetorial e estrutural em uma única API explicável;
5. project e user/global stores isolados, anexáveis e copiáveis de forma explícita;
6. extensões e algoritmos sem dependência em internals;
7. integração eficiente com o ecossistema Python/data;
8. evolução segura do schema da aplicação;
9. uma camada agentic/MCP instalável que usa essas capacidades sem contaminar o core;
10. documentação honesta sobre limites, fallback, approximation, retention e consistency.

A mensagem final de produto não deve ser “um banco para agentes”. Deve ser:

> **Okto Grafx is a local-first embedded graph and vector database for shared, durable and verifiable application state — with temporal graphs, hybrid retrieval and optional agent-native integrations.**

---

## 25. Referências de mercado usadas para selecionar as capacidades

Fontes oficiais consultadas em 2026-08-26:

- CozoDB — time travel e database relacional-grafo-vetor: <https://docs.cozodb.org/>
- SurrealDB — `VERSION`/time-travel query: <https://surrealdb.com/docs/reference/query-language/statements/select>
- Microsoft SQL Server — system-versioned temporal tables: <https://learn.microsoft.com/en-us/sql/relational-databases/tables/temporal/overview>
- MariaDB — bitemporal tables: <https://mariadb.com/docs/server/reference/sql-structure/temporal-tables/bitemporal-tables>
- LadybugDB — full-text search: <https://docs.ladybugdb.com/extensions/full-text-search/>
- SurrealDB — hybrid lexical/vector search: <https://surrealdb.com/docs/learn/data-models/vector-search/hybrid-search>
- LadybugDB — graph algorithms: <https://docs.ladybugdb.com/extensions/algo/>
- Neo4j Graph Data Science — graph algorithms/projections: <https://neo4j.com/docs/graph-data-science/current/algorithms/>
- LadybugDB — external scans e Arrow/DataFrames: <https://docs.ladybugdb.com/cypher/query-clauses/load-from/>
- LadybugDB — Python UDFs: <https://docs.ladybugdb.com/client-apis/python/>
- LadybugDB — extensions: <https://docs.ladybugdb.com/extensions/>
- SQLite — attached databases e limites de atomicidade entre arquivos: <https://sqlite.org/lang_attach.html>
- LadybugDB — attached databases: <https://docs.ladybugdb.com/cypher/attach/>
- Model Context Protocol — arquitetura e stdio: <https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture>
- Model Context Protocol — tools/resources: <https://modelcontextprotocol.io/specification/2026-07-28/server/tools> e <https://modelcontextprotocol.io/specification/2026-07-28/server/resources>
