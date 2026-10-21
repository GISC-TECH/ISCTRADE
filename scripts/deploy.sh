#!/usr/bin/env bash
# Deploy de produção da Binance Trading Platform.
#
# Uso (no servidor, na raiz do repositório):
#   ./scripts/deploy.sh            # git pull + backup + build + migrações + up + health check
#   ./scripts/deploy.sh --no-pull  # usa o código já presente no diretório
#   ./scripts/deploy.sh --skip-backup
#   ./scripts/deploy.sh rollback   # volta ao commit do deploy anterior (ver .deploy/previous_ref)
#
# Variáveis opcionais: HEALTH_TIMEOUT (segundos, padrão 180), COMPOSE (comando compose customizado).
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
STATE_DIR="$ROOT/.deploy"
COMPOSE="${COMPOSE:-docker compose -f docker-compose.yml -f docker-compose.prod.yml}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
PULL=1
BACKUP=1
ACTION="deploy"

for arg in "$@"; do
  case "$arg" in
    --no-pull) PULL=0 ;;
    --skip-backup) BACKUP=0 ;;
    rollback) ACTION="rollback" ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Argumento desconhecido: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n\033[1;34m>> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERRO:\033[0m %s\n' "$*" >&2; exit 1; }

env_value() { # lê KEY=valor do .env sem executar o arquivo
  grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' || true
}

preflight() {
  command -v docker >/dev/null || die "docker não encontrado."
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
  if [[ "$(env_value LIVE_TRADING_ENABLED)" == "true" ]]; then
    printf '\033[1;33mATENÇÃO:\033[0m LIVE_TRADING_ENABLED=true — contas em modo "live" enviarão ORDENS REAIS.\n'
  else
    echo "Modo de execução: paper (LIVE_TRADING_ENABLED != true). Nenhuma ordem real será enviada."
  fi
  $COMPOSE config -q || die "docker compose config inválido."
}

backup() {
  [[ "$BACKUP" == "1" ]] || { echo "Backup ignorado (--skip-backup)."; return; }
  if $COMPOSE ps --status running --services 2>/dev/null | grep -qx postgres; then
    log "Backup do PostgreSQL antes das migrações"
    COMPOSE="$COMPOSE" ./scripts/backup_db.sh
  else
    echo "PostgreSQL não está em execução (primeiro deploy?) — backup ignorado."
  fi
}

wait_healthy() {
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  local services=(backend frontend celery_worker celery_worker_trading)
  log "Aguardando health checks (${HEALTH_TIMEOUT}s): ${services[*]}"
  while :; do
    local pending=()
    for svc in "${services[@]}"; do
      local cid status
      cid="$($COMPOSE ps -q "$svc" 2>/dev/null || true)"
      status="$( [[ -n "$cid" ]] && docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo missing)"
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
  local domain
  domain="$(env_value DOMAIN)"
  log "Smoke test via Caddy (https://${domain}/health)"
  if curl -fsS --max-time 10 -k --resolve "${domain}:443:127.0.0.1" "https://${domain}/health" >/dev/null; then
    echo "OK"
  else
    echo "Aviso: /health via Caddy falhou (DNS/certificado ainda propagando?). Verifique: $COMPOSE logs caddy" >&2
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
    $COMPOSE logs --tail=80 backend celery_worker celery_worker_trading frontend || true
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
  echo "restaure o backup correspondente em ./backups antes de prosseguir."
  read -r -p "Voltar para $ref? [s/N] " ok
  [[ "$ok" =~ ^[sS]$ ]] || exit 1
  git checkout --detach "$ref"
  PULL=0 BACKUP=0
  deploy
}

if [[ "$ACTION" == "rollback" ]]; then rollback; else deploy; fi
