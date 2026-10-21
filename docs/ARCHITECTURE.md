# Arquitetura

## Diagrama

```
                       ┌──────────────────────────────┐
     Navegador ──────▶ │        Caddy (HTTPS/TLS)      │
                       └──────────────┬───────────────┘
                     /*               │        /api/*, /health, /health/*  (/metrics → 404)
              ┌───────────────────────┴────────────────────────┐
              ▼                                                ▼
   ┌─────────────────────┐   HTTP (cookie httpOnly)   ┌─────────────────────┐
   │ Next.js 14 (front)  │ ─────────────────────────▶ │  FastAPI (backend)  │
   │ shadcn/ui, Recharts │                            │  /api/v1, RBAC, JWT │
   └─────────────────────┘                            └──────────┬──────────┘
                                                                 │
                         ┌──────────────────────┬────────────────┼──────────────────┐
                         ▼                      ▼                ▼                  │
              ┌────────────────────┐  ┌──────────────────┐  ┌──────────────┐        │
              │  Celery workers    │◀▶│     Redis 7      │  │ PostgreSQL 15│◀───────┘
              │ bots, reconciler,  │  │ broker, cache,   │  │ usuários,    │
              │ equity snapshots   │  │ rate limit, locks│  │ workspaces,  │
              └─────────┬──────────┘  └──────────────────┘  │ contas, logs │
                        │                                   └──────────────┘
                        │  RiskManager.validate_order() ── toda ordem passa aqui
                        ▼
          ┌──────────────────────────────────────────────┐
          │        Binance USDT-M Futures                │
          │  REST  https://fapi.binance.com  (/fapi/*)   │
          │  WS    wss://fstream.binance.com (mercado,   │
          │        user data stream)                     │
          │  (Testnet: testnet.binancefuture.com)        │
          └──────────────────────────────────────────────┘
```

## Componentes

| Componente | Responsabilidade |
|---|---|
| **Caddy** | Terminação TLS, headers de segurança, roteamento `/api`, `/health`, `/health/*` → backend e `/` → frontend. `/metrics` responde 404 (métricas só na rede interna). |
| **Next.js** (`frontend`) | UI: autenticação, dashboard (saldo, PnL, posições, curva de patrimônio), contas, bots, backtest, histórico, workspaces, perfil e admin. Fala com a API só via `lib/api.ts` (cookies + header `X-Workspace-ID`). |
| **FastAPI** (`backend`) | Autenticação (JWT em cookie httpOnly com revogação por `token_version`), RBAC, workspaces, contas, bots, backtest, histórico, admin, rate limiting. |
| **migrate** | Serviço one-shot (`alembic upgrade head`) executado antes de API e workers. |
| **celery_worker** | Tarefas: tick dos bots, scanner de mercado (`scanner.run`), coleta/análise de notícias (`news.ingest`), calendário econômico (`calendar.refresh`/`calendar.alerts`), start/stop de contas, reconciliação, snapshots de equity, reset diário e entrega de alertas (`alerts.process`/`alerts.deliver`, com retries). Métricas na porta interna 9101 (modo multiprocess). |
| **celery_beat** | Agendador (uma única instância): `tick-bots` (30 s), `reconcile-accounts` (60 s), `equity-snapshots` (5 min), `daily-risk-reset` (00:00 UTC), `market-scanner` (`SCANNER_REFRESH_SECONDS`), `news-ingest` (`NEWS_REFRESH_SECONDS`), `calendar-refresh` (1 h), `calendar-alerts` (1 min), `alerts-daily-summary` (`ALERT_DAILY_SUMMARY_HOUR_UTC`:05). |
| **streams** | Processo assíncrono com WebSockets da Binance (user data stream com keepalive do listenKey e mark price), alimentando o `Reconciler` e o cache de preços. Grava um heartbeat no Redis (`streams:heartbeat`) e alerta quando um WebSocket fica fora por mais de `ALERT_WS_DISCONNECT_SECONDS`. Métricas na porta interna 9102. |
| **prometheus / grafana** | Opcionais (`docker compose --profile monitoring up -d`), portas só em `127.0.0.1` (9090 e 3001). Dashboard provisionado em `infra/monitoring/grafana/dashboards`. |
| **Redis** | Broker do Celery, cache de preços e de klines do scanner, rate limiting, locks distribuídos por conta/bot. |
| **PostgreSQL** | Persistência (usuários, workspaces, contas com chaves criptografadas, bots, ordens, execuções, posições, equity, backtests, auditoria). |
| **Binance** | REST assinado (HMAC-SHA256) para ordens/conta; WebSocket para mercado e user data stream. |

## Fluxo de uma ordem

1. A estratégia (`app/strategy`: `grid`, `ema_cross`, `rsi_bollinger` e as técnicas com stop ATR de `technical.py` — veja [STRATEGIES.md](STRATEGIES.md)) gera *intenções* de ordem a cada candle fechado (`BotRunner.tick`).
   Antes de cada **nova entrada** o `BotRunner` aplica os filtros do bot (`_entry_gate`): limite de posições da
   seleção automática, **pausa por evento econômico** e **filtro de notícias/sentimento** (bloquear ou reduzir o
   tamanho). Eles nunca criam ordens e nunca afetam saídas — veja [NEWS.md](NEWS.md).
2. `OrderService` arredonda quantidade/preço pelos filtros do símbolo (`SymbolFilters`), verifica o **kill switch global** e chama `RiskManager.validate_order()` com seis regras: kill switch por drawdown, perda diária, stop loss obrigatório, alavancagem, risco por trade e margem disponível. Ordens *reduce-only* sempre passam.
3. O executor é escolhido por conta (`build_executor`):
   - **`PaperExecutor` (padrão)** — preenchimento simulado com taxa e slippage configuráveis, stops simulados, ledger de paper na própria conta.
   - **`LiveExecutor`** — somente se `LIVE_TRADING_ENABLED=true` **e** a conta estiver em `execution_mode=live`. Envia a ordem + `STOP_MARKET` *reduce-only* à Binance.
4. User data stream (WebSocket) + `Reconciler` mantêm ordens, execuções e posições consistentes com a corretora.
5. `equity` grava snapshots usados no dashboard e nas regras de perda diária/drawdown; ao estourar o drawdown a conta vira `killed` (e, se `KILL_SWITCH_CLOSE_POSITIONS=true`, as posições são encerradas com ordens *reduce-only*).
6. Todos os eventos relevantes vão para a trilha de auditoria (`audit_logs`) com dados sensíveis mascarados.

## Multi-tenancy

- `User` ⟶ possui/participa de `Workspace` (via `WorkspaceMember`, com papel).
- `Workspace` ⟶ possui `Account`s. Toda consulta de conta é filtrada por `workspace_id` do workspace ativo (header `X-Workspace-ID`, validado contra a associação do usuário).
- As credenciais são criptografadas com AES-256-GCM usando o `workspace_id` como *associated data*, impedindo reaproveitamento do ciphertext entre tenants.

## Observabilidade

- **Logs**: JSON por linha (`LOG_FORMAT=json`) com `timestamp`, `level`, `logger`, `message`, `request_id`
  (header `X-Request-ID`, propagado às tasks Celery), `task_id`/`task_name` e campos extras. Segredos são
  mascarados (`app/core/redaction.py`) nos logs, na auditoria e nos alertas.
- **Health**: `GET /health` (liveness) e `GET /health/ready` — PostgreSQL, Redis e broker são críticos (503 quando
  falham); o heartbeat do worker `streams` apenas marca o estado como `degraded`.
- **Métricas** (`app/core/metrics.py`): HTTP (`btp_http_requests_total`, `btp_http_request_duration_seconds`),
  ordens (`btp_orders_total{mode,result,reason}`), ticks de bot, drift do reconciliador, reconexões e estado dos
  WebSockets, tarefas Celery (contagem e duração), alertas e, calculadas no scrape a partir do banco, bots por
  estado, contas por status, equity / PnL do dia / drawdown / posições por conta, kill switch global e idade do
  heartbeat dos streams. `/metrics` só é acessível pela rede interna (o Caddy devolve 404) e pode exigir
  `METRICS_TOKEN` (Bearer). Com vários processos (uvicorn `--workers`, Celery) usa `PROMETHEUS_MULTIPROC_DIR`.
- **Alertas** (`app/services/alerts/`): eventos de auditoria viram alertas enfileirados na sessão e despachados
  somente após o commit; a entrega é feita pelo Celery com antispam no Redis, histórico em `alerts` e
  provedores de WhatsApp plugáveis (`Notifier`). Detalhes em [ALERTS.md](ALERTS.md).
