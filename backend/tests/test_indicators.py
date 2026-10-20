import pytest

from app.strategy.indicators import atr, bollinger, crossed_above, crossed_below, ema, macd, rsi, sma, stdev


def test_sma() -> None:
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_seeded_with_sma() -> None:
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)  # 4*0.5 + 2*0.5
    assert out[4] == pytest.approx(4.0)


def test_stdev_and_bollinger() -> None:
    values = [2, 4, 4, 4, 5, 5, 7, 9]
    assert stdev(values, 8)[-1] == pytest.approx(2.0)
    mid, up, lo = bollinger(values, 8, 2)
    assert mid[-1] == pytest.approx(5.0)
    assert up[-1] == pytest.approx(9.0)
    assert lo[-1] == pytest.approx(1.0)


def test_rsi_extremes_and_range() -> None:
    assert rsi(list(range(1, 30)), 14)[-1] == pytest.approx(100.0)
    assert rsi(list(range(30, 1, -1)), 14)[-1] == pytest.approx(0.0)
    values = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    r = rsi(values, 14)
    assert r[13] is None
    assert r[14] == pytest.approx(70.46, abs=0.1)  # classic Wilder example


def test_atr_constant_range() -> None:
    highs = [11.0] * 20
    lows = [9.0] * 20
    closes = [10.0] * 20
    out = atr(highs, lows, closes, 14)
    assert out[12] is None
    assert out[13] == pytest.approx(2.0)
    assert out[-1] == pytest.approx(2.0)


def test_macd_shapes() -> None:
    values = [float(i) for i in range(60)]
    line, sig, hist = macd(values)
    assert len(line) == len(sig) == len(hist) == 60
    assert line[24] is None and line[25] is not None
    assert hist[-1] == pytest.approx(0.0, abs=1e-6)  # linear series -> MACD converges


def test_crosses() -> None:
    assert crossed_above([1, 3], [2, 2])
    assert not crossed_above([3, 3], [2, 2])
    assert crossed_below([3, 1], [2, 2])
    assert not crossed_below([None, 1], [2, 2])


def test_invalid_period() -> None:
    with pytest.raises(ValueError):
        sma([1, 2], 0)
