# Runbook de operação — ISCTRADE

Procedimentos de **go-live**, **resposta a incidentes**, **backups/restauração** e **monitoramento**.
Cada alerta do Prometheus (`infra/monitoring/rules/isctrade.yml`) aponta para uma seção deste arquivo.

> Comandos assumem a raiz do repositório no servidor e o compose de produção:
> `export C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"`.
> Não é recomendação de investimento. Futuros alavancados podem causar perda total.

Sumário: [Saúde](#saude) · [Go-live](#go-live) · [Simulado do kill switch](#simulado-kill-switch) ·
[Incidentes](#incidentes) · [Backups](#backups) · [Restauração](#restauracao) ·
[Teste periódico de restauração](#teste-de-restauracao) · [Monitor externo](#monitor-externo) ·
[Perda do servidor](#perda-do-servidor) · [Rotação de segredos](#rotacao-de-segredos)

---

<a id="saude"></a>
## 1. Como saber se está tudo bem

| Onde | O que mostra |
|---|---|
| `curl -s https://$DOMAIN/health/ready` | `status`: `ready` · `degraded` · `not_ready` (HTTP 503 só em `not_ready`, ou também em `degraded` com `HEALTH_READY_STRICT=true`) |
| `$C ps` | todos os serviços `healthy` (inclusive `streams` e `celery_beat`) |
| Painel → **Monitoramento** | mesmo estado de prontidão + canal de alertas |
| Prometheus `http://127.0.0.1:9090/alerts` (túnel SSH) | alertas ativos; Grafana em `127.0.0.1:3001` |

Checagens do `/health/ready`:

| Check | Crítico? | Significado | Limite (`.env`) |
|---|---|---|---|
| `database`, `redis`, `broker` | **sim** (503) | conexões básicas | — |
| `beat` | não | heartbeat gravado pelo próprio `celery_beat` (`HeartbeatScheduler`) | `BEAT_HEARTBEAT_STALE_SECONDS=120` |
| `worker_trading`, `worker_default` | não | tarefa `ops.heartbeat` agendada pelo beat **em cada fila** rodou (prova beat → Redis → worker) | `WORKER_HEARTBEAT_STALE_SECONDS=180` |
| `streams` | não | processo vivo **e** cada WebSocket: `connected` e idade da última mensagem (mercado: mark price chega a cada 1 s) | `STREAMS_HEARTBEAT_STALE_SECONDS`, `STREAMS_MESSAGE_STALE_SECONDS=90` |
| `bot_ticks` | não | com bots ativos, idade do último tick concluído (`idle` sem bots) | `BOT_TICK_STALE_SECONDS=300` |

Healthchecks do Docker: `backend`/`frontend` por HTTP; `celery_worker*` por `celery inspect ping`; `celery_beat` e
`streams` pela idade de um arquivo de heartbeat local (`python -m app.healthcheck file /tmp/... 60`), sem depender
da rede; `caddy`, `prometheus`, `alertmanager`, `grafana`, `node_exporter`, `postgres`, `redis` e `backup` também têm check.

---

<a id="go-live"></a>
## 2. Go-live (paper → testnet → live)

Faça na ordem; não pule etapas. Registre data/responsável de cada item.

### 2.1 Servidor
- [ ] Ubuntu/Debian atualizado, firewall só **22/80/443**, SSH por chave, usuário sem root no grupo `docker`.
- [ ] **Relógio sincronizado** (a Binance rejeita requisições fora do `recvWindow`, erro `-1021`):
      `timedatectl` → `System clock synchronized: yes` (chrony ou systemd-timesyncd). Ver [Relógio/NTP](#relogio-ntp).
- [ ] Disco com folga (≥ 20 GB livres) — logs do Docker já rotacionam (`LOG_MAX_SIZE` × `LOG_MAX_FILE` por container).
- [ ] DNS do domínio apontando para o servidor (Caddy emite o certificado Let's Encrypt sozinho).

### 2.2 Configuração (`.env`)
- [ ] `cp .env.example .env`; gere `JWT_SECRET`, `MASTER_ENCRYPTION_KEY`, `POSTGRES_PASSWORD` (comandos no próprio arquivo).
- [ ] `ENVIRONMENT=production`, `DOMAIN=seu.dominio`, `ACME_EMAIL`.
- [ ] **`CORS_ORIGINS=https://seu.dominio`** — obrigatório (também valida o WebSocket do painel; sem ele o tempo real
      fica em "Reconectando…"). O `deploy.sh` recusa o deploy se faltar.
- [ ] Cadastro: `REGISTRATION_MODE=closed` (ou `invite`); a produção **recusa iniciar** com cadastro aberto
      (`ALLOW_REGISTRATION=true`/`open`) sem `ALLOW_OPEN_REGISTRATION_IN_PRODUCTION=true`. Crie o admin com
      `docker compose exec backend python -m app.cli create-admin --email voce@exemplo.com` e confira com
      `python -m app.cli registration-status`.
- [ ] `LIVE_TRADING_ENABLED=false` até a etapa 2.6.
- [ ] Alertas: `WHATSAPP_*` (docs/ALERTS.md) e envie um alerta de teste pela tela Monitoramento.
- [ ] Monitoramento: `COMPOSE_PROFILES=monitoring,backup`, `GRAFANA_ADMIN_PASSWORD` forte,
      `ALERTMANAGER_WEBHOOK_TOKEN` (aleatório), `DEADMAN_PING_URL` ([Monitor externo](#monitor-externo)).
- [ ] Backups: `BACKUP_AGE_RECIPIENTS` (chave pública) + `BACKUP_RCLONE_REMOTE` ([Backups](#backups)).
- [ ] Guarde **fora do servidor** (cofre de senhas): `.env`, `MASTER_ENCRYPTION_KEY` e a chave **privada** age.
      Sem a `MASTER_ENCRYPTION_KEY` as chaves de API cifradas no banco são irrecuperáveis.

### 2.3 Deploy
```bash
./scripts/deploy.sh          # pull + backup + build + migrações + up + health checks + smoke test (/health/ready)
$C ps                        # todos healthy
curl -s https://seu.dominio/health/ready | python3 -m json.tool
```
- [ ] `status` = `ready` (os WebSockets só aparecem quando há bots ativos/contas live; o feed do painel sempre).
- [ ] Prometheus → Status → Targets: tudo `UP`; Alertas: só `Watchdog` disparando.
- [ ] O monitor externo recebe pings (dead man's switch) e o backup diário aparece em `./backups` e no remoto.

### 2.4 Paper (≥ 2 semanas)
- [ ] Bots em paper com os parâmetros que irão para o live; acompanhe PnL, rejeições de risco e alertas.
- [ ] Simule falhas: `$C restart celery_worker_trading`, `$C stop streams` (alerta em ≤ 5 min), `$C start streams`.

### 2.5 Testnet
- [ ] Conta Binance **Testnet** com chave **sem saque**, restrita ao IP do servidor; `Verificar conta` no painel.
- [ ] Ordens manuais pequenas (abrir, stop, fechar) e um bot por ≥ 1 semana; confira stops na corretora.
- [ ] **Simulado do kill switch em testnet** ([seção 3](#simulado-kill-switch)) — obrigatório antes do live.

### 2.6 Live com capital mínimo
- [ ] Chave de API **mainnet**: só *Futures*, **sem saque**, restrita ao IP do servidor; conta em **one-way mode**.
- [ ] `LIVE_TRADING_ENABLED=true` + `./scripts/deploy.sh --no-pull`; conta em modo `live` com capital mínimo e
      limites conservadores (risco/trade ≤ 0,5 %, alavancagem ≤ 3x, perda diária ≤ 2 %).
- [ ] **Simulado do kill switch em live** com 1 posição mínima ([seção 3](#simulado-kill-switch)).
- [ ] Primeiras 48 h: revisão manual 2×/dia (posições × stops na Binance, alertas, `/health/ready`).
- [ ] Aumentos de capital só após ≥ 4 semanas estáveis e comparação paper × live (ROADMAP Fase 5).

---

<a id="simulado-kill-switch"></a>
## 3. Simulado do kill switch (drill)

Objetivo: provar, com registro, que os kill switches **param os bots, bloqueiam novas aberturas e (no da conta,
se configurado) fecham posições**, e que os alertas chegam. Faça em testnet, repita em live com 1 posição mínima e
depois **a cada trimestre**.

Comportamento atual (confira em `services/kill_switch.py` se mudar):

| | Global (Admin) | Por conta (drawdown / perda diária) |
|---|---|---|
| Bots | todos → `stopped` | os da conta → parados |
| Contas | `running` → `stopped` (`status_reason=global_kill_switch`) | → `killed` (reset manual) |
| Novas aberturas | recusadas (`global_kill_switch`); ordens *reduce-only* continuam | recusadas |
| Posições abertas | **permanecem** (com seus stops) — feche pelo painel/Binance se quiser zerar | fechadas a mercado e ordens canceladas se `KILL_SWITCH_CLOSE_POSITIONS=true` |
| Auditoria | `admin.global_kill_switch` | `kill_switch.*` / `risk.*` |

1. Pré-condição: 1 bot ativo e 1 posição aberta pequena com stop; anote horário, conta, símbolo e quantidade.
2. **Global**: Painel Admin → Kill switch → Ativar (motivo "simulado AAAA-MM-DD") ou
   `POST /api/v1/admin/kill-switch` com `{"active": true, "reason": "simulado"}` (sessão de super-admin).
3. Verifique em até 1 min:
   - [ ] bots `stopped` e contas paradas no painel; nenhum tick novo nos logs do `celery_worker_trading`;
   - [ ] nova ordem manual de **abertura** é recusada (`global_kill_switch`); fechar a posição pelo painel funciona
         (reduce-only) — feche-a e confirme **na Binance** que zerou e que não sobrou ordem aberta;
   - [ ] alerta "Kill switch GLOBAL ativado" no WhatsApp e `GlobalKillSwitchActive` no Prometheus;
   - [ ] evento `admin.global_kill_switch` na Auditoria global.
4. Desative: Admin → Kill switch → Desativar; reinicie contas e bots um a um.
5. **Por conta**: em testnet, com uma posição aberta, reduza temporariamente o limite de perda diária da conta abaixo
   do PnL do dia e aguarde o próximo tick → conta `killed`, posição fechada a mercado na Binance (com
   `KILL_SWITCH_CLOSE_POSITIONS=true`), alerta "Kill switch da conta acionado" e `AccountKillSwitchTriggered`.
   Restaure o limite e `POST /api/v1/accounts/{id}/reset-kill-switch`.
6. Registre: horário de acionamento, tempo até as posições zerarem, alertas recebidos (e atraso), falhas.

Critério de aprovação: nenhuma abertura nova após o global; no da conta, posição zerada na Binance em < 60 s
(ou nenhuma nova abertura, se `KILL_SWITCH_CLOSE_POSITIONS=false`); alertas recebidos em < 2 min. Qualquer falha bloqueia o go-live até corrigir.

---

<a id="incidentes"></a>
## 4. Resposta a incidentes

**Regra de ouro:** em dúvida sobre o estado das posições, **acione o kill switch global** e confira na Binance.
Depois investigue. Registre cada incidente (horário, impacto, causa, ação, prevenção).

Primeiros comandos:
```bash
$C ps                                   # quem não está healthy?
curl -s https://$DOMAIN/health/ready    # qual check falha?
$C logs --since 30m --tail 200 backend celery_worker_trading streams celery_beat
df -h; free -m; timedatectl
```

<a id="servico-fora-do-ar"></a>
### 4.1 Serviço fora do ar (`ServiceDown`)
1. `$C ps` e `$C logs --tail 200 <serviço>`; procure `Traceback`, `OOMKilled` (`docker inspect <container> | grep OOM`).
2. `$C up -d <serviço>` (o `restart: unless-stopped` normalmente já resolve). OOM: aumente `APP_MEM_LIMIT`.
3. Se o backend não sobe por configuração (`production_problems`), a mensagem diz qual variável corrigir.
4. Se caiu após um deploy: `./scripts/deploy.sh rollback` (e, se houve migração, o `restore_db.sh --replace` indicado).

<a id="beat-ou-worker-parado"></a>
### 4.2 Beat ou worker parado (`BeatHeartbeatStale`, `WorkerHeartbeatStale`)
Sem beat: **nenhum tick, reconciliação, snapshot ou reset diário de risco**. Sem worker `trading`: ticks e ordens
paper param (stops na corretora continuam valendo para contas live).
1. `$C logs --tail 200 celery_beat` / `celery_worker_trading`. `$C restart celery_beat celery_worker_trading`.
2. Fila acumulada? `$C exec redis redis-cli -n 1 llen trading` (fila `trading` no broker DB 1).
3. Mantenha **uma única** instância de `celery_beat` (duas geram ticks duplicados).
4. Se não normalizar em 10 min e houver posições live: kill switch global e gestão manual na Binance.

<a id="dados-de-mercado-parados"></a>
### 4.3 Dados de mercado parados / WebSocket caído (`StaleMarketData`, `WebSocketDisconnected`, `StreamsHeartbeatStale`)
Impacto: preços de marcação (SL/TP de paper, painel) e eventos de ordem/posição em tempo real param; o reconciliador
(REST, a cada `RECONCILE_SECONDS`) continua corrigindo o estado das contas live.
1. `curl -s https://$DOMAIN/health/ready` → `checks.streams.streams[]` mostra qual WS e há quanto tempo.
2. `$C logs --tail 200 streams` — `ws ... error` com DNS/timeout = rede do servidor; 451 = região bloqueada.
3. Teste do host: `curl -sI https://fapi.binance.com/fapi/v1/ping` e `getent hosts fstream.binance.com`.
4. `$C restart streams`. Persistindo com posições live: kill switch global.

<a id="bots-sem-tick"></a>
### 4.4 Bots sem tick (`NoBotTicks`, `BotTickErrors`)
1. Verifique beat/worker ([4.2](#beat-ou-worker-parado)) e Redis (`$C exec redis redis-cli ping`).
2. `$C logs --tail 300 celery_worker_trading | grep -i error`; bots em `error` aparecem no painel com o motivo.
3. Erros de exchange (`-1021` relógio, `-2015` chave/IP, 418/429): seções [4.5](#limite-de-peso-ou-ban-da-binance) e [4.10](#relogio-ntp).

<a id="limite-de-peso-ou-ban-da-binance"></a>
### 4.5 Limite de peso ou ban da Binance (`BinanceWeightHigh`, `BinanceRateLimited`)
- **429**: limite de requisições; o cliente pausa sozinho até `Retry-After`. **418**: IP banido (2 min a 3 dias).
1. Não reinicie em loop (piora o ban). Veja quem consome: `$C logs --since 15m celery_worker celery_worker_trading | grep -c binance`.
2. Reduza carga: aumente `SCANNER_REFRESH_SECONDS`, diminua `SCANNER_MAX_SYMBOLS`, aumente `BOT_TICK_SECONDS`; `$C up -d`.
3. Durante ban com posições live: os stops já estão na corretora; **não** abra posições; se precisar fechar, use o app da Binance.

<a id="kill-switch-acionado"></a>
### 4.6 Kill switch acionado (`GlobalKillSwitchActive`, `AccountKillSwitchTriggered`)
1. Descubra o motivo: Admin → Auditoria (`kill_switch.*`, `risk.*`) e o alerta recebido.
2. Confira na Binance: o kill switch **da conta** fecha posições (com `KILL_SWITCH_CLOSE_POSITIONS=true`); o
   **global** só para bots/contas e bloqueia aberturas — as posições continuam abertas com seus stops. Feche
   manualmente (painel ou Binance) o que precisar zerar.
3. Só reative depois de entender a causa (perda real × depósito/saque distorcendo o pico de patrimônio × bug).
4. Reativação: Admin → Kill switch → Desativar / `reset-kill-switch` da conta; religue os bots um a um.

<a id="posicao-sem-stop"></a>
### 4.7 Posição sem stop na corretora (`UnprotectedPosition`) — **crítico**
1. Abra a Binance (app/web) → Futuros → Ordens abertas: existe `STOP_MARKET`/`closePosition` para o símbolo?
2. Se não existir: **recoloque o stop manualmente** (ou feche a posição a mercado). Não espere o próximo tick.
3. Investigue: stop cancelado manualmente? posição aberta fora da plataforma? `-2021` (stop atravessou o preço)?
4. Se várias contas/posições: kill switch global.

<a id="divergencia-de-reconciliacao"></a>
### 4.8 Divergência de reconciliação (`ReconcilerDrift`)
Muitas correções = alguém operando a mesma conta fora da plataforma, eventos de WS perdidos ou bug.
1. Admin → Auditoria (`reconciler.*`); compare posições do painel × Binance.
2. Use uma subconta exclusiva para a plataforma. Divergência persistente com posição live: kill switch.

<a id="alertas-nao-chegam"></a>
### 4.9 Alertas não chegam (`AlertDeliveryFailing`)
1. Painel → Monitoramento → "Enviar alerta de teste"; veja o erro do provedor no histórico.
2. Token/sessão do provedor WhatsApp expirado? (docs/ALERTS.md). Os alertas críticos do Prometheus também vão para o
   webhook externo (`infra/monitoring/secrets/external_url`), que não depende do backend.

<a id="relogio-ntp"></a>
### 4.10 Relógio / NTP (`ClockSkew`, erro Binance `-1021`)
```bash
timedatectl                                  # "System clock synchronized: yes"
sudo apt install -y chrony && sudo systemctl enable --now chrony
chronyc tracking                             # "System time" < 0.1 s
```
Containers usam o relógio do host. O cliente Binance repete uma vez em `-1021`, mas desvio persistente bloqueia ordens.

<a id="disco-cheio"></a>
### 4.11 Disco cheio / memória (`DiskSpaceLow`, `DiskSpaceCritical`, `HostMemoryLow`)
```bash
df -h; docker system df
docker image prune -af --filter "until=168h"   # imagens antigas de deploys anteriores
du -sh backups; ls -lt backups | head           # retenção local: BACKUP_KEEP / BACKUP_RETENTION_DAYS
```
Logs do Docker já rotacionam (10 MB × 5 por container). Retenção de dados no banco (tarefa diária `ops.cleanup`):
`REFRESH_TOKEN_RETENTION_DAYS`, `ALERTS_RETENTION_DAYS`, `EQUITY_SNAPSHOT_RETENTION_DAYS`, `AUDIT_RETENTION_DAYS`
(0 = para sempre; a auditoria fica para sempre por padrão). Notícias e scanner têm retenção própria.

<a id="credenciais-vazadas"></a>
### 4.12 Suspeita de vazamento de credenciais
1. Kill switch global. 2. **Revogue as chaves de API na Binance** (e crie novas, sem saque, restritas por IP).
3. Troque `JWT_SECRET` (derruba todas as sessões) e senhas; `./scripts/deploy.sh --no-pull`.
4. Investigue: Auditoria global, `$C logs backend | grep -i login`. 5. Veja [Rotação de segredos](#rotacao-de-segredos).

---

<a id="backups"></a>
## 5. Backups

`scripts/backup_db.sh`: `pg_dump` → gzip → **criptografia** (age recomendado, ou GPG) → `./backups` (+ `sha256`) →
**cópia off-site** (rclone: S3, Backblaze B2, Cloudflare R2, Wasabi, MinIO…) → retenção local e remota →
ping do dead man's switch (`BACKUP_PING_URL`). O `deploy.sh` também faz backup antes de cada migração.

### 5.1 Chave de criptografia (uma vez, **no seu computador**, não no servidor)
```bash
age-keygen -o isctrade-backup.key      # imprime "Public key: age1..."
# guarde isctrade-backup.key no cofre de senhas (sem ela os backups são irrecuperáveis)
```
No `.env` do servidor: `BACKUP_AGE_RECIPIENTS=age1...` (só a chave **pública**; pode listar várias separadas por vírgula).

### 5.2 Off-site (rclone)
Configure por variáveis no `.env` (exemplo S3 no `.env.example`) ou `infra/backup/secrets/rclone.conf`
(ignorado pelo git) e defina `BACKUP_RCLONE_REMOTE=s3:meu-bucket/isctrade`. Use credenciais com permissão só de
escrita/listagem nesse bucket e, se o provedor permitir, **object lock/versão** para resistir a ransomware.
`BACKUP_REMOTE_RETENTION_DAYS` (padrão 30) apaga cópias remotas antigas.

### 5.3 Agendamento (escolha um)
- **Serviço do compose** (recomendado): `COMPOSE_PROFILES=monitoring,backup` → container `backup` roda todo dia às
  `BACKUP_TIME_UTC` (padrão 03:00) e fica `unhealthy` se o último backup tiver mais de 26 h.
  `BACKUP_UID/GID` = dono do repositório no host (`id -u`), para os arquivos serem legíveis pelo restore.
- **systemd timer**: `infra/backup/isctrade-backup.{service,timer}` (instruções no arquivo).
- **cron**: `infra/backup/crontab.example`.

Manual: `./scripts/backup_db.sh`. Requer no host `age` (e `rclone`, se off-site): `sudo apt install age rclone`.

<a id="restauracao"></a>
## 6. Restauração

```bash
# 1) obter o arquivo (local ou do remoto) e a chave privada (temporariamente, fora do repositório)
rclone copy "$BACKUP_RCLONE_REMOTE/db_20261022_030000.sql.gz.age" backups/   # se necessário
export BACKUP_AGE_IDENTITY_FILE=~/isctrade-backup.key

# 2a) conferir sem tocar em nada (banco temporário, confere e apaga)
./scripts/restore_db.sh --verify backups/db_20261022_030000.sql.gz.age
# 2b) restaurar ao lado, num banco novo, para inspecionar
./scripts/restore_db.sh --to trading_inspecao backups/db_...sql.gz.age
# 2c) SUBSTITUIR o banco de produção (faz backup do estado atual, para a aplicação, troca, migra e sobe)
./scripts/restore_db.sh --replace backups/db_...sql.gz.age

shred -u ~/isctrade-backup.key   # não deixe a chave privada no servidor
```
O `--replace` preserva o banco anterior como `trading_before_restore_<data>` até você apagá-lo. Após restaurar,
o reconciliador re-sincroniza posições/ordens das contas live com a Binance no próximo ciclo; confira o painel.

<a id="teste-de-restauracao"></a>
## 7. Teste periódico de restauração (mensal)

Backup que nunca foi restaurado não é backup. **Todo mês** (dia 1) e após mudanças de infraestrutura:
1. Baixe o backup **do remoto** (não o local) para validar a cópia off-site.
2. `BACKUP_AGE_IDENTITY_FILE=... ./scripts/restore_db.sh --verify <arquivo>` → confere checksum, descriptografa,
   restaura num banco temporário, valida `alembic_version` e contagens de tabelas e apaga o banco.
3. Registre data, arquivo, tamanho, duração e resultado. Falha = incidente ([Backups](#backups)).

Automação opcional: `infra/backup/isctrade-restore-test.{service,timer}` (ou `crontab.example`) com
`BACKUP_TEST_PING_URL` (healthchecks.io avisa se o teste não rodar/falhar). Isso exige a chave privada no servidor;
se preferir não mantê-la lá, faça o teste manual mensal em outra máquina com Docker (suba só `postgres` do compose).

---

<a id="monitor-externo"></a>
## 8. Monitor externo e dead man's switch

Alertas internos não funcionam se o servidor inteiro cair. Use **dois** sinais externos:

1. **Dead man's switch (push)** — [healthchecks.io](https://healthchecks.io) (grátis), Uptime Kuma (push) ou Better Stack:
   - Check "isctrade-app": período 1 min, tolerância 5 min → `DEADMAN_PING_URL=https://hc-ping.com/<uuid>`.
     O beat agenda `ops.deadman_ping` a cada `DEADMAN_PING_SECONDS`: ping normal com `/health/ready` = `ready`,
     `<url>/fail` quando degradado; **silêncio** = beat, worker, Redis ou o host caíram.
   - Check "isctrade-monitoring": período 1 min → URL em `infra/monitoring/secrets/deadman_url` (alerta `Watchdog`
     do Prometheus via Alertmanager; silêncio = Prometheus/Alertmanager caíram).
   - Check "isctrade-backup": período 1 dia, tolerância 2 h → `BACKUP_PING_URL`; "isctrade-restore-test": 1 mês.
   - Integre o healthchecks.io ao WhatsApp/Telegram/e-mail/SMS.
2. **Uptime (pull)** — UptimeRobot/Better Stack/Uptime Kuma externo: monitor HTTP(S) de
   `https://$DOMAIN/health/ready` a cada 1–5 min, tipo *keyword* procurando `"status":"ready"` (ou
   `HEALTH_READY_STRICT=true` e checar HTTP 200). Inclui alerta de expiração do certificado TLS.

Webhook externo dos alertas críticos (independe do backend): grave a URL em
`infra/monitoring/secrets/external_url` (ntfy.sh, Discord/Slack-compatível, n8n, gateway WhatsApp que aceite o JSON
do Alertmanager). Detalhes em `infra/monitoring/README.md`.

---

<a id="perda-do-servidor"></a>
## 9. Perda total do servidor
1. Kill switch não é acessível: **feche/gerencie posições pelo app da Binance**; revogue a chave de API se o servidor
   pode ter sido comprometido.
2. Novo servidor ([2.1](#go-live)), clone o repositório, restaure o `.env` do cofre (mesma `MASTER_ENCRYPTION_KEY`).
3. `$C up -d postgres redis`; baixe o último backup do remoto; `./scripts/restore_db.sh --replace <arquivo>`.
4. `./scripts/deploy.sh --no-pull`; aponte o DNS; confira `/health/ready`, posições × Binance, alertas.
RPO = intervalo de backup (24 h por padrão; o estado de posições/ordens é re-sincronizado da Binance). RTO ≈ 1 h.

<a id="rotacao-de-segredos"></a>
## 10. Rotação de segredos
| Segredo | Como | Efeito |
|---|---|---|
| `JWT_SECRET` | novo valor + `deploy.sh --no-pull` | todos fazem login de novo |
| `POSTGRES_PASSWORD` | `ALTER USER trading PASSWORD '...'` no banco + `.env` (`POSTGRES_PASSWORD` e `DATABASE_URL`) + deploy | — |
| Chaves de API Binance | criar nova na Binance → atualizar a conta no painel → revogar a antiga | — |
| `MASTER_ENCRYPTION_KEY` | sem rotação automática ainda (GAPS G-29): recadastre as chaves de API após trocar | chaves antigas ilegíveis |
| `ALERTMANAGER_WEBHOOK_TOKEN`, `METRICS_TOKEN` | novo valor + `deploy.sh --no-pull` (o deploy reescreve `infra/monitoring/secrets/webhook_token`) | — |
| Chave age dos backups | gere nova, acrescente a pública em `BACKUP_AGE_RECIPIENTS`; mantenha a antiga para backups antigos | — |
