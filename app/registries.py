"""Private container registries: the credentials Dockup pulls with.

Each registry is a host, a username and a token. The token is encrypted
at rest in the database the same way the SMTP password is, and is never
sent back to the browser once saved.

Docker itself only reads credentials from a config.json, so the
decrypted tokens have to exist in one somewhere. Dockup writes that file
into config.DOCKER_CONFIG_DIR - a private directory in the container's
own /tmp, not the data volume - and points DOCKER_CONFIG at it for every
docker and compose call (see runtime.Runtime._env). It is rebuilt from
the database at every start and after every change, so it never outlives
the container, never lands in a backup, and is never the only copy.
"""

import base64
import json
import logging
import os
import re
import tempfile

from . import activity, config, crypto, db

# A registry host is a hostname with an optional port, as docker writes
# it in an image reference: docker.io, registry.example.com:5000,
# registry.example.com:5000. No scheme, no path, no user info.
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?$")


def normalise_host(raw: str) -> str:
    """Accept what people paste - a URL, an image reference, a trailing
    slash - and reduce it to the bare host[:port] docker keys on."""
    host = (raw or "").strip().lower()
    host = re.sub(r"^[a-z]+://", "", host)
    host = host.split("/", 1)[0]
    if not _HOST_RE.match(host) or len(host) > 253:
        raise ValueError("That isn't a registry address. Use the host, with its port if it has "
                         "one - for example registry.example.com:5000.")
    return host


def list_public() -> list[dict]:
    rows = db.get().execute(
        "SELECT id, host, username, token_enc FROM registries ORDER BY host").fetchall()
    return [{"id": r["id"], "host": r["host"], "username": r["username"],
             "hasToken": bool(r["token_enc"])} for r in rows]


def stored_token(host: str) -> str:
    row = db.get().execute("SELECT token_enc FROM registries WHERE host=?", (host,)).fetchone()
    return crypto.decrypt(row["token_enc"]) if row and row["token_enc"] else ""


def save(host: str, username: str, token: str) -> None:
    """Add a registry, or update one already saved for this host. A blank
    token keeps the saved one, so the username can be corrected without
    retyping the secret."""
    host = normalise_host(host)
    username = (username or "").strip()
    if not username:
        raise ValueError("A username is needed.")
    token = (token or "").strip()
    con = db.get()
    existing = con.execute("SELECT id FROM registries WHERE host=?", (host,)).fetchone()
    if existing is None and not token:
        raise ValueError("A token is needed.")
    with con:
        if existing is None:
            con.execute("INSERT INTO registries(host, username, token_enc) VALUES(?,?,?)",
                        (host, username, crypto.encrypt(token)))
        elif token:
            con.execute("UPDATE registries SET username=?, token_enc=? WHERE id=?",
                        (username, crypto.encrypt(token), existing["id"]))
        else:
            con.execute("UPDATE registries SET username=? WHERE id=?", (username, existing["id"]))
    write_docker_config()


def delete(registry_id: int) -> str | None:
    con = db.get()
    row = con.execute("SELECT host FROM registries WHERE id=?", (registry_id,)).fetchone()
    if row is None:
        return None
    with con:
        con.execute("DELETE FROM registries WHERE id=?", (registry_id,))
    write_docker_config()
    return row["host"]


def _say_unreadable(host: str) -> None:
    """A saved token that cannot be decrypted (the secret key was changed)
    would otherwise show up only as an unrelated pull failure."""
    message = f"The saved token for {host} could not be read. Enter it again in Settings."
    try:
        activity.log("warning", "registry", message,
                     "The secret key is not the one the token was saved with.")
    except Exception:  # the log is not ready yet, as at start-up
        logging.getLogger("dockup").warning(message)


def docker_config(rows) -> dict:
    auths = {}
    for r in rows:
        token = crypto.decrypt(r["token_enc"]) if r["token_enc"] else ""
        if not token:
            if r["token_enc"]:
                _say_unreadable(r["host"])
            continue  # undecryptable after a SECRET_KEY change - skip, don't write junk
        pair = f"{r['username']}:{token}".encode()
        auths[r["host"]] = {"auth": base64.b64encode(pair).decode()}
    return {"auths": auths}


def write_docker_config(con=None) -> None:
    """Rebuild config.json from the database. Written to a temporary file
    and renamed into place, so a docker call running at the same moment
    reads either the old file or the new one, never half of either."""
    con = con or db.get()
    rows = con.execute("SELECT host, username, token_enc FROM registries").fetchall()
    d = config.DOCKER_CONFIG_DIR
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(docker_config(rows), fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, d / "config.json")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_at_startup() -> None:
    con = db.connect()
    try:
        write_docker_config(con)
    finally:
        con.close()
