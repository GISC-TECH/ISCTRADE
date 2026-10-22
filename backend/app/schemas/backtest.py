import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import BacktestStatus
from app.schemas.bot_options import EventPauseOptions, NewsFilterOptions

SYMBOL_PATTERN = r"^[A-Z0-9]{2,20}$"


class BacktestConfigIn(BaseModel):
    initial_balance: float = Field(10000, gt=0)
    fee_rate: float = Field(0.0004, ge=0, le=0.01)
    slippage_bps: float = Field(2, ge=0, le=500)
    leverage: int = Field(1, ge=1, le=125)
    max_risk_per_trade_pct: float = Field(1, gt=0, le=100)
    max_leverage: int = Field(5, ge=1, le=125)
    max_daily_loss_pct: float = Field(3, gt=0, le=100)
    max_drawdown_pct: float = Field(15, gt=0, le=100)
    stop_on_kill_switch: bool = True
    # 0 = off; else each entry fills at most this % of the signal candle volume (rest expires)
    max_volume_pct: float = Field(0, ge=0, le=100)


class BacktestFiltersIn(BaseModel):
    """Optional news filter / event pause replayed from the news and events stored in the database.

    Only news collected since the ingestion started is available (no historical news archive), so
    older periods run effectively unfiltered; leave both off for a pure strategy backtest.
    """

    news_filter: NewsFilterOptions = Field(default_factory=NewsFilterOptions)
    event_pause: EventPauseOptions = Field(default_factory=EventPauseOptions)


class FundingOptionsIn(BaseModel):
    """Funding costs (perpetuals). ``auto``: exchange history (fallback: data.binance.vision archive) when the
    candles come from the exchange/archive; with ``candles_csv`` only ``csv``/``constant`` apply."""

    mode: Literal["auto", "exchange", "archive", "csv", "constant", "off"] = "auto"
    constant_rate: float = Field(0.0001, ge=-0.01, le=0.01, description="taxa por 8 h no modo constant")
    csv: str | None = Field(None, max_length=2_000_000, description="fundingTime,fundingRate")


class BacktestRequest(BaseModel):
    strategy_key: str
    params: dict[str, Any] = Field(default_factory=dict)
    symbol: str = Field("BTCUSDT", pattern=SYMBOL_PATTERN)
    interval: str = "1h"
    start: datetime | None = None
    end: datetime | None = None
    candles_csv: str | None = Field(None, max_length=10_000_000)
    use_testnet_data: bool = False
    # exchange = REST klines of the configured exchange; archive = data.binance.vision (Binance USDT-M mainnet)
    data_source: Literal["exchange", "archive"] = "exchange"
    funding: FundingOptionsIn = Field(default_factory=FundingOptionsIn)
    config: BacktestConfigIn = Field(default_factory=BacktestConfigIn)
    filters: BacktestFiltersIn = Field(default_factory=BacktestFiltersIn)

    @model_validator(mode="after")
    def _source(self) -> "BacktestRequest":
        if not self.candles_csv and not (self.start and self.end):
            raise ValueError("informe start/end (dados da Binance) ou candles_csv")
        return self


class WalkForwardIn(BaseModel):
    folds: int = Field(4, ge=1, le=12)
    train_ratio: float = Field(0.7, ge=0.1, le=0.95)
    anchored: bool = False
    objective: Literal["sharpe", "return_pct", "profit_factor", "calmar"] = "sharpe"
    min_trades: int = Field(1, ge=0, le=1000)


class WalkForwardRequest(BacktestRequest):
    """Walk-forward: ``param_grid`` (name -> values; até 200 combinações) otimizado em cada janela in-sample e
    validado fora da amostra. Sem grid = validação dos ``params`` informados."""

    param_grid: dict[str, list[Any]] = Field(default_factory=dict)
    walk_forward: WalkForwardIn = Field(default_factory=WalkForwardIn)


class BacktestSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    strategy_key: str
    symbol: str
    interval: str
    status: BacktestStatus
    params: dict[str, Any]
    metrics: dict[str, Any]
    created_at: datetime
    error: str | None = None


class BacktestDetail(BacktestSummary):
    config: dict[str, Any]
    equity_curve: list[Any]
    trades: list[Any]
    start_time: datetime | None = None
    end_time: datetime | None = None
