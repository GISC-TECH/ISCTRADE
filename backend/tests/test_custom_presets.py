"""Custom bot presets per workspace: list, create, clone (built-in clone is admin-only), edit, delete, isolation."""

from tests.test_api_e2e import bearer, register_login

API = "/api/v1/strategies/presets"


async def _user(client, email: str) -> tuple[dict, str]:
    login = await register_login(client, email)
    client.cookies.clear()  # use only the bearer header (several users in one test)
    h = bearer(login)
    ws = login["user"]["active_workspace_id"]
    return {**h, "X-Workspace-ID": ws}, ws


async def test_builtins_listed_and_admin_clones_then_edits(client) -> None:
    owner, ws = await _user(client, "owner@presets.com")
    listed = (await client.get(API, headers=owner)).json()
    builtin = next(p for p in listed if p["source"] == "builtin")
    assert builtin["editable"] is False and builtin["execution_mode"] == "paper"

    r = await client.post(f"{API}/clone", json={"source_key": builtin["key"]}, headers=owner)
    assert r.status_code == 201, r.text
    clone = r.json()
    assert clone["source"] == "custom" and clone["editable"] and clone["based_on"] == builtin["key"]
    assert clone["name"].endswith("(cópia)") and clone["params"] == builtin["params"]
    assert clone["key"] == f"custom:{clone['id']}"

    r = await client.patch(
        f"{API}/{clone['id']}",
        json={"name": "Minha tendência", "leverage": 2, "tags": [" a ", "a", "b"], "params": {"risk_fraction": 0.3}},
        headers=owner,
    )
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["name"] == "Minha tendência" and edited["leverage"] == 2 and edited["tags"] == ["a", "b"]
    assert edited["params"]["risk_fraction"] == 0.3
    # invalid params for the strategy -> 422; execution mode can never be set
    bad = await client.patch(f"{API}/{clone['id']}", json={"params": {"risk_fraction": -5}}, headers=owner)
    assert bad.status_code == 422
    listed = (await client.get(API, headers=owner)).json()
    assert listed[0]["id"] == clone["id"]  # customs first
    assert all(p["execution_mode"] == "paper" for p in listed)

    assert (await client.delete(f"{API}/{clone['id']}", headers=owner)).status_code == 204
    assert all(p["source"] == "builtin" for p in (await client.get(API, headers=owner)).json())


async def test_roles_and_workspace_isolation(client) -> None:
    owner, ws = await _user(client, "o2@presets.com")
    trader, _ = await _user(client, "t2@presets.com")
    viewer, _ = await _user(client, "v2@presets.com")
    other, _ = await _user(client, "x2@presets.com")
    for email, role in (("t2@presets.com", "trader"), ("v2@presets.com", "viewer")):
        r = await client.post(f"/api/v1/workspaces/{ws}/members", json={"email": email, "role": role}, headers=owner)
        assert r.status_code == 201, r.text
    trader = {**trader, "X-Workspace-ID": ws}
    viewer = {**viewer, "X-Workspace-ID": ws}
    builtin_key = (await client.get(API, headers=owner)).json()[0]["key"]

    # a trader cannot customise a built-in (admin-only) but can create from scratch
    assert (await client.post(f"{API}/clone", json={"source_key": builtin_key}, headers=trader)).status_code == 403
    body = {"name": "EMA rápida", "strategy_key": "ema_cross", "interval": "15m", "symbol": "ETHUSDT"}
    r = await client.post(API, json=body, headers=trader)
    assert r.status_code == 201, r.text
    mine = r.json()
    assert mine["params"]  # completed with the strategy defaults
    # unknown strategy / bad interval rejected
    assert (await client.post(API, json={**body, "strategy_key": "nope"}, headers=trader)).status_code == 422
    assert (await client.post(API, json={**body, "interval": "7m"}, headers=trader)).status_code == 422
    # trader can clone a custom preset; viewer can read but not write
    assert (await client.post(f"{API}/clone", json={"source_key": mine["key"]}, headers=trader)).status_code == 201
    assert any(p["id"] == mine["id"] for p in (await client.get(API, headers=viewer)).json())
    assert (await client.post(API, json=body, headers=viewer)).status_code == 403
    viewer_view = next(p for p in (await client.get(API, headers=viewer)).json() if p["id"] == mine["id"])
    assert viewer_view["editable"] is False
    # admin can edit the trader's preset; another trader's preset is not editable by a different trader
    admin_edit = await client.patch(f"{API}/{mine['id']}", json={"description": "ok"}, headers=owner)
    assert admin_edit.status_code == 200
    admin_preset = (await client.post(API, json={**body, "name": "do admin"}, headers=owner)).json()
    assert (await client.patch(f"{API}/{admin_preset['id']}", json={"name": "x"}, headers=trader)).status_code == 403
    # other workspaces never see or touch these presets
    assert all(p["source"] == "builtin" for p in (await client.get(API, headers=other)).json())
    assert (await client.delete(f"{API}/{mine['id']}", headers=other)).status_code == 404
    assert (await client.post(f"{API}/clone", json={"source_key": mine["key"]}, headers=other)).status_code == 404
