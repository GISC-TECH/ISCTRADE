#!/usr/bin/env bash
# Executa /repo/scripts/backup_db.sh todo dia em BACKUP_TIME_UTC (HH:MM, padrão 03:00) e, se
# BACKUP_INTERVAL_HOURS (>0) estiver definido, a cada N horas. Falhas são registradas e pingadas (/fail).
set -uo pipefail
BACKUP_TIME_UTC="${BACKUP_TIME_UTC:-03:00}"
INTERVAL_H="${BACKUP_INTERVAL_HOURS:-0}"
export BACKUP_DIR="${BACKUP_DIR:-/backups}"
[[ -n "${BACKUP_RCLONE_CONFIG_FILE:-}" ]] && export RCLONE_CONFIG="$BACKUP_RCLONE_CONFIG_FILE"
[[ -f /secrets/rclone.conf && -z "${RCLONE_CONFIG:-}" ]] && export RCLONE_CONFIG=/secrets/rclone.conf
echo "backup: agendado diariamente às ${BACKUP_TIME_UTC} UTC${INTERVAL_H:+ (intervalo ${INTERVAL_H}h)}; destino ${BACKUP_DIR}"
run() { /repo/scripts/backup_db.sh || echo "backup: FALHOU ($(date -u +%FT%TZ))" >&2; }
[[ "${BACKUP_ON_START:-false}" == "true" ]] && run
last_day=""
last_run=$(date +%s)
while sleep 30; do
  now=$(date -u +%H:%M); today=$(date -u +%F)
  if [[ "$now" == "$BACKUP_TIME_UTC" && "$today" != "$last_day" ]]; then
    last_day="$today"; run; last_run=$(date +%s)
  elif [[ "$INTERVAL_H" =~ ^[0-9]+$ ]] && (( INTERVAL_H > 0 )) && (( $(date +%s) - last_run >= INTERVAL_H * 3600 )); then
    run; last_run=$(date +%s)
  fi
done
