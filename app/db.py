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
        _encrypt_old_totp_secrets(con)
    con.close()


def _encrypt_old_totp_secrets(con):
    """Two-factor secrets used to be stored as plain text. Encrypt any that
    still are; an encrypted one starts with the Fernet marker and is left
    alone, so this finds nothing on every start after the first."""
    from . import crypto
    rows = con.execute("SELECT id, totp_secret FROM users WHERE totp_secret IS NOT NULL "
                       "AND totp_secret NOT LIKE 'gAAAA%'").fetchall()
    for row in rows:
        con.execute("UPDATE users SET totp_secret=? WHERE id=?",
                    (crypto.encrypt(row["totp_secret"]), row["id"]))


def get():
    if "db" not in g:
        g.db = connect()
    return g.db


def close(_exc=None):
    con = g.pop("db", None)
    if con is not None:
        con.close()
