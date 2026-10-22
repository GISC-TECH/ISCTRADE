# Análise de lacunas (gap analysis) — ISCTRADE

Data: 2026-10-22 · Commit auditado: `44cff9f` (`main`) · Escopo: tudo o que falta ou ainda não está pronto
para operar **com dinheiro real**. Complementa o [`docs/AUDIT.md`](AUDIT.md) (segurança/execução, já corrigidos)
e o [`docs/ROADMAP.md`](ROADMAP.md). Auditoria feita em clone limpo, sem alterar código.

> Não é parecer jurídico nem recomendação de investimento. Futuros perpétuos alavancados podem causar perda total.

## 1. Resumo executivo

**Veredito:** o ISCTRADE está **pronto para paper/testnet** e roda de ponta a ponta em Docker, mas **ainda não está
pronto para capital real**. A base técnica é boa (377 testes passando, cobertura de ~90%, ruff limpo, dupla trava
para ordens reais, stop obrigatório, migrações íntegras, backup/restauração funcional). Os bloqueios para dinheiro
real estão em **controles operacionais e de risco no nível da conta/instância**, não no código de ordem em si:

1. **Não há garantia contínua de que toda posição live tem stop na corretora** — o reconciliador sincroniza
   posições/ordens, mas não verifica nem recoloca o stop de proteção, nem alerta "posição sem stop".
2. **Cadastro aberto por padrão em produção** (`ALLOW_REGISTRATION=true`): quem acessar o domínio antes do dono vira
   super-admin; depois disso qualquer pessoa ainda cria conta e workspace próprio (verificado: 2º cadastro → 201).
3. **Sem 2FA** e **sem limites agregados / teto rígido de capital** por workspace ou instância.
4. **CI inativo**: nada impede um commit quebrado de chegar em `main` (e no `deploy.sh`, que faz `git pull`).
5. **Observabilidade incompleta para operação desassistida**: `/health/ready` não enxerga o beat nem a conexão real
   dos WebSockets, não há regras de alerta no Prometheus nem *dead man's switch*, e o container `streams` fica
   permanentemente `unhealthy` (bug confirmado).
6. **Backups só locais**, sem agendamento, cópia externa, criptografia ou teste periódico de restauração.
7. **Kill switch por drawdown é distorcido por depósitos/saques** (pico de patrimônio não considera transferências).

Funcionalidades prometidas ainda ausentes: trilha de auditoria imutável, Telegram/e-mail, TOTP, PnL por período,
posições consolidadas na UI, funding e walk-forward no backtest, estratégia de funding, parciais no paper, WebSocket
da Bybit, escolha de exchange no formulário, modo spot, hedge mode.

Estimativa para chegar a "conta real mínima" com segurança (Fases A+B abaixo): **~3–4 semanas de 1 dev**.

### 1.1 O que foi verificado de fato (e o que não foi)

| Verificação | Resultado |
|---|---|
| `docker compose build` (backend + frontend) | **OK** (Docker 26.1.5 + Compose 2.26.1 instalados no box) |
| `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` (todos os serviços, incl. `celery_worker_trading`) | **OK** — todos `healthy`, **exceto `streams`: `unhealthy`** (ver G-10) |
| Migrações de banco vazio (`0001 → 0008`), `alembic check`, `downgrade base` + `upgrade head` | **OK**, sem drift de modelos |
| `scripts/deploy.sh --no-pull` | **OK** (concluiu; smoke test falhou só porque a porta 443 foi remapeada nesta auditoria) |
| `scripts/backup_db.sh` + restauração em banco novo | **OK** (usuários e `alembic_version=0008` restaurados) |
| Caddy: HTTPS (cert interno), HSTS/CSP/XFO/Permissions-Policy, `/metrics` 404, `/docs` 404 em prod, HTTP→HTTPS 308 | **OK** |
| Primeiro cadastro vira super-admin; 2º cadastro aceito com `ALLOW_REGISTRATION=true` em produção | **Confirmado** (G-03b) |
| WebSocket `/api/v1/ws` via Caddy | **OK** quando a origem está em `CORS_ORIGINS`; **403** caso contrário (G-23) |
| `pytest --cov` | **377 passed, 1 skipped**; cobertura **87%** (padrão) / **90%** (com `concurrency=greenlet,thread`, medição correta para SQLAlchemy async) |
| `ruff check` / `ruff format --check` | limpos |
| `mypy app --ignore-missing-imports` | ~50 erros (maioria tipagem de `**kwargs`; sem bug real encontrado) |
| `npm ci`, `npm run lint`, `tsc --noEmit`, `next build` | OK (lint: 0 erros, 12 avisos `react-hooks/set-state-in-effect`) |
| `pip-audit -r backend/requirements.txt` | **1 vulnerabilidade: `orjson 3.11.5` (PYSEC-2026-107, corrigido em 3.11.6)** |
| `npm audit` | 9 (7 high, 2 moderate), todas na cadeia build-time do Tailwind 3/eslint-config-next (braces, micromatch, chokidar, postcss-selector-parser) |
| `gitleaks git` (67 commits) | 18 achados, **todos falsos positivos** (`placeholder-key` em testes e `key=` de presets) |
| Frontend (stack local, Chrome headless, desktop 1366px e mobile 390px) | 16 páginas renderizam (200), sem erros de console, sem overflow horizontal, `lang=pt-BR` |
| **Não verificado** | Containers do box não têm DNS/saída para a internet: Binance, notícias, calendário e WhatsApp **a partir do Docker** não foram exercitados; recuperação após reinício com bots/posições reais foi avaliada **por leitura de código**, não por teste. Binance mainnet REST é geobloqueada (451) aqui. O box exigiu `net.bridge.bridge-nf-call-iptables=0` (regras iptables legadas do sandbox) para os containers se comunicarem — não é problema do projeto. |

## 2. Tabela de lacunas

Legenda — **Prioridade**: P0 = bloqueia dinheiro real · P1 = necessário antes de escalar capital/abrir a terceiros ·
P2 = importante para operação estável · P3 = melhoria. **Status**: Faltando · Parcial · Bug · Desatualizado.
**Esforço**: P (≤1 dia) · M (2–5 dias) · G (1–2 semanas).

### 2.1 Segurança de trading e risco

| ID | Item | Área | Prio | Status | Esforço | Evidência / arquivo |
|---|---|---|---|---|---|---|
| G-01 | Garantia contínua de stop na corretora: o reconciliador não verifica se cada posição live tem `STOP_MARKET` (Algo Order) ativo, não recoloca stop cancelado/expirado e adota posições abertas fora da plataforma sem stop; sem alerta "posição desprotegida" | Risco/Reconciliação | **P0** | Faltando | M | `services/reconciler.py` (`run`, `ACCOUNT_UPDATE` só ajusta qty/preço) |
| G-02 | Limites agregados por workspace/instância (notional total, perda diária em USDT, nº máx. de posições) e **teto rígido de capital/notional para contas live** | Risco | **P0** | Faltando | M | `services/risk_manager.py` (só por conta); `docs/AGENT_GUIDE.md` Etapas 5 e 9; `ROADMAP` Fase 5 |
| G-03 | Kill switch de drawdown e perda diária distorcidos por depósitos/saques: `peak_equity`/`day_start_equity` não descontam transferências (saque dispara kill switch; depósito mascara perda) | Risco | P1 | Faltando | M | `RiskManager.from_account`; nenhum tratamento de `TRANSFER` em `services/` |
| G-04 | Stop vs. preço de liquidação não é validado; `max_leverage` aceita até 125x por conta | Risco | P1 | Faltando | P | `schemas/account.py` (`le=125`); nenhum uso de `liquidationPrice` no RiskManager |
| G-05 | Risco por trade ignora taxas e slippage/gap do stop a mercado; sem limite de concentração por símbolo/correlação nem limite de ordens por minuto | Risco | P2 | Faltando | M | `RiskManager.trade_risk` |
| G-06 | Alertas de posição ao vivo: só "aberta/fechada"; faltam PnL/limiar, proximidade do stop/liquidação, posição sem stop, divergência de preço | Alertas | P1 | Parcial | M | `services/alerts/events.py` |
| G-07 | Kill switch nunca testado em produção; runbook de go-live e de incidente incompleto (rascunho em SECURITY.md) | Operação | P1 | Faltando | P | `ROADMAP` Fase 3; `docs/SECURITY.md` §runbook |
| G-08 | Sincronização de relógio do host (NTP/chrony) não documentada nem monitorada (há só retry em `-1021`) | Operação | P2 | Faltando | P | `services/binance_client.py`; docs sem menção a NTP |
| G-09 | Lock distribuído de tick é *fail-open* quando o Redis cai (pode haver ticks concorrentes) | Execução | P3 | Aceito/Parcial | P | `services/jobs.py:redis_lock` |
| G-10a | Paper otimista: sem preenchimento parcial/profundidade; LIMIT preenche ao tocar o preço | Paper | P2 | Faltando | M | `services/paper_orders.py` |
| G-10b | Hedge mode não suportado (recusado com aviso) | Execução | P3 | Faltando (por decisão) | G | `AUDIT.md` §4 item 3 |

### 2.2 Infra, deploy e operação

| ID | Item | Área | Prio | Status | Esforço | Evidência / arquivo |
|---|---|---|---|---|---|---|
| G-10 | **Bug:** serviço `streams` herda o `HEALTHCHECK` do Dockerfile (HTTP :8000/health), que ele não serve → fica sempre `unhealthy` | Docker | P2 | **Bug (verificado)** | P | `backend/Dockerfile`, `docker-compose.yml` (`streams` sem `healthcheck`) |
| G-11 | `/health/ready` não verifica beat, worker `trading` nem a conexão real dos WebSockets (retornou `streams ok` com os WS falhando por DNS); sem *dead man's switch*/monitor externo | Observabilidade | P1 | Parcial | M | `services/health.py`; logs do `streams` |
| G-12 | Sem regras no Prometheus/Alertmanager (`rule_files` ausente); alertas dependem do próprio backend estar vivo | Observabilidade | P1 | Faltando | M | `infra/monitoring/prometheus.yml`; `ROADMAP` Fase 4 |
| G-13 | Backups: só locais, sem agendamento (cron/systemd timer), sem cópia off-site, sem criptografia, sem script de restauração nem teste periódico; o comando documentado restaura sobre o banco existente | Backup | P1 | Parcial | M | `scripts/backup_db.sh` |
| G-14 | Sem rotação de logs do Docker (`json-file` sem `max-size`) nem limites de CPU/memória → risco de disco cheio | Docker | P2 | Faltando | P | `docker-compose*.yml` (sem `logging:`/`deploy.resources`) |
| G-15 | Sem retenção para `refresh_tokens` (cresce a cada refresh), `audit_logs`, `alerts`, `equity_snapshots`, ordens/trades | Banco | P2 | Faltando | P | só scanner e notícias têm *prune* |
| G-16 | `deploy.sh`: smoke test fixo na 443, não espera `streams`/`celery_beat`, rollback não restaura banco (apenas avisa), exige `docker` sem sudo | Deploy | P3 | Parcial | P | `scripts/deploy.sh` |
| G-17 | Bootstrap do admin depende de ser o primeiro a se cadastrar (corrida entre cadastros simultâneos; contagem sem lock); sem CLI `create-admin` | Bootstrap | P1 | Parcial | P | `api/v1/endpoints/auth.py:register` |
| G-18 | Nomes fixos de redes/volumes (`btp_*`) impedem staging e produção no mesmo host | Docker | P3 | Faltando | P | `docker-compose.yml` |
| G-19 | Grafana com fallback de senha `admin`; `GRAFANA_ADMIN_USER` ausente do `.env.example` | Config | P3 | Parcial | P | `docker-compose.yml` |
| G-20 | Variáveis em `config.py` ausentes do `.env.example`: `NEWS_HTTP_TIMEOUT_SECONDS`, `NEWS_MAX_ITEMS_PER_SOURCE`, `X_API_URL` (demais completas) | Config | P3 | Parcial | P | `core/config.py` × `.env.example` |
| G-21 | Execução de ponta a ponta com Binance/WhatsApp/notícias a partir do Docker e recuperação pós-reinício com bots/posições não testadas automaticamente (sem teste de integração com Postgres+Redis+Celery reais) | Testes/Ops | P2 | Faltando | M | `tests/` (Binance sempre mockada, SQLite) |

### 2.3 Segurança e cadeia de suprimentos

| ID | Item | Área | Prio | Status | Esforço | Evidência / arquivo |
|---|---|---|---|---|---|---|
| G-03b | `ALLOW_REGISTRATION=true` aceito em produção: qualquer visitante cria usuário + workspace (e, sendo admin do próprio workspace, pode pôr conta em `live` se `LIVE_TRADING_ENABLED=true`) | Auth | **P0** | **Confirmado** | P | `core/config.py:production_problems`, `.env.example` |
| G-22 | Sem 2FA (TOTP) no login | Auth | **P0** | Faltando | M | `ROADMAP` Fase 5; `SECURITY.md` |
| G-23 | `CORS_ORIGINS` precisa conter `https://<DOMAIN>`, senão o WebSocket é recusado (403) e o painel fica em "Reconectando…"; `.env.example` traz só localhost e o `deploy.sh` não valida | Config/Realtime | P2 | Parcial | P | `realtime_ws.py` (checagem de Origin); verificado |
| G-24 | `orjson 3.11.5` com PYSEC-2026-107 (o AUDIT.md diz "pip-audit limpo") | Dependências | P2 | Faltando | P | `backend/requirements.txt` |
| G-25 | `npm audit`: 7 high + 2 moderate na cadeia build-time (Tailwind 3, eslint-config-next); migrar para Tailwind 4 | Dependências | P3 | Pendente | M | `frontend/package.json` |
| G-26 | Trilha de auditoria imutável (hash encadeado/assinatura/WORM) | Auditoria | P2 | ✅ **Resolvido** — cadeia HMAC-SHA256 selada pela tarefa `audit.seal`, trigger anti-UPDATE no PostgreSQL, sem FKs em cascata, `GET /admin/audit/verify` + `python -m app.audit_cli verify`, verificação diária (migração `0009`; `SECURITY.md` §Trilha). WORM externo continua opcional (âncora do `head`) | M | `services/audit_chain.py` |
| G-27 | Redis sem senha; métricas 9101/9102 sem autenticação (rede interna) | Infra | P2 | Aceito/Parcial | P | `docker-compose.yml` |
| G-28 | Reset de senha, lista/revogação de sessões por dispositivo | Auth | P2 | Faltando | M | rotas `/auth/*` |
| G-29 | Sem rotação da `MASTER_ENCRYPTION_KEY` (re-cifrar chaves) | Cripto | P2 | Faltando | M | `SECURITY.md` §limitações |
| G-30 | Supply chain: dependências transitivas Python não pinadas (sem lock/hashes), imagens base sem digest (`caddy:2-alpine`, `postgres:15-alpine`), sem Dependabot/Renovate, sem SBOM, sem `pip-audit`/`npm audit`/`gitleaks` no CI, sem allowlist do gitleaks | Supply chain | P2 | Faltando | P–M | `requirements.txt`, Dockerfiles, `docs/ci/ci.yml` |
| G-31 | `uvicorn --forwarded-allow-ips '*'` em produção: seguro só porque o backend não publica portas e o Caddy sobrescreve `X-Forwarded-For`; restringir à rede do Caddy | Infra | P3 | Aceito | P | `docker-compose.prod.yml` |

### 2.4 Funcionalidades prometidas (README / ROADMAP / AGENT_GUIDE)

| ID | Funcionalidade | Prio | Status | Esforço | Evidência |
|---|---|---|---|---|---|
| F-01 | CI (lint + testes + build) | **P0** | **Parcial** — workflow pronto, **inativo** (`.github/workflows/` não existe) | P | `docs/ci/ci.yml`; `ROADMAP` Fase 0 |
| F-02 | Auditoria à prova de adulteração (hash chain) | P2 | ✅ Resolvido | M | ver G-26 |
| F-03 | Limites de risco agregados por workspace | P0 | Faltando | M | ver G-02 |
| F-04 | PnL por período (`/history/pnl`, gráfico 30 dias) | P2 | Faltando | M | `history.py` não tem a rota; `AGENT_GUIDE` Etapa 6 |
| F-05 | Posições consolidadas | P2 | Parcial — `GET /history/positions` sem `account_id` já agrega o workspace, mas sem rótulo da conta e sem tabela na UI | P | `history.py:list_positions` |
| F-06 | Alertas no Telegram / e-mail | P2 | ✅ Resolvido — canais `telegram` (Bot API) e `email` (SMTP STARTTLS/SSL) no mesmo pipeline, destinatários por workspace (`telegram_chat_ids`, `emails`, migração `0011`) com padrão do servidor; `docs/ALERTS.md` | M | `services/alerts/telegram.py`, `services/alerts/email.py` |
| F-07 | TOTP 2FA | P0 | Faltando | M | ver G-22 |
| F-08 | Funding no backtest | P2 | ✅ Resolvido — `funding.mode` auto/exchange/archive/csv/constant/off; `data_source: archive` (data.binance.vision) | M | `backtest/funding.py`, `backtest/archive.py`; `STRATEGIES.md` §Funding |
| F-09 | Walk-forward / out-of-sample | P2 | ✅ Resolvido — `POST /strategies/walk-forward` (grid por fold, IS × OOS, curva OOS encadeada, eficiência, estabilidade) | M | `backtest/walkforward.py`; `STRATEGIES.md` §Walk-forward |
| F-10 | Cache local de klines (TimescaleDB opcional) | P3 | Faltando | M | `backtest/data.py`; `AGENT_GUIDE` Etapa 7 |
| F-11 | Arquivo histórico de notícias para backtest com filtro | P3 | Faltando | M | `ROADMAP` Fase 1 |
| F-12 | Alertas de posição ao vivo (além de aberta/fechada) | P1 | Parcial | M | ver G-06 |
| F-13 | Paper com preenchimento parcial | P2 | Faltando | M | ver G-10a |
| F-14 | Estratégia de funding (contrarian) | P3 | Faltando (documentado como não implementado) | M | `STRATEGIES.md` linha 113 |
| F-15 | WebSocket da Bybit | P3 | Faltando (adaptador só REST, experimental) | G | `exchanges/bybit.py:616` |
| F-16 | Escolha de exchange no formulário de contas | P3 | Faltando no frontend (API tem `GET /accounts/exchanges` e campo `exchange`) | P | `frontend/app/dashboard/accounts/page.tsx` |
| F-17 | Modo spot | P3 | Faltando (só USDT-M) | G | nenhuma referência a spot |
| F-18 | Critérios objetivos de aumento de capital / aumentos graduais | P1 | Faltando (operacional + suporte no app) | M | `ROADMAP` Fase 5 |
| F-19 | Revisão de segurança externa | P1 | Faltando | — | `ROADMAP` Fase 5 |
| F-20 | Comparação paper × backtest ≥ 4 semanas | P1 | Faltando (operacional; sem relatório no app) | M | `ROADMAP` Fase 2 |
| ✅ | Auth/JWT/refresh de uso único, RBAC, workspaces, contas cifradas, ~25 estratégias + 12 presets, scanner, notícias/sentimento, calendário, painel em tempo real + trading manual, backtest, kill switch por conta e global, Prometheus/Grafana, WhatsApp, Monitoramento | — | Implementado | — | verificado via API/UI |

### 2.5 Frontend

| ID | Item | Prio | Status | Esforço | Evidência |
|---|---|---|---|---|---|
| U-01 | Sem `not-found.tsx`/`error.tsx`/`loading.tsx`: rotas inexistentes mostram a página padrão do Next em inglês ("This page could not be found"); erros de render caem no "Application error" genérico | P3 | Faltando (verificado) | P | `frontend/app/` |
| U-02 | Mobile: menu lateral vira bloco de 13 links (~400 px) no topo de toda página, sem menu recolhível | P3 | Parcial (verificado em 390 px) | P | `components/app-shell.tsx` |
| U-03 | Formatação: painel de ordem mostra "limite 1.0000000000%" (Decimal cru) | P3 | Bug (verificado) | P | `components/trading-panel.tsx:247` |
| U-04 | Sem página de Configurações (2FA, sessões, preferências) e sem gestão de presets (presets fixos no código) | P2 | Faltando | M | `strategy/presets.py`; rotas do frontend |
| U-05 | Config. de alertas existe (dentro de Monitoramento), mas não há página dedicada de histórico/filtros de alertas | P3 | Parcial | P | `app/dashboard/monitoring/page.tsx` |
| U-06 | Sem testes de frontend (unitários ou e2e/Playwright) e sem `typecheck` em `package.json` | P2 | Faltando | M | `frontend/package.json` |
| U-07 | ESLint: 12 avisos `react-hooks/set-state-in-effect` | P3 | Pendente | P | `npm run lint` |
| U-08 | Acessibilidade: login/cadastro sem `<h1>`; 1 campo sem rótulo em Visão geral, Tempo real e Admin; sem *skip link* | P3 | Parcial | P | crawl headless |
| U-09 | Sem Caddy (acesso direto em :3000, sugerido no README) o tempo real fica em "Conectando…" para sempre (rewrite do Next não encaminha WebSocket; exige `NEXT_PUBLIC_WS_URL`) | P3 | Documentado só em REALTIME.md | P | `lib/realtime.ts`, `next.config.mjs` |
| U-10 | Cadastro não faz login automático (volta para a tela de login) | P3 | UX | P | `POST /auth/register` não emite cookies |
| ✅ | i18n: interface em português em todas as páginas verificadas; `lang=pt-BR`; links do menu sem quebras; estados vazios presentes ("Sem posições abertas", "Nenhuma ordem") | — | OK | — | crawl headless |

### 2.6 Backend (qualidade)

| ID | Item | Prio | Status | Esforço | Evidência |
|---|---|---|---|---|---|
| B-01 | Módulos críticos com baixa cobertura: `workers/streams.py` 43%, `services/prices.py` 30%, `services/dispatch.py` 53%, tasks Celery 59–75%, `services/jobs.py` 70%, `api/.../history.py` 70%, `manual_trading.py` 80% | P2 | Parcial | M | `pytest --cov` |
| B-02 | Cobertura medida sem `concurrency=greenlet` subestima endpoints (87% vs 90%); sem `.coveragerc` nem limiar no CI | P3 | Faltando | P | `backend/pytest.ini` |
| B-03 | `mypy` não faz parte do CI (~50 erros, maioria `**kwargs` em `log_event` e LSP em `cancel_order/get_order`) | P3 | Faltando | M | `mypy app` |
| B-04 | `NotImplementedError` restantes são abstrações legítimas (`technical.py:100`, `binance_ws.py:123`, `whatsapp.py:60`); nenhum TODO/FIXME | — | OK | — | grep |
| B-05 | Avisos de depreciação: `HTTP_422_UNPROCESSABLE_ENTITY`, `starlette.testclient` com httpx | P3 | Pendente | P | saída do pytest |

### 2.7 Documentação e jurídico

| ID | Item | Prio | Status | Esforço | Evidência |
|---|---|---|---|---|---|
| D-01 | Nota regulatória BCB 519/520/521 + CVM existe em `EXCHANGES.md`, mas **não** no aviso de risco do README nem na UI (ex.: ao ativar conta live/mainnet) | P1 | Parcial | P | `README.md`, `docs/EXCHANGES.md` §2 |
| D-02 | Sem nota tributária (declaração mensal à Receita — IN RFB 1888/2019 e normas sucessoras (DeCripto) — confirmar a vigente; tributação de aplicações no exterior — Lei 14.754/2023; guarda de relatórios); sem exportação CSV de trades/PnL para contabilidade | P1 | Faltando | M | — |
| D-03 | Runbooks: go-live, incidente, restauração de backup, rotação de chaves, perda do servidor | P1 | Parcial | M | `SECURITY.md` |
| D-04 | Docs desatualizadas: "Next.js 14" em README/AGENTS.md/ARCHITECTURE/AGENT_GUIDE (é 16.4.2/React 19); `AGENTS.md` diz head `0007` (é `0008`); README não lista `celery_worker_trading` | P3 | Desatualizado | P | arquivos citados |
| D-05 | `AUDIT.md` afirma pip-audit limpo — não é mais verdade (G-24) | P3 | Desatualizado | P | `docs/AUDIT.md` item 8 |
| D-06 | Sem termos de uso / política de privacidade (LGPD) caso a instância seja aberta a terceiros | P2 | Faltando | P | — |
| D-07 | LICENSE MIT presente; aviso de risco no README presente e adequado | — | OK | — | `LICENSE`, `README.md` |

## 3. Plano sugerido em fases

### Fase A — Bloqueios para dinheiro real (≈ 1,5–2 semanas)
1. **Ativar o CI** (F-01) e acrescentar `pip-audit`, `npm audit --omit=dev`, `gitleaks` e limiar de cobertura (G-30, B-02).
2. **Fechar cadastro e bootstrap** (G-03b, G-17): `ALLOW_REGISTRATION=false` por padrão; em produção bloquear o start se
   estiver `true` sem `ADMIN_BOOTSTRAP_TOKEN`; CLI `python -m app.cli create-admin`.
3. **TOTP 2FA** obrigatório para super-admin e para quem altera contas live (G-22).
4. **Guarda de stop contínua** (G-01): no reconciliador, para cada posição live verificar Algo Orders abertas; se faltar
   stop, recolocar (reduce-only) ou fechar, gerar `position.unprotected` (crítico) e alerta.
5. **Limites agregados + teto live** (G-02): `max_total_notional_usdt`, `max_daily_loss_usdt`, `max_open_positions`
   por workspace e `LIVE_MAX_NOTIONAL_USDT` global no servidor.
6. **Liquidação e alavancagem** (G-04): limite global `LIVE_MAX_LEVERAGE` (ex.: 5) e rejeitar stop além do preço de liquidação.
7. Atualizar `orjson` (G-24) e corrigir o healthcheck do `streams` (G-10).

### Fase B — Operação desassistida (≈ 1–1,5 semana)
1. `/health/ready` com heartbeat do beat, do worker `trading` (último tick) e estado real dos WS (G-11); monitor externo
   (ex.: Healthchecks/Uptime Kuma) como *dead man's switch*.
2. Regras Prometheus + Alertmanager (G-12): heartbeat velho, tick atrasado, peso Binance alto, divergência do reconciliador.
3. Backups agendados, criptografados e off-site (`restic`/`rclone`), script `restore_db.sh` e teste mensal (G-13).
4. Rotação de logs e limites de recursos no Compose (G-14); retenção de `refresh_tokens`/auditoria/alertas (G-15).
5. Ajuste de patrimônio por transferências no kill switch (G-03) e alertas de posição ao vivo (G-06).
6. Runbooks completos e **teste controlado do kill switch** em testnet e depois em live com capital mínimo (G-07, D-03).
7. Documentar NTP/chrony e validar `CORS_ORIGINS`/`DOMAIN` no `deploy.sh` (G-08, G-23).

### Fase C — Confiabilidade e validação de estratégia (≈ 2 semanas)
1. Funding e walk-forward no backtest (F-08, F-09); relatório paper × backtest (F-20).
2. Paper com parciais/profundidade (F-13); riscos com taxas e slippage do stop (G-05).
3. Testes de integração com Postgres+Redis+Celery reais e testnet (G-21); cobrir `streams.py`, `prices.py`, tasks (B-01).
4. Auditoria com hash encadeado (G-26); Telegram/e-mail (F-06); reset de senha e sessões (G-28).
5. Nota regulatória e tributária no README/UI e exportação CSV (D-01, D-02).

### Fase D — Produto e escala (contínuo)
PnL por período e posições consolidadas (F-04, F-05), página de configurações e gestão de presets (U-04),
404/erro em português, menu mobile, testes de frontend (U-01, U-02, U-06), Tailwind 4 (G-25), rotação da chave mestra
(G-29), supply chain com lockfile e digests (G-30), escolha de exchange na UI (F-16), Bybit WS, spot e hedge mode
(F-15, F-17, G-10b), revisão de segurança externa (F-19) e critérios objetivos de aumento de capital (F-18).

## 4. Como reproduzir esta auditoria

```bash
git clone https://github.com/GISC-TECH/ISCTRADE && cd ISCTRADE
cp .env.example .env   # gere JWT_SECRET, MASTER_ENCRYPTION_KEY, POSTGRES_PASSWORD
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d && docker compose ps   # streams fica "unhealthy" (G-10)
docker compose run --rm migrate sh -c 'alembic check && alembic downgrade base && alembic upgrade head'
./scripts/backup_db.sh
cd backend && pip install -r requirements-dev.txt pytest-cov pip-audit mypy
printf '[run]\nconcurrency = greenlet,thread\nsource = app\n' > /tmp/covrc
pytest --cov=app --cov-config=/tmp/covrc -q && ruff check . && pip-audit -r requirements.txt
cd ../frontend && npm ci && npm run lint && npx tsc --noEmit && npm audit --omit=dev
gitleaks git --redact .
```
