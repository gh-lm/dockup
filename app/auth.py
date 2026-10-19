"""Login, first-run setup, rate limiting and optional TOTP two-factor."""

import io
import secrets

import pyotp
import segno
from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import activity, config, db

bp = Blueprint("auth", __name__)


def user_count():
    return db.get().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return db.get().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def client_ip():
    # Never trust X-Forwarded-For here: it's client-supplied and this is
    # the one thing login lockout is keyed on (ip, username) - honoring
    # it let anyone bypass the 5-attempts lockout by sending a fresh
    # fake IP on every request. Dockup sits directly behind Tailscale
    # Serve on loopback with no configurable trusted-proxy chain, so
    # the real connecting address is always what matters.
    return request.remote_addr or "?"


def _too_many_failures(ip, username):
    row = db.get().execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ip=? AND username=? AND success=0 "
        "AND ts > datetime('now', ?)",
        (ip, username, f"-{config.LOGIN_WINDOW_MIN} minutes"),
    ).fetchone()
    return row[0] >= config.LOGIN_MAX_FAILS


def _record_attempt(ip, username, success):
    con = db.get()
    with con:
        con.execute("INSERT INTO login_attempts(ip, username, success) VALUES(?,?,?)",
                    (ip, username, 1 if success else 0))
        con.execute("DELETE FROM login_attempts WHERE ts < datetime('now', '-1 day')")


# -- first run ----------------------------------------------------------


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if user_count() > 0:
        return redirect(url_for("auth.login"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if len(username) < 3:
            error = "Pick a username of at least 3 characters."
        elif len(password) < 12:
            error = "Use a password of at least 12 characters."
        elif password != confirm:
            error = "The two passwords don't match."
        else:
            con = db.get()
            with con:
                con.execute("INSERT INTO users(username, pw_hash) VALUES(?,?)",
                            (username, generate_password_hash(password)))
            activity.log("info", "auth", f"Admin account '{username}' created")
            session.clear()
            session["uid"] = con.execute("SELECT id FROM users WHERE username=?",
                                         (username,)).fetchone()[0]
            session["csrf"] = secrets.token_urlsafe(32)
            session.permanent = True
            return redirect(url_for("views.index"))
    return render_template("setup.html", error=error)


# -- login / logout -----------------------------------------------------


@bp.route("/login", methods=["GET", "POST"])
def login():
    if user_count() == 0:
        return redirect(url_for("auth.setup"))
    if session.get("uid"):
        return redirect(url_for("views.index"))
    error = None
    show_totp = False
    if request.method == "POST":
        ip = client_ip()
        # second step: TOTP code
        if session.get("pending_uid"):
            user = db.get().execute("SELECT * FROM users WHERE id=?",
                                    (session["pending_uid"],)).fetchone()
            code = (request.form.get("code") or "").replace(" ", "")
            # The code step shares the password step's lockout: a
            # six-digit code is small enough to guess by brute force
            # if wrong answers cost nothing, and someone at this step
            # already holds the password.
            if user and _too_many_failures(ip, user["username"]):
                session.pop("pending_uid", None)
                activity.log("error", "auth",
                             f"Login locked out for '{user['username']}' from {ip}",
                             "Too many wrong two-factor codes in 15 minutes.")
                error = "Too many failed attempts. Wait 15 minutes and try again."
            elif user and user["totp_enabled"] and pyotp.TOTP(user["totp_secret"]).verify(code, valid_window=1):
                session.clear()
                session["uid"] = user["id"]
                session["csrf"] = secrets.token_urlsafe(32)
                session.permanent = True
                _record_attempt(ip, user["username"], True)
                return redirect(url_for("views.index"))
            else:
                if user:
                    _record_attempt(ip, user["username"], False)
                error = "That code wasn't right. Try again."
                show_totp = True
        else:
            # Capped so a script can't bloat the attempts table or the
            # activity log with kilobyte-long made-up usernames.
            username = (request.form.get("username") or "").strip()[:64]
            password = request.form.get("password") or ""
            if _too_many_failures(ip, username):
                activity.log("error", "auth",
                             f"Login locked out for '{username}' from {ip}",
                             "Too many failed attempts in 15 minutes.")
                error = "Too many failed attempts. Wait 15 minutes and try again."
            else:
                user = db.get().execute("SELECT * FROM users WHERE username=?",
                                        (username,)).fetchone()
                if user and check_password_hash(user["pw_hash"], password):
                    if user["totp_enabled"]:
                        session["pending_uid"] = user["id"]
                        show_totp = True
                    else:
                        session.clear()
                        session["uid"] = user["id"]
                        session["csrf"] = secrets.token_urlsafe(32)
                        session.permanent = True
                        _record_attempt(ip, username, True)
                        return redirect(url_for("views.index"))
                else:
                    _record_attempt(ip, username, False)
                    error = "Wrong username or password."
    return render_template("login.html", error=error, show_totp=show_totp)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


# -- account / 2FA API (behind login) -----------------------------------


@bp.post("/api/account/password")
def change_password():
    user = current_user()
    data = request.get_json(force=True)
    if not check_password_hash(user["pw_hash"], data.get("current", "")):
        return jsonify({"error": "Your current password wasn't right."}), 400
    new = data.get("new", "")
    if len(new) < 12:
        return jsonify({"error": "Use a password of at least 12 characters."}), 400
    con = db.get()
    with con:
        con.execute("UPDATE users SET pw_hash=? WHERE id=?",
                    (generate_password_hash(new), user["id"]))
    activity.log("info", "auth", "Password changed")
    return jsonify({"ok": True})


@bp.post("/api/2fa/begin")
def totp_begin():
    user = current_user()
    secret = pyotp.random_base32()
    session["totp_setup_secret"] = secret
    uri = pyotp.TOTP(secret).provisioning_uri(name=user["username"], issuer_name="Dockup")
    buf = io.BytesIO()
    segno.make(uri).save(buf, kind="svg", scale=4, dark="#111827", light=None)
    return jsonify({"secret": secret, "qr_svg": buf.getvalue().decode()})


@bp.post("/api/2fa/enable")
def totp_enable():
    user = current_user()
    secret = session.get("totp_setup_secret")
    code = (request.get_json(force=True).get("code") or "").replace(" ", "")
    if not secret or not pyotp.TOTP(secret).verify(code, valid_window=1):
        return jsonify({"error": "That code wasn't right - check your authenticator app."}), 400
    con = db.get()
    with con:
        con.execute("UPDATE users SET totp_secret=?, totp_enabled=1 WHERE id=?",
                    (secret, user["id"]))
    session.pop("totp_setup_secret", None)
    activity.log("info", "auth", "Two-factor authentication switched on")
    return jsonify({"ok": True})


@bp.post("/api/2fa/disable")
def totp_disable():
    user = current_user()
    code = (request.get_json(force=True).get("code") or "").replace(" ", "")
    if not user["totp_enabled"] or not pyotp.TOTP(user["totp_secret"]).verify(code, valid_window=1):
        return jsonify({"error": "That code wasn't right."}), 400
    con = db.get()
    with con:
        con.execute("UPDATE users SET totp_secret=NULL, totp_enabled=0 WHERE id=?", (user["id"],))
    activity.log("info", "auth", "Two-factor authentication switched off")
    return jsonify({"ok": True})
