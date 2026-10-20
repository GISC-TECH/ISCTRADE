#!/usr/bin/env bash
# Backup do PostgreSQL para ./backups (gzip). Mantém os últimos $KEEP arquivos (padrão 14).
# Uso: ./scripts/backup_db.sh
# Restaurar: gunzip -c backups/db_X.sql.gz | docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="${COMPOSE:-docker compose}"
KEEP="${KEEP:-14}"
mkdir -p backups
chmod 700 backups
umask 077
file="backups/db_$(date -u +%Y%m%d_%H%M%S).sql.gz"
# shellcheck disable=SC2016  # expandido dentro do container
$COMPOSE exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" --no-owner "$POSTGRES_DB"' | gzip > "$file"
[[ -s "$file" ]] || { echo "Backup vazio: $file" >&2; rm -f "$file"; exit 1; }
# shellcheck disable=SC2012  # nomes gerados por este script
ls -1t backups/db_*.sql.gz | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "Backup concluído: $file"
