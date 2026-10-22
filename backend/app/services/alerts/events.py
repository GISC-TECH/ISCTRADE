"""Alert event catalogue: keys, default severity, default on/off and Portuguese labels."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


SEVERITY_ORDER = {Severity.INFO.value: 0, Severity.WARNING.value: 1, Severity.CRITICAL.value: 2}
SEVERITY_EMOJI = {Severity.INFO.value: "ℹ️", Severity.WARNING.value: "⚠️", Severity.CRITICAL.value: "🚨"}


class AlertEvent(str, Enum):
    KILL_SWITCH_ACCOUNT = "kill_switch_account"
    KILL_SWITCH_GLOBAL = "kill_switch_global"
    RISK_LIMIT = "risk_limit"
    ORDER_REJECTED = "order_rejected"
    ORDER_FILLED = "order_filled"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    BOT_ERROR = "bot_error"
    WS_DISCONNECTED = "ws_disconnected"
    RECONCILER_MISMATCH = "reconciler_mismatch"
    TASK_FAILED = "task_failed"
    DAILY_SUMMARY = "daily_summary"
    NEWS_HIGH_IMPACT = "news_high_impact"
    ECONOMIC_EVENT = "economic_event"
    BOT_EVENT_PAUSE = "bot_event_pause"
    MANUAL_ACTION = "manual_action"
    INFRA_ALERT = "infra_alert"
    POSITION_UNPROTECTED = "position_unprotected"
    PROTECTIVE_STOP_RESTORED = "protective_stop_restored"
    TEST = "test"


@dataclass(frozen=True)
class EventSpec:
    severity: Severity
    enabled_by_default: bool
    label: str
    throttled: bool = True


EVENTS: dict[str, EventSpec] = {
    AlertEvent.KILL_SWITCH_ACCOUNT.value: EventSpec(Severity.CRITICAL, True, "Kill switch da conta acionado"),
    AlertEvent.KILL_SWITCH_GLOBAL.value: EventSpec(Severity.CRITICAL, True, "Kill switch global", throttled=False),
    AlertEvent.RISK_LIMIT.value: EventSpec(Severity.CRITICAL, True, "Limite de drawdown/perda diária atingido"),
    AlertEvent.ORDER_REJECTED.value: EventSpec(Severity.WARNING, True, "Ordem rejeitada pelo gerenciador de risco"),
    AlertEvent.ORDER_FILLED.value: EventSpec(Severity.INFO, False, "Ordem executada", throttled=False),
    AlertEvent.POSITION_OPENED.value: EventSpec(Severity.INFO, False, "Posição aberta", throttled=False),
    AlertEvent.POSITION_CLOSED.value: EventSpec(Severity.INFO, False, "Posição encerrada", throttled=False),
    AlertEvent.BOT_ERROR.value: EventSpec(Severity.WARNING, True, "Bot entrou em estado de erro"),
    AlertEvent.WS_DISCONNECTED.value: EventSpec(Severity.WARNING, True, "WebSocket desconectado"),
    AlertEvent.RECONCILER_MISMATCH.value: EventSpec(Severity.WARNING, True, "Divergência na reconciliação"),
    AlertEvent.TASK_FAILED.value: EventSpec(Severity.WARNING, True, "Falha em tarefa Celery"),
    AlertEvent.DAILY_SUMMARY.value: EventSpec(Severity.INFO, True, "Resumo diário de PnL", throttled=False),
    AlertEvent.NEWS_HIGH_IMPACT.value: EventSpec(
        Severity.WARNING, True, "Notícia de alto impacto em ativo operado", throttled=False
    ),
    AlertEvent.ECONOMIC_EVENT.value: EventSpec(Severity.INFO, True, "Evento econômico de alto impacto em breve"),
    AlertEvent.BOT_EVENT_PAUSE.value: EventSpec(Severity.INFO, True, "Bot pausou novas entradas (evento econômico)"),
    AlertEvent.MANUAL_ACTION.value: EventSpec(
        Severity.INFO, True, "Ação manual de trading (ordem, fechamento, alavancagem)", throttled=False
    ),
    AlertEvent.INFRA_ALERT.value: EventSpec(Severity.WARNING, True, "Alerta de infraestrutura (Prometheus)"),
    AlertEvent.POSITION_UNPROTECTED.value: EventSpec(
        Severity.CRITICAL, True, "Posição desprotegida (sem stop na corretora)"
    ),
    AlertEvent.PROTECTIVE_STOP_RESTORED.value: EventSpec(
        Severity.WARNING, True, "Stop de proteção recolocado / posição externa adotada"
    ),
    AlertEvent.TEST.value: EventSpec(Severity.INFO, True, "Alerta de teste", throttled=False),
}

CONFIGURABLE_EVENTS = [k for k in EVENTS if k != AlertEvent.TEST.value]


def default_toggles() -> dict[str, bool]:
    return {k: EVENTS[k].enabled_by_default for k in CONFIGURABLE_EVENTS}


def effective_toggles(overrides: dict[str, bool] | None) -> dict[str, bool]:
    toggles = default_toggles()
    for k, v in (overrides or {}).items():
        if k in toggles:
            toggles[k] = bool(v)
    return toggles


def severity_allows(severity: str, minimum: str) -> bool:
    return SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER.get(minimum, 0)
