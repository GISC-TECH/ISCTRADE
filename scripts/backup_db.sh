#!/usr/bin/env bash
# Backup do PostgreSQL: pg_dump -> gzip -> criptografia (age ou gpg) -> ./backups (+ cópia off-site via rclone).
#
# Uso (na raiz do repositório, no host):   ./scripts/backup_db.sh
# Agendado: cron/systemd timer (infra/backup/) ou o serviço "backup" do compose (--profile backup).
#
# Configuração (variáveis de ambiente ou .env; nenhuma é obrigatória — sem criptografia o script avisa):
#   BACKUP_ENCRYPTION        age | gpg | none            (padrão: age se BACKUP_AGE_RECIPIENTS definido, senão none)
#   BACKUP_AGE_RECIPIENTS    chave(s) pública(s) age "age1..." separadas por vírgula (a chave PRIVADA fica fora do servidor)
#   BACKUP_GPG_RECIPIENT     ID/e-mail da chave pública GPG importada (modo gpg)
#   BACKUP_KEEP              quantos backups locais manter (padrão 14)
#   BACKUP_RETENTION_DAYS    apaga locais mais antigos que N dias (padrão 0 = só BACKUP_KEEP)
#   BACKUP_RCLONE_REMOTE     destino off-site rclone, ex. "s3:meu-bucket/isctrade" (S3/B2/R2/Wasabi/MinIO...)
#   BACKUP_REMOTE_RETENTION_DAYS  apaga no remoto arquivos mais antigos que N dias (padrão 30; 0 = nunca)
#   BACKUP_PING_URL          dead man's switch do backup (healthchecks.io): ping no sucesso, /fail na falha
#   BACKUP_DIR               diretório local (padrão ./backups)
#   COMPOSE                  comando compose (padrão "docker compose"); PGHOST definido => pg_dump direto (container)
set -Eeuo pipefail
cd "$(dirname "$0")/.."

env_value() { # lê KEY=valor do .env sem executar o arquivo
  [[ -f .env ]] || return 0
  grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" || true
}
cfg() { local v="${!1:-}"; [[ -n "$v" ]] && { printf '%s' "$v"; return; }; v="$(env_value "$1")"; printf '%s' "${v:-${2:-}}"; }

COMPOSE="${COMPOSE:-docker compose}"
BACKUP_DIR="$(cfg BACKUP_DIR backups)"
KEEP="$(cfg BACKUP_KEEP "${KEEP:-14}")"
RETENTION_DAYS="$(cfg BACKUP_RETENTION_DAYS 0)"
AGE_RECIPIENTS="$(cfg BACKUP_AGE_RECIPIENTS)"
GPG_RECIPIENT="$(cfg BACKUP_GPG_RECIPIENT)"
ENCRYPTION="$(cfg BACKUP_ENCRYPTION)"
[[ -n "$ENCRYPTION" ]] || { [[ -n "$AGE_RECIPIENTS" ]] && ENCRYPTION=age || ENCRYPTION=none; }
REMOTE="$(cfg BACKUP_RCLONE_REMOTE)"
REMOTE_RETENTION_DAYS="$(cfg BACKUP_REMOTE_RETENTION_DAYS 30)"
PING_URL="$(cfg BACKUP_PING_URL)"

ping_monitor() { # $1 = "" (sucesso) | "/fail" | "/start"
  [[ -n "$PING_URL" ]] || return 0
  if command -v curl >/dev/null; then curl -fsS -m 10 --retry 3 -o /dev/null "${PING_URL%/}$1" || true
  elif command -v wget >/dev/null; then wget -q -T 10 -O /dev/null "${PING_URL%/}$1" || true; fi
}
fail() { echo "ERRO backup: $*" >&2; ping_monitor /fail; exit 1; }
trap 'fail "falhou na linha $LINENO"' ERR

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
umask 077
ping_monitor /start

stamp="$(date -u +%Y%m%d_%H%M%S)"
base="$BACKUP_DIR/db_${stamp}.sql.gz"
case "$ENCRYPTION" in
  age)
    command -v age >/dev/null || fail "age não instalado (apt install age)"
    [[ -n "$AGE_RECIPIENTS" ]] || fail "BACKUP_AGE_RECIPIENTS vazio"
    file="$base.age"
    enc=(age)
    IFS=',' read -r -a recips <<<"$AGE_RECIPIENTS"
    for r in "${recips[@]}"; do enc+=(-r "$(echo "$r" | xargs)"); done
    ;;
  gpg)
    command -v gpg >/dev/null || fail "gpg não instalado"
    [[ -n "$GPG_RECIPIENT" ]] || fail "BACKUP_GPG_RECIPIENT vazio"
    file="$base.gpg"
    enc=(gpg --batch --yes --trust-model always --encrypt --recipient "$GPG_RECIPIENT" --output -)
    ;;
  none)
    file="$base"
    enc=(cat)
    echo "AVISO: backup SEM criptografia (defina BACKUP_AGE_RECIPIENTS; veja docs/RUNBOOK.md §Backups)." >&2
    ;;
  *) fail "BACKUP_ENCRYPTION inválido: $ENCRYPTION" ;;
esac

dump() {
  if [[ -n "${PGHOST:-}" ]]; then # dentro do container "backup": conecta direto no serviço postgres
    PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}" pg_dump -h "$PGHOST" -U "${PGUSER:-${POSTGRES_USER:-trading}}" \
      --no-owner --no-privileges "${PGDATABASE:-${POSTGRES_DB:-trading}}"
  else
    # shellcheck disable=SC2016  # expandido dentro do container
    $COMPOSE exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" --no-owner --no-privileges "$POSTGRES_DB"'
  fi
}

tmp="$file.partial"
dump | gzip -9 | "${enc[@]}" > "$tmp"
[[ -s "$tmp" ]] || { rm -f "$tmp"; fail "backup vazio"; }
mv "$tmp" "$file"
(cd "$BACKUP_DIR" && sha256sum "$(basename "$file")" > "$(basename "$file").sha256")
size="$(du -h "$file" | cut -f1)"
echo "Backup local: $file ($size, criptografia: $ENCRYPTION)"

if [[ -n "$REMOTE" ]]; then
  command -v rclone >/dev/null || fail "rclone não instalado (BACKUP_RCLONE_REMOTE definido)"
  rclone copy --no-traverse "$file" "$REMOTE/"
  rclone copy --no-traverse "$file.sha256" "$REMOTE/"
  echo "Cópia off-site: $REMOTE/$(basename "$file")"
  if [[ "$REMOTE_RETENTION_DAYS" =~ ^[0-9]+$ ]] && (( REMOTE_RETENTION_DAYS > 0 )); then
    rclone delete --min-age "${REMOTE_RETENTION_DAYS}d" --include 'db_*' "$REMOTE/" || echo "Aviso: retenção remota falhou" >&2
  fi
fi

# Retenção local: últimos $KEEP e, opcionalmente, nada mais velho que $RETENTION_DAYS dias.
# shellcheck disable=SC2012  # nomes gerados por este script
{ ls -1t "$BACKUP_DIR"/db_*.sql.gz* 2>/dev/null || true; } | { grep -v '\.sha256$' || true; } \
  | tail -n +"$((KEEP + 1))" | while read -r old; do rm -f "$old" "$old.sha256"; done
if [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] && (( RETENTION_DAYS > 0 )); then
  find "$BACKUP_DIR" -maxdepth 1 -name 'db_*' -mtime "+$RETENTION_DAYS" -delete
fi

date -u +%s > "$BACKUP_DIR/.last_success"
trap - ERR
ping_monitor ""
echo "Backup concluído: $file"
