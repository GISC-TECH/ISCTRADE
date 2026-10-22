"""Bot presets: built-ins (read-only, in code) + custom presets saved per workspace.

Built-ins are never edited in place: a workspace admin clones one and edits the copy. Custom presets can be created,
edited and deleted by traders (their own) and workspace admins (any). Like built-ins, custom presets only pre-fill the
bot form and never carry an execution mode — bots always inherit the account mode and pass the RiskManager.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Role, has_role
from app.models import CustomPreset
from app.models.base import as_utc
from app.schemas.bot_options import BotOptions
from app.strategy.presets import PRESETS_BY_KEY, list_presets
from app.strategy.strategies import get_strategy_class

CUSTOM_PREFIX = "custom:"
MAX_CUSTOM_PRESETS = 100
CLONED_FIELDS = (
    "name",
    "description",
    "strategy_key",
    "interval",
    "symbol",
    "leverage",
    "params",
    "options",
    "risk_level",
    "tags",
)


class PresetError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def custom_key(preset_id: uuid.UUID) -> str:
    return f"{CUSTOM_PREFIX}{preset_id}"


def validate_strategy(strategy_key: str, params: dict[str, Any]) -> dict[str, Any]:
    """Complete, validated parameter set (raises StrategyParamError -> 422)."""
    return get_strategy_class(strategy_key).validate_params(params)


def can_edit(p: CustomPreset, user_id: uuid.UUID | None, role: Role | None) -> bool:
    if role is None:
        return False
    return has_role(role, Role.ADMIN) or (has_role(role, Role.TRADER) and p.created_by == user_id)


def custom_as_dict(p: CustomPreset, *, user_id: uuid.UUID | None = None, role: Role | None = None) -> dict[str, Any]:
    cls = get_strategy_class(p.strategy_key)
    return {
        "key": custom_key(p.id),
        "id": str(p.id),
        "source": "custom",
        "editable": can_edit(p, user_id, role),
        "name": p.name,
        "description": p.description,
        "strategy_key": p.strategy_key,
        "strategy_name": cls.name,
        "interval": p.interval,
        "symbol": p.symbol,
        "leverage": p.leverage,
        "params": cls.validate_params(p.params or {}),
        "options": BotOptions.model_validate(p.options or {}).model_dump(),
        "risk_level": p.risk_level,
        "experimental": bool(getattr(cls, "experimental", False)),
        "execution_mode": "paper",
        "tags": list(p.tags or []),
        "based_on": p.based_on,
        "created_by": str(p.created_by) if p.created_by else None,
        "created_at": as_utc(p.created_at).isoformat() if p.created_at else None,
        "updated_at": as_utc(p.updated_at).isoformat() if p.updated_at else None,
    }


def builtin_list() -> list[dict[str, Any]]:
    return [{**p, "id": None, "source": "builtin", "editable": False, "based_on": None} for p in list_presets()]


async def list_all(db: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, role: Role) -> list[dict[str, Any]]:
    rows = (
        await db.scalars(
            select(CustomPreset).where(CustomPreset.workspace_id == workspace_id).order_by(CustomPreset.name)
        )
    ).all()
    customs = []
    for row in rows:
        try:
            customs.append(custom_as_dict(row, user_id=user_id, role=role))
        except Exception:  # noqa: BLE001 - a strategy removed from the code must not break the list
            continue
    return customs + builtin_list()


async def get_custom(db: AsyncSession, workspace_id: uuid.UUID, preset_id: uuid.UUID) -> CustomPreset:
    row = await db.get(CustomPreset, preset_id)
    if row is None or row.workspace_id != workspace_id:
        raise PresetError(404, "preset não encontrado")
    return row


async def ensure_capacity(db: AsyncSession, workspace_id: uuid.UUID) -> None:
    count = await db.scalar(
        select(func.count()).select_from(CustomPreset).where(CustomPreset.workspace_id == workspace_id)
    )
    if (count or 0) >= MAX_CUSTOM_PRESETS:
        raise PresetError(409, f"limite de {MAX_CUSTOM_PRESETS} presets personalizados por workspace")


def ensure_can_edit(row: CustomPreset, user_id: uuid.UUID, role: Role) -> None:
    if can_edit(row, user_id, role):
        return
    raise PresetError(403, "só quem criou o preset ou um administrador do workspace pode alterá-lo")


async def source_for_clone(db: AsyncSession, workspace_id: uuid.UUID, key: str, role: Role) -> dict[str, Any]:
    """Fields of the preset being cloned. Cloning a built-in (the only way to 'edit' one) is admin-only."""
    if key.startswith(CUSTOM_PREFIX):
        try:
            preset_id = uuid.UUID(key[len(CUSTOM_PREFIX) :])
        except ValueError as exc:
            raise PresetError(404, "preset não encontrado") from exc
        row = await get_custom(db, workspace_id, preset_id)
        return {
            "name": row.name,
            "description": row.description,
            "strategy_key": row.strategy_key,
            "interval": row.interval,
            "symbol": row.symbol,
            "leverage": row.leverage,
            "params": dict(row.params or {}),
            "options": dict(row.options or {}),
            "risk_level": row.risk_level,
            "tags": list(row.tags or []),
        }
    builtin = PRESETS_BY_KEY.get(key)
    if builtin is None:
        raise PresetError(404, "preset não encontrado")
    if not has_role(role, Role.ADMIN):
        raise PresetError(403, "só administradores do workspace podem clonar (personalizar) robôs prontos")
    d = builtin.as_dict()
    return {k: d[k] for k in CLONED_FIELDS}
