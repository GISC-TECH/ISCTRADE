from decimal import Decimal as D

import httpx
import pytest

from app.backtest.data import BacktestDataError, load_csv
from app.backtest.engine import BacktestConfig, max_drawdown_pct, run_backtest, sharpe_ratio
from app.core.security import create_access_token
from app.strategy.strategies import build_strategy
from tests.factories import candles_from_closes, make_user_workspace, sine_closes

GRID = {"lower_price": 90, "upper_price": 110, "levels": 9, "quantity_per_level": 1, "stop_loss_pct": 5}


def test_grid_profits_in_range_market() -> None:
    candles = candles_from_closes(sine_closes(400, base=100, amp=8, period=40))
    cfg = BacktestConfig(max_risk_per_trade_pct=D(5), fee_rate=D("0.0002"), slippage_bps=D(1))
    res = run_backtest(build_strategy("grid", GRID), candles, "BTCUSDT", "1h", cfg)
    m = res.metrics
    assert m["num_fills"] > 10
    assert m["net_pnl"] > 0
    # net position uses average entry, so individual sells can realize small losses
    assert m["win_rate_pct"] > 50
    assert m["candles"] == 400
    assert len(res.equity_curve) == 400


def test_grid_stop_and_kill_switch_on_crash() -> None:
    closes = [100.0, 99.0, 97.0, 94.0, 92.0, 91.0] + [80.0 - i for i in range(20)]
    cfg = BacktestConfig(max_risk_per_trade_pct=D(5), max_drawdown_pct=D(50))
    res = run_backtest(build_strategy("grid", GRID), candles_from_closes(closes), "BTCUSDT", "1h", cfg)
    tags = [t.tag for t in res.trades]
    assert "stop_loss" in tags or "grid_stop" in tags
    assert res.metrics["net_pnl"] < 0
    # all positions closed afterwards
    qty = D(0)
    for t in res.trades:
        qty += t.quantity if t.side == "BUY" else -t.quantity
    assert qty == 0


def test_ema_cross_trend_following_and_risk_limits() -> None:
    closes = [100 + i * 0.5 for i in range(100)] + [150 - i * 0.7 for i in range(100)] + [80 + i for i in range(60)]
    res = run_backtest(
        build_strategy("ema_cross", {"fast": 5, "slow": 20}), candles_from_closes(closes), "BTCUSDT", "4h"
    )
    assert res.metrics["num_fills"] >= 2
    # never risks more than ~1% of equity per trade -> drawdown stays bounded
    assert res.metrics["max_drawdown_pct"] < 15
    assert res.killed_at is None


def test_metrics_helpers() -> None:
    assert max_drawdown_pct([100, 120, 90, 130]) == pytest.approx(25.0)
    assert sharpe_ratio([100, 100, 100], 3_600_000) == 0.0
    assert sharpe_ratio([100, 101, 102.5, 103, 104.2], 86_400_000) > 0


def test_load_csv_and_errors() -> None:
    csv_text = "open_time,open,high,low,close,volume\n2024-01-01T00:00:00Z,1,2,0.5,1.5,10\n1704070800,1.5,2,1,1.8,5\n"
    candles = load_csv(csv_text, "1h")
    assert len(candles) == 2 and candles[1].open_time == 1704070800000
    assert candles[0].close_time == candles[0].open_time + 3_600_000 - 1
    with pytest.raises(BacktestDataError):
        load_csv("a,b\n1,2", "1h")
    with pytest.raises(BacktestDataError):
        load_csv(csv_text, "7m")


def _csv(closes: list[float]) -> str:
    rows = ["open_time,open,high,low,close,volume"]
    for c in candles_from_closes(closes):
        rows.append(f"{c.open_time},{c.open},{c.high},{c.low},{c.close},{c.volume}")
    return "\n".join(rows)


async def _auth(db):
    user, ws = await make_user_workspace(db)
    await db.commit()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}", "X-Workspace-ID": str(ws.id)}


async def test_backtest_api_with_csv(client, db) -> None:
    headers = await _auth(db)
    body = {
        "strategy_key": "grid",
        "params": GRID,
        "symbol": "BTCUSDT",
        "interval": "1h",
        "candles_csv": _csv(sine_closes(200, 100, 8, 40)),
        "config": {"max_risk_per_trade_pct": 5},
    }
    r = await client.post("/api/v1/strategies/backtest", json=body, headers=headers)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "completed" and data["metrics"]["num_fills"] > 0
    r = await client.get("/api/v1/strategies/backtests", headers=headers)
    assert [b["id"] for b in r.json()] == [data["id"]]
    r = await client.get(f"/api/v1/strategies/backtests/{data['id']}", headers=headers)
    assert r.status_code == 200 and len(r.json()["equity_curve"]) == 200


async def test_backtest_api_fetches_klines_from_mocked_binance(client, db, monkeypatch) -> None:
    from app.api.v1.endpoints import strategies as ep
    from app.services.binance_client import BinanceFuturesClient

    candles = candles_from_closes(sine_closes(120, 100, 5, 30))
    rows = [[c.open_time, str(c.open), str(c.high), str(c.low), str(c.close), "1", c.close_time] for c in candles]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/klines"
        return httpx.Response(200, json=rows)

    monkeypatch.setattr(
        ep,
        "market_client_factory",
        lambda testnet: BinanceFuturesClient("", "", testnet=testnet, transport=httpx.MockTransport(handler)),
    )
    headers = await _auth(db)
    body = {
        "strategy_key": "ema_cross",
        "params": {"fast": 5, "slow": 20},
        "start": "2023-11-14T00:00:00Z",
        "end": "2023-11-20T00:00:00Z",
    }
    r = await client.post("/api/v1/strategies/backtest", json=body, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["metrics"]["candles"] == 120


async def test_backtest_api_validation(client, db) -> None:
    headers = await _auth(db)
    r = await client.post("/api/v1/strategies/backtest", json={"strategy_key": "grid"}, headers=headers)
    assert r.status_code == 422
    r = await client.post(
        "/api/v1/strategies/backtest",
        json={"strategy_key": "grid", "params": {"lower_price": 1}, "candles_csv": _csv([1, 2, 3])},
        headers=headers,
    )
    assert r.status_code == 422 and "required" in r.json()["detail"]
