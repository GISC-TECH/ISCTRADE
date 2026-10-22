"""Docker healthchecks without extra tools (python:3.12-slim has no curl).

    python -m app.healthcheck http [URL]          # default http://127.0.0.1:8000/health
    python -m app.healthcheck file PATH MAX_AGE   # file touched less than MAX_AGE seconds ago (beat, streams)

Exit 0 = healthy, 1 = unhealthy (reason on stderr).
"""

from __future__ import annotations

import sys
import urllib.request

from app.services.heartbeats import file_age


def check_http(url: str = "http://127.0.0.1:8000/health", timeout: float = 4.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed local URL
            return resp.status == 200, f"HTTP {resp.status}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def check_file(path: str, max_age: float) -> tuple[bool, str]:
    age = file_age(path)
    if age is None:
        return False, f"{path} ausente"
    return age <= max_age, f"{path} atualizado há {age:.0f}s (máx. {max_age:.0f}s)"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    mode, args = argv[0], argv[1:]
    if mode == "http":
        ok, msg = check_http(*args[:1])
    elif mode == "file" and len(args) == 2:
        ok, msg = check_file(args[0], float(args[1]))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    print(msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
