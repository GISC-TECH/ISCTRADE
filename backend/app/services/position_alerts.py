"""Open-position alerts (G-06): PnL thresholds, proximity to the stop and to the liquidation price.

Evaluated wherever marks are refreshed, for live and paper alike:
- live: by the reconciler (every RECONCILE_SECONDS and on user-stream events), with the exchange's own
  ``liquidationPrice``;
- paper: by ``paper.process_orders`` (every PAPER_ORDER_CHECK_SECONDS), with an estimated isolated-margin
  liquidation price (leverage + RISK_DEFAULT_MAINT_MARGIN_RATE).

Each (account, symbol, kind, level) alerts at most once per POSITION_ALERT_THROTTLE_MINUTES (Redis, with a
per-process fallback); alerts are queued on the session and dispatched after commit. The usual per-workspace
toggles and minimum severity (alert settings) still apply.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import settings
from app.models import Account, Position
from app.services.alerts import throttle
from app.services.alerts.events import AlertEvent
from app.services.alerts.service import queue_alert
from app.services.risk_manager import Side, estimate_liquidation_price

logger = logging.getLogger(__name__)

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class PositionAlert:
    event: AlertEvent
    symbol: str
    kind: str  # pnl_roe | pnl_usdt | stop | liquidation
    level: str
    severity: str
    data: dict[str, Any]


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _level_hit(value: Decimal, levels: Iterable[float]) -> Decimal | None:
    """Most extreme configured level crossed by ``value`` (positive levels: value >= level; negative: <=)."""
    hit: Decimal | None = None
    for raw in levels:
        level = Decimal(str(raw))
        if level == 0:
            continue
        if (level > 0 and value >= level) or (level < 0 and value <= level):
            if hit is None or abs(level) > abs(hit):
                hit = level
    if hit is None:
        return None
    return hit.quantize(Decimal(1)) if hit == hit.to_integral_value() else hit.normalize()


def _distance_pct(mark: Decimal, other: Decimal, long: bool) -> Decimal:
    """How far the price may still move against the position before reaching ``other`` (percent of mark)."""
    return ((mark - other) if long else (other - mark)) / mark * HUNDRED


def evaluate_position(pos: Position, liquidation_price: Decimal | None = None) -> list[PositionAlert]:
    qty = Decimal(pos.quantity or 0)
    mark = _dec(pos.mark_price)
    entry = _dec(pos.entry_price)
    if qty == 0 or not mark or mark <= 0 or not entry or entry <= 0:
        return []
    long = qty > 0
    side = "LONG" if long else "SHORT"
    pnl = qty * (mark - entry)
    margin = abs(qty) * entry / Decimal(max(int(pos.leverage or 1), 1))
    roe = pnl / margin * HUNDRED if margin > 0 else Decimal(0)
    base = {
        "symbol": pos.symbol, "side": side, "quantity": abs(qty), "price": mark, "entry_price": entry,
        "leverage": pos.leverage, "unrealized_pnl": pnl.quantize(Decimal("0.01")),
        "roe_pct": roe.quantize(Decimal("0.01")),
    }  # fmt: skip
    out: list[PositionAlert] = []

    def pnl_alert(kind: str, level: Decimal, text: str) -> None:
        sev = "info" if level > 0 else "warning"
        out.append(PositionAlert(AlertEvent.POSITION_PNL, pos.symbol, kind, str(level), sev, {**base, "text": text}))

    if (level := _level_hit(roe, settings.POSITION_ALERT_PNL_ROE_PCT)) is not None:
        word = "lucro" if level > 0 else "prejuízo"
        pnl_alert("pnl_roe", level, f"{pos.symbol} {side}: {word} de {roe:.2f}% sobre a margem (limiar {level}%).")
    if (level := _level_hit(pnl, settings.POSITION_ALERT_PNL_USDT)) is not None:
        word = "lucro" if level > 0 else "prejuízo"
        pnl_alert("pnl_usdt", level, f"{pos.symbol} {side}: {word} não realizado de {pnl:.2f} USDT (limiar {level}).")

    stop = _dec(pos.stop_loss)
    near = Decimal(str(settings.POSITION_ALERT_STOP_PROXIMITY_PCT))
    if stop and stop > 0 and near > 0:
        dist = _distance_pct(mark, stop, long)
        if 0 <= dist <= near:
            out.append(PositionAlert(
                AlertEvent.POSITION_NEAR_STOP, pos.symbol, "stop", "", "warning",
                {**base, "stop_loss": stop, "distance_pct": dist.quantize(Decimal("0.01")),
                 "text": f"{pos.symbol} {side} a {dist:.2f}% do stop ({stop})."},
            ))  # fmt: skip

    liq = liquidation_price
    if liq is None and pos.is_paper:
        liq = estimate_liquidation_price(
            Side.BUY if long else Side.SELL, entry, int(pos.leverage or 1),
            Decimal(str(settings.RISK_DEFAULT_MAINT_MARGIN_RATE)),
        )  # fmt: skip
    near = Decimal(str(settings.POSITION_ALERT_LIQ_PROXIMITY_PCT))
    if liq and liq > 0 and near > 0:
        dist = _distance_pct(mark, liq, long)
        if dist <= near:
            out.append(PositionAlert(
                AlertEvent.POSITION_NEAR_LIQUIDATION, pos.symbol, "liquidation", "", "critical",
                {**base, "liquidation_price": liq.quantize(Decimal("0.00000001")),
                 "distance_pct": dist.quantize(Decimal("0.01")),
                 "estimated": liquidation_price is None,
                 "text": f"{pos.symbol} {side} a {dist:.2f}% do preço de liquidação ({liq:.8g})."},
            ))  # fmt: skip
    return out


async def check_positions(
    db, account: Account, positions: Iterable[Position], liquidation: dict[str, Decimal | None] | None = None
) -> list[PositionAlert]:
    """Evaluate open positions and queue the (throttled) alerts on ``db``. Never raises."""
    if not settings.POSITION_ALERTS_ENABLED:
        return []
    sent: list[PositionAlert] = []
    try:
        for pos in positions:
            for alert in evaluate_position(pos, (liquidation or {}).get(pos.symbol)):
                key = f"pos:{account.id}:{alert.symbol}:{alert.kind}:{alert.level}"
                if not await throttle.acquire(key, settings.POSITION_ALERT_THROTTLE_MINUTES):
                    continue
                queue_alert(
                    db, alert.event, workspace_id=account.workspace_id, account_id=account.id,
                    severity=alert.severity, **alert.data,
                )  # fmt: skip
                sent.append(alert)
    except Exception:  # alerting must never break reconciliation or paper fills
        logger.exception("position alerts failed account=%s", account.id)
    return sent


def liquidation_prices(remote_positions: Iterable[dict] | None) -> dict[str, Decimal | None]:
    """``liquidationPrice`` per symbol from positionRisk (0 = none, e.g. cross margin with ample balance)."""
    out: dict[str, Decimal | None] = {}
    for p in remote_positions or []:
        liq = _dec(p.get("liquidationPrice"))
        if p.get("symbol"):
            out[str(p["symbol"])] = liq if liq and liq > 0 else None
    return out
