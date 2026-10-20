import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.account import AccountStatus


class AccountCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    api_key: str = Field(min_length=8)
    api_secret: str = Field(min_length=8)
    is_testnet: bool = True


class AccountRead(BaseModel):
    """Never exposes the credentials — only the last 4 chars of the key."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    label: str
    api_key_last4: str
    is_testnet: bool
    status: AccountStatus


class AccountDashboard(BaseModel):
    account_id: uuid.UUID
    wallet_balance: Decimal = Decimal("0")
    available_balance: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl_today: Decimal = Decimal("0")
    open_positions: int = 0
    status: AccountStatus
