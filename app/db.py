"""SQLite persistence layer for the Medical Affairs CRM."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "crm.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    specialty TEXT NOT NULL,
    institution TEXT,
    region TEXT,
    therapeutic_area TEXT NOT NULL,
    tier INTEGER NOT NULL DEFAULT 2 CHECK (tier BETWEEN 1 AND 3),
    publications INTEGER NOT NULL DEFAULT 0,
    trials_involved INTEGER NOT NULL DEFAULT 0,
    email TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kol_id INTEGER NOT NULL REFERENCES kols(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'in_person',
    msl_owner TEXT,
    topics TEXT,
    notes TEXT NOT NULL DEFAULT '',
    sentiment REAL CHECK (sentiment IS NULL OR sentiment BETWEEN -1 AND 1),
    duration_min INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kol_id INTEGER REFERENCES kols(id) ON DELETE SET NULL,
    interaction_id INTEGER REFERENCES interactions(id) ON DELETE SET NULL,
    category TEXT NOT NULL,
    therapeutic_area TEXT,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    rationale TEXT,
    owner_function TEXT NOT NULL DEFAULT 'Medical Affairs',
    priority TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('low','medium','high')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','done')),
    due_date TEXT,
    insight_ids TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_interactions_kol ON interactions(kol_id);
CREATE INDEX IF NOT EXISTS idx_interactions_date ON interactions(date);
CREATE INDEX IF NOT EXISTS idx_insights_kol ON insights(kol_id);
"""


def db_path() -> str:
    return os.environ.get("CRM_DB_PATH", str(DEFAULT_DB_PATH))


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]
