"""Strategy registry."""

from __future__ import annotations

from typing import Any

from app.strategy.base import Strategy, StrategyParamError
from app.strategy.ema_cross import EmaCrossStrategy
from app.strategy.grid import GridStrategy
from app.strategy.rsi_reversion import RsiBollingerStrategy
from app.strategy.technical import TECHNICAL_STRATEGIES

STRATEGIES: dict[str, type[Strategy]] = {
    cls.key: cls for cls in (GridStrategy, EmaCrossStrategy, RsiBollingerStrategy, *TECHNICAL_STRATEGIES)
}

# Backwards compatible simple mapping key -> display name
AVAILABLE_STRATEGIES: dict[str, str] = {k: v.name for k, v in STRATEGIES.items()}


def get_strategy_class(key: str) -> type[Strategy]:
    try:
        return STRATEGIES[key]
    except KeyError as exc:
        raise StrategyParamError(f"unknown strategy '{key}'") from exc


def build_strategy(key: str, params: dict[str, Any] | None = None) -> Strategy:
    return get_strategy_class(key)(params or {})


def list_strategies() -> list[dict[str, Any]]:
    return [cls.describe() for cls in STRATEGIES.values()]


def entry_signal(strategy: Strategy, symbol: str, candles: list, equity: float = 10_000.0) -> str | None:
    """'long' / 'short' when a flat position would be opened on the last closed candle (used by the scanner)."""
    from decimal import Decimal

    from app.models.enums import OrderSide
    from app.strategy.base import StrategyContext

    if len(candles) < strategy.warmup:
        return None
    ctx = StrategyContext(
        symbol=symbol,
        candles=candles,
        position_qty=Decimal(0),
        position_entry=Decimal(0),
        equity=Decimal(str(equity)),
        leverage=1,
        params=strategy.params,
        state={},
    )
    try:
        intents = strategy.on_candle(ctx)
    except Exception:
        return None
    for intent in intents:
        if not intent.reduce_only:
            return "long" if intent.side == OrderSide.BUY else "short"
    return None
