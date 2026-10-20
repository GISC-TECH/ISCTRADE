"""Reconciler (placeholder): compares local order/position state with Binance and fixes drift."""


class Reconciler:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id

    async def run(self) -> dict:
        """TODO: fetch open orders/positions, diff against DB, emit audit events for mismatches."""
        return {"account_id": self.account_id, "drift": []}
