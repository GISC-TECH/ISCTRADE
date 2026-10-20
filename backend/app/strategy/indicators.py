"""Technical indicators (pure Python, no numpy).

All functions return a list aligned with the input; warm-up positions are `None`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Series = list[float | None]


def sma(values: Sequence[float], period: int) -> Series:
    _check_period(period)
    out: Series = [None] * len(values)
    window = 0.0
    for i, v in enumerate(values):
        window += v
        if i >= period:
            window -= values[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """Exponential moving average seeded with the SMA of the first `period` values."""
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def stdev(values: Sequence[float], period: int) -> Series:
    """Rolling population standard deviation."""
    _check_period(period)
    out: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        out[i] = math.sqrt(sum((x - mean) ** 2 for x in window) / period)
    return out


def rsi(values: Sequence[float], period: int = 14) -> Series:
    """Wilder's RSI (0-100)."""
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    out: list[float] = []
    for i in range(len(closes)):
        if i == 0:
            out.append(highs[0] - lows[0])
        else:
            prev = closes[i - 1]
            out.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Series:
    """Wilder's Average True Range."""
    _check_period(period)
    tr = true_range(highs, lows, closes)
    out: Series = [None] * len(tr)
    if len(tr) < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def bollinger(values: Sequence[float], period: int = 20, num_std: float = 2.0) -> tuple[Series, Series, Series]:
    """Bollinger Bands -> (middle, upper, lower)."""
    mid = sma(values, period)
    sd = stdev(values, period)
    upper: Series = [None if m is None else m + num_std * s for m, s in zip(mid, sd)]  # type: ignore[operator]
    lower: Series = [None if m is None else m - num_std * s for m, s in zip(mid, sd)]  # type: ignore[operator]
    return mid, upper, lower


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Series, Series, Series]:
    """MACD -> (macd line, signal line, histogram)."""
    if fast >= slow:
        raise ValueError("fast period must be smaller than slow period")
    ef, es = ema(values, fast), ema(values, slow)
    line: Series = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    sig: Series = [None] * len(values)
    if first is not None:
        tail = ema([v for v in line[first:] if v is not None], signal)
        sig[first:] = tail
    hist: Series = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def crossed_above(fast: Series, slow: Series, i: int = -1) -> bool:
    """True if `fast` crossed above `slow` at index i (compared to i-1)."""
    a0, b0, a1, b1 = fast[i - 1], slow[i - 1], fast[i], slow[i]
    if None in (a0, b0, a1, b1):
        return False
    return a0 <= b0 and a1 > b1  # type: ignore[operator]


def crossed_below(fast: Series, slow: Series, i: int = -1) -> bool:
    a0, b0, a1, b1 = fast[i - 1], slow[i - 1], fast[i], slow[i]
    if None in (a0, b0, a1, b1):
        return False
    return a0 >= b0 and a1 < b1  # type: ignore[operator]


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError("period must be >= 1")
