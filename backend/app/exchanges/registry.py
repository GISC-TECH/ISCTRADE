"""Registry / factory of exchange adapters, selected per account (``Account.exchange``).

    async with adapter_for_account(account) as client: ...      # signed, account credentials
    async with public_adapter(testnet=True) as client: ...      # public market data only

New adapters call :func:`register_exchange` (see docs/EXCHANGES.md, "Como adicionar uma nova exchange").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.exchanges.base import ExchangeAdapter

if TYPE_CHECKING:
    from app.models import Account

DEFAULT_EXCHANGE = "binance_usdm"

_REGISTRY: dict[str, type[ExchangeAdapter]] = {}


class UnknownExchangeError(ValueError):
    """Unknown exchange id, or an experimental adapter while experimental adapters are disabled."""


def register_exchange(adapter_cls: type[ExchangeAdapter]) -> type[ExchangeAdapter]:
    """Register an adapter class under its ``exchange_id`` (usable as a decorator)."""
    if not adapter_cls.exchange_id:
        raise ValueError(f"{adapter_cls.__name__} has no exchange_id")
    _REGISTRY[adapter_cls.exchange_id] = adapter_cls
    return adapter_cls


def _ensure_builtin() -> None:
    if DEFAULT_EXCHANGE in _REGISTRY:
        return
    from app.exchanges.binance_usdm import BinanceUSDMAdapter
    from app.exchanges.bybit import BybitLinearAdapter

    register_exchange(BinanceUSDMAdapter)
    register_exchange(BybitLinearAdapter)


def is_enabled(adapter_cls: type[ExchangeAdapter]) -> bool:
    return not adapter_cls.experimental or settings.EXPERIMENTAL_EXCHANGES_ENABLED


def available_exchanges(include_disabled: bool = False) -> dict[str, type[ExchangeAdapter]]:
    _ensure_builtin()
    return {k: v for k, v in _REGISTRY.items() if include_disabled or is_enabled(v)}


def get_adapter_class(exchange: str | None = None) -> type[ExchangeAdapter]:
    _ensure_builtin()
    exchange = exchange or DEFAULT_EXCHANGE
    adapter_cls = _REGISTRY.get(exchange)
    if adapter_cls is None:
        raise UnknownExchangeError(f"unknown exchange '{exchange}'")
    if not is_enabled(adapter_cls):
        raise UnknownExchangeError(
            f"exchange '{exchange}' is experimental and disabled (EXPERIMENTAL_EXCHANGES_ENABLED=false)"
        )
    return adapter_cls


def create_adapter(
    exchange: str | None, api_key: str = "", api_secret: str = "", testnet: bool | None = None, **kw: Any
) -> ExchangeAdapter:
    return get_adapter_class(exchange)(api_key=api_key, api_secret=api_secret, testnet=testnet, **kw)


def public_adapter(testnet: bool | None = None, exchange: str | None = None, **kw: Any) -> ExchangeAdapter:
    """Unauthenticated adapter for public market data (klines, tickers, filters, funding)."""
    return create_adapter(exchange, "", "", testnet=testnet, **kw)


def adapter_for_account(account: Account, **kw: Any) -> ExchangeAdapter:
    """Signed adapter for ``account`` (decrypts its credentials, bound to its workspace)."""
    from app.core.security import decrypt_api_key

    aad = str(account.workspace_id).encode()
    return create_adapter(
        getattr(account, "exchange", None) or DEFAULT_EXCHANGE,
        api_key=decrypt_api_key(account.encrypted_api_key, aad),
        api_secret=decrypt_api_key(account.encrypted_api_secret, aad),
        testnet=account.is_testnet,
        **kw,
    )
