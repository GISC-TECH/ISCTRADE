# Estratégias e scanner de mercado

> [!CAUTION]
> **Nenhuma estratégia, score ou sinal garante lucro.** Futuros alavancados podem gerar perdas maiores que o
> capital alocado em um trade. Resultados de backtest são hipotéticos (sem funding, com slippage/taxas
> estimados, sujeitos a *overfitting*) e **não** indicam resultados futuros. Use paper/Testnet primeiro, compare o
> paper com o backtest por semanas antes de qualquer conta real, e mantenha os limites de risco conservadores.

## Como as estratégias funcionam

Todas as estratégias implementam a interface `Strategy` (`backend/app/strategy/base.py`):

- recebem os **candles fechados** do símbolo e o estado da posição (`StrategyContext`);
- devolvem *intenções* de ordem (`OrderIntent`), nunca ordens diretas;
- toda intenção de entrada carrega um **stop loss** (obrigatório — o `RiskManager` rejeita entradas sem stop);
- a quantidade, quando não informada, é calculada pelo risco: `risco por trade da conta × risk_fraction` dividido
  pela distância até o stop;
- as intenções passam pelo `OrderService` → **kill switch global** → `RiskManager` (6 regras) → executor (paper
  por padrão). O mesmo caminho é usado no backtest (`BacktestEngine`) e nos bots.

Os parâmetros são validados no servidor (tipo, mínimo/máximo e regras cruzadas, por exemplo EMA rápida < lenta) e
o formulário do frontend é gerado a partir de `GET /api/v1/strategies/` — uma estratégia nova aparece
automaticamente nas telas de bots e backtest.

### Parâmetros comuns (estratégias técnicas com stop ATR)

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `atr_period` | 14 | Período do ATR usado no stop |
| `atr_multiplier` | 2.0 | Stop = preço ∓ ATR × multiplicador |
| `allow_short` | sim | Permite posições vendidas |
| `risk_fraction` | 1.0 | Fração do risco máximo por trade da conta (0,01–1) |
| `trail_atr` | varia | *Trailing stop* por fechamento em ATRs (0 = desligado), quando disponível |

O *trailing stop* é avaliado no fechamento do candle e encerra a posição com ordem *reduce-only*; o stop inicial
(ATR) continua protegendo a posição entre candles (e é enviado como `STOP_MARKET` em contas reais).

## Catálogo

| Chave | Tipo | Entrada | Saída |
|---|---|---|---|
| `grid` | Grade | Níveis de compra/venda dentro de uma faixa | Nível acima/abaixo |
| `ema_cross` | Tendência | Cruzamento de EMAs | Cruzamento oposto |
| `rsi_bollinger` | Reversão | RSI extremo + fora das bandas | Média das bandas |
| `rsi_mean_reversion` | Reversão | RSI cruza de volta acima de `oversold` (compra) / abaixo de `overbought` (venda) | RSI atinge `exit_level` (50) ou stop ATR |
| `bollinger_reversion` | Reversão | Fechamento volta para dentro da banda após fechar fora | Média móvel central ou stop ATR |
| `bollinger_squeeze` | Rompimento | Largura das bandas perto da mínima de `squeeze_lookback` candles e fechamento fora da banda | Fechamento cruzando a média, trailing ATR (3) ou stop ATR |
| `donchian_breakout` | Rompimento | Fechamento acima/abaixo do canal de `entry_period` (20) candles anteriores (inverte posição oposta) | Canal de `exit_period` (10), trailing opcional ou stop ATR |
| `ema_adx_trend` | Tendência | EMA rápida > lenta, +DI > −DI e ADX ≥ `adx_threshold` (vendido: o inverso) | Cruzamento das EMAs, trailing ATR (3) ou stop ATR |
| `supertrend` | Tendência | Virada do Supertrend (`st_period`, `st_multiplier`) — inverte a posição | Virada oposta ou stop ATR |

Quando usar (orientação geral, não recomendação):

- **Reversão à média** (`rsi_mean_reversion`, `bollinger_reversion`, `rsi_bollinger`): mercados laterais. Sofrem
  em tendências fortes — combine com o scanner (`trend = lateral`, ADX baixo).
- **Rompimento** (`bollinger_squeeze`, `donchian_breakout`): após períodos de compressão. Muitos rompimentos
  falsos; o stop ATR limita a perda de cada um.
- **Tendência** (`ema_adx_trend`, `supertrend`, `ema_cross`): mercados direcionais. Perdem em lateralização
  (sequências de pequenos stops); o filtro ADX reduz entradas em mercados sem tendência.

## Backtest

`POST /api/v1/strategies/backtest` (tela **Estratégias & Backtest**) usa a mesma estratégia, o mesmo
`RiskManager`, taxas e slippage. Cada estratégia técnica tem teste de unidade e um *smoke test* de backtest em
`backend/tests/test_technical_strategies.py`. Limitações: sem custo de funding, preenchimento no fechamento do
candle (com slippage), stops intra-candle simulados com *gap*.

## Scanner de mercado

`backend/app/services/scanner.py` ranqueia os perpétuos USDT-M:

1. **Universo**: `exchangeInfo` (contratos `PERPETUAL`, `quoteAsset=USDT`, `TRADING`), tickers 24h e funding
   (`premiumIndex`). Filtro de liquidez `SCANNER_MIN_QUOTE_VOLUME` (USDT em 24h) e no máximo
   `SCANNER_MAX_SYMBOLS` símbolos (os mais líquidos).
2. **Klines** do intervalo `SCANNER_INTERVAL`, somente candles fechados, em cache no Redis
   (`SCANNER_KLINES_CACHE_SECONDS`; cache em memória quando o Redis não está disponível).
3. **Componentes** (0–1) e pesos padrão: força da tendência (ADX/50) 30%, volatilidade (ATR%/5%) 15%,
   momentum (|ROC 20|/10%) 20%, pico de volume (último candle / média de 20) 15%, funding (|funding|/0,1% —
   funding extremo indica posicionamento lotado, útil para quem busca reversões) 5% e quantidade de sinais das
   estratégias 15%. O score (0–100) mede **atividade/oportunidade**, não direção: a direção aparece em
   `trend` (EMAs + ADX ≥ 20) e `bias`. Pesos e filtros são configuráveis via `ScanParams`.
4. **Sinais**: cada estratégia técnica (exceto `grid`) é executada sobre o símbolo com posição zerada; o resultado
   (`long`/`short`) aparece como *badge*.
5. A tarefa Celery `scanner.run` (beat a cada `SCANNER_REFRESH_SECONDS`) grava o *snapshot* em `market_scans`,
   mantendo os últimos `SCANNER_KEEP_SNAPSHOTS`.

Por padrão o scanner usa **dados públicos da mainnet** (somente leitura, sem chaves; `SCANNER_USE_TESTNET=false`),
que são mais representativos que os da Testnet. Isso não envia ordens.

API: `GET /api/v1/scanner/?sort=score&order=desc&min_score=&min_quote_volume=&trend=up|down|flat&bias=&signal=&search=&limit=`
e `POST /api/v1/scanner/refresh` (papel trader/admin). Tela: **Scanner** (tabela ordenável, filtros e sinais).

### Seleção automática de símbolos nos bots

Nas **Opções** do bot (`options.auto_symbols`):

| Campo | Padrão | Descrição |
|---|---|---|
| `enabled` | falso | Liga a seleção automática (o campo Símbolo é ignorado) |
| `top_n` | 3 | Quantos símbolos do último scan o bot acompanha |
| `max_positions` | 2 | Máximo de posições simultâneas abertas pelo bot |
| `min_score` | 0 | Score mínimo para entrar na lista |
| `prefer_signals` | sim | Prioriza símbolos onde a estratégia do bot tem sinal ativo |

Símbolos com posição aberta continuam sendo gerenciados (saídas/stops) mesmo que saiam do top-N. Novas entradas
acima de `max_positions` são bloqueadas (`max_concurrent_positions`). **Todas as ordens continuam passando pelo
RiskManager da conta** — inclusive a perda diária e o kill switch, que valem para a conta inteira. Em contas
Testnet, símbolos sem dados na Testnet são ignorados.

## Veja também

- [NEWS.md](NEWS.md) — filtro de notícias/sentimento e pausa por eventos econômicos aplicados às entradas dos bots.
- [REALTIME.md](REALTIME.md) — painel em tempo real: candles ao vivo com EMAs, marcadores das entradas/saídas dos
  bots, posições com PnL ao vivo e ordens manuais (sempre via RiskManager, com stop obrigatório).
