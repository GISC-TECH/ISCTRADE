"""Bot presets ("robôs prontos"): catalogue validity, API endpoint and bot creation from a preset (paper only)."""

import re

from app.core.security import create_access_token
from app.schemas.bot import INTERVALS
from app.schemas.bot_options import BotOptions
from app.strategy.presets import PRESETS, PRESETS_BY_KEY, list_presets
from app.strategy.strategies import STRATEGIES, build_strategy
from tests.factories import make_account, make_user_workspace

REQUIRED_NAMES = {
    "Tendência conservadora BTC 4h",
    "Reversão RSI-2 1h",
    "Rompimento Turtle 1d",
    "Scalp VWAP 5m (experimental)",
}


def test_preset_catalogue_is_valid() -> None:
    assert len(PRESETS) >= 10 and len(PRESETS_BY_KEY) == len(PRESETS)  # unique keys
    assert REQUIRED_NAMES <= {p.name for p in PRESETS}
    for p in list_presets():
        assert p["strategy_key"] in STRATEGIES
        assert re.match(INTERVALS, p["interval"]), p["key"]
        assert re.match(r"^[A-Z0-9]{2,20}USDT$", p["symbol"])
        build_strategy(p["strategy_key"], p["params"])  # parameters validate
        BotOptions.model_validate(p["options"])
        assert p["execution_mode"] == "paper"
        assert 1 <= p["leverage"] <= 2, "presets stay at low leverage"
        assert p["params"].get("risk_fraction", 1) <= 0.5, "presets use at most half of the per-trade risk"
        assert p["risk_level"] in ("conservador", "moderado", "agressivo")
        if p["experimental"]:
            assert "experimental" in p["name"].lower()


def test_presets_pause_entries_around_high_impact_events() -> None:
    assert all(p["options"]["event_pause"]["enabled"] for p in list_presets())


async def test_presets_endpoint_and_create_bot_from_preset(client, db) -> None:
    user, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    await db.commit()
    h = {"Authorization": f"Bearer {create_access_token(str(user.id))}", "X-Workspace-ID": str(ws.id)}
    assert (await client.get("/api/v1/strategies/presets")).status_code == 401
    r = await client.get("/api/v1/strategies/presets", headers=h)
    assert r.status_code == 200
    presets = {p["key"]: p for p in r.json()}
    assert set(presets) == set(PRESETS_BY_KEY)
    for key in ("rsi2_reversion_1h", "trend_hunter_top3_1h", "vwap_scalp_5m"):
        p = presets[key]
        body = {
            "account_id": str(acc.id),
            "name": p["name"],
            "strategy_key": p["strategy_key"],
            "symbol": p["symbol"],
            "interval": p["interval"],
            "leverage": p["leverage"],
            "params": p["params"],
            "options": p["options"],
        }
        r = await client.post("/api/v1/bots/", json=body, headers=h)
        assert r.status_code == 201, r.text
        bot = r.json()
        assert bot["state"] == "idle" and bot["params"] == p["params"]
        assert bot["options"]["event_pause"]["enabled"] is True
