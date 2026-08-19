# SPEC VEC — Busca por similaridade combinável do Okto Grafx: tipo vetorial, espaço de embedding, operador híbrido e HNSW sob o contrato de M1 (M2–M4)

> Spec ID `69670909-0c30-53f0-9b54-92d7d257079a` · board `c5f05299-fcb5-41f3-bac9-ba7faf90fb16` · status **validated** · edition 2 · version 56
> **Depende de SPEC M1** (`780475bb-f8eb-57a0-9411-3dffaf8feeeb`) como prerequisite: nenhuma superfície de consulta antes de M1 verde (D4).

## Descrição

Especificação executável da decisão D8 na parte fora de M1: embedding como tipo de propriedade de primeira classe (D8b, M2), busca por similaridade como MODELO DE CONSULTA — combinável na mesma passagem com filtro de metadado de nó/relacionamento e predicado de travessia (D8c, M3) — e índice HNSW realizando o contrato de índice secundário fixado na SPEC M1 (M4), com o quarto teto de D5 em recall@k sob orçamento de latência (D8d).

Base factual atestada: o consumidor real (Okto Pulse) já guarda `embedding DOUBLE[384]` como coluna de nó com 9 índices HNSW, e sua busca degenera em over-fetch + cinco pós-filtros em Python porque o índice ANN do motor de referência não aceita predicado algum — o ranking híbrido anunciado executa três constantes. Esta spec existe para que essa compensação de camada seja impossível de precisar existir.

Decisões de desenho herdadas do RDL do refinamento: planejador em DOIS REGIMES por seletividade (varredura exata sob limiar de cardinalidade; travessia HNSW filter-aware da família ACORN acima), e evolução do espaço de embedding por COEXISTÊNCIA SEGREGADA com aposentadoria observável — o motor nunca gera embeddings (D6).

## Functional Requirements

- **`fr_a83f4a51`** — FR-1 Tipo vetorial de primeira classe (M2): a propriedade vetorial é declarada no esquema com {dimension, distance_metric: cosine|dot|euclidean, normalized: bool, storage_dtype: float32|float64, space_id opaco do gerador}; toda escrita valida dimensão e valores finitos; violação falha com GrafxVectorValidationError sem persistir nada.
- **`fr_18bb2631`** — FR-2 Identidade de espaço obrigatória: busca por similaridade compara APENAS vetores do mesmo space_id; qualquer comparação cruzada falha com GrafxEmbeddingSpaceMismatch — nunca resultado silenciosamente sem sentido.
- **`fr_df4ee654`** — FR-3 Evolução de espaço por coexistência segregada: múltiplos espaços por propriedade, cada um com índice próprio; a consulta declara o espaço-alvo; aposentadoria explícita torna o espaço somente-leitura (escrita falha com GrafxSpaceRetired), emite métrica de cobertura residual e marca resultados com retired=true; o motor NUNCA gera embeddings — re-embed é do chamador, no ritmo dele (D6).
- **`fr_9895f8d4`** — FR-4 Operador combinável (M3): uma única consulta combina similaridade com filtros de propriedade de nó, filtros de propriedade de relacionamento e predicados de travessia, planejada pelo executor como UMA árvore de operadores — sem over-fetch recomposto pelo chamador e sem segunda ida ao grafo por candidato.
- **`fr_7f7235ce`** — FR-5 Planejador em dois regimes: conjunto filtrado com cardinalidade estimada ABAIXO do limiar calibrado → varredura exata (recall 1.0); ACIMA → travessia HNSW filter-aware (predicado avaliado durante a navegação; nós não-satisfazentes permanecem como pontes); todo resultado carrega o rótulo exact|approximate e achieved_k — under-k silencioso é proibido.
- **`fr_66a58944`** — FR-6 HNSW sob o contrato de índice secundário da SPEC M1 (M4): escrituras do índice cobertas pelo WAL, entradas VERSIONADAS com tombstone reconciliado pelo horizonte de snapshot (reconciliação transacional, cada limpeza é registro de WAL reversível por replay), e percorrido por verify() com detecção de divergência índice-heap.
- **`fr_f8d8cf32`** — FR-7 Porta VectorMath: implementação de referência em Python puro é o ORÁCULO de correção; adaptador acelerado opcional (numpy) atrás da porta, selecionado por configuração, com resultados dentro de tolerância declarada do oráculo; nenhuma dependência numérica entra no domínio (D2).
- **`fr_b8bec236`** — FR-8 Quarto teto de D5 (D8d): recall@k sob orçamento de latência medido contra o baseline HNSW do Ladybug 0.16 pelo harness (extensão do harness da SPEC M1); publicado em oktografx_vector_recall_ratio e aplicado como gate de CI; a fase de calibração desta spec congela o limiar de regime, o alvo de recall e o default de storage_dtype — nenhum outro card de implementação inicia antes dela verde.
- **`fr_d23b595f`** — FR-9 Correção MVCC dos resultados: a busca respeita o snapshot ativo — nenhum vetor de versão não visível ao snapshot aparece no resultado, provado sob escrita concorrente.
- **`fr_ed569f88`** — FR-10 Métricas da busca: recall efetivo, latência por fase (planejamento, travessia, validação), seletividade do filtro, taxa de queda para varredura exata, tamanho e idade do índice, cobertura por espaço — tudo pela porta MetricsSink com painel Grafana no mesmo commit.

## Technical Requirements

- **`tr_004ee39f`** — TR-1 Pureza do domínio preservada: numpy é importado APENAS no adaptador da porta VectorMath; o domínio calcula com o oráculo puro; gate de import-boundary da SPEC M1 estendido para cobrir o subsistema vetorial.
- **`tr_96766184`** — TR-2 Registro de espaços persistido no catálogo: {space_id, dimension, metric, normalized, dtype, state: active|retired}; um índice por (propriedade, espaço); identidade de espaço é imutável após criação.
- **`tr_15bdc978`** — TR-3 Reconciliação de tombstone reusa o mecanismo de horizonte de snapshot da SPEC M1 — nenhum segundo ciclo de vida é inventado; cada passo de limpeza é registro de WAL (compatível com BR-1 de M1 por construção).
- **`tr_e83d6bfb`** — TR-4 Dtype de armazenamento: a API aceita float64 (paridade com DOUBLE[384] do motor de referência); o armazenamento é por propriedade com default float32 (metade do WAL e das páginas por vetor — medido: 1536B vs 3072B em 384 dims); a calibração confirma que o ranking por cosseno em vetores normalizados não diverge do f64 além da tolerância declarada.
- **`tr_dd5fa025`** — TR-5 Taxonomia de erros en-US: GrafxEmbeddingSpaceMismatch, GrafxVectorValidationError, GrafxSpaceRetired — mensagens en-US, campo retryable explícito, sempre exceção tipada.
- **`tr_14022e6b`** — TR-6 numpy exclusivamente como extra opcional [accel]; o CI executa a suíte do oráculo SEM o extra e a suíte de paridade COM o extra, nas duas famílias de SO (D9).

## Acceptance Criteria

- **`ac_9706c92d`** — AC-1 Escrita de vetor com dimensão errada, NaN ou infinito falha com GrafxVectorValidationError e NADA é persistido (heap, WAL e índice inalterados sob verify()).
- **`ac_9303f0fb`** — AC-2 Consulta declarando espaço A contra propriedade cujo vetor pertence ao espaço B falha com GrafxEmbeddingSpaceMismatch; nenhum ranking parcial é devolvido.
- **`ac_68d5675f`** — AC-3 Dois espaços coexistindo na mesma propriedade: consultas por espaço enxergam SOMENTE os vetores do próprio espaço; as métricas de cobertura por espaço somam o total de nós com vetor.
- **`ac_1c215abc`** — AC-4 Espaço aposentado: escrita falha com GrafxSpaceRetired; consulta é permitida e cada resultado carrega retired=true; a métrica de cobertura residual decresce conforme o chamador migra os vetores.
- **`ac_409a965b`** — AC-5 Filtro seletivo (cardinalidade abaixo do limiar calibrado): o plano usa varredura exata, recall medido = 1.0 contra ground truth, resultado rotulado exact.
- **`ac_9f139c7c`** — AC-6 Filtro amplo (acima do limiar): o plano usa travessia filter-aware; recall@k medido >= alvo calibrado contra ground truth exato; resultado rotulado approximate com achieved_k correto.
- **`ac_bac3258a`** — AC-7 Consulta combinando similaridade + filtro de nó + filtro de relacionamento + predicado de travessia executa como UM plano: a inspeção do plano mostra uma árvore única de operadores, sem etapa de over-fetch e sem segunda consulta por candidato.
- **`ac_10550688`** — AC-8 Crash injetado no meio de atualização do índice HNSW: após recuperação, verify() reporta índice-heap consistentes; a reconciliação de tombstones é limitada pelo horizonte de snapshot e cada limpeza aparece como registro de WAL no replay.
- **`ac_71a31513`** — AC-9 Suíte de paridade VectorMath: oráculo puro e adaptador numpy produzem rankings idênticos dentro da tolerância declarada num corpus fixo com seed; a suíte passa SEM o extra instalado (oráculo) e COM o extra (paridade), em windows-latest E ubuntu-latest.
- **`ac_3cf7cb03`** — AC-10 O harness publica oktografx_vector_recall_ratio e as latências por fase; o gate de CI falha quando o recall viola o alvo (verificado forçando violação artificial); a calibração congela limiar de regime, alvo de recall e default de dtype com valores registrados; todas as métricas de FR-10 aparecem no dashboard versionado.

## Business Rules

### `br_0929e02f` BR-1 Comparação cruzada de espaços é sempre erro tipado
- **Quando**: Uma busca ou operação de similaridade envolveria vetores de space_id diferentes
- **Então**: A operação falha com GrafxEmbeddingSpaceMismatch antes de calcular qualquer distância; nenhum ranking parcial ou misto é devolvido em circunstância alguma

### `br_1a3ac43b` BR-2 Resultado aproximado é sempre rotulado; under-k nunca é silencioso
- **Quando**: Qualquer busca por similaridade retorna resultados
- **Então**: O resultado carrega o rótulo exact|approximate e o achieved_k real; se o motor não alcança o k pedido, isso é visível no resultado, nunca omitido

### `br_118a4e99` BR-3 Manutenção de índice nunca destrói e respeita o snapshot
- **Quando**: Tombstones são reconciliados, o índice é atualizado sob escrita concorrente, ou uma busca executa sob snapshot antigo
- **Então**: Cada limpeza é registro de WAL reversível por replay (nenhum rebuild destrutivo sancionado); a reconciliação respeita o horizonte de snapshot; e nenhum vetor de versão não visível ao snapshot ativo aparece em resultado

### `br_444f5c19` BR-4 O motor nunca gera embeddings
- **Quando**: Um espaço evolui, um vetor está ausente, ou qualquer operação poderia sintetizar um embedding
- **Então**: O motor jamais chama modelo algum nem re-embedda dados; a migração de espaço é sempre executada pelo chamador, incrementalmente, com a cobertura residual observável por métrica

### `br_280f0f49` BR-5 O tipo vetorial valida na escrita, nunca na leitura
- **Quando**: Uma escrita de vetor viola dimensão, contém NaN/infinito, ou referencia espaço inexistente/aposentado
- **Então**: A escrita falha com o erro tipado correspondente (GrafxVectorValidationError, GrafxSpaceRetired) e nada é persistido em heap, WAL ou índice; leituras nunca precisam se defender de vetor malformado

### `br_4bc7a44e` BR-6 A combinação é do plano, nunca do chamador
- **Quando**: Uma consulta combina similaridade com qualquer predicado (metadado de nó, de relacionamento, travessia)
- **Então**: O executor produz uma única árvore de operadores; o chamador jamais precisa fazer over-fetch, pós-filtro ou segunda consulta por candidato para obter o resultado combinado

### `br_cb6338ee` BR-7 O oráculo puro decide a correção
- **Quando**: Um adaptador acelerado da porta VectorMath está ativo
- **Então**: Seus resultados são validados contra o oráculo em Python puro dentro da tolerância declarada; divergência além da tolerância é defeito do adaptador, e a suíte do oráculo roda sem o extra instalado

### `br_11d9fdcf` BR-8 Recall é gate de CI, não aspiração
- **Quando**: O pipeline de CI executa a suíte vetorial, ou uma busca degrada o recall abaixo do alvo calibrado
- **Então**: O gate lê oktografx_vector_recall_ratio da porta MetricsSink e FALHA em violação; toda métrica de FR-10 tem painel no dashboard do mesmo commit

## Test Scenarios

- **`ts_9f3bd07f` VTS-1** (negative) — Vetor inválido rejeitado sem persistir nada. Given: propriedade dimension=384, cosine, float32, espaço ativo; digest do estado capturado. When: escritas com dimensão 383, NaN, infinito, espaço inexistente. Then: cada uma falha com GrafxVectorValidationError (ou erro de espaço); verify() limpo; heap/WAL/índice byte-idênticos ao digest.
- **`ts_86ce7a66` VTS-2** (negative) — Mistura de espaços falha tipada, nunca ranking parcial. Then: GrafxEmbeddingSpaceMismatch antes de qualquer cálculo de distância; métrica de erro incrementa com label bounded.
- **`ts_3099186c` VTS-3** (integration) — Coexistência segregada com cobertura correta por espaço (60/40); cada consulta enxerga só o próprio espaço; métricas somam o total.
- **`ts_1ea2f434` VTS-4** (integration) — Aposentadoria observável: escrita falha GrafxSpaceRetired; consultas funcionam com retired=true; cobertura residual decresce.
- **`ts_8ff440a9` VTS-5** (integration) — Regime exato sob filtro seletivo: plano usa varredura exata, recall = 1.0, rótulo exact, métrica de queda registra.
- **`ts_e57c74ec` VTS-6** (integration) — Regime ANN filter-aware sob filtro amplo: predicado avaliado na navegação (nós não-satisfazentes como pontes, verificado por instrumentação), recall@k >= alvo, rótulo approximate com achieved_k.
- **`ts_513de3e7` VTS-7** (integration) — Consulta híbrida completa numa única árvore de operadores: similaridade + filtro de nó + filtro de relacionamento + travessia de 2 saltos; sem over-fetch nem re-consulta por candidato; correto contra força bruta; latências por fase nas métricas.
- **`ts_b50fca4b` VTS-8** (negative) — Crash no meio da atualização do HNSW: verify() consistente após recuperação; reconciliação nunca remove entrada referenciada por snapshot vivo; cada limpeza no replay do WAL.
- **`ts_ad343472` VTS-9** (e2e) — Paridade oráculo x adaptador: rankings idênticos dentro da tolerância; 4 combinações (2 famílias de SO x com/sem extra) verdes.
- **`ts_bc84a040` VTS-10** (e2e) — Calibração vetorial e gate de recall no CI: congela limiar/alvo/dtype; publica oktografx_vector_recall_ratio; gate FALHA na violação forçada; toda métrica de FR-10 no dashboard.

## Decisions

### `dec_fc3351c3` Planejador de dois regimes
Escolha por estimativa de cardinalidade do filtro: filtros seletivos -> varredura exata sobre o conjunto filtrado (recall 1.0, rótulo exact); filtros amplos -> travessia HNSW filter-aware estilo ACORN, avaliando o predicado durante a navegação e usando nós não-satisfazentes como pontes. Nunca over-fetch + pós-filtro. Limiar calibrado no harness e congelado.
Rejeitadas: pós-filtro com over-fetch fixo; pré-filtro sempre; regime ANN único sem fallback exato.

### `dec_c9eb0563` Identidade de espaço de embedding com coexistência segregada e retirada observável
Espaço = (dimension, metric, space_id). Comparação cruzada = erro tipado. Migração = coexistência segregada; retirada é observável (métrica + ledger), nunca rebuild destrutivo.
Rejeitadas: coluna vetorial global sem identidade; migração big-bang com rebuild; conversão automática entre espaços.

### `dec_6b65207f` dtype de armazenamento f32 default, f64 opt-in por espaço
f32 = metade dos bytes (1536 vs 3072 em 384 dims) com perda irrelevante para ranking. Acumulação interna dos kernels em precisão dupla independentemente do dtype de armazenamento.
Rejeitadas: f64 sempre; f16/quantização como default; dtype global não configurável.

## Integration Requirements

- **`ir_1f903e2c`** IR-1 — Extensão do harness de calibração da M1 com baseline HNSW do Ladybug 0.16: dataset vetorial versionado (corpus com seed + ground truth exato) alimentando o quarto teto relativo de D5/D8.
- **`ir_dca33d39`** IR-2 — numpy exclusivamente como extra opcional `[accel]` atrás do porto VectorMath: o core puro-Python funciona e passa a suíte do oráculo sem ele.

## Observability Requirements

- **`or_9418ca25`** OR-1 Métricas de busca vetorial por regime — `oktografx_vector_recall_ratio`, `oktografx_vector_query_latency_seconds` (label regime=exact|approximate), `oktografx_vector_exact_fallback_total`, achieved_k. Labels nunca incluem conteúdo de embedding. Threshold: recall_ratio >= alvo congelado (gate de CI).
- **`or_e76e9138`** OR-2 Métricas do índice vetorial e reconciliação — `oktografx_vector_tombstone_backlog`, `oktografx_vector_reconciliation_total`, `oktografx_vector_index_entries` (label space_id), `oktografx_vector_space_retired_total`.
- **`or_ae2b397a`** OR-3 Dashboard Grafana vetorial versionado — painéis para TODAS as métricas de FR-10; métrica publicada sem painel FALHA o CI.

## Cards derivados

- `355c027b` V1 [M2] Tipo vetorial de primeira classe + registro de espaços de embedding
- `6cd64d59` V2 [M3] Operador combinável + planejador em dois regimes
- `67e8cf4e` V3 [M4] HNSW sob o contrato de índice secundário da M1 + reconciliação de tombstones
- `cee380d6` V4 Porta VectorMath + calibração vetorial/gate de recall + métricas e dashboard
- `f8156876` TV1 · `21f63173` TV2 · `f025645a` TV3 · `af6d353a` TV4 · `032296c7` TV5 (test cards)
