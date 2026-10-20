"""Grid bot service (placeholder).

Generates a price grid between lower/upper bounds; every order produced here
must be validated by RiskManager before reaching BinanceFuturesClient.
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class GridConfig:
    symbol: str
    lower_price: Decimal
    upper_price: Decimal
    levels: int
    quantity_per_level: Decimal
    leverage: int = 1


def build_grid(config: GridConfig) -> list[Decimal]:
    if config.levels < 2 or config.upper_price <= config.lower_price:
        raise ValueError("invalid grid configuration")
    step = (config.upper_price - config.lower_price) / (config.levels - 1)
    return [config.lower_price + step * i for i in range(config.levels)]


class GridBot:
    def __init__(self, config: GridConfig) -> None:
        self.config = config
        self.levels = build_grid(config)

    async def tick(self) -> None:
        """TODO: reconcile grid orders with exchange state and replace filled levels."""
        raise NotImplementedError
