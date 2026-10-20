"""Domain enums shared by models, schemas and services."""

import enum


class AccountStatus(str, enum.Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    KILLED = "killed"  # kill switch triggered (drawdown / global) - needs manual reset
    ERROR = "error"


class ExecutionMode(str, enum.Enum):
    PAPER = "paper"  # simulated fills (default)
    LIVE = "live"  # real orders on Binance (testnet or mainnet) - must be explicitly enabled


class BotState(str, enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    KILLED = "killed"


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"  # created locally, not yet acknowledged
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class BacktestStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
