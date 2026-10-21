"""Ready-made bot templates ("robôs prontos"): strategy + timeframe + parameters + risk settings + bot options.

Presets only pre-fill the bot creation form; they never choose the execution mode. Bots inherit the mode of the
account (paper by default) and every order still goes through the account RiskManager. Presets are meant for
paper/Testnet evaluation first — none of them is a recommendation or a promise of profit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.bot_options import BotOptions
from app.strategy.strategies import get_strategy_class

PAUSE_HIGH_IMPACT = {"enabled": True, "minutes_before": 30, "minutes_after": 30, "min_impact": "high"}
PAUSE_WIDE = {
    "enabled": True,
    "minutes_before": 60,
    "minutes_after": 60,
    "min_impact": "medium",
    "tighten_minutes": 120,
    "risk_multiplier": 0.5,
}
NEWS_REDUCE = {"mode": "reduce_risk", "size_multiplier": 0.5}
NEWS_FILTER = {"mode": "filter"}


@dataclass(frozen=True)
class BotPreset:
    key: str
    name: str
    description: str
    strategy_key: str
    interval: str
    symbol: str = "BTCUSDT"
    leverage: int = 1
    params: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "moderado"  # conservador | moderado | agressivo
    experimental: bool = False
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        cls = get_strategy_class(self.strategy_key)
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "strategy_key": self.strategy_key,
            "strategy_name": cls.name,
            "interval": self.interval,
            "symbol": self.symbol,
            "leverage": self.leverage,
            "params": cls.validate_params(self.params),  # complete, validated parameter set
            "options": BotOptions.model_validate(self.options).model_dump(),
            "risk_level": self.risk_level,
            "experimental": self.experimental,
            "execution_mode": "paper",  # presets never enable live trading
            "tags": list(self.tags),
        }


PRESETS: tuple[BotPreset, ...] = (
    BotPreset(
        key="trend_conservative_btc_4h",
        name="Tendência conservadora BTC 4h",
        description=(
            "Segue tendências fortes do BTC (EMA 20/50 + ADX ≥ 25) com trailing stop de 3 ATRs, metade do risco por "
            "trade, alavancagem 1x, pausa em eventos de alto impacto e redução de tamanho com notícias contrárias."
        ),
        strategy_key="ema_adx_trend",
        interval="4h",
        params={
            "fast": 20,
            "slow": 50,
            "adx_threshold": 25,
            "trail_atr": 3.0,
            "atr_multiplier": 2.5,
            "risk_fraction": 0.5,
        },
        options={"event_pause": PAUSE_HIGH_IMPACT, "news_filter": NEWS_REDUCE},
        risk_level="conservador",
        tags=("tendência", "swing"),
    ),
    BotPreset(
        key="rsi2_reversion_1h",
        name="Reversão RSI-2 1h",
        description=(
            "RSI(2) de Larry Connors: compra recuos curtos acima da SMA 200 (vende repiques abaixo), sai na SMA 5 ou "
            "após 10 candles. Stop largo (3 ATRs) e metade do risco por trade."
        ),
        strategy_key="connors_rsi2",
        interval="1h",
        params={"oversold": 10, "overbought": 90, "max_bars": 10, "atr_multiplier": 3.0, "risk_fraction": 0.5},
        options={"event_pause": PAUSE_HIGH_IMPACT},
        risk_level="moderado",
        tags=("reversão",),
    ),
    BotPreset(
        key="turtle_breakout_1d",
        name="Rompimento Turtle 1d",
        description=(
            "Regras dos Turtles no diário: rompimento de 20 (com filtro do último trade) ou 55 candles, stop em 2N e "
            "saída no canal de 10. Poucos trades, longos; aceita sequências de pequenas perdas."
        ),
        strategy_key="turtle",
        interval="1d",
        params={"risk_fraction": 0.5},
        options={"event_pause": PAUSE_HIGH_IMPACT},
        risk_level="conservador",
        tags=("rompimento", "tendência", "position"),
    ),
    BotPreset(
        key="vwap_scalp_5m",
        name="Scalp VWAP 5m (experimental)",
        description=(
            "Reversão à VWAP do dia UTC em 5 minutos: entra na volta para dentro da banda de 2 desvios, alvo na VWAP, "
            "zera no fim do dia. Muito sensível a taxas/slippage — risco por trade reduzido a 25% e filtro de notícias."
        ),
        strategy_key="vwap_reversion",
        interval="5m",
        params={"band_std": 2.0, "min_session_bars": 12, "atr_multiplier": 1.5, "risk_fraction": 0.25},
        options={"event_pause": PAUSE_WIDE, "news_filter": NEWS_FILTER},
        risk_level="agressivo",
        experimental=True,
        tags=("reversão", "intraday", "scalp"),
    ),
    BotPreset(
        key="holy_grail_eth_1h",
        name="Holy Grail ETH 1h",
        description=(
            "Pullback à EMA 20 em tendência forte (ADX > 30) no ETH, stop abaixo do candle do pullback, alvo na "
            "máxima recente e trailing de 3 ATRs."
        ),
        strategy_key="holy_grail",
        symbol="ETHUSDT",
        interval="1h",
        params={"adx_threshold": 30, "risk_fraction": 0.5},
        options={"event_pause": PAUSE_HIGH_IMPACT},
        tags=("tendência", "pullback"),
    ),
    BotPreset(
        key="ichimoku_btc_4h",
        name="Nuvem Ichimoku BTC 4h",
        description="Rompimento da nuvem com confirmação Tenkan/Kijun e Chikou; saída na Kijun. Parâmetros clássicos.",
        strategy_key="ichimoku_breakout",
        interval="4h",
        params={"risk_fraction": 0.5},
        options={"event_pause": PAUSE_HIGH_IMPACT},
        risk_level="conservador",
        tags=("tendência",),
    ),
    BotPreset(
        key="macd_trend_eth_4h",
        name="MACD tendência ETH 4h",
        description="Cruzamentos do MACD somente a favor da EMA 200, com trailing stop de 3 ATRs.",
        strategy_key="macd_trend",
        symbol="ETHUSDT",
        interval="4h",
        params={"trail_atr": 3.0, "risk_fraction": 0.5},
        options={"event_pause": PAUSE_HIGH_IMPACT},
        tags=("tendência",),
    ),
    BotPreset(
        key="keltner_breakout_1h",
        name="Rompimento Keltner 1h",
        description="Rompimentos do canal de Keltner (EMA 20 ± 2 ATR) com trailing de 2,5 ATRs.",
        strategy_key="keltner_breakout",
        interval="1h",
        params={"risk_fraction": 0.5},
        options={"event_pause": PAUSE_HIGH_IMPACT, "news_filter": NEWS_REDUCE},
        tags=("rompimento",),
    ),
    BotPreset(
        key="ribbon_btc_1h",
        name="Fita de EMAs BTC 1h",
        description="Alinhamento das EMAs 8/21/55 com trailing stop; simples e didático para comparar com o backtest.",
        strategy_key="triple_ema",
        interval="1h",
        params={"risk_fraction": 0.5},
        options={"event_pause": PAUSE_HIGH_IMPACT},
        tags=("tendência",),
    ),
    BotPreset(
        key="williams_vbo_1d",
        name="Volatility breakout Larry Williams 1d",
        description=(
            "Compra fechamentos acima de abertura + 0,6 × amplitude do dia anterior (a favor da EMA 50) e sai no "
            "candle seguinte. Trades curtos e frequentes."
        ),
        strategy_key="volatility_breakout",
        interval="1d",
        params={"k": 0.6, "hold_bars": 1, "risk_fraction": 0.5},
        options={"event_pause": PAUSE_HIGH_IMPACT},
        tags=("rompimento", "curto prazo"),
    ),
    BotPreset(
        key="orb_ny_15m",
        name="Rompimento da abertura de NY 15m (experimental)",
        description=(
            "Faixa da primeira hora a partir de 13:00 UTC; entra no primeiro rompimento com stop no lado oposto, "
            "alvo 2R e saída no fim da sessão (8h). Pausa ampla em eventos macro (payroll, CPI, FOMC)."
        ),
        strategy_key="session_breakout",
        interval="15m",
        params={
            "session_start_hour": 13,
            "range_minutes": 60,
            "session_hours": 8,
            "take_profit_r": 2.0,
            "risk_fraction": 0.25,
        },
        options={"event_pause": PAUSE_WIDE, "news_filter": NEWS_FILTER},
        risk_level="agressivo",
        experimental=True,
        tags=("rompimento", "intraday", "sessão"),
    ),
    BotPreset(
        key="trend_hunter_top3_1h",
        name="Caçador de tendências — top 3 do scanner 1h",
        description=(
            "Supertrend nos 3 símbolos mais bem ranqueados pelo scanner (prioriza os que têm sinal), no máximo 2 "
            "posições simultâneas e metade do risco por trade."
        ),
        strategy_key="supertrend",
        interval="1h",
        params={"risk_fraction": 0.5},
        options={
            "auto_symbols": {"enabled": True, "top_n": 3, "max_positions": 2, "min_score": 40, "prefer_signals": True},
            "event_pause": PAUSE_HIGH_IMPACT,
            "news_filter": NEWS_REDUCE,
        },
        tags=("tendência", "multi-símbolo"),
    ),
)

PRESETS_BY_KEY: dict[str, BotPreset] = {p.key: p for p in PRESETS}


def list_presets() -> list[dict[str, Any]]:
    return [p.as_dict() for p in PRESETS]
