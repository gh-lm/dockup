"""Persistent activity log. Errors are kept, highlighted in the UI, and
optionally emailed - and if email isn't fully set up, the failure to send
is itself recorded so nothing silently looks like it worked.
"""

import html
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from . import db, settingsvc

_email_lock = threading.Lock()
_last_email_at = 0.0
EMAIL_THROTTLE_SECONDS = 300  # at most one alert email per 5 minutes

_LOGO_PATH = Path(__file__).parent / "static" / "icons" / "dockup-email-96.png"
_LOGO_CID = "dockup-logo"


def log(level: str, category: str, message: str, detail: str = ""):
    con = db.connect()  # own connection: callable from worker threads
    try:
        with con:
            con.execute(
                "INSERT INTO activity(level, category, message, detail) VALUES(?,?,?,?)",
                (level, category, message, detail or None),
            )
            con.execute(
                "DELETE FROM activity WHERE id NOT IN "
                "(SELECT id FROM activity ORDER BY id DESC LIMIT 5000)"
            )
    finally:
        con.close()
    if level == "error":
        threading.Thread(target=_maybe_email_error, args=(category, message, detail), daemon=True).start()


def recent(limit=200, errors_only=False):
    q = "SELECT id, datetime(ts, 'localtime') AS ts, level, category, message, detail FROM activity"
    if errors_only:
        q += " WHERE level='error'"
    q += " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in db.get().execute(q, (limit,)).fetchall()]


def _maybe_email_error(category, message, detail):
    global _last_email_at
    try:
        con = db.connect()
        try:
            # read settings on our own connection (no flask context here)
            def s(key):
                default, _sec = settingsvc.SCHEMA[key]
                row = con.execute("SELECT value, encrypted FROM settings WHERE key=?", (key,)).fetchone()
                if row is None or row[0] is None:
                    return default
                from . import crypto
                return crypto.decrypt(row[0]) if row[1] else row[0]

            if s("alerts.on_error") != "1":
                return
            host, sender, to = s("smtp.host"), s("smtp.from"), s("alerts.email_to")
            if not (host and sender and to):
                return  # not configured; the settings screen says so already
            with _email_lock:
                if time.time() - _last_email_at < EMAIL_THROTTLE_SECONDS:
                    return
                _last_email_at = time.time()
            send_email(
                subject=f"Dockup error: {category}",
                body=f"{message}\n\n{detail or ''}\n\n- Dockup",
                override={
                    "smtp.host": host, "smtp.port": s("smtp.port"),
                    "smtp.security": s("smtp.security"), "smtp.username": s("smtp.username"),
                    "smtp.password": s("smtp.password"), "smtp.from": sender,
                    "alerts.email_to": to,
                },
            )
        finally:
            con.close()
    except Exception as exc:  # never let alerting take the app down
        try:
            con = db.connect()
            with con:
                con.execute(
                    "INSERT INTO activity(level, category, message, detail) VALUES(?,?,?,?)",
                    ("warning", "email", "Could not send the error alert email", str(exc)),
                )
            con.close()
        except Exception:
            pass


def _html_body(subject: str, body: str) -> str:
    # The plain-text body always ends "- Dockup" as its sign-off for
    # clients that only show the text part - redundant once the HTML
    # version has a branded header doing the same job, so drop it here.
    text = body.rstrip()
    if text.endswith("- Dockup"):
        text = text[: -len("- Dockup")].rstrip()
    paragraphs = "".join(
        f'<p style="margin:0 0 12px;white-space:pre-wrap;">{html.escape(p).replace(chr(10), "<br>")}</p>'
        for p in text.split("\n\n") if p.strip()
    )
    # Dark only, to match the app - no light baseline and no
    # prefers-color-scheme switch. Every colour is inline as well as
    # classed, since plenty of mail clients strip <style> blocks
    # outright; the classes exist only so a client that keeps them
    # can't be talked into a lighter card. The navy header is the
    # brand mark and was always fixed.
    return f"""\
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<style>
  body {{ background:#09090b; }}
  .card {{ background:#1b1d29; border-color:#5a5a63; }}
  .body-text {{ color:#fafafa; }}
  .footer {{ color:#a1a1aa; border-color:#5a5a63; }}
</style>
</head>
<body style="margin:0;padding:24px;background:#09090b;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="card" style="max-width:480px;margin:0 auto;background:#1b1d29;border:1px solid #5a5a63;border-radius:14px;overflow:hidden;">
<tr><td style="background:#111827;padding:20px 24px;">
<table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td style="padding-right:10px;"><img src="cid:{_LOGO_CID}" width="32" height="32" alt="Dockup" style="display:block;border-radius:7px;"></td>
<td style="color:#ffffff;font-size:18px;font-weight:700;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Dockup</td>
</tr></table>
</td></tr>
<tr><td class="body-text" style="padding:24px;color:#fafafa;font-size:15px;line-height:1.6;">
<p style="margin:0 0 16px;font-weight:700;font-size:16px;">{html.escape(subject)}</p>
{paragraphs}
</td></tr>
<tr><td class="footer" style="padding:16px 24px;border-top:1px solid #5a5a63;color:#a1a1aa;font-size:12px;">
Sent automatically by your Dockup instance.
</td></tr>
</table>
</body>
</html>
"""


def send_email(subject: str, body: str, override: dict | None = None):
    """Send via configured SMTP. Raises on failure so callers can report it."""
    s = override if override is not None else settingsvc.get_many(settingsvc.SCHEMA.keys())
    host = s["smtp.host"]
    if not host:
        raise RuntimeError("SMTP is not configured")
    port = int(s["smtp.port"] or 587)
    security = s["smtp.security"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s["smtp.from"]
    msg["To"] = s["alerts.email_to"]
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    msg.add_alternative(_html_body(subject, body), subtype="html")
    if _LOGO_PATH.exists():
        msg.get_payload()[1].add_related(
            _LOGO_PATH.read_bytes(), maintype="image", subtype="png", cid=f"<{_LOGO_CID}>",
        )

    ctx = ssl.create_default_context()
    if security == "tls":
        server = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
    try:
        if security == "starttls":
            server.starttls(context=ctx)
        if s["smtp.username"]:
            server.login(s["smtp.username"], s["smtp.password"])
        server.send_message(msg)
    finally:
        server.quit()
