"""Alert pipeline.

Producers never block trading:

* inside a DB transaction, :func:`queue_alert` stashes the alert in ``session.info``; it is dispatched only
  after the transaction commits (and dropped on rollback), so we never alert on work that was rolled back;
* elsewhere (WS hooks, Celery signals, beat tasks) :func:`emit_alert` dispatches immediately.

Dispatch = ``celery send_task("alerts.process", retry=False)`` in a background thread, so an unreachable
broker costs nothing on the hot path. The worker then applies settings/toggles/severity/throttle
(:func:`process_alert`), persists an :class:`AlertRecord` and delivers it (:func:`deliver_alert`) with retries.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.metrics import ALERTS, safe_inc
from app.core.redaction import mask_phone, redact
from app.models.account import Account
from app.models.alert import AlertRecord, AlertSettings
from app.models.trading import Trade
from app.models.workspace import Workspace
from app.services.alerts import throttle
from app.services.alerts.events import EVENTS, AlertEvent, effective_toggles, severity_allows
from app.services.alerts.formatting import render
from app.services.alerts.notifier import Notifier, NotifierError
from app.services.alerts.whatsapp import build_notifier, normalize_phone

logger = logging.getLogger(__name__)

PENDING_KEY = "pending_alerts"
SessionFactory = async_sessionmaker[AsyncSession]


class DeliveryError(Exception):
    """All recipients failed with a retryable error."""


# --------------------------------------------------------------------------- producers


def build_payload(
    event: str | AlertEvent,
    *,
    workspace_id: uuid.UUID | str | None = None,
    account_id: uuid.UUID | str | None = None,
    severity: str | None = None,
    dedup: str | None = None,
    **data: Any,
) -> dict[str, Any]:
    key = event.value if isinstance(event, AlertEvent) else str(event)
    spec = EVENTS.get(key)
    return {
        "event": key,
        "severity": severity or (spec.severity.value if spec else "info"),
        "workspace_id": str(workspace_id) if workspace_id else None,
        "account_id": str(account_id) if account_id else None,
        "dedup": dedup,
        "data": redact(_jsonable(data)),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID | datetime):
        return str(value)
    if hasattr(value, "value") and not isinstance(value, str | int | float | bool):  # enums
        return value.value
    return value


def _sender() -> Callable[[dict[str, Any]], None]:
    def send(payload: dict[str, Any]) -> None:
        try:
            from app.tasks.celery_app import celery_app

            celery_app.send_task("alerts.process", kwargs={"payload": payload}, retry=False)
        except Exception as exc:  # broker down: drop the alert, never the trade
            safe_inc(ALERTS, event=payload.get("event", "?"), status="dispatch_failed")
            logger.warning("alert dispatch failed event=%s: %s", payload.get("event"), type(exc).__name__)

    return send


def dispatch(payload: dict[str, Any]) -> bool:
    """Fire-and-forget hand-off to the Celery worker. Never raises, never blocks."""
    if not settings.ALERTS_ENABLED or not settings.CELERY_DISPATCH_ENABLED:
        return False
    threading.Thread(target=_sender(), args=(payload,), daemon=True, name="alert-dispatch").start()
    return True


def emit_alert(event: str | AlertEvent, **kwargs: Any) -> bool:
    try:
        return dispatch(build_payload(event, **kwargs))
    except Exception:  # pragma: no cover - defensive: alerting must never break the caller
        logger.exception("emit_alert failed")
        return False


def queue_alert(db: AsyncSession, event: str | AlertEvent, **kwargs: Any) -> None:
    """Queue an alert for dispatch after `db` commits."""
    try:
        db.sync_session.info.setdefault(PENDING_KEY, []).append(build_payload(event, **kwargs))
    except Exception:  # pragma: no cover
        logger.exception("queue_alert failed")


@sa_event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    pending = session.info.pop(PENDING_KEY, None)
    for payload in pending or []:
        dispatch(payload)


@sa_event.listens_for(Session, "after_soft_rollback")
def _after_rollback(session: Session, previous_transaction: Any) -> None:
    if previous_transaction.parent is None:  # outermost rollback only
        session.info.pop(PENDING_KEY, None)


# --------------------------------------------------------------------------- audit mapping


def _reason_prefix(reason: Any) -> str:
    return str(reason or "").split(":", 1)[0].strip()


def alert_from_audit(
    event: str,
    *,
    workspace_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    """Translate an audit event into an alert payload (or None)."""
    ctx = {"workspace_id": workspace_id, "account_id": account_id}
    if event == "risk.kill_switch":
        return build_payload(AlertEvent.KILL_SWITCH_ACCOUNT, dedup=f"ks:{account_id}", reason=data.get("reason"), **ctx)
    if event == "admin.global_kill_switch":
        active = bool(data.get("active", True))
        return build_payload(
            AlertEvent.KILL_SWITCH_GLOBAL,
            severity="critical" if active else "info",
            active=active,
            reason=data.get("reason"),
            **ctx,
        )
    if event == "order.rejected":
        prefix = _reason_prefix(data.get("reason"))
        if prefix == "global_kill_switch":
            return None  # the global kill switch itself already alerted
        fields = {k: data.get(k) for k in ("symbol", "side", "quantity", "reason")}
        if prefix in ("max_daily_loss", "max_drawdown", "kill_switch"):
            return build_payload(AlertEvent.RISK_LIMIT, dedup=f"risk:{account_id}:{prefix}", **ctx, **fields)
        return build_payload(
            AlertEvent.ORDER_REJECTED, dedup=f"rej:{account_id}:{data.get('symbol')}:{prefix}", **ctx, **fields
        )
    if event in ("order.filled", "order.submitted"):
        fields = {k: data.get(k) for k in ("symbol", "side", "quantity", "price", "mode")}
        return build_payload(AlertEvent.ORDER_FILLED, **ctx, **fields)
    if event == "position.opened":
        fields = {k: data.get(k) for k in ("symbol", "side", "quantity", "price", "leverage")}
        return build_payload(AlertEvent.POSITION_OPENED, **ctx, **fields)
    if event in ("position.closed", "position.stop_loss"):
        fields = {k: data.get(k) for k in ("symbol", "side", "quantity", "price", "realized_pnl", "reason")}
        if event == "position.stop_loss" and not fields.get("reason"):
            fields["reason"] = "stop loss"
        return build_payload(AlertEvent.POSITION_CLOSED, **ctx, **fields)
    if event == "bot.error":
        return build_payload(
            AlertEvent.BOT_ERROR,
            dedup=f"bot:{data.get('bot_id')}",
            bot=data.get("bot") or data.get("bot_id"),
            symbol=data.get("symbol"),
            error=data.get("error"),
            **ctx,
        )
    if event == "reconcile.changes" and int(data.get("positions_changed") or 0) > 0:
        return build_payload(
            AlertEvent.RECONCILER_MISMATCH,
            dedup=f"recon:{account_id}",
            changes=data.get("positions_changed"),
            detail=data.get("detail"),
            **ctx,
        )
    return None


def on_audit_event(
    db: AsyncSession | None,
    event: str,
    *,
    workspace_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
    data: dict[str, Any],
) -> None:
    """Called by audit.log_event. Must never raise."""
    try:
        payload = alert_from_audit(event, workspace_id=workspace_id, account_id=account_id, data=data)
        if payload is None:
            return
        if db is None:
            dispatch(payload)
        else:
            db.sync_session.info.setdefault(PENDING_KEY, []).append(payload)
    except Exception:  # pragma: no cover
        logger.exception("alert mapping failed for %s", event)


# --------------------------------------------------------------------------- worker side


async def get_settings_row(db: AsyncSession, workspace_id: uuid.UUID) -> AlertSettings | None:
    return (
        await db.execute(select(AlertSettings).where(AlertSettings.workspace_id == workspace_id))
    ).scalar_one_or_none()


def _clean_recipients(numbers: list[str]) -> list[str]:
    out: list[str] = []
    for n in numbers:
        try:
            phone = normalize_phone(n)
        except ValueError:
            continue
        if phone not in out:
            out.append(phone)
    return out


async def resolve_recipients(db: AsyncSession, workspace_id: uuid.UUID | None, event: str) -> list[str]:
    """Workspace recipients (fallback WHATSAPP_TO). Global events: WHATSAPP_TO + opted-in workspaces."""
    if workspace_id is not None:
        row = await get_settings_row(db, workspace_id)
        numbers = list(row.recipients) if row and row.recipients else list(settings.WHATSAPP_TO)
        return _clean_recipients(numbers)
    numbers = list(settings.WHATSAPP_TO)
    for row in (await db.execute(select(AlertSettings).where(AlertSettings.enabled.is_(True)))).scalars():
        if effective_toggles(row.events).get(event, False):
            numbers.extend(row.recipients or [])
    return _clean_recipients(numbers)


async def _labels(db: AsyncSession, workspace_id: str | None, account_id: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if account_id:
        acc = await db.get(Account, uuid.UUID(account_id))
        if acc is not None:
            out["account"] = acc.label
            out["mode"] = f"{acc.execution_mode.value} / {'testnet' if acc.is_testnet else 'mainnet'}"
    if workspace_id:
        ws = await db.get(Workspace, uuid.UUID(workspace_id))
        if ws is not None:
            out["workspace"] = ws.name
    return out


async def process_alert(sessions: SessionFactory, payload: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Apply settings, toggles, min severity and throttle; persist the AlertRecord.

    Returns {"status": "skipped", "reason": ...} or {"status": "pending"|"disabled", "alert_id": ...}.
    `force=True` (test alerts) bypasses toggles/severity/throttle.
    """
    if not settings.ALERTS_ENABLED and not force:
        return {"status": "skipped", "reason": "alerts_disabled"}
    event = payload["event"]
    severity = payload.get("severity") or "info"
    ws_id = payload.get("workspace_id")
    spec = EVENTS.get(event)
    async with sessions() as db:
        row = await get_settings_row(db, uuid.UUID(ws_id)) if ws_id else None
        throttle_minutes = settings.ALERT_THROTTLE_MINUTES
        if row is not None and not force:
            if not row.enabled:
                return {"status": "skipped", "reason": "workspace_disabled"}
            if not severity_allows(severity, row.min_severity):
                return {"status": "skipped", "reason": "below_min_severity"}
            if row.throttle_minutes is not None:
                throttle_minutes = row.throttle_minutes
        if not force and ws_id and not effective_toggles(row.events if row else None).get(event, False):
            return {"status": "skipped", "reason": "event_disabled"}
        dedup = payload.get("dedup")
        if not force and dedup and (spec is None or spec.throttled):
            if not await throttle.acquire(f"{ws_id}:{event}:{dedup}", throttle_minutes):
                safe_inc(ALERTS, event=event, status="throttled")
                return {"status": "skipped", "reason": "throttled"}

        data = dict(payload.get("data") or {})
        data.update({k: v for k, v in (await _labels(db, ws_id, payload.get("account_id"))).items() if k not in data})
        title, message = render(event, severity, data)
        record = AlertRecord(
            workspace_id=uuid.UUID(ws_id) if ws_id else None,
            account_id=uuid.UUID(payload["account_id"]) if payload.get("account_id") else None,
            event=event,
            severity=severity,
            title=title[:200],
            message=message,
            status="pending",
            data=data,
        )
        notifier = build_notifier()
        if not notifier.configured:
            record.status = "disabled"
            record.error = "WhatsApp não configurado (WHATSAPP_ENABLED/credenciais)"
        db.add(record)
        await db.commit()
        safe_inc(ALERTS, event=event, status="created" if record.status == "pending" else record.status)
        return {"status": record.status, "alert_id": str(record.id)}


async def deliver_alert(
    sessions: SessionFactory,
    alert_id: uuid.UUID | str,
    *,
    notifier: Notifier | None = None,
    final: bool = True,
) -> dict[str, Any]:
    """Send a pending alert to every recipient. Raises DeliveryError (retry) unless `final`."""
    notifier = notifier or build_notifier()
    async with sessions() as db:
        record = await db.get(AlertRecord, uuid.UUID(str(alert_id)))
        if record is None:
            return {"status": "missing"}
        if record.status in ("sent", "disabled"):
            return {"status": record.status}
        if not notifier.configured:
            record.status = "disabled"
            record.error = "WhatsApp não configurado (WHATSAPP_ENABLED/credenciais)"
            await db.commit()
            return {"status": record.status}
        recipients = await resolve_recipients(db, record.workspace_id, record.event)
        record.recipients = [mask_phone(r) for r in recipients]
        record.attempts += 1
        if not recipients:
            record.status = "failed"
            record.error = "nenhum destinatário configurado"
            await db.commit()
            safe_inc(ALERTS, event=record.event, status="failed")
            return {"status": record.status}
        already = set((record.data or {}).get("_delivered", []))
        errors: list[str] = []
        retryable = False
        delivered = list(already)
        for phone in recipients:
            masked = mask_phone(phone)
            if masked in already:
                continue
            try:
                await notifier.send(phone, record.message)
                delivered.append(masked)
            except NotifierError as exc:
                errors.append(f"{masked}: {exc}")
                retryable = retryable or exc.retryable
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(f"{masked}: {type(exc).__name__}")
                retryable = True
        record.data = {**(record.data or {}), "_delivered": delivered}
        if not errors:
            record.status = "sent"
            record.sent_at = datetime.now(UTC)
            record.error = None
        else:
            record.status = "partial" if delivered else "failed"
            record.error = "; ".join(errors)[:500]
        await db.commit()
        safe_inc(ALERTS, event=record.event, status=record.status)
        if errors and retryable and not final:
            raise DeliveryError(record.error or "delivery failed")
        return {"status": record.status, "delivered": len(delivered), "errors": len(errors)}


# --------------------------------------------------------------------------- daily summary


async def daily_summary(sessions: SessionFactory, *, now: datetime | None = None) -> dict[str, Any]:
    """Queue one 'daily_summary' alert per workspace with accounts (previous 24h)."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=1)
    queued = 0
    async with sessions() as db:
        accounts = list((await db.execute(select(Account).order_by(Account.workspace_id, Account.label))).scalars())
        by_ws: dict[uuid.UUID, list[Account]] = {}
        for acc in accounts:
            by_ws.setdefault(acc.workspace_id, []).append(acc)
        pnl_rows = await db.execute(
            select(
                Trade.account_id,
                func.coalesce(func.sum(Trade.realized_pnl - Trade.fee), 0),
                func.count(Trade.id),
            )
            .where(Trade.executed_at >= since)
            .group_by(Trade.account_id)
        )
        pnl = {r[0]: (Decimal(str(r[1])), int(r[2])) for r in pnl_rows}
        payloads = []
        for ws_id, accs in by_ws.items():
            rows = []
            total = Decimal("0")
            for acc in accs:
                realized, n = pnl.get(acc.id, (Decimal("0"), 0))
                total += realized
                equity = acc.last_equity if acc.last_equity is not None else acc.paper_balance
                rows.append(
                    f"{acc.label}: PnL {float(realized):+,.2f} USDT · {n} trades · equity {float(equity):,.2f}"
                    f" · {acc.status.value}"
                )
            payloads.append(
                build_payload(
                    AlertEvent.DAILY_SUMMARY,
                    workspace_id=ws_id,
                    date=f"{since:%d/%m/%Y %H:%M} → {now:%d/%m/%Y %H:%M} UTC",
                    daily_pnl=f"{float(total):+,.2f} USDT",
                    rows=rows,
                )
            )
    for payload in payloads:
        if dispatch(payload):
            queued += 1
    return {"workspaces": len(payloads), "queued": queued}


async def recent_alert_counts(
    db: AsyncSession, *, hours: int = 24, workspace_id: uuid.UUID | None = None
) -> dict[str, int]:
    """Alerts created in the last `hours`, by delivery status."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    stmt = (
        select(AlertRecord.status, func.count(AlertRecord.id))
        .where(AlertRecord.created_at >= since)
        .group_by(AlertRecord.status)
    )
    if workspace_id is not None:
        stmt = stmt.where(or_(AlertRecord.workspace_id == workspace_id, AlertRecord.workspace_id.is_(None)))
    return {str(status): int(n) for status, n in (await db.execute(stmt)).all()}
