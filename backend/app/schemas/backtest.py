import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import BacktestStatus

SYMBOL_PATTERN = r"^[A-Z0-9]{2,20}$"


class BacktestConfigIn(BaseModel):
    initial_balance: float = Field(10000, gt=0)
    fee_rate: float = Field(0.0004, ge=0, le=0.01)
    slippage_bps: float = Field(2, ge=0, le=500)
    leverage: int = Field(1, ge=1, le=125)
    max_risk_per_trade_pct: float = Field(1, gt=0, le=100)
    max_leverage: int = Field(5, ge=1, le=125)
    max_daily_loss_pct: float = Field(3, gt=0, le=100)
    max_drawdown_pct: float = Field(15, gt=0, le=100)
    stop_on_kill_switch: bool = True


class BacktestRequest(BaseModel):
    strategy_key: str
    params: dict[str, Any] = Field(default_factory=dict)
    symbol: str = Field("BTCUSDT", pattern=SYMBOL_PATTERN)
    interval: str = "1h"
    start: datetime | None = None
    end: datetime | None = None
    candles_csv: str | None = Field(None, max_length=10_000_000)
    use_testnet_data: bool = False
    config: BacktestConfigIn = Field(default_factory=BacktestConfigIn)

    @model_validator(mode="after")
    def _source(self) -> "BacktestRequest":
        if not self.candles_csv and not (self.start and self.end):
            raise ValueError("informe start/end (dados da Binance) ou candles_csv")
        return self


class BacktestSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    strategy_key: str
    symbol: str
    interval: str
    status: BacktestStatus
    params: dict[str, Any]
    metrics: dict[str, Any]
    created_at: datetime
    error: str | None = None


class BacktestDetail(BacktestSummary):
    config: dict[str, Any]
    equity_curve: list[Any]
    trades: list[Any]
    start_time: datetime | None = None
    end_time: datetime | None = None
