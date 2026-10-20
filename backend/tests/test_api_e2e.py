"""End-to-end API tests over an async SQLite database (register -> login -> workspaces -> accounts -> bots)."""

from decimal import Decimal as D

from sqlalchemy import select

from app.models import Account, ExecutionMode, User
from app.models.enums import OrderSide
from app.services.execution import OrderService, PaperExecutor
from app.services.risk_manager import AccountRiskState
from app.strategy.base import OrderIntent

PW = "s3nha-forte-123"
GRID = {"lower_price": 90, "upper_price": 110, "levels": 5, "quantity_per_level": 0.5, "stop_loss_pct": 5}


async def register_login(client, email: str) -> dict:
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": PW, "full_name": "Fulano"})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def test_register_login_me_refresh_logout(client) -> None:
    data = await register_login(client, "owner@example.com")
    assert data["user"]["role"] == "admin"  # first user = super-admin
    # cookies set by login (httpOnly) are enough for authenticated calls
    assert "access_token" in client.cookies
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 200 and r.json()["email"] == "owner@example.com"
    assert r.json()["active_workspace_id"]

    r = await client.post("/api/v1/auth/refresh")
    assert r.status_code == 200 and r.json()["access_token"]

    r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 204
    client.cookies.clear()
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


async def test_second_user_is_trader_and_duplicates_rejected(client) -> None:
    await register_login(client, "a@example.com")
    b = await register_login(client, "b@example.com")
    assert b["user"]["role"] == "trader"
    r = await client.post("/api/v1/auth/register", json={"email": "B@example.com", "password": PW})
    assert r.status_code == 409


async def test_bad_credentials_and_rate_limit(client) -> None:
    await client.post("/api/v1/auth/register", json={"email": "x@example.com", "password": PW})
    codes = [
        (await client.post("/api/v1/auth/login", json={"email": "x@example.com", "password": "wrong-pass"})).status_code
        for _ in range(11)
    ]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


async def test_password_change_revokes_old_tokens(client) -> None:
    login = await register_login(client, "pw@example.com")
    old = bearer(login)
    client.cookies.clear()
    r = await client.post(
        "/api/v1/auth/me/password", json={"current_password": PW, "new_password": "nova-senha-456"}, headers=old
    )
    assert r.status_code == 200
    client.cookies.clear()
    assert (await client.get("/api/v1/auth/me", headers=old)).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=bearer(r.json()))).status_code == 200


async def test_workspaces_members_and_tenant_isolation(client, db) -> None:
    owner = bearer(await register_login(client, "owner@example.com"))
    other = bearer(await register_login(client, "other@example.com"))
    client.cookies.clear()

    ws_list = (await client.get("/api/v1/workspaces/", headers=owner)).json()
    assert len(ws_list) == 1 and ws_list[0]["is_active"] and ws_list[0]["role"] == "admin"
    ws_id = ws_list[0]["id"]

    r = await client.post(
        "/api/v1/accounts/",
        json={"label": "Teste", "api_key": "placeholder-key-1234", "api_secret": "placeholder-secret-5678"},
        headers=owner,
    )
    assert r.status_code == 201, r.text
    acc = r.json()
    assert acc["api_key_last4"] == "1234" and "api_key" not in acc and acc["execution_mode"] == "paper"
    stored = await db.scalar(select(Account))
    assert "placeholder" not in stored.encrypted_api_key

    # other user cannot see or touch the account
    assert (await client.get("/api/v1/accounts/", headers=other)).json() == []
    assert (await client.get(f"/api/v1/accounts/{acc['id']}", headers=other)).status_code == 404
    r = await client.get("/api/v1/accounts/", headers={**other, "X-Workspace-ID": ws_id})
    assert r.status_code == 404

    # invite as viewer -> read-only access
    r = await client.post(f"/api/v1/workspaces/{ws_id}/members", json={"email": "other@example.com"}, headers=owner)
    assert r.status_code == 201 and r.json()["role"] == "viewer"
    h_other_ws = {**other, "X-Workspace-ID": ws_id}
    assert len((await client.get("/api/v1/accounts/", headers=h_other_ws)).json()) == 1
    assert (await client.post(f"/api/v1/accounts/{acc['id']}/start", headers=h_other_ws)).status_code == 403

    # promote to trader
    other_id = (await client.get("/api/v1/auth/me", headers=other)).json()["id"]
    r = await client.patch(f"/api/v1/workspaces/{ws_id}/members/{other_id}", json={"role": "trader"}, headers=owner)
    assert r.json()["role"] == "trader"
    assert (await client.post(f"/api/v1/accounts/{acc['id']}/start", headers=h_other_ws)).status_code == 200
    # traders cannot switch to live mode (admin only)
    await client.post(f"/api/v1/accounts/{acc['id']}/stop", headers=h_other_ws)
    r = await client.patch(f"/api/v1/accounts/{acc['id']}", json={"execution_mode": "live"}, headers=h_other_ws)
    assert r.status_code == 403

    members = (await client.get(f"/api/v1/workspaces/{ws_id}/members", headers=owner)).json()
    assert [m["is_owner"] for m in members] == [True, False]

    # activate shared workspace for the other user (no header needed afterwards)
    r = await client.post(f"/api/v1/workspaces/{ws_id}/activate", headers=other)
    assert r.json()["is_active"]
    assert len((await client.get("/api/v1/accounts/", headers=other)).json()) == 1

    # leave
    assert (await client.delete(f"/api/v1/workspaces/{ws_id}/members/{other_id}", headers=other)).status_code == 204
    assert (await client.get("/api/v1/accounts/", headers=other)).json() == []


async def test_workspace_crud(client) -> None:
    h = bearer(await register_login(client, "crud@example.com"))
    r = await client.post("/api/v1/workspaces/", json={"name": "Fundo B"}, headers=h)
    assert r.status_code == 201
    ws_id = r.json()["id"]
    r = await client.patch(f"/api/v1/workspaces/{ws_id}", json={"name": "Fundo C"}, headers=h)
    assert r.json()["name"] == "Fundo C"
    assert (await client.delete(f"/api/v1/workspaces/{ws_id}", headers=h)).status_code == 204
    personal = (await client.get("/api/v1/workspaces/", headers=h)).json()[0]["id"]
    assert (await client.delete(f"/api/v1/workspaces/{personal}", headers=h)).status_code == 409


async def test_bots_lifecycle_and_history(client, db) -> None:
    h = bearer(await register_login(client, "bots@example.com"))
    acc = (
        await client.post(
            "/api/v1/accounts/",
            json={"label": "P", "api_key": "placeholder-key-0001", "api_secret": "placeholder-secret-0001",
                  "max_leverage": 3},
            headers=h,
        )
    ).json()  # fmt: skip
    body = {"account_id": acc["id"], "name": "Grid BTC", "strategy_key": "grid", "symbol": "BTCUSDT", "params": GRID}
    assert (await client.post("/api/v1/bots/", json={**body, "leverage": 10}, headers=h)).status_code == 422
    assert (await client.post("/api/v1/bots/", json={**body, "params": {"levels": 3}}, headers=h)).status_code == 422
    r = await client.post("/api/v1/bots/", json=body, headers=h)
    assert r.status_code == 201, r.text
    bot = r.json()
    assert bot["state"] == "idle"

    r = await client.post(f"/api/v1/bots/{bot['id']}/start", headers=h)
    assert r.json()["state"] == "starting"
    assert (await client.get(f"/api/v1/accounts/{acc['id']}", headers=h)).json()["status"] == "running"
    assert (await client.patch(f"/api/v1/bots/{bot['id']}", json={"params": GRID}, headers=h)).status_code == 409
    assert (await client.post(f"/api/v1/bots/{bot['id']}/pause", headers=h)).status_code == 409  # starting
    assert (await client.post(f"/api/v1/bots/{bot['id']}/stop", headers=h)).json()["state"] == "stopped"
    assert (await client.patch(f"/api/v1/bots/{bot['id']}", json={"name": "Grid 2"}, headers=h)).json()[
        "name"
    ] == "Grid 2"
    assert len((await client.get("/api/v1/bots/", headers=h)).json()) == 1

    # generate paper fills directly and read them through the history API
    account = await db.get(Account, __import__("uuid").UUID(acc["id"]))
    rs = AccountRiskState(D(10000), D(10000), D(10000))
    svc = OrderService(PaperExecutor())
    for _ in range(3):
        await svc.submit(db, account, OrderIntent(OrderSide.BUY, D("0.1"), stop_loss=D(95)), symbol="BTCUSDT",
                         ref_price=D(100), leverage=1, risk_state=rs)  # fmt: skip
    await svc.submit(db, account, OrderIntent(OrderSide.BUY, D(1)), symbol="BTCUSDT", ref_price=D(100), leverage=1,
                     risk_state=rs)  # fmt: skip
    await db.commit()

    r = await client.get("/api/v1/history/orders", params={"limit": 2}, headers=h)
    page = r.json()
    assert page["total"] == 4 and len(page["items"]) == 2
    r = await client.get("/api/v1/history/orders", params={"status": "REJECTED"}, headers=h)
    assert r.json()["total"] == 1 and r.json()["items"][0]["reason"].startswith("stop_loss_required")
    assert (await client.get("/api/v1/history/trades", headers=h)).json()["total"] == 3
    positions = (await client.get("/api/v1/history/positions", headers=h)).json()
    assert D(positions[0]["quantity"]) == D("0.3")
    audit = (await client.get("/api/v1/history/audit", params={"event": "bot."}, headers=h)).json()
    assert {i["event"] for i in audit["items"]} >= {"bot.created", "bot.start", "bot.stop"}
    dash = (await client.get(f"/api/v1/accounts/{acc['id']}/dashboard", headers=h)).json()
    assert dash["source"] == "paper" and dash["open_positions"] == 1
    assert (await client.delete(f"/api/v1/bots/{bot['id']}", headers=h)).status_code == 204


async def test_admin_endpoints_and_global_kill_switch(client, db) -> None:
    admin = bearer(await register_login(client, "root@example.com"))
    user = bearer(await register_login(client, "user@example.com"))
    client.cookies.clear()
    assert (await client.get("/api/v1/admin/stats", headers=user)).status_code == 403
    stats = (await client.get("/api/v1/admin/stats", headers=admin)).json()
    assert stats["users"] == 2 and stats["global_kill_switch"]["active"] is False

    users = (await client.get("/api/v1/admin/users", params={"q": "user@"}, headers=admin)).json()
    assert users["total"] == 1
    uid = users["items"][0]["id"]
    wss = (await client.get("/api/v1/admin/workspaces", headers=admin)).json()
    assert wss["total"] == 2

    acc = (
        await client.post(
            "/api/v1/accounts/",
            json={"label": "P", "api_key": "placeholder-key-0002", "api_secret": "placeholder-secret-0002"},
            headers=user,
        )
    ).json()
    await client.post(f"/api/v1/accounts/{acc['id']}/start", headers=user)

    r = await client.post("/api/v1/admin/kill-switch", json={"active": True, "reason": "teste"}, headers=admin)
    assert r.json()["active"] is True
    assert (await client.get(f"/api/v1/accounts/{acc['id']}", headers=user)).json()["status"] == "stopped"
    assert (await client.post(f"/api/v1/accounts/{acc['id']}/start", headers=user)).status_code == 409
    await client.post("/api/v1/admin/kill-switch", json={"active": False}, headers=admin)
    assert (await client.post(f"/api/v1/accounts/{acc['id']}/start", headers=user)).status_code == 200

    # deactivate user -> sessions revoked
    r = await client.patch(f"/api/v1/admin/users/{uid}", json={"is_active": False}, headers=admin)
    assert r.json()["is_active"] is False
    assert (await client.get("/api/v1/auth/me", headers=user)).status_code == 401
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    assert (
        await client.patch(f"/api/v1/admin/users/{me['id']}", json={"role": "viewer"}, headers=admin)
    ).status_code == 409
    audit = (await client.get("/api/v1/admin/audit", params={"event": "admin."}, headers=admin)).json()
    assert audit["total"] >= 3
    assert (await db.scalar(select(User).where(User.email == "user@example.com"))).token_version == 1


async def test_live_mode_requires_workspace_admin_and_server_flag(client) -> None:
    h = bearer(await register_login(client, "live@example.com"))
    r = await client.post(
        "/api/v1/accounts/",
        json={"label": "L", "api_key": "placeholder-key-0003", "api_secret": "placeholder-secret-0003",
              "execution_mode": "live"},
        headers=h,
    )  # fmt: skip
    assert r.status_code == 201 and r.json()["execution_mode"] == ExecutionMode.LIVE.value
    assert (await client.post(f"/api/v1/accounts/{r.json()['id']}/start", headers=h)).status_code == 409
