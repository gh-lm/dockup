from flask import Blueprint, jsonify, redirect, render_template, request, session

from . import activity, settingsvc

bp = Blueprint("views", __name__)


@bp.get("/")
def index():
    return render_template("app.html", csrf=session.get("csrf", ""))


@bp.get("/health")
def health():
    return jsonify({"ok": True})


@bp.get("/favicon.ico")
def favicon():
    return redirect("/static/icons/dockup.svg", code=301)


@bp.get("/api/activity")
def api_activity():
    errors_only = request.args.get("errors") == "1"
    return jsonify({"entries": activity.recent(200, errors_only)})


@bp.get("/api/onboarding")
def api_onboarding():
    return jsonify({"offerBulkAdopt": settingsvc.get("onboarding.bulk_adopt_offered") != "1"})


@bp.post("/api/onboarding/dismiss")
def api_onboarding_dismiss():
    settingsvc.set_many({"onboarding.bulk_adopt_offered": "1"})
    return jsonify({"ok": True})
