import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import BotState
from app.schemas.backtest import SYMBOL_PATTERN
from app.schemas.bot_options import BotOptions
from app.schemas.common import Leverage

INTERVALS = r"^(1m|3m|5m|15m|30m|1h|2h|4h|6h|8h|12h|1d)$"


class BotCreate(BaseModel):
    account_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    strategy_key: str
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    interval: str = Field("1h", pattern=INTERVALS)
    leverage: Leverage = 1
    params: dict[str, Any] = Field(default_factory=dict)
    options: BotOptions = Field(default_factory=BotOptions)


class BotUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    interval: str | None = Field(None, pattern=INTERVALS)
    leverage: Leverage | None = None
    params: dict[str, Any] | None = None
    options: BotOptions | None = None
    is_enabled: bool | None = None


class BotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    name: str
    strategy_key: str
    symbol: str
    interval: str
    leverage: int
    params: dict[str, Any]
    options: dict[str, Any] = Field(default_factory=dict)
    state: BotState
    runtime: dict[str, Any]
    is_enabled: bool
    last_tick_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
