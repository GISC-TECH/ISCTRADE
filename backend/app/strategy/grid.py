"""Long-only grid strategy.

Buys one `quantity_per_level` each time price crosses DOWN through a grid level and sells it when
price reaches the next level above. A protective stop sits `stop_loss_pct` below the lowest level;
if price closes below it the whole grid is closed and the strategy halts until reset.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.models.enums import OrderSide
from app.strategy.base import OrderIntent, ParamSpec, Strategy, StrategyContext, StrategyParamError, to_decimal


def grid_levels(lower: Decimal, upper: Decimal, levels: int) -> list[Decimal]:
    if levels < 2 or upper <= lower:
        raise StrategyParamError("invalid grid: need levels >= 2 and upper_price > lower_price")
    step = (upper - lower) / (levels - 1)
    return [lower + step * i for i in range(levels)]


class GridStrategy(Strategy):
    key = "grid"
    name = "Grid (long)"
    description = "Compra em níveis abaixo e vende no nível seguinte acima, com stop abaixo da grade."
    params_spec = (
        ParamSpec("lower_price", "number", None, "Preço inferior da grade", required=True, min=0),
        ParamSpec("upper_price", "number", None, "Preço superior da grade", required=True, min=0),
        ParamSpec("levels", "integer", 10, "Número de níveis", min=2, max=200),
        ParamSpec("quantity_per_level", "number", None, "Quantidade por nível", required=True, min=0),
        ParamSpec("stop_loss_pct", "number", 3.0, "Stop abaixo do nível inferior (%)", min=0.1, max=50),
    )

    @classmethod
    def check_params(cls, params: dict[str, Any]) -> None:
        if params["upper_price"] <= params["lower_price"]:
            raise StrategyParamError("upper_price must be greater than lower_price")
        if params["quantity_per_level"] <= 0:
            raise StrategyParamError("quantity_per_level must be > 0")

    @property
    def levels(self) -> list[Decimal]:
        return grid_levels(
            to_decimal(self.params["lower_price"]), to_decimal(self.params["upper_price"]), self.params["levels"]
        )

    @property
    def stop_price(self) -> Decimal:
        return to_decimal(self.params["lower_price"]) * (1 - to_decimal(self.params["stop_loss_pct"]) / 100)

    @property
    def qty(self) -> Decimal:
        return to_decimal(self.params["quantity_per_level"])

    def on_candle(self, ctx: StrategyContext) -> list[OrderIntent]:
        st = ctx.state
        held: list[int] = st.setdefault("held", [])
        if st.get("halted"):
            return []
        # Position closed outside the grid (stop loss hit / manual close): forget held levels.
        if held and ctx.position_qty <= 0:
            held.clear()

        price = ctx.price
        prev = st.get("last_price")
        st["last_price"] = str(price)
        levels = self.levels

        if held and price <= self.stop_price:
            qty = min(self.qty * len(held), ctx.position_qty)
            return [OrderIntent(OrderSide.SELL, qty, reduce_only=True, tag="grid_stop", meta={"action": "stop"})]

        intents: list[OrderIntent] = []
        # Sell levels whose target (next level up) was reached.
        for i in sorted(held):
            if price >= levels[i + 1]:
                intents.append(
                    OrderIntent(OrderSide.SELL, self.qty, reduce_only=True, tag=f"grid_sell_{i}", meta={"level": i})
                )
        if prev is None:
            return intents
        prev_price = Decimal(prev)
        # Buy levels crossed downward (the top level is only a sell target).
        for i in range(len(levels) - 1):
            if i in held:
                continue
            if price <= levels[i] < prev_price:
                intents.append(
                    OrderIntent(
                        OrderSide.BUY,
                        self.qty,
                        stop_loss=self.stop_price,
                        tag=f"grid_buy_{i}",
                        meta={"level": i},
                    )
                )
        return intents

    def on_result(self, intent: OrderIntent, filled: bool, ctx: StrategyContext) -> None:
        if not filled:
            return
        held: list[int] = ctx.state.setdefault("held", [])
        if intent.meta.get("action") == "stop":
            held.clear()
            ctx.state["halted"] = True
            return
        level = intent.meta.get("level")
        if level is None:
            return
        if intent.side == OrderSide.BUY and level not in held:
            held.append(level)
        elif intent.side == OrderSide.SELL and level in held:
            held.remove(level)

    def on_stop_out(self, ctx: StrategyContext) -> None:
        ctx.state["held"] = []
        ctx.state["halted"] = True
