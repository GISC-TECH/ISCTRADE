# Hedge mode (posições LONG/SHORT simultâneas) — decisão: continuar recusando

> Status: **recusado de propósito** (G-10b). Este documento registra o porquê e o que seria preciso para suportar.

## Como funciona hoje

O ISCTRADE trabalha com **uma posição líquida por símbolo e conta** (one-way mode, `positionSide=BOTH` na Binance,
`positionIdx=0` na Bybit). Contas em hedge mode são detectadas e recusadas com uma mensagem clara:

| Onde | Comportamento |
|---|---|
| `services/position_mode.py` | `ensure_one_way()` consulta `GET /fapi/v1/positionSide/dual` (cache de 5 min para a resposta one-way) e levanta `HedgeModeUnsupported` |
| Ordens manuais live (`services/manual_trading.py`) | HTTP 409 com a instrução para mudar para one-way mode |
| Bots (`services/jobs.py`) | falham ao iniciar com a mesma mensagem |
| `POST /accounts/{id}/verify` | `hedge_mode=true`, `ok=false` |
| Reconciler / stream | linhas com `ps` ≠ `BOTH` (Binance) ou `positionIdx` ≠ 0 (Bybit WS → `ps=LONG/SHORT`) são ignoradas |
| Guarda de stop (`services/protection.py`) | considera só posições `positionSide=BOTH` |

## Por que não implementar agora

1. **Modelo de dados**: `positions` tem `UNIQUE (account_id, symbol)` e quantidade assinada; hedge exige uma linha
   por lado (`UNIQUE (account_id, symbol, side)`) e muda toda leitura de "a posição do símbolo".
2. **Matemática de posição em todo o sistema**: `apply_fill`, paper, backtest, `RiskManager` (risco agregado,
   margem, liquidação), PnL, painel e kill switch assumem uma posição líquida. Com dois lados, exposição líquida ≠
   margem usada (as duas pernas consomem margem).
3. **Ordens**: na Binance, em hedge mode toda ordem precisa de `positionSide=LONG|SHORT` e **`reduceOnly` não pode
   ser enviado** — o fechamento é "SELL + positionSide=LONG". Toda a proteção atual (stops `closePosition`,
   saídas *reduce-only*, kill switch "fechar tudo") teria de ser reescrita por lado. Na Bybit: `positionIdx=1/2`.
4. **Estratégias** emitem intenções sobre uma posição líquida (`ctx.position_qty`); nenhuma das ~26 estratégias
   precisa de duas pernas. Um *hedge* real (ex.: long spot + short perp, *basis trade*) é outro produto.
5. **Risco operacional**: a guarda de stops (G-01) e a reconciliação são as partes mais sensíveis do sistema;
   duplicá-las por lado aumenta muito a superfície de erro para um benefício pequeno.

## O que seria preciso (se houver demanda)

1. Migração: `positions.side` (`BOTH|LONG|SHORT`, padrão `BOTH`), nova *unique* `(account_id, symbol, side)`;
   `orders.position_side`.
2. `OrderRequest`/`OrderIntent` com `position_side`; o adaptador envia `positionSide` (Binance) / `positionIdx`
   (Bybit) e **nunca** `reduceOnly` em hedge.
3. Reconciler, user-data stream e guarda de stops por (símbolo, lado); stops `closePosition` com `positionSide`.
4. `RiskManager`: margem somando as duas pernas; risco por trade por perna; kill switch fechando as duas.
5. Paper e backtest com duas pernas; métricas por perna.
6. Testes de regressão em one-way (o padrão continua sendo one-way) e um conjunto novo para hedge.

Estimativa: **G** (várias semanas), com revisão de segurança dedicada. Até lá: mantenha a conta em one-way mode.
