"""Test configuration. Ephemeral keys are generated at runtime — never hardcode secrets."""
import base64
import os
import secrets

os.environ.setdefault("MASTER_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("ENVIRONMENT", "test")
