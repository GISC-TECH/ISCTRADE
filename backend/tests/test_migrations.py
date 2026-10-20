"""Checks that the Alembic migrations match the ORM metadata.

Runs against PostgreSQL when TEST_POSTGRES_URL is set (CI), otherwise skipped.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PG_URL = os.getenv("TEST_POSTGRES_URL")
BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not PG_URL, reason="TEST_POSTGRES_URL not set")
def test_upgrade_downgrade_and_no_drift() -> None:
    env = {**os.environ, "DATABASE_URL": PG_URL}
    for args in (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"], ["check"]):
        res = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True, text=True
        )
        assert res.returncode == 0, res.stdout + res.stderr
