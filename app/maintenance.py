"""Maintenance: disk usage and pruning - each type separately or all at
once. Volume pruning always shows exactly what will be deleted first.

Dockup does not update itself. It is a container like any other, so it
appears in its own stack list and is updated the same way as everything
else there: pull a newer image and redeploy.
"""

import math
import re
from pathlib import Path

import yaml
from flask import Blueprint, Response, jsonify, request, stream_with_context

from . import activity, config, envsub, runtime

bp = Blueprint("maintenance", __name__, url_prefix="/api/system")

SAFE_ORDER = ["containers", "images", "networks", "buildcache", "volumes"]


@bp.get("/df")
def disk_usage():
    try:
        return jsonify({"usage": runtime.current().disk_usage()})
    except runtime.RuntimeError_ as exc:
        return jsonify({"error": str(exc)}), 502


def _declared_volumes() -> dict[str, str]:
    """Full docker volume name -> the managed stack that declares it in its
    compose file. Compose prefixes named volumes with the project name unless
    the volume sets an explicit `name:` or is `external`."""
    from . import stacks, stackbackup

    owners: dict[str, str] = {}
    if not config.STACKS_DIR.exists():
        return owners
    for d in sorted(config.STACKS_DIR.iterdir()):
        if d.name.startswith(".") or not d.is_dir():
            continue
        cp = next((d / f for f in config.COMPOSE_FILENAMES if (d / f).exists()), None)
        if cp is None:
            continue
        compose_text = cp.read_text()
        envp = d / ".env"
        env_text = envp.read_text() if envp.exists() else ""
        env = envsub.parse_env(env_text)
        try:
            doc = yaml.safe_load(compose_text) or {}
        except yaml.YAMLError:
            doc = {}
        top = doc.get("volumes") if isinstance(doc, dict) else None
        top = top if isinstance(top, dict) else {}
        project = stacks._safe_project(d.name)
        for m in stackbackup._parse_mounts(compose_text, env_text):
            if m["type"] != "volume":
                continue
            short = m["source"]
            spec = top.get(short)
            if isinstance(spec, dict) and spec.get("name"):
                full = envsub.substitute(str(spec["name"]), env)
            elif isinstance(spec, dict) and spec.get("external"):
                full = short
            else:
                full = f"{project}_{short}"
            owners[full] = d.name
    return owners


def _known_projects() -> dict[str, str]:
    """Compose project name -> stack name, for every stack Dockup can see."""
    from . import stacks

    out = {}
    try:
        listed, _ = stacks.list_stacks()
    except Exception:
        return out
    for s in listed:
        out[stacks._safe_project(s["name"])] = s["name"]
        out[s["name"]] = s["name"]
    return out


def _flatten(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def _classify(name: str, declared: dict[str, str], projects: dict[str, str]) -> dict:
    """Say whose data a volume is, so the confirmation step is a decision and
    not a guess. Errs toward claiming an owner: a volume wrongly called
    orphaned is the one mistake that loses data."""
    stack = declared.get(name)
    if stack:
        return {"verdict": "in-use", "stack": stack,
                "note": f"belongs to stack “{stack}”, which isn't running - "
                        f"this is its live data, not leftovers"}
    # Compare with separators stripped: compose drops them from project names,
    # so uptimekuma owns uptime-kuma_data. Longest match wins.
    flat = _flatten(name)
    for project in sorted(projects, key=len, reverse=True):
        if project and flat.startswith(_flatten(project)):
            return {"verdict": "superseded", "stack": projects[project],
                    "note": f"left over from stack “{projects[project]}”, "
                            f"which no longer uses it"}
    return {"verdict": "orphaned", "stack": "",
            "note": "no stack on this host claims it"}


@bp.get("/prune/volumes/preview")
def volume_preview():
    try:
        vols = runtime.current().dangling_volumes()
    except runtime.RuntimeError_ as exc:
        return jsonify({"error": str(exc)}), 502
    declared, projects = _declared_volumes(), _known_projects()
    return jsonify({"volumes": [v | _classify(v["name"], declared, projects) for v in vols]})


@bp.post("/prune")
def prune():
    targets = request.get_json(force=True).get("targets", [])
    unknown = [t for t in targets if t not in runtime.PRUNE_TARGETS]
    if unknown or not targets:
        return jsonify({"error": "Pick at least one valid thing to prune"}), 400
    rt = runtime.current()
    results = {}
    ok = True
    for target in [t for t in SAFE_ORDER if t in targets]:
        try:
            results[target] = {"ok": True, "message": rt.prune(target)}
            activity.log("info", "prune", f"Pruned {target}: {results[target]['message']}")
        except runtime.RuntimeError_ as exc:
            ok = False
            results[target] = {"ok": False, "message": str(exc)}
            activity.log("error", "prune", f"Prune of {target} FAILED", str(exc))
    return jsonify({"ok": ok, "results": results})


@bp.get("/versions")
def api_versions():
    rt = runtime.current()
    try:
        engine = rt.ping()
    except runtime.RuntimeError_ as exc:
        engine = {"ok": False, "engine": "Docker", "version": "", "error": str(exc)}

    return jsonify({
        "dockup": {"version": config.VERSION},
        "docker": {
            "engine": engine.get("engine", "Docker"),
            "version": engine.get("version", ""),
            "ok": bool(engine.get("ok")),
            "error": engine.get("error", ""),
        },
    })
