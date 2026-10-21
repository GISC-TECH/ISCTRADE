import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import AccountStatus, ExecutionMode


class AccountCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    exchange: str = Field("binance_usdm", min_length=1, max_length=32)
    api_key: str = Field(min_length=8, max_length=256)
    api_secret: str = Field(min_length=8, max_length=256)
    is_testnet: bool = True
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    paper_initial_balance: Decimal = Field(Decimal("10000"), gt=0, le=Decimal("100000000"))
    max_leverage: int = Field(5, ge=1, le=125)
    max_risk_per_trade_pct: Decimal = Field(Decimal("1"), gt=0, le=100)
    max_daily_loss_pct: Decimal = Field(Decimal("3"), gt=0, le=100)
    max_drawdown_pct: Decimal = Field(Decimal("15"), gt=0, le=100)

    @field_validator("exchange")
    @classmethod
    def _known_exchange(cls, value: str) -> str:
        from app.exchanges.registry import available_exchanges

        if value not in available_exchanges():
            raise ValueError(f"exchange indisponível: {value}")
        return value


class AccountUpdate(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=120)
    execution_mode: ExecutionMode | None = None
    is_testnet: bool | None = None
    max_leverage: int | None = Field(None, ge=1, le=125)
    max_risk_per_trade_pct: Decimal | None = Field(None, gt=0, le=100)
    max_daily_loss_pct: Decimal | None = Field(None, gt=0, le=100)
    max_drawdown_pct: Decimal | None = Field(None, gt=0, le=100)


class AccountRead(BaseModel):
    """Never exposes the credentials — only the last 4 chars of the key."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    label: str
    exchange: str = "binance_usdm"
    api_key_last4: str
    is_testnet: bool
    execution_mode: ExecutionMode
    status: AccountStatus
    status_reason: str | None = None
    max_leverage: int
    max_risk_per_trade_pct: Decimal
    max_daily_loss_pct: Decimal
    max_drawdown_pct: Decimal
    paper_balance: Decimal
    paper_initial_balance: Decimal
    last_equity: Decimal | None = None
    created_at: datetime


class PositionOut(BaseModel):
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal | None = None
    unrealized_pnl: Decimal
    leverage: int
    stop_loss: Decimal | None = None


class AccountDashboard(BaseModel):
    account_id: uuid.UUID
    source: str
    status: AccountStatus
    status_reason: str | None = None
    execution_mode: ExecutionMode
    is_testnet: bool
    wallet_balance: Decimal = Decimal("0")
    available_balance: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    equity: Decimal = Decimal("0")
    realized_pnl_today: Decimal = Decimal("0")
    total_pnl: Decimal | None = None
    open_positions: int = 0
    positions: list[PositionOut] = []
    peak_equity: Decimal | None = None
    day_start_equity: Decimal | None = None
    drawdown_pct: float = 0.0
    error: str | None = None


class ExchangeInfoOut(BaseModel):
    id: str
    name: str
    experimental: bool
    testnet: bool
    user_data_stream: bool
    market_streams: bool
    notes: list[str] = []


class AccountVerifyResult(BaseModel):
    ok: bool
    can_trade: bool | None = None
    wallet_balance: Decimal | None = None
    hedge_mode: bool | None = None
    error: str | None = None
