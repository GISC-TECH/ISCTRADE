from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_api_v1_routes_registered() -> None:
    paths = set(app.openapi()["paths"])  # stable across FastAPI router internals
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/accounts/" in paths
    assert "/api/v1/accounts/{account_id}/dashboard" in paths
