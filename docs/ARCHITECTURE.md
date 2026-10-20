# Arquitetura

## Diagrama

```
                       ┌──────────────────────────────┐
     Navegador ──────▶ │        Caddy (HTTPS/TLS)      │
                       └──────────────┬───────────────┘
                     /*               │              /api/*, /health
              ┌───────────────────────┴────────────────────────┐
              ▼                                                ▼
   ┌─────────────────────┐   HTTP (cookie httpOnly)   ┌─────────────────────┐
   │ Next.js 14 (front)  │ ─────────────────────────▶ │  FastAPI (backend)  │
   │ shadcn/ui, Recharts │                            │  /api/v1, RBAC, JWT │
   └─────────────────────┘                            └──────────┬──────────┘
                                                                 │
                         ┌──────────────────────┬────────────────┼──────────────────┐
                         ▼                      ▼                ▼                  │
              ┌────────────────────┐  ┌──────────────────┐  ┌──────────────┐        │
              │  Celery workers    │◀▶│     Redis 7      │  │ PostgreSQL 15│◀───────┘
              │ bots, reconciler,  │  │ broker, cache,   │  │ usuários,    │
              │ equity snapshots   │  │ rate limit, locks│  │ workspaces,  │
              └─────────┬──────────┘  └──────────────────┘  │ contas, logs │
                        │                                   └──────────────┘
                        │  RiskManager.validate_order() ── toda ordem passa aqui
                        ▼
          ┌──────────────────────────────────────────────┐
          │        Binance USDT-M Futures                │
          │  REST  https://fapi.binance.com  (/fapi/*)   │
          │  WS    wss://fstream.binance.com (mercado,   │
          │        user data stream)                     │
          │  (Testnet: testnet.binancefuture.com)        │
          └──────────────────────────────────────────────┘
```

## Componentes

| Componente | Responsabilidade |
|---|---|
| **Caddy** | Terminação TLS, headers de segurança, roteamento `/api` → backend e `/` → frontend. |
| **Next.js** | UI: landing, autenticação, dashboard (saldo, PnL, posições, curva de patrimônio), admin. |
| **FastAPI** | Autenticação (JWT em cookie httpOnly), RBAC, workspaces (tenant), contas Binance, histórico. |
| **Celery** | Execução dos bots (loop de estratégia), reconciliação de ordens/posições, snapshots de equity. |
| **Redis** | Broker do Celery, cache de saldos/posições, rate limiting, locks distribuídos por conta. |
| **PostgreSQL** | Persistência (usuários, workspaces, contas com chaves criptografadas, ordens, auditoria). |
| **Binance** | REST assinado (HMAC-SHA256) para ordens/conta; WebSocket para mercado e user data stream. |

## Fluxo de uma ordem

1. Estratégia (`app/strategy`) gera uma *intenção* de ordem.
2. `RiskManager.validate_order(order, account)` aplica as regras determinísticas; se reprovar, registra auditoria e (no caso de drawdown) aciona o *kill switch*, mudando a conta para `killed`.
3. `BinanceFuturesClient.place_order` envia a ordem (com stop loss) à Binance.
4. User data stream (WebSocket) + `Reconciler` mantêm o estado local consistente com a corretora.
5. `equity` grava snapshots usados no dashboard e nas regras de perda diária/drawdown.

## Multi-tenancy

- `User` ⟶ possui/participa de `Workspace` (via `WorkspaceMember`, com papel).
- `Workspace` ⟶ possui `Account`s. Toda consulta de conta é filtrada por `workspace_id` do workspace ativo (header `X-Workspace-ID`, validado contra a associação do usuário).
- As credenciais são criptografadas com AES-256-GCM usando o `workspace_id` como *associated data*, impedindo reaproveitamento do ciphertext entre tenants.
