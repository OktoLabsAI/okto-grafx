# Okto Grafx — plano final da nova rodada de performance

**Status:** em execução, com escopo congelado  
**Linha de evolução:** `0.0.2`  
**Data:** 2026-09-02 (`America/Sao_Paulo`)  
**Origem:** análise adversarial de `FABLE_PERFORMANCE_GRAFX.md`, código e roadmaps normativos, com consenso Codex/Claude pelo Okto Nexus

## 1. Decisão executiva

A tese principal do FABLE está correta: depois das melhorias já entregues, o maior potencial não está em tornar cada decode marginalmente mais rápido, mas em **evitar scans, releituras e materializações que não deveriam ocorrer**. O código confirma quatro custos importantes:

- `require_endpoints` pode executar dois lookups físicos lineares por aresta;
- o pouso de traversal monta um mapa por scan completo da tabela de nós;
- um acerto de índice exato é lido e decodificado no validador e novamente no executor;
- operações do `BufferPool` e partes do commit percorrem estruturas maiores que o conjunto efetivamente alterado.

O FABLE, contudo, **não deve ser executado literalmente**. A revisão adversarial encontrou erros de baseline, complexidade, encoding, overflow, semântica de corrupção, instrumentação e escopo. Este documento incorpora as correções aceitas por Codex e Claude e passa a ser a autoridade operacional desta rodada. O FABLE permanece como relatório de investigação e backlog de hipóteses.

A rodada terá somente:

1. diagnóstico e instrumentação reproduzíveis;
2. um lote fechado de otimizações Python puras, concentrado no caminho de leitura e com D-09 condicional no pipeline de commit;
3. no máximo **uma** otimização estrutural escolhida por evidência — ou nenhuma, se a evidência não justificar;
4. fechamento com gates de qualidade obrigatórios e relatório de resultados.

Não entram nesta rodada codecs nativos, mudança de formato, FTS, Arrow, compressão de WAL, quantização, alteração de ranking vetorial ou mudança das premissas multiwriter/multireader.

## 2. Baseline e autoridades

### 2.1 Commits pinados

- Base pública analisada pelo FABLE: Grafx `origin/main@ead05a4cfad5f5ca60c8677b330cc16bb6824b9b`.
- Base funcional usada no run real do Pulse: Grafx `c5ab19d`, que contém `ead05a4` + `58e2e7d` (admissão limitada dos valores documentais do Pulse) + documentação.
- Contraparte Pulse Community do run: `d50c03404bd72873b596596f1c4848d56dbcd437`.
- Contraparte Pulse Core do run: `f602c7cc2f6a9f5ef446d4c991309196bd4667c7`.
- O checkout primário atual `perf/a-index-freshness@4c474b5` está 96 commits atrás de `origin/main` e **não pode ser usado como base de implementação ou benchmark**.

A branch da rodada deve nascer do commit que promover `c5ab19d` para a linha canônica. Até essa promoção, `c5ab19d` é a base provisória exata. Rebase, merge ou avanço de `main` exige registrar um novo SHA e repetir somente o baseline afetado; nenhuma base pode mudar silenciosamente durante a rodada.

### 2.2 Hierarquia documental

1. Este documento governa apenas esta rodada de performance.
2. `EVOLUTION_PLAN_CODEX.md` continua sendo a autoridade do roadmap geral e dos itens estruturais da linha `0.0.2`.
3. `GRAFX_COMPLEMENTARY_EVOLUTION_PLAN_CODEX.md` continua governando capabilities, formatos e ecossistema.
4. `AGENT_FIRST_EVOLUTION_PLAN_CODEX.md` continua governando a camada agent-first.
5. `FABLE_PERFORMANCE_GRAFX.md` é a fonte extensa de hipóteses e medições exploratórias; em conflito com este documento, prevalece este plano para a rodada corrente.

### 2.3 Evidência já disponível — e seu limite

Durante uma janela observada do backfill real, um único card permaneceu por muitos minutos sem crescimento dos arquivos amostrados, enquanto o CPU do worker avançou aproximadamente um segundo por segundo e RSS/private ficaram em um platô. Isso é compatível com trabalho CPU-bound no caminho de leitura e com custo que cresce com o grafo.

Essa observação **não identifica** H1, H2, H5, H7 ou outra causa isoladamente. Memória estável nessa janela exclui apenas crescimento contínuo naquele intervalo; não exclui uma estrutura grande já materializada. O processo vivo não será perfilado nem terá o banco aberto por ferramentas desta rodada. Contadores do sistema operacional (`cpu_times`, RSS e private bytes) podem continuar sendo lidos, pois não abrem o store nem injetam código.

Os números `[MEDIDO-micro]` do FABLE foram obtidos sob carga concorrente e por scripts que ainda não estão versionados nesta árvore. Eles valem como evidência exploratória e ordem de grandeza, não como baseline canônico ou critério de promoção.

A revisão final do FABLE reconciliada após o consenso Nexus tem `229482` bytes e SHA-256 `0a9c004fc81a95b61cc81c53038f0b015a269654b9ac35795c7f0098643af8d9`. Esse hash identifica exatamente a fonte analisada por este plano; alterações posteriores exigem novo confronto explícito, sem ampliar silenciosamente o escopo desta rodada.

## 3. Invariantes não negociáveis

Nenhuma otimização pode:

- reduzir o modelo multiwriter/multireader ou introduzir modo single-writer como correção;
- retirar ou deslocar validações OCC, lease/epoch, WAL barrier, apply, publish ou recovery;
- alterar a serialização por board do Pulse;
- transformar erro, corrupção ou incerteza em resposta parcial;
- enfraquecer torn-read, checksum, `page_lsn`, stale/rebuild ou certificados pré/pós-leitura;
- criar cache ilimitado ou fora de budget;
- mudar resultados, ordenação, tie-break, precisão, recall ou tipos públicos;
- alterar formato on-disk ou capability sem ADR, compatibilidade de frota e autorização aplicável;
- executar profiling invasivo no backfill vivo.

A formulação correta da imagem de página é: a imagem WAL produzida pelo commit preserva o estado semântico e o `page_lsn`; ao persistir, `_write_back` publica uma nova `seq` física e recalcula o CRC. Portanto, **WAL e bytes finais do disco não são byte-idênticos**. Testes de D-09 devem comparar o novo e o antigo gerador da imagem WAL antes do flush.

## 4. Correções adversariais incorporadas

| Tema | Achado | Decisão final |
|---|---|---|
| Coerência do FABLE | a revisão `0a9c004f…` limpou as divergências centrais apontadas no Nexus, mas o relatório extenso ainda conserva propostas históricas não operativas — por exemplo, a ordem D-31 e a causalidade de truncamento em D-34, corrigidas abaixo | este documento é a especificação única da rodada e prevalece integralmente; o FABLE fornece investigação e proveniência |
| Baseline | `ead05a4` não admite sozinho todo o corpus Pulse atual | benchmarkar o SHA de integração promovido a partir de `c5ab19d`; `strict` e `generation` sempre separados |
| H5 / attach | attach genérico não arma `_completed_rebuild_through`; a cerca extra existe apenas no handle que concluiu rebuild | reproduzir `attach + rebuild + commit em outra tabela` e distinguir os três `index_view_unavailable`; corrigir somente se reproduzido |
| D-18 | memo exato mede reutilização/cache, não custo de codec nativo | usar apenas como experimento de cota, nunca como gate de release ou prova suficiente para abandonar/autorizar NT-1 |
| D-02 | inverter `HeapStore.lookup` pode mudar qual versão é retornada em estado de duplicidade/corrupção | manter `lookup` e ordem de scan intactos; criar locator especializado para endpoint |
| D-03 | `lookup` não retorna `RecordRef`; R2 copiava `(ref, header, content)` de toda a tabela sem budget | introduzir porta interna com ref; usar R1 deduplicado; R2 somente se limitado por bytes, admitido e com fallback |
| D-05 | slots overflow contêm ponteiro, não payload; decode preguiçoso também pode esconder corrupção que hoje é recusada | reconstruir overflow e preservar paridade de recusas inclusive em linha não incidente; caso contrário, não implementar |
| D-08 | 64 buckets fixos dão `O(N/64)`, não `O(1)`; `encode_value(INT64)` não cobre `RecordId` `u64` | rejeitar D-08 como escrita; eventual P2-ID exige rehash/bucket sizing ou unicidade real e encoding `u64` canônico |
| D-10 | conjunto de dirty candidates estava sem invariante de pin/unpin/doomed/eviction | só implementar após desenho formal e equivalência com o scan integral |
| D-26 | o denominador multiwriter não é apenas `COMMIT_SECTION`; a writer lease exclusiva cobre uma janela maior | medir wait/hold da lease e wait/hold da commit section, além das fases internas |
| D-11 | recovery valida uma página quatro vezes no caminho observado, não três | qualquer token deve reduzir inicialmente 4→3 e ser ligado ao conteúdo e ao subplano exatos; item deferido salvo evidência |
| D-28 | `verify` normal pode abrir recovery e o verifier não cobre WAL | separar verify explicitamente read-only e byte-preserving de recovery/WAL em cópia descartável |
| D-15 | certificado de page 0 não autentica um cache HNSW em disco | retirar cache persistido da rodada; futuro formato exige UUID, geração, algoritmo, matemática, checksum, quota e publicação atômica |
| D-14/D-16 | NumPy/lote e SQ8 podem alterar ranking/precisão | backlog opt-in, sujeito a autorização; nunca default desta rodada |
| D-30..D-34 | API pública, capabilities, FTS, heap v2 e WAL comprimido ampliam produto/formato | permanecem nos roadmaps `0.0.2`, fora desta rodada |

## 5. Evidência direta no código

As referências desta seção são da base `ead05a4`; linhas devem ser recalculadas no SHA promovido antes do primeiro patch.

- `HeapStore.require_endpoints` chama `lookup` para as duas pontas; `lookup` percorre `_walk` e retorna o primeiro match visível: `src/okto_grafx/engine/heap_store.py:555-600` e `:1205-1217`.
- `_walk` constrói `RecordHeader` para todo slot antes de aplicar o predicado: `heap_store.py:1870-1920`; `RecordHeader.decode` faz length check, `Struct.unpack_from` e dataclass: `domain/model/record.py:150-171`.
- `_owner_nodes` escaneia e decodifica toda a tabela para formar o mapa de pouso: `src/okto_grafx/engine/query_engine.py:4028-4068`.
- `IndexManager.validated` lê e decodifica a versão no heap, mas devolve apenas refs; `_index_seek` e `by_index` leem novamente: `index_manager.py:4354-4393` e `query_engine.py:2124-2127,2244-2257`.
- `_payload_of` demonstra que registros overflow precisam seguir a cadeia antes de acessar o payload: `heap_store.py:1962-1970`.
- `flush`, `modified_pages` e `has_dirty_pages` percorrem frames residentes: `buffer_pool.py:722-786`.
- a writer lease é adquirida em `txn_manager.py:2325` e liberada em `:2587-2601`; `COMMIT_SECTION` começa em `:2330`; sinks só podem ser chamados depois de `:2607`.
- `_write_back` altera a geração física/CRC da página: `buffer_pool.py:1490-1554`; `apply_page_image` materializa a imagem lógica: `:1885-1927`.
- o índice hash usa 64 buckets por default e percorre a cadeia do bucket: `domain/index/keys.py:49` e `index_manager.py:2223-2230,2259-2270,2554-2598`.
- `RecordId` cabe em `u64`, enquanto o codec público `INT64` é assinado: `heap_store.py:237-256` e `domain/model/value.py:375-383`.
- o build vetorial ainda executa um walk integral. D-12 agora faz `live_count()` caminhar apenas
  quando não há contagem derivada certificada; hits quentes continuam passando pela cerca de page 0,
  mas não decodificam novamente todas as entradas. A presença do build no backfill não foi demonstrada.

## 6. Ordem fixa de execução

### P0 — diagnóstico, baseline e reprodutibilidade

P0 pode produzir somente instrumentos, artefatos e a correção H5 caso o defeito seja reproduzido. Nenhuma medição abre o board original.

#### P0.0 — congelar a base

1. promover ou criar branch diretamente de `c5ab19d`;
2. registrar SHAs Grafx/Pulse, Python, SO/filesystem, dependências, `page_size`, buffer budget, checksum e `descriptor_revalidation`;
3. confirmar que os valores do corpus Pulse admitidos por `58e2e7d` continuam cobertos;
4. proibir benchmark a partir de `4c474b5`.

#### P0.1 — reproduzir ou falsificar H5

Em banco isolado:

1. processo A cria e fecha schema/índices;
2. processo B apenas anexa, commita em A e consulta índice de B;
3. processo B anexa, conclui rebuild, commita em tabela não tocada e consulta o índice reconstruído;
4. registrar mensagem/details para cada site: flag stale durável, `_completed_rebuild_through` e cobertura insuficiente do header;
5. registrar `rows_seeked`, `rows_scanned`, `stale_indexes` e número de retries.

Se reproduzido, H5 vira correção de disponibilidade P0 e deve fechar seus testes de qualidade antes de P1. Reabrir handle pode ser braço diagnóstico, nunca correção. Se não reproduzido, publicar a falsificação e encerrar a hipótese.

#### P0.2 — versionar os instrumentos

Trazer para a árvore somente os scripts realmente usados, com seed, parâmetros, formato de saída e hashes. Raw outputs devem registrar proveniência e não ser confundidos com resultados oficiais quando a máquina estiver carregada.

#### P0.3 — perfil e censo pós-drain

Depois de o backfill terminar:

- copiar o board e provar o hash/inventário da origem e da cópia;
- reproduzir um card longo na cópia;
- usar `py-spy record` sem `--locals`, mais contadores do SO e `QueryResult.statistics`;
- contar `_walk`, headers, tuples, heap reads, scans, seeks, endpoint lookups, statements e transações;
- medir distribuição de distância do acerto à cauda, versões vivas/mortas e presença de busca/build vetorial;
- nunca anexar profiler ao worker de produção.

Estado de implementação: os instrumentos estão prontos em `perf/v002-p0-census@222a854`, mas esta
etapa só termina quando forem executados sobre uma cópia declarada e estável após o drain. O replay
instrumentado mede dinamicamente a localidade dos acertos de endpoint e chamadas de busca/rebuild
vetorial. O censo separado é estático, read-only e sem timing: “dead” não significa vacuum-safe, e
presença de catálogo vetorial não significa atividade. O profiler aceita somente o PID do filho que
ele próprio criou e usa READY/GO antes da operação; seus deltas de CPU/I/O partem do snapshot
pré-GO, enquanto o perfil inclui também pós-validação e fechamento, ambos explicitamente rotulados.

#### P0.4 — baseline same-code

- mínimo de três execuções independentes por modo, com warmup descartado;
- uma quarta execução somente se a dispersão tornar o resultado inconclusivo;
- `strict` e `generation` são séries separadas;
- máquina sem carga concorrente relevante;
- registrar throughput, p50/p90/p99, RSS/private, conflitos, bytes WAL e write amplification;
- publicar faixa de ruído/intervalo, não apenas soma de medianas.

P0 não é um gate de release. Ele fixa a evidência com que cada otimização será mantida ou revertida.

### P1 — lote Python puro, sem formato e sem protocolo novo

#### P1.1 — instrumentação das janelas exclusivas (D-26 corrigido)

Adicionar métricas locais e de cardinalidade fechada para:

- espera e duração de writer lease;
- espera e duração de `COMMIT_SECTION`;
- fases internas de OCC, materialização, geração de records, append, barrier, apply, flush, index e publish;
- páginas logadas, bytes WAL, frames examinados, flushes, foreign commits e retargets.

Nenhum sink/callback roda sob lease ou section; a emissão ocorre após `txn_manager.py:2607`. O aceite exige cobertura reconciliada da janela medida, overhead desabilitado praticamente nulo e probe effect quantificado quando habilitado.

#### P1.2 — header peek sem dataclass para versões rejeitadas (D-01)

- usar um único `_HEADER_STRUCT.unpack_from` e extrair `record_id/xmin/xmax`;
- manter a recusa de comprimento antes do unpack;
- construir `RecordHeader` completo para toda versão aceita;
- provar offsets por mutante e paridade de erros.

#### P1.3 — uma leitura por acerto de índice (D-04)

- adicionar porta interna que devolva `(RecordRef, HeapVersion)` validado dentro da mesma stable view;
- manter a API pública existente de refs;
- consumir a versão já validada em `_index_seek` e `by_index`;
- reaplicar corretamente `ended`/`changed`/owner overlay.

#### P1.4 — locator especializado para endpoint (D-02 corrigido)

- não alterar `HeapStore.lookup` nem a ordem pública de scan;
- introduzir `_lookup_with_ref` somente para os consumidores internos necessários;
- caminhar cabeça→cauda por um cursor de prefixo canônico, concluindo o `peek` de toda a página antes de devolver um candidato;
- revalidar e decodificar integralmente apenas o candidato solicitado; corrupção nunca vira fallback;
- limitar todo estado retido por handle — memo, slot de tabela, identidade/ref e prova de páginas — com accounting atômico sob guard injetado, sem I/O do heap sob esse guard;
- usar o lookup canônico em saturação, estado derivado stale ou uso concorrente do mesmo cursor;
- incluir IDs antigos, snapshots antigos, updates, duplicidade/corrupção e o mesmo reader antes/depois de reconciliação multiprocesso nos testes.

#### P1.5 — pouso preguiçoso e limitado (D-03 corrigido)

- regime R1 por identidade usando `lookup_with_ref`, com deduplicação;
- reproduzir exatamente overlays `ended`, `changed`, `inserted` e `PendingRowRef`;
- avaliar um limiar de troca para scan somente se houver evidência e um R2 integralmente admitido;
- proibir cache global ou por transação sem limite;
- qualquer R2 deve cobrar bytes, ter quota, admissão, evicção/fallback e só avançar após `tracemalloc`/RSS demonstrarem segurança.

**Disposição executada:** R1 mantido; limiar/R2 não selecionado. `1e2997e` + `13a5bda`
integram a resolução por identidade sobre a porta D-02, deduplicam pousos e limitam, por handle,
memo de transação, slot de tabela, view, overlays, fingerprint e resultados a 32 MiB/131.072
entradas. A revisão recusou a primeira versão porque o histórico bruto do fingerprint e o
`_Context` do statement podiam ficar retidos sem cobrança; o hardening passou a cobrar cada item
do histórico e seu payload antes da publicação e a fornecer o contexto somente por chamada.
`DELETE` participa do fingerprint e paga a carga fixa por entrada, mas seu tuple vazio é marcador
de ausência de payload e não passa por `encode_tuple`; o teste mantém a view admitida, reutiliza o
cache sem novo decode e os testes de settlement devolvem toda a quota. Dentro das quotas D-02 +
D-03 o custo repetido é amortizado para `O(N+E)`; se ambas saturarem, permanece explicitamente o
fallback de até um lookup canônico `O(N)` por identidade distinta. Introduzir R2 sem evidência
recriaria a materialização integral e ampliaria a superfície de memória, portanto não faz parte
do fechamento de P1.

#### P1.6 — endpoint decode parcial, condicional (D-05 corrigido)

A condição original era P0 mostrar esse caminho em pelo menos 10% da parede do card observado.
Ela foi supersedida pela decisão posterior do usuário de retirar gates de performance e promover
somente por qualidade; não se alega que o censo real tenha ocorrido. A implementação deve:

- reconstruir payload overflow antes de ler offsets;
- validar schema, comprimento, tags e estrutura também para linhas não incidentes, preservando a recusa atual;
- manter paridade para inline, overflow multipágina, cadeia truncada, payload/tag inválidos, self-loop e overlays;
- publicar a evidência direcional sem transformar ausência de ganho temporal conclusivo em gate.

**Disposição executada:** mantido em `bd12a5c`. O payload de toda relação visível continua sendo
reconstruído e validado integralmente, inclusive propriedades de linhas não incidentes; somente os
dois endpoints são retidos e `_incident_edges` deixa de construir o `HeapVersion` e o tuple de
propriedades completos. Duas revisões adversariais passaram, incluindo paridade de recusas,
overflow, ordem de validação, overlays e corrupção não incidente. O microbench curto no SHA
integrado mediu `107452,15 -> 78707,15 ns/linha` (`1,365x`) no decoder sintético de 6.321 bytes.
É evidência direcional e dependente do mix de propriedades, não ganho end-to-end nem mudança
assintótica: `DETACH DELETE` continua `O(|R| + bytes dos payloads visíveis)`.

#### P1.7 — gerar a imagem WAL uma vez, condicional (D-09 corrigido)

A condição original exigia que P1.1 demonstrasse materialidade na writer lease; esse gate temporal
foi supersedido pela decisão posterior de promover por qualidade e evidência direcional. O novo
gerador ainda deve ser byte-idêntico ao gerador antigo **antes do flush**, inclusive padding, slots,
flags e `page_lsn`; imagens externas pré-staged continuam passando pelo caminho de validação
integral.

Branches de P1.2 e P1.3 podem ser desenvolvidas em paralelo após P0.0, sem usar o worker ou o store
vivo. A ordem original condicionava a promoção a P0.1–P0.4; a decisão explícita posterior de retirar
os gates de performance a supersedeu, mantendo P0.3/P0.4 como evidência e exigindo os gates de
qualidade antes da promoção. H5 permaneceu obrigatório e foi fechado. P1.4 depende de P1.2; P1.5
depende da porta de P1.4. As condições temporais de P1.6 sobre o censo real e de P1.7 sobre a
materialidade medida por P1.1 também foram supersedidas; seus contratos técnicos e gates de
qualidade permaneceram integrais.

### P2 — no máximo uma solução estrutural

Depois de P1, selecionar exatamente uma opção abaixo, ou selecionar “nenhuma”. A seleção não reabre P1 nem adiciona outro eixo.

| Opção | Quando pode vencer | Pré-condições obrigatórias |
|---|---|---|
| **P2-ID — access path de identidade redesenhado** | endpoint/landing lookup continuar em ≥25% da parede e baixa localidade tornar D-02 insuficiente | integrar P1.12 do roadmap: bucket sizing/rehash ou estrutura de unicidade real; chave `u64`; escopo real de tabelas; freshness/rebuild/frota; medir write amplification e p99 multiwriter |
| **P2-DIRTY — dirty candidates (D-10)** | tempo medido de enumeração de frames continuar material na writer lease | conjunto formal cobre pins atuais, dirty despinadas, multi-pin, `_doomed`, `_modified`, apply, invalidate, eviction e write-back; toda candidata é revalidada; oracle integral e mutantes |
| **P2-VAC — vacuum/compaction** | versões mortas dominarem os headers/tuples processados | cumprir primeiro os watermarks/horizons e a segurança de reader antigo já exigidos pelo plano principal; nenhuma remoção sem prova de invisibilidade |
| **Nenhuma** | nenhum residual superar a faixa de ruído ou as pré-condições não estiverem prontas | registrar o resultado e encerrar a rodada sem inventar nova meta |

D-08 original não é uma opção: com 64 buckets continua `O(N/64)` e sua chave assinada não cobre o domínio de `RecordId`. P2-ID é a substituição tecnicamente válida. Qualquer variante que altere formato/capability ou as premissas de concorrência exige ADR e autorização antes do código.

**Seleção final de P2: nenhuma.** Não existe evidência pós-P1 dos gatilhos congelados: P2-ID não
demonstrou lookup/landing em pelo menos 25% da parede com baixa localidade; P2-DIRTY não demonstrou
enumeração material de frames dentro da writer lease; e P2-VAC não possui censo pós-P1 mostrando
predominância de versões mortas nem suas pré-condições de segurança. Implementar uma opção
estrutural agora seria trabalhar por hipótese, não por evidência.

## 7. Política de medição e decisão

Performance não é gate de release nesta rodada. Por decisão explícita posterior do usuário,
throughput, latência, RSS e comparações same-code orientam diagnóstico e a futura seleção de P2,
mas não bloqueiam promoção. A regra provisória anterior de reverter automaticamente um patch por
não superar a faixa de ruído está supersedida. Uma alteração P1 é mantida quando todos os gates de
qualidade passam, sua direção de ganho é tecnicamente demonstrada e não há regressão funcional,
de integridade, recovery ou concorrência. Evidência temporal curta nunca é extrapolada para o
fluxo completo; uma regressão material observada continua exigindo correção ou reversão.

Regras de benchmark:

- instrumentos e raw outputs versionados;
- SHA/configuração/ambiente completos;
- RAW nunca comparado a corrida instrumentada;
- cold e warm identificados;
- sem asserts de duração na suíte funcional;
- comparação por distribuição, não por uma única média;
- número de testes vem do collection manifest do SHA, não de constante copiada em documento.

## 8. Gates de qualidade obrigatórios

Testes focados rodam por item. Suítes longas podem ser acumuladas após P1.2–P1.5, conforme a política de eficiência já acordada, mas devem passar antes do milestone e novamente após P2.

1. testes focados de storage/index/query e regressões novas;
2. oráculo antigo × novo para resultados, refs, overlays e erros tipados;
3. corrupção: mesma classe, mensagem e `details` nas superfícies alteradas;
4. `verify --read-only` frio, com todos os arquivos preexistentes e bytes duráveis inalterados; o
   inventário bruto antes/depois continua publicado e só pode divergir pela criação de exatamente
   um `<grafo autenticado>/control/txn-<8hex>.lock` correspondente à participant section capturada
   do próprio handle, arquivo regular de 0 B e SHA-256 vazio — ou por delta zero quando esse lock já
   existia vazio; qualquer remoção, alteração ou outro acréscimo falha fechado;
5. recovery e danos de WAL apenas em cópias descartáveis, com arquivos mutáveis declarados;
6. crash/fault injection e mutantes para D-09, D-10 ou qualquer mudança em recovery;
7. multiprocesso com writers/readers, zero torn read, duplicata, phantom ou row perdida, e total 500/500 no instrumento atual;
8. exact-view/stale/rebuild em dois processos para D-02, H5 e P2-ID;
9. `strict` e `generation` cobertos separadamente;
10. full suite, lint, type/format checks aplicáveis e `verify` após cold reopen; neste fechamento, a
    única falha da corrida ampla foi uma docstring e a composição explícita `9.628/17/1 + 66/66`
    após a correção neutra substitui uma repetição integral — nenhuma falha comportamental recebe
    essa exceção;

## 9. Rastreabilidade das direções D-01..D-37

| Direção FABLE | Destino nesta rodada |
|---|---|
| D-01 | P1.2, aceita com unpack único e paridade de recusas |
| D-02 | P1.4, redesenhada como locator especializado; `lookup` genérico intocado |
| D-03 | P1.5, R1 com ref/dedupe promovido; limiar/R2 não selecionado sem evidência e admissão completa |
| D-04 | P1.3, aceita e prioritária |
| D-05 | P1.6 promovida após retirada do gate temporal; overflow-safe, fail-closed e ainda `O(|R|)` |
| D-06 | fora do lote; apenas passar extent já calculado pode voltar se medido; memo só seria admissível em `generation` |
| D-07 | deferida; cache/zone map derivado requer budget e prova de invalidação |
| D-08 | rejeitada como escrita; substituída por P2-ID integrado a rehash e chave `u64` |
| D-09 | P1.7 promovida; diferencial imagem WAL antiga × nova byte-idêntico antes do flush, com cache restrito à tentativa |
| D-10 | candidata P2-DIRTY após P1.1 e desenho formal |
| D-11 | deferida; corrigir contagem 4→3 e autoridade do subplano antes de repropor |
| D-12 | promovida sem formato: cardinalidade derivada cercada, exata após primeira leitura e incremental em `(key, ref)` |
| D-13 | promovida na variante HNSW/NumPy estreita em `eebc497`: bytes f32/f64 imutáveis por nó; Pure e APIs públicas permanecem tuple |
| D-14 | lote rejeitado após medir entradas reais e expansão certificada; ganho otimista `1,70–2,18x` apenas em DOT não justifica a superfície semântica |
| D-15 | cache persistida removida; delta estrangeiro também fechado como NO-GO na arquitetura HNSW mutável atual, pois publicação atômica segura reintroduz `O(N)` e o patch in-place viola leitores concorrentes |
| D-16 | lane vetorial seguinte, condicionada a perfil/recall agrupado e sem dependência implícita do D-14 rejeitado |
| D-17 | promovida em `b7fb52d` + correção fail-closed `82ea61b`: walks privados de count/ref sem DTO, mantendo `walk()` autoritativo |
| D-18 | experimento de cota após P1, não gate nem escopo obrigatório |
| D-19..D-22 | lane NT-1 futura; só após call counts, microbench e microprotótipo ABI real; adapter por instância |
| D-23 | P0.3, somente pós-drain em cópia e sem `--locals` |
| D-24 | P0.3, aceita com corpus/instrumentos versionados |
| D-25 | P0.0/P0.4, baseline corrigido para o SHA de integração promovido e mínimo de três runs |
| D-26 | P1.1, expandida para lease + commit section + fases |
| D-27 | lane vetorial futura, com matriz maior e presença produtiva primeiro |
| D-28 | parcialmente incorporada aos gates; verify read-only separado de recovery/WAL |
| D-29 | dívida pré-native; memo do corpus precisa chave estável, pois `load_provider()` cria lambda nova por chamada |
| D-30 | avaliada e fechada como NO-GO no cursor atual: decode vetorial isolado chega a `20,98x`, mas ocupa só `6,4–12,1%` dos endpoints reais (`~1,07–1,11x` de teto); ganho material exige redesenhar a representação contígua, não uma API paralela orientada ao benchmark |
| D-31 | roadmap GX-CAP-0/1; ordem de capabilities deve respeitar commit metadata antes de temporal |
| D-32 | roadmap FTS; contrato semântico/analyzer/BM25/lifecycle antes do formato físico |
| D-33 | formato futuro; não se apoia na alegação falsa de D-08 `O(1)` |
| D-34 | posteriormente promovido no item 12 como full-page zlib v2: capability de catálogo + `format_version=2` fecham downgrade; tipos obrigatórios desconhecidos agora recusam também recycle/append sem mutação. Delta continua futuro; chunk físico e fisiológico são NO-GO no recorte |
| D-35..D-37 | aceitas como braços default de não construir native/formato/vetor sem evidência |

## 10. Itens que não foram perdidos

Os seguintes itens permanecem normativos no `EVOLUTION_PLAN_CODEX.md`, mas não entram automaticamente nesta rodada:

- budgets de query/transação e accounting de RSS;
- single-flight, locks e I/O por arquivo;
- rehash/rebuild e índices secundários;
- bulk ingest/`executemany` e group commit;
- top-N e acumuladores O(1);
- vacuum completo;
- HNSW ponta a ponta, `max_visits`, filtros, budget e recall;
- plan cache — continua hipótese a medir, não ideia refutada;
- capabilities, temporal, FTS, Arrow, backup/migração e WAL futuro;
- overhead e escalabilidade da camada agent-first.
- higiene futura de `control/txn-*.lock`: arquivos vazios podem acumular entre participantes;
  qualquer coleta exige ADR e provas multiprocesso contra split-lock ou reuso concorrente antes de
  alterar seu lifecycle.

P2 pode puxar somente o item estrutural vencedor descrito na seção 6. Todo o restante conserva seu proprietário e ordem nos roadmaps existentes.

## 11. Entregáveis e definição de concluído

Cada milestone deve produzir código/testes, atualização de `docs/PERFORMANCE.md` quando houver número canônico, changelog quando houver mudança observável e raw evidence com SHA/configuração.

A rodada termina quando:

1. H5 estiver reproduzido e corrigido, ou formalmente falsificado;
2. P0.0–P0.2 e P1 estiverem encerrados com cada item marcado `mantido`, `revertido` ou
   `não selecionado`; P0.3/P0.4 podem permanecer como evidência pós-backfill, não gate;
3. no máximo uma opção P2 tiver sido implementada e validada, ou “nenhuma” estiver registrada;
4. todos os gates de qualidade estiverem verdes, incluindo a composição explicitamente limitada e
   registrada no item 10 da seção 8;
5. instrumentos before/after permanecerem versionados e aptos a publicar performance, RSS,
   conflitos e write amplification sem misturar `strict`, `generation`, RAW ou instrumentado; as
   execuções reais P0.3/P0.4 podem permanecer evidência pós-backfill e não gate, conforme a decisão
   explícita posterior registrada na seção 7;
6. não restar blocker de integridade, recovery ou disponibilidade descoberto pela rodada;
7. itens deferidos continuarem nos roadmaps proprietários, sem serem promovidos implicitamente para o escopo corrente.

## 12. Registro do consenso Nexus

- entrega original do FABLE: `msg_3b7729af534946248d0640e671f1a797` / `art_1a99f018b5894869a08dd65e6a4f8aa7`;
- primeira revisão adversarial Codex: `msg_921d741883114305bcbd64fbe38139a5`;
- resposta e primeira convergência Claude: `msg_38633cbfc9574d96a44fb1f6c60fa1c8`;
- correções finais Codex sobre lease, D-08, overflow, WAL, verify e reprodutibilidade: `msg_6db7f10ad7fa4a498049f53c508b969e`;
- confirmação final do Claude após verificação no código: `msg_10cb893a9d0c40a3b764fc757194892e`;
- encerramento do consenso e congelamento do escopo pelo Codex: `msg_fdd6df6455e14146990d6af7fd778952`;
- ressalva final do Codex sobre trechos legados ainda contraditórios no FABLE: `msg_b9cb89e778254362bdea25838eac8b09`;
- confirmação de precedência e limpeza editorial final do FABLE pelo Claude: `msg_4458baf9ef744818b5500b9eb4dc3283`.

O consenso não autoriza mudança de formato, redução das garantias concorrentes ou profiling do store vivo. Esses pontos permanecem fora desta rodada até decisão explícita.

## 13. Rastreabilidade da execução

| Data | Marco | Estado | Evidência |
|---|---|---|---|
| 2026-09-02 | P0.0 — branch e ambiente da rodada | concluído | `feature/v0.0.2` criada diretamente de `c5ab19d874e59962ba1b66eaa7ab682d1e8b7fac`; ambiente/configuração em `docs/PERFORMANCE_ROUND_0_0_2.md` |
| 2026-09-02 | versão de desenvolvimento | concluído | `pyproject.toml`, pacote e README em `0.0.2`; testes pontuais de packaging/CLI verdes |
| 2026-09-03 | P0.1 — H5 e relevância para o Pulse | concluído | reprodução multiprocesso e correção de page 0 promovidas em `7a9414c`, `e8c3583`, `cef70db`; auditoria estática independente do Pulse `d50c034` confirmou que o backfill não chama `rebuild_vector_index` e não alcança a cerca process-local remanescente (`hof_5278e93d6f09409cbce04bbbcbe787f1`, verificada pelo Codex) |
| 2026-09-03 | P1.1 — D-26 | promovido | origem `perf/v002-d26-commit-metrics@5abfd396`; integrado em `7aa410d` e corrigido em `59020a4` para classificar completion estrangeiro como `other`, não OCC; focado 18/18 pós-integração, Ruff e diff-check verdes |
| 2026-09-03 | P1.2 — D-01 | promovido | origem `perf/v002-d01-header-peek@1239a0e`; integrado em `b23bcbc` + `07dfb02`; suíte focada de heap e Ruff verdes |
| 2026-09-03 | P1.3 — D-04 | promovido e limitado por cardinalidade | origem `perf/v002-d04-index-version@e898fe7`; integrado em `2e6bbf9` + `f6e7531` + `873f419` + `bc3ede4`; somente PK automática reutiliza a versão, enquanto endpoint/índice geral preserva leitura lazy e memória limitada; gates focados verdes |
| 2026-09-03 | P1.4 — D-02 | promovido e endurecido | o protótipo cauda-primeiro foi rejeitado por ocultar duplicata corrupta; `49a9b03` integrou o cursor incremental cabeça→cauda e `e26af74` fechou hard cap/atomicidade/settlement e os dois momentos do reader multiprocesso. `fb984a7` alinhou a documentação da carga conservadora. Isolado 16/16, bateria agrupada 60/60, pós-integração 35/35, probe concorrente, Ruff e diff-check verdes; dentro da quota o custo repetido passa de `O(E*N)` para `O(N+E)`, com fallback canônico honesto quando ela satura |
| 2026-09-03 | P1.5 — D-03 | R1 promovido e endurecido; limiar/R2 não selecionado | origem `perf/v002-d03-lazy-landing@e977285` + `7b152a9`; integrado em `1e2997e` + `13a5bda`. A revisão bloqueou fingerprint/contexto não cobrados antes da integração; o resultado final limita todo estado retido, mantém heap I/O fora do guard e trata `DELETE` como entrada fixa sem reencodar seu tuple vazio. 107 testes pós-integração, Ruff e diff-check verdes; 96 nós/9 pousos/2 identidades decodificam só 2 payloads e a repetição na transação decodifica zero |
| 2026-09-03 | P1.6 — D-05 | promovido após retirada explícita do gate temporal | origem `perf/v002-d05-incident-endpoints@7751ea1`; integrado em `bd12a5c`. 250 testes pós-integração, duas revisões adversariais, diferencial adicional de 44 mil payloads malformados, Ruff e diff-check verdes. O decoder integrado mediu `1,365x` no payload sintético de 6.321 bytes; ganho somente do componente, mantendo o scan `O(|R|)` e validação fail-closed integral |
| 2026-09-03 | lane vetorial — D-12 | promovido e endurecido | `df09c2e` integrou a contagem cercada; a revisão adversarial recusou deltas identificados apenas por `ref`; `6f6b410` alinhou a identidade a `(key, ref)` sem retirar o update incremental do HNSW e `16fbc0a` tornou falhas do cache derivado conservadoras, nunca falhas pós-barreira. Regressão integrada dos três arquivos afetados verde, Ruff e diff-check verdes; nenhum formato/WAL/protocolo de concorrência mudou |
| 2026-09-03 | P1.7 — D-09 | promovido e verificado | origem `perf/v002-d09-single-wal-image@765cd07` + `56e7883`; integrado em `c3ef29f` + `69368c3`. Cópia profunda da página local elimina um encode+decode verificado antes da imagem WAL; pré-staged externo preserva verificação integral; retarget reutiliza somente `(txn_id, csn)` exatos. Diferencial byte-idêntico, 6 mutantes mortos, 8/8 focados e 75/75 com commit/WAL vizinhos, Ruff e diff-check verdes. Microbench sintético carregado: p50 `5009→2031 µs/página` (`2,47x` nessa etapa), sem alegar a mesma razão para o commit completo |
| 2026-09-03 | P0.2 — instrumentos reproduzíveis | concluído; execução pós-drain permanece em P0.3/P0.4 | primitivas integradas em `c276dec`; driver autenticado integrado em `be286fa` + `2d43d75`, com origem imutável `perf/v002-p0-card-driver@e8a6be0`. Cada run usa clone integral descartável, pins separados de Community/Core, rota Grafx autenticada, lifecycle público de exatamente um card, oráculos de ACK/audit, RAW sem hooks e instrumentação bounded/reversível. `warm` é leitura sequencial provada, `mixed` é cache não controlado, `cold` é recusado; budget diferente dos 64 MiB realmente suportados também é recusado. Regressão focada 46/46, Ruff, `py_compile`, diff-check e duas auditorias adversariais PASS; smokes sintéticos RAW/instrumentado passaram, sem acesso ao board vivo e sem comparação inválida entre os dois modos |
| 2026-09-03 | P0.3 — perfil/censo | instrumentos concluídos; execução pós-drain pendente | `perf/v002-p0-census@222a854`: localidade dinâmica de endpoint e atividade vetorial no replay, profiler py-spy one-shot com READY/GO e contadores pré-GO, além de censo agregado read-only com inventário integral e reconciliação independente. Regressão combinada 98/98, Ruff, `py_compile`, diff-check e smokes sintéticos/isolados verdes; nenhum acesso ao board vivo |
| 2026-09-03 | P0.3 — hardening adversarial | promovido | `89cb893` removeu o limite de 100 mil hits por agregação exata `(table_id, page) -> peso` e adicionou preflight que recusa launchers/trampolines antes do attach py-spy; focado 28/28, Ruff, py_compile e diff-check verdes; nenhum dado Pulse acessado |
| 2026-09-03 | censo read-only frio | corrigido e verificado | `b445736` preserva todo arquivo preexistente e byte durável, admite somente o lock vazio da participant section no caminho exato do binding Grafx autenticado e publica os inventários brutos; schemas elevados a v2. Prova real descartável: um único lock novo, `41.944 -> 41.944` bytes e `verify("all")` limpo; 43/43 focados, Ruff lint/format, `py_compile`, diff-check e auditorias local/Nexus `hof_b0100393646349f095544abdd6137a58` PASS |
| 2026-09-03 | fechamento agrupado de P1 | concluído por composição explícita | a regressão ampla terminou com `9.628 passed, 17 skipped, 1 failed`; a única falha foi a ausência de docstring no callback aninhado `count`, corrigida sem mudança de comportamento em `8f0af84`; o lote afetado passou 66/66. A suíte completa não foi reexecutada após essa correção documental |
| 2026-09-03 | gate multiprocesso | concluído | 500/500 operações reconhecidas em 46,6 s, 510 registros incluindo dez seeds de contenção, 44 conflitos retryable absorvidos, zero perda, duplicata, phantom ou torn read e `verify("all")` limpo em live e reopen |
| 2026-09-03 | seleção finita de P2 | nenhuma | nenhuma das três opções atingiu seu gatilho congelado; a rodada encerra sem mudança estrutural por hipótese |
| 2026-09-03 | item 10 / P2-ID — projeção ACTIVE | concluído | `99622af` aplica a autoridade exata da geração ACTIVE a registry/planner/DML/redo/freshness/verifier/inventário, preserva os access paths custom v1 e limita count/staging por linha aos índices da própria tabela; gate agrupado 242/242, Ruff, compile e diff-check verdes, com duas revisões adversariais sem blocker |
| 2026-09-03 | item 10 / P2-ID — lifecycle de identidade | concluído | `0d353ae` leva o `record_id` durável por quota, INSERT/UPDATE/DELETE, lookup, rebuild e verifier, inclusive domínio u64 e DELETE sem reencode do intent vazio; gate agrupado 435/435, Ruff, compile e diff-check verdes, revisão adversarial sem blocker; WAL continua lógico por nome conforme o ADR |
| 2026-09-03 | item 10 / P2-ID — roteamento de endpoint | concluído | `01c496d` fixa por statement a escolha índice-versus-fallback, seleciona por projeção `O(K_t)`, usa `RecordId` u64 e heap validation, trata miss como definitivo e propaga falha após seleção; 26/26 testes de rota/locator, regressão relacional focada, Ruff, compile e diff-check verdes, revisão adversarial sem blocker |
| 2026-09-03 | item 10 / P2-ID — ativação física e DDL automático v2 | concluído | `ba8ca9a` implementa `ensure_identity_indexes()` explícito/idempotente, shadows nonced, escopo de endpoints, NODE/REL v2 atômicos, OCC integral sobre heaps lidos, barreira pré-catálogo para geração vazia e quota `max_index_build_entries` antes do primeiro `g_*`; casefold permanece scan-only e RID não é candidato genérico. Gate agrupado 367/367, slice final 5/5, Ruff/compile/diff-check verdes e duas revisões adversariais sem blocker |
| 2026-09-03 | item 10 / P2-ID — índices exatos customizados | concluído | `2fa81b1` entrega `CREATE INDEX`, `Database.create_index()`/facade de manutenção, chaves compostas ordenadas e sizing determinístico. Shadow custom e eventual migração v2 são um commit cercado; o receipt ACTIVE traz nonce/metadados/horizons certificados, equality preserva NULL e equivalência numérica via fallback pré-seek, e public views não executam descriptor hostil nem perdem vector/proximity. Gates 394/394 + 281/281, live/cold verify, Ruff/compile/diff-check e revisão adversarial sem blocker residual |
| 2026-09-03 | item 10 / P2-ID — rehash foreground growth-only | concluído | `72694bd` entrega `Database.rehash_index()`/facade, sizing estritamente crescente, rotação ACTIVE→STALE por shadow imutável, coativação v1 construída uma vez e recovery old-or-new completo. Handles long-lived adotam autoridade estrangeira no próximo read view; DML/checkpoint sem alteração real do catálogo não reabre todos os headers e geração selecionada ausente mantém falha fechada persistente. Gate focal 163/163, recovery, multiprocesso `strict`/`generation`, read-only, Ruff/compile/diff-check e duas revisões adversariais sem blocker |
| 2026-09-03 | exceção de qualidade — roster de métrica retida | corrigido | `0374dc9` inclui `oktografx_buffer_retained_estimate_bytes`, já emitida e congelada no contrato desde `283cffa`, no roster executável do teste storage-core. A correção é test-only; 11/11 focados mais o catálogo observability adjacente e Ruff passaram |
| 2026-09-03 | item 11 / P2-VAC — censo read-only | concluído; autorização destrutiva veio no marco seguinte | `db.maintenance.bloat(table=None)` mede headers numa observação sem pruning do horizonte limitado pelo checkpoint, com DTOs imutáveis, filtro por tabela e limites de accounting explícitos. O fresh view recusa dirty em vez de fazer flush, reader records TTL-stalled permanecem pins e nenhum WAL/página é escrito; `vacuum_safety_established=False`. Passou 196 testes focais mais testes de buffer/read-view/coordenação, Ruff, compile e diff-check. Auditoria concluiu que TTL não prova quiescência e que reuse sem incarnation cria ABA; o contrato mínimo proposto foi foreground/process-quiescent com floor global durável e foi explicitamente autorizado antes do código |
| 2026-09-04 | item 11 / P2-VAC — vacuum MVCC process-quiescent | concluído e publicado | `75e799f` entrega `maintenance.vacuum(...)`, capability requerida `heap_reclaim_v1`, floor global monotônico, recusa retryable de snapshots antigos, free/compact inline sem reuso de identidade, relink de cadeias e reconcile de todo índice ACTIVE no mesmo commit. Overflow, truncagem, reuse e modo online/background continuam excluídos. Fault injection nos pontos de WAL/heap/index/publicação e gates agrupados 490/490 + 736/736 passaram; quatro falhas da seleção transacional integral foram reproduzidas no baseline `6d3e62c` e nominadamente excluídas. Ruff/format/compile/diff-check verdes; revisão Nexus `hof_df837d11c96a430786856895c7f7c744` verificada PASS após correção adversarial do parecer |
| 2026-09-04 | item 12 / D-34 — full-page WAL zlib v2 | concluído e publicado | `24f2f63` entrega `WRITE_PAGE` full-image zlib1 sob capability `wal_record_v2`, ativada em transação v1 anterior. Lotes raw que fazem segment roll ficam v1 para preservar terminal CSN; `max_wal_batch_bytes` mede a forma final. Unknown v2 só é ignorado com flag exata `SKIPPABLE`; semântica obrigatória desconhecida recusa read/append/recycle/recovery sem mutação. Block-delta foi adiado; chunk físico e fisiológico são NO-GO neste protocolo. Gate agrupado 1.496/1.496 após quatro exclusões históricas nominadas, gates focais e checks estáticos verdes. Parecer de desenho rev. 3 `hof_44e3743219b2476a95ecc02d00ac0eb6` e revisão de implementação `hof_b192c61ffcea4297852c7b2b8f00064b` verificados PASS; contrato em `docs/architecture/WAL_PAGE_COMPRESSION_V1.md` |
| 2026-09-04 | item 13 / D-19 — page codec NumPy híbrido | concluído e publicado | `b720f7e` fecha os achados arquiteturais do gate; `d644dc3` adiciona `codec="numpy"` per-instance e byte-idêntico ao formato v1. Encode/decode vetorizam diretórios a partir de 16/96 slots; o oracle puro recebe páginas pequenas e toda recusa. Sem `auto`, migração, capability, cache global ou mudança de WAL/concorrência. Diferenciais cobrem 3.000 mutações em 4/8/32 KiB, faixa u16, cold reopen e receipts multi-DB; focado 556/556 e correção adjacente 661/661. A partição transacional ampla reteve somente as mesmas quatro falhas históricas do marco anterior. Micro final 200 slots/8 KiB: encode `3,06x`, decode `1,83x`, sem extrapolação end-to-end. Revisões `hof_fd26b1d96ce74dac80c9671e00a55999` e `hof_19b2cca000214564832d7ff15cf77618` verificadas PASS; contrato em `docs/architecture/NATIVE_PAGE_CODEC_V1.md` |
| 2026-09-04 | item 14 — hot paths limitados de query/heap/txn/vetor | concluído e publicado | `9603115` elimina trabalho fixo repetido: bootstrap sem `exists/page_count` redundante mas ainda valida o header residente; sync de catálogo/índice condicionado ao CE-3; uma projeção ACTIVE por statement; cache de parse/plano/public view por `Database` e limitado a 256/128/128; packing de slots, page count lazy e decode escalar preplanned; retenção de read view somente sob composição WAL-only; exact vector header-first, decode em bloco e `PreparedVectorMath` opcional. Sem mudança de formato, WAL, OCC ou concorrência. VEC medido: validação `2,59–3,19x`, HNSW build `1,77x`, busca `1,93–2,09x`, exact 10% visível `2,99x` e 100% `1,47x`; ranking idêntico. Payload invisível/filtrado deixa de ser decodificado nessa busca e sua corrupção passa a ser detectada por verifier/scan, sem relaxar a validação de linha admitida. Revisão `hof_47afe28f5f004696a473718eeca9d980`; 21/21 mutantes, gates focais e checks estáticos verdes |
| 2026-09-04 | lote de escala 1 — recovery/storage/heap | concluído e publicado | `c1d537e` + `9ee51f3` + `2be59c9` + `2afd876` + `210b685` + `c12e67c`: replay page-only coalescido apenas quando inequívoco, barreiras de dados antes da publicação, preflight de página reutilizado com vínculo de conteúdo/passagem e replay misto sequencial; leitura control local fundida; dirty candidates revalidados; slot de extent defensivo; teto lazy de descritores 256 com override. Evidência: flush limpo em 8.192 frames `3,99–5,24 ms -> 3,21–7,00 us`; extent em 200 tabelas `663,21 -> 107,22 us` (`6,19x`); 192 artefatos `384/320 -> 192/0` misses/evictions e `~2,06x` direcional. Formato, WAL, OCC e multiwriter/multireader preservados; regressões focais agrupadas e checks estáticos verdes |
| 2026-09-04 | lote de escala 1 — métricas e multiconjunto de índice | concluído e publicado | `8604661`: backlog de tombstones faz uma semeadura exata e deltas O(1), invalidando conservadoramente em reopen/rebase/falha; métricas off não caminham. As duas validações por `list.remove` viraram `Counter` e mantêm multiplicidade exata. Micro K=1.000 `291,53 -> 8,77 ms` (`~33x`); 73 testes independentes e Ruff verdes. No Pulse 0.3.3 instalado, `connect()` não recebe sink e mantém métricas Grafx noop, portanto INDEX-3 corrige escalabilidade de observabilidade mas não explica sozinho a carga Pulse atual |
| 2026-09-04 | lote de escala 1 — inventário de artefatos de índice | concluído e publicado | `592fd22`: definições ACTIVE relevantes são deduplicadas e o steady state com objeto registrado/definição exata não lista mais `index/`; qualquer geração ausente ou divergente materializa um único inventário canônico. A prova física fresca permanece antes do WAL e os casos v1 opcional/v2 obrigatório, nonce e definição continuam fail-closed. 9/9 focados e Ruff verdes |
| 2026-09-04 | lote de escala 2 — intents e fronteiras repetidas | concluído e publicado | `02e6f41` elimina a redução integral de PK por linha com fold incremental e rebuild canônico em rewrite/rollback (`67,16x` no trecho K=1.000); `e0e18f8` faz plano preparado sobreviver a adoção de catálogo byte-idêntico sem remover bytes/índices/dirty tables da chave; `855c9cf` memoiza projeção ACTIVE por autoridade; `3b72d4a` retém catálogo somente em same-token/own/CE-3 sem DDL; `82bd176` permite short-circuit de travessia 1-hop sob LIMIT por índice de endpoint, com blocking/stale/wider shapes no scan. Multiwriter/multireader, WAL, OCC e formato preservados; gates focais agrupados e checks estáticos verdes |
| 2026-09-04 | lote de escala 3 — build, recovery, commit e checkpoint | concluído e publicado | `c1f1a1f` materializa geração vazia com diretório first-fit efêmero e imagem física byte-idêntica (`2,05x–9,43x`, N=64–256); `b8eff8a` elimina o WAL walk exploratório em recovery nativo sem remover fallback dinâmico; `26baa86` agrupa stamps MVCC por página em `O(R+A)`; `46fa9fe` retém caches de topologia em commits apenas de conteúdo e invalida crescimento/relink/foreign view; `aaf72f2` reduz a observação de reader horizon de duas para uma por checkpoint. Foram acumulados gates focais de índice, recovery, transação, heap/redo e checkpoint; formato, WAL, OCC e multiwriter/multireader permanecem inalterados |
| 2026-09-04 | lote de escala 4 — VEC-4 / candidatos exatos selados | concluído e publicado | `c77d414`: certificado privado vinculado a owner/espaço/tabela/coluna/`read_lsn`, revalidação MVCC e autenticação `(key, ref)` por bucket; qualquer prova incompleta faz fallback integral antes do ranking. Caso focal: 4 leituras contra 64 e zero walk completo. Custo honesto `Θ(U·E/B)`, sem alegação O(K); corrupção fora do subconjunto visitado permanece no full scan/`verify`. 28 casos finais + 57 adjacentes e revisão adversarial verdes |
| 2026-09-04 | lote de escala 4 — CAT-5 / nonce registrado | concluído e publicado | `40552df`: gerações v2 reutilizam o nonce da definição imutável registrada; somente legado nonce-zero abre/valida header, preservando recusa de corrupção e colisão. Amostra do allocator N=64 `352→61 ms` (`5,8x`) e DDL `1.085→789 ms` (`1,37x`); 20 casos de allocator + 15 de schema/índice verdes |
| 2026-09-04 | EDGE / otimização transparente do cartesiano | dois protótipos recusados e revertidos | O pushdown `b236697` alterava erros, corrupção e budget; `030b39d` restaura o plano. O replay `bf8ed4e` media `~3,6x` em N=80, mas sob pressão de pool podia ocultar corrupção antes da segunda passagem e confirmar escrita, violando A63; `4bcdad0` o remove integralmente e congela recusa/zero persistência. O cliff `N+N²` permanece; o número do protótipo não é ganho entregue |
| 2026-09-04 | estabilidade / semântica de predicado em IndexSeek | concluído e publicado | `f5152de` recompõe a conjunção original sobre hits e recusa o seek quando termo observável antecede a igualdade. `5938a03` preserva o seek de padrões posteriores através de igualdades totais já ligadas, sem atravessar termo parcial. Presença do índice deixa de decidir se um residual isolado recusa; 135 focais, conjunto combinado após a correção e a fatia restante de `tests/query` verdes |
| 2026-09-04 | HNSW / ordem de construção e knobs | propriedade registrada; knobs adiados | cold build canônico e apply incremental podem produzir grafos aproximados diferentes para sequências de inserção diferentes, dentro do contrato atual. `ef_construction`/neighbours só serão considerados após perfil 8.192×384 e recall harness deliberado; não são gate desta rodada |
| 2026-09-04 | lote de escala 5 — TXN-1 / observação decodificada do WAL | protótipo recusado e removido antes de commit | O micro de nove runs media 754→250 decodes e `30,211→11,156 ms` (`2,71x`), mas a observação cobria somente o delta novo e não o prefixo físico exigido pela marca esparsa de `read_from`; podia ocultar dano antigo/quente. Também alterava o budget físico de `read_bounded`. O handoff `hof_0dc74badf8504e09af643bd9ae4357bd` confirmou NO-GO; reautenticar o plano consumiria o ganho, então o worktree voltou integralmente a `22d9694` |
| 2026-09-04 | lote de escala 5 — CAT-4 / inventário de diretório | cache por lifetime recusado; recorte efêmero não selecionado | Cache por `LocalStorageDevice` não enxerga publicação/remoção/corrupção de outro processo. A única foto admissível vive numa mesma `COMMIT_SECTION`, não cruza catalog apply e mantém a primeira caminhada O(N). Economia direcional estimada: ~4 ms em 16 entradas e ~73 ms em 4.096; no porte do Pulse, poucos ms e fora do steady state já corrigido por `592fd22`. Esforço médio e ganho baixo: nenhum código introduzido |
| 2026-09-04 | lote de escala 6 — D-17 / walks de índice sem DTO | concluído e publicado | `b7fb52d` migra somente contagem, refs de exact scan e métrica para leitores privados header-only; `walk()` integral continua autoritativo para verifier/reconcile/build. A revisão adversarial encontrou a faixa u64→48-bit omitida no contador; `82ea61b` compartilha a mesma validação de `RecordRef.decode` sem alocar DTO e congela corrupção nos bits altos. Micro final de 20 mil imagens: full/count/refs `166,87/33,20/65,98 ms` (`5,03x`/`2,53x`); diferencial de 40.010 imagens sem divergência e mutante discriminante; fatia focal de índice/vetor/concurrency/cross-process, Ruff e diff-check verdes; formato, WAL/OCC e concorrência inalterados |
| 2026-09-04 | lote de escala 7 — D-13H / componentes HNSW compactos | concluído e publicado | `eebc497` ativa bytes f32/f64 imutáveis somente no HNSW derivado com adapter NumPy explícito; views são readonly/efêmeras, `values_of()` e Pure continuam tuple, tombstones retêm bridge e REMOVE/rebuild liberam. Micro 128×384: residência `1,513→0,199 MiB` (`7,59x`), build `0,934→0,629 s` (`1,48x`), busca `7,764→6,368 ms` (`1,22x`), mesma shape/resposta. 121 testes independentes + 18 com NumPy real e revisão adversarial verdes; nenhuma mudança durável ou de concorrência |
| 2026-09-04 | lote de escala 8 — D-14 / scoring em lote | NO-GO fechado; nenhum código de produção | Exact real paga materialização tuple→matriz e mediu apenas `1,26–2,04x`; o `282x` anterior excluía `99,5%` do custo alcançável. Na expansão HNSW, scores não escalares mudariam beam/visited/break; o único desenho exato usa lote só para provar rejeições e escalariza toda entrada possível/incerta. Em 4.096×384, o teto otimista ficou `1,70x` (`ef=320`) a `2,18x` (`ef=64`), somente para DOT e antes do custo dos intervalos. Cosine/Euclidean não têm esse limite. Handoff adversarial `hof_1b20013e41154a00b21d65af4153af42` verificado PASS; D-14 foi encerrado sem alterar formato, WAL/OCC ou concorrência |
| 2026-09-04 | lote de escala 9 — D-15(b) / delta HNSW estrangeiro | NO-GO fechado; draft removido integralmente antes de commit | O intervalo CE-3 é íntegro, mas delta lógico vazio não autentica ausência de avanço esparso do header. `_note/_install` mutaria o snapshot HNSW já publicado durante leituras concorrentes; copy-on-write do grafo mutável volta a `O(N)` e elimina a vantagem `O(i)`. Reentrada do host `VectorMath` a partir de `begin` e divergência topológica permanente entre processos são superfícies adicionais. Revisão `hof_52f3f82fe7f34175ab704d40a9bbd1e8` verificada PASS; nenhum código/teste residual, formato, WAL/OCC ou regra multiwriter/multireader mudou |
| 2026-09-04 | lote de escala 10 — fronteira HNSW larga e trim | concluído em `f1d1a75`; revisão `hof_cad8d1a0ae734da19db0903334cc5c3c` verificada PASS | `_trim` troca insort repetido por score único + sort total + slice. Buscas com `N>=4096` usam heap somente quando filtradas ou exaustivas; o caminho aproximado comum continua legado. Fronteira seletiva: `82,668→13,916 ms` em 4.096 (`5,94x`) e `5.792,201→175,065 ms` em 50 mil (`33,09x`); build comum 512×64 melhora `1,074x`. Diferenciais cobrem shape, ties, callbacks, stats, limite 4095/4096 e dois saltos; resultado/recall, formato, WAL/OCC e multiwriter/multireader permanecem idênticos |
| 2026-09-04 | lote de escala 11 — D-30 / decode vetorial | NO-GO fechado; nenhum código de produção | O corpo isolado mede `20,98x`, mas representa só `6,4–7,4%` de `scan_rows_v1` e `8,6–12,1%` do exact frio; tetos end-to-end `~1,07x` e `~1,11x`, e cerca de `0,07%` do build HNSW. O custo dominante é ownership/materialização de rows/tuples. Sem redesenho contíguo integral, o patch seria marginal; API, integridade e concorrência ficaram intactas |
| 2026-09-04 | lote de escala 12 — projeção vetorial e escopo NumPy | concluído em `cd32623`; revisão `hof_c5ee0c49b2ec43beb0406f77e6f8eebf` verificada PASS | Somente resultado decodificado com tupla exata de floats exatos evita a revalidação Python por componente; input público, subclasses, `bool`/`int`/`Decimal` e tipos hostis mantêm o fallback integral. Projeção 2.048x384 `6,71x`; endpoint `scan_rows_v1` `2,11–2,87x`. A fusão de dois `numpy.errstate` consecutivos e equivalentes preserva ordem/recusas/warnings e acelera build HNSW 256x64 em `1,153x`. Gates focais `223 + 31`, Ruff, diff-check e auditoria adversarial verdes; formato, WAL/OCC e multiwriter/multireader inalterados |
| 2026-09-04 | lote de escala 13 — hot paths imutáveis compostos | concluído em `69f9cea`, `40e7514`, `60850b5` e `0fa3406`; detalhes em `docs/PERFORMANCE_ROUND_0_0_2.md` | `math.isfinite` elimina redispatch escalar (`1,195x` no build focal); o cold build vetorial consome headers e constrói cada DTO final uma vez (`2,503x` no walk); normalização automática por `TableDef` + memo de proveniência por manager corta chamadas `2.600→50` e reduz `~44%` do matcher no perfil de 50 tabelas; cache cosine por geração imutável mede score+norm atomicamente, invalida em REMOVE e fecha reentrada/race de publicação (`1,15–1,19x` no build e `1,350x` na busca repetida). Gates focais e revisão adversarial verdes; caches são process-locais/bounded e formato, WAL/OCC, durabilidade e multiwriter/multireader permanecem intactos |
| 2026-09-04 | lote de escala 14 — batching de cabeçalhos no replay lógico | concluído em `ae8d01e`; revisão Nexus `hof_e04db78c505a4ea0922fc19e8b278d5b` verificada PASS | Replay 100% lógico e comum lê/escreve page 0 no máximo uma vez por índice e mantém efeitos na ordem WAL. Mixed, RESET, rebuild, stale, índice desconhecido e subclasses com `apply` próprio caem integralmente no legado. A auditoria reproduziu e fechou reuso de plano que limpava STALE: a capability promovida é atômica, o estado é privado/imutável e a regressão preserva a geração posterior. Micro 500 efeitos `2,14x`; perfil público aponta checkpoint `~1,69–1,82x` e redo `~2,57x`. Foram aprovados 13 focais, 136 regressões agrupadas e 14 casos multiprocesso; formato, WAL/OCC, durabilidade e multiwriter/multireader não mudaram. A amortização da exact-view entre itens ficou NO-GO: teto inseguro `26,6%`, mas a forma segura mudaria lazy/callback/freshness |
| 2026-09-04 | lote de escala 15 — extent reservado e diretório de bucket quente | concluído em `da58471`, `d50eab2`, `90f0560`; revisão Nexus `hof_0b518924b36444a595988f6e0151a117` verificada PASS | A prova de extent vive apenas durante `insert_reserved` e cai no lookup canônico se o derived epoch mudar (`1,362x` em 500 inserts). Buckets com ao menos oito efeitos de replay são validados uma vez e usam diretório efêmero com tetos de buckets/alvos/páginas, mantendo `(key, ref)`, first-fit autoritativo na página, ordem WAL, contadores e stale-on-failure; micros de mesmo bucket `7,88x` em 250 e `38,75x` em 1.000 efeitos. A regressão encontrou e corrigiu a perda anterior da publicação page-0-last causada pelo set de dirty candidates, preservando `O(D)` sem scan de frames limpos. O perfil público de 500 CREATEs foi direcional: total `2,478 s`, checkpoint `0,477 s`, redo `0,228 s`. Gates focais, diferenciais aleatórios, fault injection, multiprocesso e fences passaram; formato, WAL/OCC, durabilidade e multiwriter/multireader não mudaram |
| 2026-09-04 | lote de escala 16 — materialização somente dos alvos no replay | concluído em `28dd5d4`; revisão adversarial independente PASS | O preflight continua validando todos os slots físicos em ordem, mas só constrói `IndexEntry` para o `(key, ref)` exato procurado. O lookup em dois níveis evita alocações para refs não relacionadas e também evita busca `O(alvos-por-ref)` quando chaves distintas compartilham o mesmo ref. Micro do scanner com 4.000 entradas/400 páginas/8 alvos: `42,092→15,227 ms` (`2,76x`). Passaram 24 discriminantes focais, 125 casos agrupados e checks estáticos; formato, WAL/OCC, durabilidade e multiwriter/multireader não mudaram |
| 2026-09-04 | lote de escala 17 — metadados imutáveis, travessia fundida e commit vivo em batch | concluído em `d47eaec`, `3657c47`, `5fc25f0`; revisões Nexus `hof_f9015f5ba1264be69404ed2427698444` e `hof_70a7620b5b35472aa889cde8faa22d52` PASS | Derivados imutáveis deixam de ser refeitos por operação; lookup fixa uma vez cada página do bucket; e a porta privada/revogável do `TransactionManager` prepara diretório efêmero somente para buckets vivos com ≥2 efeitos. A autorização é vinculada ao manager/txn/store exatos, expira antes do reset de contexto e declina para o escalar em chamada direta, hook customizado, RESET, rebuild, stale, retry ou teto excedido. O HNSW preserva seu `commit` externo e reutiliza apenas os hooks físicos herdados. Estimativas focais: `~1,70x` em batch distribuído maduro e `~9,16–21,04x` em colisão concentrada; 19 focais, 590 agrupados e 15 multiprocesso verdes. O baseline público anterior até `26df492`, que não inclui este lote, já media `1,53x` agregado. Formato, WAL/OCC, durabilidade e multiwriter/multireader permanecem intactos |
| 2026-09-04 | lote de escala 18 — dirty tables incrementais e sequências float exatas | concluído em `3c847ea`, `00299ed`, `787a464`, `3888830`; test drift em `ea11621`; revisões `hof_a2d2ceffff124c77a48fa9e5c9287cfa` e `hof_c1cfada9144b44d7a7d764fe2482b131` verificadas PASS | A descoberta de tabelas alteradas consome somente o sufixo novo da lista revisionada e faz rebuild integral em rewrite/rollback/replacement; iteráveis estrangeiros mantêm scan completo. A fronteira pública reutiliza tuple exato de floats exatos e destaca list exata uma vez, sem atravessar limites, subclasses ou componentes mistos. A auditoria encontrou e fechou um bypass no depth 64, inclusive pela API pública. Micro em memória de 500 itens `1,49x`, mas o perfil em storage limita esse trecho a `~2,2%` da escrita (`~1,02x` de teto isolado); snapshot float mede `17,96x` tuple e `9,58x` list no componente, família antes em `~7%` da escrita. Passaram 371 casos agrupados, 81 focais pós-correção e checks estáticos; nenhuma regra WAL/OCC, durabilidade ou multiwriter/multireader mudou. A prova de identidade por intervalo foi posteriormente encerrada como NO-GO no lote 21, sem criar gate ou capability |
| 2026-09-04 | lote de escala 19 — decisões heap sob pin único | concluído em `08c0197`, `526e882`; revisões `hof_627aebc2a59a4e7981052ef87eb8c1ae` e `hof_942f47dd6b06455fa4614b892587c777` verificadas PASS/GO | Append inline com hint já correto insere sob o pin que provou capacidade; drift e growth mantêm integralmente a ordem antiga. Extent read/write e reclaim-floor validam e usam page 0 num só pin quente, repetindo bootstrap + validação operacional após mudança de epoch; planners de imagem fresca continuam separados. Em commit público focal de 100 linhas, pins totais `1.176→876` (`-25,5%`) e heap page-0 `606→306` (`-49,5%`); componente reservado de 2.000 linhas foi direcionalmente `~1,42x`, sem diferença estável no endpoint wall-clock. Diferencial 720/720 e 369 regressões passaram; as varreduras de falha confirmam oito pontos restantes contra dez anteriores. Nenhum formato, WAL/OCC, durabilidade ou multiwriter/multireader mudou |
| 2026-09-04 | lote de escala 20 — extent observado e exact hit sem releitura | concluído em `51543a7`, `b9cfa10`; revisões adversariais independentes PASS | `insert` comum transporta o extent pós-floor sob prova de derived epoch capturada antes da observação e faz fallback em qualquer mudança; first extent e tail repair preservam o floor. Unicidade PK consome a versão já decodificada pelo certificado exact apenas com os dois hooks canônicos, mantendo overrides/doubles no legado. Em 100 linhas: pins `876→776`, heap page-0 `306→206`, `_find_extent 201→101`, `_extent_for 200→100`. Duplicata focal: heap reads `100→50`, mediana `7,204→6,636 ms`; não há ganho alegado para misses. O heap batch mais amplo foi NO-GO por teto agregado otimista `~1,034x` e risco desproporcional (`hof_2b85ac586d6042e78e0a973f86db45f4`). Formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 2026-09-04 | lote de escala 21 — reuso da identidade final no-follow | concluído em `b5cff4a`; revisão Nexus `hof_a701a6b052134462be310aeb1bc78b0e` verificada PASS | Cada revalidação de descritor reutiliza o `lstat` fresco do componente final e elimina o `stat(path)` imediatamente redundante. O walk completo de root/componentes e o `fstat` do descritor continuam; final/intermediário ausente retorna miss, redirects/junctions/reparse seguem fail-closed e o ramo sem `_paths` repete containment completo. Teste estrutural: `stat 1→0` por warm hit; nove discriminantes POSIX passaram no Ubuntu/WSL. Strict beneficia todo hit; generation somente seus mesmos boundaries. STOR-1 foi encerrado: 1.786 provas em 1.018 participant sections dão máximo `1,65x` no componente e `~1,02x` agregado, insuficiente para nova autoridade. Nenhum cache entre chamadas, formato, WAL/OCC, durabilidade ou premissa multiwriter/multireader mudou |
| 2026-09-04 | lote de escala 22 — quota única e scans quentes sem alocação redundante | concluído em `7b5ce1f`; revisão adversarial independente PASS | O manager canônico reutiliza no verificador de staging a mesma contagem que calculou o COMMIT LSN e ainda compara `produced != expected`; subclasses/doubles/overrides mantêm a segunda chamada legada. O bucket scan consome `iter_slot_views()` sob o mesmo pin e o diretório efêmero mantém `page/slot` na tupla sem clonar DTOs apenas para localização. Tetos medidos são cumulativos e modestos (`~1,015x` para a projeção removida e `~1,01x` para as alocações); 80 testes selecionados e checks estáticos passaram. Nenhuma regra de WAL/OCC, durabilidade, writer lease ou multiwriter/multireader mudou |
| 2026-09-04 | lote de escala 23 — contexto de pin sem gerador | concluído em `b964155`; revisão adversarial independente PASS após correções de lifetime | `BufferPool.pinned()` usa classe privada/slotted, mantendo aquisição lazy, dirty no exit, objeto original para doomed frame, BaseException/chaining, nesting, uso único e `ContextDecorator`. A auditoria impediu retenção do pool por contexto consumido e recriação após uso. Micro final 300 mil corpos vazios `0,563464→0,428061 s` (`1,316x` no componente); teto honesto do perfil público apenas `~1,006x` read/`~1,003x` write, sem abrir churn em outros context managers. 154 casos de buffer + 367 adjacentes passaram; formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 2026-09-04 | lote de escala 24 — scores transitórios no cold-build HNSW | concluído em `cdcf8c4`; revisões Nexus `hof_1fbc7385740a457cbed30c71ab3b6cd4` e `hof_7df9f6701ce04867aaeb773949f8c356` verificadas PASS | Adjacências cheias reutilizam scores alinhados e calculam somente o peer novo no overflow; remoção preserva o prefixo ou invalida conservadoramente. O cache existe apenas durante o build, é `O(E)` limitado, cai em `finally` antes da publicação/falha e só é autorizado pela classe concreta Pure/NumPy; subclasses/custom ficam no caminho canônico. Caminho público padrão forçado: mediana `9,45→6,09 s` (`1,55x`), respostas idênticas; abaixo do threshold 4.096 a busca exata não é afetada. 165 testes agrupados e diferenciais com churn passaram. Persistência HNSW e redução do threshold continuam não selecionadas; formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 2026-09-04 | lote de escala 25 — descritor participant desbloqueado no `executemany` | concluído em `429d192`; revisão adversarial independente PASS após correção fail-closed | Um batch durável mantém somente o fd do lock file aberto entre itens; preflight, cada item e settle ainda fazem acquire/release real do SO. Escopo é thread+nome, interno à operação pública, sem cobrir commit section/lease/WAL/OCC; memória e coordenadores alternativos usam fallback. A auditoria encontrou e fechou vazamento caso o wrapper Python falhasse após o acquire. Três itens: `os.open=1`, acquires/releases `5/5`, com outro thread admitido entre itens; micro conservador `~1,06x`. 572 testes de batch/coordenação passaram; formato, durabilidade e multiwriter/multireader intactos |
| 2026-09-04 | lote de escala 26 — scores HNSW alinhados sob adjacency underfull | concluído em `8602abf`; revisões independente e Nexus `hof_157e6798ef814a778409b1aa57b72549` PASS | Sentinelas posicionais preservam scores provados através de unlink/reinsert e só calculam peers novos quando um overflow realmente ocorre. Falha do scorer não publica refresh parcial; mismatch descarta o cache e usa scoring canônico completo. Em N=256/d64 Pure, chamadas de score `71.173→48.332` (`-32,1%`) sobre o lote 24, com ganho wall conservador `~1,20x`; 94% dos trims com overflow usaram cache. Topologia, scores hex, resultados e stats foram bit a bit sob Pure/NumPy e churn; 167 testes agrupados passaram. Cache continua transitório, bounded e descartado antes de publicação/falha; formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 2026-09-04 | lote de escala 27 — decode direto após tag VECTOR provada | concluído em `bed6846`; revisão adversarial independente PASS | O decoder de tuple já havia provado igualdade entre tag persistida e schema; F32/F64 entram diretamente no mesmo decoder de corpo e eliminam apenas a releitura/redispatch redundante. LIST/MAP e todas as guardas de header, body, offset e trailing payload permanecem. Diferencial adicional de 40 mil buffers preservou valor ou classe/mensagem/details/cause de corrupção; 485 regressões adjacentes passaram. Tupla mista mediu `1,042x` em d64 e `1,057x` em d384, ganho pequeno e constante sem novo gate. Não reabre o redesenho amplo recusado no lote 11; formato, integridade, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 2026-09-04 | lote de escala 28 — descritor participant revalidado entre statements | concluído em `764c546`; revisões local e Nexus `hof_8bdda5e6dda34172bf688bd1aa54f279` PASS | Uma transação local reutiliza somente o fd desbloqueado do participant lock; cada operação continua com acquire/release real. Todo borrow quente revalida identidade física e mismatch/erro fecha e reabre fail-closed. Commit, rollback, retry, OCC, pós-barreira, thread hop e close drenam convergentemente; memória/custom/subclasses não optam implicitamente. Em quatro statements mais commit/settle, opens `6→3` com acquires/releases `6/6`; amostra alternada de 1.000 PK seeks observou `1,124x`, registrada apenas como indicativa. 203 testes passaram e um skip POSIX declarado permaneceu; duas fixtures stale de `HEAD` foram corrigidas separadamente em `674e420`. Formato, WAL/OCC, durabilidade, writer lease e multiwriter/multireader intactos |
| 2026-09-05 | lote de escala 29 — clonagem defensiva de planos compilada | concluído em `c06463f`; revisão Nexus `hof_93cce5a9688049988570694a4588ecea` verificada PASS | Somente roots provadas como internas pelo engine exato compilam uma receita após snapshot e `validate_plan`; colaboradores externos preservam o caminho hostil integral. Cada retorno reconstrói todos os nós, schemas, caches derivados e literais mutáveis, enquanto a receita fica limitada ao LRU preexistente de 128 entradas. O componente representativo mediu `1,983x`; a consulta pública curta ficou em `~1,02x` ruidoso, registrado sem gate nem extrapolação. O auditor encontrou zero nós de gramática e zero mutáveis compartilhados. Foram aprovados 62 focais e slices adjacentes de 180/85 casos, além de Ruff/diff-check. Formato, WAL/OCC, locks, durabilidade e multiwriter/multireader intactos |
| 2026-09-05 | lote de escala 30 — publicação confiável de resultados e Protocols concretos exatos | concluído em `e572227`; revisão Nexus `hof_9016dfc426f5439c9d204f9792dea69d` verificada PASS | O engine exato e a fronteira pública, somente depois de normalização/snapshot integral, deixam de repetir a validação hostil de `QueryResult`; construtor público, colaboradores e wrappers continuam no caminho completo. `Snapshot`, `TransactionContext` e `WalRecord` exatos evitam reflexão de Protocol, enquanto subclasses/doubles preservam o fallback e a taxonomia. O quarto candidato foi recusado por ciclo de importação e ganho pontual. O auditor reconstruiu oito resultados reais pelo construtor público e confirmou imports/tipos em processos novos. Slice focal/adjacente 422/422 e checks estáticos verdes; leitura trivial indicou `~1,08x`, escrita apenas ganho pequeno/ruidoso, ambos sem gate. Formato, WAL/OCC, locks, durabilidade e multiwriter/multireader intactos |
| 2026-09-05 | lote de escala 31 — witnesses bounded da gramática de nomes exatos | concluído em `c228b0a`; auditoria independente GO, sem gate | Sucessos de strings built-in exatas usam LRU process-local de 512 entradas: `(label,value,limit)` para identificadores e `file` integral para nomes lógicos. Erros não são cacheados; limits diferentes, subclasses e hostis mantêm o caminho, a ordem e a taxonomia; o wrapper devolve o argumento corrente. Não foi criado cache redundante por segmento. Componentes indicaram `~4,6x`/`~13,7x`, mas o teto plausível no PK seek NTFS é `<1%`, registrado como ganho cumulativo. Suítes 26/26 e 147/147, Ruff/import/diff verdes. Nenhuma autoridade de path/descriptor, formato, WAL/OCC, lock, durabilidade ou premissa multiwriter/multireader mudou |
| 2026-09-05 | lote de escala 32 — um descritor desbloqueado por autocommit read | concluído em `1038ddb`; revisão Nexus `hof_6b1f9e6b31b54c5a97ebed76a2214a32` verificada PASS | `Database.execute` cerca `begin→execute→commit/rollback` com a capability já auditada e uma transição pública externa. Aberturas participant caem deterministicamente `4→1`, enquanto locks/unlocks reais permanecem `4/4`. Falha de commit só faz rollback se ACTIVE; rollback que deixa wrapper inalcançável sela e drena o facade. Scope custom não suprime primária nem a substitui por falha de exit. API 80/80, coordination 29 + 1 skip declarado e multiprocesso 5/5; checks estáticos verdes. Amostra temporal indicou `~1,11x`, sem gate; a prova promovida é estrutural. Duração de locks, formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 2026-09-05 | lote de escala 33 — sizing de novas gerações automáticas | concluído em `2dbc367`; revisão Nexus `hof_37ec8f454ada4a668678ebd943b5df6d` verificada PASS | `automatic_index_expected_cardinality` é uma dica opt-in por índice, limitada a 262.144 entradas esperadas e convertida no diretório power-of-two existente. Open gravável ativa somente catálogo v1 vazio; v1 não vazio mantém migração explícita e recusa ignorar a dica em novo DDL. Ativação v1→v2 e DDL v2 aplicam-na apenas a novas gerações PK/`ef_`/`et_`; `record_id` usa-a como piso da contagem cercada. Reopen preserva cardinalidade/buckets/nonce persistidos mesmo com outra configuração. Default, índices custom/vetoriais, formato, WAL/OCC e multiwriter/multireader não mudam. A sonda física confirmou 64→4.096 buckets com dica 200 mil e zero mutação no reopen legado. O teto ocupa 4.097 páginas por índice; dois nós keyed referenciados e uma relação somam seis artefatos (~192 MiB a páginas de 8 KiB), por isso oversizing é explicitamente desaconselhado enquanto walks forem `O(bucket_count + entries)` |
| 2026-09-05 | lote de escala 34 — reaproveitamento da prova imediata de raiz | concluído em `bce884c`; revisão Nexus `hof_8995dc6efa8848f0be519389b1a35a84` verificada PASS | A resolução de nome exato e a validação de pais deixam de repetir dois `lstat` idênticos e consecutivos da raiz antes do primeiro componente. O postcheck após a listagem e todos os brackets de diretórios intermediários permanecem. O atalho só vale para raiz, identidade capturada e prefixo vazio; um teste prova que hint indevido em pai intermediário continua executando ambos os checks. Um `exists` de dois componentes cai `7→6` `lstat`; sonda curta de cinco writes caiu `956→874` (`-8,6%`), evidência direcional sem gate temporal. Slice 55/55 coletado, com sete skips de plataforma declarados, Ruff e diff-check verdes. Case exato, recusa de redirects, descriptor revalidation, formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 2026-09-05 | lote de escala 35 — rehash assistido bounded | concluído em `8c13d0f`; revisão adversarial GO e Nexus `hof_0c6e83b53d60466397ea63cd6c453019` verificada PASS | A nova porta explícita prova a geração física e inspeciona somente os `B<=4096` head pages, sem decode, walk de overflow ou censo O(N). Ocupação `64*B` ou overflow físico configurado solicita um único degrau `B→2B` pelo rehash shadow/WAL/OCC já auditado; no teto, a identidade é provada e o scan é pulado. Ensaio direcional: 4.000 linhas não cresceram e 5.000 fizeram `64→128`, com todas as linhas/seeks íntegros. Não existe hook de commit/background; `None` não certifica saúde e corrida exige reavaliação, sem promessa universal de retryable. Slices locais 31+67 e revisão independente 107 verdes; formato, durabilidade e multiwriter/multireader intactos |
| 2026-09-05 | lote de escala 36 — witness revogável de replay local no checkpoint | concluído em `2792701`; Nexus `hof_fbf973af72084c928938aa64dcb522d4` verificado PASS após corrigir blocker de eficácia | Um witness O(1), apenas do processo, nasce em checkpoint completo e avança somente depois de DML local — inclusive subcommit CN-1 — com WAL durable, páginas/índices aplicados e flushados e estado publicado. Checkpoint ainda lê/prova toda a linhagem WAL e executa preflight integral; só omite dispatch/flush redundante no intervalo exato. Escritor estrangeiro, DDL/generation/RESET, dirty state, recovery/falha/close e colaborador custom caem no replay canônico. Em 4.000 linhas/16 txns, 7 checkpoints pularam o dispatch e `CommitRedo.apply` caiu `32→18`; micro isolado de 250 inserts mediu `1,47x`, sem claim end-to-end. Slices 101+11 e checks estáticos verdes; multiwriter/multireader, OCC e durabilidade intactos |
| 2026-09-05 | lote de escala 37 — preflight lógico estrito sem duplicação | concluído em `75b0fdc`; Nexus `hof_6a091147d30e460eb85518398c096ee0` verificado PASS | O fast path do lote 36 exige `touched_catalog=False`; portanto seu full preflight já usa `allow_unregistered_indexes=False` e valida nomes/generation, shape versioned e limite de chave. O segundo preflight do subplano lógico foi removido sem criar janela entre prova e decisão. Sequência focal caiu `[3,2,0]→[3,0]`; auditoria em 2.000 linhas confirmou um único preflight estrito com efeitos por shortcut e manteve o permissivo somente para DDL, que não pula. Slice 62 verde e checks estáticos limpos; melhoria estrutural simples, sem gate temporal nem mudança de WAL/OCC, durabilidade ou concorrência |
| 2026-09-05 | lote de escala 38 — fatos RESET reaproveitados das validações obrigatórias | concluído em `6b142ba`; revisões local e Nexus `hof_a85992ce987045a7ac73b2bb42ac822c` PASS | O full preflight de checkpoint e `validate_staged_records` já decodificavam todos os efeitos; seus proofs privados agora transportam `contains_index_reset` e eliminam os dois rescans. Checkpoint exige seal/owner/replay/passage/signature verificados; ausência ou incompatibilidade mantém replay canônico. Commit confia somente no `IndexManager` e validator built-in exatos; retarget continua revalidando o multiset. RESET é detectado pela operação decodificada porque também usa `INDEX_WRITE`. A/B 2.000 CREATEs/8 commits/8 checkpoints teve medianas `11,397→11,166 s`, mas ranges sobrepostos tornam o delta temporal indistinguível de ruído; o claim é estrutural, sem gate. Payload inválido, provas replay/passage/owner erradas e validator substituído foram cobertos; formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 2026-09-05 | lote de escala 39 — reuse de decode do certificado fresco byte-idêntico | concluído em `f28c686`; revisão Nexus `hof_0f5a8046fe2844d79b0c3bbd5a90ae72` verificada PASS | Cada observação continua invalidando a identidade do descriptor e lendo page 0 fisicamente. Igualdade de todos os bytes reutiliza somente o par imutável witness/certificado já validado; mudança, corrupção ou recusa percorre o caminho integral. Decodes genéricos `1.015→12` e semânticos `1.007→4`; timings ruidosos, sem claim temporal. Custo lazy de uma raw page por IndexStore acessado (~8 KiB default; ~1,1 MiB/141), fora do retained estimate do BufferPool. A mutação adversarial levou a cobrir corrupção exclusiva do último byte; 17 focais e 298 agrupados passaram. OCC, WAL, durabilidade e multiwriter/multireader intactos. |
| 2026-09-05 | lote de escala 40 — piso inicial atômico e cursor de extent selado | concluído em `4ecebc8`; revisão adversarial independente GO | O primeiro row instala o piso final planejado no mesmo commit e os demais usam a reserva selada, eliminando rewrites de page zero por identidade. A sonda de 500 rows registrou 1 criação, 499 inserts reservados, 3 lookups e 62 rewrites para 62 crescimentos reais. Regressões adversariais cobrem piso/raiz/cursor, corrida vazia, falha pré-WAL e lote misto. WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 2026-09-05 | lote de escala 41 — autoridade de índice local ao statement/commit | concluído em Grafx `613bff6` e consumidor Pulse `e4ac346`; revisões estrutural e de correção independentes GO; Grafx 321/321 e Pulse 57/57 | Footprints built-in fechados e seu commit canônico deixam de enumerar tabelas e índices não relacionados. A projeção nasce pós-OCC/rebase/sync dentro das seções já serializadas, revalida revision/observations/claims e é sempre revogada; desconhecidos, pre-staged e custom mantêm fallback global. Com 1 versus 80 tabelas, CREATE Person v1/v2 conservou contagens idênticas e zero scans globais, inspecionando somente Person. Validadores exatos, ambas OCC, WAL, durabilidade e multiwriter/multireader permanecem integrais |
| 2026-09-05 | lote de escala 42 — precheck de DELETE em relações | concluído em `9bad8c0`; gate focal e revisão adversarial verdes | Em batches insert-only, a prova de que não há relação encerrada deixa de reconstruir views por tabela a cada seek; qualquer DELETE/held-delete delega ao caminho integral. Harness Pulse-shaped T=30/1.500 relações mediu `60,17→2,03` views/relação e `-44,7%` na fase, com resultado e verifier idênticos. Formato, WAL/OCC, durabilidade e multiwriter/multireader intactos |
| 2026-09-05 | lote de escala 43 — descriptor bounded por transação lexical | concluído em `f7100d2`; gates Grafx/Pulse verdes e revisão independente PASS | A fronteira pública lexical reutiliza somente o fd destravado, revalidando identidade física antes de cada acquire; locks reais, cleanup, thread-hop, retry/OCC e close permanecem. Contagem `4→1` opens por transação e `0→3` revalidações, sem promover timing ruidoso. Nenhum descriptor cruza transações e nenhuma premissa de concorrência muda |
| 2026-09-05 | lote de escala 44 — publicação exata sem cópias repetidas | concluído em `19882b9`; Nexus `hof_d48f29fa9bbe46f9951912e7f50af166` e validação Codex PASS | Valores `int`/`float`/`str` built-in exatos evitam canonicalização repetida somente depois das guardas compartilhadas; `bool`, subclasses e hostis preservam o caminho completo. Nomes de campo/ReturnItem são calculados uma vez por shape, mantendo child vazio lazy. Em 20 mil linhas, chamadas de `ReturnItem.name` `40.040→30`, `_builtin_int` `60.054→46` e `_builtin_text` `40.053→50`; sem claim temporal. 479 focais do executor mais validação independente passaram; formato, taxonomia pública, WAL/OCC e concorrência intactos |
| 2026-09-05 | lote de escala 45 — watermarks de checkpoint pelo escopo físico provado | concluído em `5a85af8`; gate agrupado 99/99 e handoff `hof_fc109137ae1343feb44c52358465b6ef` PASS | O preflight obrigatório transporta os table ids das páginas HEAP verificadas e o checkpoint fotografa somente as tabelas tocadas, mantendo resposta completa com os watermarks anteriores das intocadas. DDL, META/FREE/reclaim, prova incompatível e manager custom fazem fallback integral; OVERFLOW é irrelevante somente porque o high-water lê headers. O `open()` final reutiliza a mesma foto ainda dentro da `COMMIT_SECTION` e lê fresh qualquer tabela nova ausente. Teste com escritor estrangeiro e A/B/C prova que apenas A é percorrida; DDL e scope desconhecido percorrem todas. O hotspot anterior ocupava `25,9–26,5%` da escrita em T=40 populado, mas este marco não promove número temporal pós-patch. WAL, OCC, durabilidade e multiwriter/multireader permanecem integrais |
| 2026-09-05 | lote de escala 46 — prova device-fresh das raízes META do heap | concluído em `2b936f6`; gate agrupado 112/112; handoffs Nexus `hof_13ed0382ed514771925bd36e9cf1b877` e `hof_d28a9d1d8df14ab7878659857054768f` PASS | O preflight lê uma única baseline física durável de page 0 e compara somente `(table_id, first_page)`, os dados que realmente selecionam os walks de high-water. Root criada/removida/movida escopa as tabelas exatas; baseline ilegível, diretório malformado/duplicado, tipo/local inesperado, FREE, catálogo e manager custom preservam fallback integral. No mesmo T=40 populado, 15/16 fotos ficaram escopadas, walks caíram `440/480→168/172` e a fatia observada `17,5/20,4%→3,6/4,4%`; timings são diagnósticos, não gate. Treze provas focais e o slice agrupado passaram, com `verify(all)` limpo. Formato, WAL/OCC, durabilidade, leases e multiwriter/multireader permanecem integrais |

Esta seção registra fatos concluídos e trabalho explicitamente em andamento. H5 e os itens 12--21
foram fechados. Por
decisão explícita posterior do usuário, P0.3/P0.4 continuam evidência de engenharia, mas não são
gates de promoção; os gates de qualidade da seção 8 permanecem integralmente vinculantes.
