from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import configure_mappers

from app.core.security import hash_password
from app.models import Account, Base, BotConfig, Order, OrderSide, OrderType, User, Workspace

EXPECTED_TABLES = {
    "users",
    "alert_settings",
    "alerts",
    "market_scans",
    "news_items",
    "economic_events",
    "workspaces",
    "workspace_members",
    "accounts",
    "bot_configs",
    "refresh_tokens",
    "registration_invites",
    "orders",
    "trades",
    "positions",
    "equity_snapshots",
    "audit_logs",
    "backtest_runs",
    "system_settings",
}


def test_mappers_configure() -> None:
    configure_mappers()
    assert EXPECTED_TABLES == set(Base.metadata.tables)
    assert Workspace.accounts.property.mapper.class_ is Account


async def test_crud_roundtrip(db) -> None:
    user = User(email="a@example.com", hashed_password=hash_password("password123"))
    db.add(user)
    await db.flush()
    ws = Workspace(name="WS", owner_id=user.id)
    db.add(ws)
    await db.flush()
    acc = Account(workspace_id=ws.id, label="x", encrypted_api_key="e", encrypted_api_secret="e", api_key_last4="1234")
    db.add(acc)
    await db.flush()
    bot = BotConfig(workspace_id=ws.id, account_id=acc.id, name="b", strategy_key="grid", symbol="BTCUSDT")
    db.add(bot)
    db.add(
        Order(
            workspace_id=ws.id,
            account_id=acc.id,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            quantity=Decimal("0.001"),
            client_order_id="test-1",
        )
    )
    await db.commit()
    acc_db = await db.scalar(select(Account))
    assert acc_db.paper_balance == Decimal("10000")
    assert acc_db.execution_mode.value == "paper"
    order = await db.scalar(select(Order))
    assert order.quantity == Decimal("0.001")
