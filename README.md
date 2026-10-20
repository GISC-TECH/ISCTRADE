# Binance Trading Platform

> [!CAUTION]
> **AVISO DE RISCO — LEIA ANTES DE USAR**
>
> Operar contratos futuros perpétuos (USDT-M) com alavancagem envolve **risco altíssimo de perda total do capital**, inclusive em minutos. Este projeto é um **scaffold experimental**, fornecido "no estado em que se encontra" (licença MIT), **sem qualquer garantia** de lucro, desempenho ou ausência de bugs. Nada aqui constitui recomendação de investimento.
>
> - Comece **sempre** pelo backtest e pela **Testnet** da Binance (`BINANCE_USE_TESTNET=true`).
> - Nunca habilite permissão de **saque** nas chaves de API; restrinja por **IP**.
> - Em conta real, use apenas capital que você pode perder integralmente.
> - Você é o único responsável pelas ordens enviadas pela sua instância.

## Visão geral

Plataforma **self-hosted** para automação e monitoramento de operações em **Binance USDT-M Futures**. Cada usuário organiza suas contas Binance em **workspaces** isolados (multi-tenant), as chaves de API são armazenadas **criptografadas com AES-256-GCM**, e toda ordem passa por um **gerenciador de risco determinístico** (stop loss obrigatório, risco máximo por trade, limite de alavancagem, perda diária máxima e *kill switch* por drawdown) antes de chegar à corretora.

## Stack

| Camada | Tecnologia |
|---|---|
| Backend | Python 3.11, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2 |
| Tarefas assíncronas | Celery + Redis 7 |
| Banco de dados | PostgreSQL 15 |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind CSS, shadcn/ui, Recharts |
| Proxy / TLS | Caddy 2 (HTTPS automático) |
| Infra | Docker / Docker Compose |
| Corretora | Binance USDT-M Futures (REST `/fapi` + WebSocket) |

## Estrutura

```
backend/    API FastAPI, modelos, serviços (risco, cliente Binance, bots), tarefas Celery, migrações
frontend/   Next.js 14 (landing, auth, dashboard, admin)
docs/       ARCHITECTURE.md, SECURITY.md, ROADMAP.md
scripts/    scripts de deploy/backup
```

## Fases de desenvolvimento

1. **Backtest** — validar estratégias com dados históricos (klines) e custos realistas.
2. **Dry-run / paper trading** — rodar em tempo real na Testnet ou em modo simulado, sem capital real.
3. **Conta real mínima** — capital pequeno, alavancagem baixa, limites de risco conservadores.
4. **Monitoramento e logs** — alertas, auditoria, reconciliação, métricas e dashboards.
5. **Escala de capital** — aumento gradual, somente após métricas estáveis nas fases anteriores.

Detalhes e checklists em [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Como rodar localmente

### Pré-requisitos
- Docker e Docker Compose v2.24+
- (Opcional, para desenvolvimento sem Docker) Python 3.11 e Node.js 20

### Com Docker Compose

```bash
cp .env.example .env
# Edite .env e substitua TODOS os valores CHANGE_ME. Para gerar as chaves:
python -c "import secrets; print(secrets.token_urlsafe(64))"                 # JWT_SECRET
python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())" # MASTER_ENCRYPTION_KEY

docker compose up -d --build
```

- Frontend: http://localhost (via Caddy) ou http://localhost:3000
- API: http://localhost/api/v1 — docs interativas em http://localhost:8000/docs
- Health check: http://localhost:8000/health

### Backend sem Docker

```bash
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                         # testes (health, risco, segurança, cliente Binance)
uvicorn app.main:app --reload  # requer PostgreSQL e Redis acessíveis (ver .env)
```

Migrações:

```bash
alembic revision --autogenerate -m "initial tables"
alembic upgrade head
```

### Frontend sem Docker

```bash
cd frontend
npm install
npm run dev   # http://localhost:3000
```

### Produção

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
# ou
./scripts/deploy.sh
```

Defina `DOMAIN` e `ACME_EMAIL` no `.env` para que o Caddy emita certificados TLS automaticamente.

## Segurança

Veja [`docs/SECURITY.md`](docs/SECURITY.md). Resumo: nunca faça commit do `.env`; chaves Binance só com permissão de leitura + futuros (sem saque) e whitelist de IP; JWT em cookie `httpOnly`; credenciais criptografadas em repouso.

## Licença

[MIT](LICENSE) © 2026 E3 Advocacia
