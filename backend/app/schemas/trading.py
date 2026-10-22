import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models import OrderSide
from app.schemas.common import Leverage
from app.schemas.history import OrderRead


class _SymbolModel(BaseModel):
    account_id: uuid.UUID
    symbol: str = Field(min_length=2, max_length=20, pattern=r"^[A-Za-z0-9]+$")

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class ManualOrderCreate(_SymbolModel):
    side: OrderSide
    type: Literal["MARKET", "LIMIT"] = "MARKET"
    quantity: Decimal = Field(gt=0)
    price: Decimal | None = Field(default=None, gt=0, description="Obrigatório para LIMIT")
    stop_loss: Decimal = Field(gt=0, description="Stop loss obrigatório")
    take_profit: Decimal | None = Field(default=None, gt=0)
    leverage: Leverage | None = None
    confirm: bool = Field(description="Precisa ser true (confirmação explícita no painel)")


class ClosePositionRequest(_SymbolModel):
    confirm: bool


class CloseAllRequest(BaseModel):
    account_id: uuid.UUID
    confirm: bool


class LeverageRequest(_SymbolModel):
    leverage: Leverage
    confirm: bool


class ManualOrderResult(BaseModel):
    ok: bool
    status: str
    reason: str
    mode: str
    order: OrderRead | None = None
    fill_price: Decimal | None = None
    quantity: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")


class CloseAllResult(BaseModel):
    closed: list[str]
    failed: list[str]
    orders_canceled: int


class LeverageResult(BaseModel):
    symbol: str
    leverage: int
