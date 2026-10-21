"""Application settings loaded from environment variables (pydantic-settings)."""

import os
from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    # BTP_ENV_FILE overrides the dotenv path; an empty value disables it (the test suite does this so a
    # developer's local .env never leaks into tests).
    model_config = SettingsConfigDict(
        env_file=os.environ.get("BTP_ENV_FILE", ".env") or None, env_file_encoding="utf-8", extra="ignore"
    )

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

    ALLOW_REGISTRATION: bool = True
    LOGIN_RATE_LIMIT_PER_MINUTE: int = 10
    REGISTER_RATE_LIMIT_PER_MINUTE: int = 5

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
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    # Celery
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"
    CELERY_DISPATCH_ENABLED: bool = True  # API -> worker task dispatch (disabled in tests)

    # Rate limiting
    RATE_LIMIT_PER_MINUTE: int = 60

    # Execution safety. Real orders require BOTH this flag AND account.execution_mode == "live".
    LIVE_TRADING_ENABLED: bool = False
    PAPER_TAKER_FEE: float = 0.0004  # 0.04%
    PAPER_SLIPPAGE_BPS: float = 2.0
    KILL_SWITCH_CLOSE_POSITIONS: bool = True
    BOT_TICK_SECONDS: int = 30

    # Market scanner (public market data only; no API keys needed)
    SCANNER_ENABLED: bool = True
    SCANNER_USE_TESTNET: bool = False  # public mainnet data is read-only and much more complete
    SCANNER_INTERVAL: str = "1h"
    SCANNER_REFRESH_SECONDS: int = 300
    SCANNER_MAX_SYMBOLS: int = 40  # most liquid symbols analysed with klines
    SCANNER_MIN_QUOTE_VOLUME: float = 50_000_000.0  # 24h volume in USDT
    SCANNER_KLINES_CACHE_SECONDS: int = 120
    SCANNER_KEEP_SNAPSHOTS: int = 48
    # News & sentiment (a FILTER for entries, never a trade trigger)
    NEWS_ENABLED: bool = True
    NEWS_REFRESH_SECONDS: int = 300
    NEWS_RETENTION_DAYS: int = 14
    NEWS_HTTP_TIMEOUT_SECONDS: float = 10.0
    NEWS_RSS_FEEDS: Annotated[list[str], NoDecode] = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://www.theblock.co/rss.xml",
    ]
    NEWS_MAX_ITEMS_PER_SOURCE: int = 50
    NEWS_SENTIMENT_HALF_LIFE_HOURS: float = 6.0
    NEWS_SENTIMENT_WINDOW_HOURS: float = 48.0
    NEWS_ALERT_MIN_ABS_SENTIMENT: float = 0.5  # high-impact news alerts need |sentiment| >= this
    CRYPTOPANIC_API_TOKEN: str = ""  # optional (paid plans); empty = disabled
    CRYPTOPANIC_API_URL: str = "https://cryptopanic.com/api/growth/v2/posts/"
    CRYPTOPANIC_MIN_INTERVAL_SECONDS: int = 900  # respect the monthly quota
    X_BEARER_TOKEN: str = ""  # optional X API v2 recent search; empty = disabled
    X_API_URL: str = "https://api.x.com/2/tweets/search/recent"
    X_ACCOUNTS: Annotated[list[str], NoDecode] = []  # e.g. "whale_alert,WatcherGuru"
    X_QUERIES: Annotated[list[str], NoDecode] = []  # e.g. "bitcoin ETF -is:retweet lang:en"
    X_MAX_RESULTS: int = 20
    X_MIN_INTERVAL_SECONDS: int = 900
    # LLM sentiment (any OpenAI-compatible /chat/completions API). Empty key = lexicon fallback.
    LLM_API_BASE: str = "https://api.openai.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TIMEOUT_SECONDS: float = 20.0
    LLM_MAX_CALLS_PER_HOUR: int = 60
    LLM_MAX_ITEMS_PER_RUN: int = 20

    # Economic calendar (pause new entries around high-impact macro events)
    ECON_CALENDAR_ENABLED: bool = True
    ECON_CALENDAR_URL: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    ECON_CALENDAR_REFRESH_SECONDS: int = 3600
    ECON_CALENDAR_COUNTRIES: Annotated[list[str], NoDecode] = ["USD"]
    ECON_CALENDAR_MANUAL_EVENTS: str = ""  # JSON list: [{"title","time"(ISO),"country","impact"}]
    ECON_ALERT_LEAD_MINUTES: int = 60

    # Real-time panel (WebSocket fed by Redis pub/sub)
    REALTIME_ENABLED: bool = True
    REALTIME_HEARTBEAT_SECONDS: int = 15
    REALTIME_MAX_CONNECTIONS_PER_USER: int = 20  # one per open browser tab
    REALTIME_PRICE_THROTTLE_MS: int = 250  # <= 4 price/kline updates per second per symbol and connection
    REALTIME_KLINE_TTL_SECONDS: int = 120  # kline subscriptions requested by clients expire without renewal
    REALTIME_MARKET_FEED: bool = True  # streams worker publishes tickers/mark/klines
    PAPER_ORDER_CHECK_SECONDS: int = 10  # paper resting limit orders + SL/TP checks (manual trading)

    RECONCILE_SECONDS: int = 60
    EQUITY_SNAPSHOT_SECONDS: int = 300

    # Observability
    LOG_FORMAT: str = "json"  # json | text
    LOG_LEVEL: str = "INFO"
    METRICS_ENABLED: bool = True
    METRICS_TOKEN: str = ""  # optional bearer token required on /metrics (empty = internal network only)
    METRICS_PORT: int = 0  # >0: Celery worker / streams expose their own /metrics on this port
    STREAMS_HEARTBEAT_STALE_SECONDS: int = 180

    # Alerts (WhatsApp). Messages never contain credentials.
    ALERTS_ENABLED: bool = True  # master switch for generating alerts (history is kept even without a channel)
    ALERT_THROTTLE_MINUTES: int = 15
    ALERT_WS_DISCONNECT_SECONDS: int = 120
    ALERT_DAILY_SUMMARY_HOUR_UTC: int = 0  # summary of the previous UTC day, sent at HH:05 UTC
    WHATSAPP_ENABLED: bool = False
    WHATSAPP_PROVIDER: str = "wago"  # wago (WAGO hosted API) | wago_api | meta_cloud | evolution | generic_http
    WHATSAPP_API_URL: str = ""  # empty = provider default (wago: https://wago-api.hablame.co/v1)
    WHATSAPP_API_TOKEN: str = ""
    WHATSAPP_AUTH_HEADER: str = "Authorization"
    WHATSAPP_AUTH_PREFIX: str = "Bearer "
    WHATSAPP_SESSION: str = Field(default="", validation_alias=AliasChoices("WHATSAPP_SESSION", "WHATSAPP_INSTANCE"))
    WHATSAPP_DEVICE_ID: str = ""  # wago: optional WhatsApp number/device ID used to send
    WHATSAPP_PHONE_NUMBER_ID: str = ""  # meta_cloud
    WHATSAPP_HTTP_METHOD: str = "POST"
    WHATSAPP_BODY_TEMPLATE: str = '{"to": "{to}", "text": "{text}"}'
    WHATSAPP_EXTRA_HEADERS: str = ""  # optional JSON object with extra headers
    WHATSAPP_TO: Annotated[list[str], NoDecode] = []  # default recipients (E.164, comma-separated)
    WHATSAPP_TIMEOUT_SECONDS: float = 10.0

    # Risk defaults
    RISK_MAX_RISK_PER_TRADE_PCT: float = 1.0
    RISK_MAX_LEVERAGE: int = 5
    RISK_MAX_DAILY_LOSS_PCT: float = 3.0
    RISK_MAX_DRAWDOWN_PCT: float = 15.0

    @field_validator(
        "CORS_ORIGINS",
        "WHATSAPP_TO",
        "NEWS_RSS_FEEDS",
        "X_ACCOUNTS",
        "X_QUERIES",
        "ECON_CALENDAR_COUNTRIES",
        mode="before",
    )
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json

                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"

    def production_problems(self) -> list[str]:
        """Configuration errors that must block startup in production (never includes secret values)."""
        import base64
        import binascii

        problems: list[str] = []
        if "CHANGE_ME" in self.JWT_SECRET or len(self.JWT_SECRET) < 32:
            problems.append("JWT_SECRET ausente/fraco (mínimo 32 caracteres aleatórios)")
        try:
            key_ok = len(base64.b64decode(self.MASTER_ENCRYPTION_KEY, validate=True)) == 32
        except (binascii.Error, ValueError):
            key_ok = False
        if not key_ok:
            problems.append("MASTER_ENCRYPTION_KEY deve ser base64 de 32 bytes")
        if "CHANGE_ME" in self.DATABASE_URL:
            problems.append("DATABASE_URL contém CHANGE_ME")
        if not self.COOKIE_SECURE:
            problems.append("COOKIE_SECURE deve ser true em produção")
        if self.DEBUG:
            problems.append("DEBUG deve ser false em produção")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
