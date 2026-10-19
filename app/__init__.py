import hmac
import secrets
from datetime import timedelta

from flask import Flask, g, jsonify, redirect, request, session, url_for

from . import config


def create_app():
    config.validate()
    config.ensure_dirs()

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=config.SECRET_KEY,
        # Not Flask's default "session": browsers scope cookies by hostname
        # only, ignoring the port - so on a machine where many apps share
        # one hostname on different ports (e.g. via Tailscale Serve), any
        # other Flask app using the default name overwrites ours every
        # time it's open in another tab, silently logging Dockup out.
        SESSION_COOKIE_NAME="dockup_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Off only in mock/dev mode (plain http://localhost). In real use
        # Dockup is only reachable through Tailscale Serve's HTTPS, so the
        # cookie should never go out over plain HTTP.
        SESSION_COOKIE_SECURE=not config.MOCK_MODE,
        PERMANENT_SESSION_LIFETIME=timedelta(days=config.SESSION_DAYS),
        # Most requests are small (JSON, compose text) - 8MB covers those
        # comfortably. Uploading a stack backup with real data needs far
        # more room, handled per-request below rather than raising this
        # globally.
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=8 * 1024 * 1024,
    )

    from . import db
    db.init()
    # Docker reads registry credentials only from a config.json, which
    # lives outside the data volume and so has to be rebuilt from the
    # database each time the container starts - see registries.py.
    from . import registries
    registries.write_at_startup()
    app.teardown_appcontext(db.close)

    from . import auth, backup, hostcompanion_api, maintenance, settings_api, stacks, views
    app.register_blueprint(views.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(stacks.bp)
    app.register_blueprint(maintenance.bp)
    app.register_blueprint(settings_api.bp)
    app.register_blueprint(backup.bp)
    app.register_blueprint(hostcompanion_api.bp)

    from .sockets import sock
    sock.init_app(app)

    PUBLIC = {"auth.login", "auth.setup", "views.health", "views.favicon", "static"}

    @app.before_request
    def make_csp_nonce():
        # CodeMirror 6 themes itself by injecting a <style> tag at
        # runtime rather than shipping a separate CSS file - blocked
        # outright by a plain style-src 'self'. The editor bundle reads
        # the nonce below from app.html's <meta property="csp-nonce">
        # and passes it through CM6's own EditorView.cspNonce facet, so
        # that one style tag carries a nonce the CSP header allows.
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.context_processor
    def inject_csp_nonce():
        return {"csp_nonce": getattr(g, "csp_nonce", "")}

    @app.before_request
    def raise_limit_for_backup_uploads():
        # Restoring real app data needs far more room than the 8MB
        # default meant for JSON/compose bodies. Scoped to this one
        # endpoint rather than raised globally.
        if request.endpoint == "stacks.api_stack_backup_upload":
            request.max_content_length = 4 * 1024 * 1024 * 1024  # 4GB
            request.max_form_memory_size = 8 * 1024 * 1024

    @app.before_request
    def require_login():
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC or endpoint.startswith("static"):
            return None
        # The account behind the session must still exist, not just the
        # cookie, so a browser stays signed in only while its user row does
        # (a reset done straight in the database, or a restored backup,
        # would otherwise leave old sessions valid).
        if not session.get("uid") or auth.current_user() is None:
            session.clear()
            if request.path.startswith("/api/") or request.path.startswith("/ws/"):
                return jsonify({"error": "Not signed in"}), 401
            return redirect(url_for("auth.login"))
        # CSRF: state-changing requests must echo the session token
        if request.method in ("POST", "PUT", "DELETE") and not request.path.startswith("/ws/"):
            token = request.headers.get("X-CSRF") or request.form.get("csrf")
            if not token or not hmac.compare_digest(token, session.get("csrf", "")):
                return jsonify({"error": "Session expired - reload the page"}), 403
        return None

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        nonce = getattr(g, "csp_nonce", "")
        resp.headers.setdefault(
            "Content-Security-Policy",
            # apps.lightmorphic.com serves the Lightmorphic app launcher
            # in the top bar - one script plus its icons, named here
            # rather than opened up to the web generally. It is the only
            # thing on the page Dockup doesn't serve itself, and it runs
            # with the same rights as the rest of the page: see
            # SECURITY.md. Everything else stays as it was, including the
            # style nonce, which the launcher does not need (it styles
            # itself inside a shadow root).
            f"default-src 'self'; img-src 'self' data: https://apps.lightmorphic.com; "
            f"style-src 'self' 'nonce-{nonce}'; "
            "script-src 'self' https://apps.lightmorphic.com; connect-src 'self' ws: wss:; "
            "base-uri 'self'; frame-ancestors 'none'",
        )
        return resp

    # forms on login/setup need a CSRF token too - simple approach:
    # they're rate-limited and create the session, so the standard
    # SameSite=Lax cookie policy covers them.

    backup.start_scheduler()

    # The before-backup dumps run on their own clock, early enough that
    # the nightly off-machine copy picks up fresh files rather than
    # yesterday's.
    from . import predump
    predump.start_scheduler()

    from . import updatecheck
    updatecheck.start(app)

    return app
