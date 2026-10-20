"""Strategy registry."""

from __future__ import annotations

from typing import Any

from app.strategy.base import Strategy, StrategyParamError
from app.strategy.ema_cross import EmaCrossStrategy
from app.strategy.grid import GridStrategy
from app.strategy.rsi_reversion import RsiBollingerStrategy

STRATEGIES: dict[str, type[Strategy]] = {cls.key: cls for cls in (GridStrategy, EmaCrossStrategy, RsiBollingerStrategy)}

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
