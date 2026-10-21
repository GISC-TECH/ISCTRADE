# Guia: usando agentes de IA para concluir o ISCTRADE

Este guia explica como conectar agentes de código (Claude Code, Cursor, GitHub Copilot, Aider ou scripts próprios)
ao repositório **GISC-TECH/ISCTRADE**, o que já está pronto e como delegar cada etapa que falta com prompts
prontos para colar. Tudo aqui se baseia no código atual do repositório.

> [!CAUTION]
> **Aviso de risco.** Operar futuros perpétuos alavancados pode causar **perda total do capital** em minutos.
> Código gerado por IA pode conter bugs sutis. Este projeto é software experimental, sem garantia (licença MIT),
> e nada aqui é recomendação de investimento. Valide tudo em **backtest → paper → Testnet** antes de qualquer
> capital real, revise cada diff e use apenas dinheiro que você pode perder integralmente.

## Sumário
1. [Preparando o ambiente e os agentes](#1-preparando-o-ambiente-e-os-agentes)
2. [O que já está pronto](#2-o-que-já-está-pronto)
3. [Etapas restantes (o que existe e o que falta)](#3-etapas-restantes-o-que-existe-e-o-que-falta)
4. [Fluxo de delegação e prompts por etapa](#4-fluxo-de-delegação-e-prompts-por-etapa)
5. [Regras de segurança](#5-regras-de-segurança)

---

## 1. Preparando o ambiente e os agentes

### 1.1 Clonar e configurar

```bash
git clone https://github.com/GISC-TECH/ISCTRADE.git
cd ISCTRADE
cp .env.example .env
# Substitua TODOS os valores CHANGE_ME do .env:
python3 -c "import secrets; print(secrets.token_urlsafe(64))"                  # JWT_SECRET
python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())" # MASTER_ENCRYPTION_KEY
# POSTGRES_PASSWORD: escolha uma senha e repita-a dentro de DATABASE_URL
```

O `.env` está no `.gitignore` (assim como `.env.*`). Nunca o adicione ao git.

### 1.2 Rodando com Docker Compose (recomendado)

```bash
docker compose up -d --build
docker compose ps            # aguarde backend, frontend e celery_worker ficarem "healthy"
docker compose logs -f backend celery_worker
```

Serviços definidos em `docker-compose.yml`:

| Serviço | O que faz |
|---|---|
| `postgres` | PostgreSQL 15 (`postgres:15-alpine`), volume `btp_postgres_data` |
| `redis` | Redis 7 (broker do Celery, cache de preços, rate limiting, locks) |
| `migrate` | Executa `alembic upgrade head` uma vez e encerra; os demais serviços esperam por ele |
| `backend` | FastAPI (`uvicorn app.main:app`), porta `127.0.0.1:8000` em dev |
| `celery_worker` | `celery -A app.tasks.celery_app worker` (ticks de bots, reconciliação, snapshots) |
| `celery_beat` | `celery -A app.tasks.celery_app beat` — **uma única instância** |
| `streams` | `python -m app.workers.streams` (WebSockets da Binance) — **uma única instância** |
| `frontend` | Next.js 14 (porta `127.0.0.1:3000`) |
| `caddy` | Proxy HTTPS: `/api/*`, `/health`, `/docs` → backend; resto → frontend |

Acesso: https://localhost (Caddy) ou http://localhost:3000; API em `/api/v1`; documentação interativa em
http://localhost:8000/docs (desligada em produção). Crie o primeiro usuário em `/auth/register` — ele vira super-admin.

Produção: `./scripts/deploy.sh` (valida `.env`, faz backup, build, migrações, sobe e checa health) — ou
`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.

### 1.3 Rodando sem Docker

Requer Python 3.12, Node.js 20, PostgreSQL 15 e Redis 7 locais. As configurações do backend vêm de variáveis de
ambiente ou de um arquivo `.env` no diretório onde o processo roda (`backend/.env`, também ignorado pelo git).
Ajuste `DATABASE_URL`/`REDIS_URL`/`CELERY_*` para `localhost` e use `COOKIE_SECURE=false` sem HTTPS.

```bash
# Backend
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload                               # terminal 1
celery -A app.tasks.celery_app worker --loglevel=info       # terminal 2
celery -A app.tasks.celery_app beat --loglevel=info         # terminal 3
python -m app.workers.streams                               # terminal 4 (opcional)

# Frontend
cd frontend
npm install           # ou npm ci
BACKEND_INTERNAL_URL=http://localhost:8000 npm run dev      # http://localhost:3000, /api/* é encaminhado ao backend
```

### 1.4 Testes, lint e build (o "portão" de qualidade)

```bash
cd backend
ruff check . && ruff format --check .
pytest -q                          # SQLite temporário, Redis desligado, Binance sempre mockada
TEST_POSTGRES_URL=postgresql+asyncpg://USER:SENHA@localhost:5432/DB_DE_TESTE pytest -q tests/test_migrations.py

cd ../frontend
npm run lint && npx tsc --noEmit && npm run build

# se mexer em infra
cp .env.example .env && docker compose config -q && docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
shellcheck scripts/*.sh
```

O workflow de CI com todos esses passos está pronto em `docs/ci/ci.yml` (ativação na [Etapa 0](#etapa-0--ativar-o-ci)).

### 1.5 Arquivos de instrução para agentes (já no repositório)

| Arquivo | Para quem | Conteúdo |
|---|---|---|
| `AGENTS.md` | Cursor, Codex, Aider e outros que leem `AGENTS.md` | Regras compartilhadas: stack, onde fica cada coisa, comandos, regras obrigatórias |
| `CLAUDE.md` | Claude Code | Aponta para `AGENTS.md` + comandos de verificação |
| `.github/copilot-instructions.md` | GitHub Copilot (chat e coding agent) | Resumo das regras + link para `AGENTS.md` |

Mantenha **`AGENTS.md` como fonte única** — ao mudar uma convenção, edite-o e, se preciso, os resumos.

### 1.6 Configurando cada ferramenta

**Claude Code**
1. Instale o Claude Code e rode `claude` na raiz do repositório. O `CLAUDE.md` é carregado automaticamente.
2. Peça para ler `AGENTS.md` e este guia antes da primeira tarefa.
3. Permita apenas os comandos necessários (pytest, ruff, npm, git) e mantenha a confirmação para `git push`.

**Cursor**
1. Abra a pasta do repositório. O Cursor lê `AGENTS.md` na raiz como regras do projeto.
2. Opcional: crie regras específicas em `.cursor/rules/` (ex.: `.cursor/rules/backend.mdc` com
   `globs: backend/**` apontando para `AGENTS.md`). Não é obrigatório.
3. Use o modo agente com uma branch por etapa e cole o prompt da etapa (seção 4).

**GitHub Copilot**
1. Chat/edits no VS Code e o **coding agent** do GitHub usam `.github/copilot-instructions.md` automaticamente.
2. Coding agent: crie uma *issue* com o prompt da etapa e atribua ao Copilot; ele abre um PR. Revise como qualquer PR.
3. O coding agent precisa de CI para validar mudanças — ative o CI primeiro (Etapa 0).

**Agentes próprios (API de LLM, Aider etc.)**
- **Aider**: `pip install aider-chat`, depois na raiz: `aider --read AGENTS.md --read docs/AGENT_GUIDE.md`
  e cole o prompt da etapa. Configure a chave do provedor por variável de ambiente (nunca em arquivo versionado).
- **Script próprio**: envie `AGENTS.md` como *system prompt*, mais os arquivos citados no prompt da etapa como
  contexto; aplique o patch retornado numa branch e rode o portão de qualidade (1.4) antes de commitar.
  Guarde a chave da API em variável de ambiente (`export ...`), nunca no repositório.

### 1.7 TimescaleDB (opcional — **não** faz parte da stack atual)

O projeto usa **PostgreSQL 15 puro** (`postgres:15-alpine`). Não há TimescaleDB nem tabela de candles: o backtest
baixa klines sob demanda (`backend/app/backtest/data.py`, `fetch_candles`) ou lê CSV. TimescaleDB só vale a pena se
você quiser **armazenar candles** localmente e/ou acelerar consultas de `equity_snapshots`. É uma etapa opcional com
prompt próprio: [Etapa 7](#etapa-7-opcional--timescaledb).

---

## 2. O que já está pronto

| Etapa | Principais arquivos |
|---|---|
| **Banco + migrações** | `backend/app/models/` (`user.py`, `workspace.py`, `account.py`, `bot.py`, `trading.py`, `audit.py`, `backtest.py`, `system.py`), `backend/alembic/versions/0001_initial_schema.py`, `backend/app/core/database.py`, `tests/test_models.py`, `tests/test_migrations.py` |
| **Núcleo de trading + RiskManager** | `backend/app/services/risk_manager.py` (6 regras: kill switch por drawdown, perda diária, stop obrigatório, alavancagem, risco por trade, margem), `execution.py` (`OrderService`, `PaperExecutor`, `LiveExecutor`), `position_math.py`, `symbol_filters.py`, `kill_switch.py`, `bot_runner.py`, `backend/app/strategy/` (`grid`, `ema_cross`, `rsi_bollinger`, `technical.py` com as estratégias de stop ATR, `indicators.py`, `state_machine.py`), `scanner.py` (scanner de mercado e seleção automática de símbolos) — veja [STRATEGIES.md](STRATEGIES.md) |
| **Backtest** | `backend/app/backtest/engine.py` (`BacktestEngine`, taxas, slippage, stops com gap, métricas), `backend/app/backtest/data.py`, rota `POST /api/v1/strategies/backtest`, `tests/test_backtest.py` |
| **Integração Binance** | `backend/app/services/binance_client.py` (`BinanceFuturesClient`, REST assinado), `binance_ws.py` (`MarketStream`, `UserDataStream`), `reconciler.py` (`Reconciler`), `prices.py`, `backend/app/workers/streams.py` (`StreamSupervisor`) |
| **Tarefas Celery** | `backend/app/tasks/celery_app.py` (beat: `tick-bots`, `reconcile-accounts`, `equity-snapshots`, `daily-risk-reset`, `alerts-daily-summary`, `market-scanner`, `news-ingest`, `calendar-refresh`, `calendar-alerts`), `backend/app/tasks/bot_tasks.py`, `backend/app/services/jobs.py`, `backend/app/services/dispatch.py` |
| **API** | `backend/app/api/v1/endpoints/` (`auth`, `workspaces`, `accounts`, `bots`, `strategies`, `scanner`, `news` (+ `calendar`), `history`, `admin`), `backend/app/api/deps.py`, `backend/app/schemas/`, `tests/test_api_e2e.py` |
| **Dashboard (frontend)** | `frontend/app/dashboard/` (visão geral, `accounts`, `bots`, `strategies`, `scanner`, `news`, `calendar`, `history`, `workspaces`, `profile`), `frontend/app/admin/page.tsx`, `frontend/lib/api.ts`, `frontend/middleware.ts`, `frontend/components/` |
| **Monitoramento e alertas (Etapa 1 ✅)** | `backend/app/core/logging.py` (logs JSON + `request_id`), `core/redaction.py`, `core/middleware.py`, `core/metrics.py` (Prometheus), `services/health.py` (`/health/ready`), `tasks/signals.py`, `services/alerts/` (eventos, `Notifier`, provedores WhatsApp `wago`/`wago_api`/`meta_cloud`/`evolution`/`generic_http`, antispam, entrega), `tasks/alert_tasks.py`, `api/v1/endpoints/alerts.py` e `monitoring.py`, migração `0002_alerts.py`, `frontend/app/dashboard/monitoring/page.tsx`, `infra/monitoring/` (Prometheus + Grafana), `docs/ALERTS.md`, testes `test_observability.py` e `test_alerts.py` |
| **Estratégias técnicas + scanner** | `backend/app/strategy/technical.py`, `backend/app/services/scanner.py`, `tasks/scanner_tasks.py`, `api/v1/endpoints/scanner.py`, `schemas/bot_options.py` (`auto_symbols`), migração `0003`, `frontend/app/dashboard/scanner/page.tsx`, testes `test_technical_strategies.py` e `test_scanner.py` — [STRATEGIES.md](STRATEGIES.md) |
| **Notícias, sentimento e calendário** | `backend/app/services/news/` (`sources.py`, `tagging.py`, `sentiment.py`, `gate.py`, `calendar.py`, `store.py`), `tasks/news_tasks.py`, `api/v1/endpoints/news.py`, `schemas/bot_options.py` (`news_filter`, `event_pause`), `BotRunner._entry_gate`, migração `0004`, `frontend/app/dashboard/news` e `calendar`, `tests/test_news.py` — [NEWS.md](NEWS.md) |
| **Ops** | `docker-compose.yml` (perfil `monitoring`), `docker-compose.prod.yml`, `Caddyfile`, `scripts/deploy.sh`, `scripts/backup_db.sh`, `docs/ci/ci.yml` |

---

## 3. Etapas restantes (o que existe e o que falta)

| # | Etapa | Já existe | Falta |
|---|---|---|---|
| 0 | **Ativar CI** | Workflow completo em `docs/ci/ci.yml` | Mover para `.github/workflows/ci.yml` com um token/conta com escopo `workflow`; exigir CI verde na proteção da branch `main` |
| 1 | **Monitoramento e logs** ✅ | **Concluída**: logs JSON com `request_id`/`task_id`, `/health/ready`, `/metrics` (interno/token), Prometheus + Grafana (perfil `monitoring`), alertas via **WhatsApp** com histórico e tela Monitoramento | — (melhorias futuras: regras no Alertmanager) |
| 2 | **Auditoria à prova de adulteração** | `audit_logs` + `log_event` com mascaramento, telas de auditoria (workspace e admin) | Encadeamento por hash (`prev_hash`/`hash`), bloqueio de UPDATE/DELETE no PostgreSQL, verificação da cadeia |
| 3 | **Alertas no Telegram** | Pipeline completo de alertas (`app/services/alerts/`, interface `Notifier`, entrega via Celery, antispam, histórico, configurações por workspace) com canal WhatsApp | Implementar `TelegramNotifier` (e opcionalmente e-mail) sobre a mesma interface e permitir escolher canais por workspace |
| 4 | **Kill switch (completar)** | Kill switch por conta (drawdown no `RiskManager`, `trigger_account_kill_switch`), reset via `POST /accounts/{id}/reset-kill-switch` e UI, kill switch global via `POST /admin/kill-switch` e painel Admin | Acionamento **manual** por conta (endpoint + botão "parar tudo e fechar posições"), comando `/kill` pelo Telegram, teste controlado em produção |
| 5 | **Multi-conta / workspaces (completar)** | Workspaces, membros, papéis, limites de risco **por conta** | Limites **agregados** por workspace (exposição total, perda diária somada), visão consolidada das contas |
| 6 | **Dashboard: PnL, posições e scanner** | Dashboard por conta (equity, PnL do dia, posições, curva) | PnL por período (dia/semana/mês) e por bot/símbolo, posições de todas as contas, **scanner de mercado** (variação 24h, volume, funding) |
| 7 | **TimescaleDB (opcional)** | Nada (PostgreSQL 15 puro) | Imagem `timescale/timescaledb` pg15, tabela de candles, hypertables |
| 8 | **Validação em paper trading (dry-run)** | Modo paper por padrão, Testnet, snapshots de equity, backtest | Relatório paper × backtest, critérios objetivos de aprovação, **≥ 4 semanas** rodando em paper (operacional) |
| 9 | **Live com capital pequeno** | Dupla trava (`LIVE_TRADING_ENABLED` + `execution_mode=live`), `STOP_MARKET` de proteção, verificação de credenciais | Teto rígido de capital/notional para contas live, checklist de pré-voo, confirmação explícita na UI, runbook testado |

Outros pendentes do `docs/ROADMAP.md` que podem virar etapas extras: custos de *funding* e walk-forward no
backtest, 2FA (TOTP), rotação automática da `MASTER_ENCRYPTION_KEY`.

**Ordem recomendada:** 0 → ~~1~~ → 2 → 3 → 4 → 5 → 6 → (7) → 8 → 9. Live (9) só depois de 8 aprovada.

---

## 4. Fluxo de delegação e prompts por etapa

### 4.1 Fluxo recomendado (uma etapa = uma branch = um PR)

1. Atualize a `main`: `git checkout main && git pull --ff-only`.
2. Crie a branch: `git checkout -b feat/<etapa>` (ex.: `feat/telegram-alerts`).
3. Cole o prompt da etapa no agente. Peça que ele **leia `AGENTS.md` primeiro**.
4. O agente implementa, adiciona testes e roda o portão de qualidade (1.4).
5. O agente faz commits (Conventional Commits) e abre um PR (ou você faz `git push -u origin feat/<etapa>` e abre o PR).
6. **Você revisa o diff** (veja checklist abaixo) e espera o **CI verde**.
7. Merge na `main` (squash ou merge commit). Nunca force-push na `main`.
8. Deploy: `./scripts/deploy.sh` no servidor; confira `docker compose ps` e os logs.

**Checklist de revisão de diff de agente**
- Nenhum segredo, token, chave ou `.env` no diff (`git diff main... | grep -iE "secret|token|key|password"`).
- Nenhum default mudou para live/mainnet; a dupla trava de ordens reais continua intacta.
- Nenhuma ordem fora de `OrderService`/`LiveExecutor`/`kill_switch.py`.
- Testes novos cobrem o comportamento e **mockam a Binance**.
- Migração nova tem `downgrade()` e não altera `0001_initial_schema.py`.
- Consultas novas filtram por `workspace_id`.

### 4.2 Prompts por etapa

Cada prompt é autocontido: copie o bloco inteiro.

#### Etapa 0 — Ativar o CI

Esta etapa é quase toda manual (exige permissão `workflow`):

```bash
git checkout main && git pull --ff-only
mkdir -p .github/workflows
git mv docs/ci/ci.yml .github/workflows/ci.yml
git commit -m "ci: enable GitHub Actions workflow"
git push origin main     # com uma conta/token que tenha o escopo "workflow"
```

Depois, em *Settings → Branches*, proteja a `main` exigindo os checks `Backend (ruff + pytest)`,
`Frontend (lint + tsc + build)` e `docker compose config`. Prompt para um agente ajustar o restante:

```text
Leia AGENTS.md. Tarefa: finalizar a ativação do CI do ISCTRADE.
Contexto: o workflow foi movido de docs/ci/ci.yml para .github/workflows/ci.yml.
Faça:
1. Remova docs/ci/README.md (instruções de ativação) e atualize as referências em README.md (seção "Integração contínua"/estrutura) e docs/ROADMAP.md (marque "Ativar o workflow" como concluído).
2. Adicione um badge de status do workflow "CI" no topo do README.md (repositório GISC-TECH/ISCTRADE).
3. Não altere os jobs do workflow, exceto se algum passo falhar de fato no GitHub Actions; nesse caso explique a causa no PR.
Critérios de aceite: nenhum link quebrado para docs/ci; README e ROADMAP consistentes.
Comandos que devem passar: cd backend && ruff check . && ruff format --check . && pytest -q; cd frontend && npm run lint && npx tsc --noEmit && npm run build.
Branch: feat/ci-activation. Commit: "docs(ci): finalize CI activation". Abra um PR para main.
```

#### Etapa 1 — Monitoramento e logs ✅ concluída

Implementada no código (não é preciso delegar). Resumo para agentes que forem mexer nessa área:

- Logs: `configure_logging()` (`app/core/logging.py`) é chamado na API, no Celery (sinal `setup_logging`) e no
  worker `streams`. Use `logger.info("mensagem", extra={...})` — os extras viram campos JSON e passam pelo
  mascaramento de segredos.
- Métricas: declare novas métricas em `app/core/metrics.py` (labels de cardinalidade limitada — nada de IDs em
  contadores) e incremente com `safe_inc`. Gauges vindos do banco ficam em `DatabaseCollector` +
  `services/metrics_snapshot.py`.
- Alertas: para alertar sobre algo novo, registre o evento em `services/alerts/events.py`, mapeie o evento de
  auditoria em `alert_from_audit` (`services/alerts/service.py`) **ou** chame `emit_alert(...)` fora de transações.
  Nunca envie mensagens de forma síncrona no caminho de trading. Testes com `httpx.MockTransport` — nunca a API real.
- Documentação do usuário: `docs/ALERTS.md`.

#### Etapa 2 — Auditoria à prova de adulteração

```text
Leia AGENTS.md. Tarefa: tornar a trilha de auditoria do ISCTRADE evidente a adulterações (hash encadeado).
Contexto: modelo backend/app/models/audit.py (tabela audit_logs: id, created_at, event, level, user_id, workspace_id, account_id, ip_address, data JSON); serviço backend/app/services/audit.py (log_event com _sanitize). Leituras em GET /api/v1/history/audit e GET /api/v1/admin/audit.
Implemente:
1. Colunas novas em AuditLog: seq (inteiro crescente, único), prev_hash (String(64)) e hash (String(64)), compatíveis com SQLite.
2. Em log_event: hash = sha256 de JSON canônico (sort_keys, separators compactos) contendo seq, created_at ISO, event, level, user_id, workspace_id, account_id, ip_address, data já sanitizado e prev_hash. prev_hash = hash do registro anterior (cadeia global) ou "0"*64 no primeiro. Serialize o acesso ao último registro (no PostgreSQL use pg_advisory_xact_lock; no SQLite não é necessário) para não bifurcar a cadeia.
3. backend/app/services/audit.py: verify_chain(db, limit=None) -> resultado com ok, total verificado e o primeiro seq inválido.
4. Migração backend/alembic/versions/0005_audit_hash_chain.py (revision "0005", down_revision "0004" — confira o head atual com `alembic heads` e use o próximo número livre): adiciona as colunas; preenche seq/prev_hash/hash dos registros existentes em ordem de created_at,id; índice único em seq; SOMENTE no PostgreSQL cria trigger que impede UPDATE e DELETE em audit_logs (RAISE EXCEPTION). downgrade remove trigger e colunas. Atenção: a FK workspace_id usa ondelete CASCADE — documente que excluir um workspace passará a falhar com o trigger, e troque a FK para SET NULL na migração (com downgrade revertendo).
5. Endpoint GET /api/v1/admin/audit/verify (super-admin) retornando o resultado de verify_chain; botão "Verificar integridade" na aba de auditoria de frontend/app/admin/page.tsx (via frontend/lib/api.ts e tipos em frontend/lib/types.ts).
6. Atualize docs/SECURITY.md (remova a limitação "não é imutável" e descreva o mecanismo e seus limites: quem tem acesso de superusuário ao banco ainda pode recriar a cadeia; recomende exportar o último hash periodicamente).
Testes: backend/tests/test_audit.py — cadeia válida após vários log_event; alterar data de um registro diretamente no banco faz verify_chain apontar o seq; o endpoint verify exige super-admin; test_migrations.py continua passando (upgrade/downgrade/check) com TEST_POSTGRES_URL.
Comandos que devem passar: cd backend && ruff check . && ruff format --check . && pytest -q && TEST_POSTGRES_URL=... pytest -q tests/test_migrations.py; cd frontend && npm run lint && npx tsc --noEmit && npm run build.
Branch: feat/audit-hash-chain. Abra PR para main.
```

#### Etapa 3 — Alertas no Telegram (novo canal)

```text
Leia AGENTS.md e docs/ALERTS.md. Tarefa: adicionar o Telegram como segundo canal do pipeline de alertas existente do ISCTRADE.
Contexto: backend/app/services/alerts/ já tem eventos, severidades, antispam, histórico (tabela alerts), entrega via Celery (alerts.process/alerts.deliver com retries) e a interface Notifier (notifier.py) implementada pelos provedores de WhatsApp (whatsapp.py, build_notifier). Hoje deliver_alert usa um único notifier e destinatários = números de WhatsApp.
Implemente:
1. Configs em backend/app/core/config.py e .env.example (placeholders): TELEGRAM_ENABLED (default false), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (padrão do servidor).
2. backend/app/services/alerts/telegram.py: TelegramNotifier (channel="telegram") fazendo POST https://api.telegram.org/bot<TOKEN>/sendMessage com chat_id e text (converta o *negrito* do WhatsApp para HTML ou envie texto puro); o token nunca pode aparecer em logs, erros ou no histórico (a URL contém o token: trate exceções sem repassar a URL).
3. Suporte a múltiplos canais: build_notifiers() retornando os canais configurados; deliver_alert entrega em cada canal (status por canal em data, status geral sent/partial/failed). AlertSettings ganha telegram_chat_ids (JSON) com migração própria (o próximo número livre após o head atual) — testada com upgrade/downgrade/check.
4. Endpoints existentes (/api/v1/alerts/settings, /alerts/test) passam a aceitar/mostrar os chats do Telegram; tela frontend/app/dashboard/monitoring/page.tsx com o campo novo.
5. Atualize docs/ALERTS.md (seção Telegram com curl de teste) e README.
Testes (backend/tests/test_alerts.py): URL e corpo corretos com httpx.MockTransport (nunca a API real), token ausente de mensagens de erro e do histórico, entrega em dois canais com falha parcial, migração.
Comandos que devem passar: cd backend && ruff check . && ruff format --check . && pytest -q; frontend: npm run lint && npx tsc --noEmit && npm run build.
Branch: feat/telegram-alerts. Abra PR para main.
```

#### Etapa 4 — Kill switch (completar)

```text
Leia AGENTS.md. Tarefa: completar o kill switch do ISCTRADE.
Já existe: regra de drawdown no RiskManager (backend/app/services/risk_manager.py), trigger_account_kill_switch/reset_account_kill_switch e set_global_kill_switch em backend/app/services/kill_switch.py, POST /api/v1/accounts/{id}/reset-kill-switch, POST /api/v1/admin/kill-switch e UI do kill switch global em frontend/app/admin/page.tsx.
Implemente:
1. POST /api/v1/accounts/{account_id}/kill (papel admin do workspace, em backend/app/api/v1/endpoints/accounts.py): chama trigger_account_kill_switch com motivo "manual" (para bots, marca a conta killed e fecha posições conforme KILL_SWITCH_CLOSE_POSITIONS; parâmetro opcional close_positions no corpo). Registra auditoria com o usuário.
2. Frontend: botão "Kill switch" vermelho por conta em frontend/app/dashboard/accounts/page.tsx e no dashboard (frontend/app/dashboard/page.tsx), com diálogo de confirmação digitando o nome da conta; banner vermelho no app-shell (frontend/components/app-shell.tsx) quando o kill switch global estiver ativo (adicione um endpoint leve GET /api/v1/system/status acessível a qualquer usuário logado, sem dados sensíveis).
3. Telegram (se a etapa 3 já estiver feita): comando /kill <account_label|all> e /status recebidos por polling (getUpdates) em um processo novo backend/app/workers/telegram_bot.py, aceitando SOMENTE mensagens do TELEGRAM_CHAT_ID configurado; /kill all aciona o global. Novo serviço no docker-compose.yml ("telegram_bot", uma instância, perfil opcional) e override em docker-compose.prod.yml. Se a etapa 3 não existir, pule este item e diga isso no PR.
4. Documente o procedimento de teste controlado em produção em docs/SECURITY.md (runbook): conta paper/testnet, acionar, verificar posições zeradas, resetar.
Testes: kill manual muda status para killed, para bots e fecha posições paper (backend/tests/test_execution.py ou novo test_kill_switch.py); viewer/trader recebem 403; /system/status reflete o global; parser de comandos do Telegram ignora chat_id diferente (sem rede: MockTransport).
Comandos que devem passar: cd backend && ruff check . && ruff format --check . && pytest -q; cd frontend && npm run lint && npx tsc --noEmit && npm run build; se mexer no compose: docker compose config -q.
Branch: feat/kill-switch-manual. Abra PR para main.
```

#### Etapa 5 — Multi-conta / limites agregados por workspace

```text
Leia AGENTS.md. Tarefa: limites de risco agregados entre contas de um mesmo workspace no ISCTRADE.
Contexto: limites atuais são por conta (Account.max_leverage, max_risk_per_trade_pct, max_daily_loss_pct, max_drawdown_pct) aplicados pelo RiskManager (backend/app/services/risk_manager.py) via OrderService (backend/app/services/execution.py). Workspaces em backend/app/models/workspace.py.
Implemente:
1. Campos opcionais em Workspace: max_total_notional_usdt, max_daily_loss_usdt, max_open_positions (todos nullable = sem limite). Migração backend/alembic/versions/0002_... (ou o próximo número livre) com downgrade.
2. Regra nova no RiskManager (ou um WorkspaceRiskGuard chamado pelo OrderService antes do RiskManager): soma exposição (notional das posições abertas) e perda realizada do dia de TODAS as contas do workspace (paper e live separados ou somados — decida, documente e teste); rejeita ordens de abertura que excedam; reduce-only sempre passa. Motivos de rejeição claros (ex.: "workspace_notional_limit").
3. API: PATCH /api/v1/workspaces/{id} aceita os novos campos (papel admin); GET /api/v1/workspaces/{id}/risk retorna uso atual x limites.
4. Frontend: seção "Limites do workspace" em frontend/app/dashboard/workspaces/page.tsx e cartão de uso consolidado (todas as contas) no topo de frontend/app/dashboard/page.tsx.
5. Atualize backend/app/backtest/engine.py somente se necessário (backtest é de conta única; não precisa aplicar).
Testes: duas contas no mesmo workspace; ordem que excede o notional agregado é rejeitada, reduce-only passa, workspace sem limites não muda nada; isolamento: contas de outro workspace não contam; endpoint risk; migração upgrade/downgrade.
Comandos que devem passar: cd backend && ruff check . && ruff format --check . && pytest -q; cd frontend && npm run lint && npx tsc --noEmit && npm run build.
Branch: feat/workspace-risk-limits. Abra PR para main.
```

#### Etapa 6 — Dashboard: PnL, posições e scanner de mercado

```text
Leia AGENTS.md. Tarefa: ampliar o dashboard do ISCTRADE com PnL por período, posições consolidadas e scanner de mercado.
Contexto: dashboard por conta em backend/app/services/dashboard.py (GET /api/v1/accounts/{id}/dashboard), histórico em backend/app/api/v1/endpoints/history.py (orders, trades, equity, positions, audit), frontend em frontend/app/dashboard/page.tsx; cliente Binance em backend/app/services/binance_client.py (não tem endpoint de ticker 24h ainda).
Implemente:
1. GET /api/v1/history/pnl?account_id=&period=day|week|month&group_by=symbol|bot: soma de trades.realized_pnl e fees por período (UTC), filtrado pelo workspace. Agregação em Python ou SQL compatível com SQLite e PostgreSQL.
2. GET /api/v1/history/positions já existe e, sem account_id, retorna as posições abertas de todas as contas do workspace: inclua o label da conta no schema PositionRead (backend/app/schemas/history.py) para a tabela consolidada.
3. Scanner: método get_24h_tickers() em BinanceFuturesClient (GET /fapi/v1/ticker/24hr, público) e get_premium_index() (GET /fapi/v1/premiumIndex, funding). Serviço backend/app/services/scanner.py que combina variação 24h, volume em USDT, funding e preço; filtros (min_volume, top N, ordenar por variação/volume/funding); cache no Redis de 30 s com fallback em memória. Rota GET /api/v1/market/scanner em novo backend/app/api/v1/endpoints/market.py, registrada em router.py, usando Testnet ou mainnet conforme BINANCE_USE_TESTNET (dados públicos, sem chaves).
4. Frontend: nova página frontend/app/dashboard/scanner/page.tsx (tabela ordenável, atualização a cada 30 s), item no menu em frontend/components/app-shell.tsx; no dashboard, gráfico de barras Recharts com PnL diário dos últimos 30 dias e tabela de posições de todas as contas. Tipos em frontend/lib/types.ts, chamadas em frontend/lib/api.ts. Textos em português.
Testes: pnl agrupa corretamente (fábricas em backend/tests/factories.py), isolamento por workspace; scanner com httpx.MockTransport (nunca a Binance real) cobrindo filtros, ordenação e cache; client methods com MockTransport.
Comandos que devem passar: cd backend && ruff check . && ruff format --check . && pytest -q; cd frontend && npm run lint && npx tsc --noEmit && npm run build.
Branch: feat/dashboard-scanner. Abra PR para main.
```

#### Etapa 7 (opcional) — TimescaleDB

```text
Leia AGENTS.md. Tarefa OPCIONAL: adotar TimescaleDB no ISCTRADE para armazenar candles e acelerar equity_snapshots.
Contexto: hoje o banco é postgres:15-alpine (docker-compose.yml, serviço postgres) e não há tabela de candles; o backtest baixa klines via backend/app/backtest/data.py (fetch_candles) ou CSV. EquitySnapshot está em backend/app/models/trading.py (tabela equity_snapshots, PK id UUID, coluna taken_at). Os testes usam SQLite.
Implemente:
1. docker-compose.yml: trocar a imagem do serviço postgres para timescale/timescaledb:latest-pg15 (mesmo volume/variáveis). Documente em README.md que volumes existentes de postgres:15-alpine são compatíveis só se a versão major for a mesma (15) e que é preciso backup antes (scripts/backup_db.sh). CI: em docs/ci/ci.yml ou .github/workflows/ci.yml, usar a mesma imagem no service do job backend.
2. Modelo novo Candle em backend/app/models/ (tabela candles: symbol, interval, open_time timestamptz, open, high, low, close, volume Numeric(28,10), is_testnet), PK composta (symbol, interval, is_testnet, open_time) — compatível com SQLite.
3. Migração 0002 (ou próximo número): cria candles; SOMENTE se dialect == postgresql E a extensão timescaledb estiver disponível (consulte pg_available_extensions): CREATE EXTENSION IF NOT EXISTS timescaledb; SELECT create_hypertable('candles','open_time', if_not_exists => TRUE); para equity_snapshots é necessário que a PK inclua taken_at — altere a PK para (id, taken_at) antes de create_hypertable('equity_snapshots','taken_at', migrate_data => TRUE). Política de compressão opcional para candles > 30 dias. Sem Timescale disponível, a migração apenas cria as tabelas (PostgreSQL puro continua funcionando). downgrade reverte.
4. backend/app/backtest/data.py: fetch_candles passa a ler do banco primeiro e baixar só os intervalos faltantes, gravando com upsert (ON CONFLICT DO NOTHING no PostgreSQL; fallback para SQLite).
5. Task Celery opcional "market.backfill_candles" para símbolos/intervalos dos bots ativos.
Testes: cache de candles (segunda chamada não acessa a rede — MockTransport conta requisições); upsert idempotente; test_migrations.py passa em PostgreSQL puro E (se disponível) em Timescale.
Comandos que devem passar: cd backend && ruff check . && ruff format --check . && pytest -q; cp .env.example .env && docker compose config -q.
Branch: feat/timescaledb. Abra PR para main.
```

#### Etapa 8 — Validação em paper trading (dry-run)

```text
Leia AGENTS.md. Tarefa: ferramentas para validar estratégias em paper trading antes de qualquer capital real no ISCTRADE.
Contexto: contas paper usam PaperExecutor (backend/app/services/execution.py); snapshots de equity em equity_snapshots (task accounts.snapshot_all); trades em trades (is_paper=true); backtest em backend/app/backtest/engine.py com métricas em BacktestResult.
Implemente:
1. backend/app/services/performance.py: métricas a partir de trades/equity reais de uma conta ou bot num intervalo (retorno, drawdown máximo, Sharpe diário, win rate, profit factor, nº de trades, taxas), reutilizando as funções de métricas do engine se possível (refatore para um módulo comum sem mudar resultados — os testes de backtest existentes devem continuar passando).
2. GET /api/v1/history/performance?account_id=&bot_id=&start=&end= e POST /api/v1/bots/{id}/paper-vs-backtest: roda um backtest com a mesma estratégia/params/símbolo/intervalo no mesmo período do paper (dados públicos) e retorna as duas séries de métricas lado a lado + desvios.
3. Critérios de aprovação configuráveis (config + .env.example): PAPER_MIN_DAYS=28, PAPER_MAX_DRAWDOWN_PCT, PAPER_MIN_TRADES, PAPER_MAX_DEVIATION_PCT (desvio máximo de retorno vs backtest). Endpoint retorna "approved": true/false e os motivos.
4. Frontend: aba "Validação paper" em frontend/app/dashboard/bots/page.tsx (ou página nova) mostrando dias em paper, métricas, comparação com backtest e status de aprovação.
5. docs/ROADMAP.md: explique que a etapa só termina após >= 4 semanas reais de paper aprovadas (operacional).
Testes: métricas com trades sintéticos (backend/tests/factories.py); critérios de aprovação (aprovado/reprovado por cada motivo); paper-vs-backtest com market data mockado (httpx.MockTransport ou o hook market_client_factory em backend/app/api/v1/endpoints/strategies.py); nenhuma chamada real à Binance.
Comandos que devem passar: cd backend && ruff check . && ruff format --check . && pytest -q; cd frontend && npm run lint && npx tsc --noEmit && npm run build.
Branch: feat/paper-validation. Abra PR para main.
```

Depois do merge: crie contas **paper** (Testnet), rode os bots por **pelo menos 4 semanas** e só avance quando o
status estiver aprovado.

#### Etapa 9 — Live com capital pequeno

```text
Leia AGENTS.md. Tarefa: salvaguardas para operar conta REAL com capital pequeno no ISCTRADE. NÃO altere defaults: paper e testnet continuam padrão; LIVE_TRADING_ENABLED continua false por padrão.
Contexto: LiveExecutor em backend/app/services/execution.py só é usado com LIVE_TRADING_ENABLED=true e Account.execution_mode=live; verificação de credenciais em POST /api/v1/accounts/{id}/verify; limites por conta no RiskManager.
Implemente:
1. Tetos rígidos de servidor (config + .env.example): LIVE_MAX_NOTIONAL_PER_ORDER_USDT (default 50), LIVE_MAX_TOTAL_NOTIONAL_USDT (default 200), LIVE_MAX_LEVERAGE (default 2), LIVE_MAX_RISK_PER_TRADE_PCT (default 0.5). Aplicados no OrderService SOMENTE para contas live, depois do RiskManager, rejeitando com motivo claro e auditoria. Não podem ser afrouxados pela API/UI, só por env.
2. Checklist de pré-voo GET /api/v1/accounts/{id}/live-preflight: credenciais válidas e can_trade; conta NÃO testnet; paper do mesmo bot aprovado (se a etapa 8 existir); limites da conta <= tetos; kill switch global desligado; modo de posição one-way; alavancagem do símbolo <= teto. Se possível, consulte as restrições da chave na Binance (endpoint de API restrictions da Binance) para confirmar que saque está DESABILITADO — se não for possível com a API de futuros, exiba o item como "verificação manual obrigatória".
3. Mudar uma conta para execution_mode=live (PATCH /api/v1/accounts/{id}) exige papel admin, preflight todo verde e campo de confirmação "confirm_live": "<label da conta>"; registra auditoria critical (o alerta via WhatsApp já sai pelo pipeline de alertas; mapeie o novo evento em alert_from_audit).
4. Frontend: em frontend/app/dashboard/accounts/page.tsx, assistente "Ativar modo real" mostrando o checklist, os tetos e exigindo digitar o nome da conta; badge vermelho "REAL" persistente no app-shell quando a conta ativa for live.
5. docs/SECURITY.md: runbook de go-live (capital mínimo, chave sem saque e com whitelist de IP, primeira semana com 1 bot e 1 símbolo, revisão diária, critérios para aumentar capital).
Testes (sempre com Binance mockada via httpx.MockTransport, sem nenhuma ordem real): tetos rejeitam ordens live acima do limite e não afetam paper; PATCH para live sem confirmação/preflight falha; com tudo ok passa e audita; LIVE_TRADING_ENABLED=false continua forçando paper.
Comandos que devem passar: cd backend && ruff check . && ruff format --check . && pytest -q; cd frontend && npm run lint && npx tsc --noEmit && npm run build.
Branch: feat/live-safeguards. Abra PR para main.
```

Depois do merge: só então, conscientemente, defina `LIVE_TRADING_ENABLED=true` no `.env` do servidor e ative
**uma** conta com capital pequeno.

---

## 5. Regras de segurança

1. **Nunca commite segredos**: tokens do GitHub, chaves da Binance, tokens do WhatsApp (WAGO etc.) e do Telegram, chaves de API de LLM,
   senhas, `.env`. Use variáveis de ambiente / `.env` (ignorado pelo git) e mantenha só placeholders
   (`CHANGE_ME_...`) no `.env.example`. Não cole segredos em prompts de agentes nem em issues/PRs.
2. **Chaves da Binance**: crie chaves dedicadas com **leitura + trading de futuros** apenas, **sem permissão de
   saque**, com **whitelist de IP** do servidor. Use chaves separadas para Testnet e mainnet.
3. **Testnet/paper primeiro**: toda etapa nova roda em paper/Testnet. Ordens reais só com
   `LIVE_TRADING_ENABLED=true` **e** conta `execution_mode=live` — nunca deixe um agente mudar esses defaults.
4. **Teste cada etapa antes do push**: rode o portão de qualidade (seção 1.4). Testes sempre mockam a Binance.
5. **Push/merge na `main` só com testes passando** (CI verde) e após revisão humana do diff. Nada de force-push.
6. **Revise os diffs dos agentes** com o checklist da seção 4.1, com atenção a: segredos, mudanças em
   `risk_manager.py`, `execution.py`, `kill_switch.py`, `config.py`, migrações e permissões de rotas.
7. **Vazou um segredo?** Revogue/rotacione **imediatamente** na origem (Binance, GitHub, WhatsApp/WAGO, Telegram, provedor de LLM),
   depois troque no `.env` e reinicie os serviços. Remover do histórico do git não basta — considere-o comprometido.
   Para `JWT_SECRET`/`MASTER_ENCRYPTION_KEY`, siga `docs/SECURITY.md`.
8. **Limites para capital real**: comece com um valor que você aceita perder por inteiro, alavancagem ≤ 2x,
   risco por trade ≤ 0,5%, perda diária ≤ 2–3%, drawdown máximo com kill switch configurado; aumente capital só
   gradualmente e após semanas de resultados estáveis.
9. **Permissões dos agentes**: dê aos agentes o mínimo necessário (sem acesso ao `.env` de produção, sem chaves
   reais, sem permissão de deploy). O servidor de produção não deve ser operado por agentes.

> [!WARNING]
> Nenhuma automação elimina o risco de mercado. Bugs, falhas de rede, indisponibilidade da corretora, *slippage*
> e eventos extremos podem causar perdas maiores que os limites configurados. Você é o único responsável pelas
> ordens enviadas pela sua instância.
