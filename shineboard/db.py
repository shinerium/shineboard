# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

"""The SQLite database: its schema, its connections and its transactions.

One process writes, in WAL mode, on the server's own disk; every request opens a
connection of its own, and a write takes the database with BEGIN IMMEDIATE, so
that the sessions' writes queue instead of failing.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3
from collections.abc import Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    name          TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    last_brief_at TEXT
);

-- A message to one or more sessions, or to all. Its status is the parts:
-- one for each recipient, which that recipient keeps.
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    day        TEXT NOT NULL,             -- the message's date, YYYY-MM-DD (in day_zone)
    posted_at  TEXT NOT NULL,             -- UTC, ISO 8601
    sender     TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    reply_to   INTEGER REFERENCES messages(id),
    source     TEXT NOT NULL DEFAULT 'mcp',  -- mcp, import:board, import:archive
    import_key TEXT UNIQUE,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_day ON messages(day);
CREATE INDEX IF NOT EXISTS messages_sender ON messages(sender);

CREATE TABLE IF NOT EXISTS parts (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    session    TEXT NOT NULL,             -- a session's name, or 'all'
    state      TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (message_id, session)
);
CREATE INDEX IF NOT EXISTS parts_session_state ON parts(session, state);

CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY,
    ref        TEXT UNIQUE,               -- a work item's ID, such as COM-12 or FHS-172
    area       TEXT NOT NULL,             -- the session that owns it
    title      TEXT NOT NULL,
    status     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    message_id INTEGER REFERENCES messages(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_area_status ON tasks(area, status);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,   -- a short name, such as security-contact
    title         TEXT NOT NULL,
    decision      TEXT NOT NULL,
    decided_on    TEXT NOT NULL,          -- YYYY-MM-DD
    decided_by    TEXT NOT NULL DEFAULT 'user',
    areas         TEXT NOT NULL DEFAULT '',
    home          TEXT NOT NULL DEFAULT '',   -- the doc and section that record it
    status        TEXT NOT NULL DEFAULT 'in-force',
    superseded_by TEXT,
    recorded_by   TEXT NOT NULL,
    recorded_at   TEXT NOT NULL
);

-- Full-text search over messages (with their parts' notes), tasks and decisions.
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    kind UNINDEXED, ref UNINDEXED, title, body,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY,
    at     TEXT NOT NULL,
    actor  TEXT NOT NULL,
    client TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);

-- OAuth: registered clients, authorizations waiting for the owner's login,
-- codes and tokens (only their SHA-256 is kept).
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id  TEXT PRIMARY KEY,
    info       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used  TEXT
);
CREATE TABLE IF NOT EXISTS oauth_pending (
    id         TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    params     TEXT NOT NULL,
    csrf       TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash  TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,             -- access, refresh, static
    client_id  TEXT NOT NULL,
    scopes     TEXT NOT NULL,
    family     TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    expires_at INTEGER,
    created_at TEXT NOT NULL,
    last_used  TEXT
);
CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family);

CREATE TABLE IF NOT EXISTS login_failures (
    ip TEXT NOT NULL,
    at INTEGER NOT NULL
);
"""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextlib.contextmanager
def opened(path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def write(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction: BEGIN IMMEDIATE takes the database at once, so a
    second writer waits for it (busy_timeout) instead of failing midway."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init(conn: sqlite3.Connection, sessions: list[str]) -> None:
    """Creates the schema (or checks it), WAL mode and the sessions."""
    conn.execute("PRAGMA journal_mode = WAL")
    with write(conn):
        for statement in _statements(SCHEMA):
            conn.execute(statement)
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        elif int(row["value"]) > SCHEMA_VERSION:
            raise RuntimeError(f"the database's schema is version {row['value']}, newer than this ShineBoard's {SCHEMA_VERSION}")
        for name in sessions:
            conn.execute("INSERT OR IGNORE INTO sessions(name) VALUES (?)", (name,))


def _statements(script: str) -> list[str]:
    """The script's statements, split where sqlite3.complete_statement says one ends."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        if not buf and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError("the schema ends in an incomplete statement")
    return out


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
