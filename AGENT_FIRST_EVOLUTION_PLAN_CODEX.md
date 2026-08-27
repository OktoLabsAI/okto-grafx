# Plano complementar de evolução do Okto Grafx — Agent-First Local Knowledge Layer

**Status:** proposta para refinamento e execução incremental
**Documento-base:** `EVOLUTION_PLAN_CODEX.md`
**Natureza:** roadmap complementar; não substitui nem duplica o plano técnico já existente
**Objetivo de consumo:** orientar implementação incremental pelo Codex, com contratos, dependências, gates e critérios de aceite explícitos

---

## 1. Resumo executivo

Este plano propõe uma evolução opcional do Okto Grafx para torná-lo uma infraestrutura de estado e conhecimento compartilhado na qual agentes sejam **first-class citizens**, sem acoplar conceitos de agentes, MCP ou harnesses ao core do banco.

A tese de produto é:

> O Okto Grafx continua sendo um graph + vector database embedded e genérico. Sobre ele, uma camada agent-first fornece identidade, sessões, proveniência, memória, claims, evidências, escopos de conhecimento e uma interface MCP local por `stdio`, permitindo que vários agentes independentes compartilhem o mesmo estado local sem daemon, servidor ou broker central.

A experiência-alvo é:

```text
Harness / agente A ── inicia ──> MCP stdio A ──┐
                                               │
Harness / agente B ── inicia ──> MCP stdio B ──┼──> Agent API ──> Grafx project store
                                               │                └─> Grafx user/global store
Harness / agente C ── inicia ──> MCP stdio C ──┘
```

Cada harness mantém seu próprio processo MCP. Os processos não compartilham memória, mas resolvem deterministicamente os mesmos diretórios e abrem os mesmos stores Grafx. A coordenação multiprocesso, o WAL, o MVCC, o recovery e a durabilidade continuam sendo responsabilidade do core existente.

Os novos diferenciais propostos são:

1. agentes identificados e auditáveis, sem identidade fornecida livremente pelo próprio tool call;
2. proveniência automática e atômica para toda mutação semântica;
3. memória, claims e evidências como primitivas interoperáveis entre agentes;
4. conhecimento com escopo de projeto e escopo global do usuário;
5. MCP `stdio` distribuído junto com o produto, mas arquiteturalmente separado do core;
6. full-text e hybrid retrieval combinando texto, vetor e expansão em grafo;
7. contexto montado sob orçamento de tokens, com justificativa de seleção e fontes;
8. histórico lógico, contradições, supersession e validade temporal explícitos;
9. autonomia de schema limitada por namespaces, ownership e políticas fail-closed.

---

## 2. Relação com o roadmap já existente

Este documento parte do princípio de que `EVOLUTION_PLAN_CODEX.md` permanece como fonte de verdade para integridade, performance, operação e compatibilidade com o Pulse.

### 2.1 Itens deliberadamente fora deste plano

Não devem ser reimplementados ou replanejados aqui:

- correções M0 de recovery, durabilidade, read-only, bootstrap e HNSW concorrente;
- configuração honesta, budgets, tipagem pública, health e maintenance já previstas em M1;
- identity-range leasing;
- redução de locks globais, custo de publicação e amplificação de I/O;
- transformação do HNSW em access path real;
- bulk ingest e group commit;
- vacuum físico e relatório de bloat;
- backup e restore;
- export/import e migração do formato;
- secondary indexes genéricos, planner, `EXPLAIN` e `PROFILE` já previstos;
- capacidades Cypher e de grafo já enumeradas no plano atual;
- changefeed, sincronização, replicação e conflitos distribuídos;
- compatibilidade e migração completa do Okto Pulse;
- criptografia em repouso.

### 2.2 Dependências com o plano existente

| Capacidade deste plano | Dependência do plano atual |
|---|---|
| Contratos, packages e Agent API | pode iniciar imediatamente |
| Resolver de workspace e stores | preferencialmente após a facade pública tipada de M1 |
| Identidade, proveniência e claims | requer transações multi-statement e read-your-own-writes já disponíveis/em consolidação |
| MCP funcional em preview | não depende de M2 para corretude |
| MCP sob carga concorrente relevante | depende de identity-range leasing e redução do custo de commit de M2 |
| Hybrid retrieval escalável | depende do HNSW como access path real e da nova capacidade textual descrita neste plano |
| Release agent-first considerada operacionalmente segura | depende também de backup/restore, export/import, health e migração do roadmap atual |

O desenvolvimento agent-first pode, portanto, começar antes da conclusão de toda a Fase 2, mas a promessa comercial de alto throughput multiprocesso não deve ser feita antes dos respectivos gates estruturais.

---

## 3. Decisões arquiteturais vinculantes

### D1 — O core permanece genérico

`okto-grafx` não deve conhecer:

- MCP;
- agentes;
- harnesses;
- modelos de linguagem;
- memória de agente;
- claims;
- escopos de workspace;
- embeddings providers;
- rerankers;
- regras de autorização de agentes.

A dependência deve apontar sempre para dentro:

```text
okto-grafx-mcp
      ↓
okto-grafx-agent
      ↓
okto-grafx
```

Nunca no sentido inverso.

### D2 — MCP é um adapter de produto, não um mecanismo do banco

O produto pode ser instalado e distribuído com suporte MCP, mas o listener/protocolo não pertence ao engine.

Distribuição recomendada:

```text
okto-grafx          core embedded, sem dependências obrigatórias adicionais
okto-grafx-agent    Agent API, scopes, políticas, conhecimento e proveniência
okto-grafx-mcp      adapter MCP e entrypoint stdio
```

Podem residir no mesmo monorepo, desde que sejam wheels e import graphs independentes.

### D3 — `project` e `user/global` são stores físicos distintos

Os escopos não devem ser implementados apenas como uma coluna `scope` dentro de uma única base crescente.

```text
PROJECT STORE
<workspace>/.grafx/store/

USER/GLOBAL STORE
<user-data-dir>/okto-grafx/stores/user/
```

Motivos:

- isolamento entre projetos;
- menor risco de vazamento acidental;
- lifecycle independente por projeto;
- políticas e permissões diferentes;
- possibilidade de apagar ou mover um projeto sem tocar no conhecimento global;
- ausência de uma base global única contendo dados privados de todos os projetos;
- alinhamento com permissões do filesystem e com a natureza embedded do Grafx.

O nome interno recomendado é `user`, por ser global apenas para o usuário local. A CLI e a documentação podem aceitar `global` como alias de experiência.

### D4 — A localização dos stores é definida na inicialização

Nenhuma tool MCP deve aceitar um path arbitrário informado pelo agente.

O processo MCP inicia já vinculado a:

- zero ou um project store;
- zero ou um user/global store;
- uma política de leitura;
- um write scope padrão;
- uma identidade de agente.

Isso impede que uma instrução armazenada, um prompt malicioso ou uma decisão do modelo faça o servidor abrir outro diretório.

### D5 — Uma transação escreve em exatamente um store

Não haverá transação atômica transparente entre `project` e `user`.

- writes em `merged` são inválidos;
- promoção entre scopes é uma operação lógica, idempotente e recuperável;
- reads em `merged` usam snapshots independentes e devem retornar os tokens/snapshots de cada store;
- relações físicas não atravessam stores.

### D6 — Identidade e proveniência são injetadas pelo servidor

O agente não pode declarar livremente `asserted_by="outro-agente"`.

A identidade é configurada na inicialização do MCP ou fornecida por um `AgentIdentityProvider`. Toda mutação recebe automaticamente:

- agent profile;
- agent session;
- operation id;
- scope;
- timestamp;
- idempotency key;
- tool/operação de origem;
- correlation id opcional;
- hashes de entrada/resultado quando aplicável.

### D7 — Claims não são automaticamente tratados como fatos

O Grafx deve preservar a diferença entre:

- observação;
- hipótese;
- afirmação;
- decisão;
- recomendação;
- fato verificado;
- informação contestada.

Agentes diferentes podem registrar claims contraditórios. O sistema deve representar `contradicts`, `corroborates`, `supersedes`, `retracts` e evidências, em vez de escolher silenciosamente um vencedor.

### D8 — Histórico de conhecimento é lógico, não dependente do MVCC

MVCC é mecanismo de isolamento e poderá ter versões antigas removidas pelo vacuum físico já planejado.

Qualquer promessa de `as_of`, histórico, supersession ou validade temporal deve ser representada por registros lógicos explícitos. Não deve depender de retenção indefinida das versões internas do storage.

### D9 — O Grafx não vira um barramento de coordenação

A camada agent-first não deve implementar:

- inbox;
- handoff;
- presença online;
- roteamento por capacidade;
- broadcast;
- delegação;
- heartbeat;
- scheduling de agentes.

Essas responsabilidades pertencem ao Okto Nexus ou a outro coordenador. O Grafx guarda estado, conhecimento, memória e proveniência. Um `nexus_agent_id` e um `correlation_id` podem ser registrados como metadados opcionais, sem dependência entre os produtos.

### D10 — Raw write é exceção administrativa

A primeira versão MCP não deve oferecer Cypher de escrita irrestrito como tool padrão do modelo.

Motivos:

- bypass de proveniência;
- bypass de políticas de namespace;
- risco de DDL arbitrário;
- dificuldade de idempotência;
- risco de mutações destrutivas difíceis de classificar.

Reads Cypher podem ser expostos em modo explicitamente read-only e limitados a um único scope. Writes comuns devem passar pelas operações semânticas ou por um batch estruturado e governado.

---

## 4. Arquitetura-alvo

```text
┌─────────────────────────────────────────────────────────────────────┐
│ Harness A / Harness B / IDE / worker                                │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ MCP stdio, um subprocesso por client
┌───────────────────────────────▼─────────────────────────────────────┐
│ okto-grafx-mcp                                                     │
│ - framing e lifecycle MCP                                          │
│ - tools e resources                                                │
│ - validação de input/output                                        │
│ - mapeamento de erros                                               │
│ - identidade configurada do agent                                  │
│ - nenhum conhecimento do storage interno                           │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ DTOs e serviços
┌───────────────────────────────▼─────────────────────────────────────┐
│ okto-grafx-agent                                                   │
│ - AgentWorkspace                                                   │
│ - ScopedStoreRouter                                                │
│ - AgentRegistryService                                             │
│ - ProvenanceService                                                │
│ - KnowledgeService                                                 │
│ - ClaimService                                                     │
│ - SchemaRegistryService                                            │
│ - ContextService                                                   │
│ - PolicyService                                                    │
│ - ports de embedding, reranking, tokenização e redaction           │
└──────────────────────┬───────────────────────────────┬──────────────┘
                       │                               │
          ┌────────────▼────────────┐     ┌────────────▼────────────┐
          │ ProjectStoreAdapter     │     │ UserStoreAdapter        │
          │ <workspace>/.grafx/...  │     │ user-data-dir/...       │
          └────────────┬────────────┘     └────────────┬────────────┘
                       │                               │
          ┌────────────▼───────────────────────────────▼──────────────┐
          │ okto-grafx core                                           │
          │ graph + vector + WAL + MVCC + recovery + verification     │
          └────────────────────────────────────────────────────────────┘
```

### 4.1 Ports da camada agent-first

Os ports abaixo pertencem a `okto-grafx-agent`, não ao core:

- `WorkspaceResolver` — encontra e valida o workspace;
- `UserDataPathProvider` — resolve o diretório global do usuário por plataforma;
- `AgentIdentityProvider` — fornece identidade confiável configurada;
- `PolicyProvider` — carrega permissões e limites;
- `IdGenerator` — gera IDs estáveis e testáveis;
- `EmbeddingProvider` — produz embeddings, quando instalado;
- `TokenEstimator` — estima o tamanho de context packs;
- `Reranker` — reranking opcional, nunca requisito de corretude;
- `ContentRedactor` — detecta ou remove conteúdo sensível antes da persistência;
- `AgentMetricsSink` — métricas específicas da camada agent-first.

A implementação default deve funcionar sem LLM, embedding provider ou reranker. Nessa condição, graph e lexical retrieval continuam disponíveis.

---

## 5. Conceitos públicos

| Conceito | Definição |
|---|---|
| `Workspace` | Um projeto identificado por manifesto e associado a um project store |
| `Store` | Um diretório físico Grafx aberto pela Agent API |
| `Scope` | `project`, `user/global` ou `merged` para leitura federada |
| `Namespace` | Domínio lógico que possui schemas e tabelas |
| `AgentProfile` | Identidade lógica relativamente estável de um agente/harness |
| `AgentSession` | Uma execução do processo MCP ou uso da Agent API |
| `AgentOperation` | Uma operação atômica e auditável executada pelo agente |
| `MutationRecord` | Registro lógico do alvo e tipo de cada mutação |
| `MemoryItem` | Conteúdo recuperável, com tipo, proveniência e lifecycle |
| `Claim` | Afirmação semanticamente classificada, potencialmente contestável |
| `Evidence` | Fonte que sustenta ou contextualiza um claim |
| `ContextPack` | Conjunto de contexto selecionado sob limite de itens/tokens |
| `ScopedUri` | Referência estável que identifica scope, namespace e objeto |

---

## 6. Workspace e escopos

### 6.1 Estrutura recomendada

```text
<workspace>/
  .grafx/
    workspace.toml
    policy.toml              # opcional; pode ser parte do workspace.toml
    .gitignore
    store/                   # nunca versionado
```

Exemplo conceitual de manifesto:

```toml
version = 1
workspace_id = "<uuid>"
project_store = ".grafx/store"
default_read_scopes = ["project", "user"]
default_write_scope = "project"

[user_store]
enabled = true
mode = "read"

[policy]
allow_raw_read_query = true
allow_raw_write_query = false
allow_project_schema_create = false
allow_user_schema_create = false
allow_user_write = false
```

A identidade do agente não deve ser gravada no manifesto compartilhado do projeto. Ela pertence à configuração do servidor MCP/harness.

### 6.2 Resolução determinística

Ordem recomendada:

1. `--workspace <path>`;
2. `OKTO_GRAFX_WORKSPACE`;
3. busca ascendente por `.grafx/workspace.toml` a partir do working directory;
4. nenhum fallback silencioso para um diretório arbitrário.

A criação ocorre por comando explícito:

```text
oktografx-agent init [path]
```

Um modo `--create-workspace` pode existir, mas não deve ser o default do MCP.

O resolver deve:

- canonicalizar paths;
- detectar symlink escape quando a política restringir o workspace;
- validar que o project store pertence ao `workspace_id` esperado;
- validar que o user store foi criado como `kind=user`;
- rejeitar store trocado, incompatível ou apontado para filesystem não suportado;
- nunca aceitar path vindo de uma tool call.

### 6.3 Metadados do store

Cada store gerenciado pela Agent API deve possuir uma identidade interna instalada em schema reservado:

- `store_id`;
- `store_kind`: `project` ou `user`;
- `workspace_id`, apenas para project store;
- `agent_schema_version`;
- `created_at`;
- `created_by_version`.

O open falha se o manifesto físico contradizer o mount solicitado.

### 6.4 Semântica de leitura e escrita

| Operação | `project` | `user/global` | `merged` |
|---|---:|---:|---:|
| Leitura semântica | sim | sim, se montado | sim, federada |
| Cypher read-only | sim | sim | não na primeira versão |
| Escrita | default | somente com capability explícita | inválida |
| Criação de schema | conforme ownership/policy | normalmente administrativa | inválida |
| Transação | um store | um store | não existe |
| Relação física | dentro do store | dentro do store | não atravessa stores |

Toda resposta federada deve informar:

- scope de cada item;
- store id;
- snapshot token de cada store consultado;
- se houve deduplicação;
- se o resultado foi truncado;
- estratégia usada para combinar scores.

### 6.5 Precedência e deduplicação

O project store não deve sobrescrever silenciosamente um item global apenas porque o conteúdo é parecido.

Regras iniciais:

1. itens de scopes diferentes permanecem distintos;
2. conteúdo com fingerprint idêntico pode ser deduplicado na resposta, preservando todas as proveniências;
3. project scope pode receber um boost de ranking configurável, nunca uma regra invisível de verdade;
4. claims conflitantes são retornados como conflito, não removidos do contexto;
5. scores vetoriais de embedding spaces diferentes não são comparados diretamente.

### 6.6 Referências entre scopes

Como não existem edges físicas entre bases distintas, referências cross-scope devem usar URI lógica:

```text
grafx://project/<workspace-id>/<namespace>/<kind>/<object-id>
grafx://user/<store-id>/<namespace>/<kind>/<object-id>
```

O objeto consumidor armazena a URI, não um `RecordId` do outro store.

### 6.7 Promoção e materialização

Operações previstas:

- `promote`: copia conhecimento de `project` para `user`;
- `materialize`: copia conhecimento de `user` para `project` para permitir adaptação local;
- `copy`: forma genérica usada internamente.

Regras:

- a origem nunca é removida;
- a cópia recebe nova identidade no destino;
- a cópia registra `derived_from`/`promoted_from` com URI e snapshot da origem;
- a operação é idempotente no destino;
- falha parcial é retomável;
- global write é negado por default;
- não há sincronização automática entre as cópias neste plano.

---

## 7. Agentes como first-class citizens

### 7.1 AgentProfile

Campos mínimos:

- `agent_id` estável;
- `display_name`;
- `harness`;
- `provider` opcional;
- `model` opcional;
- `client_version` opcional;
- `identity_assurance`: `configured`, `derived` ou `ephemeral`;
- `capabilities` declarativas;
- `nexus_agent_id` opcional;
- `metadata_json` limitado;
- `created_at`.

A ausência de identidade configurada não deve ser mascarada. Pode ser criada uma identidade efêmera, claramente marcada como tal.

### 7.2 AgentSession

Cada processo MCP cria uma sessão de aplicação própria, independentemente de qualquer conceito de sessão do protocolo:

- `session_id`;
- `agent_id`;
- `workspace_id`;
- `client_name` e `client_version`;
- `model` efetivamente usado, quando conhecido;
- `started_at`;
- `ended_at`, best-effort;
- `correlation_id` opcional;
- `identity_assurance` herdida.

Uma morte abrupta pode deixar a sessão sem `ended_at`; isso deve ser interpretado como sessão interrompida, não como corrupção.

### 7.3 AgentOperation e proveniência

Toda mutação semântica deve criar uma operação no mesmo commit do dado alterado.

Campos mínimos:

- `operation_id`;
- `session_id`;
- `agent_id`;
- `tool_name` ou `operation_kind`;
- `scope`;
- `namespace`;
- `idempotency_key`;
- `intent` curto e opcional;
- `input_digest`;
- `output_digest`;
- `status`;
- `started_at`;
- `committed_at`;
- `retry_count`;
- `correlation_id` opcional.

Cada alvo recebe um `MutationRecord` com:

- target URI;
- mutation kind: create, update, link, unlink, tombstone, supersede;
- schema version;
- before digest, quando disponível;
- after digest, quando disponível.

Invariante:

> Não pode existir mutação sem proveniência nem proveniência afirmando uma mutação que não foi commitada.

Reads podem ser auditados por política, mas a gravação de prompts/transcrições completas fica desativada por default.

### 7.4 Idempotência

Tools mutáveis devem aceitar `idempotency_key`.

Chave lógica recomendada:

```text
(store_id, agent_id, operation_kind, idempotency_key)
```

Ao repetir uma operação concluída:

- retornar o mesmo resultado lógico;
- não duplicar claims, memories ou relações;
- informar `replayed=true`.

Ao repetir a mesma chave com payload incompatível:

- recusar com `AgentIdempotencyConflict`;
- nunca reinterpretar a chave.

O service layer deve tratar `GrafxWriteConflict` com retry limitado e jitter, mantendo a mesma idempotency key.

---

## 8. Primitivas de conhecimento

### 8.1 System schema reservado

Usar prefixo reservado, por exemplo `_ogx_`. Nenhum namespace de domínio pode criar ou alterar tabelas com esse prefixo.

Modelo físico inicial sugerido, sujeito a uma spec de schema separada:

#### Node tables

- `_ogx_store_meta`;
- `_ogx_namespace`;
- `_ogx_agent`;
- `_ogx_session`;
- `_ogx_operation`;
- `_ogx_mutation`;
- `_ogx_memory`;
- `_ogx_claim`;
- `_ogx_evidence`.

#### Relationship tables

- `_ogx_session_agent`;
- `_ogx_operation_session`;
- `_ogx_mutation_operation`;
- `_ogx_memory_operation`;
- `_ogx_claim_operation`;
- `_ogx_claim_evidence`;
- `_ogx_claim_supersedes`;
- `_ogx_claim_contradicts`;
- `_ogx_claim_corroborates`;
- `_ogx_memory_derived_from`.

As tabelas precisam usar somente tipos e invariantes suportados pelo Grafx atual. IDs públicos devem ser estáveis e independentes de page/slot. A spec física deve validar explicitamente o uso de UUID/STRING como primary key; até essa prova, pode-se usar um identificador INT64 gerado pela Agent API e protegido pela PK.

### 8.2 MemoryItem

Campos conceituais:

- `memory_id`;
- `namespace`;
- `kind`: note, observation, decision, summary, artifact, code_anchor, instruction, preference;
- `title` opcional;
- `content`;
- `content_hash`;
- `metadata_json`;
- `importance` opcional;
- `pinned`;
- `expires_at` opcional;
- `status`: active, superseded, retracted, expired;
- `created_at`;
- `embedding_space` e embedding opcionais;
- provenance operation.

Atualizações relevantes criam nova revisão ou supersession; não reescrevem silenciosamente a história.

### 8.3 Claim

Campos conceituais:

- `claim_id`;
- `namespace`;
- `kind`: observation, hypothesis, assertion, decision, recommendation, verified_fact;
- `statement` textual;
- `subject_ref` opcional;
- `predicate` opcional;
- `object_ref` ou `object_value` opcional;
- `confidence` opcional e claramente não equivalente a verdade;
- `status`: active, disputed, superseded, retracted;
- `asserted_at`;
- `valid_from` e `valid_to` opcionais;
- `content_hash`;
- embedding opcional;
- provenance operation.

Transições:

- `assert` cria claim;
- `corroborate` cria relação de apoio;
- `dispute` mantém o claim e registra contestação;
- `supersede` cria novo claim e relação de supersession;
- `retract` encerra a validade lógica sem apagar evidência.

### 8.4 Evidence

Campos conceituais:

- `evidence_id`;
- `source_kind`: file, url, user, tool, command, database, agent, artifact;
- `locator`;
- `source_version`: hash, commit, ETag ou equivalente;
- `observed_at`;
- `excerpt` opcional e limitado;
- `content_digest`;
- `metadata_json`;
- provenance operation.

Fontes de código devem poder carregar, quando aplicável:

- repository/workspace id;
- file path relativo;
- symbol/method;
- commit hash;
- content hash;
- linhas apenas como hint, nunca como identidade única.

### 8.5 Relações semânticas

Relações mínimas:

- `derived_from`;
- `supports`;
- `contradicts`;
- `corroborates`;
- `supersedes`;
- `about`;
- `related_to`;
- `summarizes`;
- `promoted_from`;
- `produced_by`.

Relações genéricas entre schemas arbitrários podem começar como referências URI. Relações físicas tipadas devem ser criadas somente quando o par de tipos for conhecido e governado.

---

## 9. Autonomia de schema e namespaces

A resposta à pergunta “o agente escolhe seu schema?” é: **sim, dentro de um contrato de ownership e compatibilidade**.

### 9.1 Tipos de namespace

- `system` — reservado à Agent API;
- `shared` — definido pelo workspace ou usuário e interoperável entre agentes;
- `agent-private` — pertencente a um agent id específico;
- `application` — pertencente a uma aplicação consumidora, como Pulse.

Nomes físicos podem ser codificados como `<namespace>__<table>` para respeitar as regras atuais de identificador do Grafx.

### 9.2 NamespaceDefinition

Campos:

- namespace id;
- nome lógico;
- scope;
- owner kind e owner id;
- descrição;
- schema version;
- compatibility policy;
- created_at;
- status.

### 9.3 Regras de criação

1. o agente consulta o schema existente antes de propor um novo;
2. `schema.ensure` é idempotente;
3. na primeira versão, tabela existente deve corresponder exatamente ao schema esperado;
4. mismatch falha com diff estruturado;
5. alteração incompatível ou destrutiva é recusada;
6. project schema creation depende de capability;
7. user/global schema creation é administrativa por default;
8. system namespace nunca é alterável por agents;
9. migrations físicas continuam subordinadas ao roadmap principal.

### 9.4 Domain mutation API

Após o MVP de memória e claims, expor um batch estruturado:

```text
domain.apply(
  scope,
  namespace,
  operations=[
    upsert_node,
    update_node,
    tombstone_node,
    create_relationship,
    update_relationship,
    delete_relationship
  ],
  idempotency_key
)
```

Cada batch:

- executa em uma única transaction Grafx;
- valida namespace e schema;
- registra AgentOperation e MutationRecords;
- não aceita path ou identidade;
- não atravessa scopes;
- recusa propriedades desconhecidas ou tipos incompatíveis;
- retorna referências lógicas, não objetos mutáveis do engine.

Raw Cypher de escrita pode existir apenas em modo administrativo e precisa passar por wrapper instrumentado. Não deve ser uma tool default do modelo.

---

## 10. Agent API pública

A Agent API é a fonte de verdade semântica. MCP apenas a adapta.

Exemplo conceitual:

```python
workspace = AgentWorkspace.open(
    workspace="/repo",
    identity=AgentIdentity(
        agent_id="codex-main",
        display_name="Codex",
        harness="codex",
    ),
)

workspace.remember(
    scope="project",
    namespace="architecture",
    kind="decision",
    content="Technical anchors use symbol identity, not line ranges.",
    idempotency_key="decision-technical-anchor-v1",
)

workspace.assert_claim(
    scope="project",
    namespace="architecture",
    statement="The parser is the only supported entry point for query text.",
    evidence=[...],
    confidence=0.92,
    idempotency_key="parser-entry-claim-v1",
)

context = workspace.recall(
    query="How are technical anchors resolved?",
    scopes=("project", "user"),
    token_budget=4000,
)
```

### 10.1 Serviços internos

- `AgentWorkspace` — facade principal;
- `ScopedStoreRouter` — mounts e roteamento;
- `AgentRegistryService` — profiles e sessions;
- `ProvenanceService` — operations, mutations e idempotência;
- `MemoryService`;
- `ClaimService`;
- `EvidenceService`;
- `SchemaRegistryService`;
- `DomainMutationService`;
- `ContextService`;
- `ScopeTransferService`;
- `PolicyService`.

Nenhum DTO público deve expor `TransactionContext`, buffer pool, `RecordRef`, adapter ou manager interno do core.

---

## 11. MCP local por stdio

### 11.1 Empacotamento

Entrypoint recomendado:

```text
oktografx-mcp
```

Instalação recomendada:

```text
pip install okto-grafx-mcp
```

Esse pacote instala `okto-grafx-agent` e `okto-grafx`, preservando o core sem dependência MCP.

### 11.2 Inicialização

Fluxo:

1. validar CLI/env/config;
2. resolver e canonicalizar workspace;
3. resolver user store;
4. carregar policy;
5. criar identidade do agente;
6. abrir stores;
7. validar store metadata e system schema;
8. registrar AgentSession;
9. publicar tools/resources;
10. processar MCP por stdin/stdout;
11. no EOF, fechar sessão best-effort e fechar os Database handles.

Logs vão exclusivamente para stderr. Stdout contém somente mensagens do protocolo.

### 11.3 Configuração por harness

Exemplo conceitual:

```json
{
  "command": "oktografx-mcp",
  "args": [
    "--workspace", "/repo",
    "--agent-id", "codex-main",
    "--agent-name", "Codex",
    "--harness", "codex",
    "--read-scopes", "project,user",
    "--write-scope", "project"
  ]
}
```

Dois harnesses usam identidades distintas, mas o mesmo workspace. Cada um inicia seu próprio processo e ambos abrem os mesmos stores.

### 11.4 Tool surface inicial

Manter a superfície pequena e sem operações redundantes.

#### `grafx_status`

Retorna:

- agente e sessão atuais;
- workspace;
- scopes montados;
- read/write capabilities;
- system schema version;
- health resumido;
- limitações/degradações, como embedding provider ausente.

#### `grafx_schema_describe`

Entrada:

- scope explícito;
- namespace opcional;
- table opcional.

Retorna apenas schema e ownership. Não altera nada.

#### `grafx_recall`

Entrada:

- query;
- scopes;
- namespaces/filtros;
- kinds;
- agent filters opcionais;
- temporal filter/as_of;
- graph hops limitados;
- result limit;
- token budget.

Saída: `ContextPack` estruturado.

#### `grafx_remember`

Entrada:

- scope de escrita;
- namespace;
- kind;
- content/title;
- tags/metadata limitados;
- source/evidence opcional;
- importance, pin e expiry opcionais;
- idempotency key.

Saída:

- memory URI;
- operation id;
- scope;
- snapshot token;
- `deduplicated`/`replayed`.

#### `grafx_claim`

Uma tool com action explícita:

- `assert`;
- `corroborate`;
- `dispute`;
- `supersede`;
- `retract`.

Toda action mutável requer idempotency key.

#### `grafx_evidence_attach`

Anexa evidência a memory ou claim existente, respeitando scope e ownership.

#### `grafx_domain_apply`

Feature posterior ao MVP e desabilitada por default. Executa batch estruturado em namespace autorizado.

#### `grafx_query`

- read-only;
- scope único obrigatório;
- budgets obrigatórios;
- sem DDL;
- sem write query;
- retorna resultado limitado e tipado.

#### `grafx_scope_copy`

Actions:

- `promote` project → user;
- `materialize` user → project.

Exige capability no destino.

### 11.5 Tools administrativas

Não devem ser expostas ao modelo por default:

- `grafx_schema_ensure`;
- `grafx_namespace_create`;
- `grafx_policy_validate`;
- raw write query;
- maintenance destrutiva.

Podem existir na CLI administrativa ou ser habilitadas explicitamente.

### 11.6 Resources

Resources sugeridos:

- `grafx://workspace/manifest`;
- `grafx://workspace/policy`;
- `grafx://agent/self`;
- `grafx://schema/project`;
- `grafx://schema/user`;
- `grafx://health`;
- `grafx://claim/{scope}/{claim-id}`;
- `grafx://memory/{scope}/{memory-id}`;
- `grafx://entity/{scope}/{namespace}/{kind}/{id}`.

Busca dinâmica e mutações continuam sendo tools. Resources são usados para contexto identificável e leitura direta.

### 11.7 Contrato de erros

Erros públicos específicos:

- `AgentWorkspaceNotFound`;
- `AgentStoreKindMismatch`;
- `AgentScopeNotMounted`;
- `AgentScopeWriteDenied`;
- `AgentCrossScopeTransactionUnsupported`;
- `AgentIdentityRequired`;
- `AgentIdentitySpoofingDenied`;
- `AgentIdempotencyConflict`;
- `AgentNamespaceDenied`;
- `AgentSchemaConflict`;
- `AgentProvenanceInvariantFailed`;
- `AgentEmbeddingUnavailable`;
- `AgentContextBudgetExceeded`;
- `AgentSensitiveContentDenied`.

Erros do core não devem ser mascarados. A camada mapeia o código e preserva `retryable` quando aplicável.

---

## 12. Full-text, hybrid retrieval e ContextPack

Full-text/hybrid retrieval não está explicitamente fechado no roadmap atual e deve ser tratado como uma capacidade nova, ainda que reutilize o futuro framework de secondary indexes.

### 12.1 Full-text search

Requisitos:

- inverted index persistido e verificável;
- analyzer versionado;
- busca por termo, prefixo e frase;
- filtros por namespace, kind, status e tempo;
- tokenizer para linguagem natural;
- tokenizer identifier-aware para código, preservando símbolos, caminhos e `camelCase`/`snake_case`;
- rebuild e freshness explícitos;
- nenhuma resposta silenciosamente incompleta quando o índice estiver stale.

A implementação pode começar como extension da camada agent-first, mas a forma final preferida é um access path nativo/genérico, sem semântica de agentes no core.

### 12.2 Pipeline híbrido

```text
query
  ↓
normalização
  ├─ lexical candidates
  ├─ vector candidates por embedding space
  └─ entity/graph seed candidates
        ↓
policy, scope, status e temporal filters
        ↓
graph expansion limitada
        ↓
deduplicação cross-scope
        ↓
score fusion explicável
        ↓
reranker opcional
        ↓
ContextPack sob token budget
```

Regras:

- lexical search funciona sem embeddings;
- embedding generation fica fora do core;
- model id, dimension, dtype e embedding version são persistidos;
- troca de modelo cria novo embedding space/version;
- raw distances de spaces diferentes não são comparadas diretamente;
- reranking por LLM é opcional e nunca necessário para corretude;
- cada item retorna razões de seleção e score components;
- conteúdo armazenado é tratado como dado não confiável, não como instrução.

### 12.3 ContextPack

Campos mínimos:

- query;
- items;
- token estimate;
- requested/effective scopes;
- snapshot token por store;
- truncated;
- retrieval regimes usados;
- warnings/degradations;
- score explanation;
- provenance e evidence refs.

Cada item deve trazer:

- URI;
- scope;
- namespace/kind;
- conteúdo ou trecho;
- relevance score normalizado;
- lexical/vector/graph contributions;
- agent/source;
- claim status;
- timestamps;
- evidências;
- motivos de inclusão.

### 12.4 Graph expansion

A expansão deve ser controlada por:

- relationship allowlist;
- hop limit;
- node/edge budget;
- cycle detection;
- temporal/status filter;
- policy por namespace.

Nunca executar expansão ilimitada solicitada pelo modelo.

---

## 13. Temporalidade, contradição e lifecycle de memória

### 13.1 Dois tempos

Separar:

- transaction/recorded time — quando o Grafx registrou a informação;
- valid time — quando a afirmação é considerada válida no domínio.

`as_of` deve usar os registros lógicos de validade e supersession.

### 13.2 Lifecycle

Memory e claims suportam:

- `active`;
- `superseded`;
- `retracted`;
- `disputed`;
- `expired`;
- `pinned`.

### 13.3 Forgetting sem apagar evidência

`forget` deve, por default:

- encerrar a validade lógica;
- manter proveniência mínima;
- retirar o item do recall normal;
- deixar physical deletion para política administrativa e mecanismos de maintenance/vacuum.

### 13.4 Compaction sem perda de lineage

A camada pode resumir várias memories em uma nova memory:

- nova memory `summary`;
- relações `summarizes`/`derived_from` para as originais;
- originais continuam consultáveis conforme policy;
- nunca substituir um conjunto por resumo sem registrar lineage;
- o modelo/resumidor usado fica em provenance.

### 13.5 Resolução de claims

Views derivadas podem oferecer:

- active claims;
- claim history;
- disputed claims;
- corroborated claims;
- latest non-retracted revision.

Essas views não apagam as versões anteriores nem declaram automaticamente verdade.

---

## 14. Políticas e segurança local

Mesmo em `stdio`, o servidor manipula dados sensíveis e tools model-controlled. A primeira versão precisa de políticas explícitas.

### 14.1 Capabilities

Capabilities sugeridas:

- `project:read`;
- `project:write`;
- `project:schema:read`;
- `project:schema:write`;
- `user:read`;
- `user:write`;
- `user:schema:read`;
- `user:schema:write`;
- `raw:read`;
- `raw:write`;
- `scope:promote`;
- `admin`.

Defaults recomendados:

- project read/write: habilitado;
- user read: habilitado quando montado;
- user write: desabilitado;
- schema write: desabilitado;
- raw read: configurável;
- raw write: desabilitado.

### 14.2 Content policy

- limite de bytes por content/evidence/metadata;
- redaction hook antes do write;
- recusa ou mascaramento de secrets conhecidos;
- path e locator normalizados;
- excerpts limitados;
- prompt/transcript capture desativado;
- global promotion pode exigir scanner/approval policy;
- stored content marcado como untrusted no ContextPack.

### 14.3 Identity policy

- tool input não contém actor id autoritativo;
- tentativas de atribuir operação a outro agent são recusadas;
- identidade efêmera é visível nas respostas;
- identity assurance participa de filtros de confiança, mas não altera dados silenciosamente.

### 14.4 Schema poisoning

- system prefix reservado;
- namespace ownership;
- exact-match ensure;
- criação de schema por capability;
- no DDL via `grafx_query`;
- limites de quantidade de tabelas, colunas e schema proposals na camada agent-first.

### 14.5 Scope leakage

- nenhuma tool aceita path;
- user store pode ser totalmente desabilitado;
- cada resultado inclui scope;
- project A não consulta project B;
- promoção é explícita e auditada;
- symlink/path traversal testados.

---

## 15. Observabilidade agent-first

Métricas propostas, separadas do catálogo interno do core quando necessário:

- `agent_operations_total{operation,scope,status}`;
- `agent_operation_latency_seconds{operation,scope}`;
- `agent_write_conflicts_total{scope}`;
- `agent_idempotency_replays_total{operation}`;
- `agent_policy_denials_total{capability}`;
- `agent_schema_conflicts_total{scope}`;
- `agent_memories_total{kind,status,scope}`;
- `agent_claims_total{kind,status,scope}`;
- `agent_context_pack_items{scope}`;
- `agent_context_pack_tokens_estimated`;
- `agent_recall_latency_seconds{regime}`;
- `agent_global_promotions_total{status}`;
- `mcp_requests_total{tool,status}`.

Logs devem conter `operation_id`, `session_id`, `agent_id`, `scope` e `correlation_id`, sem conteúdo sensível por default.

---

## 16. Roadmap de implementação

## AGENT-0 — Contratos, ADRs e package boundaries

### Objetivo

Congelar a arquitetura antes de implementar comportamento.

### Entregas

- ADR para cada decisão D1–D10;
- layout de packages/wheels;
- protocols e DTOs da Agent API;
- taxonomia de erros;
- versões independentes:
  - `agent_api_version`;
  - `agent_schema_version`;
  - `mcp_contract_version`;
- import-boundary tests;
- skeleton sem comportamento;
- documento de threat model inicial.

### Gate de saída

- `okto-grafx` mantém as dependências runtime atuais;
- importar o core não importa MCP/agent packages;
- remover agent/mcp packages não afeta a suíte do core;
- dependency graph automatizado na CI;
- todas as decisões arquiteturais possuem teste ou regra verificável associada.

### Dependência

Nenhuma. Pode iniciar imediatamente.

---

## AGENT-1 — Workspace resolver e scoped stores

### Objetivo

Fazer vários processos resolverem deterministicamente o mesmo project/user store, sem aceitar paths do modelo.

### Entregas

- `WorkspaceResolver`;
- `UserDataPathProvider`;
- `workspace.toml` e policy loader;
- `oktografx-agent init`;
- `oktografx-agent status`;
- `StoreMount` e `ScopedStoreRouter`;
- store metadata reservado;
- read scope `project`, `user`, `merged`;
- write scope único;
- snapshot metadata em respostas;
- URI lógica cross-scope.

### Gate de saída

- dois processos iniciados em subdiretórios diferentes resolvem o mesmo workspace;
- dois projetos distintos permanecem isolados;
- ambos podem montar o mesmo user store quando habilitado;
- user write é negado por default;
- nenhuma chamada semântica aceita path;
- mount com store kind incorreto falha;
- merged read retorna scope e snapshot por item/store;
- tentativa de write em `merged` falha com erro tipado.

### Dependência

AGENT-0. Preferir a facade pública tipada de M1.

---

## AGENT-2 — Agent identity, sessions, provenance e idempotência

### Objetivo

Tornar toda mutação atribuível, retomável e atomicamente auditável.

### Entregas

- system schema v1 para store, agent, session, operation e mutation;
- `AgentIdentityProvider`;
- registro de AgentProfile/AgentSession;
- `ProvenanceService`;
- idempotency receipts;
- retry wrapper para conflitos;
- policy capabilities;
- correlation id opcional com Nexus/outros sistemas.

### Gate de saída

- mutação e operation provenance aparecem juntas ou não aparecem;
- crash/retry com mesma idempotency key não duplica resultado;
- mesma chave com payload diferente é recusada;
- agent não consegue se passar por outro via input;
- system namespace é imutável para domain tools;
- duas instâncias do mesmo/diferentes agents escrevem concorrentemente e preservam autoria;
- sessão interrompida é distinguível de sessão encerrada.

### Dependência

AGENT-1 e transações multi-statement estáveis.

---

## AGENT-3 — Memory, claims, evidence e Agent API

### Objetivo

Entregar as primitivas semanticamente interoperáveis, antes de expô-las via protocolo.

### Entregas

- system schema de memory, claim e evidence;
- `MemoryService`;
- `ClaimService`;
- `EvidenceService`;
- `remember`, `recall` inicial, `assert`, `dispute`, `corroborate`, `supersede`, `retract`;
- referências URI;
- promotion/materialization idempotentes;
- Python Agent API pública;
- documentação de exemplos sem MCP.

### Gate de saída

- agente A grava memory e agente B a recupera;
- claims contraditórios coexistem e mantêm autores/evidências;
- supersession não destrói histórico;
- retraction remove do recall normal, mas mantém lineage;
- promoção project → user exige capability e é idempotente;
- não existe edge física cross-store;
- todo resultado semântico possui provenance e scope.

### Dependência

AGENT-2.

---

## AGENT-4 — MCP stdio preview

### Objetivo

Tornar a Agent API acessível por diferentes harnesses locais sem daemon.

### Entregas

- package `okto-grafx-mcp`;
- entrypoint `oktografx-mcp`;
- servidor stdio;
- tools MVP;
- resources MVP;
- schema JSON versionado;
- mapeamento de erros;
- logging exclusivo em stderr;
- exemplos de configuração para harnesses;
- testes com client de referência/inspector.

### Gate de saída

- dois clients lançam dois subprocessos MCP independentes;
- ambos acessam o mesmo project store;
- uma gravação de A é recuperável por B;
- nenhum daemon, socket ou porta é iniciado;
- stdout contém somente protocolo;
- EOF/termination fecha handles;
- kill durante write mantém as garantias do core e retry idempotente;
- tools não expõem path nem actor id autoritativo;
- tool contract permanece estável sob `mcp_contract_version`.

### Dependência

AGENT-3.

### Observação de release

Este milestone pode ser publicado como preview funcional. Não deve prometer throughput multiprocesso elevado antes do gate de identity leasing/performance do roadmap principal.

---

## AGENT-5 — Namespaces governados e domain mutation

### Objetivo

Permitir que agentes criem e usem schemas de domínio sem transformar o store em uma coleção incompatível de tabelas arbitrárias.

### Entregas

- `NamespaceDefinition`;
- ownership e capabilities;
- `schema.describe`, `schema.propose` e `schema.ensure`;
- exact-match validation;
- diff estruturado;
- `DomainMutationService`;
- `grafx_domain_apply` desabilitado por default;
- limites de schema e property payload;
- provenance por target URI.

### Gate de saída

- agent-private namespace não altera shared namespace;
- schema incompatível falha antes de qualquer write;
- `ensure` repetido é idempotente;
- global schema write é negado por default;
- batch inteiro é atômico dentro de um store;
- todas as mutações possuem operation/mutation records;
- raw write continua fora da tool surface padrão.

### Dependência

AGENT-3; não precisa bloquear AGENT-4, pois o preview pode operar apenas com o system schema.

---

## AGENT-6 — Full-text, hybrid retrieval e ContextPack

### Objetivo

Transformar o Grafx em um substrate de contexto útil para agentes, não apenas em um storage acessível por tools.

### Entregas

- text-search spec;
- analyzer natural e code-aware;
- lexical candidate generation;
- integração com vector search;
- graph expansion com budgets;
- score fusion explicável;
- embedding/token/reranker ports;
- `ContextPack`;
- merged retrieval project + user;
- deduplicação com provenance preservada;
- `as_of` e status filters básicos.

### Gate de saída

- recall funciona sem embedding provider;
- queries por identificador de código encontram símbolos relevantes;
- token budget é respeitado antes do retorno;
- nenhuma comparação direta entre scores incompatíveis de embedding spaces;
- cada item informa por que foi selecionado;
- stale text/vector index não produz resultado silenciosamente curto;
- expansão em grafo respeita hop/node/edge budgets;
- conteúdo retornado é marcado como untrusted data.

### Dependência

AGENT-3. Para escala e promessa de performance, depende do HNSW access path real e da fundação de index/planner do roadmap principal.

---

## AGENT-7 — Temporalidade, lifecycle e qualidade de memória

### Objetivo

Controlar envelhecimento, contradição e compactação do conhecimento sem perder lineage.

### Entregas

- transaction time e valid time;
- query `as_of` lógica;
- expiry/pinning;
- views de active/disputed/history;
- compaction/summarization com lineage;
- deduplication policy;
- trust/authority filters configuráveis;
- forget/retract sem physical delete imediato;
- policy de promoção baseada em status/evidência.

### Gate de saída

- vacuum físico futuro não quebra histórico lógico declarado;
- `as_of` retorna a versão semanticamente válida;
- summary aponta para todas as fontes resumidas;
- claims disputados não desaparecem;
- expired/retracted não entram no recall default;
- auditor pode reconstruir autor, sessão, operação e evidências.

### Dependência

AGENT-3 e AGENT-6 para recall completo.

---

## AGENT-8 — Hardening, DX e release operacional

### Objetivo

Transformar o preview em produto operável e documentado.

### Entregas

- security test suite;
- secret/redaction adapters;
- métricas agent-first;
- tracing/correlation;
- CLI de inspeção;
- viewer/inspector opcional;
- templates de configuração por harness;
- conformance matrix;
- migration/version compatibility policy da Agent API;
- runbooks de backup, restore, verify e recuperação;
- benchmarks de overhead de proveniência e merged recall;
- documentação de limites.

### Gate de saída

- multiprocess/crash matrix completa;
- nenhuma fuga project → outro project;
- user write e schema write continuam fail-closed;
- restore testado de ponta a ponta;
- upgrade do agent schema testado;
- performance de provenance medida e documentada;
- ferramentas funcionam em Windows e POSIX;
- documentação executável na CI;
- release depende dos gates operacionais correspondentes do roadmap principal.

### Dependência

AGENT-4 a AGENT-7 e capacidades de operação segura do plano principal.

---

## 17. Paralelismo recomendado

```text
AGENT-0
   ↓
AGENT-1
   ↓
AGENT-2
   ↓
AGENT-3 ───────────────┬──────────────┐
   ↓                   ↓              ↓
AGENT-4 MCP       AGENT-5 schema  AGENT-6 retrieval
   └──────────────┬─────┴──────────────┘
                  ↓
               AGENT-7
                  ↓
               AGENT-8
```

- AGENT-0 pode começar imediatamente.
- AGENT-4 e AGENT-5 podem avançar em paralelo depois de AGENT-3.
- AGENT-6 pode começar pela contract/spec, mas seu gate de performance depende das melhorias existentes do engine.
- AGENT-8 não deve declarar GA antes de backup/restore/migration e dos gates de concorrência do plano principal.

---

## 18. Cenário end-to-end de aceite

O roadmap só está funcionalmente completo quando o cenário abaixo passa de ponta a ponta:

1. usuário executa `oktografx-agent init` em `repo-a`;
2. configura dois harnesses com o mesmo workspace e agentes diferentes;
3. cada harness inicia seu próprio `oktografx-mcp` por stdio;
4. agente A registra uma decisão arquitetural no project scope com evidence de arquivo/commit;
5. agente B recupera a decisão via `grafx_recall`;
6. agente B registra claim contraditório com nova evidence;
7. o banco preserva ambos, suas autorias e a relação de contradição;
8. uma revisão cria novo claim que supersede o anterior sem apagá-lo;
9. usuário habilita `user:write` e promove apenas a versão verificada;
10. `repo-b` monta o mesmo user store e recupera o item global;
11. `repo-b` não consegue consultar nenhum dado exclusivo do project store de `repo-a`;
12. um processo é morto durante uma mutação e o retry com a mesma idempotency key não duplica o item;
13. `verify()` permanece limpo nos dois stores;
14. o ContextPack informa scope, snapshot, autoria, evidências, status e razão de seleção.

---

## 19. Estrutura de código sugerida

Preferência por monorepo com packages independentes:

```text
packages/
  okto-grafx/
    src/okto_grafx/...

  okto-grafx-agent/
    src/okto_grafx_agent/
      domain/
        models.py
        errors.py
        scopes.py
        identifiers.py
        policies.py
      ports/
        workspace.py
        identity.py
        embedding.py
        tokenizer.py
        reranker.py
        redaction.py
        metrics.py
      application/
        workspace.py
        scoped_router.py
        agent_registry.py
        provenance.py
        memory.py
        claims.py
        evidence.py
        schema_registry.py
        domain_mutation.py
        context.py
        scope_transfer.py
      adapters/
        grafx_store.py
        workspace_local.py
        identity_config.py
        policy_toml.py
        embedding_none.py
        tokenizer_basic.py
      schema/
        v1.py
        installer.py
        validator.py
      api/
        facade.py
        dto.py

  okto-grafx-mcp/
    src/okto_grafx_mcp/
      server.py
      config.py
      lifecycle.py
      tools.py
      resources.py
      errors.py
      output.py
      entry.py
```

Se o repositório permanecer single-package inicialmente, manter ao menos namespaces e testes de import boundary independentes. A migração para wheels separados deve continuar possível sem mover o core internamente.

---

## 20. Estratégia de testes

### 20.1 Unitários

- workspace resolution;
- path canonicalization;
- scope routing;
- capability checks;
- schema diff;
- idempotency keys;
- claim transitions;
- score fusion;
- token budgets;
- URI parsing;
- redaction hooks.

### 20.2 Integração

- dois Database handles no mesmo processo;
- dois processos Agent API no mesmo store;
- dois subprocessos MCP stdio;
- project + user merged reads;
- promotion/materialization;
- schema install concorrente;
- AgentOperation + mutation atomicity;
- claims/evidence/relations no mesmo commit.

### 20.3 Crash e recovery

- kill antes/depois do commit de uma semantic operation;
- kill durante criação da sessão;
- kill durante promotion;
- retry após commit incerto;
- reabertura read-only;
- verify após cada janela.

### 20.4 Segurança

- path traversal;
- symlink escape;
- actor spoofing;
- system namespace mutation;
- user write sem capability;
- raw write via read query;
- payload acima do limite;
- secret redaction/denial;
- prompt injection armazenada tentando alterar scope/path;
- cross-project leakage.

### 20.5 Conformance MCP

- framing stdio;
- stdout limpo;
- JSON schemas;
- tool/resource discovery;
- malformed inputs;
- cancellation/termination quando suportadas pelo SDK;
- compatibility matrix por protocolo/SDK suportado.

### 20.6 Performance

- overhead por semantic write com provenance;
- contention com N MCP processes;
- project-only vs merged recall;
- lexical/vector/graph candidate sizes;
- ContextPack latency e peak memory;
- idempotent replay;
- promotion de lotes.

---

## 21. Instruções de execução para o Codex

1. tratar `EVOLUTION_PLAN_CODEX.md` como autoridade para todos os itens excluídos deste documento;
2. não alterar o core para acomodar semântica de agente sem provar que a capacidade é genérica;
3. qualquer mudança inevitável no core deve entrar primeiro como requisito genérico no roadmap principal;
4. implementar um milestone por branch/PR;
5. começar cada milestone por contratos, erros e testes de gate;
6. não introduzir dependency MCP no wheel do core;
7. não oferecer raw write no MVP;
8. não implementar `merged` como transação distribuída;
9. não armazenar prompt/transcript por default;
10. não escolher “claim vencedor” automaticamente;
11. manter um registro de execução com SHA, testes, auditoria e débitos abertos;
12. não declarar milestone concluído apenas porque os happy paths passaram;
13. executar testes multiprocesso e crash/recovery nos milestones que escrevem dados;
14. preservar Windows e POSIX como cidadãos equivalentes;
15. falhar explicitamente diante de schema, scope, store ou identity ambíguos.

Formato recomendado de registro:

```text
Milestone:
Branch:
Commit imutável:
Entregas:
Testes coletados:
Testes aprovados:
Matriz multiprocesso:
Matriz de crash:
Auditoria:
Débitos aceitos:
Próximo gate:
```

---

## 22. Non-goals e itens deliberadamente adiados

- daemon central para o MCP;
- Streamable HTTP no primeiro ciclo;
- OAuth para o deployment local stdio;
- cluster, consenso, sharding ou distributed transactions;
- sincronização automática project ↔ user;
- coordenação, handoff ou messaging entre agents;
- memória implícita extraída de todos os prompts;
- execução automática de comandos armazenados;
- LLM obrigatório para recall;
- embeddings gerados dentro do core;
- arbitrary DDL por qualquer agent;
- raw write Cypher como tool padrão;
- confiança numérica tratada como verdade;
- histórico baseado na retenção acidental de versões MVCC;
- relações físicas atravessando stores;
- compatibilidade retroativa ilimitada durante pre-alpha.

---

## 23. Ordem de prioridade recomendada

A ordem de maior retorno para menor risco é:

1. **package boundary + Agent API contracts**;
2. **workspace/project/user scopes**;
3. **agent identity + session + proveniência + idempotência**;
4. **memory + claims + evidence**;
5. **MCP stdio preview**;
6. **namespace/schema governance**;
7. **full-text + hybrid retrieval + ContextPack**;
8. **temporalidade e lifecycle de memória**;
9. **hardening, DX e release operacional**.

A primeira proposta de valor tangível não exige que Grafx vire um banco distribuído nem que alcance paridade funcional completa com ferramentas maiores:

> Dois ou mais agentes locais, iniciados por harnesses diferentes, conseguem compartilhar conhecimento durável no mesmo projeto, sabem quem afirmou cada informação, preservam evidências e conflitos, consultam contexto local e global e fazem tudo isso sem um servidor central.

Esse resultado transforma a principal característica arquitetural do Grafx — acesso embedded multiprocesso ao mesmo store — em uma experiência de produto diretamente compreensível para aplicações agentic.
