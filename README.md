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
>
> **Aviso regulatório e tributário (residentes no Brasil).** Este software não é corretora, não custodia recursos e não presta consultoria, recomendação de investimento nem serviço jurídico ou contábil. A prestação de serviços de ativos virtuais no Brasil é regulada pelo Banco Central (Resoluções BCB nº 519, 520 e 521) e operações com derivativos podem estar sujeitas às normas da CVM: verifique se a corretora utilizada pode atender residentes no Brasil antes de operar com dinheiro real. Ganhos com criptoativos e em plataformas no exterior podem gerar imposto e obrigações acessórias; as regras mudam com frequência e qualquer referência tributária deve ser **confirmada com um contador ou advogado tributarista**. A interface exige a ciência deste aviso no primeiro acesso (página `/legal`) e oferece exportação CSV de execuções e PnL para a contabilidade.

## Visão geral

Plataforma **self-hosted** para automação e monitoramento de operações em **Binance USDT-M Futures**. Cada usuário organiza suas contas Binance em **workspaces** isolados (multi-tenant), as chaves de API são armazenadas **criptografadas com AES-256-GCM**, e toda ordem passa por um **gerenciador de risco determinístico** (stop loss obrigatório, risco máximo por trade, limite de alavancagem, margem, perda diária máxima e *kill switch* por drawdown) antes de chegar ao executor.

> [!IMPORTANT]
> **Execução simulada (paper) é o padrão.** Ordens reais só são enviadas quando **as duas** condições são verdadeiras:
> `LIVE_TRADING_ENABLED=true` no servidor **e** a conta configurada com `execution_mode=live`. Contas novas usam Testnet + paper.

### Funcionalidades
- **Autenticação**: registro/login, JWT de acesso + refresh em cookies `httpOnly`, logout de todas as sessões, troca de senha, rate limiting de login/registro. O primeiro usuário cadastrado vira super-admin.
- **2FA (TOTP)**: cadastro por QR code (Google Authenticator, Aegis, 1Password…), 10 códigos de recuperação de uso único, verificação no login, desativação com senha + código. Obrigatório para super-admins (painel Admin), para quem pode operar contas **live** e, opcionalmente, para todos os usuários (política no painel Admin) — veja [docs/SECURITY.md](docs/SECURITY.md#autenticação-em-dois-fatores-totp).
- **Workspaces e RBAC**: papéis `admin`, `trader`, `viewer` por workspace; membros, troca de workspace ativo.
- **Contas Binance**: conexão com chaves criptografadas, verificação de credenciais, start/stop, reset do kill switch, limites de risco por conta.
- **Bots**: estratégias `grid`, `ema_cross`, `rsi_bollinger`, `rsi_mean_reversion`, `bollinger_reversion`, `bollinger_squeeze`, `donchian_breakout`, `ema_adx_trend` e `supertrend` (stop ATR obrigatório, ver [docs/STRATEGIES.md](docs/STRATEGIES.md)) com máquina de estados (`idle → running → paused/stopped/error/killed`), executados pelo Celery a cada candle fechado.
- **Scanner de mercado**: ranking dos perpétuos USDT-M por liquidez, tendência (ADX/EMAs), ATR%, momentum, pico de volume, funding e sinais das estratégias; snapshot periódico pelo Celery, tela própria e **seleção automática de símbolos** nos bots (top-N com limite de posições simultâneas, sempre via RiskManager).
- **Notícias & calendário**: RSS (CoinDesk, Cointelegraph, Decrypt, The Block), CryptoPanic e X opcionais, sentimento por LLM compatível com OpenAI (ou léxico local), sentimento agregado por ativo e calendário econômico (ForexFactory). Usados apenas como **filtro** de entradas (bloquear/reduzir/pausar), com alertas no WhatsApp — veja [docs/NEWS.md](docs/NEWS.md).
- **Tempo real & trading manual**: WebSocket autenticado (`/api/v1/ws`) com cotações, mark price/funding, candles, ordens, execuções, posições, bots e alertas ao vivo; barra de cotações, gráfico de candles (lightweight-charts) com EMAs e marcadores de entradas/saídas, PnL ao vivo e **ordens manuais** MARKET/LIMIT com stop loss obrigatório, take profit, fechar/fechar todas e alavancagem — sempre via RiskManager, no modo da conta (paper por padrão) e respeitando os kill switches. Veja [docs/REALTIME.md](docs/REALTIME.md).
- **Backtest**: motor com taxas, slippage, stops com *gap*, mesmas regras de risco do ao vivo; métricas (retorno, drawdown, Sharpe, win rate, profit factor, buy & hold) e curva de patrimônio. Dados públicos da Binance ou CSV próprio.
- **Integração Binance**: cliente REST assinado, WebSockets (user data + mark price) com reconexão, reconciliador de ordens/posições.
- **Dashboard**: patrimônio, PnL, posições, curva de equity, histórico de ordens/execuções/auditoria.
- **Posições consolidadas e PnL & relatórios**: todas as posições abertas do workspace (por conta, modo, sem stop); PnL realizado por dia/semana/mês agrupado por conta, bot, estratégia ou símbolo, com gráficos; exportação CSV (vírgula ou ponto e vírgula) de execuções e de PnL/taxas por dia e conta.
- **Admin**: estatísticas globais, usuários (com status de 2FA e redefinição), política de 2FA, workspaces, auditoria global e **kill switch global**.
- **Monitoramento**: logs JSON com `request_id`, `/health/ready` (beat, workers, WebSockets, ticks), métricas Prometheus (`/metrics`, só rede interna), Prometheus + Alertmanager (regras → WhatsApp/webhook) + Grafana opcionais, dead man's switch externo e tela **Monitoramento** (saúde, alertas, configurações).
- **Alertas via WhatsApp**: WAGO (padrão), wago-api self-hosted, Meta Cloud API, Evolution API ou gateway HTTP genérico; kill switch, limites de risco, ordens, posições, erros, WebSocket, divergências, falhas de tarefas e resumo diário de PnL — veja [docs/ALERTS.md](docs/ALERTS.md).

## Stack

| Camada | Tecnologia |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2 |
| Tarefas assíncronas | Celery (worker + beat) + Redis 7 |
| Banco de dados | PostgreSQL 15 |
| Frontend | Next.js 16 (App Router, Turbopack) + React 19, TypeScript, Tailwind CSS, shadcn/ui (Radix), Recharts |
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
frontend/           Next.js 16 / React 19 (auth, dashboard, contas, bots, backtest, histórico, workspaces, perfil, admin)
docs/               ARCHITECTURE.md, SECURITY.md, ROADMAP.md
scripts/            deploy.sh, backup_db.sh, restore_db.sh
infra/              monitoring/ (Prometheus, regras, Alertmanager, Grafana), backup/ (container e timers de backup)
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
docker compose ps        # aguarde todos os serviços "healthy" (inclusive streams e celery_beat)
```

Serviços: `caddy`, `frontend`, `migrate` (executa `alembic upgrade head` e encerra), `backend`, `celery_worker` (fila `default`: scanner, notícias, calendário, alertas), `celery_worker_trading` (fila `trading`: ticks dos bots, ordens paper, reconciliação, snapshots, reset de risco), `celery_beat`, `streams`, `postgres`, `redis` (+ `prometheus`, `alertmanager`, `node_exporter` e `grafana` no perfil opcional `monitoring`; `backup` no perfil `backup`). Todos têm healthcheck e rotação de logs.

- Aplicação: https://localhost (Caddy, certificado local) — ou http://localhost:3000 direto no Next.js (o Next encaminha `/api/*` e o WebSocket do tempo real ao backend; em desenvolvimento qualquer origem `localhost`/`127.0.0.1` é aceita no WebSocket, em produção a origem precisa estar em `CORS_ORIGINS`)
- API: https://localhost/api/v1 — documentação interativa em http://localhost:8000/docs (desabilitada em produção)
- Health check: http://localhost:8000/health · prontidão (DB/Redis/broker + beat, workers, WebSockets, ticks): http://localhost:8000/health/ready — detalhes em [docs/RUNBOOK.md](docs/RUNBOOK.md#saude)
- Métricas: http://localhost:8000/metrics (não exposto pelo Caddy). Prometheus/Alertmanager/Grafana opcionais:
  `docker compose --profile monitoring up -d` → http://localhost:9090, :9093 e :3001 (defina `GRAFANA_ADMIN_PASSWORD` no `.env`); regras de alerta e roteamento para WhatsApp/webhook em [infra/monitoring/README.md](infra/monitoring/README.md)
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
./scripts/deploy.sh             # valida .env (inclui CORS_ORIGINS com https://DOMAIN), git pull, backup, build, migrações, up e health checks
./scripts/deploy.sh rollback    # volta ao commit do deploy anterior (indica o backup pré-deploy para restaurar)
./scripts/backup_db.sh          # backup manual criptografado (age/gpg) + cópia off-site (rclone) + retenção
./scripts/restore_db.sh --verify   # teste de restauração do último backup num banco temporário
```

Ou manualmente: `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.

- Defina `DOMAIN` e `ACME_EMAIL` no `.env` para que o Caddy emita certificados TLS automaticamente.
- Em produção a API **não inicia** se `JWT_SECRET`/`MASTER_ENCRYPTION_KEY` forem fracos, se `COOKIE_SECURE` for falso ou `DEBUG` verdadeiro.
- `CORS_ORIGINS` precisa conter `https://<DOMAIN>` (também valida o WebSocket do painel).
- Mantenha **uma** instância de `celery_beat` e de `streams`.
- Backups agendados/criptografados/off-site, monitor externo (dead man's switch), go-live e resposta a incidentes
  (incluindo o simulado do kill switch): **[docs/RUNBOOK.md](docs/RUNBOOK.md)**.

### Integração contínua

O workflow em [`docs/ci/ci.yml`](docs/ci/ci.yml) (**inativo até ser copiado** para `.github/workflows/` — veja [`docs/ci/README.md`](docs/ci/README.md)) executa em cada push/PR: `ruff check`, `ruff format --check`, `pytest` (com teste de migração em PostgreSQL 15), `pip-audit`, `eslint`, `tsc --noEmit`, `next build`, `npm audit` (dependências de produção), validação do `docker compose config`, `shellcheck`, `promtool`/`amtool` (regras de alerta) e build das imagens Docker.

## Documentação

- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) (go-live, incidentes, backups, monitor externo) · [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · [`docs/SECURITY.md`](docs/SECURITY.md) · [`docs/ROADMAP.md`](docs/ROADMAP.md) · [`docs/ALERTS.md`](docs/ALERTS.md) (alertas via WhatsApp) · [`docs/STRATEGIES.md`](docs/STRATEGIES.md) (estratégias e scanner) · [`docs/NEWS.md`](docs/NEWS.md) (notícias, sentimento e calendário) · [`docs/REALTIME.md`](docs/REALTIME.md) (painel em tempo real e trading manual) · [`docs/EXCHANGES.md`](docs/EXCHANGES.md) (adaptadores de exchange, viabilidade e prioridades)
- [`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md) — guia para usar agentes de IA (Claude Code, Cursor, Copilot, Aider) e prompts para as etapas restantes; regras em [`AGENTS.md`](AGENTS.md)

## Segurança

Veja [`docs/SECURITY.md`](docs/SECURITY.md). Resumo: nunca faça commit do `.env`; chaves Binance só com permissão de leitura + futuros (sem saque) e whitelist de IP; JWT em cookie `httpOnly`; credenciais criptografadas em repouso; execução simulada por padrão.

## Licença

[MIT](LICENSE) © 2026 E3 Advocacia
