from decimal import Decimal

from pydantic import BaseModel, Field


class RiskLimitsIn(BaseModel):
    """Omitted fields keep their current value; null or 0 disables the limit (loosening: admin only)."""

    max_total_notional_usdt: Decimal | None = Field(None, ge=0, le=Decimal("1000000000"))
    max_daily_loss_usdt: Decimal | None = Field(None, ge=0, le=Decimal("1000000000"))
    max_open_positions: int | None = Field(None, ge=0, le=10000)


class RiskLimitsOut(BaseModel):
    max_total_notional_usdt: Decimal | None = None
    max_daily_loss_usdt: Decimal | None = None
    max_open_positions: int | None = None


class RiskLimitsOverview(BaseModel):
    workspace: RiskLimitsOut
    workspace_defaults: RiskLimitsOut
    global_limits: RiskLimitsOut
    live_account_max_capital_usdt: Decimal | None = None
    max_leverage_cap: int
    include_paper: bool
