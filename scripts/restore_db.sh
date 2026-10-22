#!/usr/bin/env bash
# Restauração do PostgreSQL a partir de um backup de scripts/backup_db.sh (.sql.gz, .sql.gz.age ou .sql.gz.gpg).
#
# Uso (na raiz do repositório):
#   ./scripts/restore_db.sh --verify [ARQUIVO]     # TESTE: restaura num banco temporário, confere e apaga (padrão)
#   ./scripts/restore_db.sh --to NOME [ARQUIVO]    # restaura num banco NOVO "NOME" (não toca no banco em uso)
#   ./scripts/restore_db.sh --replace [ARQUIVO]    # SUBSTITUI o banco de produção (para a aplicação; pede confirmação)
# ARQUIVO: padrão = backup mais recente em ./backups. Pode ser baixado do remoto com:
#   rclone copy "$BACKUP_RCLONE_REMOTE/db_AAAAMMDD_HHMMSS.sql.gz.age" backups/
#
# Descriptografia: .age usa BACKUP_AGE_IDENTITY_FILE (chave PRIVADA age — mantenha fora do servidor e copie só
# para restaurar); .gpg usa o chaveiro GPG do usuário. Outras variáveis: COMPOSE, BACKUP_DIR, BACKUP_TEST_PING_URL
# (dead man's switch do teste periódico), EXPECTED_ALEMBIC_HEAD (padrão: head do repositório),
# RESTORE_CONFIRM=<nome do banco> (pula a pergunta do --replace; use com cuidado).
set -Eeuo pipefail
cd "$(dirname "$0")/.."

env_value() {
  [[ -f .env ]] || return 0
  grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" || true
}
cfg() { local v="${!1:-}"; [[ -n "$v" ]] && { printf '%s' "$v"; return; }; v="$(env_value "$1")"; printf '%s' "${v:-${2:-}}"; }
die() { printf '\033[1;31mERRO:\033[0m %s\n' "$*" >&2; ping_monitor /fail; exit 1; }
log() { printf '\n\033[1;34m>> %s\033[0m\n' "$*"; }

COMPOSE="${COMPOSE:-docker compose}"
BACKUP_DIR="$(cfg BACKUP_DIR backups)"
IDENTITY="$(cfg BACKUP_AGE_IDENTITY_FILE)"
PING_URL="$(cfg BACKUP_TEST_PING_URL)"
MODE="verify"
TARGET=""
FILE=""

ping_monitor() {
  [[ "$MODE" == "verify" && -n "$PING_URL" ]] || return 0
  curl -fsS -m 10 --retry 3 -o /dev/null "${PING_URL%/}$1" || true
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --verify) MODE="verify" ;;
    --to) MODE="to"; TARGET="${2:?--to exige o nome do banco}"; shift ;;
    --replace) MODE="replace" ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    -*) die "argumento desconhecido: $1" ;;
    *) FILE="$1" ;;
  esac
  shift
done

if [[ -z "$FILE" ]]; then
  # shellcheck disable=SC2012
  FILE="$({ ls -1t "$BACKUP_DIR"/db_*.sql.gz* 2>/dev/null || true; } | { grep -v '\.sha256$' || true; } | head -n1)"
  [[ -n "$FILE" ]] || die "nenhum backup em $BACKUP_DIR"
fi
[[ -s "$FILE" ]] || die "arquivo não encontrado ou vazio: $FILE"

if [[ -f "$FILE.sha256" ]]; then
  (cd "$(dirname "$FILE")" && sha256sum -c --quiet "$(basename "$FILE").sha256") || die "checksum não confere: $FILE"
  echo "Checksum OK: $FILE"
else
  echo "Aviso: $FILE.sha256 ausente (checksum não verificado)" >&2
fi

decrypt() {
  case "$FILE" in
    *.age)
      command -v age >/dev/null || die "age não instalado"
      [[ -n "$IDENTITY" && -f "$IDENTITY" ]] || die "defina BACKUP_AGE_IDENTITY_FILE (chave privada age) para restaurar $FILE"
      age -d -i "$IDENTITY" "$FILE" ;;
    *.gpg) gpg --batch --decrypt "$FILE" ;;
    *.sql.gz) cat "$FILE" ;;
    *) die "formato desconhecido: $FILE" ;;
  esac
}

# shellcheck disable=SC2016  # variáveis expandidas dentro do container
psql_admin() { $COMPOSE exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d postgres '"$*" </dev/null; }
# shellcheck disable=SC2016
psql_db() { local db="$1"; shift; $COMPOSE exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d '"$db $*"; }

# shellcheck disable=SC2016
main_db="$($COMPOSE exec -T postgres sh -c 'printf %s "$POSTGRES_DB"' </dev/null)"
[[ -n "$main_db" ]] || die "PostgreSQL não está em execução ($COMPOSE up -d postgres)"

case "$MODE" in
  verify) TARGET="restore_test_$(date -u +%Y%m%d%H%M%S)" ;;
  to) [[ "$TARGET" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || die "nome de banco inválido: $TARGET"
      [[ "$TARGET" != "$main_db" ]] || die "use --replace para restaurar sobre o banco principal" ;;
  replace) TARGET="${main_db}_restore_tmp" ;;
esac
[[ "$TARGET" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || die "nome de banco inválido: $TARGET"

ping_monitor /start
log "Restaurando $FILE no banco novo \"$TARGET\""
psql_admin "-c 'DROP DATABASE IF EXISTS \"$TARGET\"'" >/dev/null 2>&1
psql_admin "-c 'CREATE DATABASE \"$TARGET\"'" >/dev/null
cleanup_target() {
  if [[ "$MODE" == "verify" ]]; then psql_admin "-c 'DROP DATABASE IF EXISTS \"$TARGET\"'" >/dev/null 2>&1 || true; fi
}
trap cleanup_target EXIT
decrypt | gunzip -c | psql_db "$TARGET" >/dev/null

log "Conferência"
version="$(psql_db "$TARGET" "-At -c 'SELECT version_num FROM alembic_version'" </dev/null)"
expected="${EXPECTED_ALEMBIC_HEAD:-}"
if [[ -z "$expected" ]]; then
  # head = revisão que nenhuma outra aponta como down_revision
  expected="$(python3 - <<'PY' 2>/dev/null || true
import pathlib, re
revs, downs = set(), set()
for p in pathlib.Path("backend/alembic/versions").glob("*.py"):
    t = p.read_text()
    r = re.search(r'^revision\s*(?::\s*str)?\s*=\s*["\']([^"\']+)', t, re.M)
    d = re.search(r'^down_revision\s*(?::[^=]*)?=\s*["\']([^"\']+)', t, re.M)
    if r: revs.add(r.group(1))
    if d: downs.add(d.group(1))
heads = revs - downs
print(heads.pop() if len(heads) == 1 else "")
PY
)"
fi
echo "alembic_version: ${version:-?} (esperado: ${expected:-não verificado})"
[[ -n "$version" ]] || die "alembic_version vazio — backup inválido?"
if [[ -n "$expected" && "$version" != "$expected" ]]; then
  echo "Aviso: o backup está na revisão $version e o código em $expected (rode 'alembic upgrade head' após restaurar)." >&2
fi
for table in users workspaces accounts bot_configs orders trades positions; do
  n="$(psql_db "$TARGET" "-At -c 'SELECT count(*) FROM $table'" </dev/null 2>/dev/null || echo '?')"
  printf '  %-12s %s\n' "$table" "$n"
done
users="$(psql_db "$TARGET" "-At -c 'SELECT count(*) FROM users'" </dev/null)"
[[ "$users" =~ ^[0-9]+$ && "$users" -gt 0 ]] || die "nenhum usuário no backup restaurado"

case "$MODE" in
  verify)
    log "Teste de restauração OK (banco temporário $TARGET será removido)"
    ping_monitor "" ;;
  to)
    trap - EXIT
    log "Restaurado em \"$TARGET\". Para usá-lo, ajuste POSTGRES_DB/DATABASE_URL ou use --replace." ;;
  replace)
    trap - EXIT
    printf '\033[1;33mATENÇÃO:\033[0m o banco "%s" será SUBSTITUÍDO pelo backup. Os serviços serão parados.\n' "$main_db"
    ok="${RESTORE_CONFIRM:-}" # automação: RESTORE_CONFIRM=<nome do banco>
    [[ -n "$ok" ]] || read -r -p "Digite o nome do banco para confirmar: " ok || true
    [[ "$ok" == "$main_db" ]] || { psql_admin "-c 'DROP DATABASE IF EXISTS \"$TARGET\"'"; die "cancelado"; }
    log "Backup de segurança do estado atual"
    COMPOSE="$COMPOSE" ./scripts/backup_db.sh || die "backup pré-restauração falhou"
    log "Parando aplicação"
    $COMPOSE stop backend celery_worker celery_worker_trading celery_beat streams frontend caddy 2>/dev/null || true
    old="${main_db}_before_restore_$(date -u +%Y%m%d%H%M%S)"
    psql_admin "-c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('$main_db','$TARGET') AND pid <> pg_backend_pid()\"" >/dev/null
    psql_admin "-c 'ALTER DATABASE \"$main_db\" RENAME TO \"$old\"'"
    psql_admin "-c 'ALTER DATABASE \"$TARGET\" RENAME TO \"$main_db\"'"
    log "Migrações e subida"
    $COMPOSE run --rm migrate
    $COMPOSE up -d
    echo "Restauração concluída. Banco anterior preservado como \"$old\" (apague depois de validar:"
    echo "  $COMPOSE exec postgres psql -U \"\$POSTGRES_USER\" -d postgres -c 'DROP DATABASE \"$old\"')" ;;
esac
