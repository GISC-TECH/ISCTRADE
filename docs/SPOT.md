# Modo spot — documento de design (não implementado)

> Status: **somente design** (F-17). Nada disto existe no código ainda; o ISCTRADE opera apenas perpétuos USDT-M.
> Objetivo: permitir robôs *long-only* em mercado à vista (Binance Spot primeiro), reaproveitando estratégias,
> `RiskManager`, paper, backtest e auditoria, sem enfraquecer nenhuma garantia de segurança atual.

## 1. Escopo

- **Dentro**: Binance Spot (`api.binance.com`, Testnet `testnet.binance.vision`), pares cotados em USDT (depois
  BRL/USDC), compras e vendas à vista, *long-only*, sem alavancagem, paper/testnet como padrão.
- **Fora (por ora)**: margem spot (empréstimo), *short* spot, conversões automáticas entre moedas de cotação,
  staking/earn, outras exchanges spot (Binance BR, Mercado Bitcoin, Foxbit — ver `EXCHANGES.md`).

## 2. Diferenças que importam

| Tema | Perpétuo USDT-M (hoje) | Spot |
|---|---|---|
| Posição | quantidade assinada (long/short), uma por símbolo | saldo do ativo-base (≥ 0); "posição" = carteira |
| Alavancagem / liquidação | sim (1–125x), preço de liquidação | não; perda máxima = valor comprado |
| Funding | a cada 8 h | não existe |
| Stop de proteção | `STOP_MARKET` `closePosition`/*reduce-only* na corretora | **não há** `STOP_MARKET` nem `reduceOnly`: `STOP_LOSS_LIMIT` (pode não preencher num *gap*) ou **OCO** (stop + alvo) / **OTOCO** (entrada que, ao preencher, cria o OCO) |
| Saldo travado | margem | o ativo-base fica **bloqueado** pela ordem stop; vender manualmente exige cancelar o OCO antes |
| Taxas | em USDT | em BNB, no ativo recebido ou em USDT (`commissionAsset`) — a quantidade líquida recebida muda |
| Filtros | `LOT_SIZE`, `MIN_NOTIONAL` | `LOT_SIZE`, `NOTIONAL`, `PERCENT_PRICE_BY_SIDE`, `MAX_NUM_ORDERS`… |
| User-data stream | `listenKey` (futuros) | `listenKey` do spot **foi desativado em 2026-02-20**; usar o WebSocket API (`userDataStream.subscribe.signature`, ou `session.logon` com chave Ed25519 + `userDataStream.subscribe`) — eventos `executionReport`, `outboundAccountPosition`, `balanceUpdate`, `listStatus` |
| Dados históricos | `data.binance.vision/data/futures/um/...` | `data.binance.vision/data/spot/...` (mesmo carregador de arquivo, outro caminho) |

## 3. Desenho proposto

### 3.1 Modelo de dados (migração nova, compatível com SQLite)
- `accounts.market_type` (`usdm` | `spot`, padrão `usdm`) — define adaptador, regras de risco e telas.
- `positions` continua servindo: para spot, `quantity ≥ 0` (saldo do ativo-base atribuído ao robô), `entry_price`
  = custo médio, `leverage = 1`, `stop_loss`/`take_profit` espelhando o OCO ativo.
- `trades.fee_asset` já existe; a quantidade líquida considera taxa cobrada no ativo-base.
- `orders.order_list_id` (OCO/OTOCO) para ligar as pernas.

### 3.2 Adaptador `binance_spot` (`app/exchanges/binance_spot.py`)
Implementa `ExchangeAdapter` traduzindo para o **formato canônico** existente:
- klines/tickers/filtros: `/api/v3/klines`, `/api/v3/ticker/24hr`, `/api/v3/exchangeInfo`;
- `get_positions()`: saldos (`/api/v3/account`) → `positionAmt` = `free + locked` do ativo-base, `entryPrice` do
  custo médio local; `get_account()` com `totalWalletBalance` = USDT + Σ saldos × preço;
- `place_order()`: MARKET/LIMIT; `STOP_MARKET` canônico com `closePosition` → OCO (`/api/v3/orderList/oco`) com
  `stopPrice`, `stopLimitPrice` = stop × (1 − `SPOT_STOP_LIMIT_OFFSET_BPS`) e alvo opcional;
- sem funding/alavancagem/margem: `set_leverage`/`set_margin_type` viram no-op idempotente, funding = `[]`,
  `get_position_mode()` = `False`;
- streams: público em `wss://stream.binance.com:9443` (mesmo `MarketStream`), privado via WebSocket API
  (`wss://ws-api.binance.com:443/ws-api/v3`) traduzindo `executionReport` → `ORDER_TRADE_UPDATE` e
  `outboundAccountPosition` → `ACCOUNT_UPDATE`.

### 3.3 Risco e execução
- `RiskManager`: `leverage` forçado a 1; ordem de venda só até o saldo (sem *short*); compra limitada ao saldo da
  moeda de cotação; risco por trade continua = distância até o stop × quantidade; drawdown/perda diária em USDT.
- **Stop obrigatório** continua valendo: toda compra spot sai acompanhada de OCO (ou é OTOCO). A guarda de stops
  (G-01) passa a verificar "saldo-base > 0 sem OCO ativo".
- Kill switch: cancela OCOs e vende o saldo-base atribuído aos robôs (MARKET), nunca o resto da carteira.
- Estratégias: flag `long_only` no contexto; intenções de *short* são descartadas com motivo `spot_long_only`;
  `allow_short` é forçado a `false`.

### 3.4 Paper e backtest
- `BacktestConfig.market = "spot"`: sem funding, sem *short*, `leverage = 1`, taxa padrão 0,1%.
- Stop spot simulado como **stop-limit**: se o candle abrir abaixo do limite, a ordem não preenche (pior caso),
  diferente do perpétuo (preenche na abertura). Isso precisa aparecer nas métricas (`stop_not_filled`).
- Paper: mesmo `PaperExecutor`, com saldo por ativo em vez de `paper_balance` único.

### 3.5 Frontend
- Seletor "Mercado" (Perpétuo/Spot) no cadastro de conta; estratégias com *short* marcadas como "só compra" no
  spot; painel mostrando saldos por ativo.

## 4. Fases e esforço

| Fase | Entrega | Esforço |
|---|---|---|
| 1 | Backtest/paper spot (`market="spot"`, arquivo spot do data.binance.vision, stop-limit simulado) | M |
| 2 | Adaptador `binance_spot` REST + OCO na Testnet; `RiskManager` spot; testes com transporte simulado | G |
| 3 | User-data via WebSocket API (`userDataStream.subscribe.signature`), reconciliação e guarda de OCO | M |
| 4 | Frontend + docs + 4 semanas de paper/testnet antes de qualquer conta real | M |

## 5. Questões em aberto
- Atribuição de saldo: a carteira spot é compartilhada (ex.: BTC que o usuário já tinha). Proposta: o robô só vende
  o que ele mesmo comprou (`positions.quantity`), nunca o saldo total.
- Taxa em BNB: descontar no PnL convertendo pelo preço do momento, ou exigir taxa no ativo recebido.
- Tributação e obrigações acessórias no Brasil (declaração de operações com criptoativos) mudam com spot em
  exchange estrangeira: o app pode exportar o histórico de operações, mas não calcula imposto — consulte um contador.
- Brasil/regulação (BCB 519–521): ver `EXCHANGES.md` §2 antes de escolher a exchange spot.
