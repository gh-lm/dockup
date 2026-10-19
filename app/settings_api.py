"""Settings screen API: read (masked), save, and test buttons."""

from flask import Blueprint, jsonify, request

from . import activity, registries, runtime, settingsvc

bp = Blueprint("settings_api", __name__, url_prefix="/api/settings")


def _check_numbers(data: dict) -> str:
    """Settings that are numbers must be sensible numbers. A retention of
    zero or less used to delete every backup including the one just
    made, and a word where a number belongs broke the nightly job."""
    limits = {
        "backup.hour": (0, 23, "The backup hour needs to be a whole number from 0 to 23."),
        "predump.hour": (0, 23, "The dump hour needs to be a whole number from 0 to 23."),
        "backup.retention_days": (1, 3650, "Keep backups for a whole number of days, at "
                                           "least 1."),
    }
    for key, (low, high, message) in limits.items():
        if key not in data:
            continue
        try:
            value = int(str(data[key]).strip())
        except ValueError:
            return message
        if not low <= value <= high:
            return message
    return ""


@bp.get("")
def get_settings():
    view = settingsvc.public_view()
    view["_smtp_ready"] = settingsvc.smtp_configured()
    return jsonify(view)


@bp.post("")
def save_settings():
    data = request.get_json(force=True)
    problem = _check_numbers(data)
    if problem:
        return jsonify({"error": problem}), 400
    if "predump.jobs" in data:
        from . import predump
        try:
            predump.validate_jobs(data["predump.jobs"])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    settingsvc.set_many(data)
    activity.log("info", "settings", "Settings saved")
    return jsonify({"ok": True})


@bp.post("/test-smtp")
def test_smtp():
    # Test whatever is currently on the settings screen, not just what
    # was last saved - a field typed in but not yet saved should still
    # be what gets tested. Blank fields (in particular the password,
    # left blank on purpose to keep the saved one) fall back to the
    # stored value, same as save_settings treats a blank secret.
    data = request.get_json(force=True) or {}
    keys = ["smtp.host", "smtp.port", "smtp.security", "smtp.username",
            "smtp.password", "smtp.from", "alerts.email_to"]
    override = {k: (data.get(k) or settingsvc.get(k)) for k in keys}
    try:
        activity.send_email(
            "Dockup test email",
            "This is the test email from Dockup's settings screen. "
            "If you're reading it, email alerts are working.\n\n- Dockup",
            override=override,
        )
    except Exception as exc:
        activity.log("warning", "email", "SMTP test failed", str(exc))
        return jsonify({"error": f"Sending failed: {exc}"}), 400
    # A successful test just proved these exact values work - save them
    # so a page refresh doesn't lose what was only ever typed into the
    # form, never pressed "Save settings" for. (set_many already treats
    # a blank/masked secret as "keep what's there", so this can't wipe
    # a real saved password even though override always carries a value.)
    settingsvc.set_many(override)
    activity.log("info", "email", "SMTP test email sent")
    return jsonify({"ok": True, "message": "Test email sent and settings saved - check the inbox."})


@bp.post("/test-runtime")
def test_runtime():
    data = request.get_json(force=True)
    engine = data.get("engine") or settingsvc.get("runtime.engine")
    socket = data.get("socket") or settingsvc.get("runtime.socket")
    from . import config
    if config.MOCK_MODE:
        return jsonify({"ok": True, "message": "Mock engine responding (dev mode)"})
    result = runtime.Runtime(engine, socket).ping()
    if result["ok"]:
        return jsonify({"ok": True,
                        "message": f"Connected: {result['engine']} {result['version']}"})
    return jsonify({"error": f"Could not reach the engine socket: {result['error']}"}), 400


# -- private registries -------------------------------------------------


@bp.get("/registries")
def list_registries():
    return jsonify(registries.list_public())


@bp.post("/registries")
def save_registry():
    data = request.get_json(force=True) or {}
    try:
        registries.save(data.get("host", ""), data.get("username", ""), data.get("token", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    host = registries.normalise_host(data.get("host", ""))
    activity.log("info", "settings", f"Registry credentials saved for {host}")
    return jsonify({"ok": True})


@bp.post("/registries/test")
def test_registry():
    """Test what is on screen, like the other test buttons. A blank token
    means "the one already saved for this host", so a saved registry can
    be re-tested without the token ever coming back to the browser."""
    data = request.get_json(force=True) or {}
    try:
        host = registries.normalise_host(data.get("host", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    username = (data.get("username") or "").strip()
    token = (data.get("token") or "").strip() or registries.stored_token(host)
    if not username or not token:
        return jsonify({"error": "Fill in the username and token to test."}), 400
    ok, message = runtime.current().registry_test(host, username, token)
    if not ok:
        return jsonify({"error": message}), 400
    return jsonify({"ok": True, "message": message})


@bp.post("/registries/<int:registry_id>/delete")
def delete_registry(registry_id):
    host = registries.delete(registry_id)
    if host is None:
        return jsonify({"error": "That registry isn't saved."}), 404
    activity.log("info", "settings", f"Registry credentials removed for {host}")
    return jsonify({"ok": True})


@bp.post("/predump/run")
def run_predump():
    """Run the before-backup dumps now, so somebody setting them up can
    see them work rather than waiting until two in the morning."""
    from . import predump
    try:
        status = predump.run_all("asked for")
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    done = [s["name"] for s in status["stacks"] if s["ok"]]
    failed = [s["name"] for s in status["stacks"] if not s["ok"]]
    if not status["stacks"]:
        message = "Nothing is set up to dump yet."
    elif failed:
        message = f"{len(done)} written, {len(failed)} failed: {', '.join(failed)}"
    else:
        message = f"{len(done)} written: {', '.join(done)}"
    return jsonify({"ok": status["ok"], "message": message, "status": status})
