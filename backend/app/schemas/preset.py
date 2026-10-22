from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.backtest import SYMBOL_PATTERN
from app.schemas.bot import INTERVALS
from app.schemas.bot_options import BotOptions
from app.schemas.common import Leverage

RiskLevel = Literal["conservador", "moderado", "agressivo"]


def _clean_tags(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    out: list[str] = []
    for raw in value:
        tag = raw.strip()[:30]
        if tag and tag not in out:
            out.append(tag)
    return out


class PresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field("", max_length=2000)
    strategy_key: str = Field(min_length=1, max_length=60)
    interval: str = Field("1h", pattern=INTERVALS)
    symbol: str = Field("BTCUSDT", pattern=SYMBOL_PATTERN)
    leverage: Leverage = 1
    params: dict[str, Any] = Field(default_factory=dict)
    options: BotOptions = Field(default_factory=BotOptions)
    risk_level: RiskLevel = "moderado"
    tags: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        return _clean_tags(value) or []


class PresetUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    interval: str | None = Field(None, pattern=INTERVALS)
    symbol: str | None = Field(None, pattern=SYMBOL_PATTERN)
    leverage: Leverage | None = None
    params: dict[str, Any] | None = None
    options: BotOptions | None = None
    risk_level: RiskLevel | None = None
    tags: list[str] | None = Field(None, max_length=10)

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str] | None) -> list[str] | None:
        return _clean_tags(value)


class PresetClone(BaseModel):
    source_key: str = Field(min_length=1, max_length=80)
    name: str | None = Field(None, min_length=1, max_length=120)
