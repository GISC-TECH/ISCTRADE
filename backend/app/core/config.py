"""Application settings loaded from environment variables (pydantic-settings)."""

import os
from functools import lru_cache
from typing import Annotated, Literal

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
    # A rotated refresh token presented again within this window is a concurrent refresh, not theft.
    REFRESH_REUSE_GRACE_SECONDS: int = 10
    COOKIE_SECURE: bool = True
    COOKIE_DOMAIN: str | None = None

    # Self-service signup. closed = nobody (bootstrap the admin with `python -m app.cli create-admin`);
    # invite = only with a single-use invite (POST /admin/invites or `python -m app.cli create-invite`);
    # open = anyone. Unset: derived from the legacy ALLOW_REGISTRATION (true -> open, false -> closed).
    REGISTRATION_MODE: Literal["closed", "invite", "open"] | None = None
    ALLOW_REGISTRATION: bool = False
    # Production refuses to start with open signup unless this explicit override is set.
    ALLOW_OPEN_REGISTRATION_IN_PRODUCTION: bool = False
    INVITE_EXPIRE_HOURS: Annotated[int, Field(ge=1, le=24 * 90)] = 72
    LOGIN_RATE_LIMIT_PER_MINUTE: int = 10
    # Failed-login budget per e-mail (across all IPs) per 15 minutes; blocks distributed brute force.
    LOGIN_FAILURES_PER_EMAIL: int = 20
    # Proxies whose X-Forwarded-For is trusted (IPs/CIDRs). Default: loopback + private networks (Caddy in
    # Docker). Requests from any other peer use the socket address, so the header cannot be spoofed.
    TRUSTED_PROXIES: Annotated[list[str], NoDecode] = [
        "127.0.0.1/32",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]
    REGISTER_RATE_LIMIT_PER_MINUTE: int = 5
    # TOTP 2FA. Live trading actions (live account create/switch/start, manual live orders) require 2FA enrolled;
    # platform super-admins need it for /admin. Admins can additionally require 2FA for every user (Admin page).
    TOTP_ISSUER: str = "ISCTRADE"
    TOTP_REQUIRED_FOR_LIVE: bool = True
    TOTP_REQUIRED_FOR_ADMINS: bool = True
    TOTP_MAX_FAILURES: int = 5  # wrong codes per user per 15 min before 429
    TOTP_CHALLENGE_MINUTES: int = 5  # validity of the login step-up token

    # Encryption of exchange credentials (base64-encoded 32 bytes => AES-256)
    MASTER_ENCRYPTION_KEY: str = "CHANGE_ME"

    # Binance USDT-M Futures
    BINANCE_API_URL: str = "https://fapi.binance.com"
    BINANCE_TESTNET_API_URL: str = "https://testnet.binancefuture.com"
    BINANCE_WS_URL: str = "wss://fstream.binance.com"
    BINANCE_TESTNET_WS_URL: str = "wss://stream.binancefuture.com"
    BINANCE_USE_TESTNET: bool = True
    BINANCE_RECV_WINDOW: int = 5000
    # Request-weight guard (X-MBX-USED-WEIGHT-1M): above SOFT_PCT of the limit, non-order requests wait for the
    # next minute (at most MAX_WAIT seconds, otherwise they fail fast); after a 429/418 requests pause locally.
    BINANCE_WEIGHT_LIMIT_1M: int = 2400
    BINANCE_WEIGHT_SOFT_PCT: float = 90.0
    BINANCE_WEIGHT_MAX_WAIT_SECONDS: float = 15.0

    # Exchange adapters (app/exchanges). Experimental adapters (e.g. Bybit) are refused by the registry
    # unless explicitly enabled; they are skeletons for development on testnet only.
    EXPERIMENTAL_EXCHANGES_ENABLED: bool = False
    BYBIT_API_URL: str = "https://api.bybit.com"
    BYBIT_TESTNET_API_URL: str = "https://api-testnet.bybit.com"
    BYBIT_RECV_WINDOW: int = 5000
    # Bybit V5 WebSocket streams (experimental): off by default; without them Bybit accounts rely on REST polling.
    BYBIT_WS_ENABLED: bool = False
    BYBIT_WS_PUBLIC_URL: str = "wss://stream.bybit.com/v5/public/linear"
    BYBIT_TESTNET_WS_PUBLIC_URL: str = "wss://stream-testnet.bybit.com/v5/public/linear"
    BYBIT_WS_PRIVATE_URL: str = "wss://stream.bybit.com/v5/private"
    BYBIT_TESTNET_WS_PRIVATE_URL: str = "wss://stream-testnet.bybit.com/v5/private"

    # CORS (comma-separated list)
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    # Celery
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"
    CELERY_DISPATCH_ENABLED: bool = True  # API -> worker task dispatch (disabled in tests)
    # DB connections kept per Celery worker process (persistent event loop, app.tasks.runtime). 0 = NullPool.
    CELERY_DB_POOL_SIZE: int = 2

    # Rate limiting
    RATE_LIMIT_PER_MINUTE: int = 60

    # Execution safety. Real orders require BOTH this flag AND account.execution_mode == "live".
    LIVE_TRADING_ENABLED: bool = False
    PAPER_TAKER_FEE: float = 0.0004  # 0.04%
    PAPER_SLIPPAGE_BPS: float = 2.0
    # Paper realism (0 = off, the historical optimistic behaviour). PAPER_MAX_VOLUME_PCT caps each paper fill to that
    # % of the reference candle volume (bot entries: last closed candle; resting LIMITs: last closed 1m candle); the
    # rest of a MARKET order expires, a LIMIT keeps resting as PARTIALLY_FILLED. Exits (reduce-only) are never capped.
    PAPER_MAX_VOLUME_PCT: float = 0.0
    # Paper LIMIT orders only fill once the price trades this many bps THROUGH the limit (queue-position proxy).
    PAPER_LIMIT_THROUGH_BPS: float = 0.0
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
    # Hard caps for third-party content: total time per fetch and decoded body size (slow-drip / huge feeds).
    NEWS_FETCH_DEADLINE_SECONDS: float = 30.0
    NEWS_MAX_RESPONSE_BYTES: int = 5_000_000
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
    LLM_MAX_RESPONSE_BYTES: int = 1_000_000
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
    # Stop guarantee (reconciler, every RECONCILE_SECONDS and on user-stream events): every live position must
    # have a protective STOP at the exchange (algo orders included). A missing stop is re-created from the
    # stored stop; positions without a known stop (opened outside the platform) get an emergency stop
    # (PROTECTION_ADOPT_EXTERNAL=stop) or only a critical alert (=alert).
    PROTECTION_GUARD_ENABLED: bool = True
    PROTECTION_ADOPT_EXTERNAL: Literal["stop", "alert"] = "stop"
    PROTECTION_EMERGENCY_STOP_MODE: Literal["atr", "percent"] = "atr"
    PROTECTION_EMERGENCY_STOP_PCT: float = Field(3.0, gt=0, lt=100)  # percent mode and ATR fallback
    PROTECTION_EMERGENCY_ATR_MULTIPLIER: float = Field(2.0, gt=0)
    PROTECTION_EMERGENCY_ATR_INTERVAL: str = "1h"
    PROTECTION_EMERGENCY_ATR_PERIOD: int = Field(14, ge=2)
    # The stored stop was already crossed by the mark price (a stop there would trigger at once): close the
    # position with a reduce-only MARKET order (true) or protect it with an emergency stop instead (false).
    PROTECTION_CLOSE_IF_STOP_CROSSED: bool = True
    PROTECTION_GRACE_SECONDS: int = 30  # skip symbols with a local order this recent (stop placement in flight)
    PROTECTION_ORDER_HISTORY_DAYS: int = 7  # fallback: stop of the latest local order of the position's side
    EQUITY_SNAPSHOT_SECONDS: int = 300

    # Observability
    LOG_FORMAT: str = "json"  # json | text
    LOG_LEVEL: str = "INFO"
    METRICS_ENABLED: bool = True
    METRICS_TOKEN: str = ""  # optional bearer token required on /metrics (empty = internal network only)
    METRICS_PORT: int = 0  # >0: Celery worker / streams expose their own /metrics on this port
    STREAMS_HEARTBEAT_STALE_SECONDS: int = 180
    # Local heartbeat files read by the Docker healthchecks of streams/beat (no network dependency).
    STREAMS_HEARTBEAT_FILE: str = "/tmp/streams-heartbeat"
    BEAT_HEARTBEAT_FILE: str = "/tmp/beat-heartbeat"
    # Readiness (/health/ready): a market WS without messages for longer than this is "stale".
    STREAMS_MESSAGE_STALE_SECONDS: int = 90
    BEAT_HEARTBEAT_STALE_SECONDS: int = 120
    WORKER_HEARTBEAT_SECONDS: int = 30  # beat schedules ops.heartbeat on each queue at this period
    WORKER_HEARTBEAT_STALE_SECONDS: int = 180
    BOT_TICK_STALE_SECONDS: int = 300  # running bots but no completed tick for this long => degraded
    # true: /health/ready answers 503 also when degraded (beat/worker/streams stale), not only when DB/Redis fail.
    HEALTH_READY_STRICT: bool = False
    # Dead man's switch (healthchecks.io, Uptime Kuma push, Better Stack heartbeat...): GET every
    # DEADMAN_PING_SECONDS while the stack is healthy; "<url>/fail" when /health/ready is not "ready".
    DEADMAN_PING_URL: str = ""
    DEADMAN_PING_SECONDS: int = 60
    # Bearer token required by POST /api/v1/monitoring/alertmanager (Alertmanager -> WhatsApp). Empty = disabled.
    ALERTMANAGER_WEBHOOK_TOKEN: str = ""

    # Retention (daily cleanup task ops.cleanup at RETENTION_HOUR_UTC:30). 0 = keep forever.
    RETENTION_HOUR_UTC: int = 3
    REFRESH_TOKEN_RETENTION_DAYS: int = 7  # after expiry (expired tokens can never be used again)
    ALERTS_RETENTION_DAYS: int = 90
    AUDIT_RETENTION_DAYS: int = 0  # audit trail: kept forever by default (compliance)
    EQUITY_SNAPSHOT_RETENTION_DAYS: int = 365
    BACKTEST_RETENTION_DAYS: int = 0

    # Alerts (WhatsApp). Messages never contain credentials.
    ALERTS_ENABLED: bool = True  # master switch for generating alerts (history is kept even without a channel)
    ALERT_THROTTLE_MINUTES: int = 15
    # Open-position alerts (G-06), live (reconciler) and paper (paper.process_orders). PnL levels: ROE % on the
    # position margin and/or unrealized USDT (comma separated; negative = loss). Proximity in % of the mark price
    # (0 disables). Each account/symbol/kind/level alerts at most once per POSITION_ALERT_THROTTLE_MINUTES.
    POSITION_ALERTS_ENABLED: bool = True
    POSITION_ALERT_PNL_ROE_PCT: Annotated[list[float], NoDecode] = [-50.0, -25.0, 50.0, 100.0]
    POSITION_ALERT_PNL_USDT: Annotated[list[float], NoDecode] = []
    POSITION_ALERT_STOP_PROXIMITY_PCT: float = Field(0.5, ge=0, le=50)
    POSITION_ALERT_LIQ_PROXIMITY_PCT: float = Field(5.0, ge=0, le=100)
    POSITION_ALERT_THROTTLE_MINUTES: int = Field(60, ge=0)
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

    # Telegram alerts (Bot API). Recipients: chat ids (numbers, -100... for groups/channels) or @channel.
    TELEGRAM_ENABLED: bool = False
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_IDS: Annotated[list[str], NoDecode] = []  # default recipients
    TELEGRAM_API_URL: str = "https://api.telegram.org"
    TELEGRAM_TIMEOUT_SECONDS: float = 10.0
    # E-mail alerts (SMTP). SMTP_SECURITY: starttls (587) | ssl (465) | none (relay on a trusted network only).
    EMAIL_ENABLED: bool = False
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_SECURITY: str = "starttls"
    SMTP_TIMEOUT_SECONDS: float = 15.0
    EMAIL_FROM: str = ""
    EMAIL_TO: Annotated[list[str], NoDecode] = []  # default recipients

    # Walk-forward budget: total candles simulated per request (grid x folds x window). ~70 µs per candle step.
    WALK_FORWARD_MAX_CANDLE_STEPS: int = 1_000_000

    # Tamper-evident audit trail (app/services/audit_chain.py). Empty key = derived from MASTER_ENCRYPTION_KEY.
    AUDIT_CHAIN_KEY: str = ""
    AUDIT_SEAL_SECONDS: int = 30

    # Risk defaults
    RISK_MAX_RISK_PER_TRADE_PCT: float = 1.0
    RISK_MAX_LEVERAGE: int = 5
    RISK_MAX_DAILY_LOSS_PCT: float = 3.0
    RISK_MAX_DRAWDOWN_PCT: float = 15.0
    # Hard leverage ceiling for every account/bot/manual order (account max_leverage is clamped to it). The
    # schema refuses values above 50 no matter what this is set to.
    RISK_MAX_LEVERAGE_CAP: int = Field(20, ge=1, le=50)
    # Liquidation guard: the stop must sit at most this fraction of the way from entry to the estimated
    # liquidation price (0.8 = the stop triggers with >= 20% of the distance to liquidation still left).
    RISK_STOP_MAX_LIQ_DISTANCE_RATIO: float = Field(0.8, gt=0, le=1)
    # Maintenance margin rate used when the exchange brackets are unavailable (paper, adapters without brackets).
    RISK_DEFAULT_MAINT_MARGIN_RATE: float = Field(0.01, ge=0, lt=1)
    LEVERAGE_BRACKETS_CACHE_SECONDS: int = 3600
    # Aggregate limits (bots AND manual orders, enforced by the RiskManager). 0 = no limit. Workspace values
    # are defaults/ceilings: a workspace admin may tighten them (or restore up to these), only the platform
    # super-admin may go above. Global limits sum every workspace of the instance (super-admin only).
    RISK_AGGREGATE_INCLUDE_PAPER: bool = False  # false: aggregate limits only count/guard LIVE accounts
    RISK_WS_MAX_TOTAL_NOTIONAL_USDT: float = Field(10000.0, ge=0)
    RISK_WS_MAX_DAILY_LOSS_USDT: float = Field(300.0, ge=0)
    RISK_WS_MAX_OPEN_POSITIONS: int = Field(5, ge=0)
    RISK_GLOBAL_MAX_TOTAL_NOTIONAL_USDT: float = Field(0.0, ge=0)
    RISK_GLOBAL_MAX_DAILY_LOSS_USDT: float = Field(0.0, ge=0)
    RISK_GLOBAL_MAX_OPEN_POSITIONS: int = Field(0, ge=0)
    # Hard cap of capital per LIVE account: sizing and margin never use more equity than this (0 = no cap).
    RISK_LIVE_ACCOUNT_MAX_CAPITAL_USDT: float = Field(1000.0, ge=0)
    # Deposits/withdrawals (income TRANSFER*) are folded into peak/day-start equity (live accounts), read at
    # most every TRANSFER_SYNC_SECONDS and always before a new high / drawdown / daily-loss limit is acted on.
    TRANSFER_SYNC_SECONDS: int = 300
    TRANSFER_SETTLE_SECONDS: int = 5  # history read up to now - this (late-indexed rows land in the next read)
    TRANSFER_ASSETS: Annotated[list[str], NoDecode] = ["USDT"]

    @field_validator(
        "CORS_ORIGINS",
        "TRUSTED_PROXIES",
        "WHATSAPP_TO",
        "TELEGRAM_CHAT_IDS",
        "EMAIL_TO",
        "NEWS_RSS_FEEDS",
        "X_ACCOUNTS",
        "X_QUERIES",
        "ECON_CALENDAR_COUNTRIES",
        "TRANSFER_ASSETS",
        "POSITION_ALERT_PNL_ROE_PCT",
        "POSITION_ALERT_PNL_USDT",
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
    def registration_mode(self) -> str:
        if self.REGISTRATION_MODE:
            return self.REGISTRATION_MODE
        return "open" if self.ALLOW_REGISTRATION else "closed"

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
        if self.registration_mode == "open" and not self.ALLOW_OPEN_REGISTRATION_IN_PRODUCTION:
            problems.append(
                "cadastro aberto em produção (ALLOW_REGISTRATION=true / REGISTRATION_MODE=open): use "
                "REGISTRATION_MODE=invite ou closed e crie o admin com `python -m app.cli create-admin` "
                "(override explícito: ALLOW_OPEN_REGISTRATION_IN_PRODUCTION=true)"
            )
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
