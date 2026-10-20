# CLAUDE.md

As instruções deste repositório para agentes estão em **[AGENTS.md](AGENTS.md)** — leia-o inteiro antes de
qualquer alteração e siga as regras obrigatórias (segredos, paper/testnet por padrão, RiskManager obrigatório,
testes com Binance mockada, migrações Alembic).

Guia de etapas e prompts: `docs/AGENT_GUIDE.md`.

Antes de concluir qualquer tarefa, rode e garanta que passam:
- `cd backend && ruff check . && ruff format --check . && pytest -q`
- `cd frontend && npm run lint && npx tsc --noEmit && npm run build` (se tocou no frontend)
