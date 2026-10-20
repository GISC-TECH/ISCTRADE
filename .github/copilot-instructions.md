# Instruções para o GitHub Copilot

Siga as regras de **[AGENTS.md](../AGENTS.md)** na raiz do repositório (stack, onde fica cada coisa, comandos e
regras obrigatórias). Resumo do essencial:

- Nunca inclua segredos no código; novas configs vão em `backend/app/core/config.py` + `.env.example` com placeholder.
- Execução **paper/testnet por padrão**; ordens reais exigem `LIVE_TRADING_ENABLED=true` e conta em `execution_mode=live`.
- Toda ordem passa por `OrderService` → `RiskManager` (`backend/app/services/`).
- Testes nunca chamam a Binance real (use `httpx.MockTransport`).
- Mudanças de modelo exigem migração Alembic nova em `backend/alembic/versions/`.
- Antes de abrir PR: `ruff check . && ruff format --check . && pytest -q` em `backend/`; `npm run lint && npx tsc --noEmit && npm run build` em `frontend/`.
- Documentação/UI em português do Brasil; código e commits (Conventional Commits) em inglês.

Guia completo com as etapas restantes: `docs/AGENT_GUIDE.md`.
