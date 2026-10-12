"""Authentication, role-based access control, audit trail and admin settings."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

ROLES = ["admin", "lead", "msl", "viewer"]
SESSION_HOURS = 12

# permission -> roles allowed. Reading core CRM data is allowed for every authenticated role.
PERMISSIONS: dict[str, set[str]] = {
    "read":             {"admin", "lead", "msl", "viewer"},
    "kol.write":        {"admin", "lead", "msl"},
    "kol.delete":       {"admin"},
    "interaction.write": {"admin", "lead", "msl"},
    "interaction.delete": {"admin", "lead"},
    "insight.write":    {"admin", "lead", "msl"},
    "insight.delete":   {"admin", "lead"},
    "ai.run":           {"admin", "lead", "msl"},
    "ai.accept":        {"admin", "lead"},
    "action.write":     {"admin", "lead"},
    "action.status":    {"admin", "lead", "msl"},
    "pv.read":          {"admin", "lead", "msl"},
    "pv.resolve":       {"admin", "lead"},
    "audit.read":       {"admin", "lead"},
    "data.export":      {"admin"},
    "csv.export":       {"admin", "lead"},
    "admin":            {"admin"},
}


def can(role: str, perm: str) -> bool:
    return role in PERMISSIONS.get(perm, set())


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ passwords & sessions

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1)
    return hmac.compare_digest(dk.hex(), dk_hex)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires = iso(now_utc() + timedelta(hours=SESSION_HOURS))
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(now_utc()),))
    conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?,?,?)",
                 (_token_hash(token), user_id, expires))
    conn.commit()
    return token, expires


def user_for_token(conn: sqlite3.Connection, token: str) -> dict | None:
    r = conn.execute(
        "SELECT u.id, u.username, u.full_name, u.role, u.active FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at >= ?",
        (_token_hash(token), iso(now_utc()))).fetchone()
    if not r or not r["active"]:
        return None
    return dict(r)


def end_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    conn.commit()


def create_user(conn, username: str, password: str, role: str, full_name: str | None = None) -> int:
    if role not in ROLES:
        raise ValueError(f"invalid role {role}")
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    cur = conn.execute("INSERT INTO users (username, full_name, role, password_hash) VALUES (?,?,?,?)",
                       (username, full_name, role, hash_password(password)))
    conn.commit()
    return cur.lastrowid


def bootstrap_admin(conn: sqlite3.Connection) -> None:
    """Ensure at least one admin exists on a fresh database."""
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return
    username = os.environ.get("CRM_ADMIN_USER", "admin")
    password = os.environ.get("CRM_ADMIN_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(12)
        log.warning("Created initial admin user %r with generated password: %s "
                    "(set CRM_ADMIN_PASSWORD to choose one; change it after first login)", username, password)
        print(f"[CRM] Initial admin user '{username}' password: {password}", flush=True)
    create_user(conn, username, password, "admin", "Administrator")


# ------------------------------------------------------------------ audit trail

GENESIS = "0" * 64


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def _record_hash(prev_hash: str, record: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(record)).encode()).hexdigest()


def audit(conn: sqlite3.Connection, user: dict | None, action: str, entity: str, entity_id: int | None,
          before: dict | None = None, after: dict | None = None, reason: str | None = None) -> None:
    """Append an audit record. Call BEFORE conn.commit() so it is atomic with the change."""
    last = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev = last["hash"] if last else GENESIS
    record = {
        "ts": iso(now_utc()),
        "user_id": user["id"] if user else None,
        "username": user["username"] if user else "system",
        "action": action,
        "entity": entity,
        "entity_id": entity_id,
        "before_json": _canonical(before) if before is not None else None,
        "after_json": _canonical(after) if after is not None else None,
        "reason": reason,
    }
    h = _record_hash(prev, record)
    conn.execute(
        "INSERT INTO audit_log (ts, user_id, username, action, entity, entity_id, before_json, after_json,"
        " reason, prev_hash, hash) VALUES (:ts,:user_id,:username,:action,:entity,:entity_id,:before_json,"
        ":after_json,:reason,:prev_hash,:hash)", {**record, "prev_hash": prev, "hash": h})


def verify_audit_chain(conn: sqlite3.Connection) -> dict:
    prev = GENESIS
    n = 0
    for r in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        r = dict(r)
        record = {k: r[k] for k in ("ts", "user_id", "username", "action", "entity", "entity_id",
                                    "before_json", "after_json", "reason")}
        if r["prev_hash"] != prev or _record_hash(prev, record) != r["hash"]:
            return {"valid": False, "records": n, "first_invalid_id": r["id"]}
        prev = r["hash"]
        n += 1
    return {"valid": True, "records": n, "first_invalid_id": None}


# ------------------------------------------------------------------ settings

DEFAULT_SETTINGS: dict = {
    "type_weights": {
        "advisory_board": 3.0, "investigator_meeting": 2.5, "one_to_one": 2.0,
        "speaker_program": 1.5, "congress": 1.0, "medical_info_request": 1.0,
    },
    "recency_half_life_days": 90,
    "influence_weights": {"publications": 0.45, "trials": 0.30, "tier": 0.25},
    "pv_sla_hours": 24,
    "retention_days": 3650,
    "require_change_reason": True,
    "ai_enabled": True,
}


def get_settings(conn: sqlite3.Connection) -> dict:
    s = copy.deepcopy(DEFAULT_SETTINGS)
    for r in conn.execute("SELECT key, value FROM settings"):
        if r["key"] in s:
            s[r["key"]] = json.loads(r["value"])
    return s


def validate_settings(patch: dict) -> dict:
    out = {}
    for k, v in patch.items():
        if k not in DEFAULT_SETTINGS:
            raise ValueError(f"unknown setting {k}")
        d = DEFAULT_SETTINGS[k]
        if isinstance(d, dict):
            if not isinstance(v, dict) or set(v) != set(d):
                raise ValueError(f"{k} must have keys {sorted(d)}")
            if any(not isinstance(x, (int, float)) or x < 0 for x in v.values()):
                raise ValueError(f"{k} values must be non-negative numbers")
            if k == "influence_weights" and abs(sum(v.values()) - 1) > 1e-6:
                raise ValueError("influence_weights must sum to 1")
        elif isinstance(d, bool):
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true/false")
        elif not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"{k} must be a positive number")
        out[k] = v
    return out


def save_settings(conn: sqlite3.Connection, patch: dict) -> None:
    for k, v in patch.items():
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, json.dumps(v)))
