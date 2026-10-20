#!/usr/bin/env bash
# Deploy placeholder: atualiza o código e sobe a stack de produção.
# Uso: ./scripts/deploy.sh   (executar no servidor, na raiz do repositório)
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "ERRO: arquivo .env não encontrado. Copie .env.example e preencha os valores." >&2
  exit 1
fi

if grep -q "CHANGE_ME" .env; then
  echo "ERRO: .env ainda contém valores CHANGE_ME." >&2
  exit 1
fi

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

echo ">> Atualizando código"
git pull --ff-only

echo ">> Build das imagens"
$COMPOSE build

echo ">> Subindo serviços"
$COMPOSE up -d

echo ">> Status"
$COMPOSE ps

# TODO: backup do PostgreSQL antes de migrações, health checks pós-deploy, rollback.
