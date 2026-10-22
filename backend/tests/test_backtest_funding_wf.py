"""Backtest funding costs, data.binance.vision archive loader and walk-forward optimization (mocked I/O)."""

import io
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal as D

import httpx
import pytest

from app.backtest import archive
from app.backtest.data import BacktestDataError
from app.backtest.engine import BacktestConfig, run_backtest
from app.backtest.funding import (
    FundingEvent,
    constant_funding,
    fetch_funding,
    funding_summary,
    load_funding_csv,
    normalize,
)
from app.backtest.walkforward import (
    WalkForwardConfig,
    WalkForwardError,
    expand_grid,
    run_walk_forward,
    score,
    split_folds,
)
from app.models.enums import OrderSide
from app.strategy.base import OrderIntent, Strategy, StrategyContext
from tests.factories import HOUR_MS, candles_from_closes, sine_closes
from tests.test_backtest import _auth, _csv

H0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z


class HoldStrategy(Strategy):
    """Opens one position on the first candle and holds it (stop far away)."""

    key = "hold_test"
    name = "hold"

    def __init__(self, side: OrderSide = OrderSide.BUY, qty: str = "1") -> None:
        super().__init__({})
        self.side, self.qty = side, D(qty)
        self.calls: list[int] = []

    def on_candle(self, ctx: StrategyContext) -> list[OrderIntent]:
        self.calls.append(ctx.candles[-1].open_time)
        if ctx.position_qty == 0 and len(self.calls) == 1:
            stop = ctx.price * (D("0.5") if self.side == OrderSide.BUY else D("1.5"))
            return [OrderIntent(self.side, quantity=self.qty, stop_loss=stop, tag="enter")]
        return []


def flat(n: int, price: float = 100.0, start: int = H0) -> list:
    return candles_from_closes([price] * n, spread=0.0001, start=start)


CFG = BacktestConfig(fee_rate=D(0), slippage_bps=D(0), max_risk_per_trade_pct=D(100), max_leverage=5)


# ------------------------------------------------------------------ engine
def test_long_pays_positive_funding_short_receives() -> None:
    candles = flat(48)  # 2 days of 1h -> funding at 00/08/16 UTC
    events = constant_funding(candles[0].open_time, candles[-1].close_time, D("0.001"))
    assert len(events) == 6 and events[0].time == H0
    long = run_backtest(HoldStrategy(OrderSide.BUY), candles, "BTCUSDT", "1h", CFG, funding=events)
    # the position opens at the close of the first candle: the 00:00 event (inside candle 0) is not paid
    assert long.metrics["funding_events_applied"] == 5
    assert long.metrics["total_funding"] == pytest.approx(-5 * 100 * 0.001)
    assert long.metrics["final_equity"] == pytest.approx(10000 - 0.5, abs=0.01)
    short = run_backtest(HoldStrategy(OrderSide.SELL), candles, "BTCUSDT", "1h", CFG, funding=events)
    assert short.metrics["total_funding"] == pytest.approx(+0.5)
    neg = [FundingEvent(e.time, -e.rate) for e in events]
    assert run_backtest(HoldStrategy(OrderSide.BUY), candles, "BTCUSDT", "1h", CFG, funding=neg).metrics[
        "total_funding"
    ] == pytest.approx(0.5)


def test_no_funding_without_position_or_events() -> None:
    candles = flat(30)

    class Idle(Strategy):
        key, name = "idle", "idle"

        def on_candle(self, ctx):
            return []

    events = constant_funding(candles[0].open_time, candles[-1].close_time, D("0.01"))
    res = run_backtest(Idle({}), candles, "BTCUSDT", "1h", CFG, funding=events)
    assert res.metrics["total_funding"] == 0 and res.metrics["funding_events_applied"] == 0
    assert run_backtest(HoldStrategy(), candles, "BTCUSDT", "1h", CFG).metrics["total_funding"] == 0


def test_funding_price_is_interpolated_inside_long_candles() -> None:
    # one 1d candle from 100 to 200; events at 08:00 and 16:00 while holding (position opened on candle 0)
    day = 86_400_000
    from app.strategy.base import Candle

    candles = [
        Candle(H0, 100, 100, 100, 100, 1, H0 + day - 1),
        Candle(H0 + day, 100, 200, 100, 200, 1, H0 + 2 * day - 1),
    ]
    events = [FundingEvent(H0 + day + 8 * HOUR_MS, D("0.01")), FundingEvent(H0 + day + 16 * HOUR_MS, D("0.01"))]
    res = run_backtest(HoldStrategy(), candles, "BTCUSDT", "1d", CFG, funding=events)
    # ~133.3 and ~166.7 -> 3.0 paid in total
    assert res.metrics["total_funding"] == pytest.approx(-3.0, abs=0.01)


def test_start_index_uses_history_only_for_indicators() -> None:
    candles = flat(50)
    strat = HoldStrategy()
    res = run_backtest(strat, candles, "BTCUSDT", "1h", CFG, start_index=20)
    assert strat.calls[0] == candles[20].open_time and len(strat.calls) == 30
    assert res.metrics["candles"] == 30 and len(res.equity_curve) == 30
    with pytest.raises(ValueError):
        run_backtest(HoldStrategy(), candles, "BTCUSDT", "1h", CFG, start_index=50)


# ------------------------------------------------------------------ funding data
def test_load_funding_csv_formats_and_errors() -> None:
    api = load_funding_csv("fundingTime,fundingRate\n1704067200000,0.0001\n1704096000000,-0.0002\n")
    assert [e.rate for e in api] == [D("0.0001"), D("-0.0002")]
    arch = load_funding_csv("calc_time,funding_interval_hours,last_funding_rate\n1704067200000,8,0.00037409\n")
    assert arch == [FundingEvent(1704067200000, D("0.00037409"))]
    iso = load_funding_csv("time,rate\n2024-01-01T08:00:00Z,0.0001\n")
    assert iso[0].time == H0 + 8 * HOUR_MS
    with pytest.raises(BacktestDataError):
        load_funding_csv("a,b\n1,2\n")
    with pytest.raises(BacktestDataError):
        load_funding_csv("fundingTime,fundingRate\nxx,0.1\n")
    with pytest.raises(BacktestDataError):
        load_funding_csv("")


def test_constant_funding_alignment_and_summary() -> None:
    ev = constant_funding(H0 + 1, H0 + 24 * HOUR_MS, D("0.0001"))
    assert [e.time for e in ev] == [H0 + 8 * HOUR_MS, H0 + 16 * HOUR_MS, H0 + 24 * HOUR_MS]
    s = funding_summary(ev, "constant")
    assert s["funding_events"] == 3 and s["funding_source"] == "constant" and s["funding_first"].startswith("2024")
    assert funding_summary([], "none") == {"funding_source": "none", "funding_events": 0}
    rows = [{"fundingTime": 5, "fundingRate": "0.1"}, {"fundingTime": 5, "fundingRate": "0.2"}, {"bad": 1}]
    assert normalize(rows) == [FundingEvent(5, D("0.2"))]


async def test_fetch_funding_pages() -> None:
    class Client:
        def __init__(self):
            self.calls = []

        async def get_funding_rate_history(self, symbol, start_time=None, end_time=None, limit=100):
            self.calls.append(start_time)
            step = 8 * HOUR_MS
            first = H0 + -(-(start_time - H0) // step) * step
            rows = [{"fundingTime": t, "fundingRate": "0.0001"} for t in range(first, end_time + 1, step)]
            return rows[:limit]

    c = Client()
    end = H0 + 1500 * 8 * HOUR_MS
    events = await fetch_funding(c, "BTCUSDT", H0, end)
    assert len(events) == 1501 and len(c.calls) == 2


# ------------------------------------------------------------------ archive (mocked data.binance.vision)
def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def _kline_rows(start: int, n: int, header: bool = True) -> str:
    rows = [archive.KLINE_HEADER] if header else []
    for i in range(n):
        t = start + i * HOUR_MS
        rows.append(f"{t},100,101,99,100.5,10,{t + HOUR_MS - 1},1000,5,5,500,0")
    return "\n".join(rows)


class ArchiveServer:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        body = self.files.get(request.url.path)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    @property
    def transport(self):
        return httpx.MockTransport(self)


async def test_archive_candles_monthly_and_daily() -> None:
    jan = "/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip"
    feb1 = "/data/futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2024-02-01.zip"
    feb2 = "/data/futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2024-02-02.zip"
    feb_start = H0 + 31 * 24 * HOUR_MS
    srv = ArchiveServer(
        {
            jan: _zip("a.csv", _kline_rows(H0, 31 * 24, header=False)),  # old files have no header
            feb1: _zip("b.csv", _kline_rows(feb_start, 24)),
            feb2: _zip("c.csv", _kline_rows(feb_start + 24 * HOUR_MS, 24)),
        }
    )
    candles = await archive.fetch_archive_candles(
        "BTCUSDT", "1h", datetime(2024, 1, 31, tzinfo=UTC), datetime(2024, 2, 3, tzinfo=UTC),
        transport=srv.transport, today=date(2024, 2, 3),
    )  # fmt: skip
    assert len(candles) == 24 + 48  # Jan 31 + Feb 1-2 (Feb 3 is "today": not published)
    assert candles[0].open_time == H0 + 30 * 24 * HOUR_MS
    assert "/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-02.zip" not in srv.paths  # current month


async def test_archive_funding_and_errors() -> None:
    url = "/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"
    text = "calc_time,funding_interval_hours,last_funding_rate\n" + "\n".join(
        f"{H0 + i * 8 * HOUR_MS},8,0.0001" for i in range(93)
    )
    srv = ArchiveServer({url: _zip("f.csv", text)})
    events = await archive.fetch_archive_funding("BTCUSDT", H0, H0 + 40 * 24 * HOUR_MS, transport=srv.transport)
    assert len(events) == 93  # February missing (404) -> skipped
    bad = ArchiveServer({url: b"not a zip"})
    with pytest.raises(BacktestDataError):
        await archive.fetch_archive_funding("BTCUSDT", H0, H0 + HOUR_MS, transport=bad.transport)

    def boom(request):
        return httpx.Response(500)

    with pytest.raises(BacktestDataError):
        await archive.fetch_archive_funding("BTCUSDT", H0, H0 + HOUR_MS, transport=httpx.MockTransport(boom))


# ------------------------------------------------------------------ walk-forward
def test_split_folds_rolling_and_anchored() -> None:
    folds = split_folds(1000, WalkForwardConfig(folds=4, train_ratio=0.7), warmup=40)
    test = folds[0].oos_end - folds[0].oos_start
    train = folds[0].is_end - folds[0].is_start
    assert len(folds) == 4 and folds[0].is_start == 40 and folds[-1].oos_end == 1000
    assert train / (train + test) == pytest.approx(0.7, abs=0.02)
    assert all(f.is_end == f.oos_start for f in folds)
    assert all(b.oos_start == a.oos_end for a, b in zip(folds, folds[1:]))
    anchored = split_folds(1000, WalkForwardConfig(folds=3, anchored=True), warmup=10)
    assert all(f.is_start == 10 for f in anchored)
    with pytest.raises(WalkForwardError):
        split_folds(60, WalkForwardConfig(folds=8), warmup=20)
    with pytest.raises(WalkForwardError):
        split_folds(1000, WalkForwardConfig(train_ratio=0.99), warmup=0)


def test_expand_grid_and_score() -> None:
    assert expand_grid({"a": 1}, None) == [{"a": 1}]
    combos = expand_grid({"a": 1, "b": 2}, {"b": [3, 4], "c": [5]})
    assert combos == [{"a": 1, "b": 3, "c": 5}, {"a": 1, "b": 4, "c": 5}]
    with pytest.raises(WalkForwardError):
        expand_grid({}, {"a": list(range(20)), "b": list(range(20))})
    with pytest.raises(WalkForwardError):
        expand_grid({}, {"a": []})
    assert score({"return_pct": 10, "max_drawdown_pct": 5}, "calmar") == 2
    assert score({"profit_factor": None, "num_closed_trades": 2}, "profit_factor") == 10.0


def test_walk_forward_optimizes_and_reports_oos() -> None:
    closes = sine_closes(1200, base=100, amp=10, period=60)
    candles = candles_from_closes(closes)
    grid = {"fast": [5, 8], "slow": [20, 30]}
    rep = run_walk_forward(
        "ema_cross", {}, candles, "BTCUSDT", "1h", wf=WalkForwardConfig(folds=3), param_grid=grid,
        funding=constant_funding(candles[0].open_time, candles[-1].close_time, D("0.0001")),
    )  # fmt: skip
    m = rep["metrics"]
    assert m["mode"] == "walk_forward" and m["combinations"] == 4 and len(m["folds"]) == 3
    for f in m["folds"]:
        assert f["best_params"]["fast"] in (5, 8) and f["best_params"]["slow"] in (20, 30)
        assert f["candidates"] == 4 and f["oos"]["candles"] > 0 and f["is"]["candles"] > 0
        assert f["is_end"] < f["oos_start"]
    assert m["oos_candles"] == sum(f["oos"]["candles"] for f in m["folds"])
    assert len(rep["equity_curve"]) == m["oos_candles"]
    assert m["final_equity"] == pytest.approx(rep["equity_curve"][-1]["equity"], abs=0.01)
    # stitched curve: compounded fold returns
    prod = 1.0
    for f in m["folds"]:
        prod *= 1 + f["oos"]["return_pct"] / 100
    assert m["return_pct"] == pytest.approx((prod - 1) * 100, abs=0.05)
    assert 0 <= m["param_stability"] <= 1 and "efficiency" in m and m["total_funding"] != 0


def test_walk_forward_validation_mode_and_budget() -> None:
    candles = candles_from_closes(sine_closes(600, 100, 8, 50))
    rep = run_walk_forward("ema_cross", {"fast": 5, "slow": 20}, candles, "BTCUSDT", "1h",
                           wf=WalkForwardConfig(folds=2))  # fmt: skip
    assert rep["metrics"]["mode"] == "validation" and rep["metrics"]["param_stability"] == 1.0
    assert all(f["best_params"]["fast"] == 5 for f in rep["metrics"]["folds"])
    with pytest.raises(WalkForwardError, match="pesado"):
        run_walk_forward("ema_cross", {}, candles, "BTCUSDT", "1h",
                         wf=WalkForwardConfig(folds=2, max_candle_steps=100))  # fmt: skip
    with pytest.raises(WalkForwardError, match="objective"):
        run_walk_forward("ema_cross", {}, candles, "BTCUSDT", "1h", wf=WalkForwardConfig(objective="x"))
    with pytest.raises(WalkForwardError, match="inválidos"):
        run_walk_forward("ema_cross", {}, candles, "BTCUSDT", "1h", param_grid={"fast": [-1]})


# ------------------------------------------------------------------ API
async def test_api_backtest_with_constant_and_csv_funding(client, db) -> None:
    headers = await _auth(db)
    csv = _csv(sine_closes(300, 100, 10, 40))
    base = {"strategy_key": "ema_cross", "params": {"fast": 5, "slow": 20}, "candles_csv": csv}
    r = await client.post("/api/v1/strategies/backtest", json=base, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["config"]["funding_source"] == "none" and r.json()["metrics"]["total_funding"] == 0
    r = await client.post(
        "/api/v1/strategies/backtest",
        json={**base, "funding": {"mode": "constant", "constant_rate": 0.001}},
        headers=headers,
    )
    body = r.json()
    assert body["config"]["funding_source"] == "constant" and body["config"]["funding_events"] > 0
    assert body["metrics"]["funding_events_applied"] > 0
    r = await client.post(
        "/api/v1/strategies/backtest",
        json={**base, "funding": {"mode": "csv", "csv": "fundingTime,fundingRate\nbad,1\n"}},
        headers=headers,
    )
    assert r.status_code == 422


async def test_api_archive_source_and_walk_forward(client, db, monkeypatch) -> None:
    from app.api.v1.endpoints import strategies as ep

    n = 31 * 24
    jan_k = "/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip"
    jan_f = "/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"
    closes = sine_closes(n, 100, 10, 60)
    rows = [archive.KLINE_HEADER] + [
        f"{H0 + i * HOUR_MS},{c},{c * 1.002},{c * 0.998},{c},10,{H0 + (i + 1) * HOUR_MS - 1},0,0,0,0,0"
        for i, c in enumerate(closes)
    ]
    funding_csv = "calc_time,funding_interval_hours,last_funding_rate\n" + "\n".join(
        f"{H0 + i * 8 * HOUR_MS},8,0.0002" for i in range(93)
    )
    srv = ArchiveServer({jan_k: _zip("k.csv", "\n".join(rows)), jan_f: _zip("f.csv", funding_csv)})
    monkeypatch.setattr(ep, "archive_transport", lambda: srv.transport)
    headers = await _auth(db)
    body = {
        "strategy_key": "ema_cross",
        "params": {"fast": 5, "slow": 20},
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-01-31T23:00:00Z",
        "data_source": "archive",
        "funding": {"mode": "archive"},
    }
    r = await client.post("/api/v1/strategies/backtest", json=body, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["config"]["data_source"] == "archive" and r.json()["config"]["funding_events"] == 93
    assert r.json()["metrics"]["candles"] == n

    wf = {**body, "param_grid": {"fast": [5, 8], "slow": [20, 30]}, "walk_forward": {"folds": 3, "train_ratio": 0.6}}
    r = await client.post("/api/v1/strategies/walk-forward", json=wf, headers=headers)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["config"]["mode"] == "walk_forward" and len(run["metrics"]["folds"]) == 3
    assert run["config"]["walk_forward"]["param_grid"] == {"fast": [5, 8], "slow": [20, 30]}
    assert run["params"] == run["metrics"]["folds"][-1]["best_params"]
    listed = (await client.get("/api/v1/strategies/backtests", headers=headers)).json()
    assert {x["id"] for x in listed} >= {run["id"]}

    r = await client.post("/api/v1/strategies/walk-forward", json={**wf, "param_grid": {"nope": [1]}}, headers=headers)
    assert r.status_code == 422
    r = await client.post(
        "/api/v1/strategies/walk-forward", json={**wf, "walk_forward": {"folds": 12}}, headers=headers
    )
    assert r.status_code in (201, 422)


async def test_api_auto_funding_falls_back_to_archive_when_rest_is_blocked(client, db, monkeypatch) -> None:
    from app.api.v1.endpoints import strategies as ep
    from app.services.binance_client import BinanceFuturesClient

    candles = candles_from_closes(sine_closes(120, 100, 5, 30), start=H0)
    rows = [[c.open_time, str(c.open), str(c.high), str(c.low), str(c.close), "1", c.close_time] for c in candles]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/fundingRate":
            return httpx.Response(451, json={"code": 0, "msg": "Service unavailable from a restricted location"})
        return httpx.Response(200, json=rows)

    monkeypatch.setattr(
        ep, "market_client_factory",
        lambda testnet: BinanceFuturesClient("", "", testnet=testnet, transport=httpx.MockTransport(handler)),
    )  # fmt: skip
    jan_f = "/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"
    srv = ArchiveServer({jan_f: _zip("f.csv", "fundingTime,fundingRate\n" + f"{H0 + 8 * HOUR_MS},0.0001\n")})
    monkeypatch.setattr(ep, "archive_transport", lambda: srv.transport)
    headers = await _auth(db)
    body = {"strategy_key": "ema_cross", "params": {"fast": 5, "slow": 20},
            "start": "2024-01-01T00:00:00Z", "end": "2024-01-06T00:00:00Z"}  # fmt: skip
    r = await client.post("/api/v1/strategies/backtest", json=body, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["config"]["funding_source"] == "archive" and r.json()["config"]["funding_events"] == 1
    r = await client.post(
        "/api/v1/strategies/backtest", json={**body, "funding": {"mode": "exchange"}}, headers=headers
    )
    assert r.status_code == 502
