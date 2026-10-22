# Monitoramento (perfil `monitoring`)

```bash
# .env: COMPOSE_PROFILES=monitoring (ou monitoring,backup), GRAFANA_ADMIN_PASSWORD, ALERTMANAGER_WEBHOOK_TOKEN
./scripts/deploy.sh          # sobe Prometheus, Alertmanager, node-exporter, redis_exporter e Grafana (portas só em 127.0.0.1)
ssh -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 -L 3001:127.0.0.1:3001 usuario@servidor
```

| Arquivo | Conteúdo |
|---|---|
| `prometheus.yml` | alvos: backend `/metrics`, workers Celery (9101), streams (9102), node-exporter, redis_exporter (9121, autentica com `REDIS_PASSWORD`), Alertmanager |
| `rules/isctrade.yml` | regras de alerta (serviço fora, beat/worker/streams parados, dados de mercado parados, bots sem tick, peso/ban Binance, kill switch, posição sem stop, divergência do reconciliador, falha de entrega, disco/memória/NTP, Watchdog) |
| `tests/rules_test.yml` | testes unitários das regras (`promtool test rules`) |
| `alertmanager.yml` | roteamento: tudo → backend (WhatsApp); críticos → backend + webhook externo; `Watchdog` → dead man's switch |
| `secrets/` | **ignorado pelo git**; lido pelo Alertmanager |

## Segredos do Alertmanager (`infra/monitoring/secrets/`)

| Arquivo | Valor | Obrigatório |
|---|---|---|
| `webhook_token` | igual a `ALERTMANAGER_WEBHOOK_TOKEN` do `.env` — o `deploy.sh` grava automaticamente | sim (para WhatsApp) |
| `external_url` | URL que recebe o JSON do Alertmanager (ntfy `https://ntfy.sh/<tópico>`, n8n/Make, gateway WhatsApp, etc.) — usado para alertas **críticos**, funciona mesmo com o backend fora do ar | recomendado |
| `deadman_url` | URL de ping do healthchecks.io (check "isctrade-monitoring", período 1 min) — recebe o `Watchdog` a cada minuto | recomendado |

```bash
printf '%s' 'https://hc-ping.com/<uuid>' > infra/monitoring/secrets/deadman_url
printf '%s' 'https://ntfy.sh/<topico-secreto>' > infra/monitoring/secrets/external_url
chmod 711 infra/monitoring/secrets && chmod 644 infra/monitoring/secrets/*   # Alertmanager roda como "nobody"
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart alertmanager
```

Sem `external_url`/`deadman_url` o Alertmanager apenas registra erro de envio para esses destinos; o caminho
backend → WhatsApp continua funcionando.

## Fluxo dos alertas

```
Prometheus (regras) ──► Alertmanager ──► backend POST /api/v1/monitoring/alertmanager (Bearer) ──► WhatsApp
                                     ├──► webhook externo (críticos)          evento "infra_alert" no histórico
                                     └──► Watchdog ──► healthchecks.io (silêncio = monitoramento caiu)
celery_beat ──► ops.deadman_ping (60 s) ──► healthchecks.io (silêncio = app/host caiu; /fail = degradado)
```

O endpoint do backend fica desativado (404) sem `ALERTMANAGER_WEBHOOK_TOKEN` e o Caddy responde 404 para ele
(só a rede interna do Docker alcança). Cada alerta do Prometheus vira um alerta `infra_alert` (configurável na tela
Monitoramento como os demais) com o resumo e o link do runbook.

## Validar

```bash
docker run --rm --entrypoint promtool -v "$PWD/infra/monitoring:/m" prom/prometheus:v2.54.1 check rules /m/rules/isctrade.yml
docker run --rm --entrypoint promtool -v "$PWD/infra/monitoring:/m" -w /m/tests prom/prometheus:v2.54.1 test rules rules_test.yml
docker run --rm --entrypoint amtool -v "$PWD/infra/monitoring:/m" prom/alertmanager:v0.28.1 check-config /m/alertmanager.yml
# alerta de teste ponta a ponta (chega ao WhatsApp como "Alerta: TesteOps"):
docker compose exec alertmanager amtool --alertmanager.url=http://127.0.0.1:9093 alert add TesteOps severity=warning \
  --annotation='summary="Teste de rota"'
```

## Métricas novas
| Métrica | Origem | Uso |
|---|---|---|
| `btp_beat_heartbeat_age_seconds` | API (Redis) | beat parado |
| `btp_worker_heartbeat_age_seconds{queue}` | API (Redis) | worker/fila parada |
| `btp_bot_last_tick_age_seconds` | API (Redis) | bots sem tick |
| `btp_ws_last_message_age_seconds{stream,kind}` | streams | dados de mercado parados |
| `btp_binance_rate_limited_total{host,status}` | workers/API | 429/418 da Binance |
| `btp_positions_unprotected` | guarda de stop do reconciliador (G-01) | posição sem stop |
| `node_*` | node-exporter | disco, memória, NTP |
| `redis_up`, `redis_*` | redis_exporter | Redis fora/senha errada (`RedisDown`), memória, clientes |
