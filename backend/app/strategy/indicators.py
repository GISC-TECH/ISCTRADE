"""Basic technical indicators (pure Python, placeholder)."""
from collections.abc import Sequence


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        out.append(None if i + 1 < period else sum(values[i + 1 - period : i + 1]) / period)
    return out


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out
