"""Bot-level options (stored in BotConfig.options), independent of the strategy parameters."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AutoSymbolsOptions(BaseModel):
    """Auto symbol selection: the bot trades the top-N symbols of the latest market scan."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    top_n: int = Field(3, ge=1, le=20)
    max_positions: int = Field(2, ge=1, le=20)  # max concurrent open positions for this bot
    min_score: float = Field(0.0, ge=0, le=100)
    prefer_signals: bool = True  # rank symbols where this bot's strategy has a signal first


class BotOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_symbols: AutoSymbolsOptions = Field(default_factory=AutoSymbolsOptions)

    @classmethod
    def load(cls, raw: dict | None) -> BotOptions:
        """Lenient load for stored rows (unknown keys from newer versions are ignored)."""
        data = dict(raw or {})
        known = set(cls.model_fields)
        return cls.model_validate({k: v for k, v in data.items() if k in known})
