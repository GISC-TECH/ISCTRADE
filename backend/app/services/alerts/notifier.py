"""Notifier interface. WhatsApp is the first channel; Telegram/e-mail can implement the same protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class NotifierError(Exception):
    """Delivery failed (message must not contain secrets)."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class Notifier(Protocol):
    channel: str
    name: str

    @property
    def configured(self) -> bool: ...

    async def send(self, to: str, text: str) -> None:
        """Deliver `text` to one recipient (E.164 phone number). Raises NotifierError on failure."""
        ...


class NullNotifier:
    channel = "none"
    name = "none"
    configured = False

    async def send(self, to: str, text: str) -> None:  # pragma: no cover - never called
        raise NotifierError("no notification channel configured", retryable=False)
