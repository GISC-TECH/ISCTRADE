"""Strategy interface shared by live/paper bots and the backtest engine."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar

from app.models.enums import OrderSide


@dataclass(frozen=True)
class Candle:
    open_time: int  # ms epoch
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int  # ms epoch

    @classmethod
    def from_binance(cls, row: list[Any]) -> Candle:
        return cls(
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(row[6]),
        )


@dataclass
class OrderIntent:
    """What a strategy wants to do. Every intent is validated by the RiskManager before execution."""

    side: OrderSide
    quantity: Decimal | None = None  # None -> sized from the per-trade risk limit
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None  # optional; entries only (TAKE_PROFIT_MARKET live, simulated in paper/backtest)
    reduce_only: bool = False
    tag: str = ""
    risk_fraction: Decimal = Decimal("1")  # fraction of the max risk used when auto-sizing
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyContext:
    symbol: str
    candles: list[Candle]  # closed candles, oldest first
    position_qty: Decimal  # signed: > 0 long, < 0 short
    position_entry: Decimal
    equity: Decimal
    leverage: int
    params: dict[str, Any]
    state: dict[str, Any]  # persisted between ticks (JSON-serializable)

    @property
    def price(self) -> Decimal:
        return Decimal(str(self.candles[-1].close))

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str  # "number" | "integer" | "boolean"
    default: Any
    description: str = ""
    required: bool = False
    min: float | None = None
    max: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class StrategyParamError(ValueError):
    pass


class Strategy(ABC):
    key: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str] = ""
    params_spec: ClassVar[tuple[ParamSpec, ...]] = ()

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = self.validate_params(params or {})

    @classmethod
    def validate_params(cls, params: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        known = {p.name for p in cls.params_spec}
        unknown = set(params) - known
        if unknown:
            raise StrategyParamError(f"unknown params: {', '.join(sorted(unknown))}")
        for spec in cls.params_spec:
            raw = params.get(spec.name, spec.default)
            if raw is None:
                if spec.required:
                    raise StrategyParamError(f"param '{spec.name}' is required")
                out[spec.name] = None
                continue
            try:
                if spec.type == "integer":
                    value: Any = int(raw)
                elif spec.type == "number":
                    value = float(raw)
                elif spec.type == "boolean":
                    value = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes")
                else:
                    value = raw
            except (TypeError, ValueError) as exc:
                raise StrategyParamError(f"param '{spec.name}' must be {spec.type}") from exc
            if spec.min is not None and value < spec.min:
                raise StrategyParamError(f"param '{spec.name}' must be >= {spec.min}")
            if spec.max is not None and value > spec.max:
                raise StrategyParamError(f"param '{spec.name}' must be <= {spec.max}")
            out[spec.name] = value
        cls.check_params(out)
        return out

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        """Cross-field validation hook."""

    @property
    def warmup(self) -> int:
        """Minimum number of closed candles needed before producing signals."""
        return 2

    @abstractmethod
    def on_candle(self, ctx: StrategyContext) -> list[OrderIntent]:
        """Called once per new closed candle. Must not mutate state that depends on fills."""

    def on_result(self, intent: OrderIntent, filled: bool, ctx: StrategyContext) -> None:
        """Called after each intent is executed (filled=True) or rejected (filled=False)."""

    def on_stop_out(self, ctx: StrategyContext) -> None:
        """Called when the position was closed by its stop loss."""

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "key": cls.key,
            "name": cls.name,
            "description": cls.description,
            "params": [p.as_dict() for p in cls.params_spec],
        }


def to_decimal(value: float | int | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
