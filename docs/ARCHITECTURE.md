# Arquitetura

## Diagrama

```
                       ┌──────────────────────────────┐
     Navegador ──────▶ │        Caddy (HTTPS/TLS)      │
                       └──────────────┬───────────────┘
                     /*               │              /api/*, /health
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
| **Caddy** | Terminação TLS, headers de segurança, roteamento `/api`, `/health` → backend e `/` → frontend. |
| **Next.js** (`frontend`) | UI: autenticação, dashboard (saldo, PnL, posições, curva de patrimônio), contas, bots, backtest, histórico, workspaces, perfil e admin. Fala com a API só via `lib/api.ts` (cookies + header `X-Workspace-ID`). |
| **FastAPI** (`backend`) | Autenticação (JWT em cookie httpOnly com revogação por `token_version`), RBAC, workspaces, contas, bots, backtest, histórico, admin, rate limiting. |
| **migrate** | Serviço one-shot (`alembic upgrade head`) executado antes de API e workers. |
| **celery_worker** | Tarefas: tick dos bots, start/stop de contas, reconciliação, snapshots de equity, reset diário. |
| **celery_beat** | Agendador (uma única instância): `tick-bots` (30 s), `reconcile-accounts` (60 s), `equity-snapshots` (5 min), `daily-risk-reset` (00:00 UTC). |
| **streams** | Processo assíncrono com WebSockets da Binance (user data stream com keepalive do listenKey e mark price), alimentando o `Reconciler` e o cache de preços. |
| **Redis** | Broker do Celery, cache de preços, rate limiting, locks distribuídos por conta/bot. |
| **PostgreSQL** | Persistência (usuários, workspaces, contas com chaves criptografadas, bots, ordens, execuções, posições, equity, backtests, auditoria). |
| **Binance** | REST assinado (HMAC-SHA256) para ordens/conta; WebSocket para mercado e user data stream. |

## Fluxo de uma ordem

1. A estratégia (`app/strategy`: `grid`, `ema_cross`, `rsi_bollinger`) gera *intenções* de ordem a cada candle fechado (`BotRunner.tick`).
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
