import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.enums import OrderSide, OrderStatus, OrderType


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    bot_id: uuid.UUID | None
    symbol: str
    side: OrderSide
    type: OrderType
    status: OrderStatus
    quantity: Decimal
    price: Decimal | None
    stop_price: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None = None
    leverage: int
    reduce_only: bool
    executed_qty: Decimal
    avg_price: Decimal | None
    client_order_id: str
    exchange_order_id: str | None
    is_paper: bool
    reason: str | None
    created_at: datetime


class TradeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    order_id: uuid.UUID | None
    bot_id: uuid.UUID | None
    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    realized_pnl: Decimal
    is_paper: bool
    executed_at: datetime


class EquityPointRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: uuid.UUID
    wallet_balance: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    is_paper: bool
    taken_at: datetime


class AuditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    event: str
    level: str
    user_id: uuid.UUID | None
    workspace_id: uuid.UUID | None
    account_id: uuid.UUID | None
    ip_address: str | None
    data: dict[str, Any]
    # tamper-evident chain (null until sealed, see app/services/audit_chain.py)
    chain_seq: int | None = None
    row_hash: str | None = None


class PositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: uuid.UUID
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal | None
    unrealized_pnl: Decimal
    leverage: int
    stop_loss: Decimal | None
    take_profit: Decimal | None = None
    is_paper: bool
    updated_at: datetime
