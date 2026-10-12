from datetime import date

from app.seed import seed


def _kol(client, **kw):
    body = {"name": "Dr. Test", "specialty": "Oncology", "therapeutic_area": "Oncology", "tier": 1,
            "publications": 50, "trials_involved": 5, **kw}
    r = client.post("/api/kols", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_kol_crud(client):
    k = _kol(client)
    assert client.get(f"/api/kols/{k['id']}").json()["name"] == "Dr. Test"
    upd = client.put(f"/api/kols/{k['id']}", json={**k, "tier": 2}).json()
    assert upd["tier"] == 2
    assert client.get("/api/kols?q=Test").json()[0]["id"] == k["id"]
    assert client.delete(f"/api/kols/{k['id']}").status_code == 204
    assert client.get(f"/api/kols/{k['id']}").status_code == 404


def test_validation_errors(client):
    assert client.post("/api/kols", json={"name": "x", "specialty": "y", "therapeutic_area": "z",
                                          "tier": 9}).status_code == 422
    assert client.post("/api/interactions", json={"kol_id": 999, "date": "2026-01-01",
                                                  "type": "one_to_one"}).status_code == 404
    k = _kol(client)
    assert client.post("/api/interactions", json={"kol_id": k["id"], "date": "2026-01-01",
                                                  "type": "bogus"}).status_code == 422


def test_interaction_returns_suggestions_and_flags(client):
    k = _kol(client)
    r = client.post("/api/interactions", json={
        "kol_id": k["id"], "date": date.today().isoformat(), "type": "one_to_one", "sentiment": 0.4,
        "notes": "Wants real-world data. One patient was hospitalized with a rash."})
    assert r.status_code == 201
    body = r.json()
    assert "data_gap" in body["suggested_categories"]
    assert body["compliance_flags"][0]["flag_type"] == "potential_adverse_event"


def test_insight_inherits_kol_and_ta_from_interaction(client):
    k = _kol(client, therapeutic_area="Immunology")
    it = client.post("/api/interactions", json={"kol_id": k["id"], "date": "2026-02-01",
                                                "type": "congress"}).json()
    ins = client.post("/api/insights", json={"interaction_id": it["id"], "category": "unmet_need",
                                             "text": "Refractory patients"}).json()
    assert ins["kol_id"] == k["id"] and ins["therapeutic_area"] == "Immunology"


def test_actions_lifecycle(client):
    a = client.post("/api/actions", json={"title": "Do thing", "priority": "high",
                                          "insight_ids": [1, 2]}).json()
    assert a["insight_ids"] == [1, 2] and a["status"] == "open"
    a = client.patch(f"/api/actions/{a['id']}", json={"status": "done"}).json()
    assert a["status"] == "done"
    assert client.get("/api/actions?status=done").json()[0]["id"] == a["id"]


def test_synthesis_and_accept_on_seed_data(client, conn):
    seed(conn, today=date(2026, 6, 30))
    r = client.post("/api/ai/synthesize", json={"therapeutic_area": "Oncology", "max_interactions": 50})
    assert r.status_code == 200
    out = r.json()
    assert out["engine"] == "rules" and out["scope"]["interactions"] > 0
    res = out["result"]
    assert res["themes"] and res["actionable_outcomes"]
    before = len(client.get("/api/actions").json())
    acc = client.post("/api/ai/accept", json={"themes": res["themes"][:2],
                                              "actionable_outcomes": res["actionable_outcomes"][:1]}).json()
    assert len(acc["insights"]) == 2 and len(acc["actions"]) == 1
    assert len(client.get("/api/actions").json()) == before + 1


def test_analytics_and_exports(client, conn):
    seed(conn, today=date(2026, 6, 30))
    a = client.get("/api/analytics").json()
    for key in ("overview", "engagement", "monthly", "sentiment_by_tier", "influence_vs_engagement",
                "insight_association", "segments"):
        assert key in a
    assert a["overview"]["kols"] == 36
    csv = client.get("/api/export/engagement.csv")
    assert csv.status_code == 200 and csv.text.startswith("kol_id,")
    assert client.get("/api/export/secrets.csv").status_code == 404


def test_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Medical Affairs CRM" in r.text
    assert client.get("/static/app.js").status_code == 200
