"""Secret redaction shared by logs, audit data and alert messages."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from enum import Enum
from typing import Any

MASK = "***"
SENSITIVE_KEYS = ("secret", "password", "api_key", "apikey", "token", "signature", "authorization", "listenkey")

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization: Bearer xxx / Bearer xxx
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 " + MASK),
    # key=value / "key": "value" / key: value for sensitive names
    (
        re.compile(
            r"(?i)(\b(?:api[_-]?key|apikey|api[_-]?secret|secret|token|access[_-]?token|password|passwd|"
            r"signature|x-mbx-apikey|listen[_-]?key)\b[\"']?\s*[:=]\s*[\"']?)([^\s\"'&,;}]+)"
        ),
        r"\1" + MASK,
    ),
    # Telegram-style bot tokens (123456:ABC...)
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), MASK),
    # Long opaque credentials (Binance keys are 64 alphanumeric chars)
    (re.compile(r"\b[A-Za-z0-9]{40,}\b"), MASK),
]


def redact_text(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def is_sensitive_key(key: str) -> bool:
    k = key.lower().replace("-", "_")
    return any(s in k for s in SENSITIVE_KEYS)


def redact(value: Any) -> Any:
    """Recursively mask sensitive keys and scrub secrets from strings; returns JSON-friendly values."""
    if isinstance(value, dict):
        return {str(k): (MASK if is_sensitive_key(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(v) for v in value]
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact_text(str(value))


def mask_phone(number: str) -> str:
    digits = re.sub(r"\D", "", number)
    if len(digits) <= 6:
        return "*" * len(digits)
    return f"+{digits[:4]}{'*' * (len(digits) - 8)}{digits[-4:]}"
