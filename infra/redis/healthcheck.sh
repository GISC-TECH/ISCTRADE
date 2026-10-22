#!/bin/sh
# Healthcheck do Redis: PING autenticado (a senha vai por REDISCLI_AUTH, não aparece na linha de comando).
set -eu
if [ -n "${REDIS_PASSWORD:-}" ]; then
  export REDISCLI_AUTH="$REDIS_PASSWORD"
  if [ -n "${REDIS_USERNAME:-}" ]; then
    exec redis-cli --no-auth-warning --user "$REDIS_USERNAME" ping
  fi
fi
exec redis-cli --no-auth-warning ping
