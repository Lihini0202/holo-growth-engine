"""SQLite storage for runs, leads, consent, review queue, drafts and GEO results.

SQLite keeps the project zero-setup. The schema is plain SQL, so moving it to
Postgres is a connection-string change plus swapping AUTOINCREMENT for IDENTITY.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,              -- leads | retention | content | geo
    label         TEXT,                       -- event name, campaign, etc.
    input_hash    TEXT,                       -- sha256 of the input file, for idempotency
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',  -- running | ok | partial | failed
    stats_json    TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    email         TEXT PRIMARY KEY,
    first_name    TEXT,
    last_name     TEXT,
    phone         TEXT,
    organisation  TEXT,
    city          TEXT,
    age           INTEGER,
    source_event  TEXT,
    persona       TEXT,
    intent        TEXT,
    language      TEXT,
    enrichment_source TEXT,
    confidence    REAL,
    score         INTEGER,
    needs_review  INTEGER NOT NULL DEFAULT 0,
    first_seen_run INTEGER REFERENCES runs(id),
    last_seen_run  INTEGER REFERENCES runs(id),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only: the current state is the latest row per (email, purpose).
CREATE TABLE IF NOT EXISTS consent_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL,
    purpose    TEXT NOT NULL,                 -- marketing_email | product_updates
    granted    INTEGER NOT NULL,              -- 1 granted, 0 withdrawn
    source     TEXT NOT NULL,                 -- event form, in-app toggle, unsubscribe link
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_consent_email ON consent_events(email, purpose, id);

CREATE TABLE IF NOT EXISTS review_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,                 -- enrichment | content
    ref        TEXT NOT NULL,                 -- email or draft id
    reason     TEXT NOT NULL,
    payload    TEXT,
    status     TEXT NOT NULL DEFAULT 'open',  -- open | approved | rejected
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS row_issues (
    run_id     INTEGER NOT NULL REFERENCES runs(id),
    row_number INTEGER,
    field      TEXT,
    problem    TEXT,
    value      TEXT
);

CREATE TABLE IF NOT EXISTS lifecycle (
    holo_user_id TEXT PRIMARY KEY,
    email        TEXT,
    stage        TEXT NOT NULL,
    analyses_14d INTEGER,
    analyses_total INTEGER,
    pending_validations INTEGER,
    last_active  TEXT,
    synced_run   INTEGER REFERENCES runs(id),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS drafts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES runs(id),
    segment    TEXT NOT NULL,
    language   TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    status     TEXT NOT NULL,                 -- passed | needs_review
    findings   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS geo_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES runs(id),
    provider   TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    mentioned  INTEGER NOT NULL,
    position   INTEGER,                       -- 1 = first brand named, NULL = absent
    competitors TEXT,
    answer_excerpt TEXT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def start_run(conn: sqlite3.Connection, kind: str, label: str, input_hash: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO runs (kind, label, input_hash) VALUES (?, ?, ?)", (kind, label, input_hash)
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, stats_json: str) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, stats_json = ?, finished_at = datetime('now') WHERE id = ?",
        (status, stats_json, run_id),
    )
    conn.commit()


def previous_successful_run(conn: sqlite3.Connection, kind: str, input_hash: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM runs WHERE kind = ? AND input_hash = ? AND status IN ('ok', 'partial') "
        "ORDER BY id DESC LIMIT 1",
        (kind, input_hash),
    ).fetchone()
    return int(row["id"]) if row else None
