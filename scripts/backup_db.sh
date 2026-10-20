#!/usr/bin/env bash
# Placeholder: backup do PostgreSQL para ./backups
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "backups/db_$(date +%Y%m%d_%H%M%S).sql.gz"
echo "Backup concluído."
