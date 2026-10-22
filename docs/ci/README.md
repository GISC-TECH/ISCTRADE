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

Depois de ativar, marque os jobs como *required* na proteção da branch `main` (Settings → Branches) para que
nada quebrado chegue ao `deploy.sh` (que faz `git pull`).

## O que o workflow executa

| Job | Passos |
|---|---|
| `backend` | `ruff check`, `ruff format --check`, `pytest --cov` (inclui teste de migração em PostgreSQL 15 de serviço; falha abaixo do limiar `fail_under` de `backend/.coveragerc`, hoje 84% de linhas+ramos para 85,8% medidos), `pip-audit -r requirements.txt` |
| `frontend` | `npm ci`, `eslint`, `tsc --noEmit`, `npm test` (Vitest + Testing Library em `frontend/tests/unit`), `next build`, `npm audit --omit=dev --audit-level=critical` (subir para `high` após a migração para Tailwind 4 — GAPS G-25) |
| `e2e` | PostgreSQL + Redis de serviço, `alembic upgrade head`, uvicorn em segundo plano, `next build` + `next start` e Playwright (`frontend/e2e/smoke.spec.ts`: cadastro com login automático → aceite do aviso → ativação do 2FA → login em duas etapas → dashboard → conta paper → Tempo real; e a página 404), em desktop e mobile. Relatório HTML como artefato em caso de falha |
| `compose` | `docker compose config` (dev, prod e prod com perfis `monitoring` + `backup`), `shellcheck scripts/*.sh infra/backup/*.sh infra/redis/*.sh` |
| `secrets` | `gitleaks` no histórico inteiro (`fetch-depth: 0`) com as regras padrão + allowlist de falsos positivos em `.gitleaks.toml` (imagem oficial; a `gitleaks-action` exige licença para organizações) |
| `monitoring` | `promtool check rules` + `promtool test rules` (testes em `infra/monitoring/tests/`), `amtool check-config` |
| `images` | build das imagens `backend`, `frontend` e `backup` + *smoke test* de import (`app.main`, celery, beat, streams, `alembic heads`, `app.healthcheck`) |

## Rodar localmente (antes de cada push)

```bash
# backend
cd backend && ruff check . && ruff format --check . && pytest -q --cov && pip-audit -r requirements.txt && cd ..
# frontend
cd frontend && npm ci && npm run lint && npx tsc --noEmit && npm run build && npm audit --omit=dev --audit-level=critical && cd ..
# compose + scripts
cp -n .env.example .env
docker compose config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile monitoring --profile backup config -q
shellcheck scripts/*.sh infra/backup/*.sh infra/redis/*.sh
# segredos no histórico
docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.21.2 git /repo --config /repo/.gitleaks.toml --redact --no-banner --exit-code 1
# regras de alerta
docker run --rm --entrypoint promtool -v "$PWD/infra/monitoring:/m" prom/prometheus:v2.54.1 check rules /m/rules/isctrade.yml
docker run --rm --entrypoint promtool -v "$PWD/infra/monitoring:/m" -w /m/tests prom/prometheus:v2.54.1 test rules rules_test.yml
docker run --rm --entrypoint amtool -v "$PWD/infra/monitoring:/m" prom/alertmanager:v0.28.1 check-config /m/alertmanager.yml
```

Também é possível rodar o workflow inteiro com [`act`](https://github.com/nektos/act) depois de copiá-lo para
`.github/workflows/`.

Falso positivo do gitleaks: acrescente uma regex **específica** ao `[allowlist]` de `.gitleaks.toml` com um comentário
dizendo de onde vem. Segredo verdadeiro: revogue-o primeiro (docs/RUNBOOK.md §4.12) e só depois limpe o histórico.
