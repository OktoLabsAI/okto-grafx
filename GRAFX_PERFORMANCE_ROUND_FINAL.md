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
| D-13..D-17 | lane vetorial futura; D-15 cache persistida explicitamente removida |
| D-18 | experimento de cota após P1, não gate nem escopo obrigatório |
| D-19..D-22 | lane NT-1 futura; só após call counts, microbench e microprotótipo ABI real; adapter por instância |
| D-23 | P0.3, somente pós-drain em cópia e sem `--locals` |
| D-24 | P0.3, aceita com corpus/instrumentos versionados |
| D-25 | P0.0/P0.4, baseline corrigido para o SHA de integração promovido e mínimo de três runs |
| D-26 | P1.1, expandida para lease + commit section + fases |
| D-27 | lane vetorial futura, com matriz maior e presença produtiva primeiro |
| D-28 | parcialmente incorporada aos gates; verify read-only separado de recovery/WAL |
| D-29 | dívida pré-native; memo do corpus precisa chave estável, pois `load_provider()` cria lambda nova por chamada |
| D-30 | roadmap futuro; deve evoluir o cursor/`scan_rows_v1` com budgets, não criar API paralela orientada ao benchmark |
| D-31 | roadmap GX-CAP-0/1; ordem de capabilities deve respeitar commit metadata antes de temporal |
| D-32 | roadmap FTS; contrato semântico/analyzer/BM25/lifecycle antes do formato físico |
| D-33 | formato futuro; não se apoia na alegação falsa de D-08 `O(1)` |
| D-34 | futuro e posterior a bulk + redução de reencode + WA; fence continua necessário, mas “build antigo truncaria WAL saudável” não foi confirmado pelo fluxo atual |
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
| 2026-09-03 | item 11 / P2-VAC — censo read-only | implementado; protocolo destrutivo aguardando autorização específica | `db.maintenance.bloat(table=None)` mede headers numa observação sem pruning do horizonte limitado pelo checkpoint, com DTOs imutáveis, filtro por tabela e limites de accounting explícitos. O fresh view recusa dirty em vez de fazer flush, reader records TTL-stalled permanecem pins e nenhum WAL/página é escrito; `vacuum_safety_established=False`. Passou 196 testes focais mais testes de buffer/read-view/coordenação, Ruff, compile e diff-check. Auditoria concluiu que TTL não prova quiescência e que reuse sem incarnation cria ABA; o contrato mínimo proposto é foreground/process-quiescent com floor global durável, cuja restrição multireader exige autorização antes do código |

Esta seção registra fatos concluídos e trabalho explicitamente em andamento. H5 foi fechado. Por
decisão explícita posterior do usuário, P0.3/P0.4 continuam evidência de engenharia, mas não são
gates de promoção; os gates de qualidade da seção 8 permanecem integralmente vinculantes.
