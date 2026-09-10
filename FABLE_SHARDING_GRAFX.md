# FABLE_SHARDING_GRAFX.md — sharding no Okto Grafx: complexidade e ganho sob as premissas vigentes

**Data:** 2026-09-03 (`America/Sao_Paulo`)
**Autor:** Claude (Fable 5.1), avaliação somente-leitura, um único agente; emendado em 2026-09-03 após revisão adversarial do Codex (handoff `hof_6ca6d9e8`; ver "Revisões" no fim)
**Base lida:** `origin/feature/v0.0.2@b2aa2ba` (linha de integração da rodada) e `perf/v002-d02-endpoint-locator@51bc7c4` para `derived_epoch`; documentos versionados citados por linha
**Precedência:** `GRAFX_PERFORMANCE_ROUND_FINAL.md` governa a rodada; este documento é insumo para a decisão Codex/Claude/usuário sobre a fila estrutural. Nenhum número novo foi medido; nada do data home vivo do Pulse foi aberto.

**Consenso Codex/Claude (2026-09-03).** Sharding do motor **não entra nesta rodada nem no seu fechamento**. Fica registado na fila estrutural explícita, atrás de P2-ID/P2-DIRTY/P2-VAC, com o critério de entrada mensurável da secção 5.1. O desenho (b) por hash de identidade é recusado em definitivo. O experimento da secção 5.3 é **backlog opcional pós-rodada**, não tarefa da rodada atual: os gates de performance foram retirados da rodada e não se abre alvo móvel. As três perguntas da secção 6 continuam a ser do usuário. As estimativas deste documento são `[HIPÓTESE]` e assim permanecem até serem medidas na cópia declarada.

Convenções: `[LIDO]` = lido no código ou documento citado; `[MEDIDO]` = número versionado, com fonte; `[HIPÓTESE]` = inferência minha, não medida; `[A_MEDIR]` = só decidível por medição na cópia declarada.

---

## 0. Resumo executivo

1. **Recomendação (consenso Codex/Claude): não entra nesta iniciativa, nem "condicionado ao fim dela"; vai para a fila estrutural explícita com um critério de entrada mensurável (secção 5). A única coisa que merece medição é o experimento barato da secção 5.3, e mesmo esse é backlog opcional pós-rodada, não tarefa da rodada atual.**
2. O gargalo real do backfill do Pulse é CPU em Python no caminho de leitura de um único escritor por board (91 % de um núcleo, sem espera de lock nem I/O; `FABLE_PERFORMANCE_GRAFX.md:157-168` `[MEDIDO]`). Sharding não reduz o custo por página nem o tamanho de uma varredura de tabela; as trilhas P1/P2 em curso atacam exatamente isso.
3. Sharding só ganha onde há **vários escritores concorrentes no mesmo board** disputando a writer lease (teto `1/lease_held`, `FABLE:285` I-3). Esse regime existe no harness de concorrência (`docs/PERFORMANCE.md:109-124`: 313 ms de mediana com 4 escritores contra 95–112 ms mono-escritor, ~3x de fila `[MEDIDO]`), mas **não existe** no Pulse, que serializa por board por decisão do usuário.
4. Ganho teórico por desenho, no regime multi-escritor e apenas para transações confinadas a um shard: (a) shard por tabela: até `k` vezes na fração `s` de transações cujo **conjunto de escrita** cabe num shard. Para o fluxo do Pulse `s` é `[A_MEDIR]` no censo P0.3, não presumível: uma criação de aresta **lê** duas tabelas de nó (validação das extremidades) mas pode **escrever** só a família da relação e os seus índices; o que conta para `s` é o write-set, não o read-set; (b) shard por hash de identidade: ganho negativo, quase toda transação é cross-shard; (c) sharding lógico no Pulse: ganho 0 no fluxo atual, porque o Pulse já roteia um store por board e o escritor é único por board.
5. Custo dominante: um commit atómico entre shards exige registro de commit global ou 2PC, LSN/CSN global, replay ordenado entre WALs e ponto de consistência global no recovery. Isso reescreve o protocolo de commit congelado (`CONTRACT.md:772-854`), o algoritmo de recovery congelado (`:856-921`), o layout de ficheiros congelado (`:523-536`) e as duas portas de assinatura congelada (`:188-303`).
6. Complexidade em ordem de grandeza `[HIPÓTESE]`: (a) 16–30 pessoa-semanas; (b) 20–40 pessoa-semanas com ganho esperado negativo; (c) 0 no motor e 2–6 no Pulse com ganho 0; variante "heap por tabela sob um WAL" 4–8 pessoa-semanas, ganho `[A_MEDIR]`.
7. Risco dominante 1 (durabilidade/recuperação): no Windows a publicação de **ficheiro de controlo** custa ~16,5 ms contra 0,13 ms no Linux (`docs/PERFORMANCE.md:214-215` `[MEDIDO]`). Esse número não prova que cada barreira de WAL de shard custe o mesmo; o que se sabe é que um commit atómico em `k` shards precisa de mais pontos de durabilidade do que um commit hoje, e que a multiplicação de custo na plataforma principal é `[HIPÓTESE a medir]`, plausível mas não demonstrada.
8. Risco dominante 2 (estabilidade/isolamento): snapshot global consistente entre shards exige um alocador de CSN único ou snapshots vetoriais com visibilidade cross-shard. O alocador serializa uma **secção curta** (a atribuição do número), não necessariamente o commit inteiro; o custo real está na coordenação/commit global e na atomicidade cross-shard, que nenhuma parte do motor, do verifier ou dos 5.455 testes conhece hoje.
9. O roadmap complementar já declara "cluster, consensus, sharding e distributed transactions" fora de escopo (`GRAFX_COMPLEMENTARY_EVOLUTION_PLAN_CODEX.md:2180-2182`); reabrir isso é decisão do usuário, não desta rodada.
10. O que valeria medir custa horas, não semanas: o experimento na cópia declarada (secção 5.3) que responde "o gargalo é serialização de escrita ou custo por página?" usando a instrumentação D-26 já integrada (`docs/PERFORMANCE_ROUND_0_0_2.md:181-195`), sem implementar sharding. Por consenso fica em **backlog opcional pós-rodada**: não é tarefa desta rodada.

---

## 1. O que "sharding" significaria no Grafx

### 1.0 A topologia de hoje, que todo desenho tem de respeitar

Um board Grafx é um diretório com **um** heap para todas as tabelas, **um** catálogo, **um** ficheiro por índice, **um** WAL segmentado, **um** ledger, **um** `writer.lease` com época, registos de leitores e **um** `commit.state` (`docs/architecture/CONTRACT.md:523-536` `[LIDO]`). O `grafx.meta` grava `format_version`, `page_size`, UUID e `partitions_per_table` (`:545-551`).

Os pontos de serialização e de frescor são:

- **Writer lease + `COMMIT_SECTION`.** A lease é adquirida por commit em `txn_manager.py:2680-2690` (`_hold_lease` dentro da janela `writer_lease` traçada pelo D-26), validada em `:2693-2696` e a secção exclusiva cross-process é `coordinator.exclusive(name)` em `:5250`. A lease "não serializa transações inteiras, identifica o detentor da época" (`CONTRACT.md:281-282`), mas contém a `COMMIT_SECTION` inteira (`FABLE:285`, I-3), logo o teto de throughput por board é `1/lease_held`.
- **Um LSN/CSN.** `Snapshot.read_lsn` decide visibilidade por `xmin <= read_lsn < xmax` (`CONTRACT.md:775-779`); `commit.state` publica `{last_committed_lsn, last_csn, checkpoint_lsn}` (`:535`). O WAL é "uma corrida contígua" de LSN, e um lote nunca atravessa dois segmentos (`wal_manager.py:1-19`).
- **Página 0 como relógio de frescor por ficheiro.** `_write_back` faz CAS da página 0 sob `_page_write_section` (`buffer_pool.py:1563-1600`); cada ficheiro tem `_page_sequence_fence` (`:367`) e um `derived_epoch(file)` que soma relinks e descartes de cache (`buffer_pool.py:416-423` em `51bc7c4`). Os índices já certificam frescor por ficheiro: `_fresh_certificate` (`index_manager.py:733`), `_require_safe_certificate` (`:757`), frescor por tabela A1 via `_table_high_water` (`:362`, `:867`) e cerca de rebuild `_completed_rebuild_through` (`:358`, `:781`).
- **OCC por partição lógica.** `partition_of(table_id, key, partitions_per_table)` (`txn_manager.py:987`), default 64 (`docs/PERFORMANCE.md:2`); "partições disjuntas nunca conflitam" (`CONTRACT.md:854`, BR-6). Isto já é um hash-sharding **de interesses de conflito**, não de armazenamento.
- **Recovery único.** Varre um WAL a partir de `checkpoint_lsn`, para no primeiro registro inválido, quarentena + ledger, redo em ordem de LSN idempotente por `page_lsn`, sem undo (`CONTRACT.md:870-882`).

### 1.1 Desenho (a) — shard por tabela: ficheiros de páginas independentes, cada um com lease, WAL e página 0 próprios

**O que muda, por componente `[LIDO]`:**

| Componente | Hoje | Sob (a) |
|---|---|---|
| `txn_manager.py` commit (`:2680-2696`, `:2921`, `:5250`) | uma lease, uma secção, um `append_many` + `barrier` (passo 3.4–3.5), apply, publish | uma lease por shard tocado, adquiridas em ordem total para evitar deadlock; um registro de commit **global** (novo ficheiro/log) cuja barreira é o único ponto de ack; OCC-1 tem de ler os COMMITs de **cada** WAL de shard acima do snapshot; o "current" do passo 3.2 vira um vetor |
| `buffer_pool.py` (`:1563-1600`, `:367`, `:416-423`) | página 0 por ficheiro, já per-file | quase intacto: o CAS e o `derived_epoch` são por ficheiro; ganha-se que um commit em `T1.heap` não move a página 0 de `T2.heap` |
| `index_manager.py` (`:733-781`, `:867`) | certificado por ficheiro, frescor por tabela A1 contra `_table_high_water` num LSN único | `built_through_lsn` (`:1020`) e a cerca de rebuild passam a comparar contra o LSN **do shard da tabela**; o contrato ANN do Pulse que lê `view.built_through_lsn` recebe um número por shard |
| WAL / recovery (`wal_manager.py:1-33`; `CONTRACT.md:870-882`) | um log contíguo; recovery para no primeiro registro mau | `k` logs + log global; recovery tem de calcular o ponto de consistência global (maior commit global cujas escritas em **todos** os shards são duráveis), truncar cada shard a esse ponto e classificar no ledger cada registro descartado por shard (G8) |
| vacuum | não existe em `src` (grep = 0 ficheiros); candidato P2-VAC | horizonte de leitores por shard, ou global conservador |
| verify (`verifier.py:180`; `CONTRACT.md:898-920`) | uma caminhada física do heap contra a página 0 do heap e o catálogo | uma caminhada por shard mais a prova de que o log global e os logs de shard concordam; `orphan_page`/`table_unreadable` ganham a dimensão "shard" |
| backup | não há porta de backup no motor (grep `backup` em `database.py`/`__init__.py` = 0); cópia declarada por ferramenta (`tools/perf_round/board_copy.py`) | cópia consistente exige quiescência de **todos** os shards ou um marcador de snapshot global |
| `ports/coordination.py` (`CONTRACT.md:253-303`, assinaturas congeladas) | `acquire_writer_lease`, `validate_epoch`, `exclusive(name)` sem noção de shard | `Lease`/época por shard e uma época global, ou lease única global que devolve o paralelismo à estaca zero |
| `ports/storage.py` (`:192-240`) | namespace plano por ficheiro | intacto em assinatura; muda o que cada ficheiro significa (G6 tem de listar os novos ficheiros que nunca podem ser destruídos) |

**Onde (a) pode paralelizar de verdade:** só transações cujo **conjunto de escrita** cabe em um shard. É preciso distinguir read-set de write-set: uma criação de aresta no Pulse **lê** as duas tabelas de nó para validar as extremidades, mas o que **escreve** pode ser só a família da relação e os seus índices, o que a tornaria mono-shard sob (a); um update de nó escreve heap e índices da mesma tabela. A fração `s` de transações mono-write do fluxo real é `[A_MEDIR]` com o censo P0.3, não presumível, e a validação de extremidades sob snapshot continua a exigir uma vista física conjunta entre shards (secção 3.3), que é custo de leitura, não de lease.

### 1.2 Desenho (b) — shard por partição de hash da identidade dentro de um board (vários heaps/WALs sob um catálogo)

A chave natural existe: `partition_of(table_id, key)` (`txn_manager.py:987`). O desenho promove essa partição lógica a partição física: `k` heaps e `k` WALs; a tabela de uma linha continua a mesma, mas a sua página vive no heap do seu hash.

**O que muda:** tudo o que muda em (a), mais:

- `heap_store.py` perde a cadeia canónica única por tabela (`_find_extent`, `_walk`, `lookup`, `scan`): cada tabela passa a ter `k` cadeias; a ordem pública de varredura, que a rodada declarou intocável (`GRAFX_PERFORMANCE_ROUND_FINAL.md:82`, D-02), muda de definição.
- Identidade CN-1 (faixas duráveis de `record_id` por extent, `CONTRACT.md:803-813`) passa a ter `k` pisos por tabela; o verifier, que impõe `next_record_id > max(record_id)` por tabela (`:904-909`), tem de somar `k` cadeias.
- Uma aresta `A -> B` com `hash(A) != hash(B)` toca dois shards; qualquer `MATCH` que pousa num nó toca todos. **Quase toda transação é cross-shard**, portanto quase todo commit paga o protocolo global e nenhum ganha paralelismo.

Este desenho é o que o LadybugDB faz em memória com 256 shards do `PrimaryKeyIndex` (`FABLE:431`), e o FABLE já o classificou como transferível apenas como **índice hash durável**, não como partição de armazenamento. O caminho correto para o problema que (b) tenta resolver (lookup por identidade em O(N)) é o P2-ID da rodada (`GRAFX_PERFORMANCE_ROUND_FINAL.md:236`).

### 1.3 Desenho (c) — sharding lógico na camada de aplicação (vários boards independentes, roteamento no Pulse)

**O motor não muda.** O Pulse já tem essa topologia: capability `per-board-graph-store` (`okto-pulse-perf-st2/src/okto_pulse/community/adapters/capability_descriptors.py:117,147`), diretório de escritor único por board (`coordination.py:852`), `CommunityGraphRouteResolver` (`graph_route_resolver.py:172`), composição roteada por board (`routed_board_graph_composition.py:644`) e um grafo global roteado (`routed_global_graph_composition.py:1411`) `[LIDO na árvore `okto-pulse-perf-st2`]`.

Sharding "a mais" significaria partir **o grafo de um board** em vários diretórios Grafx (por família de entidade ou por hash). O Grafx não tem consulta entre bases; toda travessia que hoje é um `MATCH` no motor viraria junção em Python no Pulse, ou seja, mais lenta que o executor que a rodada está a otimizar. E como o escritor é único por board por decisão do usuário, não há lease disputada a dividir.

**Ganho de (c) no fluxo atual: 0.** O único ganho de (c) é o que já existe: boards diferentes em paralelo.

---

## 2. Ganho: onde está o tempo hoje e o que cada desenho pode remover

### 2.1 Evidência versionada

| Regime | Número | Fonte |
|---|---|---|
| Backfill real do Pulse, 1 escritor por board | CPU do worker ~91 % de um núcleo; heap 4,6 MB, índices 85,7 MB pré-alocados (161 × 65 páginas), WAL +0,45 MB/14 min; "não é I/O nem espera de lock"; custo por operação cresce com N | `FABLE_PERFORMANCE_GRAFX.md:153-168` `[MEDIDO, contaminação declarada]` |
| Mono-escritor, Windows | commit 95–112 ms de mediana | `docs/PERFORMANCE.md:116-118` `[MEDIDO]` |
| 4 escritores + 3 leitores, mesma tabela | 313 ms mediana / 5,1 s p99 disjunto; 328 ms contendido; 10,9 rows/s agregado; 254 conflitos retentados; leitores 134,8–173,9 stmt/s sem bloqueio | `docs/PERFORMANCE.md:88, :109-124` `[MEDIDO]` |
| Publicação de ficheiro de controlo | ~16,5 ms por commit no Windows contra ~0,13 ms no Linux (`CreateFileW` 11,4 ms) | `docs/PERFORMANCE.md:214-215` `[MEDIDO]` |
| fsync por operação | 9 → 6 (CE-1) → 4 | `docs/PERFORMANCE.md:255, :284` `[MEDIDO]` |
| Grafx vs Ladybug | 79×/82× no Windows; 5,65× no WSL2/ext4, teto D5 ≤ 10× cumprido | `docs/PERFORMANCE.md:210` `[MEDIDO]` |
| Anatomia do commit | lease CAS + fsync → `COMMIT_SECTION` → OCC-1 decodifica todo COMMIT acima do snapshot → materializa → `_build_records` → append → barrier → apply → flush → índices → publish; no Windows o hold é dominado por publicação/fsync/OCC em Python | `FABLE:100-106` `[LIDO]` |
| Materialização de imagem WAL | 5.009 → 2.031 µs/página em máquina carregada (D-09) | `docs/PERFORMANCE_ROUND_0_0_2.md:254-255` `[MEDIDO-micro]` |
| Janelas de lease/secção e 10 fases | instrumentadas (D-26), integradas em `7aa410d`/`59020a4`; números na cópia do Pulse ainda `[A_MEDIR]` (P0.3/P0.4) | `docs/PERFORMANCE_ROUND_0_0_2.md:181-195` |

### 2.2 Leitura dos números

- **No Pulse, o tempo está em CPU de leitura**: `require_endpoints` com dois lookups lineares por aresta, pouso de traversal por varredura completa, decode duplo por acerto de índice, `flush`/`modified_pages` O(frames) (`GRAFX_PERFORMANCE_ROUND_FINAL.md:10-16`). Nenhum desses custos depende de quantos escritores existem, e nenhum shard os encurta: uma varredura de `Person` tem o mesmo número de páginas dentro ou fora de um shard.
- **No harness multi-escritor, o tempo está na fila da lease**: 313 ms contra 95–112 ms é ~3× de espera, e o agregado de ~2,2 commits/s (10,9 rows/s ÷ 5 linhas) implica um hold efetivo por commit `[HIPÓTESE aritmética]` da ordem de 450 ms, maior que o commit mono-escritor. A diferença é custo **induzido** por concorrência: OCC-1 decodifica os COMMITs estrangeiros, retargets, e cada processo reconstrói o working set no próximo `pin` após um token estrangeiro (`FABLE:98`). É este custo induzido, não o hold intrínseco, que sharding por ficheiro pode reduzir, e só quando os escritores tocam **ficheiros distintos**.

### 2.3 Fração removível por desenho (teoria, Amdahl)

Seja `H` o hold da lease por commit, `W` o trabalho fora da lease, `s` a fração de transações confinadas a um shard, `k` o número de shards, `g` o custo adicional do commit global por transação cross-shard.

- Throughput por board hoje: `≤ 1/H` (I-3).
- Sob (a): `≤ 1 / [ (1-s)·(H+g) + s·H/k ]`, com `s` medido sobre o **write-set** (`[A_MEDIR]`, censo P0.3). Se `s → 0` o teto **piora** para `1/(H+g)`; se `s` for alto o ganho existe. Com `s = 0,7` e `k = 4` `[HIPÓTESE ilustrativa]`, o ganho máximo é `1/(0,3·(H+g) + 0,175·H) ≈ 2,1×` se `g = 0`, e cai abaixo de 1,5× quando `g ≥ 0,5·H`. Se `g` atinge essa ordem no Windows é `[HIPÓTESE a medir]`: o número medido (16,5 ms) é de publicação de ficheiro de controlo, não de barreira de WAL.
- Sob (b): `s ≈ 0` por construção; teto `1/(H+g) < 1/H`.
- Sob (c): `k` boards já dão `k/H` hoje; partir um board não altera `H` e adiciona junções em Python.
- **Caminho de leitura**: fração removível por sharding = 0 em (a) e (c); em (b) um lookup por identidade ficaria O(N/k), que o P2-ID entrega melhor sem formato distribuído.

### 2.4 O que P1/P2 já capturam (para não contar duas vezes)

| Trilha | O que remove | Interseção com sharding |
|---|---|---|
| D-26 | nada; mede lease/secção/fases | é o instrumento que decide se sharding tem base |
| D-01, D-04 | CPU por header/versão rejeitada e decode duplo por acerto | nenhuma |
| D-02' (locator), D-03' (pouso R1) | lookups O(E·N) → O(N+E) por transação; pouso por identidade | nenhuma; (b) tornaria a cadeia canónica `k` cadeias e invalidaria o locator |
| D-09 | materialização dentro da lease (encode/decode/encode → copy/encode) | reduz `H`; sharding não |
| P2-ID | lookup por identidade sem varredura | absorve a única motivação legítima de (b) |
| P2-DIRTY (D-10) | `flush`/`modified_pages` O(frames) dentro da lease | reduz `H`; pools por shard só o fariam por acidente |
| P2-VAC | versões mortas nas varreduras (~75 % dos headers, `FABLE:94`) | nenhuma |
| CE-1 e sequência 9 → 4 fsync | publicações por commit | (a)/(b) **reintroduzem** publicações e barreiras por shard |

---

## 3. Custo por dimensão

### 3.1 Durabilidade

Hoje: G1 "barreira antes de aplicar, aplicar antes de publicar" (`FABLE:287`), BR-4 "nenhum ack antes de `wal.barrier()`" (`CONTRACT.md:840`), uma barreira do WAL (`txn_manager.py:2921`) mais a barreira do `commit.state` (`commit_state_store.py:131-133`).

Sob (a)/(b), um commit que toca `m` shards precisa de: `m` appends + `m` barreiras de WAL de shard, **e** um registro de commit global durável antes do ack (2PC com o log global como coordenador, ou commit-record global com "presumed abort"). Sem o registro global, uma queda entre a barreira do shard 1 e a do shard 2 deixa metade da transação durável e visível ao recovery de um shard e ausente no outro. Custo: `m+1` pontos de durabilidade por commit no lugar de 1–2. O que está medido na plataforma principal é a publicação do **ficheiro de controlo** (~16,5 ms, `docs/PERFORMANCE.md:214`), que não é o custo de uma barreira de WAL; se a latência de um commit em 2 shards supera a de um commit hoje depende do custo real de cada barreira adicional e do protocolo escolhido, e é `[HIPÓTESE a medir]`, não conclusão.

Alternativa "um WAL, vários heaps" (a variante barata da secção 4): mantém uma barreira e um registro de commit; perde o paralelismo de escrita, ganha apenas relógios de frescor por ficheiro.

### 3.2 Recuperação

Hoje: varredura única a partir de `checkpoint_lsn`, paragem no primeiro registro inválido, quarentena com manifesto, ledger por registro descartado (G8), redo em ordem de LSN, sem undo porque páginas só são escritas no commit (`CONTRACT.md:870-882`).

Sob (a)/(b): o recovery tem de (1) varrer `k` logs de shard e o log global; (2) calcular o ponto de consistência global: o maior commit global cujas imagens em **todos** os shards estão duráveis; (3) truncar cada shard a esse ponto, inclusive registros de shard com CRC válido que pertencem a commits globais não duráveis, e classificá-los no ledger como `REAPPLICABLE` com origem cross-shard; (4) manter G6 para `k` heaps e G8 para `k` ledgers ou um ledger com dimensão shard. "Rollback parcial" não existe hoje porque não há undo. Com shards, o modelo redo-only **pode ser preservado** se os registos preparados (duráveis num shard) nunca forem aplicados às páginas antes do commit global: como as páginas só são escritas no commit, um commit global abortado após a barreira de um shard deixa imagens **logadas mas não aplicadas**, que o recovery descarta e quarentena em vez de desfazer. Não é undo físico por necessidade; é um protocolo novo de descarte/quarentena/recovery (classificação no ledger, G8; "só `truncate_after` destrói bytes", `FABLE:287`, G9) que hoje não existe e que o verifier e os testes de recovery não conhecem.

### 3.3 Estabilidade e isolamento

- **Snapshot global vs por shard.** `Snapshot.read_lsn` é um inteiro (`CONTRACT.md:775-779`). Com `k` WALs há duas opções: um alocador de CSN global sob uma secção curta (serializa a atribuição do número, não necessariamente o commit inteiro; o custo real é a coordenação/commit global e a atomicidade cross-shard) ou um vetor de LSN por shard com regra de visibilidade cross-shard e um `commit.state` vetorial. A segunda opção muda `Snapshot`, `commit.state`, os certificados dos índices (`_table_high_water` passa a ser por shard) e o contrato `built_through_lsn` que o Pulse consome.
- **OCC entre shards.** Hoje o passo 3.3 compara interesses contra todo COMMIT com `lsn > read_lsn` num único log (`CONTRACT.md:825-843`). Com `k` logs, OCC-1 lê `k` caudas e o conjunto de interesses tem de ser validado sob **todas** as leases tocadas, em ordem total, ou sob a secção global; uma janela em que o shard 1 já validou e o shard 2 ainda não é exatamente a anomalia que `COMMIT_SECTION` hoje impede (`:840-841`).
- **Leitores multi-processo.** Registo de leitor e horizonte são por board (`CONTRACT.md:293-299`); o horizonte de reciclagem BR-10 (`:750-751`) teria de ser o mínimo entre shards, senão um leitor com snapshot antigo no shard 2 vê o shard 1 reciclado.
- **Certificados pré/pós-leitura.** Intactos por ficheiro; a prova "resultado nunca atravessa duas vistas físicas" (`query_engine.py:3956-3964` em `51bc7c4`) passa a ser por shard e a consulta que atravessa dois shards precisa de uma vista física conjunta, que não existe.

### 3.4 Operação

- `verify(scope)` (`verifier.py:180`; `database.py:1017`, `:1967`) e `recover()` (`:1021`, `:2279`) por shard mais a verificação de concordância entre logs; `checkpoint()` (`:1013`, `:2349`) tem de publicar um checkpoint por shard e um global.
- Backup: hoje é cópia de diretório em quiescência (não há porta no motor); com shards precisa de marcador de snapshot global ou quiescência de todos.
- Attach de índices e o contrato de frescor A1 ganham a dimensão shard; o defeito de "cold-open page-0 advance" corrigido em P0.1 mostra como um relógio de página 0 a mais é uma superfície de erro a mais.

### 3.5 Formato e migração

`format_version` em `grafx.meta` (`CONTRACT.md:551-556`) já governa v1 → v2 com migração durável em dois passos. Sharding exige um `format_version = 3`, novos ficheiros, migração de boards existentes (o board de referência tem 161 índices e um heap único) e regra de recusa em builds antigos. A rodada proíbe alteração de formato sem ADR, compatibilidade de frota e autorização (`GRAFX_PERFORMANCE_ROUND_FINAL.md:69`). Para (c) não há migração de formato, mas há migração de dados do Pulse (repartir boards existentes).

### 3.6 Superfície de testes `[MEDIDO por grep em `origin/feature/v0.0.2@b2aa2ba`]`

| Universo | Valor |
|---|---|
| Ficheiros de teste | 261 |
| Funções `def test_` | 5.455 |
| Testes marcados `pytest.mark.multiprocess` | 26 |

Ficheiros de teste que mencionam premissas que (a)/(b) mudariam (contagem de ficheiros; a segunda coluna é linhas que casam, não funções):

| Premissa | ficheiros | linhas |
|---|---:|---:|
| `lease` | 125 | 1.525 |
| `wal`/`WAL` | 196 | 2.210 |
| `catalog` | 118 | 1.344 |
| `snapshot` | 120 | 861 |
| `recover` | 98 | 612 |
| `certificate`/`stale`/`rebuild` | 82 | 827 |
| `epoch` | 66 | 589 |
| `quarantine`/`ledger` | 66 | 985 |
| `verify(` | 53 | 314 |
| `multiprocess`/`spawn` | 30 | 128 |
| `HEADER_PAGE_INDEX`/`page 0` | 20 | 141 |
| `COMMIT_SECTION`/`LEASE_SECTION` | 14 | 32 |

Leitura conservadora: entre 100 e 200 dos 261 ficheiros tocam pelo menos uma premissa que (a) ou (b) redefinem; os 26 testes multiprocesso e os testes de recovery/quarentena/ledger teriam de ser reescritos, não adaptados. Para (c) a superfície no Grafx é 0 e a superfície no Pulse é a das composições roteadas.

---

## 4. Complexidade por desenho e invariantes tocados

Premissas de estimativa `[HIPÓTESE]`: Python puro sob G2/G3; paridade Windows/POSIX (G4) com testes espelhados; cada item P1 desta rodada custou 1–3 dias entre implementação, revisão adversarial e integração (`docs/PERFORMANCE_ROUND_0_0_2.md:267-283`); uma alteração de protocolo congelado custa uma ordem de grandeza mais que um item P1 por causa de recovery, fault injection, multiprocesso e verify; nenhum número aqui foi calibrado contra um projeto de sharding real.

| Desenho | Motor | Pulse | Testes | Total (pessoa-semanas) | Ganho esperado |
|---|---|---|---|---|---|
| (a) shard por tabela com lease/WAL/página 0 próprios | commit global, `k` WALs, recovery multi-log, snapshot/CSN, índices por shard, verify, meta v3, migração: 10–18 | contrato `built_through_lsn` por shard, recuperação pública de claims: 2–4 | reescrita multiprocesso/recovery/ledger/verify + fault injection cross-shard: 4–8 | **16–30** | > 1 só com `s` (write-set) alto e `g` pequeno; `s` e `g` são `[A_MEDIR]`; que no Windows tenda a < 1 é `[HIPÓTESE]` |
| (b) shard por hash de identidade | tudo de (a) + `k` cadeias por tabela, CN-1 por shard, ordem de varredura, locator/pouso, verifier de identidade: 14–24 | 2–4 | 6–12 | **20–40** | negativo (`s ≈ 0`) |
| (c) sharding lógico no Pulse | 0 | roteamento intra-board, junções cross-shard em Python, migração de boards: 2–6 | Pulse: 1–2 | **2–6, fora do Grafx** | 0 no fluxo atual |
| variante: heap por tabela sob um WAL, uma lease, um LSN (não é sharding) | `heap_store` por ficheiro, extents no catálogo, G6, meta v3, migração, verify: 3–5 | 0–1 | 1–2 | **4–8** | `[A_MEDIR]`: só retenção de cache dos leitores quando o escritor toca outras tabelas; nenhum ganho de throughput de escrita |

Invariantes e contratos tocados (`docs/architecture/CONTRACT.md`):

- (a) e (b): §4 assinaturas congeladas das portas `storage`/`coordination` (`:188-303`); §6.1 layout de ficheiros (`:523-536`); §6.2 `format_version` (`:545-556`); §8.3 WAL contíguo e reciclagem por horizonte BR-10 (`:730-751`); §8.5 protocolo de commit congelado, passos 2–4, BR-4, BR-6, BR-7 (`:772-854`); §8.6 algoritmo de recovery congelado e ledger G8 (`:856-893`); verifier de identidade e propriedade (`:898-920`); G6 ficheiros principais intocáveis (`:22`); I-1..I-4 e G1..G9 da consolidação do FABLE (`FABLE:281-291`); a lista de invariantes da rodada (`GRAFX_PERFORMANCE_ROUND_FINAL.md:58-72`), em especial "não alterar formato sem ADR" e "não deslocar OCC, lease/epoch, WAL barrier, apply, publish ou recovery".
- (b) adicionalmente: ordem pública de varredura e `lookup` intocados (`GRAFX_PERFORMANCE_ROUND_FINAL.md:82`, D-02); CN-1 (`CONTRACT.md:803-813`).
- (c): nenhum invariante do Grafx; toca a serialização por board do Pulse só se o Pulse passar a rotear um board para vários stores, o que o usuário reservou para si (`GRAFX_PERFORMANCE_ROUND_FINAL.md:64`).
- variante heap por tabela: §6.1, §6.2, G6, verifier de propriedade (`:911-920`), `_find_extent`/extents; **não** toca §8.3, §8.5, §8.6 nem as portas.

---

## 5. Critério de decisão

### 5.1 Quando sharding valeria a pena, depois de P1/P2

Todas as condições, medidas na cópia declarada com a instrumentação D-26 e o harness de concorrência de `docs/PERFORMANCE.md §2`, ≥ 3 corridas, `strict`/`generation` separados:

1. **Existe regime multi-escritor por board no produto.** Se o Pulse mantiver um escritor por board, a condição falha e sharding do motor não entra, ponto.
2. **A fila domina.** `writer_lease` wait mediano ≥ 2× o `writer_lease` hold mediano com ≥ 2 escritores concorrentes, e o hold mediano após P1/P2 ainda ≥ 50 ms na plataforma principal.
3. **O hold não é plataforma.** As fases `publish` + `barrier` + `append` do D-26 somam < 40 % do hold; se somarem mais, o teto é o custo de publicação do Windows (`docs/PERFORMANCE.md:214`) e sharding o multiplica.
4. **As transações são mono-shard.** No censo P0.3 do fluxo do Pulse, ≥ 70 % das transações escrevem numa única tabela-família candidata a shard.
5. **A leitura já não domina a parede do card.** Após P1/P2, o caminho de leitura (endpoint, pouso, índice) < 30 % da parede do card no perfil pós-drain; enquanto for maior, qualquer ganho de escrita é invisível (Amdahl).

Se (1)–(5) forem verdadeiras, a opção a desenhar por ADR é (a), nunca (b); (c) não precisa de ADR e o Pulse pode fazê-la sozinho se aceitar junções em Python.

### 5.2 O que medir primeiro, sem escrever sharding

- Séries D-26 no harness de `docs/PERFORMANCE.md §2` com 1, 2 e 4 escritores: `writer_lease` wait/hold, `commit_section` wait/hold e as 10 fases; contadores de foreign commits, retargets e frames examinados.
- O mesmo harness com escritores em **tabelas disjuntas** contra a **mesma tabela**: se o hold por commit e a espera não mudarem, o custo induzido pela concorrência não é por ficheiro e (a) não o remove.
- Censo P0.3 (`pulse_graph_census*.py`) para a fração de transações mono-tabela do fluxo real.

### 5.3 Experimento barato: "serialização de escrita ou custo por página?" — backlog opcional pós-rodada

**Estatuto (consenso Codex/Claude):** não é tarefa da rodada atual. Os gates de performance foram retirados da rodada e não se abre alvo móvel; este experimento fica em backlog opcional, a correr depois do fecho, só se o usuário o pedir.

Sem tocar no motor, na cópia declarada, três braços com o mesmo total de commits e as mesmas linhas:

| Braço | Configuração | O que isola |
|---|---|---|
| A | 1 board, 1 escritor | hold intrínseco `H` e custo por página |
| B | 1 board, 2 escritores em tabelas disjuntas | custo induzido por concorrência dentro de um relógio de frescor único |
| C | 2 boards (dois diretórios), 1 escritor cada | o teto de **qualquer** sharding: é o desenho (c) já disponível hoje |

Leitura: se `throughput(C) ≈ 2 × throughput(A)` e `throughput(B) ≪ throughput(C)`, a serialização por board custa e (a) tem um teto real igual a C. Se `throughput(C) ≈ throughput(B) ≈ throughput(A)`, o limite é CPU por página ou o disco/publicação da plataforma e nenhum sharding ajuda. Se `throughput(C) < 2 × throughput(A)`, a máquina (CPU, disco, `CreateFileW`) já satura e o teto é inferior ao que sharding promete. Custo: horas, usando `tools/perf_round/baseline_runs.py` para isolar data home por corrida e os recibos de `tools/perf_round/receipt.py`.

---

## 6. Recomendação final e perguntas ao usuário

**Recomendação e consenso Codex/Claude (2026-09-03).** Sharding do motor (a)/(b) **não entra** nesta iniciativa de performance nem no seu fecho; fica registado na fila estrutural com o critério de entrada da secção 5.1, atrás de P2-ID/P2-DIRTY/P2-VAC. (b) é recusado em definitivo, porque o seu único mérito é entregue pelo P2-ID sem formato distribuído. (c) já é a topologia do Pulse e não precisa de nada do Grafx. A variante "heap por tabela sob um WAL, uma lease e um LSN" não é sharding, muda formato e só valeria se o experimento 5.3 mostrasse custo induzido por concorrência dentro do mesmo board; esse experimento é backlog opcional pós-rodada, não tarefa desta rodada. As estimativas de esforço e de ganho continuam `[HIPÓTESE]`; `s` (fração mono-write) e `g` (custo do commit global) são `[A_MEDIR]`.

**Perguntas que só o usuário pode responder.**

1. O "um escritor por board" do Pulse é permanente ou existe intenção de vários workers escreverem no mesmo board? Sem a segunda hipótese, sharding do motor não tem alvo.
2. Uma mudança de formato (meta v3, ficheiros novos, migração de boards existentes) é autorizável na linha 0.0.2, ou só numa linha maior com ADR e compatibilidade de frota?
3. Travessias que cruzam tabelas têm de continuar a ser respondidas pelo motor num único `MATCH`? Se sim, (c) intra-board está descartado e (a) exige vista física conjunta entre shards, que é o item mais caro da lista.

---

## Revisões

- **2026-09-03, r1** — versão inicial (agente único, somente leitura).
- **2026-09-03, r2** — emenda após revisão adversarial do Codex (handoff `hof_6ca6d9e8`), registando o consenso "não entra nesta rodada nem no fechamento" e corrigindo quatro sobreafirmações: (1) criação de aresta lê duas tabelas de nó mas pode escrever só a família da relação e índices; `s` é a fração mono-**write** e é `[A_MEDIR]`, não presumida ~0 (secções 0.4, 1.1, 2.3, 4); (2) os 16,5 ms medidos são de publicação de ficheiro de controlo e não provam custo igual por barreira de WAL; a multiplicação de custo é `[HIPÓTESE a medir]` (0.7, 2.3, 3.1); (3) multi-WAL pode preservar redo-only se os prepares duráveis não forem aplicados antes do commit global; o custo é um protocolo novo de descarte/quarentena/recovery, não undo físico por necessidade (3.2); (4) o alocador global de CSN serializa uma secção curta, não o commit inteiro; o custo real é coordenação/commit global e atomicidade cross-shard (0.8, 3.3). O experimento 5.3 passa a backlog opcional pós-rodada (0.1, 0.10, 5.3, 6). Estimativas mantidas como `[HIPÓTESE]`.

---

## Anexo — fontes lidas

- `docs/architecture/CONTRACT.md`: `:12-27` (G1–G8), `:188-303` (portas), `:523-556` (ficheiros e meta), `:700-751` (pool, heap, WAL), `:772-854` (commit), `:856-921` (recovery, ledger, verifier).
- `src/okto_grafx/engine/txn_manager.py@b2aa2ba`: `:729`, `:987`, `:2680-2696`, `:2921`, `:5244-5256`.
- `src/okto_grafx/engine/buffer_pool.py@b2aa2ba`: `:367`, `:551`, `:1360`, `:1563-1600`; `@51bc7c4`: `:407-446`.
- `src/okto_grafx/engine/index_manager.py@b2aa2ba`: `:358`, `:362`, `:733`, `:757`, `:781`, `:867`, `:1020`.
- `src/okto_grafx/engine/wal_manager.py@b2aa2ba`: `:1-47`, `:1314`; `commit_state_store.py:131-133`; `verifier.py:180`; `database.py:1013-1021, :1967, :2279, :2349`.
- `src/okto_grafx/engine/query_engine.py@51bc7c4`: `:3939-3994`.
- `docs/PERFORMANCE.md`: `:2`, `:71-72`, `:88`, `:109-124`, `:138`, `:210-215`, `:245-290`.
- `docs/PERFORMANCE_ROUND_0_0_2.md`: `:181-195`, `:197-219`, `:235-257`, `:267-281`.
- `GRAFX_PERFORMANCE_ROUND_FINAL.md`: `:8-26`, `:58-72`, `:84`, `:171-241`, `:243-264`.
- `FABLE_PERFORMANCE_GRAFX.md`: `:90-106`, `:153-168`, `:281-291`, `:431`.
- `GRAFX_COMPLEMENTARY_EVOLUTION_PLAN_CODEX.md:2180-2182`.
- `okto-pulse-perf-st2/src/okto_pulse/community/adapters/`: `capability_descriptors.py:117,147`, `coordination.py:852`, `graph_route_resolver.py:172`, `routed_board_graph_composition.py:644`, `routed_global_graph_composition.py:1411`.
- Censo de testes: `git grep` em `origin/feature/v0.0.2@b2aa2ba`, `tests/`.
