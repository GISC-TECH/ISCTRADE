"""RFC 6238 TOTP (SHA-1, 6 digits, 30 s) and single-use recovery codes.

Implemented with the standard library (hmac/hashlib) so no extra crypto dependency is needed. The QR code for
enrolment is rendered server-side as SVG (``qrcode`` is pure Python, no Pillow) so the secret never leaves the
API except inside the enrolment response.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote, urlencode

TOTP_DIGITS = 6
TOTP_PERIOD = 30
TOTP_WINDOW = 1  # accept the previous/next 30 s step (clock drift)
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def generate_secret() -> str:
    """160-bit random secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // TOTP_PERIOD)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code if not ch.isspace() and ch != "-").upper()


def verify_totp(secret: str, code: str, last_used_step: int | None = None, now: float | None = None) -> int | None:
    """Return the matched time step, or None. Steps <= ``last_used_step`` are rejected (no replay)."""
    code = normalize_code(code)
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    step = current_step(now)
    for candidate in range(step - TOTP_WINDOW, step + TOTP_WINDOW + 1):
        if last_used_step is not None and candidate <= last_used_step:
            continue
        if hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret: str, account_name: str, issuer: str) -> str:
    label = quote(f"{issuer}:{account_name}", safe="@:")
    params = urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": TOTP_DIGITS,
                        "period": TOTP_PERIOD})  # fmt: skip
    return f"otpauth://totp/{label}?{params}"


def qr_svg_data_uri(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# --------------------------------------------------------------------------- #
# Recovery codes: 10 chars (~50 bits) each, shown once, stored as salted SHA-256
# --------------------------------------------------------------------------- #
def generate_recovery_codes(n: int = RECOVERY_CODE_COUNT) -> list[str]:
    codes = []
    for _ in range(n):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_recovery_code(code: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}:{normalize_code(code)}".encode()).hexdigest()
    return f"{salt}${digest}"


def recovery_code_matches(code: str, stored: str) -> bool:
    salt, _, _digest = stored.partition("$")
    return hmac.compare_digest(hash_recovery_code(code, salt), stored)


def looks_like_recovery_code(code: str) -> bool:
    c = normalize_code(code)
    return len(c) == 10 and all(ch in _RECOVERY_ALPHABET for ch in c)
