"""One-way mode guard. Orders are sent without positionSide, which the exchange rejects (Binance -4061) or
misroutes in hedge (dual-side) mode, and positions/reconciliation assume one net position per symbol.
Live trading therefore refuses accounts in hedge mode with an explicit message."""

from __future__ import annotations

import time
import uuid

HEDGE_MODE_MESSAGE = (
    "A conta está em hedge mode (posições LONG/SHORT separadas), que não é suportado. "
    "Mude para one-way mode na corretora (Futures > Preferências > Modo de posição) e tente de novo."
)
_CACHE_TTL = 300.0
_one_way_until: dict[uuid.UUID, float] = {}


class HedgeModeUnsupported(RuntimeError):
    def __init__(self) -> None:
        super().__init__(HEDGE_MODE_MESSAGE)


async def ensure_one_way(client, account_id: uuid.UUID) -> None:
    """Raise HedgeModeUnsupported if the account is in hedge mode. A one-way answer is cached for 5 minutes."""
    if _one_way_until.get(account_id, 0.0) > time.monotonic():
        return
    if await client.get_position_mode():
        _one_way_until.pop(account_id, None)
        raise HedgeModeUnsupported()
    _one_way_until[account_id] = time.monotonic() + _CACHE_TTL


def forget(account_id: uuid.UUID | None = None) -> None:
    if account_id is None:
        _one_way_until.clear()
    else:
        _one_way_until.pop(account_id, None)
