"""bcrypt runs in a worker thread so logins do not freeze the event loop (WS feeds, other requests)."""

import asyncio
import time

import bcrypt

from app.core import security


async def test_async_hash_and_verify_roundtrip() -> None:
    h = await security.hash_password_async("s3nha-forte-123")
    assert await security.verify_password_async("s3nha-forte-123", h)
    assert not await security.verify_password_async("errada", h)
    assert not await security.verify_password_async("x", "not-a-bcrypt-hash")


async def test_verify_does_not_block_event_loop(monkeypatch) -> None:
    def slow_check(password: str, hashed: str) -> bool:
        time.sleep(0.3)  # stands in for bcrypt's CPU time (releases the GIL like bcrypt does)
        return True

    monkeypatch.setattr(security, "verify_password", slow_check)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    assert await security.verify_password_async("a", "b")
    t.cancel()
    assert ticks >= 10  # the loop kept running while the hash was checked


def test_sync_verify_still_works() -> None:
    # The sync API (used inside the thread and by CLI scripts) keeps working with real hashes.
    h = bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=4)).decode()
    assert security.verify_password("x", h)
