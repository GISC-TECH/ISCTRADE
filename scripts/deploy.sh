#!/usr/bin/env bash
# Deploy de produção da Binance Trading Platform.
#
# Uso (no servidor, na raiz do repositório):
#   ./scripts/deploy.sh            # git pull + backup + build + migrações + up + health check
#   ./scripts/deploy.sh --no-pull  # usa o código já presente no diretório
#   ./scripts/deploy.sh --skip-backup
#   ./scripts/deploy.sh rollback   # volta ao commit do deploy anterior (ver .deploy/previous_ref)
#
# Variáveis opcionais: HEALTH_TIMEOUT (segundos, padrão 180), COMPOSE (comando compose customizado),
# DOCKER (padrão "docker"; ex. DOCKER="sudo docker" COMPOSE="sudo docker compose -f ... -f ...").
# Perfis opcionais via .env/ambiente: COMPOSE_PROFILES=monitoring,backup (Prometheus/Alertmanager, backup agendado).
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
STATE_DIR="$ROOT/.deploy"
DOCKER="${DOCKER:-docker}"
COMPOSE="${COMPOSE:-$DOCKER compose -f docker-compose.yml -f docker-compose.prod.yml}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
PULL=1
BACKUP=1
ACTION="deploy"

for arg in "$@"; do
  case "$arg" in
    --no-pull) PULL=0 ;;
    --skip-backup) BACKUP=0 ;;
    rollback) ACTION="rollback" ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "Argumento desconhecido: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n\033[1;34m>> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERRO:\033[0m %s\n' "$*" >&2; exit 1; }

env_value() { # lê KEY=valor do .env sem executar o arquivo
  grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' || true
}

https_origin() { # origem pública esperada pelo navegador (porta só quando != 443)
  local domain port
  domain="$(env_value DOMAIN)"
  port="$(env_value HTTPS_PORT)"
  if [[ -z "$port" || "$port" == "443" ]]; then echo "https://$domain"; else echo "https://$domain:$port"; fi
}

check_cors() {
  local origin cors
  origin="$(https_origin)"
  cors="$(env_value CORS_ORIGINS | tr -d ' []"')"
  # CORS_ORIGINS também valida o Origin do WebSocket do painel: sem o domínio, o tempo real recebe 403.
  if [[ ",$cors," != *",$origin,"* ]]; then
    die "CORS_ORIGINS deve conter $origin (atual: ${cors:-vazio}). Sem isso o painel em tempo real fica em \"Reconectando…\"."
  fi
}

check_monitoring() {
  local profiles
  profiles="${COMPOSE_PROFILES:-$(env_value COMPOSE_PROFILES)}"
  [[ ",$profiles," == *",monitoring,"* ]] || return 0
  local gpw
  gpw="$(env_value GRAFANA_ADMIN_PASSWORD)"
  [[ -n "$gpw" && "$gpw" != "admin" ]] || die "perfil monitoring ativo: defina GRAFANA_ADMIN_PASSWORD forte no .env."
  local token
  token="$(env_value ALERTMANAGER_WEBHOOK_TOKEN)"
  mkdir -p infra/monitoring/secrets
  # O Alertmanager roda como "nobody" no container: diretório só atravessável (711) e arquivos 644.
  chmod 711 infra/monitoring/secrets
  if [[ -n "$token" ]]; then # mantém o segredo do Alertmanager igual ao do backend (arquivo ignorado pelo git)
    printf '%s' "$token" > infra/monitoring/secrets/webhook_token
  else
    echo "Aviso: ALERTMANAGER_WEBHOOK_TOKEN vazio — alertas do Prometheus não chegarão ao WhatsApp." >&2
  fi
  chmod 644 infra/monitoring/secrets/* 2>/dev/null || true
  for f in external_url deadman_url; do
    [[ -s "infra/monitoring/secrets/$f" ]] || echo "Aviso: infra/monitoring/secrets/$f ausente (veja infra/monitoring/README.md)." >&2
  done
}

check_redis() { # G-27: Redis sempre com senha (ACL em infra/redis/entrypoint.sh)
  local pw
  pw="$(env_value REDIS_PASSWORD)"
  [[ ${#pw} -ge 16 ]] || die "REDIS_PASSWORD ausente ou curto (mínimo 16 caracteres; ex.: python3 -c 'import secrets; print(secrets.token_hex(24))')."
  [[ "$pw" =~ ^[A-Za-z0-9._~-]+$ ]] || die "REDIS_PASSWORD: use só letras, números e . _ ~ - (evita problemas de escape no .env/compose)."
  local name
  for name in REDIS_URL CELERY_BROKER_URL CELERY_RESULT_BACKEND; do
    case "$(env_value "$name")" in
      redis://*@* | rediss://*@*) echo "Aviso: $name já contém credenciais; confira se batem com REDIS_USERNAME/REDIS_PASSWORD." >&2 ;;
    esac
  done
}

check_registration() { # G-03b: o backend recusa subir em produção com cadastro aberto; falhe antes do deploy
  local mode
  mode="$(env_value REGISTRATION_MODE | tr '[:upper:]' '[:lower:]')"
  if [[ -z "$mode" ]]; then
    if [[ "$(env_value ALLOW_REGISTRATION | tr '[:upper:]' '[:lower:]')" =~ ^(true|1|yes|on)$ ]]; then mode=open; else mode=closed; fi
  fi
  if [[ "$mode" == "open" && ! "$(env_value ALLOW_OPEN_REGISTRATION_IN_PRODUCTION | tr '[:upper:]' '[:lower:]')" =~ ^(true|1|yes|on)$ ]]; then
    die "cadastro aberto (REGISTRATION_MODE=open / ALLOW_REGISTRATION=true): use REGISTRATION_MODE=invite ou closed e crie o admin com 'python -m app.cli create-admin'."
  fi
}

check_key_rotation() { # G-29: chaves antigas válidas e cadeia de auditoria fixada durante a rotação
  local prev key
  prev="$(env_value MASTER_ENCRYPTION_KEYS_PREVIOUS)"
  [[ -n "$prev" ]] || return 0
  IFS=',' read -ra keys <<< "$prev"
  for key in "${keys[@]}"; do
    key="${key// /}"
    [[ -z "$key" ]] && continue
    [[ "$(printf '%s' "$key" | base64 -d 2>/dev/null | wc -c)" -eq 32 ]] || die "MASTER_ENCRYPTION_KEYS_PREVIOUS: cada chave deve ser base64 de 32 bytes."
  done
  [[ -n "$(env_value AUDIT_CHAIN_KEY)" ]] || die "rotação em andamento sem AUDIT_CHAIN_KEY: rode '$COMPOSE run --rm --no-deps backend python -m app.keys_cli audit-pin --previous' e grave o valor no .env (docs/RUNBOOK.md §10)."
  echo "Rotação de chave em andamento: depois do deploy rode '$COMPOSE exec backend python -m app.keys_cli reencrypt' e esvazie MASTER_ENCRYPTION_KEYS_PREVIOUS." >&2
}

preflight() {
  command -v "${DOCKER%% *}" >/dev/null || die "docker não encontrado."
  $COMPOSE version >/dev/null 2>&1 || die "docker compose v2 não encontrado."
  [[ -f .env ]] || die "arquivo .env não encontrado. Copie .env.example e preencha os valores."
  if grep -Ev '^[[:space:]]*#' .env | grep -q "CHANGE_ME"; then die ".env ainda contém valores CHANGE_ME."; fi
  for key in JWT_SECRET MASTER_ENCRYPTION_KEY POSTGRES_PASSWORD DATABASE_URL DOMAIN; do
    [[ -n "$(env_value "$key")" ]] || die "$key não definido no .env."
  done
  local mek
  mek="$(env_value MASTER_ENCRYPTION_KEY)"
  if [[ "$(printf '%s' "$mek" | base64 -d 2>/dev/null | wc -c)" -ne 32 ]]; then
    die "MASTER_ENCRYPTION_KEY deve ser base64 de exatamente 32 bytes."
  fi
  check_redis
  check_key_rotation
  check_registration
  if [[ "$(env_value LIVE_TRADING_ENABLED)" == "true" ]]; then
    printf '\033[1;33mATENÇÃO:\033[0m LIVE_TRADING_ENABLED=true — contas em modo "live" enviarão ORDENS REAIS.\n'
  else
    echo "Modo de execução: paper (LIVE_TRADING_ENABLED != true). Nenhuma ordem real será enviada."
  fi
  check_cors
  check_monitoring
  if [[ -z "$(env_value BACKUP_AGE_RECIPIENTS)" && -z "$(env_value BACKUP_GPG_RECIPIENT)" ]]; then
    echo "Aviso: backups sem criptografia (defina BACKUP_AGE_RECIPIENTS; docs/RUNBOOK.md §Backups)." >&2
  fi
  $COMPOSE config -q || die "docker compose config inválido."
}

backup() {
  [[ "$BACKUP" == "1" ]] || { echo "Backup ignorado (--skip-backup)."; return; }
  if $COMPOSE ps --status running --services 2>/dev/null | grep -qx postgres; then
    log "Backup do PostgreSQL antes das migrações"
    COMPOSE="$COMPOSE" ./scripts/backup_db.sh
    # shellcheck disable=SC2010,SC2012
    ls -1t backups/db_*.sql.gz* 2>/dev/null | grep -v '\.sha256$' | head -n1 > "$STATE_DIR/pre_deploy_backup" || true
  else
    echo "PostgreSQL não está em execução (primeiro deploy?) — backup ignorado."
  fi
}

wait_healthy() {
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  local services=(postgres redis backend frontend celery_worker celery_worker_trading celery_beat streams caddy)
  log "Aguardando health checks (${HEALTH_TIMEOUT}s): ${services[*]}"
  while :; do
    local pending=()
    for svc in "${services[@]}"; do
      local cid status
      cid="$($COMPOSE ps -q "$svc" 2>/dev/null || true)"
      status="$( [[ -n "$cid" ]] && $DOCKER inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo missing)"
      [[ "$status" == "healthy" ]] || pending+=("$svc=$status")
    done
    if [[ ${#pending[@]} -eq 0 ]]; then echo "Todos os serviços saudáveis."; return 0; fi
    if (( $(date +%s) > deadline )); then
      echo "Serviços não saudáveis: ${pending[*]}" >&2
      return 1
    fi
    sleep 5
  done
}

smoke_test() {
  local domain port body
  domain="$(env_value DOMAIN)"
  port="$(env_value HTTPS_PORT)"
  port="${port:-443}"
  log "Smoke test via Caddy (https://${domain}:${port}/health/ready)"
  if body="$(curl -sS --max-time 10 -k --resolve "${domain}:${port}:127.0.0.1" "https://${domain}:${port}/health/ready")"; then
    local status
    status="$(printf '%s' "$body" | sed -n 's/.*"status":"\([a-z_]*\)".*/\1/p' | head -n1)"
    case "$status" in
      ready) echo "OK (ready)" ;;
      degraded) echo "Aviso: /health/ready = degraded (beat/worker/WebSocket/ticks). Detalhes: $body" >&2 ;;
      *) echo "Aviso: /health/ready = ${status:-?}: $body" >&2 ;;
    esac
  else
    echo "Aviso: /health/ready via Caddy falhou (DNS/certificado ainda propagando?). Verifique: $COMPOSE logs caddy" >&2
  fi
}

deploy() {
  preflight
  mkdir -p "$STATE_DIR"
  local prev
  prev="$(git rev-parse HEAD)"
  if [[ "$PULL" == "1" ]]; then
    log "Atualizando código (git pull --ff-only)"
    git pull --ff-only
  fi
  local current
  current="$(git rev-parse HEAD)"
  echo "Commit: $prev -> $current"

  log "Build das imagens"
  $COMPOSE build

  log "Infraestrutura (PostgreSQL, Redis)"
  $COMPOSE up -d --wait postgres redis

  backup

  log "Migrações (alembic upgrade head)"
  $COMPOSE run --rm migrate

  log "Subindo serviços"
  $COMPOSE up -d --remove-orphans

  if ! wait_healthy; then
    $COMPOSE ps
    $COMPOSE logs --tail=80 backend celery_worker celery_worker_trading celery_beat streams frontend || true
    die "deploy não ficou saudável. Para voltar: ./scripts/deploy.sh rollback (restaure o backup se houve migração)."
  fi
  smoke_test

  [[ "$prev" != "$current" ]] && echo "$prev" > "$STATE_DIR/previous_ref"
  echo "$current" > "$STATE_DIR/current_ref"
  log "Deploy concluído ($current)"
  $COMPOSE ps
}

rollback() {
  [[ -f "$STATE_DIR/previous_ref" ]] || die "nenhum deploy anterior registrado em .deploy/previous_ref."
  local ref
  ref="$(cat "$STATE_DIR/previous_ref")"
  printf '\033[1;33mATENÇÃO:\033[0m o rollback NÃO desfaz migrações do banco. Se a versão nova alterou o schema,\n'
  echo "restaure o backup feito antes do deploy:"
  if [[ -s "$STATE_DIR/pre_deploy_backup" ]]; then
    echo "  ./scripts/restore_db.sh --replace $(cat "$STATE_DIR/pre_deploy_backup")"
  else
    echo "  (nenhum backup pré-deploy registrado; veja ./backups e docs/RUNBOOK.md §Restauração)"
  fi
  read -r -p "Voltar para $ref? [s/N] " ok
  [[ "$ok" =~ ^[sS]$ ]] || exit 1
  git checkout --detach "$ref"
  PULL=0 BACKUP=0
  deploy
}

if [[ "$ACTION" == "rollback" ]]; then rollback; else deploy; fi
