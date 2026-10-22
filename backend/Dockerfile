FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Todas as dependências possuem wheels manylinux para Python 3.12 (asyncpg, bcrypt, cryptography, uvloop...).
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
# --timeout-keep-alive 130: keep idle upstream connections open longer than the clients that pool them (Caddy keeps
# idle upstream connections for 2 min, Node/Next.js for 5 s). With uvicorn's default of 5 s the server can close a
# socket at the same moment the proxy reuses it -> sporadic 502/500 "socket hang up" (ECONNRESET).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "130"]
