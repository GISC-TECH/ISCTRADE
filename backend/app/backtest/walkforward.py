"""Walk-forward optimization / validation with out-of-sample reporting (F-09).

The candle series is split into ``folds`` consecutive out-of-sample (OOS) windows at the end of the data, each
preceded by an in-sample (IS) window:

    rolling  : |---- IS 1 ----|-- OOS 1 --|
                              |---- IS 2 ----|-- OOS 2 --|  ...
    anchored : |---------- IS k (all data before OOS k) ----------|-- OOS k --|

For every fold, each parameter combination of ``param_grid`` is backtested on the IS window and the best one
(by ``objective``, among combinations with at least ``min_trades`` closed trades) is then run, untouched, on the
OOS window. With an empty grid it is a plain validation of the given parameters (IS vs OOS per fold).

Indicator warm-up: OOS (and IS) runs receive the preceding candles as history (``start_index``), so the
strategy can compute its indicators from the first OOS candle without trading before it. Each window starts
flat with ``initial_balance``; the OOS equity curves are chained (compounded) into one curve, and the aggregate
OOS metrics are computed on it. ``efficiency`` = OOS annualized return / IS annualized return of the chosen
parameters (≈1 robust, ≪1 or negative: overfitting).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

from app.backtest.data import interval_ms
from app.backtest.engine import (
    YEAR_MS,
    BacktestConfig,
    BacktestResult,
    EntryGate,
    max_drawdown_pct,
    run_backtest,
    sharpe_ratio,
)
from app.backtest.funding import FundingEvent
from app.strategy.base import Candle, StrategyParamError
from app.strategy.strategies import build_strategy

OBJECTIVES = ("sharpe", "return_pct", "profit_factor", "calmar")
MAX_COMBINATIONS = 200


class WalkForwardError(ValueError):
    pass


@dataclass
class WalkForwardConfig:
    folds: int = 4
    train_ratio: float = 0.7  # IS share of each (IS + OOS) window in rolling mode
    anchored: bool = False
    objective: str = "sharpe"
    min_trades: int = 1
    max_candle_steps: int = 1_000_000  # budget: sum of simulated candles over every run


@dataclass
class Fold:
    index: int
    is_start: int
    is_end: int  # exclusive
    oos_start: int
    oos_end: int  # exclusive
    best_params: dict[str, Any] = field(default_factory=dict)
    is_metrics: dict[str, Any] = field(default_factory=dict)
    oos_metrics: dict[str, Any] = field(default_factory=dict)
    candidates: int = 0
    eligible: int = 0
    oos_curve: list[dict[str, Any]] = field(default_factory=list)
    oos_trades: list[dict[str, Any]] = field(default_factory=list)


def expand_grid(base: dict[str, Any], grid: dict[str, list[Any]] | None) -> list[dict[str, Any]]:
    grid = {k: list(v) for k, v in (grid or {}).items() if v is not None}
    for k, values in grid.items():
        if not values:
            raise WalkForwardError(f"param_grid.{k} está vazio")
    if not grid:
        return [dict(base)]
    keys = sorted(grid)
    total = math.prod(len(grid[k]) for k in keys)
    if total > MAX_COMBINATIONS:
        raise WalkForwardError(f"param_grid gera {total} combinações (máx. {MAX_COMBINATIONS})")
    return [{**base, **dict(zip(keys, combo))} for combo in itertools.product(*(grid[k] for k in keys))]


def split_folds(n: int, cfg: WalkForwardConfig, warmup: int) -> list[Fold]:
    if cfg.folds < 1:
        raise WalkForwardError("folds deve ser >= 1")
    if not 0.1 <= cfg.train_ratio <= 0.95:
        raise WalkForwardError("train_ratio deve estar entre 0.1 e 0.95")
    usable = n - warmup
    # rolling: L + k*T = usable with L = T * r/(1-r)
    ratio = cfg.train_ratio / (1 - cfg.train_ratio)
    test = int(usable / (cfg.folds + ratio))
    train = usable - cfg.folds * test
    if test < 10 or train < 10:
        raise WalkForwardError(
            f"dados insuficientes para {cfg.folds} folds (candles={n}, aquecimento={warmup}); "
            "use mais dados ou menos folds"
        )
    folds = []
    for k in range(cfg.folds):
        oos_start = warmup + train + k * test
        oos_end = n if k == cfg.folds - 1 else oos_start + test
        is_start = warmup if cfg.anchored else oos_start - train
        folds.append(Fold(k + 1, is_start, oos_start, oos_start, oos_end))
    return folds


def score(metrics: dict[str, Any], objective: str) -> float:
    if objective == "return_pct":
        return float(metrics.get("return_pct") or 0.0)
    if objective == "profit_factor":
        pf = metrics.get("profit_factor")
        return float(pf) if pf is not None else (10.0 if metrics.get("num_closed_trades") else 0.0)
    if objective == "calmar":
        ret = float(metrics.get("return_pct") or 0.0)
        dd = float(metrics.get("max_drawdown_pct") or 0.0)
        return ret / max(dd, 0.5)
    return float(metrics.get("sharpe") or 0.0)


def _annualized(ret_pct: float, bars: int, bar_ms: int) -> float:
    years = bars * bar_ms / YEAR_MS
    if years <= 0 or ret_pct <= -100:
        return -100.0 if ret_pct <= -100 else 0.0
    return ((1 + ret_pct / 100) ** (1 / years) - 1) * 100


def run_walk_forward(
    strategy_key: str,
    base_params: dict[str, Any],
    candles: list[Candle],
    symbol: str,
    interval: str,
    config: BacktestConfig | None = None,
    wf: WalkForwardConfig | None = None,
    param_grid: dict[str, list[Any]] | None = None,
    funding: list[FundingEvent] | None = None,
    entry_gate: EntryGate | None = None,
) -> dict[str, Any]:
    wf = wf or WalkForwardConfig()
    config = config or BacktestConfig()
    if wf.objective not in OBJECTIVES:
        raise WalkForwardError(f"objective deve ser um de: {', '.join(OBJECTIVES)}")
    combos = expand_grid(base_params, param_grid)
    strategies = []
    for params in combos:
        try:
            strategies.append(build_strategy(strategy_key, params))
        except StrategyParamError as exc:
            raise WalkForwardError(f"parâmetros inválidos {params}: {exc}") from exc
    warmup = max(s.warmup for s in strategies)
    folds = split_folds(len(candles), wf, warmup)
    steps = sum(len(combos) * (f.is_end - f.is_start) + (f.oos_end - f.oos_start) for f in folds)
    if steps > wf.max_candle_steps:
        raise WalkForwardError(
            f"walk-forward pesado demais ({steps} candles simulados; máx. {wf.max_candle_steps}): "
            "reduza o período, o grid ou os folds"
        )
    bar_ms = interval_ms(interval)

    def run(params: dict[str, Any], start: int, end: int) -> BacktestResult:
        # history before `start` feeds the indicators only; the window [start, end) is simulated
        hist = max(0, start - max(warmup * 3, 200, config.lookback or 0))
        return run_backtest(
            build_strategy(strategy_key, params), candles[hist:end], symbol, interval, config, entry_gate,
            funding, start_index=start - hist,
        )  # fmt: skip

    for fold in folds:
        best: tuple[float, int, dict[str, Any], dict[str, Any]] | None = None
        fallback: tuple[float, int, dict[str, Any], dict[str, Any]] | None = None
        for idx, strat in enumerate(strategies):
            metrics = run(strat.params, fold.is_start, fold.is_end).metrics
            cand = (score(metrics, wf.objective), -idx, strat.params, metrics)
            fold.candidates += 1
            if fallback is None or cand[:2] > fallback[:2]:
                fallback = cand
            if int(metrics.get("num_closed_trades") or 0) >= wf.min_trades:
                fold.eligible += 1
                if best is None or cand[:2] > best[:2]:
                    best = cand
        chosen = best or fallback
        assert chosen is not None
        fold.best_params, fold.is_metrics = dict(chosen[2]), chosen[3]
        oos = run(fold.best_params, fold.oos_start, fold.oos_end)
        fold.oos_metrics = oos.metrics
        fold.oos_curve = oos.equity_curve
        fold.oos_trades = [t.as_dict() for t in oos.trades]

    return _report(folds, candles, config, wf, bar_ms, len(combos), param_grid or {})


def _report(
    folds: list[Fold],
    candles: list[Candle],
    config: BacktestConfig,
    wf: WalkForwardConfig,
    bar_ms: int,
    combos: int,
    grid: dict[str, list[Any]],
) -> dict[str, Any]:
    initial = float(config.initial_balance)
    stitched: list[dict[str, Any]] = []
    capital = initial
    for fold in folds:
        if not fold.oos_curve:
            continue
        scale = capital / initial
        stitched.extend({"time": p["time"], "equity": p["equity"] * scale} for p in fold.oos_curve)
        capital = stitched[-1]["equity"]
    values = [p["equity"] for p in stitched] or [initial]
    oos_bars = sum(f.oos_end - f.oos_start for f in folds)
    oos_return = (values[-1] / initial - 1) * 100
    closed = sum(int(f.oos_metrics.get("num_closed_trades") or 0) for f in folds)
    wins = sum(
        round(int(f.oos_metrics.get("num_closed_trades") or 0) * float(f.oos_metrics.get("win_rate_pct") or 0) / 100)
        for f in folds
    )
    is_ann = [_annualized(float(f.is_metrics.get("return_pct") or 0), f.is_end - f.is_start, bar_ms) for f in folds]
    oos_ann = [_annualized(float(f.oos_metrics.get("return_pct") or 0), f.oos_end - f.oos_start, bar_ms) for f in folds]
    mean_is = sum(is_ann) / len(is_ann)
    efficiency = round((sum(oos_ann) / len(oos_ann)) / mean_is, 4) if mean_is > 0 else None
    distinct = {tuple(sorted(f.best_params.items())) for f in folds}
    first, last = candles[folds[0].oos_start].close, candles[-1].close
    metrics = {
        "mode": "walk_forward" if combos > 1 else "validation",
        "initial_balance": initial,
        "final_equity": round(values[-1], 2),
        "net_pnl": round(values[-1] - initial, 2),
        "return_pct": round(oos_return, 4),
        "max_drawdown_pct": round(max_drawdown_pct(values), 4),
        "sharpe": round(sharpe_ratio(values, bar_ms), 4),
        "num_closed_trades": closed,
        "win_rate_pct": round(wins / closed * 100, 2) if closed else 0.0,
        "total_fees": round(sum(float(f.oos_metrics.get("total_fees") or 0) for f in folds), 4),
        "total_funding": round(sum(float(f.oos_metrics.get("total_funding") or 0) for f in folds), 4),
        "buy_hold_return_pct": round((last / first - 1) * 100, 4) if first else 0.0,
        "oos_candles": oos_bars,
        "is_avg_return_pct": round(sum(float(f.is_metrics.get("return_pct") or 0) for f in folds) / len(folds), 4),
        "oos_avg_return_pct": round(sum(float(f.oos_metrics.get("return_pct") or 0) for f in folds) / len(folds), 4),
        "oos_positive_folds": sum(1 for f in folds if float(f.oos_metrics.get("return_pct") or 0) > 0),
        "efficiency": efficiency,
        "param_stability": round(1 - (len(distinct) - 1) / max(len(folds) - 1, 1), 4) if len(folds) > 1 else 1.0,
        "combinations": combos,
        "start": folds[0].oos_metrics.get("start"),
        "end": folds[-1].oos_metrics.get("end"),
        "folds": [
            {
                "index": f.index,
                "is_start": _iso(candles[f.is_start].open_time),
                "is_end": _iso(candles[f.is_end - 1].close_time),
                "oos_start": _iso(candles[f.oos_start].open_time),
                "oos_end": _iso(candles[f.oos_end - 1].close_time),
                "best_params": f.best_params,
                "candidates": f.candidates,
                "eligible": f.eligible,
                "is": _slim(f.is_metrics),
                "oos": _slim(f.oos_metrics),
            }
            for f in folds
        ],
        "settings": {
            "folds": wf.folds,
            "train_ratio": wf.train_ratio,
            "anchored": wf.anchored,
            "objective": wf.objective,
            "min_trades": wf.min_trades,
            "param_grid": grid,
        },
    }
    trades = [t for f in folds for t in f.oos_trades]
    return {"metrics": metrics, "equity_curve": stitched, "trades": trades}


_SLIM_KEYS = (
    "return_pct", "max_drawdown_pct", "sharpe", "num_closed_trades", "win_rate_pct", "profit_factor",
    "total_fees", "total_funding", "buy_hold_return_pct", "candles",
)  # fmt: skip


def _slim(metrics: dict[str, Any]) -> dict[str, Any]:
    return {k: metrics.get(k) for k in _SLIM_KEYS}


def _iso(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
