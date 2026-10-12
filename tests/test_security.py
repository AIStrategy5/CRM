from datetime import date, timedelta

import pytest

from app import security
from app.seed import seed

from .conftest import PASSWORD


def _kol(c, **kw):
    r = c.post("/api/kols", json={"name": "Dr. T", "specialty": "Onc", "therapeutic_area": "Oncology", **kw})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ authentication

def test_password_hashing():
    h = security.hash_password("correct horse battery")
    assert h.startswith("scrypt$") and "correct" not in h
    assert security.verify_password("correct horse battery", h)
    assert not security.verify_password("wrong", h)
    assert not security.verify_password("x", "garbage")


def test_api_requires_auth(anon):
    assert anon.get("/api/kols").status_code == 401
    assert anon.get("/api/kols", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert anon.get("/").status_code == 200  # static shell is public, data is not


def test_login_logout_and_me(anon, login):
    assert anon.post("/api/auth/login", json={"username": "msl", "password": "bad"}).status_code == 401
    c = login("msl")
    me = c.get("/api/auth/me").json()
    assert me["role"] == "msl" and "interaction.write" in me["permissions"] and "admin" not in me["permissions"]
    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/auth/me").status_code == 401


def test_lockout_after_failed_attempts(anon):
    for _ in range(5):
        anon.post("/api/auth/login", json={"username": "lead", "password": "bad"})
    r = anon.post("/api/auth/login", json={"username": "lead", "password": PASSWORD})
    assert r.status_code == 429


def test_bootstrap_admin_only_on_empty_db(monkeypatch):
    from app.db import connect, init_db
    c = connect(":memory:")
    init_db(c)
    monkeypatch.setenv("CRM_ADMIN_PASSWORD", "bootstrap-pass-1")
    security.bootstrap_admin(c)
    security.bootstrap_admin(c)
    users = c.execute("SELECT username, role FROM users").fetchall()
    assert [tuple(u) for u in users] == [("admin", "admin")]


# ------------------------------------------------------------------ RBAC

@pytest.mark.parametrize("role,expected", [("admin", 201), ("lead", 201), ("msl", 201), ("viewer", 403)])
def test_kol_create_permissions(login, role, expected):
    r = login(role).post("/api/kols", json={"name": "X", "specialty": "Y", "therapeutic_area": "Z"})
    assert r.status_code == expected


def test_role_matrix(login):
    admin, lead, msl, viewer = (login(r) for r in ("admin", "lead", "msl", "viewer"))
    k = _kol(admin)
    assert viewer.get("/api/analytics").status_code == 200
    assert viewer.get("/api/pv").status_code == 403
    assert msl.delete(f"/api/kols/{k['id']}").status_code == 403
    assert lead.delete(f"/api/kols/{k['id']}").status_code == 403
    assert msl.post("/api/actions", json={"title": "t"}).status_code == 403
    assert msl.get("/api/audit").status_code == 403 and lead.get("/api/audit").status_code == 200
    assert lead.get("/api/users").status_code == 403
    assert msl.get("/api/export/kols.csv").status_code == 403
    assert lead.get(f"/api/kols/{k['id']}/export").status_code == 403
    a = lead.post("/api/actions", json={"title": "t"}).json()
    assert msl.patch(f"/api/actions/{a['id']}", json={"status": "done"}).status_code == 200
    assert msl.patch(f"/api/actions/{a['id']}", json={"priority": "high"}).status_code == 403


def test_user_management(client, login, anon):
    r = client.post("/api/users", json={"username": "newmsl", "role": "msl", "password": "a-long-password"})
    assert r.status_code == 201 and "password_hash" not in r.json()
    assert client.post("/api/users", json={"username": "newmsl", "role": "msl",
                                           "password": "a-long-password"}).status_code == 409
    assert client.post("/api/users", json={"username": "x", "role": "msl", "password": "short"}).status_code == 422
    uid = r.json()["id"]
    tok = anon.post("/api/auth/login", json={"username": "newmsl", "password": "a-long-password"}).json()["token"]
    assert client.patch(f"/api/users/{uid}", json={"active": False}).json()["active"] == 0
    # deactivation revokes existing sessions
    assert anon.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
    admin_id = [u for u in client.get("/api/users").json() if u["username"] == "admin"][0]["id"]
    assert client.patch(f"/api/users/{admin_id}", json={"role": "msl"}).status_code == 400  # last admin
    # password hashes never land in the audit trail
    assert all("scrypt$" not in (a["after_json"] or "") for a in client.get("/api/audit?limit=2000").json())


def test_change_password(login, anon):
    c = login("viewer")
    assert c.post("/api/auth/password", json={"current_password": "bad", "new_password": "new-password-99"}
                  ).status_code == 400
    assert c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "new-password-99"}
                  ).status_code == 204
    assert anon.post("/api/auth/login", json={"username": "viewer", "password": "new-password-99"}).status_code == 200


# ------------------------------------------------------------------ audit trail

def test_audit_records_changes_with_reason(login):
    c = login("admin", reason=None)
    k = _kol(c)
    body = {**{f: k[f] for f in ("name", "specialty", "therapeutic_area")}, "tier": 1}
    assert c.put(f"/api/kols/{k['id']}", json=body).status_code == 400  # reason required
    r = c.put(f"/api/kols/{k['id']}", json=body, headers={"X-Change-Reason": "Tier review Q3"})
    assert r.status_code == 200
    log = c.get(f"/api/audit?entity=kols&entity_id={k['id']}").json()
    assert [e["action"] for e in log] == ["update", "create"]
    assert log[0]["reason"] == "Tier review Q3" and '"tier":2' in log[0]["before_json"]
    assert log[0]["username"] == "admin"


def test_audit_chain_verifies_and_detects_tampering(client, conn):
    _kol(client)
    _kol(client)
    assert client.get("/api/audit/verify").json()["valid"]
    # The DB refuses edits outright...
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE audit_log SET username = 'mallory'")
    # ...and if someone bypasses the triggers, the hash chain exposes it.
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("UPDATE audit_log SET username = 'mallory' WHERE id = 2")
    v = client.get("/api/audit/verify").json()
    assert not v["valid"] and v["first_invalid_id"] == 2


def test_reason_requirement_can_be_disabled(login):
    admin = login("admin", reason=None)
    assert admin.put("/api/settings", json={"require_change_reason": False}).status_code == 200
    k = _kol(admin)
    assert admin.delete(f"/api/kols/{k['id']}").status_code == 204


# ------------------------------------------------------------------ PV referrals

def test_ae_creates_pv_referral_and_resolution_rules(login):
    msl, lead = login("msl"), login("lead")
    k = _kol(msl)
    it = msl.post("/api/interactions", json={"kol_id": k["id"], "date": date.today().isoformat(),
                                             "type": "one_to_one",
                                             "notes": "Patient was hospitalized after the third dose."}).json()
    assert len(it["pv_referrals"]) == 1
    ref = it["pv_referrals"][0]
    assert ref["status"] == "open" and ref["due_at"] > security.iso(security.now_utc())
    assert msl.patch(f"/api/pv/{ref['id']}", json={"status": "reported", "pv_case_ref": "X"}).status_code == 403
    assert lead.patch(f"/api/pv/{ref['id']}", json={"status": "reported"}).status_code == 400
    assert lead.patch(f"/api/pv/{ref['id']}", json={"status": "not_an_ae"}).status_code == 400
    done = lead.patch(f"/api/pv/{ref['id']}", json={"status": "reported", "pv_case_ref": "PV-2026-001"}).json()
    assert done["resolved_by"] is not None and done["pv_case_ref"] == "PV-2026-001"
    assert lead.get("/api/analytics").json()["overview"]["open_pv_referrals"] == 0


def test_no_duplicate_open_referral_and_overdue(client, conn):
    k = _kol(client)
    it = client.post("/api/interactions", json={"kol_id": k["id"], "date": "2026-01-01", "type": "congress",
                                                "notes": "Possible adverse event: rash."}).json()
    acc = client.post("/api/ai/accept", json={"compliance_flags": [
        {"interaction_id": it["id"], "flag_type": "potential_adverse_event", "excerpt": "rash",
         "recommended_step": "report"}]}).json()
    assert acc["pv_referrals"] == []  # already open from logging
    conn.execute("UPDATE pv_referrals SET due_at = '2000-01-01T00:00:00Z'")
    conn.commit()
    pv = client.get("/api/pv").json()
    assert pv[0]["overdue"] and client.get("/api/analytics").json()["overview"]["overdue_pv_referrals"] == 1


# ------------------------------------------------------------------ data protection

def test_withdrawn_consent_excluded_from_ai(client):
    a = _kol(client, consent_status="granted")
    b = _kol(client, consent_status="withdrawn")
    for k in (a, b):
        client.post("/api/interactions", json={"kol_id": k["id"], "date": "2026-03-01", "type": "one_to_one",
                                               "notes": "Need more real-world data."})
    r = client.post("/api/ai/synthesize", json={}).json()
    assert r["scope"]["interactions"] == 1 and r["scope"]["kols_excluded_no_consent"] == 1


def test_data_subject_export(client):
    k = _kol(client)
    client.post("/api/interactions", json={"kol_id": k["id"], "date": "2026-03-01", "type": "congress"})
    r = client.get(f"/api/kols/{k['id']}/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    b = r.json()
    assert b["kol"]["id"] == k["id"] and len(b["interactions"]) == 1 and b["audit_log"]
    assert client.get(f"/api/audit?entity=kols&entity_id={k['id']}").json()[0]["action"] == "export"


def test_retention_purge_dry_run_then_delete(client, conn):
    seed(conn, today=date.today())
    old = (date.today() - timedelta(days=400)).isoformat()
    k = _kol(client)
    client.post("/api/interactions", json={"kol_id": k["id"], "date": old, "type": "congress",
                                           "notes": "Patient hospitalized - adverse event."})
    before = len(client.get("/api/interactions?limit=5000").json())
    dry = client.post("/api/admin/retention", json={"older_than_days": 380, "dry_run": True}).json()
    assert dry["interactions"] >= 1 and len(client.get("/api/interactions?limit=5000").json()) == before
    real = client.post("/api/admin/retention", json={"older_than_days": 380, "dry_run": False}).json()
    after = client.get("/api/interactions?limit=5000").json()
    assert len(after) == before - real["interactions"] and all(i["date"] >= real["cutoff"] for i in after)
    assert client.get("/api/pv").json()  # PV referral survives purge
    assert client.get("/api/audit/verify").json()["valid"]


# ------------------------------------------------------------------ settings

def test_settings_validation_and_effect(client, conn):
    seed(conn, today=date.today())
    assert client.put("/api/settings", json={"bogus": 1}).status_code == 422
    assert client.put("/api/settings", json={"influence_weights": {"publications": 1, "trials": 1, "tier": 1}}
                      ).status_code == 422
    assert client.put("/api/settings", json={"recency_half_life_days": -5}).status_code == 422
    before = {k["id"]: k["influence"] for k in client.get("/api/kols").json()}
    r = client.put("/api/settings", json={"influence_weights": {"publications": 0, "trials": 0, "tier": 1}})
    assert r.status_code == 200
    after = {k["id"]: k["influence"] for k in client.get("/api/kols").json()}
    assert after != before and set(after.values()) <= {30.0, 60.0, 100.0}


def test_ai_disabled_setting_forces_rules(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    client.put("/api/settings", json={"ai_enabled": False})
    k = _kol(client)
    client.post("/api/interactions", json={"kol_id": k["id"], "date": "2026-03-01", "type": "congress",
                                           "notes": "education needed"})
    r = client.post("/api/ai/synthesize", json={}).json()
    assert r["engine"] == "rules"
    assert client.get("/api/meta").json()["ai_engine"] == "rules"


def test_accept_rejects_malformed_flag_atomically(client):
    k = _kol(client)
    it = client.post("/api/interactions", json={"kol_id": k["id"], "date": "2026-01-01", "type": "congress"}).json()
    n_before = len(client.get("/api/insights").json())
    r = client.post("/api/ai/accept", json={
        "themes": [{"title": "t", "category": "data_gap", "summary": "s", "supporting_interaction_ids": [it["id"]]}],
        "compliance_flags": [{"interaction_id": it["id"], "flag_type": "potential_adverse_event"}]})
    assert r.status_code == 422
    assert len(client.get("/api/insights").json()) == n_before  # nothing half-saved
    assert client.get("/api/audit/verify").json()["valid"]
