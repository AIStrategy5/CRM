"""FastAPI application: REST API + static single-page UI."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import ai, stats
from .db import connect, init_db, rows
from .models import (CHANNELS, INSIGHT_CATEGORIES, INTERACTION_TYPES, OWNER_FUNCTIONS,
                     ActionIn, ActionPatch, InsightIn, InteractionIn, KOLIn, SynthesisRequest)

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Medical Affairs KOL CRM", version="0.1.0")
_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
        init_db(_conn)
    return _conn


def set_db(conn: sqlite3.Connection) -> None:
    """Swap the connection (used by tests and the seed script)."""
    global _conn
    _conn = conn
    init_db(conn)


def _one(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def _require(conn, table: str, id_: int) -> dict:
    r = _one(conn, f"SELECT * FROM {table} WHERE id = ?", (id_,))
    if not r:
        raise HTTPException(404, f"{table[:-1]} {id_} not found")
    return r


def _insert(conn, table: str, data: dict) -> dict:
    cols = ", ".join(data)
    qs = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(data.values()))
    conn.commit()
    return _require(conn, table, cur.lastrowid)


def _action_out(a: dict) -> dict:
    a["insight_ids"] = json.loads(a["insight_ids"] or "[]")
    return a


# ---------------------------------------------------------------- reference data

@app.get("/api/meta")
def meta():
    return {"interaction_types": INTERACTION_TYPES, "channels": CHANNELS,
            "insight_categories": INSIGHT_CATEGORIES, "owner_functions": OWNER_FUNCTIONS,
            "ai_engine": "claude" if ai.claude_available() else "rules", "model": ai.MODEL}


# ---------------------------------------------------------------- KOLs

@app.get("/api/kols")
def list_kols(q: str | None = None, therapeutic_area: str | None = None, tier: int | None = None,
              conn=Depends(get_db)):
    sql, params = "SELECT * FROM kols WHERE 1=1", []
    if q:
        sql += " AND (name LIKE ? OR institution LIKE ? OR specialty LIKE ?)"
        params += [f"%{q}%"] * 3
    if therapeutic_area:
        sql += " AND therapeutic_area = ?"
        params.append(therapeutic_area)
    if tier:
        sql += " AND tier = ?"
        params.append(tier)
    kols = rows(conn.execute(sql + " ORDER BY name", params))
    its = rows(conn.execute("SELECT * FROM interactions"))
    ins = rows(conn.execute("SELECT kol_id, category FROM insights"))
    eng = {r["kol_id"]: r for r in stats.kol_engagement(kols, its, ins)}
    for k in kols:
        e = eng.get(k["id"], {})
        k.update({f: e.get(f) for f in ("engagement", "influence", "segment", "interactions",
                                          "last_interaction", "avg_sentiment")})
    return kols


@app.post("/api/kols", status_code=201)
def create_kol(body: KOLIn, conn=Depends(get_db)):
    return _insert(conn, "kols", body.model_dump())


@app.get("/api/kols/{kol_id}")
def get_kol(kol_id: int, conn=Depends(get_db)):
    k = _require(conn, "kols", kol_id)
    k["interactions"] = rows(conn.execute(
        "SELECT * FROM interactions WHERE kol_id = ? ORDER BY date DESC", (kol_id,)))
    k["insights"] = rows(conn.execute(
        "SELECT * FROM insights WHERE kol_id = ? ORDER BY created_at DESC", (kol_id,)))
    k["influence"] = stats.influence_score(k)
    return k


@app.put("/api/kols/{kol_id}")
def update_kol(kol_id: int, body: KOLIn, conn=Depends(get_db)):
    _require(conn, "kols", kol_id)
    data = body.model_dump()
    conn.execute(f"UPDATE kols SET {', '.join(f'{c} = ?' for c in data)} WHERE id = ?",
                 (*data.values(), kol_id))
    conn.commit()
    return _require(conn, "kols", kol_id)


@app.delete("/api/kols/{kol_id}", status_code=204)
def delete_kol(kol_id: int, conn=Depends(get_db)):
    _require(conn, "kols", kol_id)
    conn.execute("DELETE FROM kols WHERE id = ?", (kol_id,))
    conn.commit()


# ---------------------------------------------------------------- Interactions

@app.get("/api/interactions")
def list_interactions(kol_id: int | None = None, type: str | None = None, since: date | None = None,
                      limit: int = Query(500, le=5000), conn=Depends(get_db)):
    sql = ("SELECT i.*, k.name AS kol_name, k.therapeutic_area FROM interactions i "
           "JOIN kols k ON k.id = i.kol_id WHERE 1=1")
    params: list = []
    if kol_id:
        sql += " AND i.kol_id = ?"
        params.append(kol_id)
    if type:
        sql += " AND i.type = ?"
        params.append(type)
    if since:
        sql += " AND i.date >= ?"
        params.append(since.isoformat())
    return rows(conn.execute(sql + " ORDER BY i.date DESC, i.id DESC LIMIT ?", (*params, limit)))


@app.post("/api/interactions", status_code=201)
def create_interaction(body: InteractionIn, conn=Depends(get_db)):
    _require(conn, "kols", body.kol_id)
    data = body.model_dump()
    data["date"] = body.date.isoformat()
    created = _insert(conn, "interactions", data)
    # Immediate feedback for the MSL: suggested insight categories + compliance check.
    created["suggested_categories"] = ai.classify_text(f"{body.topics or ''} {body.notes}")
    created["compliance_flags"] = [f.model_dump() for f in ai.compliance_scan([created])]
    return created


@app.delete("/api/interactions/{interaction_id}", status_code=204)
def delete_interaction(interaction_id: int, conn=Depends(get_db)):
    _require(conn, "interactions", interaction_id)
    conn.execute("DELETE FROM interactions WHERE id = ?", (interaction_id,))
    conn.commit()


# ---------------------------------------------------------------- Insights

@app.get("/api/insights")
def list_insights(category: str | None = None, therapeutic_area: str | None = None,
                  conn=Depends(get_db)):
    sql = ("SELECT s.*, k.name AS kol_name FROM insights s LEFT JOIN kols k ON k.id = s.kol_id "
           "WHERE 1=1")
    params = []
    if category:
        sql += " AND s.category = ?"
        params.append(category)
    if therapeutic_area:
        sql += " AND s.therapeutic_area = ?"
        params.append(therapeutic_area)
    return rows(conn.execute(sql + " ORDER BY s.created_at DESC, s.id DESC", params))


@app.post("/api/insights", status_code=201)
def create_insight(body: InsightIn, conn=Depends(get_db)):
    data = body.model_dump()
    if body.interaction_id:
        it = _require(conn, "interactions", body.interaction_id)
        data["kol_id"] = data["kol_id"] or it["kol_id"]
    if data["kol_id"]:
        k = _require(conn, "kols", data["kol_id"])
        data["therapeutic_area"] = data["therapeutic_area"] or k["therapeutic_area"]
    return _insert(conn, "insights", data)


@app.delete("/api/insights/{insight_id}", status_code=204)
def delete_insight(insight_id: int, conn=Depends(get_db)):
    _require(conn, "insights", insight_id)
    conn.execute("DELETE FROM insights WHERE id = ?", (insight_id,))
    conn.commit()


# ---------------------------------------------------------------- AI synthesis

@app.post("/api/ai/synthesize")
def synthesize(req: SynthesisRequest, conn=Depends(get_db)):
    sql = ("SELECT i.* FROM interactions i JOIN kols k ON k.id = i.kol_id WHERE 1=1")
    params: list = []
    if req.therapeutic_area:
        sql += " AND k.therapeutic_area = ?"
        params.append(req.therapeutic_area)
    if req.kol_id:
        sql += " AND i.kol_id = ?"
        params.append(req.kol_id)
    if req.since:
        sql += " AND i.date >= ?"
        params.append(req.since.isoformat())
    its = rows(conn.execute(sql + " ORDER BY i.date DESC LIMIT ?", (*params, req.max_interactions)))
    kols = {k["id"]: k for k in rows(conn.execute("SELECT * FROM kols"))}
    out = ai.synthesize(its, kols, use_ai=req.use_ai)
    out["scope"] = {"interactions": len(its), **req.model_dump(mode="json")}
    return out


@app.post("/api/ai/accept")
def accept_synthesis(payload: dict, conn=Depends(get_db)):
    """Persist selected themes as insights and outcomes as tracked actions."""
    created = {"insights": [], "actions": []}
    for t in payload.get("themes", []):
        ids = t.get("supporting_interaction_ids") or []
        first = _one(conn, "SELECT * FROM interactions WHERE id = ?", (ids[0],)) if ids else None
        k = _one(conn, "SELECT * FROM kols WHERE id = ?", (first["kol_id"],)) if first else None
        cat = t.get("category") if t.get("category") in INSIGHT_CATEGORIES else "clinical_practice"
        created["insights"].append(_insert(conn, "insights", {
            "kol_id": k["id"] if k else None, "interaction_id": first["id"] if first else None,
            "category": cat, "therapeutic_area": k["therapeutic_area"] if k else None,
            "text": f"{t.get('title')}: {t.get('summary')}", "source": "ai"}))
    for o in payload.get("actionable_outcomes", []):
        created["actions"].append(_action_out(_insert(conn, "actions", {
            "title": o["title"], "rationale": o.get("rationale"),
            "owner_function": o.get("owner_function", "Medical Affairs"),
            "priority": o.get("priority", "medium") if o.get("priority") in ("low", "medium", "high")
            else "medium",
            "insight_ids": json.dumps([i["id"] for i in created["insights"]]), "source": "ai"})))
    return created


# ---------------------------------------------------------------- Actions

@app.get("/api/actions")
def list_actions(status: str | None = None, conn=Depends(get_db)):
    sql, params = "SELECT * FROM actions", []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += (" ORDER BY CASE status WHEN 'done' THEN 1 ELSE 0 END, "
            "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, due_date")
    return [_action_out(a) for a in rows(conn.execute(sql, params))]


@app.post("/api/actions", status_code=201)
def create_action(body: ActionIn, conn=Depends(get_db)):
    data = body.model_dump()
    data["due_date"] = body.due_date.isoformat() if body.due_date else None
    data["insight_ids"] = json.dumps(body.insight_ids)
    return _action_out(_insert(conn, "actions", data))


@app.patch("/api/actions/{action_id}")
def patch_action(action_id: int, body: ActionPatch, conn=Depends(get_db)):
    _require(conn, "actions", action_id)
    data = {k: (v.isoformat() if isinstance(v, date) else v)
            for k, v in body.model_dump(exclude_unset=True).items()}
    if data:
        conn.execute(f"UPDATE actions SET {', '.join(f'{c} = ?' for c in data)} WHERE id = ?",
                     (*data.values(), action_id))
        conn.commit()
    return _action_out(_require(conn, "actions", action_id))


@app.delete("/api/actions/{action_id}", status_code=204)
def delete_action(action_id: int, conn=Depends(get_db)):
    _require(conn, "actions", action_id)
    conn.execute("DELETE FROM actions WHERE id = ?", (action_id,))
    conn.commit()


# ---------------------------------------------------------------- Analytics

def _all(conn):
    return (rows(conn.execute("SELECT * FROM kols")),
            rows(conn.execute("SELECT * FROM interactions")),
            rows(conn.execute("SELECT * FROM insights")),
            rows(conn.execute("SELECT * FROM actions")))


@app.get("/api/analytics")
def analytics(conn=Depends(get_db)):
    return stats.full_report(*_all(conn))


@app.get("/api/export/{table}.csv")
def export_csv(table: str, conn=Depends(get_db)):
    if table == "engagement":
        k, i, s, _ = _all(conn)
        data = stats.kol_engagement(k, i, s)
    elif table in ("kols", "interactions", "insights", "actions"):
        data = rows(conn.execute(f"SELECT * FROM {table}"))
    else:
        raise HTTPException(404, "unknown table")
    buf = io.StringIO()
    if data:
        w = csv.DictWriter(buf, fieldnames=list(data[0]))
        w.writeheader()
        w.writerows(data)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={table}.csv"})


# ---------------------------------------------------------------- UI

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
