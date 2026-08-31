# CE-2 — reader pin por participante

**Estado:** implementado e certificado no candidato integrado `565ac37` (commit de produto
`3be6659`). A mudança preserva o contrato multiprocesso: continuam suportados vários writers e
readers, inclusive em processos distintos.

## Decisão

Cada `TransactionManager` mantém um único registro durável de reader enquanto o participante está
aberto, em vez de publicar e retirar um arquivo por transação. O LSN publicado é sempre um piso
conservador:

- na primeira transação, o relógio falível é lido **antes** da publicação durável e o snapshot é
  escolhido somente após uma segunda leitura do estado publicado;
- com transações abertas, o pin avança no máximo até o menor snapshot ainda vivo;
- sem transações abertas, ele pode avançar até o piso publicado, mas só é retirado por `close()`;
- checkpoint avança o pin próprio antes de calcular o horizonte e reciclar o WAL;
- commit/rollback não fazem unregister, e uma porta de finalização mantém a transação corrente no
  piso até o resultado estar resolvido;
- `advance()` é somente para frente e atualiza o handle local antes da publicação externa.

Isso remove duas barreiras e a troca de nome do registro de reader do caminho quente sem reduzir
isolamento, durabilidade, detecção de processo morto ou proteção de segmentos WAL.

## Prova com processos reais

`tools/measure_reader_pin.py` usa processos de sistema e um banco temporário. O writer opera por
comandos tokenizados, registra inventários imediatamente antes/depois de cada checkpoint e fica
parado fora desses comandos. Segmento protegido significa `last_lsn >= S`, inclusive o segmento
que contém o snapshot.

Os três cenários são:

1. `long-reader`: pelo menos quatro checkpoints e quatro segmentos `last_lsn < S` reciclados, sem
   remover nenhum segmento protegido; leituras repetidas, heartbeat e `verify()` permanecem verdes.
2. `close-advance`: rollback da transação longa, pin acima de `S` em até 10 s, reciclagem posterior
   de um segmento pertencente ao conjunto protegido congelado e retirada do registro somente no
   `close()`.
3. `kill-hold`: heartbeat novo observado primeiro pelo coordenador do writer, kill real seguido de
   `wait()`, checkpoints de retenção concluídos antes do TTL, prova explícita de que o registro ainda
   está presente/decodificável e checkpoint de liberação iniciado somente após o limite superior da
   observação. Nenhum checkpoint cruza a fronteira temporal.

O run oficial v8 em `565ac37`, com `reader_stall_threshold_seconds=15`, concluiu **47/47
critérios**: o long-reader permaneceu aberto por `90,00 s` e publicou 19 scans idênticos; cinco
segmentos pré-snapshot foram reciclados em cada cenário; sete permaneceram protegidos durante a
leitura e seis foram liberados apenas após rollback/TTL. O gap anchor→kill foi `0,23 s`, o pin
avançou `9,27 s` após rollback e o checkpoint final de retenção terminou `2,94 s` antes do TTL,
dentro da margem explícita de `3 s`. `verify("all")` retornou zero findings com os participantes
vivos e após reabertura. Artefato `reader-pin-ce2-official-v8-565ac37.json`, SHA-256
`d0285b5197a1ac05133cac3c90ab02ae1bd021f86d2785ad6ad6ffa9c3d29b3e`.

O próprio JSON autentica `Grafx@565ac37`, source/tests limpos, Python 3.13.1, comando e origem
absolutos e o instrumento SHA-256
`815cdebe1802649aab19765ad106c2187404bb9bb3250340e0c2eea46b8d19da`. A revisão lógica
independente desse SHA terminou PASS antes da execução oficial.

## Gates complementares

- suíte global: **10.910 passed, 19 skipped, 0 failed**, `1.912,75 s`;
- Ruff e `git diff --check`: verdes;
- H1–H8.1: 180/180 amostras, 12/12 famílias, digest lógico `c994255b...` e imagem forense
  `6dd6cf05...`; artefato `ce2-565ac37-64a9da6-pf5-h1h8_1.json`, SHA-256
  `bc305be2af31cd8769481f2ad4906ede3abc71f6e397a63cc3f993e0ad5fef14`.
- concorrência histórica inalterada: 4 writers + 3 readers em `46,6 s`, 500/500 linhas
  confirmadas, 10,7 rows/s, 173,9 statements/s e leitura pontual `3,16/12,97 ms`
  mediana/p99; zero perda, duplicação, phantom, leitura rasgada, escape ou finding hot/cold.

Reprodução:

```powershell
python tools/measure_reader_pin.py --src .\src --scenario all --json reader-pin.json
python tools/measure_concurrency.py
```

O segundo comando continua sendo o instrumento histórico de 4 writers + 3 readers; ele não foi
alterado pela CE-2 para preservar comparabilidade.
