"""Celery beat scheduler with a heartbeat.

``celery beat -S app.tasks.beat:HeartbeatScheduler``: on every scheduler tick (at most
``beat_max_loop_interval`` apart) it touches BEAT_HEARTBEAT_FILE (Docker healthcheck) and the Redis key
``heartbeat:beat`` (/health/ready, btp_beat_heartbeat_age_seconds). A beat that hangs or dies stops both.
"""

from __future__ import annotations

import time
from typing import Any

from celery.beat import PersistentScheduler

from app.core.config import settings
from app.services.heartbeats import BEAT_KEY, touch_file, touch_sync

HEARTBEAT_EVERY = 10.0


class HeartbeatScheduler(PersistentScheduler):
    _last_heartbeat = 0.0

    def heartbeat(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if now - self._last_heartbeat < HEARTBEAT_EVERY:
            return False
        self._last_heartbeat = now
        touch_file(settings.BEAT_HEARTBEAT_FILE)
        touch_sync(BEAT_KEY, settings.BEAT_HEARTBEAT_STALE_SECONDS * 4)
        return True

    def tick(self, *args: Any, **kwargs: Any) -> float:
        self.heartbeat()
        return super().tick(*args, **kwargs)
