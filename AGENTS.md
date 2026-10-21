# AGENTS.md — regras para agentes de código no ISCTRADE

Este arquivo é lido por agentes de IA (Cursor, Claude Code via `CLAUDE.md`, GitHub Copilot via
`.github/copilot-instructions.md`, Codex, Aider etc.). Guia completo para humanos: `docs/AGENT_GUIDE.md`.

## Projeto
Plataforma self-hosted de trading para **Binance USDT-M Futures** (multi-workspace, paper trading por padrão).
Idioma da documentação e das mensagens de UI: **português do Brasil**. Código, identificadores e mensagens de
commit: **inglês** (Conventional Commits: `feat(api): ...`, `fix(risk): ...`, `docs: ...`).

## Stack
- **Backend** (`backend/`): Python 3.12, FastAPI, SQLAlchemy 2.0 assíncrono, Alembic, Pydantic v2,
  Celery (worker + beat) com Redis 7, PostgreSQL 15, httpx, websockets.
- **Frontend** (`frontend/`): Next.js 14 (App Router), TypeScript, Tailwind, componentes shadcn/ui (Radix), Recharts.
- **Infra**: Docker Compose (`docker-compose.yml` + `docker-compose.prod.yml`), Caddy, `scripts/deploy.sh`.

## Onde fica cada coisa
| Área | Caminho |
|---|---|
| Configuração (env vars) | `backend/app/core/config.py` (+ `.env.example` na raiz) |
| Banco / sessões | `backend/app/core/database.py`, modelos em `backend/app/models/` |
| Migrações | `backend/alembic/versions/` (head atual: `0006_account_exchange.py`) |
| Segurança (JWT, AES-GCM, papéis) | `backend/app/core/security.py`, `backend/app/api/deps.py` |
| Rotas da API (`/api/v1`) | `backend/app/api/v1/endpoints/` e `backend/app/api/v1/router.py` |
| Schemas | `backend/app/schemas/` |
| Risco | `backend/app/services/risk_manager.py` (`RiskManager`) |
| Execução de ordens | `backend/app/services/execution.py` (`OrderService`, `PaperExecutor`, `LiveExecutor`) |
| Kill switch | `backend/app/services/kill_switch.py` |
| Bots | `backend/app/services/bot_runner.py`, `backend/app/strategy/` |
| Backtest | `backend/app/backtest/engine.py`, `backend/app/backtest/data.py` |
| Exchanges (adaptador) | `backend/app/exchanges/` (`base.py` interface `ExchangeAdapter`, `registry.py` por conta, `bybit.py` experimental) — `docs/EXCHANGES.md` |
| Binance | `backend/app/services/binance_client.py` (implementa `ExchangeAdapter`), `binance_ws.py`, `reconciler.py` |
| Tarefas Celery | `backend/app/tasks/celery_app.py` (agenda), `backend/app/tasks/bot_tasks.py`, `backend/app/services/jobs.py` |
| WebSockets (processo) | `backend/app/workers/streams.py`, `backend/app/workers/market_feed.py` |
| Tempo real (WebSocket da API) e trading manual | `backend/app/services/realtime.py`, `api/v1/endpoints/realtime_ws.py`, `services/manual_trading.py`, `services/paper_orders.py`, `api/v1/endpoints/trading.py`, `frontend/lib/realtime.ts` — `docs/REALTIME.md` |
| Auditoria | `backend/app/services/audit.py` (`log_event`), modelo `backend/app/models/audit.py` |
| Frontend: cliente da API | `frontend/lib/api.ts`, tipos em `frontend/lib/types.ts` |
| Frontend: páginas | `frontend/app/dashboard/*`, `frontend/app/admin/`, `frontend/app/auth/` |
| Testes | `backend/tests/` (fixtures em `conftest.py`, fábricas em `factories.py`) |

## Comandos (todos devem passar antes de commit/PR)
```bash
# backend
cd backend
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest -q
# opcional: teste de migração em PostgreSQL real
TEST_POSTGRES_URL=postgresql+asyncpg://USER:SENHA@localhost:5432/DB pytest -q tests/test_migrations.py

# frontend
cd frontend
npm ci
npm run lint && npx tsc --noEmit && npm run build

# infra (se tocar em compose/scripts)
cp .env.example .env && docker compose config -q && docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
shellcheck scripts/*.sh
```

## Regras obrigatórias
1. **Nunca** commitar segredos (tokens, chaves de API, senhas, `.env`). Novas configurações entram em
   `backend/app/core/config.py` com default seguro **e** em `.env.example` com placeholder (`CHANGE_ME_...`).
2. **Paper/Testnet é o padrão.** Ordens reais só com `LIVE_TRADING_ENABLED=true` **e** conta com
   `execution_mode=live`. Não remova nem enfraqueça essa dupla trava; não mude defaults para "live".
3. **Toda ordem passa pelo `RiskManager`** via `OrderService`. Não crie caminhos que chamem
   `BinanceFuturesClient.place_order` diretamente fora de `LiveExecutor`/`kill_switch.py`.
4. **Testes nunca acessam a Binance de verdade.** Use `httpx.MockTransport` no `BinanceFuturesClient`
   (parâmetro `transport=`), `StaticMarketData` ou monkeypatch. Nenhum teste envia ordens. O mesmo vale para
   alertas: provedores de WhatsApp são testados com `httpx.MockTransport` (`build_notifier(..., transport=)`);
   nenhum teste envia mensagens reais.
5. Os testes usam **SQLite** temporário criado com `Base.metadata.create_all` (`tests/conftest.py`), Redis
   inacessível de propósito e `CELERY_DISPATCH_ENABLED=false`. Modelos devem continuar compatíveis com SQLite;
   recursos exclusivos do PostgreSQL (triggers, extensões, hypertables) vão **apenas** em migrações Alembic,
   protegidos por `if op.get_bind().dialect.name == "postgresql"`.
6. Mudou modelo → crie migração nova em `backend/alembic/versions/` (próxima: `0002_...`, `revision = "0002"`,
   `down_revision = "0001"`), com `upgrade()` e `downgrade()` funcionais. Não edite migrações já publicadas.
7. Isolamento por workspace: toda consulta de dados de tenant filtra por `workspace_id` (use
   `get_workspace_context` / `require_workspace_role` de `app/api/deps.py`).
8. Eventos relevantes (risco, kill switch, mudanças de conta, admin) devem ser registrados com
   `app.services.audit.log_event`; nunca registre chaves/segredos (o serviço mascara campos sensíveis, mas não
   passe segredos para ele).
9. Valores monetários usam `Decimal` no backend (colunas `Numeric(28,10)`); no JSON trafegam como string.
10. Frontend: chame a API apenas por `frontend/lib/api.ts`; atualize `frontend/lib/types.ts` junto com os schemas.
11. Trabalhe em branch própria (`feat/<etapa>`), commits pequenos, PR para `main`. Nunca faça force-push em `main`
    nem reescreva histórico publicado.
12. Arquivos em `.github/workflows/` exigem token com escopo `workflow`; o CI pronto está em `docs/ci/ci.yml`.
