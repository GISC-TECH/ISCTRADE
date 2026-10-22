# Roadmap

> Avance de fase **somente** quando todos os critérios da fase atual estiverem cumpridos.
> Itens marcados indicam que a **funcionalidade existe no código**; os critérios operacionais (tempo em paper,
> testes em produção) dependem de quem opera a instância.

## Fase 0 — Fundação
- [x] Estrutura do repositório (backend, frontend, docs, infra)
- [x] Autenticação (registro, login com cookie httpOnly, refresh, `/me`, logout de todas as sessões)
- [x] Modelos de usuário, workspace e conta com chaves criptografadas (AES-256-GCM)
- [x] RiskManager determinístico com testes unitários
- [x] Cliente REST assíncrono da Binance USDT-M Futures
- [x] Primeira migração Alembic (testada com upgrade/downgrade/check em PostgreSQL)
- [x] CI (lint + testes + build) — workflow pronto em `docs/ci/ci.yml`
- [ ] Ativar o workflow movendo-o para `.github/workflows/` (requer token/conta com escopo `workflow`)
- [x] Trilha de auditoria com mascaramento de dados sensíveis

## Fase 1 — Backtest
- [x] Download de klines históricos (API pública da Binance) e importação de CSV
- [x] Motor de backtest com taxas e slippage (stops com *gap*)
- [x] Custos de *funding* no backtest (histórico da exchange, arquivo data.binance.vision, CSV ou taxa constante)
- [x] Estratégias sobre a interface `Strategy` (grid, cruzamento de EMAs, RSI + Bollinger)
- [x] Estratégias técnicas com stop ATR: RSI reversão, Bollinger reversão/squeeze, Donchian, EMA + ADX e
  Supertrend (trailing ATR) — [STRATEGIES.md](STRATEGIES.md)
- [x] Scanner de mercado USDT-M (liquidez, tendência, ATR%, momentum, volume, funding, sinais) e seleção
  automática de símbolos nos bots com limite de posições simultâneas
- [x] Notícias (RSS, CryptoPanic, X) com sentimento por LLM/léxico como **filtro** de entradas, calendário
  econômico com pausa por evento e alertas — [NEWS.md](NEWS.md)
- [ ] Arquivo histórico de notícias para backtests com filtro em períodos antigos
- [x] Integrar RiskManager no backtest
- [x] Relatórios: retorno, drawdown máximo, Sharpe, win rate, profit factor, buy & hold
- [x] Testes walk-forward / out-of-sample (`POST /strategies/walk-forward`)
- [ ] Armazenamento local de klines (cache) para backtests repetidos

## Fase 2 — Dry-run / Paper trading
- [x] Execução simulada (paper) como padrão, com taxa/slippage configuráveis
- [x] Execução em tempo real com dados da Testnet da Binance
- [x] WebSocket de mercado e user data stream (reconexão + keepalive do listenKey)
- [x] Máquina de estados do bot integrada às tasks Celery (worker + beat)
- [x] Reconciliador de ordens/posições
- [x] Kill switch por conta (drawdown) e kill switch global (admin)
- [x] Painel em tempo real (WebSocket autenticado + Redis pub/sub): cotações, candles, posições/PnL, ordens, bots e alertas ([REALTIME.md](REALTIME.md))
- [x] Trading manual (MARKET/LIMIT, SL obrigatório, TP, fechar/fechar todas, alavancagem) via RiskManager, com LIMIT/SL/TP simulados no paper
- [ ] Comparar resultados do paper com o backtest por ≥ 4 semanas *(operacional)*

## Fase 3 — Conta real mínima
- [x] Dupla trava para ordens reais (`LIVE_TRADING_ENABLED` + `execution_mode=live` por conta)
- [x] Stop loss de proteção (`STOP_MARKET` reduce-only) enviado junto da ordem real
- [ ] Chaves sem permissão de saque e com whitelist de IP *(operacional — ver SECURITY.md)*
- [ ] Capital mínimo, alavancagem ≤ 2x, risco por trade ≤ 0,5% *(configuração por conta)*
- [x] Confirmação explícita na UI para ordens manuais (destacando conta REAL)
- [ ] Kill switch testado em produção (simulação controlada)
- [ ] Runbook de incidentes completo (rascunho em SECURITY.md)

## Fase 4 — Monitoramento e logs ✅ (etapa concluída no código)
- [x] Trilha de auditoria por workspace e global
- [x] Dashboards de equity (snapshots periódicos)
- [x] Health checks em todos os serviços Docker
- [x] Script de backup do PostgreSQL com retenção (e backup automático antes de cada deploy)
- [x] Logs estruturados (JSON) com `request_id`/`task_id` e mascaramento de segredos
- [x] `/health` (liveness) e `/health/ready` (PostgreSQL, Redis, broker Celery e heartbeat dos streams)
- [x] Métricas Prometheus (`/metrics` interno/token) + Prometheus/Grafana opcionais (perfil `monitoring`)
- [x] Alertas via **WhatsApp** (WAGO por padrão; wago-api, Meta Cloud API, Evolution e HTTP genérico) para
  kill switch, limites de risco, ordens rejeitadas/executadas, posições, erros de bot, WebSocket, divergências,
  falhas de tarefas e resumo diário — com histórico, antispam e configuração por workspace ([ALERTS.md](ALERTS.md))
- [x] Tela **Monitoramento** (saúde, alertas recentes, configurações e alerta de teste)
- [ ] Canais adicionais (Telegram/e-mail) sobre a mesma interface `Notifier`
- [ ] Trilha de auditoria imutável (assinada/WORM)
- [ ] Regras de alerta no Prometheus/Alertmanager (hoje os alertas saem do próprio backend)
- [ ] Teste periódico de restauração de backup *(operacional)*

## Fase 5 — Escala de capital
- [x] Múltiplas contas/workspaces com limites de risco por conta
- [ ] Limites de risco agregados entre contas
- [ ] Critérios objetivos para aumento de capital (ex.: 3 meses com drawdown dentro do limite)
- [ ] Aumentos graduais (ex.: +25% por etapa) com revisão de risco
- [ ] Revisão de segurança externa
- [x] 2FA (TOTP) para login (obrigatório para super-admin e operação live; política opcional para todos)
