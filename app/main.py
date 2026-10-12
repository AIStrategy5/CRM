"""FastAPI application: authenticated REST API + static single-page UI."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from . import ai, security, stats
from .db import connect, init_db, rows
from .models import (CHANNELS, INSIGHT_CATEGORIES, INTERACTION_TYPES, OWNER_FUNCTIONS, ActionIn,
                     ActionPatch, InsightIn, InteractionIn, KOLIn, LoginIn, PasswordChange, PVPatch,
                     RetentionRequest, SynthesisRequest, UserIn, UserPatch)
from .security import audit, can, iso, now_utc

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Medical Affairs KOL CRM", version="0.2.0")
_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
        init_db(_conn)
        security.bootstrap_admin(_conn)
    return _conn


def set_db(conn: sqlite3.Connection) -> None:
    """Swap the connection (used by tests)."""
    global _conn
    _conn = conn
    init_db(conn)


# ================================================================ auth dependencies

bearer = HTTPBearer(auto_error=False)


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                 conn=Depends(get_db)) -> dict:
    user = security.user_for_token(conn, creds.credentials) if creds else None
    if not user:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return user


def require(perm: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if not can(user["role"], perm):
            raise HTTPException(403, f"Role '{user['role']}' lacks permission '{perm}'")
        return user
    return dep


def change_reason(x_change_reason: str | None = Header(None), conn=Depends(get_db)) -> str | None:
    """Reason for modifying/deleting a record (sent as the X-Change-Reason header)."""
    reason = (x_change_reason or "").strip() or None
    if reason is None and security.get_settings(conn)["require_change_reason"]:
        raise HTTPException(400, "A reason for change is required (X-Change-Reason header)")
    return reason


# ================================================================ data helpers

def _one(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def _require(conn, table: str, id_: int) -> dict:
    r = _one(conn, f"SELECT * FROM {table} WHERE id = ?", (id_,))
    if not r:
        raise HTTPException(404, f"{table.rstrip('s')} {id_} not found")
    return r


def _insert(conn, table: str, data: dict, user: dict | None, commit: bool = True) -> dict:
    cols = ", ".join(data)
    qs = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(data.values()))
    created = _require(conn, table, cur.lastrowid)
    audit(conn, user, "create", table, created["id"], after=created)
    if commit:
        conn.commit()
    return created


def _update(conn, table: str, id_: int, data: dict, user: dict, reason: str | None) -> dict:
    before = _require(conn, table, id_)
    if data:
        conn.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in data)} WHERE id = ?",
                     (*data.values(), id_))
    after = _require(conn, table, id_)
    if after != before:
        audit(conn, user, "update", table, id_, before=before, after=after, reason=reason)
    conn.commit()
    return after


def _delete(conn, table: str, id_: int, user: dict, reason: str | None) -> None:
    before = _require(conn, table, id_)
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (id_,))
    audit(conn, user, "delete", table, id_, before=before, reason=reason)
    conn.commit()


def _action_out(a: dict) -> dict:
    a["insight_ids"] = json.loads(a["insight_ids"] or "[]")
    return a


def _create_pv_referrals(conn, interaction: dict, flags, user) -> list[dict]:
    """Open a PV referral for each potential adverse event flag (one open referral per interaction)."""
    out = []
    sla = security.get_settings(conn)["pv_sla_hours"]
    for f in flags:
        if f.flag_type != "potential_adverse_event":
            continue
        if _one(conn, "SELECT id FROM pv_referrals WHERE interaction_id = ? AND status = 'open'",
                (interaction["id"],)):
            continue
        out.append(_insert(conn, "pv_referrals", {
            "interaction_id": interaction["id"], "kol_id": interaction["kol_id"], "excerpt": f.excerpt,
            "due_at": iso(now_utc() + timedelta(hours=sla)),
            "created_by": user["id"] if user else None}, user, commit=False))
    return out


# ================================================================ auth endpoints

_failed: dict[str, list[float]] = defaultdict(list)
MAX_FAILED, LOCK_SECONDS = 5, 900
_DUMMY_HASH = security.hash_password(security.secrets.token_urlsafe(16))


@app.post("/api/auth/login")
def login(body: LoginIn, conn=Depends(get_db)):
    key = body.username.lower()
    recent = [t for t in _failed[key] if time.time() - t < LOCK_SECONDS]
    _failed[key] = recent
    if len(recent) >= MAX_FAILED:
        raise HTTPException(429, "Too many failed attempts; try again later")
    u = _one(conn, "SELECT * FROM users WHERE username = ?", (body.username,))
    # Always run one scrypt verification so response time doesn't reveal whether the user exists.
    ok = security.verify_password(body.password, u["password_hash"] if u else _DUMMY_HASH)
    if not u or not u["active"] or not ok:
        _failed[key].append(time.time())
        audit(conn, None, "login_failed", "users", u["id"] if u else None, after={"username": body.username})
        conn.commit()
        raise HTTPException(401, "Invalid username or password")
    _failed.pop(key, None)
    token, expires = security.create_session(conn, u["id"])
    audit(conn, u, "login", "users", u["id"])
    conn.commit()
    return {"token": token, "expires_at": expires,
            "user": {k: u[k] for k in ("id", "username", "full_name", "role")}}


@app.post("/api/auth/logout", status_code=204)
def logout(creds: HTTPAuthorizationCredentials | None = Depends(bearer), user=Depends(current_user),
           conn=Depends(get_db)):
    security.end_session(conn, creds.credentials)


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    perms = sorted(p for p in security.PERMISSIONS if can(user["role"], p))
    return {**user, "permissions": perms}


@app.post("/api/auth/password", status_code=204)
def change_password(body: PasswordChange, user=Depends(current_user), conn=Depends(get_db)):
    u = _one(conn, "SELECT * FROM users WHERE id = ?", (user["id"],))
    if not security.verify_password(body.current_password, u["password_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (security.hash_password(body.new_password), user["id"]))
    audit(conn, user, "password_change", "users", user["id"])
    conn.commit()


# ================================================================ users (admin)

def _user_out(u: dict) -> dict:
    return {k: v for k, v in u.items() if k != "password_hash"}


@app.get("/api/users")
def list_users(user=Depends(require("admin")), conn=Depends(get_db)):
    return [_user_out(u) for u in rows(conn.execute("SELECT * FROM users ORDER BY username"))]


@app.post("/api/users", status_code=201)
def create_user(body: UserIn, user=Depends(require("admin")), conn=Depends(get_db)):
    if _one(conn, "SELECT id FROM users WHERE username = ?", (body.username,)):
        raise HTTPException(409, "Username already exists")
    cur = conn.execute("INSERT INTO users (username, full_name, role, password_hash) VALUES (?,?,?,?)",
                       (body.username, body.full_name, body.role, security.hash_password(body.password)))
    new = _require(conn, "users", cur.lastrowid)
    audit(conn, user, "create", "users", new["id"], after=_user_out(new))  # never log the hash
    conn.commit()
    return _user_out(new)


@app.patch("/api/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, user=Depends(require("admin")), conn=Depends(get_db)):
    target = _require(conn, "users", user_id)
    data = body.model_dump(exclude_unset=True)
    if "password" in data:
        data["password_hash"] = security.hash_password(data.pop("password"))
    if "active" in data:
        data["active"] = int(data["active"])
    demoting = data.get("role", target["role"]) != "admin" or data.get("active", 1) == 0
    if target["role"] == "admin" and demoting:
        admins = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
        if admins <= 1:
            raise HTTPException(400, "Cannot remove the last active admin")
    if data:
        conn.execute(f"UPDATE users SET {', '.join(f'{c} = ?' for c in data)} WHERE id = ?",
                     (*data.values(), user_id))
        if data.get("active") == 0 or "password_hash" in data:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    after = _require(conn, "users", user_id)
    audit(conn, user, "update", "users", user_id, before=_user_out(target), after=_user_out(after))
    conn.commit()
    return _user_out(after)


# ================================================================ reference data & settings

@app.get("/api/meta")
def meta(user=Depends(current_user), conn=Depends(get_db)):
    s = security.get_settings(conn)
    ai_on = s["ai_enabled"] and ai.claude_available()
    return {"interaction_types": INTERACTION_TYPES, "channels": CHANNELS,
            "insight_categories": INSIGHT_CATEGORIES, "owner_functions": OWNER_FUNCTIONS,
            "roles": security.ROLES, "ai_engine": "claude" if ai_on else "rules", "model": ai.MODEL,
            "require_change_reason": s["require_change_reason"]}


@app.get("/api/settings")
def get_settings(user=Depends(require("read")), conn=Depends(get_db)):
    return security.get_settings(conn)


@app.put("/api/settings")
def put_settings(patch: dict, user=Depends(require("admin")), conn=Depends(get_db)):
    try:
        clean = security.validate_settings(patch)
    except ValueError as e:
        raise HTTPException(422, str(e))
    before = security.get_settings(conn)
    security.save_settings(conn, clean)
    after = security.get_settings(conn)
    audit(conn, user, "update", "settings", None, before=before, after=after)
    conn.commit()
    return after


# ================================================================ KOLs

def _kol_data(body: KOLIn) -> dict:
    d = body.model_dump()
    d["consent_date"] = body.consent_date.isoformat() if body.consent_date else None
    return d


@app.get("/api/kols")
def list_kols(q: str | None = None, therapeutic_area: str | None = None, tier: int | None = None,
              user=Depends(require("read")), conn=Depends(get_db)):
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
    cfg = security.get_settings(conn)
    eng = {r["kol_id"]: r for r in stats.kol_engagement(kols, its, ins, cfg=cfg)}
    for k in kols:
        e = eng.get(k["id"], {})
        k.update({f: e.get(f) for f in ("engagement", "influence", "segment", "interactions",
                                          "last_interaction", "avg_sentiment")})
    return kols


@app.post("/api/kols", status_code=201)
def create_kol(body: KOLIn, user=Depends(require("kol.write")), conn=Depends(get_db)):
    return _insert(conn, "kols", _kol_data(body), user)


@app.get("/api/kols/{kol_id}")
def get_kol(kol_id: int, user=Depends(require("read")), conn=Depends(get_db)):
    k = _require(conn, "kols", kol_id)
    k["interactions"] = rows(conn.execute(
        "SELECT * FROM interactions WHERE kol_id = ? ORDER BY date DESC", (kol_id,)))
    k["insights"] = rows(conn.execute(
        "SELECT * FROM insights WHERE kol_id = ? ORDER BY created_at DESC", (kol_id,)))
    k["influence"] = stats.influence_score(k, security.get_settings(conn))
    return k


@app.put("/api/kols/{kol_id}")
def update_kol(kol_id: int, body: KOLIn, user=Depends(require("kol.write")),
               reason=Depends(change_reason), conn=Depends(get_db)):
    return _update(conn, "kols", kol_id, _kol_data(body), user, reason)


@app.delete("/api/kols/{kol_id}", status_code=204)
def delete_kol(kol_id: int, user=Depends(require("kol.delete")), reason=Depends(change_reason),
               conn=Depends(get_db)):
    _delete(conn, "kols", kol_id, user, reason)


@app.get("/api/kols/{kol_id}/export")
def export_kol(kol_id: int, user=Depends(require("data.export")), conn=Depends(get_db)):
    """Data-subject access export: everything held about one KOL."""
    k = _require(conn, "kols", kol_id)
    bundle = {
        "exported_at": iso(now_utc()),
        "kol": k,
        "interactions": rows(conn.execute("SELECT * FROM interactions WHERE kol_id = ?", (kol_id,))),
        "insights": rows(conn.execute("SELECT * FROM insights WHERE kol_id = ?", (kol_id,))),
        "pv_referrals": rows(conn.execute("SELECT * FROM pv_referrals WHERE kol_id = ?", (kol_id,))),
        "audit_log": rows(conn.execute(
            "SELECT id, ts, username, action, entity, entity_id, reason FROM audit_log "
            "WHERE entity = 'kols' AND entity_id = ? ORDER BY id", (kol_id,))),
    }
    audit(conn, user, "export", "kols", kol_id)
    conn.commit()
    return JSONResponse(bundle, headers={"Content-Disposition": f"attachment; filename=kol-{kol_id}.json"})


# ================================================================ Interactions

@app.get("/api/interactions")
def list_interactions(kol_id: int | None = None, type: str | None = None, since: date | None = None,
                      limit: int = Query(500, le=5000), user=Depends(require("read")), conn=Depends(get_db)):
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
def create_interaction(body: InteractionIn, user=Depends(require("interaction.write")), conn=Depends(get_db)):
    _require(conn, "kols", body.kol_id)
    data = body.model_dump()
    data["date"] = body.date.isoformat()
    created = _insert(conn, "interactions", data, user, commit=False)
    flags = ai.compliance_scan([created])
    referrals = _create_pv_referrals(conn, created, flags, user)
    conn.commit()
    # Immediate feedback for the MSL: suggested insight categories + compliance check.
    created["suggested_categories"] = ai.classify_text(f"{body.topics or ''} {body.notes}")
    created["compliance_flags"] = [f.model_dump() for f in flags]
    created["pv_referrals"] = referrals
    return created


@app.delete("/api/interactions/{interaction_id}", status_code=204)
def delete_interaction(interaction_id: int, user=Depends(require("interaction.delete")),
                       reason=Depends(change_reason), conn=Depends(get_db)):
    _delete(conn, "interactions", interaction_id, user, reason)


# ================================================================ Insights

@app.get("/api/insights")
def list_insights(category: str | None = None, therapeutic_area: str | None = None,
                  user=Depends(require("read")), conn=Depends(get_db)):
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
def create_insight(body: InsightIn, user=Depends(require("insight.write")), conn=Depends(get_db)):
    data = body.model_dump()
    if body.interaction_id:
        it = _require(conn, "interactions", body.interaction_id)
        data["kol_id"] = data["kol_id"] or it["kol_id"]
    if data["kol_id"]:
        k = _require(conn, "kols", data["kol_id"])
        data["therapeutic_area"] = data["therapeutic_area"] or k["therapeutic_area"]
    return _insert(conn, "insights", data, user)


@app.delete("/api/insights/{insight_id}", status_code=204)
def delete_insight(insight_id: int, user=Depends(require("insight.delete")),
                   reason=Depends(change_reason), conn=Depends(get_db)):
    _delete(conn, "insights", insight_id, user, reason)


# ================================================================ AI synthesis

@app.post("/api/ai/synthesize")
def synthesize(req: SynthesisRequest, user=Depends(require("ai.run")), conn=Depends(get_db)):
    # KOLs who withdrew consent are excluded from AI processing entirely.
    sql = ("SELECT i.* FROM interactions i JOIN kols k ON k.id = i.kol_id "
           "WHERE k.consent_status != 'withdrawn'")
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
    excluded = conn.execute("SELECT COUNT(*) FROM kols WHERE consent_status = 'withdrawn'").fetchone()[0]
    kols = {k["id"]: k for k in rows(conn.execute("SELECT * FROM kols"))}
    use_ai = req.use_ai and security.get_settings(conn)["ai_enabled"]
    out = ai.synthesize(its, kols, use_ai=use_ai)
    out["scope"] = {"interactions": len(its), "kols_excluded_no_consent": excluded,
                    **req.model_dump(mode="json")}
    audit(conn, user, "ai_synthesis", "interactions", None,
          after={"engine": out["engine"], "model": out.get("model"),
                 "interaction_ids": [i["id"] for i in its]})
    conn.commit()
    return out


@app.post("/api/ai/accept")
def accept_synthesis(payload: dict, user=Depends(require("ai.accept")), conn=Depends(get_db)):
    """Persist selected themes as insights, outcomes as tracked actions, AE flags as PV referrals."""
    created = {"insights": [], "actions": [], "pv_referrals": []}
    for t in payload.get("themes", []):
        ids = t.get("supporting_interaction_ids") or []
        first = _one(conn, "SELECT * FROM interactions WHERE id = ?", (ids[0],)) if ids else None
        k = _one(conn, "SELECT * FROM kols WHERE id = ?", (first["kol_id"],)) if first else None
        cat = t.get("category") if t.get("category") in INSIGHT_CATEGORIES else "clinical_practice"
        created["insights"].append(_insert(conn, "insights", {
            "kol_id": k["id"] if k else None, "interaction_id": first["id"] if first else None,
            "category": cat, "therapeutic_area": k["therapeutic_area"] if k else None,
            "text": f"{t.get('title')}: {t.get('summary')}", "source": "ai"}, user, commit=False))
    for o in payload.get("actionable_outcomes", []):
        created["actions"].append(_action_out(_insert(conn, "actions", {
            "title": o["title"], "rationale": o.get("rationale"),
            "owner_function": o.get("owner_function", "Medical Affairs"),
            "priority": o.get("priority") if o.get("priority") in ("low", "medium", "high") else "medium",
            "insight_ids": json.dumps([i["id"] for i in created["insights"]]), "source": "ai"},
            user, commit=False)))
    for f in payload.get("compliance_flags", []):
        it = _one(conn, "SELECT * FROM interactions WHERE id = ?", (f.get("interaction_id"),))
        if it and f.get("flag_type") == "potential_adverse_event":
            try:
                flag = ai.ComplianceFlag(**f)
            except ValueError:
                conn.rollback()
                raise HTTPException(422, f"Invalid compliance flag for interaction {it['id']}")
            created["pv_referrals"] += _create_pv_referrals(conn, it, [flag], user)
    conn.commit()
    return created


# ================================================================ Actions

@app.get("/api/actions")
def list_actions(status: str | None = None, user=Depends(require("read")), conn=Depends(get_db)):
    sql, params = "SELECT * FROM actions", []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += (" ORDER BY CASE status WHEN 'done' THEN 1 ELSE 0 END, "
            "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, due_date")
    return [_action_out(a) for a in rows(conn.execute(sql, params))]


@app.post("/api/actions", status_code=201)
def create_action(body: ActionIn, user=Depends(require("action.write")), conn=Depends(get_db)):
    data = body.model_dump()
    data["due_date"] = body.due_date.isoformat() if body.due_date else None
    data["insight_ids"] = json.dumps(body.insight_ids)
    return _action_out(_insert(conn, "actions", data, user))


@app.patch("/api/actions/{action_id}")
def patch_action(action_id: int, body: ActionPatch, user=Depends(require("action.status")),
                 conn=Depends(get_db)):
    data = {k: (v.isoformat() if isinstance(v, date) else v)
            for k, v in body.model_dump(exclude_unset=True).items()}
    if set(data) - {"status"} and not can(user["role"], "action.write"):
        raise HTTPException(403, "Only leads/admins can change priority or due date")
    return _action_out(_update(conn, "actions", action_id, data, user, None))


@app.delete("/api/actions/{action_id}", status_code=204)
def delete_action(action_id: int, user=Depends(require("action.write")), reason=Depends(change_reason),
                  conn=Depends(get_db)):
    _delete(conn, "actions", action_id, user, reason)


# ================================================================ Pharmacovigilance referrals

@app.get("/api/pv")
def list_pv(status: str | None = None, user=Depends(require("pv.read")), conn=Depends(get_db)):
    sql = ("SELECT p.*, k.name AS kol_name, i.date AS interaction_date, i.msl_owner FROM pv_referrals p "
           "LEFT JOIN kols k ON k.id = p.kol_id LEFT JOIN interactions i ON i.id = p.interaction_id")
    params = []
    if status:
        sql += " WHERE p.status = ?"
        params.append(status)
    out = rows(conn.execute(sql + " ORDER BY CASE p.status WHEN 'open' THEN 0 ELSE 1 END, p.due_at", params))
    now = iso(now_utc())
    for r in out:
        r["overdue"] = r["status"] == "open" and r["due_at"] < now
    return out


@app.patch("/api/pv/{ref_id}")
def resolve_pv(ref_id: int, body: PVPatch, user=Depends(require("pv.resolve")), conn=Depends(get_db)):
    if body.status == "reported" and not (body.pv_case_ref or "").strip():
        raise HTTPException(400, "A PV case reference is required when marking as reported")
    if body.status == "not_an_ae" and not (body.notes or "").strip():
        raise HTTPException(400, "A justification note is required when closing as not an AE")
    data = body.model_dump()
    if body.status == "open":
        data.update(resolved_by=None, resolved_at=None)
    else:
        data.update(resolved_by=user["id"], resolved_at=iso(now_utc()))
    return _update(conn, "pv_referrals", ref_id, data, user, body.notes)


# ================================================================ Audit trail

@app.get("/api/audit")
def list_audit(entity: str | None = None, entity_id: int | None = None, username: str | None = None,
               limit: int = Query(200, le=2000), user=Depends(require("audit.read")), conn=Depends(get_db)):
    sql, params = "SELECT * FROM audit_log WHERE 1=1", []
    for col, val in (("entity", entity), ("entity_id", entity_id), ("username", username)):
        if val is not None:
            sql += f" AND {col} = ?"
            params.append(val)
    return rows(conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)))


@app.get("/api/audit/verify")
def verify_audit(user=Depends(require("audit.read")), conn=Depends(get_db)):
    return security.verify_audit_chain(conn)


# ================================================================ Retention (admin)

@app.post("/api/admin/retention")
def retention_purge(req: RetentionRequest, user=Depends(require("admin")), conn=Depends(get_db)):
    days = req.older_than_days or security.get_settings(conn)["retention_days"]
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    ids = [r[0] for r in conn.execute("SELECT id FROM interactions WHERE date < ?", (cutoff,))]
    n_ins = conn.execute(f"SELECT COUNT(*) FROM insights WHERE interaction_id IN ({','.join('?' * len(ids))})",
                         ids).fetchone()[0] if ids else 0
    result = {"cutoff": cutoff, "interactions": len(ids), "linked_insights": n_ins, "dry_run": req.dry_run}
    if not req.dry_run and ids:
        qs = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM insights WHERE interaction_id IN ({qs})", ids)
        # PV referrals are kept (regulatory retention); their interaction link is nulled by the FK.
        conn.execute(f"DELETE FROM interactions WHERE id IN ({qs})", ids)
        audit(conn, user, "retention_purge", "interactions", None, after={**result, "interaction_ids": ids},
              reason=f"Retention policy: older than {days} days")
        conn.commit()
    return result


# ================================================================ Analytics & exports

def _all(conn):
    return (rows(conn.execute("SELECT * FROM kols")),
            rows(conn.execute("SELECT * FROM interactions")),
            rows(conn.execute("SELECT * FROM insights")),
            rows(conn.execute("SELECT * FROM actions")))


@app.get("/api/analytics")
def analytics(user=Depends(require("read")), conn=Depends(get_db)):
    pv = rows(conn.execute("SELECT status, due_at FROM pv_referrals"))
    return stats.full_report(*_all(conn), cfg=security.get_settings(conn), pv=pv)


@app.get("/api/export/{table}.csv")
def export_csv(table: str, user=Depends(require("csv.export")), conn=Depends(get_db)):
    if table == "engagement":
        k, i, s, _ = _all(conn)
        data = stats.kol_engagement(k, i, s, cfg=security.get_settings(conn))
    elif table in ("kols", "interactions", "insights", "actions", "pv_referrals"):
        data = rows(conn.execute(f"SELECT * FROM {table}"))
    else:
        raise HTTPException(404, "unknown table")
    audit(conn, user, "export_csv", table, None, after={"rows": len(data)})
    conn.commit()
    buf = io.StringIO()
    if data:
        w = csv.DictWriter(buf, fieldnames=list(data[0]))
        w.writeheader()
        w.writerows(data)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={table}.csv"})


# ================================================================ UI

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
