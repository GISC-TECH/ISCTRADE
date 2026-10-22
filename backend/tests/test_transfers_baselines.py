"""G-03: deposits/withdrawals are folded into the drawdown peak and the daily baseline (live accounts)."""

from datetime import timedelta
from decimal import Decimal as D

from app.exchanges.base import UnsupportedFeatureError
from app.models import ExecutionMode
from app.models.base import utcnow
from app.services.equity import refresh_risk_baselines, sync_transfers
from app.services.risk_manager import RiskManager
from tests.factories import make_account, make_user_workspace


class FakeTransfers:
    def __init__(self, rows=None, error: Exception | None = None):
        self.rows, self.error, self.calls = rows or [], error, []

    async def get_transfers(self, start_time, end_time=None):
        self.calls.append((start_time, end_time))
        if self.error:
            raise self.error
        return [r for r in self.rows if start_time <= r["time"] <= (end_time or 1 << 62)]


def ms(dt) -> int:
    return int(dt.timestamp() * 1000)


async def _live(db, peak="1000", day="1000", cursor_minutes=10, day_start_hours=1):
    _, ws = await make_user_workspace(db)
    now = utcnow()
    acc = await make_account(
        db, ws, execution_mode=ExecutionMode.LIVE, peak_equity=D(peak), day_start_equity=D(day),
        day_start_at=now - timedelta(hours=day_start_hours), last_equity=D(day),
        transfers_synced_at=now - timedelta(minutes=cursor_minutes),
    )  # fmt: skip
    return acc, now


async def test_withdrawal_does_not_trigger_kill_switch(db) -> None:
    acc, now = await _live(db)
    client = FakeTransfers([{"time": ms(now - timedelta(minutes=2)), "amount": "-500", "asset": "USDT"}])
    await refresh_risk_baselines(acc, D("480"), client, now, db=db)
    assert acc.peak_equity == D("500") and acc.day_start_equity == D("500")
    state = RiskManager().from_account(acc, D("480"))
    assert not RiskManager().is_kill_switch_triggered(state)  # 4% drawdown, not 52%
    assert acc.transfers_synced_at == now - timedelta(seconds=5)


async def test_deposit_does_not_mask_losses(db) -> None:
    acc, now = await _live(db)
    # lost 200 (equity 800), then deposited 500 -> wallet shows 1300, above the old peak
    client = FakeTransfers([{"time": ms(now - timedelta(minutes=1)), "amount": "500", "asset": "USDT"}])
    await refresh_risk_baselines(acc, D("1300"), client, now)
    assert acc.peak_equity == D("1500") and acc.day_start_equity == D("1500")
    assert RiskManager().drawdown_pct(RiskManager().from_account(acc, D("1300"))) > D("13")


async def test_transfer_before_day_start_only_moves_the_peak(db) -> None:
    acc, now = await _live(db, peak="2000", day="1000", cursor_minutes=180, day_start_hours=1)
    client = FakeTransfers([{"time": ms(now - timedelta(hours=2)), "amount": "-1000", "asset": "USDT"}])
    await refresh_risk_baselines(acc, D("990"), client, now)
    assert acc.peak_equity == D("1000") and acc.day_start_equity == D("1000")


async def test_other_assets_are_ignored(db) -> None:
    acc, now = await _live(db)
    client = FakeTransfers([{"time": ms(now - timedelta(minutes=1)), "amount": "-1", "asset": "BNB"}])
    await refresh_risk_baselines(acc, D("1000"), client, now)
    assert acc.peak_equity == D("1000")


async def test_history_failure_keeps_cursor_and_freezes_peak(db) -> None:
    acc, now = await _live(db)
    cursor = acc.transfers_synced_at
    client = FakeTransfers(error=RuntimeError("timeout"))
    await refresh_risk_baselines(acc, D("1500"), client, now)  # maybe a deposit: do not raise the peak yet
    assert acc.peak_equity == D("1000") and acc.transfers_synced_at == cursor and acc.last_equity == D("1500")
    client.error = None
    client.rows = [{"time": ms(now - timedelta(minutes=1)), "amount": "500", "asset": "USDT"}]
    await refresh_risk_baselines(acc, D("1500"), client, now)
    assert acc.peak_equity == D("1500")


async def test_reads_are_throttled_unless_a_threshold_is_hit(db) -> None:
    acc, now = await _live(db, cursor_minutes=1)
    client = FakeTransfers()
    await refresh_risk_baselines(acc, D("990"), client, now)
    assert client.calls == []  # synced 1 min ago, nothing suspicious
    await refresh_risk_baselines(acc, D("800"), client, now)  # 20% drawdown: check transfers before killing
    assert len(client.calls) == 1
    await refresh_risk_baselines(acc, D("1200"), client, now + timedelta(seconds=10))  # new high
    assert len(client.calls) == 2


async def test_first_sync_sets_cursor_and_unsupported_adapters(db) -> None:
    acc, now = await _live(db)
    acc.transfers_synced_at = None
    client = FakeTransfers()
    assert await sync_transfers(acc, client, now)
    assert client.calls == [] and acc.transfers_synced_at is not None
    acc.transfers_synced_at = now - timedelta(minutes=10)
    assert await sync_transfers(acc, FakeTransfers(error=UnsupportedFeatureError("transfer history", "x")), now)
    assert acc.transfers_synced_at == now - timedelta(seconds=5)


async def test_paper_accounts_never_query_transfers(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, peak_equity=D("1000"))
    client = FakeTransfers()
    await refresh_risk_baselines(acc, D("1100"), client)
    assert client.calls == [] and acc.peak_equity == D("1100")


async def test_snapshot_applies_transfers(db) -> None:
    from app.services.equity import take_snapshot

    acc, now = await _live(db, cursor_minutes=10)

    class Client(FakeTransfers):
        async def get_account(self):
            return {"totalWalletBalance": "400", "totalUnrealizedProfit": "0", "availableBalance": "400"}

    client = Client([{"time": ms(utcnow() - timedelta(minutes=1)), "amount": "-600", "asset": "USDT"}])
    snap = await take_snapshot(db, acc, client)
    assert snap.equity == D("400") and acc.peak_equity == D("400")
