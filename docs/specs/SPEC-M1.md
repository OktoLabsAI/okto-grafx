# SPEC M1 — Núcleo transacional do Okto Grafx: MVCC multi-processo, WAL verificável, ledger de trabalho não-aplicado e recuperação provada

> Spec ID `780475bb-f8eb-57a0-9411-3dffaf8feeeb` · board `c5f05299-fcb5-41f3-bac9-ba7faf90fb16` · status **validated** · edition 2 · version 84

## Descrição

Especificação executável do marco M1 do Okto Grafx (repositório `D:/Projetos/Techridy/okto_grafx`, pacote `okto_grafx`): o motor transacional que prova, antes de existir qualquer superfície de consulta (D4), as duas garantias que motivaram o projeto — concorrência correta por desenho (D1: N processos leem E escrevem o mesmo banco) e integridade verificável com recuperabilidade total do descartado (D7).

O escopo realiza as decisões D1–D9 do JP e as 8 decisões de refinamento (DR-1..DR-8), com as 4 resoluções de RDL da edição 4 como âncoras de desenho: validação otimista por partição configurável sobre registro AUTO-DESCRITIVO (dissolve a irreversibilidade de formato), escrita concorrente real entre processos com fallback formalizado que retorna ao JP, regra DUAL de visibilidade de índice secundário, e ledger dual-form por classe de origem.

A entrega é gated por CALIBRAÇÃO: o primeiro card do DAG mede o baseline do Ladybug 0.16 nas operações representativas do KG do Okto Pulse e aplica os tetos de D5 (≤10× commit durável, ≤5× leitura pontual, ≤3× abertura com replay) como gate de CI; nenhum outro card de implementação inicia antes da calibração verde. Plataformas-alvo por D9: Windows (NTFS) e POSIX (Linux ext4/macOS APFS) como cidadãos iguais — a bancada inteira roda nas duas famílias.

Todas as superfícies de PRODUTO são en-US (guideline do board): nomes de API, exceções (`GrafxWriteConflict`, `GrafxStaleEpoch`, ...), métricas (`oktografx_*`) e mensagens de erro. Esta spec, como artefato de colaboração, é redigida em pt-BR.

## Contexto

Cadeia de derivação: ideação d6d69798 (DONE ed.4, decisões D1–D9) → refinamento f615fec0 (DONE ed.4, 6 threads de RDL terminais: 5 resolved + 1 deferred com rationale). Varredura Stage 3 do KG executada em 2026-08-19 sobre spec:780475bb: `kg_find_contradictions` = 0 pares; `kg_get_related_context` retorna o vizinho esperado (o refinamento pai e suas decisões de RDL consolidadas como nós Decision no layer canônico) e os bindings dos 4 guidelines do board; `kg_find_similar_decisions` para "commit durável com WAL segmentado, validação otimista por partição e ledger de trabalho não-aplicado" retorna exatamente as resoluções de RDL desta cadeia (decision_6c3e9197, decision_974d5e17, decision_f35f70dd, decision_ea4fe6d7, decision_72c93a34) — ou seja, nenhuma decisão anterior conflitante e nenhuma duplicação: as âncoras canônicas são as desta linhagem. Evidência de código herdada: 6 registros atestados sobre o motor de referência (LadybugDB 0.16) e os adaptadores do Okto Pulse, todos linkados às entidades normativas desta spec. Constraint externa citada: nenhuma (os constraints de KG citados são as próprias decisões da linhagem).

## Functional Requirements

- **`fr_1505781e`** — FR-1 Abertura e identidade: okto_grafx.Database(path) cria ou abre o diretório do banco com metadados de identidade (UUID do banco) e época; database_path=':memory:' usa o adaptador StorageDevice em memória com a MESMA semântica transacional. Reabertura executa a recuperação de FR-8 antes de aceitar transações. Fechar o banco libera lease e registro de leitor; fechar com transação ativa aborta a transação, nunca corrompe.
- **`fr_9baa7040`** — FR-2 Transações com snapshot isolation: db.begin(mode='read'|'write') abre transação; leitor enxerga o snapshot consistente do instante de abertura durante toda a transação, mesmo com commits paralelos; leitores nunca bloqueiam escritores nem escritores bloqueiam leitores; commit e rollback explícitos.
- **`fr_1dab37f2`** — FR-3 Escrita concorrente multi-processo (D1): N processos E N threads mantêm transações de escrita simultâneas no mesmo banco; commits cujos conjuntos de partição não se intersectam confirmam TODOS; conflito real falha com GrafxWriteConflict (retryable) sem efeito colateral.
- **`fr_02544e01`** — FR-4 Detecção de conflito: validação otimista no commit por conjunto de leitura/escrita na granularidade de PARTIÇÃO (hash-bucket de chave por tabela; parâmetro partitions_per_table). O registro de transação em disco é AUTO-DESCRITIVO: carrega versão de formato e descritor de granularidade; mudar o parâmetro NÃO exige migração de formato.
- **`fr_41ca5e27`** — FR-5 Commit durável: commit só retorna sucesso após append no WAL e durable_barrier() com êxito; cada registro do WAL carrega comprimento, LSN monotônico, checksum CRC-32C e época do portador; NENHUM caminho de código executa escrita posicional além do EOF (a porta StorageDevice não expõe a primitiva).
- **`fr_4d11566d`** — FR-6 WAL segmentado com reciclagem por horizonte: segmentos append-only; um segmento é reciclável quando seu LSN final é menor que o menor LSN de snapshot ativo registrado (nunca por ausência de leitores, nunca por expulsão de leitor); no Windows a reciclagem usa rename para pending-delete com retry, nunca assume unlink de arquivo aberto.
- **`fr_a5afa250`** — FR-7 Coordenação entre processos: lease durável de escritor com renovação periódica, registro de leitores vivos com o LSN de snapshot de cada um, detecção de dono morto por liveness em tempo monotônico local (jamais comparação de relógio de parede entre processos), takeover seguro com incremento de época; qualquer escrita de portador de época anterior é rejeitada no ato com GrafxStaleEpoch, antes de qualquer byte chegar ao dispositivo.
- **`fr_a337225f`** — FR-8 Recuperação na abertura: replay do WAL até o último registro íntegro como comportamento DEFAULT, produzindo relatório {outcome: clean|truncated|quarantined, records_replayed, records_discarded, ledger_entries_created}; a política fail-closed (recusar abertura em corrupção) existe APENAS como opção explícita recovery_policy='refuse', nunca default.
- **`fr_ec882001`** — FR-9 Ledger de trabalho não-aplicado (D7): todo registro descartado INVOLUNTARIAMENTE (cauda truncada, checksum falho, época obsoleta) gera entrada no ledger com classificação: REAPLICÁVEL (operação lógica decodificada; origem íntegra) ou FORENSE (bytes brutos + offset + LSN esperado + motivo + digest; checksum falho). APIs: ledger.list(filtros), ledger.inspect(id), ledger.reprocess(id) — só reaplicáveis, idempotente por LSN de origem, exatamente-uma-vez — e ledger.export(id) para forenses; reprocess de forense falha com erro tipado; purga apenas por ato explícito do operador com flag dedicada.
- **`fr_be83578f`** — FR-10 Quarentena reversível: segmento suspeito é movido para quarentena com manifesto (o que, quando, por quê, digests); existe restore auditável; NENHUMA operação sancionada do motor move, renomeia ou apaga o arquivo principal de dados.
- **`fr_d3716857`** — FR-11 Verificação sob demanda: db.verify(scope='pages'|'records'|'indexes'|'all') percorre páginas, checksums de registro, heap e entradas de índice secundário, produzindo relatório legível por máquina com localização precisa de cada achado; executável em CI e pelo operador; banco limpo produz relatório vazio.
- **`fr_34a72686`** — FR-12 Contrato de índice secundário (D8a) + índice de referência: API de registro de índice no motor; TODO índice é coberto pelo WAL, declara sua classe de visibilidade (EXATO: entradas sem versão + validação obrigatória contra o heap; PROXIMIDADE: entradas versionadas + tombstone reconciliado pelo horizonte de snapshot, reconciliação transacional WAL-coberta) e é percorrido por verify(); M1 entrega um índice EXATO (hash) de referência sobre o heap mínimo provando o contrato ponta a ponta.
- **`fr_7560ec45`** — FR-13 Orçamento de memória por banco: cada Database tem orçamento próprio de páginas residentes; falha de alocação produz GrafxBufferBudgetExceeded (retryable) APENAS nas transações daquele banco; não existe cooldown global nem bloqueio de abertura de outro banco por pressão alheia.
- **`fr_fe73339d`** — FR-14 Métricas nativas: todos os comportamentos operacionais de M1 (lease, conflitos, fsync, checkpoint, WAL, checksums, replay, ledger, recuperações, orçamento) emitem pela porta MetricsSink; a instalação default expõe OpenMetrics (GET /metrics, text/plain version=0.0.4) e o repositório versiona o dashboard Grafana JSON que consome cada métrica; o adaptador no-op não aloca nem formata no caminho quente.
- **`fr_18f8eff7`** — FR-15 Harness de baseline e calibração (FASE 1 BLOQUEANTE do DAG): mede o Ladybug 0.16 e o Okto Grafx nas operações representativas do KG do Okto Pulse (upsert de nó, commit em lote, leitura pontual, abertura com replay de WAL de tamanho controlado); aplica os tetos de D5 (commit durável ≤10x, leitura pontual ≤5x, abertura com replay ≤3x) como gate de CI lendo os números da porta MetricsSink; calibra e congela o default de partitions_per_table; se o múltiplo de commit exceder 10x, o pipeline PARA em estado explícito 'consult JP' — a troca para lease justo só por Q&A com o JP (altera D1).
- **`fr_ad1c85ec`** — FR-16 Bancada de injeção de falhas: gêmeo determinístico do StorageDevice com seed reproduzível injetando crash em QUALQUER ponto de escrita, escrita parcial, reordenação, disco cheio e fsync mentiroso; as 4 assinaturas conhecidas do motor de referência (zeros interiores no WAL, writer obsoleto, replay com cauda ruim, morte durante checkpoint) são reproduções dirigidas PERMANENTES da suíte.

## Technical Requirements

- **`tr_d2652522`** — TR-1 Núcleo Python puro (D2): o pacote de domínio okto_grafx/domain usa exclusivamente a stdlib e NÃO contém sys.platform, os.name, open(), pathlib de IO, mmap, socket, threading concreto nem import de cliente; gate de import-boundary no CI com budget ZERO, falhando fechado.
- **`tr_7f464278`** — TR-2 Portas fail-closed: StorageDevice {read_page, write_page(página alocada), append_log, durable_barrier, allocate, atomic_replace} — deliberadamente SEM escrita posicional arbitrária; ProcessCoordinator {acquire_writer_lease, renew_lease, release_lease, register_reader, detect_dead_owner, takeover}; Clock {monotonic(), wall()} como contratos distintos — lease e liveness usam APENAS monotonic; PageCodec; MetricsSink {observe, increment, time}. Slot de porta não preenchido RECUSA o arranque com erro tipado.
- **`tr_448396a4`** — TR-3 Adaptadores por família de SO sob contrato idêntico (D9): LocalStorageDevice e LocalProcessCoordinator com implementação POSIX (fcntl advisory, fsync de arquivo E de diretório) e Windows (LockFileEx/msvcrt, FlushFileBuffers, os.replace exclusivamente, pending-delete com retry, retry curto com backoff em PermissionError de antivírus/indexador); identidade de arquivo nunca depende de case.
- **`tr_9335efbc`** — TR-4 Formato auto-descritivo: cabeçalho de registro de transação com {format_version, granularity_descriptor, epoch, lsn, crc}; decodificador versionado que lê qualquer format_version anterior; testes de round-trip por versão.
- **`tr_050bb06c`** — TR-5 Ledger: estrutura append-only durável com checksums próprios e entrada {id, origin_class: reapplicable|forensic, reason, lsn_range, epoch, captured_at, payload, digest}; a escrita no ledger é ela mesma WAL-coberta e sobrevive a crash durante a própria recuperação.
- **`tr_b417ac27`** — TR-6 Taxonomia de erros en-US, sempre exceção, nunca morte de processo: GrafxWriteConflict, GrafxLeaseTimeout, GrafxLeaseStolen, GrafxStaleEpoch, GrafxCorruptionDetected, GrafxDeviceFull, GrafxDurabilityBarrierFailed, GrafxRecoveryRefused, GrafxBufferBudgetExceeded, GrafxSchemaVersionMismatch — todas com mensagem en-US e campo retryable explícito; nenhuma falha de adaptador pode derrubar o processo hospedeiro.
- **`tr_9b992a0e`** — TR-7 Contrato de nomes de métrica: snake_case, prefixo oktografx_, sufixo de unidade (_seconds, _bytes, _total), descrição en-US; label com cardinalidade não limitada (id de nó, texto, caminho) é REJEITADO no registro da métrica, não em produção.
- **`tr_9c4f2ead`** — TR-8 CI em matriz dual obrigatória (D9): windows-latest E ubuntu-latest executando a suíte completa — bancada de injeção, testes multi-processo, harness de baseline e import-boundary; verde exige as DUAS famílias; teste que só roda numa família é marcado como cobertura parcial declarada.
- **`tr_42b78355`** — TR-9 Empacotamento: pacote okto_grafx com layout src/, wheel pure-python única, zero dependência de runtime além da stdlib; ladybug==0.16.0 e numpy são dependências exclusivamente de dev/teste (harness), isoladas em extra [bench].

## Acceptance Criteria

- **`ac_a787696d`** — AC-1 Dois processos escrevem em partições disjuntas e fazem commit simultaneamente: ambos confirmam; após fechar e reabrir o banco, os dois efeitos estão presentes; zero registros perdidos.
- **`ac_63378178`** — AC-2 Dois processos escrevem na MESMA partição e tentam commit: exatamente um confirma; o outro recebe GrafxWriteConflict com retryable=true; a retentativa do perdedor confirma e o estado final contém os dois efeitos em ordem serializável.
- **`ac_469e7987`** — AC-3 Um leitor em processo distinto abre snapshot antes de um commit paralelo: todas as leituras da transação refletem o estado pré-commit; nenhuma leitura mista/parcial é observável em nenhum interleaving testado.
- **`ac_dc1cfa8d`** — AC-4 Com crash injetado em CADA ponto de escrita do caminho de commit (bancada FR-16): a reabertura recupera até o último registro íntegro; nenhum commit CONFIRMADO se perde; todo registro descartado tem entrada correspondente no ledger; o relatório de recuperação bate com o que foi injetado (contagens exatas).
- **`ac_305fda28`** — AC-5 WAL sintetizado com runs de zeros interiores (assinatura NTFS do motor de referência): a recuperação trunca no último registro válido, cria entradas FORENSES no ledger com offset e digest, e o arquivo principal permanece intocado byte a byte.
- **`ac_2b7ad238`** — AC-6 Portador de época anterior tenta escrever após takeover: recebe GrafxStaleEpoch ANTES de qualquer byte chegar ao dispositivo (verificado pelo gêmeo de injeção); o payload da tentativa íntegra vira entrada REAPLICÁVEL no ledger.
- **`ac_469ee001`** — AC-7 Dono do lease morto por kill -9: um segundo processo detecta por liveness monotônica, executa takeover com época incrementada; teste de interleaving exaustivo (seed reproduzível) não encontra janela em que dois portadores escrevam sob a mesma época.
- **`ac_089e0705`** — AC-8 Leitor de vida longa segura APENAS os segmentos do seu horizonte: segmentos posteriores são reciclados normalmente; o uso de disco do WAL permanece limitado; a métrica oktografx_wal_truncation_lag_segments aponta o snapshot retentor; nenhum leitor é expulso.
- **`ac_6044def9`** — AC-9 No Windows (windows-latest), reciclagem de segmento com handle aberto por outro processo conclui via pending-delete com retry dentro de limite de tentativas; sem crescimento não-limitado; o mesmo teste passa no POSIX pelo caminho de unlink direto.
- **`ac_2289e9c5`** — AC-10 Disco cheio injetado no meio do commit: a transação aborta com GrafxDeviceFull; após reopen, NENHUM registro parcial é visível; o banco aceita novas transações quando há espaço.
- **`ac_6ca12107`** — AC-11 O gêmeo de injeção prova ordenação de durabilidade: nenhuma confirmação de commit é emitida antes de durable_barrier retornar; sob fsync mentiroso + crash, nenhum commit confirmado é silenciosamente perdido sem que a verificação o detecte.
- **`ac_b6e7d962`** — AC-12 verify() detecta cada classe de corrupção semeada (checksum de página, checksum de registro, divergência índice-heap) com localização precisa; banco limpo produz relatório vazio; ledger.reprocess de entrada forense falha com erro tipado; ledger.reprocess de entrada reaplicável aplica exatamente uma vez (repetir é no-op idempotente).
- **`ac_be26dde1`** — AC-13 Falha de alocação forçada no banco A (orçamento esgotado): transações do banco A recebem GrafxBufferBudgetExceeded; o banco B abre e confirma commits normalmente no MESMO processo, sem cooldown.
- **`ac_7f69d7dc`** — AC-14 O harness publica os 3 múltiplos de D5 via MetricsSink e o gate de CI falha se qualquer teto for excedido; a calibração congela partitions_per_table default com registro do valor; múltiplo de commit >10x põe o pipeline no estado 'consult JP' e NADA prossegue; toda métrica listada nas ORs é emitida e referenciada no dashboard JSON versionado; o import-boundary reporta zero violações; a suíte completa está verde em windows-latest E ubuntu-latest.

## Business Rules

### `br_26e5097b` BR-2 O ledger só recebe perda involuntária
- **Regra**: A fronteira semântica de D7: o ledger não é escape para falha evitável.
- **Quando**: Um caminho de código pode falhar sincronamente com exceção tipada
- **Então**: Ele falha sincronamente e NÃO escreve no ledger; apenas trabalho perdido involuntariamente (crash, corrupção, época obsoleta) entra no ledger, sempre classificado como reaplicável ou forense
- Requisitos: fr_ec882001

### `br_c4f529c1` BR-4 Durabilidade só após a barreira
- **Regra**: Nenhum commit é reportado durável antes de a barreira de durabilidade retornar sucesso.
- **Quando**: Um commit está sendo confirmado ao chamador
- **Então**: durable_barrier() retornou sucesso ANTES da confirmação; se a barreira falha, o chamador recebe GrafxDurabilityBarrierFailed e o commit não é reportado como durável
- Requisitos: fr_41ca5e27

### `br_872114a9` BR-1 Nenhuma operação sancionada destrói o arquivo principal
- **Regra**: Recuperação, quarentena, reciclagem e purga jamais tocam o arquivo principal de dados de forma destrutiva.
- **Quando**: Qualquer operação de recuperação, quarentena, reciclagem de segmento ou purga executa, em qualquer plataforma
- **Então**: O arquivo principal de dados nunca é movido, renomeado ou apagado; o pior desfecho possível é a perda de registros pós-checkpoint, que ficam preservados e recuperáveis pelo ledger e pela quarentena
- Requisitos: fr_a337225f, fr_be83578f

### `br_835194ce` BR-3 Descarte sem rastro é defeito
- **Regra**: A prova de recuperabilidade: relatar não basta, todo descarte tem entrada correspondente.
- **Quando**: A recuperação descarta um registro do WAL por qualquer motivo
- **Então**: Existe entrada correspondente no ledger com a classificação correta e o relatório de recuperação contabiliza o descarte; a ausência de entrada para um registro descartado é FALHA DE TESTE, nunca comportamento aceitável
- Requisitos: fr_a337225f, fr_ec882001

### `br_f41828e8` BR-5 Verde exige as duas famílias de SO
- **Regra**: D9 aplicada ao gate: aprovação numa plataforma não vale pela outra.
- **Quando**: A suíte, a bancada de injeção ou o harness passam numa família de SO e falham, não executam ou são pulados na outra
- **Então**: O gate de CI falha; verde exige windows-latest E ubuntu-latest com a suíte completa executada nas duas famílias
- Requisitos: fr_18f8eff7, fr_ad1c85ec

### `br_0dd5e028` BR-6 Conflito é por interseção real, nunca por existência de outro escritor
- **Regra**: A garantia central de D1: concorrência não é exclusão organizada.
- **Quando**: Dois commits concorrentes são validados
- **Então**: Se os conjuntos de partição não se intersectam, AMBOS confirmam; se se intersectam, EXATAMENTE UM confirma e o outro recebe GrafxWriteConflict retryable — nunca serialização pela mera existência de outro escritor
- Requisitos: fr_1dab37f2, fr_02544e01

### `br_95418156` BR-7 A época decide, nunca o dado
- **Regra**: A eliminação estrutural do writer obsoleto — a causa plausível da corrupção do motor de referência.
- **Quando**: Um portador de época anterior à corrente tenta qualquer escrita
- **Então**: A escrita é rejeitada no ato com GrafxStaleEpoch e NENHUM byte chega ao dispositivo; o payload íntegro da tentativa é preservado como entrada reaplicável no ledger
- Requisitos: fr_a5afa250

### `br_6d0d73e9` BR-8 Falha de porta recusa o arranque; falha de banco é local
- **Regra**: Fail-closed nas portas e isolamento por banco — os dois lados do contrato hexagonal.
- **Quando**: Um slot de porta está vazio no arranque, OU a alocação de memória falha num banco específico
- **Então**: No primeiro caso o arranque recusa com erro tipado (sem default silencioso, sem no-op); no segundo, apenas as transações DAQUELE banco recebem GrafxBufferBudgetExceeded — nenhum outro banco é afetado, sem cooldown global
- Requisitos: fr_1505781e, fr_7560ec45

### `br_f4a79fb9` BR-9 O snapshot é imutável para o leitor
- **Regra**: Snapshot isolation observável: a visão do leitor é fixada na abertura.
- **Quando**: Um leitor abre uma transação de leitura e commits paralelos acontecem em qualquer processo
- **Então**: Toda leitura da transação reflete exatamente o estado do instante de abertura; nenhum efeito de commit posterior é observável dentro da transação; nenhum estado parcial é observável jamais
- Requisitos: fr_9baa7040

### `br_2564e79c` BR-10 Reciclagem por horizonte, nunca por expulsão
- **Regra**: A prevenção estrutural do checkpoint starvation e do close fail-open.
- **Quando**: Existe leitor vivo com snapshot antigo referenciando segmentos do WAL
- **Então**: Nenhum segmento referenciado pelo horizonte dele é reciclado e nenhum leitor é expulso; quando o horizonte avança, os segmentos elegíveis são reciclados sem depender da ausência total de leitores
- Requisitos: fr_4d11566d

### `br_dcb41285` BR-11 O índice nunca é menos seguro que o heap
- **Regra**: O invariante genérico de índice secundário (D8a) como regra verificável.
- **Quando**: Um índice secundário é registrado no motor, ou verify() executa com scope que inclui índices
- **Então**: As escritas do índice são WAL-cobertas e a regra de visibilidade da sua classe declarada se aplica (exato: validação contra heap; proximidade: versão+tombstone por horizonte); verify() percorre as entradas e reporta qualquer divergência índice-heap com localização
- Requisitos: fr_d3716857, fr_34a72686

### `br_29ea2662` BR-12 Métrica é contrato
- **Regra**: O guideline de observabilidade como regra executável do motor.
- **Quando**: Uma métrica é registrada no MetricsSink, ou o adaptador no-op está ativo
- **Então**: O nome segue oktografx_ snake_case com sufixo de unidade e descrição en-US, e labels de cardinalidade não limitada são rejeitados NO REGISTRO; com o no-op ativo, o caminho quente não paga alocação nem formatação de string
- Requisitos: fr_fe73339d

## Test Scenarios

### `ts_c166f093` TS-1 Commits disjuntos multi-processo confirmam ambos  _(integration)_
- **Given**: Um banco aberto por dois processos independentes (spawn real, não threads), com tabela mínima e partitions_per_table calibrado, cada processo com transação de escrita tocando partições comprovadamente disjuntas
- **When**: Os dois processos executam commit simultaneamente (barreira de sincronização de teste garantindo sobreposição temporal)
- **Then**: Ambos os commits retornam sucesso durável; após fechar e reabrir o banco num terceiro processo, os dois efeitos estão presentes e a contagem de registros bate exatamente; a métrica oktografx_write_conflicts_total não incrementa
- Critérios: ac_a787696d

### `ts_5d36d9b7` TS-2 Conflito real: um confirma, o outro recebe GrafxWriteConflict e retenta  _(negative)_
- **Given**: Dois processos com transações de escrita tocando a MESMA partição da mesma tabela
- **When**: Ambos executam commit com sobreposição temporal garantida
- **Then**: Exatamente um commit confirma; o outro falha com GrafxWriteConflict e retryable=true, sem efeito colateral no banco; a retentativa do perdedor confirma; o estado final contém os dois efeitos em ordem serializável; oktografx_write_conflicts_total incrementa exatamente 1
- Critérios: ac_63378178

### `ts_15d3980a` TS-3 Snapshot de leitor atravessa commit paralelo sem leitura mista  _(integration)_
- **Given**: Processo A abre transação de leitura (snapshot S); processo B prepara commit de escrita que altera registros lidos por A
- **When**: B confirma o commit enquanto A ainda lê, sob interleavings controlados por seed (mínimo 1000 permutações)
- **Then**: Todas as leituras de A refletem exatamente o estado pré-commit de S; nenhuma permutação produz leitura mista ou parcial; após A fechar e abrir novo snapshot, o efeito de B é visível integralmente
- Critérios: ac_469e7987

### `ts_f1883c80` TS-4 Crash em cada ponto de escrita: recuperação sem perda confirmada e com rastro total  _(integration)_
- **Given**: O gêmeo de injeção do StorageDevice configurado para enumerar TODOS os pontos de escrita do caminho de commit, com seed reproduzível e um workload de N commits conhecidos
- **When**: Para cada ponto de escrita, o processo é morto exatamente naquele ponto e o banco é reaberto
- **Then**: Em cada iteração: todo commit confirmado antes do crash está presente; o replay para no último registro íntegro; cada registro descartado tem entrada no ledger; o relatório {records_replayed, records_discarded, ledger_entries_created} bate com as contagens injetadas
- Critérios: ac_dc1cfa8d

### `ts_090a7685` TS-5 Assinatura NTFS de zeros interiores: truncar, registrar forense, principal intocado  _(negative)_
- **Given**: Um WAL válido sintetizado e depois adulterado com runs de zeros do tamanho de página no INTERIOR, com registros válidos depois — a assinatura documentada do motor de referência; digest do arquivo principal capturado antes
- **When**: O banco é reaberto com a política default de recuperação
- **Then**: A recuperação trunca no último registro válido antes dos zeros; os bytes descartados viram entradas FORENSES no ledger com offset, LSN esperado e digest; o arquivo principal permanece byte a byte idêntico ao digest capturado; o banco aceita novas transações
- Critérios: ac_305fda28

### `ts_6f1ca281` TS-6 Writer de época obsoleta é rejeitado antes do primeiro byte  _(negative)_
- **Given**: Processo A detém o lease; processo B executa takeover legítimo (época incrementada); A ainda mantém seu handle antigo e tenta escrever
- **When**: A submete uma escrita com a época anterior
- **Then**: A recebe GrafxStaleEpoch; o gêmeo de injeção confirma que NENHUMA chamada de escrita chegou ao dispositivo; o payload íntegro da tentativa aparece no ledger como entrada REAPLICÁVEL; o banco permanece consistente sob verify()
- Critérios: ac_2b7ad238

### `ts_205e41e6` TS-7 Takeover após kill -9 sem janela de dupla escrita  _(integration)_
- **Given**: Processo A detém o lease e é morto com kill -9 (sem cleanup); processo B aguarda
- **When**: B detecta o dono morto por liveness monotônica e executa takeover; em paralelo, um interleaving exaustivo com seed injeta tentativas de escrita do handle zumbi de A em todos os pontos do protocolo de takeover
- **Then**: B adquire o lease com época incrementada; NENHUMA permutação permite dois portadores escreverem sob a mesma época; toda tentativa do zumbi falha com GrafxStaleEpoch; a espera de B é medida em oktografx_lease_wait_seconds
- Critérios: ac_469ee001

### `ts_bc595a6c` TS-8 Leitor de vida longa não trava a reciclagem além do seu horizonte  _(integration)_
- **Given**: Um leitor abre snapshot e permanece vivo enquanto um escritor gera commits suficientes para criar múltiplos segmentos de WAL após o horizonte do leitor
- **When**: A reciclagem por horizonte executa repetidamente durante a vida do leitor
- **Then**: Segmentos com LSN final acima do horizonte do leitor são preservados e todos os demais elegíveis são reciclados; o total de segmentos vivos permanece limitado por uma função do horizonte; oktografx_wal_truncation_lag_segments identifica o snapshot retentor; o leitor nunca é expulso e conclui suas leituras corretamente
- Critérios: ac_089e0705

### `ts_ebb79872` TS-9 Reciclagem no Windows com handle aberto via pending-delete  _(e2e)_
- **Given**: Em windows-latest: um segundo processo mantém handle aberto sobre um segmento elegível para reciclagem (simulando leitor lento, antivírus ou indexador)
- **When**: A reciclagem por horizonte tenta liberar o segmento
- **Then**: O caminho pending-delete renomeia o segmento e o remove após o handle fechar, dentro do limite de tentativas com backoff; o espaço é recuperado; nenhum erro é vazado ao chamador; o MESMO teste no POSIX conclui pelo unlink direto — mesmo contrato, mecanismos por família
- Critérios: ac_6044def9

### `ts_651c0c1a` TS-10 Disco cheio aborta limpo, sem registro parcial  _(negative)_
- **Given**: O gêmeo de injeção configurado para devolver disco cheio no meio do append de um commit
- **When**: O commit executa e atinge a condição injetada
- **Then**: A transação aborta com GrafxDeviceFull; após reopen, verify() está limpo e NENHUM registro parcial é visível; quando o espaço é restaurado, novas transações confirmam normalmente
- Critérios: ac_2289e9c5

### `ts_2bc6021b` TS-11 Ordem de durabilidade provada sob fsync mentiroso  _(unit)_
- **Given**: O gêmeo de injeção registrando a sequência exata de chamadas (append, barrier, ack) e configurado para mentir no fsync (ack sem persistir) seguido de crash
- **When**: Um lote de commits executa sob esse regime
- **Then**: A trilha de chamadas prova que NENHUMA confirmação foi emitida antes de durable_barrier retornar; no cenário do fsync mentiroso com crash, os commits confirmados-e-perdidos são detectados pela verificação contra a trilha do gêmeo — a ordenação do motor está correta e a mentira é do dispositivo, exposta e não mascarada
- Critérios: ac_6ca12107

### `ts_0633e0bc` TS-12 verify() localiza cada corrupção semeada; ledger respeita classes  _(integration)_
- **Given**: Um banco com corrupções semeadas uma a uma: checksum de página, checksum de registro, divergência índice-heap; e um ledger com entradas reaplicáveis e forenses conhecidas
- **When**: db.verify(scope='all') executa; ledger.reprocess é chamado sobre cada classe; ledger.reprocess é repetido sobre a mesma entrada reaplicável
- **Then**: Cada corrupção é detectada com localização precisa (página/LSN/índice) e banco limpo produz relatório vazio; reprocess de reaplicável aplica exatamente uma vez e a repetição é no-op idempotente; reprocess de forense falha com erro tipado e export devolve os bytes com digest
- Critérios: ac_b6e7d962

### `ts_d4400f04` TS-13 Isolamento de orçamento: pressão no banco A não toca o banco B  _(integration)_
- **Given**: Dois bancos A e B abertos no mesmo processo; o orçamento de páginas de A é esgotado deliberadamente por um workload de leitura ampla
- **When**: Transações continuam em A e B simultaneamente, e um terceiro banco C é aberto durante a pressão
- **Then**: Transações de A recebem GrafxBufferBudgetExceeded (retryable); TODAS as transações de B confirmam normalmente; C abre sem espera nem cooldown; oktografx_buffer_budget_exceeded_total incrementa apenas com label do banco A
- Critérios: ac_be26dde1

### `ts_9a26486c` TS-14 Calibração, tetos de D5, cobertura de métricas e verde dual como gate  _(e2e)_
- **Given**: O harness com o Ladybug 0.16 instalado no ambiente de CI, workload representativo do KG do Pulse, e o dashboard Grafana JSON versionado no repositório
- **When**: O pipeline completo executa em windows-latest e ubuntu-latest: harness de baseline, calibração de partitions_per_table, suíte, bancada e import-boundary
- **Then**: Os 3 múltiplos de D5 saem publicados via MetricsSink e o gate falha se qualquer teto exceder (verificado forçando um estouro artificial); a calibração registra o default congelado; um estouro >10x no commit põe o pipeline no estado 'consult JP' e nada prossegue; toda métrica das ORs é emitida e referenciada no dashboard; o import-boundary reporta zero; o gate agregado só fica verde com as DUAS famílias verdes
- Critérios: ac_7f69d7dc

## Decisions

### `dec_d59681f7` SD-1 Registro de transação auto-descritivo dissolve a irreversibilidade da granularidade
- **Rationale**: A granularidade de conflito parecia irreversível por definir formato em disco; carregando format_version + granularity_descriptor no cabeçalho de cada registro, o parâmetro partitions_per_table vira calibração, não contrato congelado. Origem: resolução da thread TR-M1-CONFLICT-GRANULARITY do refinamento (nó KG decision_6c3e9197199661b9e452b599).
- **Contexto**: D1 exige estado de conflito compartilhado entre processos; a escolha da granularidade era o único elemento que parecia exigir medição pré-spec.
- Alternativa rejeitada: Granularidade fixa em tabela — degenera para escritor único num grafo
- Alternativa rejeitada: Granularidade fixa em chave — conjunto caro em transações de lote
- Alternativa rejeitada: Medição pré-spec obrigatória — substituída por formato auto-descritivo + calibração bloqueante em FR-15

### `dec_88b1410a` SD-2 Calibração como fase 1 bloqueante do DAG, com fallback que retorna ao JP
- **Rationale**: A escrita concorrente entre processos (leitura literal de D1) está além do que SQLite WAL e LMDB entregam; o risco fica cercado por um número objetivo: o harness mede o múltiplo de commit durável contra o Ladybug e, acima de 10x (teto D5), o pipeline PARA em estado 'consult JP' — a troca para lease justo altera D1 e só acontece por Q&A. Nenhum outro card de implementação inicia antes da calibração verde. Origem: nós KG decision_974d5e17c8cc97b79e4681ee e decision_6c3e9197199661b9e452b599.
- **Contexto**: Fallback formalizado da thread TR-M1-MULTIPROCESS-WRITER-MODEL; nunca acionado silenciosamente.
- Alternativa rejeitada: Lease justo serializado como entrega de M1 — opção oferecida ao JP no Q1 e NÃO escolhida
- Alternativa rejeitada: Prosseguir sem gate numérico — aposta aberta rejeitada

### `dec_d5ded469` SD-3 Regra dual de visibilidade de índice secundário por classe
- **Rationale**: Índices exatos usam entradas sem versão + validação obrigatória contra o heap (superconjunto seguro, custo aceitável em igualdade/faixa); índices de proximidade usam entradas versionadas + tombstone reconciliado pelo horizonte de snapshot, com reconciliação transacional WAL-coberta — compatível com BR-1 por construção, porque cada limpeza é registro normal de WAL, reversível por replay. Reusa o mecanismo de horizonte que a reciclagem de segmentos já obriga a existir. Origem: nó KG decision_ea4fe6d7f5ea51f9cec506bc.
- **Contexto**: Realização de D8a; a validação contra heap é exatamente o custo que degrada busca vetorial, por isso a regra é por classe e não única.
- Alternativa rejeitada: Regra única versionada para todos — infla índices exatos sem necessidade
- Alternativa rejeitada: Regra única heap-validação — reintroduz o custo por candidato que degradou o consumidor
- Alternativa rejeitada: Copy-on-write por snapshot (LMDB) — amplificação de escrita alta

### `dec_66e501ac` SD-4 Ledger dual-form por classe de origem num recipiente único
- **Rationale**: Entrada REAPLICÁVEL (operação decodificada; origem íntegra) e entrada FORENSE (bytes brutos + metadados; checksum falho) têm garantias diferentes e o contrato as distingue na API — reprocess só aceita reaplicáveis, forenses têm export. Um recipiente único porque a fronteira semântica de D7 é uma só; a separação é atributo da entrada. O registro auto-descritivo (SD-1) garante a decodificabilidade da classe íntegra através de versões de formato. Origem: nó KG decision_72c93a34a60b5659798ed828.
- **Contexto**: Apresentar as duas classes sob a mesma operação convidaria o operador a reprocessar lixo — corrupção derivada.
- Alternativa rejeitada: Somente bytes brutos — vira material forense após migração de formato
- Alternativa rejeitada: Somente operação decodificada — impossível para checksum falho
- Alternativa rejeitada: Duas estruturas separadas — fronteira semântica é uma só; rejeitada

## API Contracts

### `api_c5e17ca5` 
```json
{
  "id": "api_c5e17ca5",
  "method": "GET",
  "path": "/metrics",
  "description": "Exposição OpenMetrics do adaptador default da porta MetricsSink. Único endpoint HTTP de M1 — o motor é biblioteca embarcada e esta superfície existe apenas quando o publicador OpenMetrics está habilitado por configuração. Corpo text/plain; version=0.0.4, com todas as métricas oktografx_* das ORs.",
  "request_body": null,
  "response_success": {
    "content_type": "text/plain; version=0.0.4",
    "body_example": "# HELP oktografx_ledger_depth Number of unapplied-work ledger entries by origin class.\n# TYPE oktografx_ledger_depth gauge\noktografx_ledger_depth{origin_class=\"reapplicable\"} 0\noktografx_ledger_depth{origin_class=\"forensic\"} 0"
  },
  "response_errors": [
    {
      "status": 503,
      "reason": "metrics publisher not initialized or no-op adapter active"
    }
  ],
  "linked_requirements": [
    "fr_fe73339d"
  ],
  "linked_rules": null,
  "notes": null,
  "linked_task_ids": [
    "7f4b2135-9e6b-4793-8242-a7a0bade4425"
  ],
  "contract_type": "http"
}
```

## Integration Requirements

### `ir_74316d7b` IR-1 Ladybug 0.16.0 como dependência de teste do harness
- **Tipo**: other · provider: ladybug==0.16.0 (PyPI, extra [bench]) · consumer: harness de baseline (FR-15)
- O motor de referência é instalado APENAS no ambiente de dev/CI do harness (extra [bench]), nunca como dependência de runtime. Restrições operacionais conhecidas do Ladybug respeitadas no harness: um único Database de escrita por caminho, uma transação de escrita por processo, nunca duas instâncias no mesmo diretório, e nenhum checkpoint disparado de thread de background (o crash 0xc0000005 documentado). O harness roda o Ladybug num processo dedicado e efêmero.

### `ir_331273f0` IR-2 CI em matriz dual Windows + Linux
- **Tipo**: other · provider: GitHub Actions (windows-latest, ubuntu-latest) · consumer: suíte completa, bancada de injeção, harness, import-boundary
- Requisito de ambiente de D9: runners nas duas famílias executando o pipeline inteiro; o job agregado só fica verde com ambas as famílias verdes; teste pulado numa família é reportado como cobertura parcial declarada, nunca silenciado.

### `ir_ccaaf1a3` IR-3 Exposição OpenMetrics para scrape Prometheus/Grafana
- **Tipo**: api · provider: okto_grafx metrics publisher (adaptador OpenMetrics) · consumer: Prometheus scrape → dashboards Grafana versionados
- Formato text/plain; version=0.0.4 (OpenMetrics). É o único acoplamento com a stack de observabilidade: por formato padrão, nunca por cliente no domínio. 503 quando o publicador não está inicializado ou o adaptador ativo é no-op.

## Observability Requirements

### `or_e32ab34d` OR-1 Métricas de concorrência
- **Signal**: metric · metric: `oktografx_lease_wait_seconds, oktografx_write_conflicts_total, oktografx_commit_retries_total, oktografx_active_transactions` · threshold: None
- Histograma de espera por lease (labels: outcome=granted|timeout|takeover), contador de conflitos de escrita, retentativas e gauge de transações ativas por modo (read|write). Toda espera de coordenação é medida — contenção invisível é proibida por desenho.

### `or_c7a4cece` OR-2 Métricas de durabilidade e WAL
- **Signal**: metric · metric: `oktografx_fsync_duration_seconds, oktografx_barrier_failures_total, oktografx_wal_size_bytes, oktografx_wal_segments, oktografx_wal_truncation_lag_segments` · threshold: None
- Latência de fsync (histograma), falhas de barreira, tamanho total do WAL, contagem de segmentos vivos e lag de truncamento com o snapshot retentor identificável (label bounded: reader_present=true|false). O starvation vira estado diagnosticável, nunca crescimento silencioso.

### `or_5d01dd98` OR-3 Métricas de integridade, recuperação e ledger
- **Signal**: metric · metric: `oktografx_checksum_verifications_total, oktografx_checksum_failures_total, oktografx_recovery_replays_total, oktografx_recovery_discarded_records_total, oktografx_ledger_depth, oktografx_ledger_oldest_entry_age_seconds, oktografx_quarantine_entries` · threshold: None
- Checksums verificados/falhados, replays executados, registros descartados, PROFUNDIDADE e IDADE do ledger de trabalho não-aplicado (por origin_class=reapplicable|forensic) e entradas em quarentena. Ledger não-vazio é estado alto e visível — nunca caixa de entrada silenciosa (D7).

### `or_b46f0195` OR-4 Métricas de memória, ciclo de vida e baseline
- **Signal**: metric · metric: `oktografx_buffer_budget_used_bytes, oktografx_buffer_budget_exceeded_total, oktografx_database_opens_total, oktografx_recoveries_total, oktografx_baseline_ceiling_multiple` · threshold: None
- Uso e estouro de orçamento POR BANCO (label bounded: db como hash curto, não caminho), aberturas, recuperações por outcome, e os múltiplos dos 3 tetos de D5 publicados pelo harness (label ceiling=durable_commit|point_read|open_replay) — o gate de CI lê ESTA métrica, não um log.

### `or_ebaf4f0f` OR-5 Dashboard Grafana versionado no repositório
- **Signal**: dashboard · metric: `None` · threshold: None
- O repositório versiona dashboards/oktografx-m1.json (Grafana) com painéis consumindo TODAS as métricas de OR-1..OR-4; a entrega de uma métrica nova sem painel correspondente no mesmo commit é dívida bloqueante (guideline de métricas do board). O CI valida que todo metric_name das ORs aparece referenciado no JSON.

### `or_a30cb730` OR-6 Exposição OpenMetrics default
- **Signal**: metric · metric: `None` · threshold: None
- A instalação default expõe GET /metrics em formato OpenMetrics (text/plain; version=0.0.4) pelo adaptador Prometheus da porta MetricsSink; adaptadores OTLP/StatsD/JSON/no-op selecionáveis por configuração, nenhum com atalho de código; com no-op ativo o caminho quente não aloca (verificado por teste de contagem de alocações).

## Cards derivados

- `1c648911-fe62-4587-92a5-5d5b7c8d04b6` [None] **[TEST] T1 — Concorrência multi-processo: commits disjuntos e conflito real (TS-1, TS-2)** — not_started
- `1ae51309-79cf-4c30-8ae6-3fd927820a20` [None] **[TEST] T2 — Snapshot isolation e crash em todo ponto de escrita (TS-3, TS-4)** — not_started
- `eb8e63e0-39b1-454f-b027-2226a06d2b82` [None] **[TEST] T3 — Assinatura NTFS e época obsoleta (TS-5, TS-6)** — not_started
- `74e7795c-8279-4471-8252-d3e4f0f8cd39` [None] **[TEST] T4 — Takeover sem TOCTOU e reciclagem por horizonte (TS-7, TS-8)** — not_started
- `58bdf73b-19da-4420-b4d5-6e63bb71768b` [None] **[TEST] T5 — Reciclagem Windows pending-delete e disco cheio (TS-9, TS-10)** — not_started
- `4abb8b7b-7cf8-4c3a-a76d-2b85db03c9ff` [None] **[TEST] T6 — Ordem de durabilidade e verify()/ledger por classe (TS-11, TS-12)** — not_started
- `c1d3a128-00d1-480c-a628-47fa45357e9b` [None] **[TEST] T7 — Isolamento de orçamento e gate de calibração/CI dual (TS-13, TS-14)** — not_started
- `0f52da5f-6cae-4c68-91bf-f8d7d57035ad` [None] **I1 [CALIBRAÇÃO — FASE 1 BLOQUEANTE] Harness de baseline Ladybug + calibração de partição** — not_started
- `ff9f2fec-58f3-432d-99c9-b6586f8400c6` [None] **I2 — Portas do domínio + adaptadores locais POSIX/Windows + esqueleto do pacote** — not_started
- `85e9fd57-d444-4830-a14b-cd8b1192b58f` [None] **I3 — WAL segmentado, formato auto-descritivo e commit durável** — not_started
- `1eb3aebc-0108-41e0-a3ff-e63c978ac593` [None] **I4 — MVCC, validação otimista por partição e coordenação entre processos** — not_started
- `c15a7703-deda-4cbc-b3c2-f432997fcc20` [None] **I5 — Recuperação, ledger dual-form, quarentena, verify() e índice de referência** — not_started
- `7f4b2135-9e6b-4793-8242-a7a0bade4425` [None] **I6 — Métricas, dashboard Grafana, gêmeo de injeção e CI dual** — not_started
