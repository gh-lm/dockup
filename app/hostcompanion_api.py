"""API for the optional dockup-companion host service: host OS update
checks/apply, Tailscale Serve status/install/toggle, and installing the
companion itself. Everything here degrades to a plain "not available"
if the companion isn't installed - see companion/install.sh and the
runbook.
"""

import shutil
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, stream_with_context

from . import activity, config, hostcompanion, runtime, stacks

bp = Blueprint("hostcompanion_api", __name__, url_prefix="/api/hostcompanion")


@bp.get("/status")
def api_status():
    if not hostcompanion.is_available():
        return jsonify({"available": False})
    try:
        os_info = hostcompanion.os_info()
        ts = hostcompanion.tailscale_status()
    except hostcompanion.CompanionUnavailable as exc:
        return jsonify({"available": False, "error": str(exc)})
    return jsonify({"available": True, "os": os_info, "tailscale": ts})


@bp.post("/os-update-check")
def api_os_update_check():
    try:
        result = hostcompanion.os_update_check()
    except hostcompanion.CompanionUnavailable as exc:
        return jsonify({"error": str(exc)}), 502
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "Check failed")}), 400
    return jsonify(result)


@bp.post("/os-update-apply")
def api_os_update_apply():
    try:
        result = hostcompanion.os_update_apply()
    except hostcompanion.CompanionUnavailable as exc:
        return jsonify({"error": str(exc)}), 502
    if not result.get("ok"):
        activity.log("error", "host-update", "Host OS update failed", result.get("error", ""))
        return jsonify({"error": result.get("error", "Update failed")}), 400
    activity.log("info", "host-update", "Host OS packages updated")
    return jsonify(result)


@bp.post("/tailscale/install")
def api_tailscale_install():
    try:
        result = hostcompanion.tailscale_install()
    except hostcompanion.CompanionUnavailable as exc:
        return jsonify({"error": str(exc)}), 502
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "Install failed")}), 400
    activity.log("info", "tailscale", "Tailscale installed on the host")
    return jsonify(result)


@bp.post("/reboot")
def api_reboot():
    activity.log("warning", "host", "Host reboot requested from Dockup")
    try:
        result = hostcompanion.reboot()
    except hostcompanion.CompanionUnavailable as exc:
        return jsonify({"error": str(exc)}), 502
    if not result.get("ok"):
        activity.log("error", "host", "Host reboot failed", result.get("error", ""))
        return jsonify({"error": result.get("error", "Reboot failed")}), 400
    return jsonify(result)


@bp.post("/docker-restart")
def api_docker_restart():
    activity.log("warning", "host", "Docker restart requested from Dockup")
    try:
        result = hostcompanion.docker_restart()
    except hostcompanion.CompanionUnavailable as exc:
        return jsonify({"error": str(exc)}), 502
    if not result.get("ok"):
        activity.log("error", "host", "Docker restart failed", result.get("error", ""))
        return jsonify({"error": result.get("error", "Docker restart failed")}), 400
    activity.log("info", "host", "Docker restarted on the host")
    return jsonify(result)


@bp.get("/stacks/<name>/serve")
def api_stack_serve_status(name):
    try:
        cp = stacks.compose_path(name)
        compose_text = cp.read_text() if cp.exists() else ""
        ep = stacks.stack_dir(name) / ".env"
        env_text = ep.read_text() if ep.exists() else ""
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    ports = hostcompanion.published_ports(compose_text, env_text)
    if not hostcompanion.is_available():
        return jsonify({"available": False, "ports": ports, "served": []})
    try:
        served = hostcompanion.tailscale_serve_list()
    except hostcompanion.CompanionUnavailable:
        return jsonify({"available": False, "ports": ports, "served": []})
    return jsonify({"available": True, "ports": ports, "served": served.get("ports", [])})


@bp.post("/stacks/<name>/serve")
def api_stack_serve_toggle(name):
    data = request.get_json(force=True)
    port = data.get("port")
    on = bool(data.get("on"))
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return jsonify({"error": "Invalid port"}), 400
    try:
        result = hostcompanion.tailscale_serve(port, on)
    except hostcompanion.CompanionUnavailable as exc:
        return jsonify({"error": str(exc)}), 502
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "Failed")}), 400
    activity.log("info", "tailscale", f"Serve {'enabled' if on else 'disabled'} for '{name}' port {port}")
    return jsonify(result)


@bp.post("/install")
def api_install_companion():
    """One-click host install + reconnect: stage Dockup's own bundled
    copy of the companion source into its data dir (reachable from the
    host via DOCKUP_DATA_HOST_PATH, the same trick backups use), run
    the real install.sh on the host through a short-lived privileged
    container, then uncomment the socket line in Dockup's own
    compose.yaml and restart Dockup to reconnect - see
    Runtime.install_companion_stream / reconnect_companion_stream. No
    standing extra permissions for Dockup's own container once this
    returns; the restart is why the stream ends abruptly instead of
    with a clean final line - expected, not a failure."""
    if not config.MOCK_MODE and not config.DATA_HOST_PATH:
        return jsonify({"error": "DOCKUP_DATA_HOST_PATH isn't set, so Dockup doesn't know its own "
                                  "real path on the host - see the runbook to set it in compose.yaml."}), 400
    bundled = Path(__file__).resolve().parent.parent / "companion"
    staging = config.DATA_DIR / ".companion-install"
    try:
        staging.mkdir(parents=True, exist_ok=True)
        for fname in ("dockup-companion.py", "dockup-companion.service", "install.sh"):
            shutil.copy(bundled / fname, staging / fname)
    except OSError as exc:
        return jsonify({"error": f"Couldn't stage companion files: {exc}"}), 500

    rt = runtime.current()
    data_host_path = config.DATA_HOST_PATH or str(config.DATA_DIR)
    staging_host_dir = f"{data_host_path.rstrip('/')}/.companion-install"
    # compose.yaml lives one level up from the data dir it mounts as
    # ./data - true by construction for every install this project
    # documents (DOCKUP_DATA_HOST_PATH is defined as "wherever
    # compose.yaml's own ./data resolves to on the host").
    compose_dir = str(Path(data_host_path).parent)
    compose_path = f"{compose_dir}/compose.yaml"

    def generate():
        ok = True
        try:
            for line in rt.install_companion_stream(staging_host_dir):
                if line.startswith("[dockup-exit:"):
                    ok = line == "[dockup-exit:0]"
                else:
                    yield line + "\n"
        except runtime.RuntimeError_ as exc:
            ok = False
            yield f"ERROR: {exc}\n"
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        if not ok:
            activity.log("error", "companion", "Companion install failed - see output panel")
            yield "[dockup-done:error]\n"
            return

        activity.log("info", "companion", "Companion installed on the host")
        yield "Companion installed. Reconnecting Dockup to it...\n"
        yield "[dockup-restarting]\n"
        try:
            for line in rt.reconnect_companion_stream(compose_path, compose_dir):
                if not line.startswith("[dockup-exit:"):
                    yield line + "\n"
        except runtime.RuntimeError_:
            pass  # expected - Dockup's own container recreation races this request
        yield "[dockup-done:ok]\n"

    # stream_with_context: without it, Flask doesn't keep the request/app
    # context alive for the generator's whole lifetime, and anything in
    # here needing it (activity.log) can throw well after the response
    # has already started - see the same fix in stacks.py's api_action
    # for the full story.
    return Response(stream_with_context(generate()), mimetype="text/plain",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
