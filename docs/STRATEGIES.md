# Estratégias e scanner de mercado

> [!CAUTION]
> **Nenhuma estratégia, score ou sinal garante lucro.** Futuros alavancados podem gerar perdas maiores que o
> capital alocado em um trade. Resultados de backtest são hipotéticos (funding, slippage e taxas
> estimados, sujeitos a *overfitting*) e **não** indicam resultados futuros. Use paper/Testnet primeiro, compare o
> paper com o backtest por semanas antes de qualquer conta real, e mantenha os limites de risco conservadores.

## Como as estratégias funcionam

Todas as estratégias implementam a interface `Strategy` (`backend/app/strategy/base.py`):

- recebem os **candles fechados** do símbolo e o estado da posição (`StrategyContext`);
- devolvem *intenções* de ordem (`OrderIntent`), nunca ordens diretas;
- toda intenção de entrada carrega um **stop loss** (obrigatório — o `RiskManager` rejeita entradas sem stop);
- a intenção pode carregar um **take profit** opcional (`OrderIntent.take_profit`): em contas reais vira uma ordem
  `TAKE_PROFIT_MARKET` *reduce-only*; no paper é verificado pela tarefa `paper.process_orders` contra o preço de
  marcação; no backtest é simulado no preço do alvo (se o mesmo candle tocar stop e alvo, conta o **stop** —
  hipótese pessimista);
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
| `macd_trend` | Tendência | MACD cruza a linha de sinal a favor da EMA 200 (inverte a posição) | Cruzamento oposto, trailing opcional ou stop ATR |
| `turtle` | Rompimento | Rompimento de 20 candles (pula após trade vencedor) ou de 55 (failsafe) | Canal de 10 ou stop 2N |
| `triple_ema` | Tendência | EMAs 8/21/55 passam a ficar alinhadas | Rápida cruza a média, trailing ATR (3) ou stop ATR |
| `heikin_ashi_ema` | Tendência | Heikin-Ashi muda de cor a favor da EMA 50 | Heikin-Ashi muda de cor ou stop ATR |
| `ichimoku_breakout` | Tendência | Fechamento rompe a nuvem + Tenkan/Kijun + Chikou | Fechamento além da Kijun ou stop ATR |
| `holy_grail` | Tendência | ADX ≥ 30, pullback à EMA 20 e fechamento acima da máxima do pullback | Alvo na máxima recente (ou 2R), trailing ATR ou stop estrutural |
| `keltner_breakout` | Rompimento | Fechamento rompe o canal de Keltner | Volta à EMA central, trailing ATR (2,5) ou stop ATR |
| `volatility_breakout` | Rompimento | Fechamento > abertura + k × amplitude anterior (filtro EMA 50) | Saída por tempo (N candles), stop ATR ou alvo |
| `session_breakout` | Rompimento | Primeiro rompimento da faixa de abertura da sessão UTC | Alvo 2R, stop no lado oposto da faixa, fim da sessão |
| `connors_rsi2` | Reversão | RSI(2) < 10 acima da SMA 200 (vendido: > 90 abaixo) | Fechamento além da SMA 5, saída por tempo ou stop ATR largo |
| `stoch_rsi_reversal` | Reversão | %K cruza %D no extremo (filtro EMA 200) | %K no extremo oposto ou stop ATR |
| `vwap_reversion` | Reversão | Volta para dentro da banda de 2σ da VWAP do dia UTC | Alvo na VWAP, fim do dia UTC ou stop ATR |

Quando usar (orientação geral, não recomendação):

- **Reversão à média** (`rsi_mean_reversion`, `bollinger_reversion`, `rsi_bollinger`, `connors_rsi2`,
  `stoch_rsi_reversal`, `vwap_reversion`): mercados laterais. Sofrem
  em tendências fortes — combine com o scanner (`trend = lateral`, ADX baixo).
- **Rompimento** (`bollinger_squeeze`, `donchian_breakout`, `turtle`, `keltner_breakout`, `volatility_breakout`,
  `session_breakout`): após períodos de compressão. Muitos rompimentos
  falsos; o stop ATR limita a perda de cada um.
- **Tendência** (`ema_adx_trend`, `supertrend`, `ema_cross`, `macd_trend`, `triple_ema`, `heikin_ashi_ema`,
  `ichimoku_breakout`, `holy_grail`): mercados direcionais. Perdem em lateralização
  (sequências de pequenos stops); o filtro ADX reduz entradas em mercados sem tendência.

## Estratégias clássicas (detalhes, fontes e riscos)

As 12 estratégias abaixo (`backend/app/strategy/classic.py`) são **reimplementações independentes** de regras
publicadas em livros, artigos e setups clássicos de indicadores. **Nenhum código foi copiado** de repositórios de
terceiros: coleções como `freqtrade-strategies` (GPL-3.0) e OctoBot (GPL-3.0) são incompatíveis com a licença MIT
deste projeto e serviram, no máximo, como referência de *quais* ideias são populares; Jesse (MIT) e Hummingbot
(Apache-2.0) também não tiveram código reaproveitado. As regras foram reescritas a partir das fontes citadas.

Parâmetros comuns a todas: `atr_period`, `atr_multiplier` (stop em ATRs), `take_profit_r` (alvo em múltiplos do
risco; 0 = desligado), `allow_short` e `risk_fraction`. Stops estruturais (Holy Grail, rompimento de sessão)
nunca ficam a menos de 0,25 ATR do preço, para evitar posições gigantes.

| Nome (chave) | Tipo | Timeframe sugerido | Ideia | Fonte / inspiração | Parâmetros principais (padrão) | Riscos |
|---|---|---|---|---|---|---|
| MACD + sinal com filtro (`macd_trend`) | Tendência | 1h–1d | Cruzamento MACD × sinal somente a favor da EMA 200 | Gerald Appel (criador do MACD); filtro de média longa popularizado em diversos manuais | `fast` 12, `slow` 26, `signal` 9, `trend_period` 200, `use_trend_filter` sim, stop 2,5 ATR | Muitos sinais falsos em lateralização; atraso nas viradas |
| Turtle (`turtle`) | Rompimento / tendência | 1d (4h) | Rompimento de 20 candles (pula se o último rompimento foi vencedor) ou 55 (failsafe); stop 2N; saída no canal de 10 | Regras dos Turtles de Richard Dennis e William Eckhardt, publicadas por Curtis Faith (*Way of the Turtle*) e no documento público *The Original Turtle Trading Rules* | `entry_period` 20, `failsafe_period` 55, `exit_period` 10, `atr_period` (N) 20, stop 2N, `skip_after_win` sim | Taxa de acerto baixa (~30–40%), longas sequências de perdas; sem *pyramiding* (posição única, ver nota) |
| Fita de 3 EMAs (`triple_ema`) | Tendência | 15m–4h | Entrada quando EMAs 8/21/55 se alinham; saída quando a rápida cruza a média | Setup clássico de "ribbon"/médias múltiplas (ex.: Guppy, *Multiple Moving Averages*) | `fast` 8, `mid` 21, `slow` 55, `trail_atr` 3 | Whipsaw em lateralização |
| Heikin-Ashi + EMA (`heikin_ashi_ema`) | Tendência | 1h–1d | Virada de cor do Heikin-Ashi a favor da EMA 50 | Técnica japonesa Heikin-Ashi (Dan Valcu, *Using the Heikin-Ashi Technique*, 2004) | `ema_period` 50, `require_no_wick` não | Muitas viradas curtas; os candles HA são sintéticos (o preço de entrada é o fechamento real) |
| Ichimoku (`ichimoku_breakout`) | Tendência | 4h–1d | Rompimento da nuvem com Tenkan > Kijun e Chikou acima do preço de 26 candles atrás | Goichi Hosoda (Ichimoku Kinko Hyo); parâmetros 9/26/52 | `tenkan` 9, `kijun` 26, `senkou_b` 52, `displacement` 26, stop 2,5 ATR | Atrasado; muitas falsas saídas em lateralização |
| Holy Grail (`holy_grail`) | Tendência / pullback | 1h–1d | ADX ≥ 30 e +DI > −DI; pullback toca a EMA 20; entrada no fechamento acima da máxima do candle do pullback; alvo na máxima recente | Linda Bradford Raschke e Laurence Connors, *Street Smarts* (1995) | `adx_threshold` 30, `ema_period` 20, `swing_lookback` 20, `stop_buffer_atr` 0,25, `take_profit_r` 2, `trail_atr` 3 | Pullbacks que viram reversão; poucos sinais |
| Keltner (`keltner_breakout`) | Rompimento | 1h–4h | Fechamento rompe EMA 20 ± 2 ATR(10) | Chester Keltner (1960), versão com ATR de Linda Raschke | `kc_period` 20, `kc_atr_period` 10, `kc_multiplier` 2, `trail_atr` 2,5 | Rompimentos falsos; *gaps* |
| Volatility breakout (`volatility_breakout`) | Rompimento / curto prazo | 4h–1d | Fechamento acima de abertura + k × amplitude do candle anterior; saída por tempo | Larry Williams, *Long-Term Secrets to Short-Term Trading* (1999) | `k` 0,6, `hold_bars` 1, `trend_period` 50 | Avaliado no **fechamento** (o original usa ordem stop intradiária); sensível a custos |
| Rompimento de sessão (`session_breakout`) | Rompimento intradiário | 5m–15m | Faixa dos primeiros `range_minutes` após `session_start_hour` UTC; primeiro rompimento; fecha no fim da sessão | *Opening Range Breakout* — Toby Crabel, *Day Trading with Short Term Price Patterns and Opening Range Breakout* (1990) | `session_start_hour` 13 (NY), `range_minutes` 60, `session_hours` 8, `take_profit_r` 2, stop no lado oposto (ou no meio) | Cripto opera 24h: a "abertura" é convencional; stops curtos exigem notional > capital (alavancagem 3x no robô pronto) |
| RSI-2 de Connors (`connors_rsi2`) | Reversão | 1h–1d | RSI(2) < 10 acima da SMA 200 → compra; sai acima da SMA 5 | Larry Connors e Cesar Alvarez, *Short Term Trading Strategies That Work* (2008) | `rsi_period` 2, `oversold` 10, `overbought` 90, `trend_period` 200, `exit_period` 5, `max_bars` 10, stop 3 ATR | O original não usa stop (aqui há stop largo obrigatório); perdas grandes em quedas fortes; acerto alto com perdas maiores que ganhos |
| Stochastic RSI (`stoch_rsi_reversal`) | Reversão | 15m–4h | %K cruza %D abaixo de 20 (acima de 80 para venda), a favor da EMA 200 | Tushar Chande e Stanley Kroll, *The New Technical Trader* (1994) | `rsi_period` 14, `stoch_period` 14, `k_smooth` 3, `d_smooth` 3, 20/80, filtro EMA 200 | Indicador muito rápido/ruidoso; contra-tendência |
| VWAP intradiária (`vwap_reversion`) | Reversão intradiária (experimental) | 1m–1h | Volta para dentro da banda de 2σ da VWAP ancorada no dia UTC; alvo na VWAP; zera no fim do dia | Prática institucional de VWAP / bandas de desvio (ex.: *VWAP bands* em plataformas como TradingView) | `band_std` 2, `min_session_bars` 6, `flat_at_session_end` sim, stop 1,5 ATR | Muitos trades → custos dominam; em tendência a VWAP "anda" contra a posição; não opera em 1d |

Notas:

- **Turtle**: o tamanho calculado pelo `RiskManager` (risco por trade ÷ distância até o stop de 2N) equivale à
  "unidade N" dos Turtles. *Pyramiding* (até 4 unidades) e o limite de unidades correlacionadas não são
  implementados (posição líquida única). A regra "pular após vencedor" é simplificada: o rompimento pulado zera o
  filtro (o original acompanha o resultado hipotético do trade pulado).
- **Funding contrarian** não foi implementado: o pipeline de estratégias (bots e backtest) ainda não recebe o
  histórico de *funding*. O scanner já exibe o funding atual de cada símbolo.
- **Sem lookahead**: canais e faixas de entrada usam apenas candles anteriores ao candle fechado; a nuvem do
  Ichimoku usa os valores calculados `displacement` candles antes. Há testes que comparam indicadores calculados no
  histórico completo com o prefixo, e backtests cortados × completos (`tests/test_classic_strategies.py`).

## Robôs prontos (presets)

`GET /api/v1/strategies/presets` lista modelos de bot prontos (`backend/app/strategy/presets.py`): estratégia +
timeframe + símbolo + parâmetros + alavancagem + opções (pausa em eventos, filtro de notícias, seleção automática).
Na tela **Bots → Novo bot** escolha um "Robô pronto" para preencher o formulário (tudo continua editável); na tela
**Estratégias & Backtest** use "Testar um robô pronto" para rodar o backtest com a mesma configuração.

Regras de segurança dos presets: **sempre paper** (com um preset selecionado só contas paper aparecem; nenhum preset
liga o modo real), no máximo metade do risco por trade (`risk_fraction` ≤ 0,5), alavancagem 1x (3x apenas nos
intradiários, porque stops curtos exigem notional maior que o capital — o risco por trade continua limitado pelo
stop) e pausa em eventos de alto impacto ligada em todos.

| Chave | Nome | Estratégia | Mercado | Risco | Filtros |
|---|---|---|---|---|---|
| `trend_conservative_btc_4h` | Tendência conservadora BTC 4h | `ema_adx_trend` (ADX ≥ 25, trailing 3 ATR) | BTCUSDT 4h, 1x | conservador | eventos + notícias (reduz tamanho) |
| `rsi2_reversion_1h` | Reversão RSI-2 1h | `connors_rsi2` | BTCUSDT 1h, 1x | moderado | eventos |
| `turtle_breakout_1d` | Rompimento Turtle 1d | `turtle` | BTCUSDT 1d, 1x | conservador | eventos |
| `vwap_scalp_5m` | Scalp VWAP 5m (experimental) | `vwap_reversion` (25% do risco) | BTCUSDT 5m, 3x | agressivo | eventos (amplo) + notícias (bloqueia) |
| `holy_grail_eth_1h` | Holy Grail ETH 1h | `holy_grail` | ETHUSDT 1h, 1x | moderado | eventos |
| `ichimoku_btc_4h` | Nuvem Ichimoku BTC 4h | `ichimoku_breakout` | BTCUSDT 4h, 1x | conservador | eventos |
| `macd_trend_eth_4h` | MACD tendência ETH 4h | `macd_trend` (trailing 3 ATR) | ETHUSDT 4h, 1x | moderado | eventos |
| `keltner_breakout_1h` | Rompimento Keltner 1h | `keltner_breakout` | BTCUSDT 1h, 1x | moderado | eventos + notícias (reduz) |
| `ribbon_btc_1h` | Fita de EMAs BTC 1h | `triple_ema` | BTCUSDT 1h, 1x | moderado | eventos |
| `williams_vbo_1d` | Volatility breakout Larry Williams 1d | `volatility_breakout` | BTCUSDT 1d, 1x | moderado | eventos |
| `orb_ny_15m` | Rompimento da abertura de NY 15m (experimental) | `session_breakout` (25% do risco) | BTCUSDT 15m, 3x | agressivo | eventos (amplo) + notícias (bloqueia) |
| `trend_hunter_top3_1h` | Caçador de tendências — top 3 do scanner 1h | `supertrend` + seleção automática (top 3, máx. 2 posições, score ≥ 40) | auto, 1h, 1x | moderado | eventos + notícias (reduz) |

## Backtest

`POST /api/v1/strategies/backtest` (tela **Estratégias & Backtest**) usa a mesma estratégia, o mesmo
`RiskManager`, taxas e slippage. Cada estratégia técnica tem teste de unidade e um *smoke test* de backtest em
`backend/tests/test_technical_strategies.py` (clássicas: `backend/tests/test_classic_strategies.py`). Limitações: preenchimento no fechamento do candle (com slippage),
stops intra-candle simulados com *gap*, take profit preenchido exatamente no alvo (sem *gap* favorável), sem
profundidade de livro.

### Dados (`data_source`)
- `exchange` (padrão): klines REST da exchange configurada (mainnet pública ou Testnet).
- `archive`: arquivo público **data.binance.vision** (Binance USDT-M mainnet; arquivos mensais e, no mês corrente,
  diários até ontem). Útil quando a REST da mainnet está bloqueada por região (HTTP 451). `app/backtest/archive.py`.
- `candles_csv`: CSV próprio.

### Funding (`funding.mode`)
Em cada horário de funding (00/08/16 UTC na Binance) a posição mantida paga/recebe `qty × preço × taxa`
(comprado paga taxa positiva; vendido recebe), com o preço interpolado entre abertura e fechamento do candle.
O resultado traz `metrics.total_funding` (negativo = pago) e `funding_events_applied`; a configuração salva informa
`funding_source` e quantos eventos havia. Modos:
- `auto` (padrão): histórico da exchange (`/fapi/v1/fundingRate`, paginado); se a REST falhar (ex.: 451), tenta o
  arquivo `data.binance.vision` (`fundingRate` mensal); sem dados → `funding_source: "unavailable"`. Com
  `candles_csv`, só aplica funding se vier `funding.csv` (senão `none`).
- `exchange`, `archive`: força a fonte (erro 502/422 se indisponível).
- `csv`: `funding.csv` com `fundingTime,fundingRate` (ms/s/ISO) ou o formato do arquivo
  (`calc_time,funding_interval_hours,last_funding_rate`).
- `constant`: `funding.constant_rate` a cada 8 h (modelo grosseiro quando não há histórico).
- `off`.

### Walk-forward / out-of-sample (`POST /api/v1/strategies/walk-forward`)
Mesmo corpo do backtest + `param_grid` (nome → lista de valores; até 200 combinações) e `walk_forward`
(`folds` 1–12, `train_ratio` 0,1–0,95, `anchored`, `objective` = `sharpe` \| `return_pct` \| `profit_factor` \|
`calmar`, `min_trades`). Os dados são divididos em `folds` janelas OOS consecutivas no fim do período, cada uma
precedida de uma janela in-sample (IS) — deslizante ou ancorada no início. Em cada fold, todas as combinações são
testadas na IS, a melhor (com pelo menos `min_trades` trades fechados) é aplicada **sem alteração** na OOS. Sem
`param_grid` vira uma validação dos `params` informados (IS × OOS por fold).

O resultado é salvo como um backtest (`config.mode = "walk_forward"` ou `"validation"`): `metrics` traz o agregado
**fora da amostra** (curva OOS encadeada com juros compostos: retorno, drawdown, Sharpe, trades, taxas, funding),
`is_avg_return_pct` × `oos_avg_return_pct`, `oos_positive_folds`, `efficiency` (retorno anualizado OOS / IS:
≈ 1 robusto, ≪ 1 ou negativo = *overfitting*), `param_stability` (1 = mesmos parâmetros em todos os folds) e
`metrics.folds` (datas, parâmetros escolhidos, métricas IS e OOS). Cada janela começa zerada com o saldo inicial e
recebe os candles anteriores só como histórico para os indicadores. Limite de custo:
`WALK_FORWARD_MAX_CANDLE_STEPS` (padrão 1.000.000 candles simulados ≈ 1 min).

```bash
curl -sS -X POST https://SEU_DOMINIO/api/v1/strategies/walk-forward -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{
    "strategy_key": "ema_cross", "symbol": "BTCUSDT", "interval": "1h",
    "start": "2024-01-01T00:00:00Z", "end": "2024-06-30T23:00:00Z", "data_source": "archive",
    "param_grid": {"fast": [9, 12, 20], "slow": [26, 50]},
    "walk_forward": {"folds": 4, "train_ratio": 0.7, "objective": "sharpe"}}'
```

Exemplo real (BTCUSDT 1h, jan–jun/2024, arquivo data.binance.vision, funding do arquivo): o backtest simples do
`ema_cross` padrão caiu de +3,75% para +2,73% ao incluir funding (−100 USDT em 414 cobranças); no walk-forward acima
os folds IS renderam em média +12,7% e os OOS −1,7% (eficiência ≈ 0) — exatamente o tipo de *overfitting* que a
validação fora da amostra existe para mostrar.

### Resultados indicativos (out/2026) — NÃO são garantia

> [!WARNING]
> Os números abaixo são **hipotéticos e indicativos**, gerados uma única vez para verificar que as estratégias
> funcionam de ponta a ponta. **Não indicam resultados futuros**, não foram otimizados (parâmetros padrão / do
> preset) e cobrem períodos curtos e um único mercado por linha. A maioria fica perto de zero ou negativa — o que
> é esperado para regras públicas após custos. Use-os apenas como ponto de partida para os seus próprios testes
> em paper.

Como reproduzir: `cd backend && python -m scripts.strategy_report --source vision --end 2026-09-30`. Dados: klines
de futuros USDT-M da **mainnet** do arquivo público `data.binance.vision` (a API REST da mainnet é bloqueada por
região no servidor de testes; os klines da Testnet têm *spikes* artificiais de até 190% num candle e **não** servem
para avaliar estratégias). Configuração: saldo 10.000 USDT, taxa 0,04% por execução, slippage 2 bps, risco 1% por
trade × `risk_fraction`, perda diária máx. 3%, kill switch de drawdown em 15% (o backtest para quando dispara —
"kill switch" na tabela), sem funding. Períodos: 5m = 30 dias, 15m = 90 dias, 1h = 1 ano, 4h = 2 anos, 1d = 3 anos.
"Rejeições" = intenções barradas pelo RiskManager (margem, perda diária). O "Caçador de tendências" usa seleção
automática de símbolos nos bots; no backtest roda só em BTCUSDT.

**Robôs prontos**

| Robô / estratégia | Mercado | Período | Trades | Retorno | DD máx. | Acerto | Profit factor | Buy & hold | Rejeições |
|---|---|---|---|---|---|---|---|---|---|
| Tendência conservadora BTC 4h | BTCUSDT 4h | 2024-10-01 → 2026-09-30 | 87 | -2.03% | 5.57% | 31% | 0.96 | +31.2% | 0 |
| Reversão RSI-2 1h | BTCUSDT 1h | 2025-10-01 → 2026-09-30 | 372 | -14.39% | 14.72% | 59% | 0.77 | -26.8% | 5 |
| Rompimento Turtle 1d | BTCUSDT 1d | 2023-10-02 → 2026-09-30 | 25 | +5.96% | 4.28% | 44% | 2.17 | +204.2% | 0 |
| Scalp VWAP 5m (experimental) | BTCUSDT 5m | 2026-09-01 → 2026-09-30 | 104 | -14.94% (kill switch) | 15.04% | 38% | 0.67 | +6.3% | 24 |
| Holy Grail ETH 1h | ETHUSDT 1h | 2025-10-01 → 2026-09-30 | 54 | -2.48% | 5.58% | 50% | 0.93 | -35.3% | 0 |
| Nuvem Ichimoku BTC 4h | BTCUSDT 4h | 2024-10-01 → 2026-09-30 | 97 | +5.26% | 6.75% | 33% | 1.39 | +31.2% | 0 |
| MACD tendência ETH 4h | ETHUSDT 4h | 2024-10-01 → 2026-09-30 | 158 | +16.50% | 5.43% | 34% | 1.66 | +2.1% | 0 |
| Rompimento Keltner 1h | BTCUSDT 1h | 2025-10-01 → 2026-09-30 | 180 | +12.67% | 11.24% | 35% | 1.41 | -26.8% | 3 |
| Fita de EMAs BTC 1h | BTCUSDT 1h | 2025-10-01 → 2026-09-30 | 164 | -8.83% (kill switch) | 15.20% | 30% | 0.91 | -26.8% | 4 |
| Volatility breakout Larry Williams 1d | BTCUSDT 1d | 2023-10-02 → 2026-09-30 | 205 | +0.14% | 2.35% | 49% | 1.11 | +204.2% | 0 |
| Rompimento da abertura de NY 15m (experimental) | BTCUSDT 15m | 2026-07-03 → 2026-09-30 | 87 | -3.72% | 4.24% | 41% | 0.92 | +36.1% | 0 |
| Caçador de tendências — top 3 do scanner 1h | BTCUSDT 1h | 2025-10-01 → 2026-09-30 | 165 | -9.37% (kill switch) | 15.24% | 32% | 0.92 | -26.8% | 0 |

**Estratégias clássicas** (parâmetros padrão, BTCUSDT; 3x nos intradiários)

| Robô / estratégia | Mercado | Período | Trades | Retorno | DD máx. | Acerto | Profit factor | Buy & hold | Rejeições |
|---|---|---|---|---|---|---|---|---|---|
| `macd_trend` (1x) | BTCUSDT 4h | 2024-10-01 → 2026-09-30 | 153 | +7.22% | 8.46% | 33% | 1.22 | +31.2% | 0 |
| `turtle` (1x) | BTCUSDT 1d | 2023-10-02 → 2026-09-30 | 25 | +11.98% | 8.53% | 44% | 2.12 | +204.2% | 0 |
| `triple_ema` (1x) | BTCUSDT 4h | 2024-10-01 → 2026-09-30 | 133 | +20.03% | 12.88% | 29% | 1.36 | +31.2% | 0 |
| `heikin_ashi_ema` (1x) | BTCUSDT 4h | 2024-10-01 → 2026-09-30 | 298 | -7.60% (kill switch) | 15.07% | 33% | 1.03 | +31.2% | 0 |
| `ichimoku_breakout` (1x) | BTCUSDT 4h | 2024-10-01 → 2026-09-30 | 97 | +10.18% | 13.21% | 33% | 1.37 | +31.2% | 0 |
| `holy_grail` (1x) | BTCUSDT 4h | 2024-10-01 → 2026-09-30 | 28 | -5.93% | 11.69% | 39% | 0.65 | +31.2% | 1 |
| `keltner_breakout` (1x) | BTCUSDT 4h | 2024-10-01 → 2026-09-30 | 92 | +19.72% | 11.32% | 34% | 1.50 | +31.2% | 0 |
| `volatility_breakout` (1x) | BTCUSDT 1d | 2023-10-02 → 2026-09-30 | 205 | +0.48% | 4.54% | 49% | 1.12 | +204.2% | 0 |
| `session_breakout` (3x) | BTCUSDT 15m | 2026-07-03 → 2026-09-30 | 72 | -14.38% (kill switch) | 15.33% | 36% | 0.75 | +36.1% | 77 |
| `connors_rsi2` (1x) | BTCUSDT 4h | 2024-10-01 → 2026-09-30 | 180 | -7.71% | 9.50% | 64% | 0.86 | +31.2% | 0 |
| `stoch_rsi_reversal` (1x) | BTCUSDT 4h | 2024-10-01 → 2026-09-30 | 43 | -14.96% (kill switch) | 15.12% | 37% | 0.41 | +31.2% | 0 |
| `vwap_reversion` (3x) | BTCUSDT 15m | 2026-07-03 → 2026-09-30 | 31 | -15.16% (kill switch) | 15.16% | 39% | 0.48 | +36.1% | 47 |

## Scanner de mercado

`backend/app/services/scanner.py` ranqueia os perpétuos USDT-M:

1. **Universo**: `exchangeInfo` (contratos `PERPETUAL`, `quoteAsset=USDT`, `TRADING`), tickers 24h e funding
   (`premiumIndex`). Filtro de liquidez `SCANNER_MIN_QUOTE_VOLUME` (USDT em 24h) e no máximo
   `SCANNER_MAX_SYMBOLS` símbolos (os mais líquidos).
2. **Klines** do intervalo `SCANNER_INTERVAL` (300 candles — o suficiente para filtros de 200 períodos), somente
   candles fechados, em cache no Redis
   (`SCANNER_KLINES_CACHE_SECONDS`; cache em memória quando o Redis não está disponível).
3. **Componentes** (0–1) e pesos padrão: força da tendência (ADX/50) 30%, volatilidade (ATR%/5%) 15%,
   momentum (|ROC 20|/10%) 20%, pico de volume (último candle / média de 20) 15%, funding (|funding|/0,1% —
   funding extremo indica posicionamento lotado, útil para quem busca reversões) 5% e quantidade de sinais das
   estratégias 15%. O score (0–100) mede **atividade/oportunidade**, não direção: a direção aparece em
   `trend` (EMAs + ADX ≥ 20) e `bias`. Pesos e filtros são configuráveis via `ScanParams`.
   Como o componente de sinais satura com 2 sinais, a lista maior de estratégias faz esse componente chegar ao
   máximo com mais frequência; ajuste `ScanParams.strategies`/`weights` se quiser outro comportamento.
4. **Sinais**: cada estratégia técnica e clássica (exceto `grid`) é executada sobre o símbolo com posição zerada; o resultado
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
