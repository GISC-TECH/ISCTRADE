"""Test configuration.

- Ephemeral secrets are generated at runtime (never hardcoded).
- A file-based async SQLite database (aiosqlite) replaces PostgreSQL.
- Binance is always mocked; no network calls are made.
"""

import base64
import os
import secrets
import tempfile

_TMP = tempfile.mkdtemp(prefix="btp-tests-")
# Never read the developer's local .env (backend/.env): tests must only see the variables set here.
os.environ["BTP_ENV_FILE"] = ""
os.environ["MASTER_ENCRYPTION_KEY"] = base64.b64encode(os.urandom(32)).decode()
os.environ["JWT_SECRET"] = secrets.token_urlsafe(48)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/test.db"
os.environ["ENVIRONMENT"] = "test"
os.environ["COOKIE_SECURE"] = "false"
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"  # unreachable on purpose -> in-memory fallbacks
os.environ["LIVE_TRADING_ENABLED"] = "false"
os.environ["CELERY_DISPATCH_ENABLED"] = "false"
# Alerts: never inherit a real WhatsApp configuration from the developer's shell.
for _var in [k for k in os.environ if k.startswith("WHATSAPP_")]:
    del os.environ[_var]
os.environ["WHATSAPP_ENABLED"] = "false"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core.database import AsyncSessionLocal, engine  # noqa: E402
from app.models import Base  # noqa: E402


@pytest_asyncio.fixture
async def db_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db(db_schema):
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_schema):
    from app.core import rate_limiting
    from app.main import app

    rate_limiting.reset_memory_limiter()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def anyio_backend():
    return "asyncio"
