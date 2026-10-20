"""Test data helpers."""

from __future__ import annotations

import math
import uuid
from decimal import Decimal

from app.core.security import encrypt_api_key, hash_password
from app.models import Account, AccountStatus, BotConfig, BotState, User, Workspace
from app.strategy.base import Candle

HOUR_MS = 3_600_000
T0 = 1_700_000_000_000


async def make_user_workspace(db, email: str | None = None) -> tuple[User, Workspace]:
    user = User(email=email or f"u{uuid.uuid4().hex[:8]}@example.com", hashed_password=hash_password("password123"))
    db.add(user)
    await db.flush()
    ws = Workspace(name="WS", owner_id=user.id)
    db.add(ws)
    await db.flush()
    return user, ws


async def make_account(db, ws: Workspace, **kw) -> Account:
    aad = str(ws.id).encode()
    acc = Account(
        workspace_id=ws.id,
        label=kw.pop("label", "paper"),
        encrypted_api_key=encrypt_api_key("dummy-key-0000", aad),
        encrypted_api_secret=encrypt_api_key("dummy-secret-0000", aad),
        api_key_last4="0000",
        status=kw.pop("status", AccountStatus.RUNNING),
        **kw,
    )
    db.add(acc)
    await db.flush()
    return acc


async def make_bot(db, acc: Account, strategy_key: str, params: dict, **kw) -> BotConfig:
    bot = BotConfig(
        workspace_id=acc.workspace_id,
        account_id=acc.id,
        name=kw.pop("name", strategy_key),
        strategy_key=strategy_key,
        symbol=kw.pop("symbol", "BTCUSDT"),
        interval=kw.pop("interval", "1h"),
        params=params,
        state=kw.pop("state", BotState.RUNNING),
        **kw,
    )
    db.add(bot)
    await db.flush()
    return bot


def candles_from_closes(closes: list[float], spread: float = 0.001, start: int = T0) -> list[Candle]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) * (1 + spread)
        lo = min(prev, c) * (1 - spread)
        out.append(Candle(start + i * HOUR_MS, prev, hi, lo, c, 100.0, start + (i + 1) * HOUR_MS - 1))
        prev = c
    return out


def sine_closes(n: int = 300, base: float = 100.0, amp: float = 10.0, period: int = 50) -> list[float]:
    return [base + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


D = Decimal
