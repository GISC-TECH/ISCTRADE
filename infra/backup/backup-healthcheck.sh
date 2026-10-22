#!/usr/bin/env bash
# Saudável se o último backup bem-sucedido tem menos de BACKUP_MAX_AGE_HOURS (padrão 26 h).
# Logo após subir (sem backup ainda) também é saudável, para não marcar unhealthy antes do 1º horário.
f="${BACKUP_DIR:-/backups}/.last_success"
max=$(( ${BACKUP_MAX_AGE_HOURS:-26} * 3600 ))
[[ -f "$f" ]] || { [[ $(( $(date +%s) - $(stat -c %Y /proc/1) )) -lt $max ]] && exit 0 || exit 1; }
age=$(( $(date +%s) - $(cat "$f") ))
[[ $age -lt $max ]] || { echo "último backup há $((age / 3600)) h" >&2; exit 1; }
