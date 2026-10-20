# Roadmap

> Avance de fase **somente** quando todos os critérios da fase atual estiverem cumpridos.

## Fase 0 — Fundação (scaffold)
- [x] Estrutura do repositório (backend, frontend, docs, infra)
- [x] Autenticação (registro, login com cookie httpOnly, `/me`)
- [x] Modelos de usuário, workspace e conta com chaves criptografadas (AES-256-GCM)
- [x] RiskManager determinístico com testes unitários
- [x] Cliente REST assíncrono da Binance USDT-M Futures
- [ ] Primeira migração Alembic
- [ ] CI (lint + testes)

## Fase 1 — Backtest
- [ ] Download e armazenamento de klines históricos
- [ ] Motor de backtest com taxas, funding e slippage
- [ ] Implementar primeira estratégia (ex.: grid) sobre a interface `Strategy`
- [ ] Integrar RiskManager no backtest
- [ ] Relatórios: retorno, drawdown máximo, Sharpe, win rate, profit factor
- [ ] Testes walk-forward / out-of-sample

## Fase 2 — Dry-run / Paper trading
- [ ] Execução em tempo real na Testnet da Binance
- [ ] WebSocket de mercado e user data stream
- [ ] Máquina de estados do bot integrada às tasks Celery
- [ ] Reconciliador de ordens/posições
- [ ] Comparar resultados do paper com o backtest por ≥ 4 semanas

## Fase 3 — Conta real mínima
- [ ] Chaves sem permissão de saque e com whitelist de IP
- [ ] Capital mínimo, alavancagem ≤ 2x, risco por trade ≤ 0,5%
- [ ] Kill switch testado em produção (simulação controlada)
- [ ] Runbook de incidentes (como pausar, fechar posições, revogar chaves)

## Fase 4 — Monitoramento e logs
- [ ] Logs estruturados e trilha de auditoria imutável
- [ ] Alertas (Telegram/e-mail) para erros, kill switch e divergências
- [ ] Métricas (Prometheus/Grafana) e dashboards de equity
- [ ] Backups automáticos e testados do PostgreSQL

## Fase 5 — Escala de capital
- [ ] Critérios objetivos para aumento de capital (ex.: 3 meses com drawdown dentro do limite)
- [ ] Aumentos graduais (ex.: +25% por etapa) com revisão de risco
- [ ] Múltiplas contas/workspaces e limites de risco agregados
- [ ] Revisão de segurança externa
