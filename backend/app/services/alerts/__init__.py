"""Alerting service (WhatsApp first; the Notifier protocol allows Telegram/e-mail later).

See docs/ALERTS.md.
"""

from app.services.alerts.events import EVENTS, AlertEvent, Severity
from app.services.alerts.notifier import Notifier, NotifierError
from app.services.alerts.service import emit_alert, queue_alert

__all__ = ["EVENTS", "AlertEvent", "Notifier", "NotifierError", "Severity", "emit_alert", "queue_alert"]
