import hashlib
import hmac
from urllib.parse import urlencode

from app.services.binance_client import BinanceFuturesClient


async def test_signature_and_testnet_url() -> None:
    client = BinanceFuturesClient("dummy-key", "dummy-secret", testnet=True)
    params = {"symbol": "BTCUSDT", "timestamp": 1}
    expected = hmac.new(b"dummy-secret", urlencode(params).encode(), hashlib.sha256).hexdigest()
    assert client.sign(params) == expected
    assert "testnet" in client.base_url
    await client.close()
