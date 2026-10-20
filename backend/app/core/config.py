"""Application settings loaded from environment variables (pydantic-settings)."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # General
    PROJECT_NAME: str = "Binance Trading Platform"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # Database / cache
    DATABASE_URL: str = "postgresql+asyncpg://trading:CHANGE_ME@postgres:5432/trading"
    DB_USE_NULLPOOL: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    DB_ECHO: bool = False
    REDIS_URL: str = "redis://redis:6379/0"

    # Auth / JWT
    JWT_SECRET: str = "CHANGE_ME"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    COOKIE_SECURE: bool = True
    COOKIE_DOMAIN: str | None = None

    # Encryption of exchange credentials (base64-encoded 32 bytes => AES-256)
    MASTER_ENCRYPTION_KEY: str = "CHANGE_ME"

    # Binance USDT-M Futures
    BINANCE_API_URL: str = "https://fapi.binance.com"
    BINANCE_TESTNET_API_URL: str = "https://testnet.binancefuture.com"
    BINANCE_WS_URL: str = "wss://fstream.binance.com"
    BINANCE_TESTNET_WS_URL: str = "wss://stream.binancefuture.com"
    BINANCE_USE_TESTNET: bool = True
    BINANCE_RECV_WINDOW: int = 5000

    # CORS (comma-separated list)
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # Celery
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    # Rate limiting
    RATE_LIMIT_PER_MINUTE: int = 60

    # Execution safety. Real orders require BOTH this flag AND account.execution_mode == "live".
    LIVE_TRADING_ENABLED: bool = False
    PAPER_TAKER_FEE: float = 0.0004  # 0.04%
    PAPER_SLIPPAGE_BPS: float = 2.0
    KILL_SWITCH_CLOSE_POSITIONS: bool = True
    BOT_TICK_SECONDS: int = 30
    RECONCILE_SECONDS: int = 60
    EQUITY_SNAPSHOT_SECONDS: int = 300

    # Risk defaults
    RISK_MAX_RISK_PER_TRADE_PCT: float = 1.0
    RISK_MAX_LEVERAGE: int = 5
    RISK_MAX_DAILY_LOSS_PCT: float = 3.0
    RISK_MAX_DRAWDOWN_PCT: float = 15.0

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip().startswith("["):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
