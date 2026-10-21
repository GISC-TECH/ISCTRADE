# Binance Trading Platform

> [!CAUTION]
> **AVISO DE RISCO — LEIA ANTES DE USAR**
>
> Operar contratos futuros perpétuos (USDT-M) com alavancagem envolve **risco altíssimo de perda total do capital**, inclusive em minutos. Este projeto é **software experimental**, fornecido "no estado em que se encontra" (licença MIT), **sem qualquer garantia** de lucro, desempenho ou ausência de bugs. Nada aqui constitui recomendação de investimento.
>
> - Comece **sempre** pelo backtest e pela **Testnet** da Binance (`BINANCE_USE_TESTNET=true`).
> - Nunca habilite permissão de **saque** nas chaves de API; restrinja por **IP**.
> - Em conta real, use apenas capital que você pode perder integralmente.
> - Você é o único responsável pelas ordens enviadas pela sua instância.

## Visão geral

Plataforma **self-hosted** para automação e monitoramento de operações em **Binance USDT-M Futures**. Cada usuário organiza suas contas Binance em **workspaces** isolados (multi-tenant), as chaves de API são armazenadas **criptografadas com AES-256-GCM**, e toda ordem passa por um **gerenciador de risco determinístico** (stop loss obrigatório, risco máximo por trade, limite de alavancagem, margem, perda diária máxima e *kill switch* por drawdown) antes de chegar ao executor.

> [!IMPORTANT]
> **Execução simulada (paper) é o padrão.** Ordens reais só são enviadas quando **as duas** condições são verdadeiras:
> `LIVE_TRADING_ENABLED=true` no servidor **e** a conta configurada com `execution_mode=live`. Contas novas usam Testnet + paper.

### Funcionalidades
- **Autenticação**: registro/login, JWT de acesso + refresh em cookies `httpOnly`, logout de todas as sessões, troca de senha, rate limiting de login/registro. O primeiro usuário cadastrado vira super-admin.
- **Workspaces e RBAC**: papéis `admin`, `trader`, `viewer` por workspace; membros, troca de workspace ativo.
- **Contas Binance**: conexão com chaves criptografadas, verificação de credenciais, start/stop, reset do kill switch, limites de risco por conta.
- **Bots**: estratégias `grid`, `ema_cross`, `rsi_bollinger`, `rsi_mean_reversion`, `bollinger_reversion`, `bollinger_squeeze`, `donchian_breakout`, `ema_adx_trend` e `supertrend` (stop ATR obrigatório, ver [docs/STRATEGIES.md](docs/STRATEGIES.md)) com máquina de estados (`idle → running → paused/stopped/error/killed`), executados pelo Celery a cada candle fechado.
- **Scanner de mercado**: ranking dos perpétuos USDT-M por liquidez, tendência (ADX/EMAs), ATR%, momentum, pico de volume, funding e sinais das estratégias; snapshot periódico pelo Celery, tela própria e **seleção automática de símbolos** nos bots (top-N com limite de posições simultâneas, sempre via RiskManager).
- **Notícias & calendário**: RSS (CoinDesk, Cointelegraph, Decrypt, The Block), CryptoPanic e X opcionais, sentimento por LLM compatível com OpenAI (ou léxico local), sentimento agregado por ativo e calendário econômico (ForexFactory). Usados apenas como **filtro** de entradas (bloquear/reduzir/pausar), com alertas no WhatsApp — veja [docs/NEWS.md](docs/NEWS.md).
- **Backtest**: motor com taxas, slippage, stops com *gap*, mesmas regras de risco do ao vivo; métricas (retorno, drawdown, Sharpe, win rate, profit factor, buy & hold) e curva de patrimônio. Dados públicos da Binance ou CSV próprio.
- **Integração Binance**: cliente REST assinado, WebSockets (user data + mark price) com reconexão, reconciliador de ordens/posições.
- **Dashboard**: patrimônio, PnL, posições, curva de equity, histórico de ordens/execuções/auditoria.
- **Admin**: estatísticas globais, usuários, workspaces, auditoria global e **kill switch global**.
- **Monitoramento**: logs JSON com `request_id`, `/health/ready`, métricas Prometheus (`/metrics`, só rede interna), Prometheus + Grafana opcionais e tela **Monitoramento** (saúde, alertas, configurações).
- **Alertas via WhatsApp**: WAGO (padrão), wago-api self-hosted, Meta Cloud API, Evolution API ou gateway HTTP genérico; kill switch, limites de risco, ordens, posições, erros, WebSocket, divergências, falhas de tarefas e resumo diário de PnL — veja [docs/ALERTS.md](docs/ALERTS.md).

## Stack

| Camada | Tecnologia |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2 |
| Tarefas assíncronas | Celery (worker + beat) + Redis 7 |
| Banco de dados | PostgreSQL 15 |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind CSS, shadcn/ui (Radix), Recharts |
| Proxy / TLS | Caddy 2 (HTTPS automático) |
| Infra | Docker / Docker Compose, GitHub Actions (CI) |
| Corretora | Binance USDT-M Futures (REST `/fapi` + WebSocket) |

## Estrutura

```
backend/
  app/api/          rotas FastAPI (auth, workspaces, accounts, bots, strategies/backtest, history, admin)
  app/models/       modelos SQLAlchemy          app/schemas/   schemas Pydantic
  app/services/     risco, execução (paper/live), cliente/WS Binance, reconciliador, equity, auditoria
  app/strategy/     indicadores, estratégias e máquina de estados dos bots
  app/backtest/     motor de backtest e carregadores de dados
  app/tasks/        Celery (worker + agenda do beat)     app/workers/   processo de WebSockets
  alembic/          migrações                   tests/         pytest (Binance sempre mockada)
frontend/           Next.js 14 (auth, dashboard, contas, bots, backtest, histórico, workspaces, perfil, admin)
docs/               ARCHITECTURE.md, SECURITY.md, ROADMAP.md
scripts/            deploy.sh, backup_db.sh
docs/ci/            workflow de CI do GitHub Actions (lint + testes + build) — ativação pendente
```

## Fases de desenvolvimento

1. **Backtest** — validar estratégias com dados históricos (klines) e custos realistas.
2. **Dry-run / paper trading** — rodar em tempo real na Testnet ou em modo simulado, sem capital real.
3. **Conta real mínima** — capital pequeno, alavancagem baixa, limites de risco conservadores.
4. **Monitoramento e logs** ✅ — logs JSON, `/health/ready`, métricas Prometheus/Grafana, auditoria e alertas via WhatsApp ([docs/ALERTS.md](docs/ALERTS.md)).
5. **Escala de capital** — aumento gradual, somente após métricas estáveis nas fases anteriores.

Detalhes e checklists em [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Como rodar localmente

### Pré-requisitos
- Docker e Docker Compose v2.24+
- (Opcional, para desenvolvimento sem Docker) Python 3.12, Node.js 20, PostgreSQL 15 e Redis 7

### Com Docker Compose

```bash
cp .env.example .env
# Edite .env e substitua TODOS os valores CHANGE_ME. Para gerar as chaves:
python3 -c "import secrets; print(secrets.token_urlsafe(64))"                 # JWT_SECRET
python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())" # MASTER_ENCRYPTION_KEY

docker compose up -d --build
docker compose ps        # aguarde backend/frontend/celery_worker "healthy"
```

Serviços: `caddy`, `frontend`, `migrate` (executa `alembic upgrade head` e encerra), `backend`, `celery_worker`, `celery_beat`, `streams`, `postgres`, `redis` (+ `prometheus` e `grafana` no perfil opcional `monitoring`).

- Aplicação: https://localhost (Caddy, certificado local) — ou http://localhost:3000 direto no Next.js
- API: https://localhost/api/v1 — documentação interativa em http://localhost:8000/docs (desabilitada em produção)
- Health check: http://localhost:8000/health · prontidão (DB/Redis/broker/streams): http://localhost:8000/health/ready
- Métricas: http://localhost:8000/metrics (não exposto pelo Caddy). Prometheus/Grafana opcionais:
  `docker compose --profile monitoring up -d` → http://localhost:9090 e http://localhost:3001 (defina `GRAFANA_ADMIN_PASSWORD` no `.env`)
- Alertas via WhatsApp: preencha `WHATSAPP_*` no `.env` (ex.: `WHATSAPP_ENABLED=true`, `WHATSAPP_PROVIDER=wago`, `WHATSAPP_API_TOKEN`, `WHATSAPP_TO`) — passo a passo em [docs/ALERTS.md](docs/ALERTS.md)

Acesse `/auth/register` e crie o primeiro usuário (vira super-admin). Depois, em **Contas**, conecte uma conta (Testnet + paper por padrão) e crie bots em **Bots**. Depois de criar os usuários necessários, defina `ALLOW_REGISTRATION=false`.

### Backend sem Docker

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

ruff check . && ruff format --check .
pytest                                   # SQLite temporário; Binance sempre mockada
TEST_POSTGRES_URL=postgresql+asyncpg://user:senha@localhost:5432/test pytest tests/test_migrations.py  # opcional

# Com PostgreSQL e Redis acessíveis (ajuste DATABASE_URL/REDIS_URL no ambiente):
alembic upgrade head
uvicorn app.main:app --reload
celery -A app.tasks.celery_app worker --loglevel=info
celery -A app.tasks.celery_app beat --loglevel=info
python -m app.workers.streams
```

Nova migração após alterar modelos: `alembic revision --autogenerate -m "descricao"` e revise o arquivo gerado.

### Frontend sem Docker

```bash
cd frontend
npm ci
BACKEND_INTERNAL_URL=http://localhost:8000 npm run dev   # http://localhost:3000; /api/* é encaminhado ao backend
npm run lint && npx tsc --noEmit && npm run build
```

Em desenvolvimento sem HTTPS, use `COOKIE_SECURE=false` no backend.

### Produção

```bash
./scripts/deploy.sh             # valida .env, git pull, backup, build, migrações, up e health checks
./scripts/deploy.sh rollback    # volta ao commit do deploy anterior
./scripts/backup_db.sh          # backup manual (retém os 14 mais recentes em ./backups)
```

Ou manualmente: `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.

- Defina `DOMAIN` e `ACME_EMAIL` no `.env` para que o Caddy emita certificados TLS automaticamente.
- Em produção a API **não inicia** se `JWT_SECRET`/`MASTER_ENCRYPTION_KEY` forem fracos, se `COOKIE_SECURE` for falso ou `DEBUG` verdadeiro.
- Mantenha **uma** instância de `celery_beat` e de `streams`.

### Integração contínua

O workflow em [`docs/ci/ci.yml`](docs/ci/ci.yml) (mova para `.github/workflows/` para ativar — veja [`docs/ci/README.md`](docs/ci/README.md)) executa em cada push/PR: `ruff check`, `ruff format --check`, `pytest` (com teste de migração em PostgreSQL 15), `next lint`, `tsc --noEmit`, `next build`, validação do `docker compose config`, `shellcheck` e build das imagens Docker.

## Documentação

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · [`docs/SECURITY.md`](docs/SECURITY.md) · [`docs/ROADMAP.md`](docs/ROADMAP.md) · [`docs/ALERTS.md`](docs/ALERTS.md) (alertas via WhatsApp) · [`docs/STRATEGIES.md`](docs/STRATEGIES.md) (estratégias e scanner) · [`docs/NEWS.md`](docs/NEWS.md) (notícias, sentimento e calendário)
- [`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md) — guia para usar agentes de IA (Claude Code, Cursor, Copilot, Aider) e prompts para as etapas restantes; regras em [`AGENTS.md`](AGENTS.md)

## Segurança

Veja [`docs/SECURITY.md`](docs/SECURITY.md). Resumo: nunca faça commit do `.env`; chaves Binance só com permissão de leitura + futuros (sem saque) e whitelist de IP; JWT em cookie `httpOnly`; credenciais criptografadas em repouso; execução simulada por padrão.

## Licença

[MIT](LICENSE) © 2026 E3 Advocacia
