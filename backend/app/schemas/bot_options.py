"""Bot-level options (stored in BotConfig.options), independent of the strategy parameters."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AutoSymbolsOptions(BaseModel):
    """Auto symbol selection: the bot trades the top-N symbols of the latest market scan."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    top_n: int = Field(3, ge=1, le=20)
    max_positions: int = Field(2, ge=1, le=20)  # max concurrent open positions for this bot
    min_score: float = Field(0.0, ge=0, le=100)
    prefer_signals: bool = True  # rank symbols where this bot's strategy has a signal first


class NewsFilterOptions(BaseModel):
    """News/sentiment FILTER for new entries (never a trigger). Exits are never blocked."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "filter", "reduce_risk"] = "off"
    # aggregated sentiment against the trade direction at/over this magnitude counts as "strong opposing"
    opposing_threshold: float = Field(0.4, ge=0.05, le=1)
    min_confidence: float = Field(0.3, ge=0, le=1)  # min aggregated weight (0..1) before sentiment is trusted
    high_impact_minutes: int = Field(120, ge=0, le=1440)  # high-impact negative news window (0 = ignore)
    size_multiplier: float = Field(0.5, ge=0.05, le=1)  # used by reduce_risk instead of blocking


class EventPauseOptions(BaseModel):
    """Pause new entries around high-impact economic events (exits keep working)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    minutes_before: int = Field(30, ge=0, le=720)
    minutes_after: int = Field(30, ge=0, le=720)
    min_impact: Literal["medium", "high"] = "high"
    countries: list[str] = Field(default_factory=lambda: ["USD"], max_length=20)
    tighten_minutes: int = Field(60, ge=0, le=1440)  # extra window around the pause with reduced size
    risk_multiplier: float = Field(1.0, ge=0.05, le=1)  # 1 = do not tighten

    @field_validator("countries")
    @classmethod
    def _countries(cls, v: list[str]) -> list[str]:
        out = []
        for c in v:
            c = c.strip().upper()
            if not c or len(c) > 10 or not c.isalnum():
                raise ValueError(f"país/moeda inválido: {c!r}")
            out.append(c)
        return out


class BotOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_symbols: AutoSymbolsOptions = Field(default_factory=AutoSymbolsOptions)
    news_filter: NewsFilterOptions = Field(default_factory=NewsFilterOptions)
    event_pause: EventPauseOptions = Field(default_factory=EventPauseOptions)

    @classmethod
    def load(cls, raw: dict | None) -> BotOptions:
        """Lenient load for stored rows (unknown keys from newer versions are ignored)."""
        data = dict(raw or {})
        known = set(cls.model_fields)
        return cls.model_validate({k: v for k, v in data.items() if k in known})
