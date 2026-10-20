"""Strategy registry (placeholder). No trading strategy is implemented yet."""

from abc import ABC, abstractmethod
from typing import Any

AVAILABLE_STRATEGIES: dict[str, str] = {
    "grid": "Grid (placeholder)",
}


class Strategy(ABC):
    key: str = "base"

    @abstractmethod
    def on_candle(self, candle: dict[str, Any]) -> list[dict[str, Any]]:
        """Return a list of order intents (to be validated by RiskManager)."""
