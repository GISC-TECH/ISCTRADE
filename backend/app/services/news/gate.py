"""Pure decision logic for the news filter and the economic-event pause.

Shared by the live bot runner and the backtest engine, so both behave the same given the same data.
News and events never CREATE trades: they can only block a new entry or reduce its size. Exits
(reduce-only intents) are never affected.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.models.enums import OrderSide
from app.schemas.bot_options import EventPauseOptions, NewsFilterOptions

IMPACT_WEIGHT = {"low": 1.0, "medium": 2.0, "high": 3.0}
IMPACT_RANK = {"low": 0, "medium": 1, "high": 2, "holiday": -1}
ONE = Decimal("1")


@dataclass(frozen=True)
class NewsSignal:
    published_at: datetime
    sentiment: float
    impact: str
    symbols: tuple[str, ...]
    title: str = ""


@dataclass(frozen=True)
class EventInfo:
    key: str
    title: str
    country: str
    impact: str
    time: datetime


@dataclass
class AssetSentiment:
    symbol: str
    score: float = 0.0  # weighted, time-decayed mean in -1..1
    confidence: float = 0.0  # 0..1 (total decayed weight / 3, capped)
    count: int = 0
    high_impact_negative: NewsSignal | None = None
    latest: list[NewsSignal] = field(default_factory=list)

    def as_dict(self) -> dict:
        hin = self.high_impact_negative
        return {
            "symbol": self.symbol,
            "score": round(self.score, 3),
            "confidence": round(self.confidence, 3),
            "count": self.count,
            "high_impact_negative": {"title": hin.title, "published_at": hin.published_at.isoformat()} if hin else None,
        }


@dataclass
class GateDecision:
    block: str | None = None
    multiplier: Decimal = ONE
    notes: list[str] = field(default_factory=list)

    def merge(self, other: GateDecision) -> GateDecision:
        return GateDecision(
            self.block or other.block, min(self.multiplier, other.multiplier), [*self.notes, *other.notes]
        )


def aggregate(
    signals: Iterable[NewsSignal],
    symbol: str,
    now: datetime,
    half_life_hours: float = 6.0,
    window_hours: float = 48.0,
    high_impact_minutes: int = 120,
) -> AssetSentiment:
    """Time-decayed, impact-weighted sentiment of `symbol` as of `now` (future items are ignored)."""
    out = AssetSentiment(symbol)
    total_w = acc = 0.0
    hl = max(half_life_hours, 0.01)
    for s in signals:
        if symbol not in s.symbols:
            continue
        age_h = (now - s.published_at).total_seconds() / 3600
        if age_h < 0 or age_h > window_hours:
            continue
        w = IMPACT_WEIGHT.get(s.impact, 1.0) * 0.5 ** (age_h / hl)
        total_w += w
        acc += w * s.sentiment
        out.count += 1
        out.latest.append(s)
        if (
            s.impact == "high"
            and s.sentiment < 0
            and age_h * 60 <= high_impact_minutes
            and (out.high_impact_negative is None or s.published_at > out.high_impact_negative.published_at)
        ):
            out.high_impact_negative = s
    if total_w > 0:
        out.score = max(-1.0, min(1.0, acc / total_w))
        out.confidence = min(1.0, total_w / 3.0)
    out.latest.sort(key=lambda s: s.published_at, reverse=True)
    out.latest = out.latest[:5]
    return out


def news_gate(side: OrderSide, sent: AssetSentiment | None, opts: NewsFilterOptions) -> GateDecision:
    if opts.mode == "off" or sent is None or sent.count == 0:
        return GateDecision()
    reasons = []
    if opts.high_impact_minutes > 0 and sent.high_impact_negative is not None:
        reasons.append("news_high_impact_negative")
    if sent.confidence >= opts.min_confidence:
        if side == OrderSide.BUY and sent.score <= -opts.opposing_threshold:
            reasons.append("news_sentiment_opposing")
        elif side == OrderSide.SELL and sent.score >= opts.opposing_threshold:
            reasons.append("news_sentiment_opposing")
    if not reasons:
        return GateDecision()
    note = f"{reasons[0]}:{sent.score:+.2f}"
    if opts.mode == "reduce_risk":
        return GateDecision(None, Decimal(str(opts.size_multiplier)), [note])
    return GateDecision(note, ONE, [note])


def event_gate(
    events: Iterable[EventInfo], now: datetime, opts: EventPauseOptions
) -> tuple[GateDecision, EventInfo | None]:
    """Return the decision and the event that caused it (block during the pause, reduce in the tighten window)."""
    if not opts.enabled:
        return GateDecision(), None
    min_rank = IMPACT_RANK[opts.min_impact]
    countries = {c.upper() for c in opts.countries}
    before, after = timedelta(minutes=opts.minutes_before), timedelta(minutes=opts.minutes_after)
    tighten = timedelta(minutes=opts.tighten_minutes)
    soft: EventInfo | None = None
    for ev in sorted(events, key=lambda e: e.time):
        if IMPACT_RANK.get(ev.impact, -1) < min_rank:
            continue
        if countries and ev.country.upper() not in countries:
            continue
        if ev.time - before <= now <= ev.time + after:
            return GateDecision(f"economic_event:{ev.title}"[:120], ONE, [ev.key]), ev
        if soft is None and opts.risk_multiplier < 1 and ev.time - before - tighten <= now <= ev.time + after + tighten:
            soft = ev
    if soft is not None:
        return GateDecision(None, Decimal(str(opts.risk_multiplier)), [f"economic_event_window:{soft.key}"]), soft
    return GateDecision(), None


def apply_multiplier(intent, multiplier: Decimal):
    """Scale a (non reduce-only) intent's size; never increases it."""
    from dataclasses import replace

    if multiplier >= ONE or intent.reduce_only:
        return intent
    if intent.quantity is not None:
        return replace(intent, quantity=intent.quantity * multiplier)
    return replace(intent, risk_fraction=intent.risk_fraction * multiplier)
