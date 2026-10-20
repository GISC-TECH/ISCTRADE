"""Grid bot: BotRunner specialised for the grid strategy.

`GridBot.tick()` runs a full cycle: fetch closed candles -> grid strategy decides buys/sells
-> every order validated by RiskManager -> executed (paper by default) -> runtime state saved.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.bot_runner import BotRunner, TickResult
from app.strategy.grid import GridStrategy, grid_levels


@dataclass
class GridConfig:
    symbol: str
    lower_price: Decimal
    upper_price: Decimal
    levels: int
    quantity_per_level: Decimal
    stop_loss_pct: Decimal = Decimal("3")
    leverage: int = 1

    def as_params(self) -> dict:
        return {
            "lower_price": float(self.lower_price),
            "upper_price": float(self.upper_price),
            "levels": self.levels,
            "quantity_per_level": float(self.quantity_per_level),
            "stop_loss_pct": float(self.stop_loss_pct),
        }


def build_grid(config: GridConfig) -> list[Decimal]:
    return grid_levels(config.lower_price, config.upper_price, config.levels)


class GridBot(BotRunner):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if self.bot.strategy_key != GridStrategy.key:
            raise ValueError("GridBot requires a bot configured with the 'grid' strategy")
        GridStrategy.validate_params(self.bot.params)

    async def tick(self) -> TickResult:
        return await super().tick()
