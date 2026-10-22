# Auditoria de segurança, dependências e execução — ISCTRADE

Data: 2026-10-21 (atualizado no mesmo dia: itens 15, 16, 20–23 corrigidos) · Escopo: backend (FastAPI/Celery), frontend (Next.js 16), integrações externas,
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
| 6 | Alta | Login sem bloqueio por e-mail (força bruta distribuída por IPs) e com diferença de tempo que revelava se o e-mail existia. Adicionados limite por e-mail (`LOGIN_FAILURES_PER_EMAIL`, janela de 15 min) e hash bcrypt fictício. | `api/v1/endpoints/auth.py` | Corrigido |
| 7 | Alta | RBAC: qualquer membro do workspace podia afrouxar limites de risco da conta ou trocar testnet → mainnet. Agora exige admin do workspace. | `api/v1/endpoints/accounts.py` | Corrigido |
| 8 | Alta | Dependências com CVEs conhecidas (backend) | `backend/requirements*.txt` / `pyproject` | Corrigido (pip-audit limpo; reaberto em 2026-10 por PYSEC-2026-107 no `orjson` 3.11.5 e corrigido com `orjson` 3.11.9 — GAPS G-24) |
| 9 | Média | XSS por link `javascript:` em notícias (URL vinda de feeds/RSS renderizada em `href`). Validação no backend (`safe_url`) e no frontend (`safeHref`). | `news/sources.py`, `frontend/lib/utils.ts`, `frontend/app/dashboard/news/page.tsx` | Corrigido |
| 10 | Média | Ausência de CSP, Permissions-Policy e COOP no proxy. | `Caddyfile` | Corrigido |
| 11 | Média | Regra de margem do RiskManager ignorava a margem já usada pelas posições abertas. | `services/risk_manager.py`, `services/execution.py` | Corrigido |
| 12 | Média | `-1021` (relógio dessincronizado) não era tratado; `sync_time()` nunca era chamado. Agora ressincroniza e repete uma vez. GETs repetem em erro de rede/5xx (escritas nunca são repetidas às cegas). | `services/binance_client.py` | Corrigido |
| 13 | Média | Nomes de streams convertidos para minúsculas (Binance diferencia maiúsculas) e sockets "meio abertos" sem reconexão: o painel ficava sem dados. | `workers/streams.py`, `services/binance_ws.py` | Corrigido |
| 14 | Média | Tokens de autenticação/refresh/bearer podiam aparecer em logs. | `core/redaction.py` | Corrigido |
| 15 | Média | Next.js 14 com CVEs só corrigidos em 15.5.24+/16. Atualizado para **Next.js 16.4.2 + React 19** (ESLint 9 flat config, `middleware.ts` → `proxy.ts`); `npm audit --omit=dev` sem achados no Next/React. | `frontend/package.json`, `frontend/eslint.config.mjs`, `frontend/proxy.ts` | Corrigido |
| 16 | Média | Refresh tokens não eram de uso único. Agora cada token é registrado (`refresh_tokens`, migração 0008) por família de login; é trocado uma única vez e a reapresentação de um token já usado (fora de uma janela de 10 s para abas concorrentes, `REFRESH_REUSE_GRACE_SECONDS`) **revoga a família inteira** e gera `auth.refresh_reuse_detected` (critical). Logout revoga a família no servidor. | `api/v1/endpoints/auth.py`, `models/refresh_token.py` | Corrigido |
| 17 | Baixa | LIMIT usava `MARKET_LOT_SIZE` (máx. e passo) em vez de `LOT_SIZE`. | `services/symbol_filters.py`, `services/execution.py` | Corrigido |
| 18 | Baixa | Limite de WebSocket por usuário (4429) baixo e sem mensagem na UI → "Reconectando…" infinito. Padrão 20 e mensagem clara. | `core/config.py`, `frontend/lib/realtime.ts` | Corrigido |
| 19 | Baixa | Testes carregavam o `.env` local (resultados dependiam da máquina). `BTP_ENV_FILE` isola os testes. | `core/config.py`, `tests/conftest.py` | Corrigido |
| 20 | Baixa | Buscas externas (RSS, CryptoPanic, X, calendário, LLM) sem limite de tamanho nem prazo total (timeouts do httpx são por leitura: servidor "gota a gota" nunca expirava; gzip bomb descompactava sem limite). `fetch_limited`: corpo decodificado ≤ `NEWS_MAX_RESPONSE_BYTES` (5 MB) / `LLM_MAX_RESPONSE_BYTES` (1 MB) e prazo `NEWS_FETCH_DEADLINE_SECONDS` (30 s). SSRF continua possível apenas via URLs definidas por variável de ambiente (não por usuário). | `core/http_limits.py`, `news/*.py` | Corrigido |
| 21 | Baixa | `get_realized_pnl` lia só as primeiras 1000 linhas de `income`. Agora pagina (até 100 páginas) recomeçando no timestamp da última linha e deduplicando por `tranId`. | `services/binance_client.py` | Corrigido |
| 22 | Baixa | Hedge mode (posições LONG/SHORT simultâneas) não é suportado (ordens sem `positionSide`, posição líquida por símbolo). Decisão: **detectar e recusar** — ordens manuais live retornam 409 com instrução para mudar para one-way mode, bots falham ao iniciar com a mesma mensagem, e `POST /accounts/{id}/verify` retorna `hedge_mode=true`/`ok=false`. | `services/position_mode.py`, `services/manual_trading.py`, `services/jobs.py`, `api/v1/endpoints/accounts.py` | Corrigido (recusado com erro claro) |
| 23 | Baixa | bcrypt rodava no event loop (~200 ms bloqueados por login/cadastro/troca de senha, congelando WebSockets e outras requisições). Agora em thread (`anyio.to_thread`). | `core/security.py`, `api/v1/endpoints/auth.py` | Corrigido |
| 24 | Info | Dependências transitivas do Tailwind 3 (`braces`, `micromatch`, `postcss-selector-parser`) com avisos do `npm audit` — apenas em tempo de build, sem correção upstream de `braces`; resolver exige migrar para Tailwind 4. | `frontend/package-lock.json` | Aceito |

## 2. Dependências e código suspeito

- `pip-audit -r backend/requirements.txt` (2026-10-22): **sem vulnerabilidades conhecidas** após `orjson` 3.11.5 → 3.11.9
  (PYSEC-2026-107, corrigido em 3.11.6). FastAPI fixado em 0.158 (com ajuste de teste). O CI (`docs/ci/ci.yml`) roda
  `pip-audit` a cada push.
- `npm audit` (2026-10-22): 9 achados (7 high, 2 moderate), **todos** na cadeia do Tailwind 3 (`braces`, `micromatch`,
  `chokidar`, `fast-glob`, `postcss-selector-parser`, `postcss-nested`) — item 24; `npm audit --omit=dev` mostra 7
  porque `tailwindcss-animate` declara o Tailwind como *peer*. São usados só no `next build` (o runtime *standalone*
  não os inclui); `braces` não tem versão corrigida, a saída é migrar para o Tailwind 4 (GAPS G-25). Next.js 16.4.2 e
  React 19 sem achados. O CI roda `npm audit --omit=dev --audit-level=critical` (subir para `high` após G-25).
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

1. Migrar Tailwind 3 → 4 para eliminar os avisos de build do item 24.
2. Revisar o uso do provedor WAGO para WhatsApp (terceiro recebe o texto dos alertas) — preferir Meta Cloud API ou gateway próprio.
3. Se houver demanda, suportar hedge mode de fato (`positionSide` em ordens, posições por lado no reconciliador).
4. Opcional: restringir hosts das URLs de feed (lista permitida) para eliminar o SSRF residual via configuração.

## 5. Desempenho (latência e throughput)

Auditoria de velocidade de ponta a ponta: sinal → RiskManager → ordem, banco, Celery, streams/WebSocket e
frontend. Todas as medições foram feitas na mesma máquina (Postgres 17 e Redis locais, Python 3.12), com a
Binance simulada por `httpx.MockTransport` com RTT de 25 ms (nenhuma conta real é tocada). "Antes" é o commit
`b997207` (benchmark adicionado, nenhuma otimização); "depois" é o `main` atual.

Como reproduzir (em `backend/`, com um banco descartável):

```bash
BENCH_DATABASE_URL=postgresql+asyncpg://u:p@localhost:5432/scratch python -m benchmarks.perf_bench
BENCH_DATABASE_URL=postgresql+asyncpg://u:p@localhost:5432/scratch2 python -m benchmarks.index_bench
python -m benchmarks.perf_bench http   # só GET /fapi/v1/ping público
```

### 5.1 Resultados (medianas)

| Cenário | Antes | Depois | Ganho |
|---|---:|---:|---|
| Ordem live (entrada + SL + TP, RTT 25 ms) | 86,0 ms | 59,7 ms | −31 % (SL e TP em paralelo) |
| Tick de bot paper com 1 execução | 11,3 ms / 19 queries | 7,9 ms / 13 queries | −30 %, −6 queries |
| Overhead por task Celery (`run_async`: SELECT 1 + GET Redis) | 25,9 ms | 0,78 ms | 33× |
| Fan-out WS: 50 sessões × mensagem de 600 tickers (45 KB) | 20,5 ms | 0,40 ms | 51× (MarketHub + orjson) |
| Publicação de evento após commit (`_publish_sync`) | 0,22 ms | 0,04 ms | 5× |
| Chamada HTTP Binance (ping real, cliente novo × pool keep-alive) | 69,5 ms | 11,3 ms | handshake TLS eliminado |
| `GET /accounts/{id}/dashboard` (paper) | 5,2 ms | 5,4 ms | igual (ganho é no modo live: chamadas em paralelo, sem `positionRisk` extra) |
| Endpoints de histórico/contas | 3,5–5,5 ms | 3,3–5,6 ms | igual (dados pequenos; ver índices abaixo) |

Índices (migração `0007`, 400 mil ordens/trades e 200 mil linhas de auditoria, `EXPLAIN ANALYZE`):

| Consulta | 0006 | 0007 |
|---|---:|---:|
| Histórico de ordens (workspace, 50 mais recentes) | 11,66 ms | 0,023 ms |
| Histórico de trades (workspace, 50 mais recentes) | 10,29 ms | 0,034 ms |
| Ordens LIMIT abertas do paper (conta) | 4,82 ms | 0,40 ms |
| Deduplicação de fills do reconciliador (conta, trade id) | 4,33 ms | 0,008 ms |
| Exclusão de bot: `trades.bot_id` → NULL | 17,2 ms | 2,05 ms |
| Contagem do histórico de ordens | 9,7 ms | 10,0 ms (seq scan agregado; ver limitações) |
| Exclusão de conta: `audit_logs.account_id` → NULL | 57,8 ms | 60,4 ms (dominado pela escrita das linhas) |

Frontend (`npm run build`, First Load JS):

| Rota | Antes | Depois |
|---|---:|---:|
| `/dashboard` | 214 kB | 114 kB |
| `/dashboard/strategies` | 219 kB | 119 kB |
| `/dashboard/live` | 184 kB | 128 kB |

Re-renderizações (React 18 + jsdom, 400 mensagens `tickers`/`mark` de 8 símbolos, barra de cotações + página
ao vivo): o corpo da página ao vivo (gráfico, tabelas de ordens/eventos, bots) passou de 400 renderizações para
0; commits da barra de cotações de 400 para 183; tempo de render total de 113 ms para 45 ms. O EMA do gráfico
passou de recalcular e reenviar 950 pontos por kline (27 µs de cálculo + `setData` com redesenho completo)
para 2 pontos via `series.update` (2 µs).

### 5.2 Gargalos encontrados e correções

1. **Cliente HTTP novo por instância do `BinanceClient`** (handshake TCP+TLS a cada tick/ordem). Agora há um
   `httpx.AsyncClient` compartilhado por event loop (`app/services/binance_http.py`), com keep-alive
   (64 conexões, 32 ociosas, 60 s), timeouts explícitos (10 s; conexão 3 s; pool 5 s) e fechamento no shutdown.
2. **Sem controle de peso da Binance.** `X-MBX-USED-WEIGHT-1M` é lido em toda resposta e exportado na métrica
   `btp_binance_used_weight_1m`; acima de `BINANCE_WEIGHT_SOFT_PCT` (90 %) de `BINANCE_WEIGHT_LIMIT_1M` (2400)
   chamadas não críticas esperam o próximo minuto (máx. `BINANCE_WEIGHT_MAX_WAIT_SECONDS`); 429/418 abrem uma
   janela de bloqueio pelo `Retry-After`, e novas chamadas falham na hora em vez de agravar o ban. Ordens,
   cancelamentos e listenKey nunca são atrasadas pelo limite suave.
3. **`exchangeInfo` baixado repetidamente** (peso 1 a cada uso, ~1 MB). Cache de 15 min compartilhado entre
   scanner e filtros de símbolo.
4. **Celery com `asyncio.run` + `NullPool` + conexão Redis nova por task** (~26 ms de overhead fixo). Agora
   cada processo do worker mantém um event loop persistente e um engine com pool pequeno
   (`CELERY_DB_POOL_SIZE=2`), encerrados em `worker_process_shutdown` (`app/tasks/runtime.py`).
5. **Fila única com concorrência 2** compartilhada entre ticks de bots e tarefas lentas (notícias/LLM,
   scanner, calendário), além de resultados gravados no Redis sem uso. Agora há as filas `trading`
   (bots, paper, contas, risco) e `default`, com exchanges próprias, `task_ignore_result=True`, `expires` nos
   ticks (ticks atrasados não se acumulam) e limites de tempo em `bots.tick`. O `docker-compose` ganhou o
   serviço `celery_worker_trading` (`-Q trading --concurrency=4`), com healthcheck, alvo no Prometheus e
   inclusão no `scripts/deploy.sh`.
6. **SL e TP enviados em série** após a entrada: agora em paralelo (`asyncio.gather`), mantendo a regra de
   nunca deixar a entrada sem stop.
7. **Dashboard live com chamadas em série** e `positionRisk` redundante: `account` e `income` em paralelo;
   `positionRisk` só quando a conta não traz posições.
8. **Mesmas posições consultadas várias vezes por tick** (N+1): cache por sessão no `get_position`
   (19 → 13 queries por tick com execução).
9. **Índices faltando** para histórico por workspace, ordens abertas por conta, deduplicação de fills e FKs
   (`trades.bot_id`, `audit_logs.account_id`, `alerts.account_id`): migração `0007`, criada com
   `CREATE INDEX CONCURRENTLY IF NOT EXISTS` no Postgres (sem bloquear escrita), removendo índices simples
   redundantes.
10. **Cada sessão WebSocket decodificava cada mensagem de mercado (45 KB)** e o Redis copiava a mensagem para
    cada sessão. Agora há uma assinatura por processo (`MarketHub`): decodifica uma vez, indexa por símbolo e
    entrega a cada sessão apenas os símbolos observados. `orjson` no encode/decode e nos logs JSON.
11. **Uma thread e uma conexão Redis nova por commit** para publicar eventos (podia inclusive inverter a ordem
    dos eventos). Agora há uma única thread publicadora com fila limitada (10 mil), pipeline e conexão em pool.
12. **Streams gravavam um `SET` no Redis por mensagem de mark price**: agora em buffer e gravados em um
    pipeline a cada 0,5 s (e no shutdown).
13. **Preços do paper buscados em série**: agora em paralelo.
14. **Frontend: cada mensagem de ticker/mark (até ~8/s) re-renderizava a página ao vivo inteira** (gráfico,
    tabelas, painel de ordem) e refazia os 200 marcadores do gráfico. Agora há seletores por símbolo
    (`useTicker`, `useMark`, `useLivePrice`, `useLivePrices`, `useRealtimeSelector`); só a cotação, a tabela de
    posições, o card da conta e o painel de ordem re-renderizam, e só quando o preço deles muda. Linhas e
    marcadores do gráfico são memoizados; EMA incremental; `recharts` e `lightweight-charts` em chunks lazy.

### 5.3 Limitações e próximos passos

- Sem HTTP/2 (exigiria a dependência `h2`); o keep-alive já elimina a maior parte do custo.
- A janela de ban 429/418 e o peso usado são por processo (não compartilhados via Redis); cada worker
  aprende o peso pela próxima resposta.
- A contagem total do histórico continua fazendo varredura agregada (~10 ms com 400 mil linhas); se crescer,
  trocar por contagem estimada ou paginação por cursor.
- A exclusão de conta é dominada pela atualização das linhas de auditoria (`SET NULL`), não pela busca.
- Ao subir o novo serviço `celery_worker_trading`, faça deploy do worker e do beat juntos: tasks de trading
  enviadas para a fila `trading` sem esse worker ficam paradas.
