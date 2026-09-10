# Adoção das capacidades do Grafx no Pulse

Data: 2026-09-09. Status: **avaliação estática; não implementado**.

Atualização posterior: o usuário autorizou a adoção. A instalação e o primeiro
lote estão registrados em [GRAFX_V005_ADOPTION.md](../../../okto-pulse-v003-kg-load-codex/docs/GRAFX_V005_ADOPTION.md).
Testes de importação corrigiram a identificação do par Community/Core: o Core
compatível com o worktree Community atual é `okto-pulse-core-kg5-codex@9303f98`,
não o baseline parcial de paginação citado abaixo. A análise original é mantida
como histórico; não constitui evidência de que toda a adoção foi implementada.

[Índice de relatórios](README.md) · [Validação do candidato 0.0.5](V005_PRE_PULSE_VALIDATION.md)

## Conclusão e limites

O Pulse pode aproveitar mais o Grafx sem introduzir dependência do motor no Core.
As oportunidades mais imediatas são a separação de participantes no Global
Discovery, a distribuição limitada de leituras e a busca textual nativa. A busca
vetorial já tem integração com índices: não é uma capacidade inteiramente ausente.

Há duas ressalvas importantes:

1. Core agnóstico não significa Core congelado. Novas semânticas de pesquisa ou
   uma revisão da serialização de consolidações podem exigir contratos/políticas
   genéricos no Core, implementados pelo Community.
2. O híbrido nativo do Grafx não é semanticamente equivalente ao híbrido atual do
   Pulse. Uma substituição direta perderia capacidades de expansão e ranking.

Esta avaliação não iniciou instalações, chamadas ao Pulse, consolidações,
benchmarks ou alterações de implementação. Os ganhos abaixo são hipóteses
fundamentadas no código, não medições de latência do processo ativo. A evolução
do motor continua pausada; este documento não cria uma nova rodada de Grafx.

## 1. Baseline que deve preceder o teste no Pulse

- Grafx avaliado: `feature/v0.0.5`, commit
  `8c3f6f2a6be8c8bdcdd15113fd5f37b2b965a173`, com a validação registrada no
  [relatório do candidato](V005_PRE_PULSE_VALIDATION.md).
- Community integrado: worktree
  `D:/Projetos/Techridy/okto-pulse-v003-kg-load-codex`, branch
  `fix/v0.3.3-grafx-transparent-recovery`, HEAD `7158383`, com alterações locais
  preexistentes. Referências abaixo descrevem os arquivos inspecionados, não
  garantem que todos estejam contidos nesse commit. O `pyproject.toml` ainda
  referencia `okto-grafx[accel]==0.0.4`.
- Core correspondente: `D:/Projetos/Techridy/okto-pulse-core-pagination-codex`,
  HEAD `4dca9b8`. Os arquivos de contratos, coordenação e híbrido comparados
  coincidem com a instalação local após normalização de finais de linha.
- O checkout `D:/Projetos/Techridy/okto-pulse` é um baseline anterior, sem essa
  integração. Não foi utilizado como evidência de ausência de recursos atuais.

Foram identificadas duas instalações de Pulse 0.3.3: Python user-site e ferramenta
global do `uv`. Ambas declaram Grafx 0.0.5, mas, dos 185 arquivos Python do candidato,
**101 coincidem, 48 diferem e 36 estão ausentes em cada instalação**, normalizando
CRLF/LF. Isso evidencia builds de desenvolvimento diferentes sob a mesma versão;
não demonstra corrupção do banco. Também há divergências no Community instalado:
a separação de leitores existe, mas o módulo recente `grafx_read_lanes.py` não está
presente na instalação user-site examinada.

Antes de comparar desempenho, instalar o artefato exato validado com `[accel]`,
identificar o launcher/ambiente utilizado e registrar hashes e procedência do
Community. Apenas verificar `__version__` não é suficiente. Não foi identificado
nesta avaliação qual código um eventual processo já aberto mantém em memória.

## 2. Fronteira arquitetural obrigatória

| Responsabilidade | Local correto |
| --- | --- |
| Intenção da busca, autorização, regras de consolidação e consistência de negócio | Core, independente do banco |
| Contratos de busca, transação, paginação e resultados parciais | Portas/DTOs neutros do Core |
| `Database`, `RecordId`, índices, opções Grafx, participantes, tradução de consultas e erros | Adaptadores Community |
| Limites de concorrência, filas de execução, configuração do motor e lifecycle | Composição/runtime Community, respeitando as políticas do Core |
| Parâmetros específicos do Grafx em Settings e sua validação | Community; não contaminar DTOs de domínio |
| IDs físicos, CSN, receipts e diagnósticos nativos | Interpretados no Community; expor ao Core somente informação neutra necessária |

Não introduzir `import okto_grafx`, tipos nativos, nomes de índices ou condições
`if backend == "grafx"` no Core. A aplicação também não pode delegar autorização
ao motor: a API embarcada não fornece uma fronteira de autenticação/ACL.

## 3. Ordem recomendada de adoção

Esforço e impacto são estimativas qualitativas. A numeração é uma sequência
proposta para o Pulse, não uma autorização de implementação.

| Ordem | Iniciativa | Impacto potencial | Esforço | Fronteira |
| --- | --- | --- | --- | --- |
| 0 | Fixar o artefato e o baseline realmente instalados | Evita testes e diagnósticos sobre versões distintas | Baixo/médio | Instalação/Community |
| 1 | Separar leituras do Global Discovery e completar o uso das lanes do Board | Alto sob escrita/consolidação concorrente | Médio/alto | Community |
| 2 | Execução bloqueante delimitada e concorrência com limites/prioridades | Alto se há espera no event loop ou fila compartilhada | Médio | Community; preservar políticas genéricas |
| 3 | Busca textual nativa, opt-in, com índices seletivos | Alto para pesquisa textual e relevância | Médio | Community; contrato neutro se houver nova semântica |
| 4 | Reduzir fallback vetorial e integrar recuperação híbrida onde houver equivalência | Médio/alto em buscas; depende da seletividade | Médio/alto | Community + possíveis portas neutras |
| 5 | Escritas homogêneas em lote e revisão de concorrência por conflito real | Médio/alto para ingestão; requer atribuição do custo | Médio/alto | Community; política de consolidação no Core |
| 6 | Projeções/analytics e transporte de páginas mais econômicos | Seletivo: analytics e payload, não ganho universal | Médio | Community + contratos neutros quando necessário |
| 7 | Proveniência operacional, manutenção e backup verificado | Alto em operabilidade; ganho de latência indireto | Médio | Community |

### 3.1 Participantes independentes: completar o que já existe

O Board já possui dois pools de leitura separados do escritor. No worktree, a
seleção usa ocupação por operação, com desempate circular. Portanto, a entrega não
deve recriar uma solução já existente: deve alinhar a instalação e verificar todos
os consumidores, inclusive vetor, health e leituras em streaming. A escolha de
lane e a proteção contra fechamento devem cobrir a operação inteira, não somente
a obtenção do handle. [C1, C2]

O Global Discovery oferece uma oportunidade mais clara: `_call_runtime` mantém
`_global_lock` durante a chamada ao runtime; o runtime Grafx também utiliza seu
próprio `RLock` em leituras e escritas, com um único resolver. A capacidade
multi-reader do banco fica limitada por essa serialização externa. [C3, C4]

Proposta: participantes de leitura globais independentes, quantidade limitada,
admissão curta e proteção de geração por operação. Preservar exclusividade e
drenagem para recovery, promoção, purge, revogação de acesso e shutdown; preservar
writer fencing. **Não basta remover os locks.** Um leitor não pode continuar
servindo uma geração cuja autorização já foi revogada.

Expor quantidade de leitores, limite de leituras ativas, espera de admissão e
reserva de capacidade para UI/background no Community, com validação conjunta e
métricas. Não tornar pools ilimitados nem alterar suas regras de fechamento por
mera configuração: handles adicionais multiplicam memória e descritores.

O próprio contrato Grafx mantém uma seção exclusiva de publicação, validações OCC
e conflitos físicos possíveis. Participantes independentes permitem sobreposição
legítima; não significam commits sem serialização nem escalabilidade linear.
Ver [concorrência operacional](../OPERATIONS.md).

### 3.2 Execução bloqueante e filas

`GrafxGraphTransaction.commit` é assíncrono na interface, mas chama `commit()`
nativo síncrono. Isso pode bloquear o event loop quando o chamador não está numa
ponte de execução bloqueante. Não é correto concluir que todo caminho já bloqueia:
o outbox usa a infraestrutura genérica de blocking I/O, e o Community já possui
execução rastreada, preservação de contexto e drenagem no cancelamento. [C5, K2]

Mapear os chamadores e reutilizar essa infraestrutura nos caminhos descobertos.
Não distribuir aleatoriamente cada statement por threads: respeitar propriedade
da transação, contexto de autoridade, locks e fechamento. Separar admissão de
trabalho interativo e background, com limites e backpressure. Threads não resolvem
por si só CPU Python limitada pelo GIL.

Medir separadamente espera de fila, admissão/open, consulta, commit e serialização
da resposta. Isso distingue latência do Grafx de espera introduzida pelo host.

### 3.3 Busca textual: capacidade disponível e não adotada

Não foi encontrado uso de `create_text_index`, `search_text` ou `search_hybrid`
nativos nos adaptadores Community examinados. Existem pesquisas com `CONTAINS`,
por exemplo em áreas de Learning e tópicos de Decision. [C6, K3]

O Grafx oferece índices textuais persistidos, BM25, pesos por campo, analisadores
e filtros de candidatos. Proposta inicial: pesquisa ranqueada sobre campos úteis
de Decision/DecisionDigest e Learning, em vez de indexar indiscriminadamente
todos os conteúdos. Para leitura quente, avaliar `statistics_mode="durable"`;
resumos históricos têm retenção limitada e custo próprio. Não prometer ausência
de census para todo snapshot. [Full-text search](../FULL_TEXT_SEARCH.md)

Cuidados de contrato:

- Busca por tokens não equivale a substring, frase ou linguagem booleana. Manter
  `CONTAINS` quando esse for o contrato; oferecer o novo modo explicitamente.
- Aplicar visibilidade, board, camada, estado ativo/superseded e revogação antes
  do top-k, no mesmo snapshot. Resolver IDs físicos para IDs lógicos no adapter.
- Filtros não ocultam estatísticas do corpus. Avaliar separação de corpus por
  escopo e exposição de scores quando houver isolamento de informação.
- Índices acrescentam armazenamento, manutenção e escrita/WAL. Criá-los por
  migração controlada, com readiness, não em uma leitura da UI.
- Corrupção, timeout ou índice inválido não devem virar uma lista vazia de hits.

### 3.4 Vetores e híbrido: melhorar sem perder semântica

A busca vetorial do Board já executa `similarity(...)` com ordenação e limite.
Porém, `_needs_exact` aciona fallback quando a página não excede top-k ou há empate
na borda; o caminho exato busca embeddings elegíveis e calcula/ordena scores em
Python. Isso pode ser caro em consultas seletivas ou com poucos hits. [C7]

Avaliar APIs tipadas `search_vectors`, filtros nativos, diagnósticos e janelas
limitadas de candidatos para reduzir trabalho duplicado. Preservar normalização,
desempate e modo exato/exaustivo: uma janela ANN maior não prova completude global.
Registrar frequência e volume real do fallback antes de escolher a mudança.

O híbrido atual do Pulse combina sementes vetoriais, expansão entre tipos de nós,
proximidade, confiança, recência e eventual rerank. Já o híbrido Grafx combina texto
e vetor por RRF numa tabela de nós; o componente de grafo não cria novos hits fora
dos candidatos-base e tem escopo restrito. [C8, K4]

Usar o híbrido nativo como recuperação especializada por tipo, se útil, preservando
a expansão e as regras de domínio posteriores. Não comparar BM25 de corpora
independentes como se fosse a mesma escala nem rotular RRF como similaridade
cosseno. Se necessário, criar uma porta neutra de recuperação ranqueada com
origem, score/rank, explicação e estado parcial, em vez de distorcer
`VectorSeedProvider`. O contrato nativo `allow_partial` não permite esconder
corrupção ou expiração de orçamento. [Busca híbrida](../HYBRID_SEARCH.md)

Também revisar o `except Exception` por tipo no seed provider atual: hoje uma
falha pode apenas gerar log e seguir, confundindo indisponibilidade com ausência
de resultados. A UI precisa distinguir resultado vazio de resultado incompleto.

### 3.5 Escrita: lotes e concorrência de negócio

Usar `Transaction.executemany` para lotes homogêneos pode reduzir preparação e
overhead por chamada. Ele tem savepoint por lote e **não faz commit sozinho**.
O adaptador já possui tradução e agrupamentos; comparar antes de substituir
esses caminhos. Preservar unidade de trabalho, recibos, referências, endpoints,
fencing e a distinção entre falha pré-COMMIT e escrita possivelmente durável. [C5;
API de lotes](../API_REFERENCE.md#transactionexecutemany)

Há uma restrição fora do Community: `commit_consolidation` passa por
`run_with_commit_lock_and_retry`, cujo coordenador mantém lock por board durante
a operação e retries. Logo, múltiplos handles não tornam essas consolidações do
mesmo board concorrentes. Isso é política genérica de aplicação, não deficiência
da API multiwriter do Grafx. [K1]

Começar pela concorrência entre boards e operações realmente independentes.
Para o mesmo board, uma futura granularidade por conjunto de conflitos exige
prova de invariantes de domínio, SQLite/outbox, deduplicação e retries. Essa
revisão pode ocorrer no Core de modo agnóstico; o Community não deve contornar
o coordenador. Grafx não torna a transação SQLite + grafo atomicamente distribuída.

### 3.6 Projeções, páginas e analytics

`GraphProjection`, adjacência preparada, componentes, caminhos, PageRank e
exportação tabular permitem implementar análises de conectividade e vizinhança
sem um loop de consultas por nó. São bons candidatos para relatórios de KG Health
ou análises explícitas, não substitutos da consolidação cognitiva de domínio.
[Projeções](../GRAPH_PROJECTIONS.md)

Capturar subgrafos limitados, executar analytics sobre a captura e associar caches
à geração/snapshot e ao escopo autorizado, com limites de memória. **Não carregar
o grafo inteiro para atender cada página de +500 nós.**

Para páginas/exportações, explorar seleção de colunas e orçamento de bytes em
`scan_rows_v1`, omitindo embeddings quando não forem necessários. Logical transfer
já utiliza essa família de scans no Community. Preservar ordenação, filtros,
totais e cursor público; um cursor nativo pertencente a uma transação não pode
ser reutilizado entre requisições sem lifecycle e TTL explícitos. Não alongar
snapshots indiscriminadamente para implementar paginação. [C9]

### 3.7 Operação e observabilidade

Metadados de commit podem correlacionar job/outbox/consolidação a evidências
duráveis no Community. Não equivalem a idempotência, time-travel ou atomicidade
com SQLite. [Histórico de commits](../COMMIT_HISTORY.md)

Backup verificado, métricas de índices e manutenção limitada podem enriquecer
health/recovery. Preservar promoção de geração, reconciliação da aplicação e
validação fail-closed. Restaurar apenas o Grafx não garante um ponto coerente com
SQLite; manutenção com exigência de quiescência não deve ser disparada como leitura
de health. [Backup/restore](../BACKUP_RESTORE.md) · [Operações](../OPERATIONS.md)

## 4. Critério finito de validação da adoção

Cada lote deve ter testes focados, e uma regressão consolidada ao final. Não criar
novos gates de performance longos nem consumir todas as specs reservadas.

1. Confirmar artefato/launcher e executar um smoke de integração isolado antes do
   teste no data home real. Não reconstruir ou consolidar dados como efeito oculto.
2. Testar leitura durante commit, leitores simultâneos, escritas independentes,
   conflitos reais e invariantes de snapshot/durabilidade. Cobrir cancelamento,
   fila cheia, fechamento, recovery e troca/revogação de geração.
3. Para buscas, comparar resultados esperados: texto vs substring, filtros antes
   do top-k, score/rank, desempate, fallback exato e sinalização de falha parcial.
4. Repetir um conjunto pequeno e fixo pela API Grafx, API Pulse e UI: carga inicial
   do KG, +500, busca e leitura durante uma escrita controlada. Registrar latência
   ponta a ponta e por fase, frio/quente, volume retornado, CPU/memória e custo de
   escrita após novos índices. Não publicar ganhos percentuais sem esse experimento.
5. Verificar que Core continua sem imports/tipos/configuração do Grafx; testar
   novos contratos com implementação neutra/fake, sem necessidade de Ladybug.

## 5. Referências do código inspecionado

Os links de Pulse abaixo apontam para worktrees locais irmãos e podem não existir
em outro checkout. As linhas são as observadas nesta avaliação; o baseline e a
ressalva sobre alterações locais estão na seção 1.

- **C1:** [routed_board_graph_composition.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/routed_board_graph_composition.py), linhas 397–471 e 979–989; [grafx_read_lanes.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/grafx_read_lanes.py).
- **C2:** [grafx_graph_store.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/grafx_graph_store.py), `_read` linha 260 e `vector_search` linha 1169; [grafx_database_pool.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/grafx_database_pool.py), `get` linha 313 e admissão/ownership.
- **C3:** [routed_global_discovery.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/routed_global_discovery.py), `_call_runtime` linhas 422–453; [routed_global_graph_composition.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/routed_global_graph_composition.py), `_GrafxGlobalPoolManager` linha 318.
- **C4:** [grafx_global_discovery_runtime.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/grafx_global_discovery_runtime.py), construtor linhas 115–137, `execute` linha 309, busca linha 332 e upsert linha 383.
- **C5:** [grafx_graph_transaction.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/grafx_graph_transaction.py), tradução linha 728, commit linha 3272, begin linha 3388; [worker_runners.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/worker_runners.py), `TrackedBlockingExecution` linha 24.
- **C6:** [grafx_graph_store.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/grafx_graph_store.py), filtro textual de Learning linhas 1297–1300.
- **C7:** [grafx_board_vector_search.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/grafx_board_vector_search.py), consulta indexada linha 57, `_needs_exact` linha 218 e execução linha 240.
- **C8:** [hybrid_search.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/hybrid_search.py), seed provider linha 23, loop por tipos linha 61 e expander linha 108.
- **C9:** [logical_transfer_grafx.py](../../../okto-pulse-v003-kg-load-codex/src/okto_pulse/community/adapters/logical_transfer_grafx.py), uso de `scan_rows_v1` linha 438.
- **K1:** [kg_operations.py](../../../okto-pulse-core-pagination-codex/src/okto_pulse/core/application/kg_operations.py), `commit_consolidation` linhas 351–374; [commit_coordinator.py](../../../okto-pulse-core-pagination-codex/src/okto_pulse/core/kg/commit_coordinator.py), lock por board e `run` linhas 32–80.
- **K2:** [blocking_io.py](../../../okto-pulse-core-pagination-codex/src/okto_pulse/core/kg/blocking_io.py), linha 26; [global_outbox.py](../../../okto-pulse-core-pagination-codex/src/okto_pulse/core/application/processors/global_outbox.py), ponte de I/O linha 140; [global_discovery_writer.py](../../../okto-pulse-core-pagination-codex/src/okto_pulse/core/kg/global_discovery_writer.py), autoridade/fencing.
- **K3:** [cypher_templates.py](../../../okto-pulse-core-pagination-codex/src/okto_pulse/core/kg/cypher_templates.py), pesquisas por tópico com `CONTAINS`, linhas 157 e 269.
- **K4:** [hybrid.py](../../../okto-pulse-core-pagination-codex/src/okto_pulse/core/kg/hybrid_search/hybrid.py), protocolos linhas 129/143, ranking linha 177, pipeline linha 261.
