"""Portuguese WhatsApp message rendering (plain text with WhatsApp *bold* markup and emoji severity)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.redaction import redact_text
from app.services.alerts.events import EVENTS, SEVERITY_EMOJI, AlertEvent

MAX_LEN = 1500
BRAND = "ISCTRADE"

FIELD_LABELS: dict[str, str] = {
    "workspace": "Workspace",
    "account": "Conta",
    "mode": "Modo",
    "bot": "Bot",
    "symbol": "Símbolo",
    "side": "Lado",
    "quantity": "Quantidade",
    "price": "Preço",
    "leverage": "Alavancagem",
    "reason": "Motivo",
    "detail": "Detalhe",
    "realized_pnl": "PnL realizado",
    "daily_pnl": "PnL do dia",
    "drawdown_pct": "Drawdown (%)",
    "equity": "Equity",
    "error": "Erro",
    "stream": "Stream",
    "seconds": "Duração (s)",
    "changes": "Alterações",
    "task": "Tarefa",
    "task_id": "Task ID",
    "active": "Ativo",
    "actor": "Responsável",
    "trades": "Trades",
    "date": "Data",
    "title": "Título",
    "sentiment": "Sentimento (-1 a 1)",
    "summary": "Resumo",
    "source": "Fonte",
    "economic_event": "Evento",
    "country": "País/moeda",
    "time": "Horário",
    "minutes": "Em (min)",
    "forecast": "Projeção",
    "previous": "Anterior",
    "until": "Pausado até",
    "action": "Ação",
    "order_type": "Tipo",
    "stop_loss": "Stop loss",
    "take_profit": "Take profit",
    "result": "Resultado",
    "closed": "Posições fechadas",
    "url": "Link",
}

REASONS_PT: dict[str, str] = {
    "kill_switch": "kill switch ativo",
    "global_kill_switch": "kill switch global ativo",
    "max_daily_loss": "limite de perda diária atingido",
    "max_drawdown": "drawdown máximo atingido",
    "stop_loss_required": "stop loss obrigatório ausente",
    "leverage_limit": "alavancagem acima do limite",
    "max_risk_per_trade": "risco por trade acima do limite",
    "insufficient_margin": "margem insuficiente",
    "aggregate_daily_loss": "limite de perda diária agregada (USDT) atingido",
    "aggregate_notional": "exposição total agregada acima do limite",
    "aggregate_positions": "número máximo de posições simultâneas atingido",
    "stop_beyond_liquidation": "stop depois do (ou perto demais do) preço de liquidação",
    "leverage_bracket": "alavancagem acima do permitido pela corretora para este tamanho",
}


def _title(event: str, data: dict[str, Any]) -> str:
    if event == AlertEvent.KILL_SWITCH_GLOBAL.value:
        return "Kill switch GLOBAL ativado" if data.get("active", True) else "Kill switch global desativado"
    if event == AlertEvent.WS_DISCONNECTED.value and data.get("recovered"):
        return "WebSocket reconectado"
    if event == AlertEvent.DAILY_SUMMARY.value:
        return "Resumo diário"
    spec = EVENTS.get(event)
    return spec.label if spec else event


_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "sim" if value else "não"
    if isinstance(value, float):
        value = Decimal(str(value))
    if isinstance(value, str) and _NUMBER.match(value):
        value = Decimal(value)
    if isinstance(value, Decimal):
        text = format(value.quantize(Decimal("0.00000001")).normalize(), "f")
        return text
    return str(value)


def _reason(value: Any) -> str:
    text = str(value)
    prefix = text.split(":", 1)[0].strip()
    pt = REASONS_PT.get(prefix)
    return f"{pt} ({text})" if pt and pt != text else text


def render(event: str, severity: str, data: dict[str, Any], *, now: datetime | None = None) -> tuple[str, str]:
    """Return (title, message). Secrets are scrubbed from the final text."""
    now = now or datetime.now(UTC)
    title = _title(event, data)
    if event == AlertEvent.KILL_SWITCH_GLOBAL.value and not data.get("active", True):
        emoji = "✅"
    elif event == AlertEvent.WS_DISCONNECTED.value and data.get("recovered"):
        emoji = "✅"
    elif event == AlertEvent.DAILY_SUMMARY.value:
        emoji = "📊"
    elif event == AlertEvent.TEST.value:
        emoji = "🧪"
    else:
        emoji = SEVERITY_EMOJI.get(severity, "ℹ️")

    lines = [f"{emoji} *{BRAND} · {title}*"]
    if data.get("text"):
        lines.append(str(data["text"]))
    for key, label in FIELD_LABELS.items():
        if key in data and data[key] not in (None, "", [], {}):
            value = _reason(data[key]) if key == "reason" else _fmt(data[key])
            lines.append(f"• {label}: {value}")
    for row in data.get("rows") or []:  # daily summary: one line per account
        lines.append(f"• {row}")
    lines.append(f"🕒 {now:%d/%m/%Y %H:%M} UTC · severidade: {severity}")
    message = redact_text("\n".join(lines))
    if len(message) > MAX_LEN:
        message = message[: MAX_LEN - 1] + "…"
    return title, message
