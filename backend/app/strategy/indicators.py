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


def highest(values: Sequence[float], period: int) -> Series:
    """Rolling max over `period` values ending at each index (inclusive)."""
    _check_period(period)
    return [None if i + 1 < period else max(values[i + 1 - period : i + 1]) for i in range(len(values))]


def lowest(values: Sequence[float], period: int) -> Series:
    _check_period(period)
    return [None if i + 1 < period else min(values[i + 1 - period : i + 1]) for i in range(len(values))]


def donchian(highs: Sequence[float], lows: Sequence[float], period: int = 20) -> tuple[Series, Series, Series]:
    """Donchian channel -> (upper, middle, lower)."""
    up, lo = highest(highs, period), lowest(lows, period)
    mid: Series = [None if a is None or b is None else (a + b) / 2 for a, b in zip(up, lo)]
    return up, mid, lo


def _wilder(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> tuple[Series, Series, Series]:
    """Wilder's ADX -> (adx, +DI, -DI). Values are None during warm-up (about 2*period candles)."""
    _check_period(period)
    n = len(closes)
    none: Series = [None] * n
    if n < period + 1:
        return none, list(none), list(none)
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, n):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr = true_range(highs, lows, closes)
    # Wilder smoothing starting at index 1 (index 0 has no previous candle)
    atr_s, pdm_s, mdm_s = _wilder(tr[1:], period), _wilder(plus_dm[1:], period), _wilder(minus_dm[1:], period)
    pdi: Series = [None]
    mdi: Series = [None]
    dx: list[float | None] = [None]
    for a, p, m in zip(atr_s, pdm_s, mdm_s):
        if a is None or p is None or m is None or a == 0:
            pdi.append(None)
            mdi.append(None)
            dx.append(None)
            continue
        pv, mv = 100 * p / a, 100 * m / a
        pdi.append(pv)
        mdi.append(mv)
        dx.append(0.0 if pv + mv == 0 else 100 * abs(pv - mv) / (pv + mv))
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    out: Series = [None] * n
    if first is not None:
        tail = _wilder([v for v in dx[first:] if v is not None], period)
        out[first:] = tail
    return out, pdi, mdi


def supertrend(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 10, multiplier: float = 3.0
) -> tuple[Series, list[int]]:
    """Supertrend -> (line, direction) with direction 1 (up/bullish), -1 (down) or 0 (warm-up)."""
    a = atr(highs, lows, closes, period)
    n = len(closes)
    line: Series = [None] * n
    direction = [0] * n
    fu = fl = None  # final upper / lower bands
    for i in range(n):
        if a[i] is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2
        bu, bl = hl2 + multiplier * a[i], hl2 - multiplier * a[i]  # type: ignore[operator]
        prev_close = closes[i - 1] if i > 0 else closes[i]
        fu = bu if fu is None or bu < fu or prev_close > fu else fu
        fl = bl if fl is None or bl > fl or prev_close < fl else fl
        prev_dir = direction[i - 1] if i > 0 else 0
        if prev_dir == 0:
            d = 1 if closes[i] >= hl2 else -1
        elif prev_dir == 1:
            d = -1 if closes[i] < fl else 1
        else:
            d = 1 if closes[i] > fu else -1
        direction[i] = d
        line[i] = fl if d == 1 else fu
    return line, direction


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
