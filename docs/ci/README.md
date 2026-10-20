# Workflow de CI (ativação pendente)

`ci.yml` é o workflow do GitHub Actions do projeto (lint + testes + build). Ele está aqui, e não em
`.github/workflows/`, porque o token usado no push automatizado não tinha o escopo `workflow`, exigido
pelo GitHub para criar arquivos de workflow.

Para ativar, um mantenedor com permissão deve executar:

```bash
mkdir -p .github/workflows
git mv docs/ci/ci.yml .github/workflows/ci.yml
git commit -m "ci: enable GitHub Actions workflow"
git push origin main
```

Ou criar o arquivo pela interface web do GitHub (Add file → Create new file → `.github/workflows/ci.yml`)
colando o conteúdo de `docs/ci/ci.yml`.

O workflow executa: `ruff check`, `ruff format --check`, `pytest` (com teste de migração em PostgreSQL 15),
`next lint`, `tsc --noEmit`, `next build`, `docker compose config` (dev e prod), `shellcheck` e build das imagens Docker.
