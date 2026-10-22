from typing import Annotated, Generic, TypeVar

from pydantic import AfterValidator, BaseModel, Field

# Absolute ceiling regardless of configuration (exchanges allow 125x; nothing here ever should).
HARD_MAX_LEVERAGE = 50


def _leverage_cap(value: int) -> int:
    from app.core.config import settings

    cap = min(int(settings.RISK_MAX_LEVERAGE_CAP), HARD_MAX_LEVERAGE)
    if value > cap:
        raise ValueError(f"alavancagem acima do teto da plataforma ({cap}x, RISK_MAX_LEVERAGE_CAP)")
    return value


Leverage = Annotated[int, Field(ge=1, le=HARD_MAX_LEVERAGE), AfterValidator(_leverage_cap)]

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
