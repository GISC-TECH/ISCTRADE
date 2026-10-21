# Painel em tempo real e trading manual

> [!CAUTION]
> Ordens manuais seguem **as mesmas travas** dos bots: kill switch global e da conta, filtros do símbolo e o
> `RiskManager` (stop loss obrigatório, risco por trade, alavancagem, margem, perda diária e drawdown). O modo da
> conta decide o executor: **paper (simulado) é o padrão**; ordens reais só com `LIVE_TRADING_ENABLED=true` **e**
> a conta em `execution_mode=live`. Nenhuma tela, score ou sinal garante lucro.

## Visão geral

```
Binance WS (público)                     Binance user data stream
  !miniTicker@arr, !markPrice@arr@1s,      ORDER_TRADE_UPDATE / ACCOUNT_UPDATE
  <símbolo>@kline_<intervalo> (sob demanda)          │
            │                                        │
            ▼                                        ▼
   worker streams: MarketFeed (main + testnet)  StreamSupervisor (por conta)
            │  rt:market:main | rt:market:test       │  rt:ws:<workspace_id>
            ▼                                        ▼
  ┌──────────────────────── Redis pub/sub ────────────────────────┐
  │  rt:ws:<workspace_id> ◀── audit (ordens, posições, kill switch,│
  │                           bots, ações manuais), alertas,       │
  │                           tick dos bots, snapshots de conta    │
  │  rt:global            ◀── scanner, notícias, kill switch global│
  └───────────────────────────────┬───────────────────────────────┘
                                  ▼
             FastAPI  GET /api/v1/ws?workspace_id=…  (WebSocket)
                                  │  Caddy: wss://<domínio>/api/v1/ws
                                  ▼
          Next.js: RealtimeProvider → barra de cotações, gráfico, /dashboard/live
```

- Eventos de domínio são publicados **depois do commit** da transação (rollback descarta). Se o Redis estiver
  fora, a publicação cai num broker em memória do próprio processo (útil em desenvolvimento/testes; em produção
  com vários processos os eventos dos workers só chegam via Redis).
- O `MarketFeed` também grava o mark price de todos os símbolos no cache de preços (`price:<net>:<símbolo>`),
  usado pelas verificações de SL/TP/LIMIT das contas paper.

## Protocolo do WebSocket

URL: `wss://<host>/api/v1/ws?workspace_id=<uuid>` (sem `workspace_id`, usa o workspace ativo do usuário).

Autenticação e autorização:
- cookie `access_token` (o mesmo JWT httpOnly da API REST), validado com `token_version` (logout em todos os
  dispositivos/troca de senha derruba a conexão na próxima verificação);
- cabeçalho `Origin` precisa estar em `CORS_ORIGINS` (proteção contra *cross-site WebSocket hijacking*); clientes
  sem `Origin` (não-navegadores) são aceitos;
- só o canal do workspace do qual o usuário é membro; associação e sessão são revalidadas a cada 60 s;
- limite de conexões simultâneas por usuário (`REALTIME_MAX_CONNECTIONS_PER_USER`).

Códigos de fechamento: `4401` sessão inválida/expirada (o cliente chama `/auth/refresh` e reconecta), `4403`
origem/acesso negado, `4404` workspace não encontrado, `4429` conexões demais, `1013` tempo real desativado.

Cliente → servidor:

```json
{"op": "subscribe", "net": "main", "symbols": ["BTCUSDT", "ETHUSDT"], "klines": [{"symbol": "BTCUSDT", "interval": "15m"}]}
{"op": "ping"}
```

Até 60 símbolos e 4 streams de candles por conexão. `net` é `main` (dados públicos da mainnet, padrão) ou `test`
(Testnet — o painel usa `test` quando a conta selecionada é Testnet). As assinaturas de candles são registradas no
Redis com expiração (`REALTIME_KLINE_TTL_SECONDS`) e renovadas enquanto houver navegador conectado.

Servidor → cliente (`{"type", "ts", "data"}`):

| Tipo | Conteúdo |
|---|---|
| `hello`, `heartbeat` (a cada `REALTIME_HEARTBEAT_SECONDS`), `pong`, `subscribed`, `error` | controle |
| `tickers` | `[{s, c, o, h, l, q, P}]` — último preço, variação 24 h (%) e volume em USDT, só dos símbolos assinados |
| `mark` | `[{s, p, r, T}]` — mark price, funding e próximo funding |
| `kline` | `{s, i, t, T, o, h, l, c, v, x}` — candle em formação ou fechado (`x`) |
| `order`, `position`, `manual`, `kill_switch`, `bot` | eventos da trilha de auditoria (`data.event` = nome do evento) |
| `order_update`, `account_update` | resumo dos eventos do user data stream (contas live) |
| `account` | snapshot de patrimônio; `alert` | alerta criado; `scanner`, `news` | novos dados globais |

**Throttling**: preços e candles são agregados por símbolo e enviados no máximo a cada
`REALTIME_PRICE_THROTTLE_MS` (250 ms ⇒ até 4 atualizações/s); candles fechados nunca são descartados.

Cliente (`frontend/lib/realtime.ts`): reconexão com *backoff* exponencial + *jitter* (1 s → 30 s), reconexão ao
voltar para a aba/rede, detecção de conexão parada (3 heartbeats sem mensagens) e assinaturas com contagem de
referências entre componentes.

## Tela "Tempo real & Trading" (`/dashboard/live`)

- **Barra de cotações** (em todas as telas do dashboard) com lista editável e indicador de conexão.
- **Gráfico de candles** ([lightweight-charts](https://github.com/tradingview/lightweight-charts), Apache-2.0)
  com histórico via `GET /api/v1/market/klines`, candles ao vivo, EMAs 20/50, marcadores de entradas/saídas
  (execuções da conta) e linhas de entrada, stop loss, take profit e ordens LIMIT abertas.
- **Posições** com PnL e ROE recalculados pelo mark price ao vivo; **ordens recentes** (cancelar LIMIT abertas);
  **eventos ao vivo**; **bots** da conta com estado; patrimônio ao vivo.
- **Ordem manual**: compra/venda, MARKET ou LIMIT, quantidade, **stop loss obrigatório**, take profit opcional,
  alavancagem (até o limite da conta), prévia de risco (USDT e % do patrimônio), nocional, margem e R:R, e
  **diálogo de confirmação** (destacando conta REAL). Botões de fechar posição e fechar todas (também com
  confirmação). Papel `viewer` só visualiza; com kill switch ativo o formulário de abertura fica bloqueado.

## API de trading manual (papel `trader` ou `admin`)

| Rota | Descrição |
|---|---|
| `POST /api/v1/trading/orders` | `{account_id, symbol, side, type: MARKET\|LIMIT, quantity, price?, stop_loss, take_profit?, leverage?, confirm: true}` |
| `DELETE /api/v1/trading/orders/{id}?account_id=` | cancela ordem aberta (paper: LIMIT pendente; live: também na Binance) |
| `POST /api/v1/trading/close` | `{account_id, symbol, confirm}` — reduce-only a mercado da posição inteira + cancela ordens do símbolo |
| `POST /api/v1/trading/close-all` | `{account_id, confirm}` — fecha todas as posições e cancela ordens abertas |
| `POST /api/v1/trading/leverage` | `{account_id, symbol, leverage, confirm}` — 1..`max_leverage` da conta (live: `POST /fapi/v1/leverage`) |
| `GET /api/v1/trading/status` | `{global_kill_switch}` (qualquer membro) |

Regras:
- `confirm` precisa ser `true`; sem stop loss a requisição é rejeitada (422). SL/TP do lado errado, risco acima do
  limite, alavancagem acima do máximo ou margem insuficiente ⇒ ordem `REJECTED` com o motivo.
- **Kill switch** global ou da conta (`killed`): novas entradas e mudanças de alavancagem retornam 409 (auditado
  como `manual.order_blocked`); **fechar/cancelar é sempre permitido**.
- **Paper**: MARKET preenche no mark price com slippage; LIMIT não executável fica `NEW` e é preenchida pela tarefa
  `paper.process_orders` (a cada `PAPER_ORDER_CHECK_SECONDS`) quando o mark price cruza o limite (cancelada se um
  kill switch for acionado). A mesma tarefa encerra posições paper que tocam o SL ou o TP
  (`position.stop_loss` / `position.take_profit`).
- **Live**: LIMIT GTC (ou MARKET) + `STOP_MARKET` e `TAKE_PROFIT_MARKET` com `closePosition=true` (mark price) na
  Binance; execuções chegam pelo user data stream e pelo reconciliador.
- Toda ação gera auditoria `manual.*` (com o usuário), um evento em tempo real e o alerta **`manual_action`**
  (WhatsApp, ligado por padrão; desligue em Monitoramento › Configurações).

## Configuração

| Variável | Padrão | Descrição |
|---|---|---|
| `REALTIME_ENABLED` | `true` | Liga o WebSocket e a publicação de eventos |
| `REALTIME_HEARTBEAT_SECONDS` | `15` | Intervalo do heartbeat |
| `REALTIME_MAX_CONNECTIONS_PER_USER` | `20` | Conexões simultâneas por usuário (cada aba aberta do painel usa uma) |
| `REALTIME_PRICE_THROTTLE_MS` | `250` | Intervalo mínimo entre atualizações de preço/candle por símbolo |
| `REALTIME_KLINE_TTL_SECONDS` | `120` | Expiração das assinaturas de candles sem renovação |
| `REALTIME_MARKET_FEED` | `true` | O worker `streams` publica tickers/mark/candles públicos |
| `PAPER_ORDER_CHECK_SECONDS` | `10` | Intervalo da tarefa `paper.process_orders` |
| `NEXT_PUBLIC_WS_URL` | vazio | URL do WebSocket no build do frontend; vazio = mesma origem. Em dev sem Caddy use `ws://localhost:8000/api/v1/ws` (o rewrite do Next.js não encaminha WebSocket) |

Nenhuma delas é secreta; os padrões funcionam sem alteração. O Caddy já encaminha `/api/v1/ws` sem buffer
(`flush_interval -1`) e, ao recarregar a configuração, mantém conexões abertas por até 5 min
(`stream_close_delay`); o cliente reconecta sozinho.

## Limitações

- Preços ao vivo dependem do worker `streams` e do Redis; sem eles o painel mostra os dados REST (posições,
  ordens) e o indicador fica "Reconectando…".
- SL/TP de contas paper são verificados a cada `PAPER_ORDER_CHECK_SECONDS` pelo mark price (sem simular gaps
  intra-intervalo); ordens LIMIT paper são preenchidas inteiras no preço limite (sem fila/execução parcial).
- O PnL ao vivo da tela é uma estimativa pelo mark price; os valores oficiais são os da corretora/reconciliador.
- Marcadores do gráfico usam as últimas 200 execuções da conta no símbolo.
