import sqlite3
from flask import g

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    totp_secret TEXT,
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    encrypted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(ts DESC);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    ip TEXT NOT NULL,
    username TEXT NOT NULL,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts ON login_attempts(ip, username, ts);
CREATE TABLE IF NOT EXISTS stack_updates (
    name TEXT PRIMARY KEY,
    available INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS registries (
    id INTEGER PRIMARY KEY,
    host TEXT UNIQUE NOT NULL,
    username TEXT NOT NULL,
    token_enc TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect():
    con = sqlite3.connect(config.DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init():
    con = connect()
    with con:
        con.executescript(SCHEMA)
        _rename_old_activity(con)
    con.close()


def _rename_old_activity(con):
    """One-off tidy-up after the rename to Dockup: activity rows written
    under the old name still say it, so a log going back before the
    rename reads as though it belongs to a different program. The
    category those rows are filed under says it too, and the Activity
    page shows that. Only the app's own name is touched - a stack called after the old name, or a
    quoted error from Docker, is left exactly as it was, since those are
    a record of what really happened. Costs one UPDATE on a table with a
    5000-row cap, and finds nothing on every start after the first."""
    con.execute(
        "UPDATE activity SET category = replace(category, 'dockle', 'dockup') "
        "WHERE category LIKE 'dockle%'")
    for column in ("message", "detail"):
        con.execute(
            f"UPDATE activity SET {column} = replace(replace(replace({column},"
            f" 'Dockle', 'Dockup'),"
            f" 'dockle-companion', 'dockup-companion'),"
            f" 'lightmorphic/dockle', 'lightmorphic/dockup') "
            f"WHERE {column} LIKE '%Dockle%'"
            f"   OR {column} LIKE '%dockle-companion%'"
            f"   OR {column} LIKE '%lightmorphic/dockle%'")


def get():
    if "db" not in g:
        g.db = connect()
    return g.db


def close(_exc=None):
    con = g.pop("db", None)
    if con is not None:
        con.close()
