"""Market scanner (mocked Binance public endpoints), scanner API and auto symbol selection for bots."""

import httpx
import pytest
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models import AccountStatus, BotState
from app.models.scanner import MarketScan
from app.models.trading import Position
from app.services import scanner
from app.services.binance_client import BinanceFuturesClient
from app.services.bot_runner import run_bot_tick
from app.services.execution import PaperExecutor
from app.services.market_data import StaticMarketData
from tests.factories import candles_from_closes, make_account, make_bot, make_user_workspace, sine_closes
from tests.test_api_e2e import bearer, register_login


def kline_rows(closes, volume_last=100.0):
    rows = []
    for i, c in enumerate(candles_from_closes(closes)):
        vol = volume_last if i == len(closes) - 1 else c.volume
        rows.append([c.open_time, str(c.open), str(c.high), str(c.low), str(c.close), str(vol), c.close_time])
    return rows


SERIES = {
    "BTCUSDT": [100 + i * 0.8 for i in range(150)],  # strong uptrend
    "ETHUSDT": sine_closes(150, amp=3, period=20),  # range
    "SOLUSDT": [200 - i * 0.9 for i in range(150)],  # downtrend
}


def binance_mock():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/fapi/v1/exchangeInfo":
            sym = lambda s, q="USDT", ct="PERPETUAL", st="TRADING": {  # noqa: E731
                "symbol": s, "quoteAsset": q, "contractType": ct, "status": st,
            }  # fmt: skip
            return httpx.Response(200, json={"symbols": [
                sym("BTCUSDT"), sym("ETHUSDT"), sym("SOLUSDT"), sym("DOGEUSDT"),
                sym("BTCBUSD", q="BUSD"), sym("BTCUSDT_240628", ct="CURRENT_QUARTER"), sym("OLDUSDT", st="SETTLING"),
            ]})  # fmt: skip
        if path == "/fapi/v1/ticker/24hr":
            vol = {"BTCUSDT": 9e9, "ETHUSDT": 5e9, "SOLUSDT": 1e9, "DOGEUSDT": 1e6, "BTCBUSD": 9e9, "OLDUSDT": 9e9}
            return httpx.Response(
                200, json=[{"symbol": s, "quoteVolume": str(v), "priceChangePercent": "1.5"} for s, v in vol.items()]
            )
        if path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "lastFundingRate": "0.0005"}])
        if path == "/fapi/v1/klines":
            s = request.url.params["symbol"]
            return httpx.Response(200, json=kline_rows(SERIES[s], volume_last=400.0 if s == "BTCUSDT" else 100.0))
        return httpx.Response(404, json={"code": -1, "msg": "not mocked"})

    return BinanceFuturesClient("", "", testnet=False, transport=httpx.MockTransport(handler)), calls


@pytest.fixture(autouse=True)
def _clear_cache():
    scanner._memory_cache.clear()
    yield
    scanner._memory_cache.clear()


async def test_run_scan_filters_universe_and_scores() -> None:
    client, calls = binance_mock()
    out = await scanner.run_scan(client, scanner.ScanParams(min_quote_volume=1e8, max_symbols=10))
    await client.close()
    symbols = [r["symbol"] for r in out["results"]]
    assert set(symbols) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}  # no low-volume, non-USDT, quarterly or settling
    by = {r["symbol"]: r for r in out["results"]}
    btc, sol, eth = by["BTCUSDT"], by["SOLUSDT"], by["ETHUSDT"]
    assert btc["trend"] == "up" and sol["trend"] == "down"
    assert btc["volume_spike"] > 3 and btc["funding_rate_pct"] == pytest.approx(0.05)
    assert btc["score"] > eth["score"]
    assert out["results"][0]["rank"] == 1 and all(0 <= r["score"] <= 100 for r in out["results"])
    assert set(btc["components"]) == set(scanner.DEFAULT_WEIGHTS)
    assert calls.count("/fapi/v1/klines") == 3

    # klines are cached (memory fallback because Redis is unreachable in tests)
    client2, calls2 = binance_mock()
    await scanner.run_scan(client2, scanner.ScanParams(min_quote_volume=1e8))
    await client2.close()
    assert "/fapi/v1/klines" not in calls2


def test_filter_results() -> None:
    def row(sym, score, vol, trend, bias, signals, rsi_v):
        return {"symbol": sym, "score": score, "quote_volume": vol, "trend": trend, "bias": bias,
                "signals": signals, "rsi": rsi_v}  # fmt: skip

    rows = [
        row("AUSDT", 50, 1e9, "up", "long", {"x": "long"}, None),
        row("BUSDT", 70, 1e8, "down", "short", {}, 40),
        row("CUSDT", 10, 5e9, "flat", "neutral", {}, 60),
    ]
    f = scanner.filter_results
    assert [r["symbol"] for r in f(rows)] == ["BUSDT", "AUSDT", "CUSDT"]
    assert [r["symbol"] for r in f(rows, sort="quote_volume", desc=False)] == ["BUSDT", "AUSDT", "CUSDT"]
    assert [r["symbol"] for r in f(rows, sort="rsi")] == ["CUSDT", "BUSDT", "AUSDT"]  # None last
    assert [r["symbol"] for r in f(rows, min_quote_volume=5e8, trend="up")] == ["AUSDT"]
    assert [r["symbol"] for r in f(rows, signal="x")] == ["AUSDT"]
    assert [r["symbol"] for r in f(rows, search="cu", limit=1)] == ["CUSDT"]
    assert f(rows, sort="__class__")[0]["symbol"] == "BUSDT"  # unknown sort -> score


async def test_store_and_api(client, monkeypatch) -> None:
    mock, _ = binance_mock()
    monkeypatch.setattr(scanner.settings, "SCANNER_MIN_QUOTE_VOLUME", 1e8)
    monkeypatch.setattr(scanner.settings, "SCANNER_KEEP_SNAPSHOTS", 1)
    h = bearer(await register_login(client, "scan@example.com"))

    r = await client.get("/api/v1/scanner/", headers=h)
    assert r.status_code == 200 and r.json()["results"] == [] and r.json()["scanned_at"] is None

    await scanner.run_and_store(AsyncSessionLocal, mock)
    await scanner.run_and_store(AsyncSessionLocal, mock)
    await mock.close()
    async with AsyncSessionLocal() as s:
        assert len((await s.scalars(select(MarketScan))).all()) == 1  # pruned to SCANNER_KEEP_SNAPSHOTS

    r = await client.get("/api/v1/scanner/", headers=h)
    body = r.json()
    assert body["symbols_scanned"] == 3 and body["results"][0]["symbol"] == "BTCUSDT"
    r = await client.get("/api/v1/scanner/?trend=down&sort=quote_volume&order=asc", headers=h)
    assert [x["symbol"] for x in r.json()["results"]] == ["SOLUSDT"]
    assert (await client.get("/api/v1/scanner/?trend=sideways", headers=h)).status_code == 422
    r = await client.post("/api/v1/scanner/refresh", headers=h)
    assert r.status_code == 202 and r.json() == {"queued": False}  # Celery dispatch disabled in tests


# ------------------------------------------------------------------ auto symbol selection
BREAKOUT = [100 + (1 if i % 2 else -1) for i in range(30)] + [106.0]
DONCHIAN = {"entry_period": 10, "exit_period": 5, "atr_period": 5}


async def _store_scan(db, symbols):
    results = [{"symbol": s, "score": 90 - i, "signals": {}} for i, s in enumerate(symbols)]
    db.add(MarketScan(testnet=False, interval="1h", symbols_scanned=len(symbols), params={}, results=results))
    await db.flush()


async def test_auto_symbol_bot_trades_top_symbols_with_position_limit(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    bot = await make_bot(db, acc, "donchian_breakout", DONCHIAN,
                         options={"auto_symbols": {"enabled": True, "top_n": 3, "max_positions": 2}})  # fmt: skip
    await _store_scan(db, ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"])
    await db.commit()
    market = StaticMarketData({s: candles_from_closes(BREAKOUT) for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT")})
    res = await run_bot_tick(db, bot, acc, market, executor=PaperExecutor())
    assert res.status == "ok", res
    assert res.filled == 2 and res.reasons == ["max_concurrent_positions"]
    positions = {p.symbol: p.quantity for p in (await db.scalars(select(Position))).all() if p.quantity != 0}
    assert set(positions) == {"AAAUSDT", "BBBUSDT"}
    assert bot.runtime["auto"] == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    assert set(bot.runtime["symbols"]) == {"AAAUSDT", "BBBUSDT", "CCCUSDT"}

    # next scan: held positions stay managed even if they leave the top-N; no new candle -> skipped
    await _store_scan(db, ["ZZZUSDT"])
    await db.commit()
    res = await run_bot_tick(db, bot, acc, market, executor=PaperExecutor())
    assert res.status == "skipped"


async def test_auto_symbol_bot_without_scan_is_skipped(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    bot = await make_bot(db, acc, "supertrend", {}, options={"auto_symbols": {"enabled": True}})
    await db.commit()
    res = await run_bot_tick(db, bot, acc, StaticMarketData(), executor=PaperExecutor())
    assert res.status == "skipped" and res.message == "no_symbols"
    assert acc.status == AccountStatus.RUNNING and bot.state == BotState.RUNNING


async def test_bot_api_validates_options(client) -> None:
    h = bearer(await register_login(client, "auto@example.com"))
    acc = (await client.post("/api/v1/accounts/", json={"label": "p", "api_key": "k" * 16, "api_secret": "s" * 16},
                             headers=h)).json()  # fmt: skip
    base = {"account_id": acc["id"], "name": "auto", "strategy_key": "supertrend", "symbol": "BTCUSDT"}
    r = await client.post("/api/v1/bots/", json={**base, "options": {"auto_symbols": {"enabled": True, "top_n": 99}}},
                          headers=h)  # fmt: skip
    assert r.status_code == 422
    r = await client.post("/api/v1/bots/", json={**base, "options": {"bogus": 1}}, headers=h)
    assert r.status_code == 422
    r = await client.post("/api/v1/bots/", json={**base, "options": {"auto_symbols": {"enabled": True, "top_n": 5}}},
                          headers=h)  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["options"]["auto_symbols"] == {
        "enabled": True, "top_n": 5, "max_positions": 2, "min_score": 0.0, "prefer_signals": True,
    }  # fmt: skip
    r = await client.patch(f"/api/v1/bots/{r.json()['id']}", json={"options": {}}, headers=h)
    assert r.status_code == 200 and r.json()["options"]["auto_symbols"]["enabled"] is False
