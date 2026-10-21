"""Account helpers: decrypting credentials into the account's exchange adapter."""

from __future__ import annotations

from app.exchanges.base import ExchangeAdapter
from app.exchanges.registry import adapter_for_account
from app.models import Account


def client_for_account(account: Account) -> ExchangeAdapter:
    """Signed adapter for ``account.exchange`` (Binance USDT-M by default). See ``app.exchanges.registry``."""
    return adapter_for_account(account)
