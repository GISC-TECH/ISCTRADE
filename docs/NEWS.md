# Notícias, sentimento e calendário econômico

> [!CAUTION]
> **Notícias são um FILTRO, nunca um gatilho.** O módulo de notícias só pode **bloquear** ou **reduzir** novas
> entradas geradas pelas estratégias; ele nunca abre, inverte ou encerra posições sozinho, e saídas/stops nunca
> são bloqueados. Análise automática de sentimento (LLM ou léxico) erra com frequência, reage a manchetes
> enganosas e chega depois do mercado. **Nenhum filtro garante lucro** nem evita perdas; mantenha os limites do
> RiskManager conservadores.

## Visão geral

```
RSS/Atom ─┐
CryptoPanic (opcional) ─┼─► dedup ─► tagging (ativo → símbolo USDT-M) ─► news_items ─► sentimento (LLM | léxico)
X recent search (opcional) ─┘                                                         │
                                                                                      ▼
            ForexFactory + manuais ─► economic_events        agregação por ativo (decaimento + impacto)
                         │                                              │
                         ▼                                              ▼
              pausa por evento (bot) ──────────────►  _entry_gate do BotRunner ─► RiskManager ─► executor
```

Tarefas Celery (beat): `news.ingest` (`NEWS_REFRESH_SECONDS`, padrão 5 min), `calendar.refresh`
(`ECON_CALENDAR_REFRESH_SECONDS`, padrão 1 h) e `calendar.alerts` (a cada minuto).

## Fontes

| Fonte | Configuração | Observações |
|---|---|---|
| RSS/Atom | `NEWS_RSS_FEEDS` (lista separada por vírgula) | Padrão: CoinDesk, Cointelegraph, Decrypt, The Block. XML lido com `defusedxml` (bloqueia *entity expansion*/XXE). |
| CryptoPanic | `CRYPTOPANIC_API_TOKEN`, `CRYPTOPANIC_API_URL` | Opcional (planos pagos). Respeita `CRYPTOPANIC_MIN_INTERVAL_SECONDS` por causa da cota mensal. Usa as moedas marcadas pela própria CryptoPanic. |
| X (Twitter) | `X_BEARER_TOKEN`, `X_ACCOUNTS`, `X_QUERIES`, `X_MAX_RESULTS` | Opcional. API v2 *recent search*; consulta = `from:conta OR (query)`, sem retweets. Throttle `X_MIN_INTERVAL_SECONDS`. |

Deduplicação por URL normalizada (sem query/fragmento/`www`) ou, sem URL, pelo título normalizado. Notícias mais
antigas que `NEWS_RETENTION_DAYS` são descartadas/podadas. Uma fonte com erro não interrompe as outras; o erro é
registrado só com o tipo da exceção (a CryptoPanic leva o token na URL, que por isso nunca é logada).

**Tagging**: nomes (bitcoin, ethereum, solana…) sem diferenciar maiúsculas, tickers em maiúsculas (`BTC`, `$SOL`)
e, quando existe um scan de mercado, todos os ativos do universo do scanner. Tickers que também são palavras
comuns (`ONE`, `GAS`, `NEAR`, `OP`…) só contam com `$`. Contratos cotados por 1000 unidades (`1000PEPEUSDT`) são
resolvidos automaticamente.

## Sentimento

- **LLM** (opcional): qualquer API compatível com OpenAI (`LLM_API_BASE`, `LLM_API_KEY`, `LLM_MODEL`), via
  `POST /chat/completions` com `response_format=json_object` e `temperature=0`. Saída validada:
  `sentiment` (−1 a 1), `impact` (`low`/`medium`/`high`), `assets` e `summary_pt` (resumo curto em português).
- **Privacidade**: só o texto público da notícia (fonte, título, trecho) é enviado. Nunca saldo, posições, chaves,
  e-mails ou qualquer dado de conta/workspace. O prompt instrui o modelo a ignorar instruções contidas na notícia
  (*prompt injection*) e a saída é sempre tratada como dado (validada e limitada).
- **Custos e limites**: cache por hash do conteúdo (Redis, 7 dias), timeout `LLM_TIMEOUT_SECONDS`, no máximo
  `LLM_MAX_CALLS_PER_HOUR` chamadas por hora e `LLM_MAX_ITEMS_PER_RUN` por coleta. Excedeu, falhou ou sem chave →
  **léxico local** (palavras positivas/negativas em inglês e português, negação simples e termos de alto impacto
  como *hack*, *exploit*, *delisting*, *ETF*, *FOMC*).

### Agregação por ativo

Para cada símbolo: média ponderada do sentimento das notícias dos últimos `NEWS_SENTIMENT_WINDOW_HOURS` (48 h),
com peso = impacto (baixo 1, médio 2, alto 3) × `0,5^(idade/NEWS_SENTIMENT_HALF_LIFE_HOURS)` (meia-vida 6 h).
`confidence` = peso total / 3 (máx. 1). Também é marcado se houve notícia **negativa de alto impacto** recente.
API: `GET /api/v1/news/sentiment?symbols=BTCUSDT,ETHUSDT`.

## Filtro de notícias nos bots (`options.news_filter`)

| Campo | Padrão | Descrição |
|---|---|---|
| `mode` | `off` | `off` · `filter` (bloqueia) · `reduce_risk` (reduz o tamanho em vez de bloquear) |
| `opposing_threshold` | 0,4 | Sentimento agregado contra a direção (≤ −0,4 para compras, ≥ 0,4 para vendas) |
| `min_confidence` | 0,3 | Confiança mínima para considerar o sentimento |
| `high_impact_minutes` | 120 | Notícia negativa de **alto impacto** nesta janela bloqueia/reduz **qualquer** nova entrada no símbolo (0 = ignora) |
| `size_multiplier` | 0,5 | Fator aplicado ao tamanho no modo `reduce_risk` (nunca aumenta) |

Entradas filtradas aparecem nos motivos do tick (`news_sentiment_opposing:…`, `news_high_impact_negative:…`) e na
auditoria (`bot.entry_filtered`). Ordens *reduce-only* nunca são afetadas; tudo o que passa continua pelo
`RiskManager`.

## Calendário econômico e pausa por evento (`options.event_pause`)

- Fonte: JSON semanal do ForexFactory (`ECON_CALENDAR_URL`, espelho `nfs.faireconomy.media`), mais eventos manuais
  em `ECON_CALENDAR_MANUAL_EVENTS` (JSON `[{"title","time","country","impact"}]`) — usados também quando o
  ForexFactory está fora do ar.
- Bot com `event_pause.enabled`: nenhuma nova entrada entre `minutes_before` antes e `minutes_after` depois de um
  evento com impacto ≥ `min_impact` nas moedas `countries` (padrão USD). Opcionalmente, `risk_multiplier < 1`
  reduz o tamanho numa janela extra de `tighten_minutes` em volta da pausa.
- Ao entrar em pausa o bot registra `bot.event_pause` na auditoria e envia o alerta `bot_event_pause`.

## Alertas (WhatsApp)

| Evento | Quando |
|---|---|
| `news_high_impact` | Notícia de alto impacto com \|sentimento\| ≥ `NEWS_ALERT_MIN_ABS_SENTIMENT` em símbolo operado pelo workspace (bots ativos, incluindo seleção automática, ou posições abertas). Uma vez por notícia. |
| `economic_event` | Evento de alto impacto (moedas `ECON_CALENDAR_COUNTRIES`) começando em até `ECON_ALERT_LEAD_MINUTES`, para workspaces com bots ativos. |
| `bot_event_pause` | Um bot começou a pausar entradas por causa de um evento. |

Todos podem ser desligados por workspace na tela **Monitoramento** (veja [ALERTS.md](ALERTS.md)).

## Backtest

O backtest aceita os mesmos filtros (`filters.news_filter` e `filters.event_pause`, aba **Notícias & eventos**),
usando a mesma lógica pura (`app/services/news/gate.py`). **Limitação**: só existem as notícias e os eventos
coletados por esta instância desde que a coleta foi ligada — não há arquivo histórico de notícias, e o sentimento
de notícias antigas pode ter sido calculado depois do fato. Períodos sem dados rodam efetivamente sem filtro (o
resultado informa `news_items_available` e `events_available`). Para avaliar a estratégia em si, deixe os filtros
**desligados** (padrão).

## API

- `GET /api/v1/news/?symbol=&impact=&sentiment=positive|negative|neutral&source=rss|cryptopanic|x&search=&hours=&limit=&offset=`
- `GET /api/v1/news/sentiment?symbols=` · `GET /api/v1/news/status` (fontes ativas, sem tokens)
- `POST /api/v1/news/refresh` e `POST /api/v1/calendar/refresh` (papel trader/admin, com rate limit)
- `GET /api/v1/calendar/?days_back=&days_ahead=&impact=&country=`

Telas: **Notícias** (feed, badges de sentimento/impacto e medidores por ativo) e **Calendário**.
