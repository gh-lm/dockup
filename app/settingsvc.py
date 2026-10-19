"""Application settings stored in SQLite; secret values encrypted at rest.

A secret is never sent back to the browser - the API returns a mask, and
saving the mask (or an empty string) keeps the stored value untouched.
"""

from . import crypto, db

MASK = "••••••••"

# key -> (default, is_secret)
SCHEMA = {
    "runtime.engine": ("docker", False),
    "runtime.socket": ("/var/run/docker.sock", False),
    "smtp.host": ("", False),
    "smtp.port": ("587", False),
    "smtp.security": ("starttls", False),       # starttls | tls | none
    "smtp.username": ("", False),
    "smtp.password": ("", True),
    "smtp.from": ("", False),
    "alerts.email_to": ("", False),
    "alerts.on_error": ("1", False),
    "ui.accent": ("", False),                   # empty = brand yellow
    "backup.hour": ("3", False),                # daily backup hour, local time
    "backup.retention_days": ("14", False),
    # Dumps written before the nightly off-machine copy, as JSON:
    # [{"name": "...", "container": "...", "command": [...], "file": "..."}]
    "predump.jobs": ("[]", False),
    "predump.hour": ("2", False),
    "adopt.exclude_paths": ("", False),          # comma-separated path prefixes to leave alone
    "onboarding.bulk_adopt_offered": ("0", False),
}


def get(key: str) -> str:
    default, _is_secret = SCHEMA[key]
    row = db.get().execute("SELECT value, encrypted FROM settings WHERE key=?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return default
    return crypto.decrypt(row["value"]) if row["encrypted"] else row["value"]


def get_many(keys) -> dict:
    return {k: get(k) for k in keys}


def set_many(values: dict):
    con = db.get()
    with con:
        for key, value in values.items():
            if key not in SCHEMA:
                continue
            _default, is_secret = SCHEMA[key]
            value = "" if value is None else str(value).strip()
            if is_secret:
                if value == "" or value == MASK:
                    continue  # keep what's there
                con.execute(
                    "INSERT INTO settings(key,value,encrypted) VALUES(?,?,1) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, encrypted=1",
                    (key, crypto.encrypt(value)),
                )
            else:
                con.execute(
                    "INSERT INTO settings(key,value,encrypted) VALUES(?,?,0) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, encrypted=0",
                    (key, value),
                )


def public_view() -> dict:
    """Everything the settings screen shows; secrets masked, never revealed."""
    out = {}
    for key, (_default, is_secret) in SCHEMA.items():
        if is_secret:
            out[key] = MASK if get(key) else ""
        else:
            out[key] = get(key)
    return out


def smtp_configured() -> bool:
    return bool(get("smtp.host") and get("smtp.from") and get("alerts.email_to"))
