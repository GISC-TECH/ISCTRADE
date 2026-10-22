#!/bin/sh
# redis-cli autenticado dentro do container: docker compose exec redis /opt/isctrade/cli.sh -n 1 llen trading
set -eu
if [ -n "${REDIS_PASSWORD:-}" ]; then
  export REDISCLI_AUTH="$REDIS_PASSWORD"
  [ -n "${REDIS_USERNAME:-}" ] && exec redis-cli --no-auth-warning --user "$REDIS_USERNAME" "$@"
fi
exec redis-cli --no-auth-warning "$@"
