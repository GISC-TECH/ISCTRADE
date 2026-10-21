"""News ingestion, tagging, sentiment (LLM mocked + lexicon), aggregation, gates, calendar and bot filters.

Every external HTTP call (RSS, CryptoPanic, X, LLM, ForexFactory) goes through httpx.MockTransport.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from defusedxml import EntitiesForbidden
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models import BotState
from app.models.audit import AuditLog
from app.models.enums import OrderSide
from app.models.news import EconomicEvent, NewsItem
from app.models.trading import Position
from app.schemas.bot_options import EventPauseOptions, NewsFilterOptions
from app.services.alerts import service as alert_service
from app.services.bot_runner import run_bot_tick
from app.services.execution import PaperExecutor
from app.services.market_data import StaticMarketData
from app.services.news import calendar, sentiment, sources, store, tagging
from app.services.news.gate import EventInfo, NewsSignal, aggregate, event_gate, news_gate
from app.strategy.base import OrderIntent
from tests.factories import candles_from_closes, make_account, make_bot, make_user_workspace
from tests.test_api_e2e import bearer, register_login

NOW = datetime.now(UTC)
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>Bitcoin ETF approved as $SOL and ETH rally</title><link>https://www.coindesk.com/a/1?utm=x</link>
<description>&lt;p&gt;Big &lt;b&gt;news&lt;/b&gt; for ONE market&lt;/p&gt;</description>
<pubDate>{d1}</pubDate><guid>a1</guid></item>
<item><title>Exchange hacked, $200M drained in XRP exploit</title><link>https://www.coindesk.com/a/2</link>
<pubDate>{d2}</pubDate></item>
</channel></rss>"""
ATOM = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
<entry><title>Dogecoin jumps after listing</title><link rel="alternate" href="https://decrypt.co/b/1"/>
<id>tag:1</id><updated>{d1}</updated><summary>DOGE up</summary></entry>
<entry><title>Duplicate of coindesk story</title><link href="https://coindesk.com/a/1"/><updated>{d1}</updated></entry>
</feed>"""


def _rfc(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


@pytest.fixture(autouse=True)
def _reset_memory(monkeypatch):
    sentiment._memory_cache.clear()
    sentiment._memory_budget.clear()
    store._memory_last.clear()
    for k in ("LLM_API_KEY", "CRYPTOPANIC_API_TOKEN", "X_BEARER_TOKEN"):
        monkeypatch.setattr(sentiment.settings, k, "")
    yield


@pytest.fixture
def captured(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(alert_service, "dispatch", lambda payload: out.append(payload) or True)
    return out


# ------------------------------------------------------------------ parsing / tagging
def test_parse_rss_and_atom() -> None:
    items = sources.parse_feed(RSS.format(d1=_rfc(NOW), d2=_rfc(NOW)), "https://www.coindesk.com/rss")
    assert [i.title for i in items][0].startswith("Bitcoin ETF approved")
    assert items[0].body == "Big news for ONE market" and items[0].source_name == "coindesk.com"
    assert items[0].external_id == "a1" and items[0].published_at.tzinfo is not None
    atom = sources.parse_feed(ATOM.format(d1=NOW.isoformat()), "https://decrypt.co/feed")
    assert atom[0].url == "https://decrypt.co/b/1" and atom[0].body == "DOGE up"


def test_parse_feed_rejects_entity_expansion() -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        "<rss><item><title>&b;</title></item></rss>"
    )
    with pytest.raises(EntitiesForbidden):
        sources.parse_feed(bomb, "https://evil.example/rss")


def test_tagging() -> None:
    assets, symbols = tagging.tag("Bitcoin ETF approved as $SOL and ETH rally; ONE more thing from the SEC")
    assert assets == ["BTC", "SOL", "ETH"] and symbols == ["BTCUSDT", "SOLUSDT", "ETHUSDT"]
    assert tagging.tag("$NEAR pumps while NEAR stays flat")[0] == ["NEAR"]
    assert tagging.tag("NEAR the top")[0] == []
    universe = {"BTCUSDT", "1000PEPEUSDT", "WIFUSDT"}
    assert tagging.tag("PEPE and WIF memecoins surge, CEO says", universe=universe)[1] == ["1000PEPEUSDT", "WIFUSDT"]
    assert tagging.tag("Cardano news", universe=universe)[1] == []  # ADAUSDT not in the universe
    assert tagging.tag("", hints=["eth"])[1] == ["ETHUSDT"]
    assert tagging.base_asset("1000PEPEUSDT") == "PEPE"


# ------------------------------------------------------------------ sentiment
def test_lexicon_sentiment() -> None:
    neg = sentiment.lexicon_sentiment("Exchange hacked, $200M drained in exploit")
    pos = sentiment.lexicon_sentiment("SEC approves spot ETF, bitcoin surges to record high")
    flat = sentiment.lexicon_sentiment("Weekly market wrap")
    assert neg.sentiment < -0.5 and neg.impact == "high" and neg.method == "lexicon"
    assert pos.sentiment > 0.5 and pos.impact == "high"
    assert flat.sentiment == 0 and flat.impact == "low"
    assert sentiment.lexicon_sentiment("Exchange denies hack rumours").sentiment > neg.sentiment


def test_parse_llm_json() -> None:
    r = sentiment.parse_llm_json('```json\n{"sentiment": -3, "impact": "Alto", "assets": ["$btc", "bad asset!"], '
                                 '"summary_pt": "Queda forte"}\n```')  # fmt: skip
    assert (r.sentiment, r.impact, r.assets, r.summary_pt, r.method) == (-1.0, "high", ["BTC"], "Queda forte", "llm")
    with pytest.raises(ValueError):
        sentiment.parse_llm_json("no json here")


async def test_llm_sentiment_cache_budget_and_privacy(monkeypatch) -> None:
    monkeypatch.setattr(sentiment.settings, "LLM_API_KEY", "sk-test-placeholder")
    monkeypatch.setattr(sentiment.settings, "LLM_API_BASE", "https://llm.example/v1")
    monkeypatch.setattr(sentiment.settings, "LLM_MAX_CALLS_PER_HOUR", 2)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        content = json.dumps({"sentiment": 0.7, "impact": "medium", "assets": ["ETH"], "summary_pt": "Alta do ETH"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        r1 = await sentiment.analyse("Ether rallies", "body", "coindesk.com", http)
        r2 = await sentiment.analyse("Ether rallies", "body", "coindesk.com", http)  # cached
        r3 = await sentiment.analyse("Other story", "x", "coindesk.com", http)
        r4 = await sentiment.analyse("Bitcoin hacked exploit", "", "coindesk.com", http)  # over budget -> lexicon
    assert (r1.method, r1.sentiment, r1.summary_pt) == ("llm", 0.7, "Alta do ETH") and r2 == r1
    assert r3.method == "llm" and r4.method == "lexicon" and r4.sentiment < 0
    assert len(seen) == 2
    req = seen[0]
    assert str(req.url) == "https://llm.example/v1/chat/completions"
    body = json.loads(req.content)
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0
    sent_text = json.dumps(body["messages"])
    assert "Ether rallies" in sent_text and "sk-test" not in sent_text  # key only in the auth header


async def test_llm_failure_falls_back_to_lexicon(monkeypatch, caplog) -> None:
    monkeypatch.setattr(sentiment.settings, "LLM_API_KEY", "sk-test-placeholder")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        r = await sentiment.analyse("Bitcoin surges", "", "x", http)
    assert r.method == "lexicon" and r.sentiment > 0
    assert "sk-test" not in caplog.text


# ------------------------------------------------------------------ aggregation & gates
def _sig(hours_ago: float, s: float, impact="medium", sym="BTCUSDT") -> NewsSignal:
    return NewsSignal(NOW - timedelta(hours=hours_ago), s, impact, (sym,), "t")


def test_aggregate_time_decay_and_future_items() -> None:
    sigs = [_sig(0, -0.8), _sig(12, 0.8), _sig(-1, 1.0), _sig(1, 0.5, sym="ETHUSDT"), _sig(100, 1.0)]
    a = aggregate(sigs, "BTCUSDT", NOW, half_life_hours=6, window_hours=48)
    assert a.count == 2  # future item, other symbol and out-of-window item ignored
    assert -0.8 < a.score < -0.4  # recent negative dominates (weights 1 vs 0.25)
    assert a.high_impact_negative is None
    hi = aggregate([_sig(0.5, -0.6, "high")], "BTCUSDT", NOW, high_impact_minutes=60)
    assert hi.high_impact_negative is not None
    assert aggregate([_sig(2, -0.6, "high")], "BTCUSDT", NOW, high_impact_minutes=60).high_impact_negative is None


def test_news_gate_modes() -> None:
    bearish = aggregate([_sig(0, -0.9), _sig(0.1, -0.7)], "BTCUSDT", NOW)
    off = NewsFilterOptions()
    flt = NewsFilterOptions(mode="filter")
    red = NewsFilterOptions(mode="reduce_risk", size_multiplier=0.25)
    assert news_gate(OrderSide.BUY, bearish, off).block is None
    assert news_gate(OrderSide.BUY, bearish, flt).block.startswith("news_sentiment_opposing")
    assert news_gate(OrderSide.SELL, bearish, flt).block is None  # aligned with the sentiment
    d = news_gate(OrderSide.BUY, bearish, red)
    assert d.block is None and d.multiplier == Decimal("0.25")
    weak = aggregate([_sig(20, -0.9, "low")], "BTCUSDT", NOW)  # low confidence
    assert news_gate(OrderSide.BUY, weak, flt).block is None
    shock = aggregate([_sig(0.2, -0.5, "high")], "BTCUSDT", NOW)
    assert news_gate(OrderSide.SELL, shock, flt).block.startswith("news_high_impact_negative")


def test_event_gate_windows() -> None:
    ev = EventInfo("e1", "CPI m/m", "USD", "high", NOW + timedelta(minutes=20))
    eur = EventInfo("e2", "ECB rate", "EUR", "high", NOW + timedelta(minutes=5))
    med = EventInfo("e3", "Claims", "USD", "medium", NOW + timedelta(minutes=5))
    opts = EventPauseOptions(enabled=True, minutes_before=30, minutes_after=30)
    d, which = event_gate([eur, med, ev], NOW, opts)
    assert d.block == "economic_event:CPI m/m" and which is ev
    assert event_gate([ev], NOW, EventPauseOptions()) == (event_gate([], NOW, EventPauseOptions())[0], None)
    assert event_gate([ev], NOW + timedelta(minutes=51), opts)[0].block is None  # 31 min after
    tight = EventPauseOptions(
        enabled=True, minutes_before=10, minutes_after=10, tighten_minutes=30, risk_multiplier=0.5
    )
    d, which = event_gate([ev], NOW, tight)  # 20 min before: outside pause, inside tighten window
    assert d.block is None and d.multiplier == Decimal("0.5") and which is ev
    d, _ = event_gate([med], NOW, EventPauseOptions(enabled=True, min_impact="medium"))
    assert d.block is not None


# ------------------------------------------------------------------ ingestion pipeline
def news_transport(fail_x: bool = False):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        host = request.url.host
        if host == "www.coindesk.com":
            return httpx.Response(200, text=RSS.format(d1=_rfc(NOW - timedelta(minutes=30)), d2=_rfc(NOW)))
        if host == "decrypt.co":
            return httpx.Response(200, text=ATOM.format(d1=(NOW - timedelta(hours=1)).isoformat()))
        if host == "cointelegraph.com":
            return httpx.Response(503)
        if host == "cryptopanic.com":
            assert request.url.params["auth_token"] == "cp-placeholder"
            return httpx.Response(200, json={"results": [{
                "id": 9, "title": "Whales accumulate", "published_at": NOW.isoformat(),
                "currencies": [{"code": "ADA"}], "source": {"title": "CP"}, "original_url": "https://x.example/9",
            }]})  # fmt: skip
        if host == "api.x.com":
            if fail_x:
                return httpx.Response(429)
            assert request.headers["authorization"] == "Bearer x-placeholder"
            assert "from:whale_alert" in request.url.params["query"]
            return httpx.Response(200, json={
                "data": [{"id": "77", "text": "BTC liquidations spike", "author_id": "1",
                          "created_at": NOW.isoformat()}],
                "includes": {"users": [{"id": "1", "username": "whale_alert"}]},
            })  # fmt: skip
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


async def test_ingest_dedup_tag_analyse_and_throttle(db_schema, monkeypatch) -> None:
    s = store.settings
    monkeypatch.setattr(s, "NEWS_RSS_FEEDS", ["https://www.coindesk.com/rss", "https://cointelegraph.com/rss",
                                              "https://decrypt.co/feed"])  # fmt: skip
    monkeypatch.setattr(s, "CRYPTOPANIC_API_TOKEN", "cp-placeholder")
    monkeypatch.setattr(s, "X_BEARER_TOKEN", "x-placeholder")
    monkeypatch.setattr(s, "X_ACCOUNTS", ["whale_alert"])
    transport, calls = news_transport()
    async with httpx.AsyncClient(transport=transport) as http:
        res = await store.ingest(AsyncSessionLocal, http)
        res2 = await store.ingest(AsyncSessionLocal, http)
    assert res["created"] == 5 and res["analysed"] == 5, res  # 2 rss + 1 atom (1 dup) + cryptopanic + x
    assert res["errors"] == ["cointelegraph.com: HTTPStatusError"]
    assert res2["created"] == 0 and res2["analysed"] == 0
    assert sum(1 for c in calls if c.url.host == "cryptopanic.com") == 1  # throttled on the 2nd run
    assert sum(1 for c in calls if c.url.host == "api.x.com") == 1
    async with AsyncSessionLocal() as db:
        items = {i.title: i for i in await db.scalars(select(NewsItem))}
    etf = items["Bitcoin ETF approved as $SOL and ETH rally"]
    assert etf.symbols == ["BTCUSDT", "SOLUSDT", "ETHUSDT"] and etf.sentiment > 0 and etf.analysis_method == "lexicon"
    hack = items["Exchange hacked, $200M drained in XRP exploit"]
    assert hack.sentiment < 0 and hack.impact == "high" and hack.symbols == ["XRPUSDT"]
    assert items["Whales accumulate"].symbols == ["ADAUSDT"] and items["Whales accumulate"].source == "cryptopanic"
    assert items["BTC liquidations spike"].source_name == "@whale_alert"


async def test_source_errors_never_leak_tokens(db_schema, monkeypatch) -> None:
    monkeypatch.setattr(store.settings, "NEWS_RSS_FEEDS", [])
    monkeypatch.setattr(store.settings, "CRYPTOPANIC_API_TOKEN", "cp-placeholder")
    monkeypatch.setattr(store.settings, "CRYPTOPANIC_API_URL", "https://cryptopanic.com/fail/")

    def handler(request):
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        items, errors = await store.fetch_all(http)
    assert items == [] and errors == ["cryptopanic: HTTPStatusError"]


async def test_high_impact_alert_only_for_traded_symbols(db, captured) -> None:
    _, ws = await make_user_workspace(db)
    _, other_ws = await make_user_workspace(db, email="other@example.com")
    acc = await make_account(db, ws)
    await make_bot(db, acc, "ema_cross", {}, symbol="XRPUSDT")
    acc2 = await make_account(db, other_ws)
    await make_bot(db, acc2, "ema_cross", {}, symbol="BTCUSDT", state=BotState.STOPPED)
    db.add(NewsItem(source="rss", source_name="coindesk.com", dedup_key="k1", title="XRP exploit", body="",
                    published_at=NOW, symbols=["XRPUSDT", "BTCUSDT"], sentiment=-0.9, impact="high",
                    analyzed_at=NOW))  # fmt: skip
    db.add(NewsItem(source="rss", source_name="c", dedup_key="k2", title="minor", body="", published_at=NOW,
                    symbols=["XRPUSDT"], sentiment=-0.2, impact="high", analyzed_at=NOW))  # fmt: skip
    await db.commit()
    async with AsyncSessionLocal() as s:
        assert await store.alert_high_impact(s) == 1
        await s.commit()
        assert await store.alert_high_impact(s) == 0  # already alerted
    news = [p for p in captured if p["event"] == "news_high_impact"]
    assert len(news) == 1 and news[0]["workspace_id"] == str(ws.id)
    assert news[0]["data"]["symbol"] == "XRPUSDT" and news[0]["data"]["title"] == "XRP exploit"


# ------------------------------------------------------------------ calendar
FF = [
    {"title": "CPI m/m", "country": "USD", "date": (NOW + timedelta(minutes=40)).isoformat(), "impact": "High",
     "forecast": "0.3%", "previous": "0.2%"},
    {"title": "German PMI", "country": "EUR", "date": (NOW + timedelta(minutes=40)).isoformat(), "impact": "High"},
    {"title": "Bank Holiday", "country": "USD", "date": (NOW + timedelta(days=1)).isoformat(), "impact": "Holiday"},
    {"title": "", "country": "USD", "date": "bad"},
]  # fmt: skip


async def test_calendar_refresh_manual_fallback_and_alerts(db, monkeypatch, captured) -> None:
    manual = [{"title": "FOMC (manual)", "time": (NOW + timedelta(hours=3)).isoformat(), "country": "USD",
               "impact": "high"}]  # fmt: skip
    monkeypatch.setattr(calendar.settings, "ECON_CALENDAR_MANUAL_EVENTS", json.dumps(manual))
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json=FF))
    async with httpx.AsyncClient(transport=ok) as http:
        res = await calendar.refresh_calendar(AsyncSessionLocal, http)
        res_again = await calendar.refresh_calendar(AsyncSessionLocal, http)
    assert res == {"events": 4, "created": 4, "errors": []} and res_again["created"] == 0
    down = httpx.MockTransport(lambda r: httpx.Response(503))
    async with httpx.AsyncClient(transport=down) as http:
        res = await calendar.refresh_calendar(AsyncSessionLocal, http)
    assert res["events"] == 1 and res["errors"] == ["forexfactory: HTTPStatusError"]
    events = {e.title: e for e in await db.scalars(select(EconomicEvent))}
    assert events["Bank Holiday"].impact == "holiday" and events["CPI m/m"].forecast == "0.3%"
    assert events["FOMC (manual)"].source == "manual"

    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    await make_bot(db, acc, "ema_cross", {})
    await db.commit()
    async with AsyncSessionLocal() as s:
        assert await store.alert_upcoming_events(s) == 1  # CPI only (EUR filtered, FOMC beyond lead time)
        await s.commit()
    econ = [p for p in captured if p["event"] == "economic_event"]
    assert len(econ) == 1 and econ[0]["data"]["title"] == "CPI m/m" and econ[0]["workspace_id"] == str(ws.id)


# ------------------------------------------------------------------ bot integration
BREAKOUT = [100 + (1 if i % 2 else -1) for i in range(30)] + [106.0]
DONCHIAN = {"entry_period": 10, "exit_period": 5, "atr_period": 5, "allow_short": False}


async def _bot_tick(db, options: dict, email: str):
    _, ws = await make_user_workspace(db, email=email)
    acc = await make_account(db, ws)
    bot = await make_bot(db, acc, "donchian_breakout", DONCHIAN, symbol="AAAUSDT", options=options)
    await db.commit()
    market = StaticMarketData({"AAAUSDT": candles_from_closes(BREAKOUT)})
    res = await run_bot_tick(db, bot, acc, market, executor=PaperExecutor())
    pos = await db.scalar(select(Position).where(Position.account_id == acc.id, Position.symbol == "AAAUSDT"))
    return res, bot, (pos.quantity if pos else Decimal(0))


async def _bearish_news(db):
    for i, s in enumerate((-0.9, -0.8)):
        db.add(NewsItem(source="rss", source_name="c", dedup_key=f"b{i}", title=f"AAA bad {i}", body="",
                        published_at=NOW - timedelta(minutes=10 + i), symbols=["AAAUSDT"], sentiment=s,
                        impact="medium", analyzed_at=NOW))  # fmt: skip
    await db.commit()


async def test_bot_news_filter_blocks_reduces_or_ignores(db) -> None:
    await _bearish_news(db)
    res, _, base_qty = await _bot_tick(db, {}, "off@example.com")
    assert res.filled == 1 and base_qty > 0  # filter off by default

    res, bot, qty = await _bot_tick(db, {"news_filter": {"mode": "filter"}}, "flt@example.com")
    assert res.filled == 0 and res.reasons[0].startswith("news_sentiment_opposing") and qty == 0
    audit = await db.scalar(select(AuditLog).where(AuditLog.event == "bot.entry_filtered"))
    assert audit is not None and audit.data["reason"].startswith("news_sentiment_opposing")

    res, _, qty = await _bot_tick(db, {"news_filter": {"mode": "reduce_risk", "size_multiplier": 0.5}}, "red@x.com")
    assert res.filled == 1 and 0 < qty < base_qty
    assert abs(qty - base_qty / 2) <= base_qty * Decimal("0.05")


async def test_bot_event_pause_blocks_entries_and_alerts(db, captured) -> None:
    db.add(EconomicEvent(source="manual", dedup_key="ev1", title="CPI y/y", country="USD", impact="high",
                         event_time=NOW + timedelta(minutes=10)))  # fmt: skip
    await db.commit()
    res, bot, qty = await _bot_tick(db, {"event_pause": {"enabled": True}}, "ev@example.com")
    assert res.filled == 0 and res.reasons == ["economic_event:CPI y/y"] and qty == 0
    assert bot.runtime.get("event_pause")
    pause = [p for p in captured if p["event"] == "bot_event_pause"]
    assert len(pause) == 1 and "CPI y/y" in pause[0]["data"]["economic_event"]
    res, _, qty = await _bot_tick(db, {"event_pause": {"enabled": True, "countries": ["EUR"]}}, "eur@example.com")
    assert res.filled == 1 and qty > 0


async def test_backtest_news_filter_option(client, db) -> None:
    await _bearish_news(db)
    h = bearer(await register_login(client, "btnews@example.com"))
    csv = "open_time,open,high,low,close,volume\n" + "\n".join(
        f"{c.open_time},{c.open},{c.high},{c.low},{c.close},{c.volume}" for c in candles_from_closes(BREAKOUT)
    )
    body = {"strategy_key": "donchian_breakout", "params": DONCHIAN, "symbol": "AAAUSDT", "interval": "1h",
            "candles_csv": csv}  # fmt: skip
    r = await client.post("/api/v1/strategies/backtest", json=body, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["metrics"]["num_fills"] >= 1 and "filters" not in r.json()["config"]
    r = await client.post("/api/v1/strategies/backtest", json={**body, "filters": {"news_filter": {"mode": "filter"}}},
                          headers=h)  # fmt: skip
    assert r.status_code == 201, r.text
    # candles are historical (2023) while the stored news is from now: nothing to replay -> unfiltered
    assert r.json()["config"]["news_items_available"] == 0
    r = await client.post("/api/v1/strategies/backtest", json={**body, "filters": {"news_filter": {"mode": "bogus"}}},
                          headers=h)  # fmt: skip
    assert r.status_code == 422


def test_backtest_engine_gate_blocks_entries() -> None:
    from app.backtest.engine import run_backtest
    from app.services.news.gate import GateDecision
    from app.strategy.strategies import build_strategy

    candles = candles_from_closes(BREAKOUT)
    free = run_backtest(build_strategy("donchian_breakout", DONCHIAN), candles, "AAAUSDT", "1h")
    gated = run_backtest(build_strategy("donchian_breakout", DONCHIAN), candles, "AAAUSDT", "1h",
                         entry_gate=lambda intent, candle: GateDecision("news_sentiment_opposing:-0.9"))  # fmt: skip
    assert free.metrics["num_fills"] >= 1 and gated.metrics["num_fills"] == 0
    assert gated.rejections == {"news_sentiment_opposing": 1}
    assert isinstance(OrderIntent(OrderSide.BUY), OrderIntent)


# ------------------------------------------------------------------ API
async def test_news_and_calendar_api(client, db) -> None:
    await _bearish_news(db)
    db.add(NewsItem(source="x", source_name="@a", dedup_key="p", title="BTC rally", body="", published_at=NOW,
                    symbols=["BTCUSDT"], sentiment=0.6, impact="low", analyzed_at=NOW, summary_pt="Alta"))  # fmt: skip
    db.add(EconomicEvent(source="manual", dedup_key="e", title="NFP", country="USD", impact="high",
                         event_time=NOW + timedelta(days=1)))  # fmt: skip
    await db.commit()
    h = bearer(await register_login(client, "newsapi@example.com"))
    r = await client.get("/api/v1/news/?symbol=aaausdt", headers=h)
    assert r.status_code == 200 and r.json()["total"] == 2
    r = await client.get("/api/v1/news/?sentiment=positive&source=x", headers=h)
    assert [i["title"] for i in r.json()["items"]] == ["BTC rally"]
    r = await client.get("/api/v1/news/sentiment?symbols=AAAUSDT,BTCUSDT", headers=h)
    by = {x["symbol"]: x for x in r.json()}
    assert by["AAAUSDT"]["score"] < -0.5 and by["BTCUSDT"]["score"] > 0
    r = await client.get("/api/v1/news/sentiment", headers=h)
    assert {x["symbol"] for x in r.json()} == {"AAAUSDT", "BTCUSDT"}
    st = (await client.get("/api/v1/news/status", headers=h)).json()
    assert st["llm_enabled"] is False and st["total_items"] == 3 and "coindesk.com" in st["rss_sources"]
    assert "token" not in json.dumps(st).lower().replace("cryptopanic_enabled", "")
    r = await client.get("/api/v1/calendar/?impact=high", headers=h)
    assert [e["title"] for e in r.json()] == ["NFP"]
    assert (await client.post("/api/v1/news/refresh", headers=h)).json() == {"queued": False}
    assert (await client.post("/api/v1/calendar/refresh", headers=h)).json() == {"queued": False}
