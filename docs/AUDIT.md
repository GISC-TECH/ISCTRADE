# Auditoria de segurança, dependências e execução — ISCTRADE

Data: 2026-10-21 · Escopo: backend (FastAPI/Celery), frontend (Next.js 14), integrações externas,
correção da execução de ordens e confiabilidade. Fora do escopo deste documento: desempenho/latência
(ver commits `perf(...)`) e o suporte a outras corretoras (ver `docs/EXCHANGES.md`).

Legenda de status: **Corrigido** (com testes, em `main`) · **Pendente** (documentado, não corrigido) ·
**Aceito** (risco conhecido e mitigado/assumido).

## 1. Resumo

| # | Severidade | Achado | Arquivo(s) | Status |
|---|---|---|---|---|
| 1 | Crítica | Stops/TPs de proteção (STOP_MARKET/TAKE_PROFIT_MARKET) enviados para `/fapi/v1/order`; desde 09/12/2025 a Binance exige a Algo Order API (`-4120`). Toda entrada live ficava **sem stop na corretora**. | `services/binance_client.py`, `services/binance_http.py` | Corrigido |
| 2 | Crítica | Se a colocação do stop falhava depois da entrada, a exceção era registrada como "rejeitada" mas a posição continuava aberta e desprotegida. Agora a entrada é desfeita (LIMIT cancelada / quantidade executada zerada com MARKET reduce-only), evento `order.protection_failed` (critical) e resultado com falha. Falha apenas do TP não derruba mais o trade. | `services/execution.py` | Corrigido |
| 3 | Alta | Kill switch `close_live_positions` abortava na primeira falha e só cancelava ordens de símbolos com posição; ordens abertas em outros símbolos continuavam vivas. Sem fechamento de posições, ordens de entrada não eram canceladas. | `services/kill_switch.py` | Corrigido |
| 4 | Alta | Ordem com resultado desconhecido (timeout, 5xx, `-1007`) virava "rejeitada" mesmo podendo estar viva na corretora (risco de duplicidade/posição sem controle). Agora a ordem é consultada pelo `clientOrderId`. | `services/binance_client.py` | Corrigido |
| 5 | Alta | IP do cliente lido de `X-Forwarded-For` sem validação: qualquer um contornava o rate limit de login forjando o cabeçalho. Agora só proxies em `TRUSTED_PROXIES` são considerados (hop mais à direita não confiável). | `core/rate_limiting.py`, `core/config.py` | Corrigido |
| 6 | Alta | Login sem bloqueio por e-mail (força bruta distribuída por IPs) e com diferença de tempo que revelava se o e-mail existia. Adicionados limite por e-mail (`LOGIN_FAILURES_PER_EMAIL`, janela de 15 min) e hash bcrypt fictício. | `api/v1/auth.py` | Corrigido |
| 7 | Alta | RBAC: qualquer membro do workspace podia afrouxar limites de risco da conta ou trocar testnet → mainnet. Agora exige admin do workspace. | `api/v1/accounts.py` | Corrigido |
| 8 | Alta | Dependências com CVEs conhecidas (backend) | `backend/requirements*.txt` / `pyproject` | Corrigido (pip-audit limpo) |
| 9 | Média | XSS por link `javascript:` em notícias (URL vinda de feeds/RSS renderizada em `href`). Validação no backend (`safe_url`) e no frontend (`safeHref`). | `news/sources.py`, `frontend/lib/utils.ts`, `frontend/app/dashboard/news/page.tsx` | Corrigido |
| 10 | Média | Ausência de CSP, Permissions-Policy e COOP no proxy. | `Caddyfile` | Corrigido |
| 11 | Média | Regra de margem do RiskManager ignorava a margem já usada pelas posições abertas. | `services/risk_manager.py`, `services/execution.py` | Corrigido |
| 12 | Média | `-1021` (relógio dessincronizado) não era tratado; `sync_time()` nunca era chamado. Agora ressincroniza e repete uma vez. GETs repetem em erro de rede/5xx (escritas nunca são repetidas às cegas). | `services/binance_client.py` | Corrigido |
| 13 | Média | Nomes de streams convertidos para minúsculas (Binance diferencia maiúsculas) e sockets "meio abertos" sem reconexão: o painel ficava sem dados. | `workers/streams.py`, `services/binance_ws.py` | Corrigido |
| 14 | Média | Tokens de autenticação/refresh/bearer podiam aparecer em logs. | `core/redaction.py` | Corrigido |
| 15 | Média | Next.js 14: CVEs remanescentes só corrigidos em 15.5.24+/16 (upgrade de versão maior). Mitigação atual: não usamos `next/image` com domínios remotos nem Server Actions; o app fica atrás do Caddy. | `frontend/package.json` | Pendente |
| 16 | Média | Refresh tokens não são de uso único (sem detecção de reutilização / rotação com revogação da família). | `api/v1/auth.py`, `core/security.py` | Pendente |
| 17 | Baixa | LIMIT usava `MARKET_LOT_SIZE` (máx. e passo) em vez de `LOT_SIZE`. | `services/symbol_filters.py`, `services/execution.py` | Corrigido |
| 18 | Baixa | Limite de WebSocket por usuário (4429) baixo e sem mensagem na UI → "Reconectando…" infinito. Padrão 20 e mensagem clara. | `core/config.py`, `frontend/lib/realtime.ts` | Corrigido |
| 19 | Baixa | Testes carregavam o `.env` local (resultados dependiam da máquina). `BTP_ENV_FILE` isola os testes. | `core/config.py`, `tests/conftest.py` | Corrigido |
| 20 | Baixa | Feeds RSS sem limite de tamanho de resposta; SSRF possível apenas via URLs configuradas por variável de ambiente (não por usuário). | `news/sources.py` | Pendente |
| 21 | Baixa | `get_realized_pnl` lê só as primeiras 1000 linhas de `income` (sem paginação). | `services/binance_client.py` | Pendente |
| 22 | Baixa | Hedge mode (posições LONG/SHORT simultâneas) não suportado; a conta deve estar em one-way mode. | execução | Pendente (documentado) |
| 23 | Baixa | bcrypt roda no event loop (bloqueia por ~200 ms por login). | `core/security.py` | Pendente (escopo de desempenho) |
| 24 | Info | Dependências transitivas do Tailwind com avisos do `npm audit` — apenas em tempo de build. | `frontend/package-lock.json` | Aceito |

## 2. Dependências e código suspeito

- `pip-audit` (backend) sem vulnerabilidades após as atualizações; FastAPI fixado em 0.158 (com ajuste de teste).
- `npm audit`: restam apenas os itens 15 e 24 acima.
- `package-lock.json` resolve **somente** de `registry.npmjs.org`; nenhum pacote de origem git/URL.
- Único `postinstall` relevante: `unrs-resolver` (dependência legítima de `eslint-config-next`, baixa binário nativo do próprio pacote npm).
- Nenhum código ofuscado, `eval` de conteúdo remoto, beacon/telemetria oculta ou envio de chaves para terceiros foi encontrado.
- Chaves de API da corretora são cifradas em repouso e nunca saem para hosts que não sejam a própria corretora.
- Ponto de atenção: o provedor padrão de WhatsApp é **WAGO (`wago-api.hablame.co`)**, um terceiro que recebe o texto dos alertas (símbolo, lado, preços, PnL). Se isso não for aceitável, use Meta Cloud API (`graph.facebook.com`) ou um gateway próprio (Evolution). Com `WHATSAPP_ENABLED=false` nada é enviado.

## 3. Hosts externos contatados

| Host | Uso | Quando |
|---|---|---|
| `fapi.binance.com`, `fstream.binance.com` | REST e WebSocket Binance USDT-M (mainnet) | contas mainnet |
| `testnet.binancefuture.com`, `stream.binancefuture.com`, `fstream.binancefuture.com` | REST e WebSocket testnet | contas testnet / dados de mercado em testnet |
| `api.bybit.com` / `api-testnet.bybit.com` | adaptador experimental Bybit | somente contas configuradas com `exchange=bybit` |
| `www.coindesk.com`, `cointelegraph.com`, `decrypt.co`, `www.theblock.co` | feeds RSS de notícias | worker de notícias |
| `cryptopanic.com` | agregador de notícias | se `CRYPTOPANIC_API_TOKEN` definido |
| `api.x.com` (X API v2) | posts do X | se `X_BEARER_TOKEN` definido |
| `api.openai.com` (ou `LLM_API_BASE`) | resumo/sentimento de notícias por LLM | se `LLM_API_KEY` definido |
| `nfs.faireconomy.media` | calendário econômico | worker de calendário |
| `wago-api.hablame.co` | WhatsApp (WAGO, padrão) | se `WHATSAPP_ENABLED=true` |
| `graph.facebook.com` | WhatsApp Cloud API (Meta) | se configurado |
| gateway Evolution / genérico (URL do usuário) | WhatsApp alternativo | se configurado |
| `registry.npmjs.org`, `pypi.org` | instalação de dependências | somente build |
| Let's Encrypt (ACME) | certificados TLS via Caddy | produção |

## 4. Recomendações pendentes (por prioridade)

1. Migrar o frontend para Next.js 15.5.24+ (ou 16) e reexecutar `npm audit`.
2. Refresh token de uso único com detecção de reutilização (revogar a família ao detectar reuso).
3. Limitar o tamanho das respostas RSS (ex.: 2 MB) e restringir esquemas/hosts das URLs de feed.
4. Paginar `income` em `get_realized_pnl`.
5. Mover bcrypt para thread (`anyio.to_thread`) e revisar a decisão sobre o provedor WAGO.
6. Validar no cadastro da conta que a Binance está em one-way mode (`GET /fapi/v1/positionSide/dual`).
