"""Account helpers: decrypting credentials into a Binance client."""

from __future__ import annotations

from app.core.security import decrypt_api_key
from app.models import Account
from app.services.binance_client import BinanceFuturesClient


def client_for_account(account: Account) -> BinanceFuturesClient:
    aad = str(account.workspace_id).encode()
    return BinanceFuturesClient(
        api_key=decrypt_api_key(account.encrypted_api_key, aad),
        api_secret=decrypt_api_key(account.encrypted_api_secret, aad),
        testnet=account.is_testnet,
    )
