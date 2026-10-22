#!/bin/sh
# Redis do ISCTRADE com autenticação (GAPS G-27).
#   REDIS_PASSWORD vazio  -> sem senha (apenas desenvolvimento; deploy.sh e o backend em produção recusam)
#   REDIS_USERNAME vazio  -> senha no usuário "default"
#   REDIS_USERNAME=nome   -> usuário ACL "nome" e o "default" desativado
# O arquivo ACL guarda só o SHA-256 da senha, em tmpfs do container (recriado a cada start).
# Comandos administrativos (CONFIG, DEBUG, SHUTDOWN, ACL SETUSER, REPLICAOF, MONITOR...) e FLUSHALL/FLUSHDB ficam
# bloqueados para a aplicação: um vazamento da senha não dá controle do servidor nem apaga tudo de uma vez.
set -eu

if [ -z "${REDIS_PASSWORD:-}" ]; then
  echo "AVISO: Redis sem senha (REDIS_PASSWORD vazio) — use só em desenvolvimento" >&2
  exec redis-server --appendonly yes "$@"
fi

user="${REDIS_USERNAME:-default}"
case "$user" in
  *[!A-Za-z0-9_.-]*) echo "REDIS_USERNAME inválido: use letras, números, _ . -" >&2; exit 1 ;;
esac
hash=$(printf '%s' "$REDIS_PASSWORD" | sha256sum | cut -d' ' -f1)
rules="on #$hash ~* &* +@all -@admin -flushall -flushdb"

acl=/tmp/redis-users.acl
umask 077
if [ "$user" = "default" ]; then
  echo "user default $rules" > "$acl"
else
  {
    # "off" impede autenticar como default; as permissões continuam +@all porque o cliente interno que recarrega o
    # AOF na inicialização usa as permissões do default (com -@all o replay de MULTI/EXEC falha com NOPERM).
    echo "user default off resetpass ~* &* +@all"
    echo "user $user $rules"
  } > "$acl"
fi
exec redis-server --appendonly yes --aclfile "$acl" "$@"
