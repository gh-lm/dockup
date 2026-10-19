"""Stack management: every folder in the stacks directory containing a
compose file is a stack, exactly as Dockge treats them. API + streaming
actions live here.
"""

import os
import re
import shutil
import socket
import subprocess
import tarfile
import time
from pathlib import Path

import yaml
from flask import Blueprint, Response, jsonify, request, stream_with_context

from . import activity, composeconv, composegen, config, locks, runtime

bp = Blueprint("stacks", __name__, url_prefix="/api")

NAME_RE = re.compile(config.STACK_NAME_RE)
ARCHIVE_DIR = config.STACKS_DIR / ".archived"


def stack_dir(name):
    if not NAME_RE.match(name or ""):
        raise ValueError("Stack names are lowercase letters, numbers, - and _ only")
    d = (config.STACKS_DIR / name).resolve()
    if d.parent != config.STACKS_DIR.resolve():
        raise ValueError("Invalid stack name")
    return d


def archived_stack_dir(name):
    if not NAME_RE.match(name or ""):
        raise ValueError("Stack names are lowercase letters, numbers, - and _ only")
    d = (ARCHIVE_DIR / name).resolve()
    if d.parent != ARCHIVE_DIR.resolve():
        raise ValueError("Invalid stack name")
    return d


def compose_path(name):
    d = stack_dir(name)
    for fname in config.COMPOSE_FILENAMES:
        p = d / fname
        if p.exists():
            return p
    return d / "compose.yaml"


class FileNotWritable(Exception):
    """A stack's compose file or .env could not be written to disk."""


def _not_writable_message(path):
    """Say, in words the person can act on, why a stack file will not save.
    Dockup runs as a plain user, so a stack folder owned by root - one
    made with sudo rather than through Dockup - cannot be saved to until
    it changes hands."""
    me, my_group = os.getuid(), os.getgid()
    target = path if path.exists() else path.parent
    msg = f"Dockup cannot save {path.name}. It runs as user {me}"
    try:
        msg += f" and {target} belongs to user {os.stat(target).st_uid}"
    except OSError:
        pass
    folder = target if target.is_dir() else target.parent
    return (msg + ". On the server, give that folder to the same user: "
            f"sudo chown -R {me}:{my_group} {folder}")


def write_stack_file(path, text):
    """Write one of a stack's files, turning a permission or disk problem
    into a message instead of a bare 500."""
    try:
        path.write_text(text)
    except PermissionError:
        raise FileNotWritable(_not_writable_message(path)) from None
    except OSError as exc:
        raise FileNotWritable(
            f"Could not save {path.name}: {exc.strerror or exc}") from None


def make_stack_dir(d):
    """Create a stack's folder, with the same plain words on failure."""
    try:
        d.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise FileNotWritable(_not_writable_message(d / "compose.yaml")) from None
    except OSError as exc:
        raise FileNotWritable(
            f"Could not make the folder for this stack: {exc.strerror or exc}") from None


def _own_container_ids() -> set:
    """Every way this process can learn its own container id. The
    hostname is the usual one, but a `hostname:` in an override file or
    host networking changes it, so the container's id is also read from
    its own mounts, where Docker puts it in the path of /etc/hostname."""
    ids = {socket.gethostname()[:12]}
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                m = re.search(r"/containers/([0-9a-f]{64})/", line)
                if m:
                    ids.add(m.group(1)[:12])
    except OSError:
        pass
    return ids


def _own_container(containers):
    """Dockup's own container in a ps() list."""
    ids = _own_container_ids()
    own = next((c for c in containers if c["id"] in ids), None)
    if own is None and config.MOCK_MODE:
        own = next((c for c in containers if c["project"] == "dockup"), None)
    return own


def own_compose_project(containers):
    """Which compose project Dockup itself belongs to. Dockup is a
    container like any other and appears in its own list, so this marks
    the one stack that happens to be Dockup rather than hiding it."""
    own = _own_container(containers)
    return (own["project"] or own["name"]) if own else None


def own_compose_project_safe():
    """own_compose_project without making the caller reach for a
    container list, and without raising when the engine is unreachable."""
    try:
        return own_compose_project(runtime.current().ps())
    except Exception:
        return None


def own_compose_dir(containers):
    """Dockup's own compose folder as the HOST sees it, straight off its
    container's compose labels. Needed because the one thing Dockup
    cannot do to itself is recreate its own container: that job goes to
    a helper running in the host's namespaces, which needs a host path,
    not Dockup's view of one."""
    own = _own_container(containers)
    return (own or {}).get("workingDir", "")


def list_stacks():
    rt = runtime.current()
    containers = []
    engine_error = None
    try:
        containers = rt.ps()
    except runtime.RuntimeError_ as exc:
        engine_error = str(exc)

    by_project: dict[str, list] = {}
    for c in containers:
        if c["project"]:
            by_project.setdefault(c["project"], []).append(c)

    stacks = {}
    if config.STACKS_DIR.exists():
        for d in sorted(config.STACKS_DIR.iterdir()):
            if d.name.startswith("."):
                continue  # .archived and any other dotfile/dir aren't stacks
            if d.is_dir() and any((d / f).exists() for f in config.COMPOSE_FILENAMES):
                stacks[d.name] = {"name": d.name, "managed": True, "containers": []}
    for project, cs in by_project.items():
        entry = stacks.setdefault(project, {"name": project, "managed": False, "containers": []})
        entry["containers"] = cs

    own_project = own_compose_project(containers)
    for s in stacks.values():
        # Dockup is one of the stacks, not a special case - but the page
        # still has to know which one it is, so it can say that stopping
        # this one takes the screen with it.
        s["isSelf"] = s["name"] == own_project
        states = {c["state"] for c in s["containers"]}
        has_warning = any(
            c["state"] == "restarting" or (c["state"] == "running" and "unhealthy" in c.get("status", "").lower())
            for c in s["containers"]
        )
        if not s["containers"]:
            s["status"] = "inactive"
        elif has_warning and "running" in states:
            s["status"] = "warning"
        elif states == {"running"}:
            s["status"] = "running"
        elif "running" in states:
            s["status"] = "partial"
        else:
            s["status"] = "stopped"
    return sorted(stacks.values(), key=lambda s: s["name"]), engine_error


def validate_compose(text):
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return f"YAML problem: {exc}"
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        return "A compose file needs a 'services:' section with at least one service"
    return None


_LIVE_PORT_RE = re.compile(r":(\d+)->")


def _declared_ports_by_stack(exclude=""):
    """port -> stack name, from every OTHER managed stack's own compose
    file. Checked even if that stack isn't currently running, since the
    whole point is to catch a collision before either side is deployed."""
    from . import hostcompanion
    result = {}
    if not config.STACKS_DIR.exists():
        return result
    for d in sorted(config.STACKS_DIR.iterdir()):
        if d.name.startswith(".") or d.name == exclude:
            continue
        if not d.is_dir() or not any((d / f).exists() for f in config.COMPOSE_FILENAMES):
            continue
        try:
            cp = compose_path(d.name)
            compose_text = cp.read_text() if cp.exists() else ""
            envp = d / ".env"
            env_text = envp.read_text() if envp.exists() else ""
            ports = hostcompanion.published_ports(compose_text, env_text)
        except OSError:
            continue
        for p in ports:
            result.setdefault(p, d.name)
    return result


def _live_bound_ports(exclude_names=frozenset()):
    """port -> container name, from every running container's actual
    port binding right now - catches anything Dockup doesn't manage
    (adopted elsewhere, started with a bare `docker run`) that a
    compose-file-only comparison would miss."""
    result = {}
    try:
        containers = runtime.current().ps()
    except runtime.RuntimeError_:
        return result
    for c in containers:
        if c["state"] != "running" or c["name"] in exclude_names:
            continue
        for m in _LIVE_PORT_RE.finditer(c.get("ports", "")):
            result.setdefault(int(m.group(1)), c["name"])
    return result


def check_port_conflicts(name, compose_text, env_text):
    """Ports this compose file would publish that collide with another
    managed stack's declared ports, or with any container's actual live
    binding right now. Returns [{"port": int, "with": str}, ...]."""
    from . import hostcompanion
    try:
        ports = hostcompanion.published_ports(compose_text, env_text)
    except Exception:
        return []
    if not ports:
        return []
    declared = _declared_ports_by_stack(exclude=name)
    own_containers = set()
    if name:
        try:
            own_containers = {c["name"] for c in runtime.current().ps() if c.get("project") == name}
        except runtime.RuntimeError_:
            pass
    live = _live_bound_ports(exclude_names=own_containers)
    conflicts = []
    for p in ports:
        source = declared.get(p) or live.get(p)
        if source and source != name:
            conflicts.append({"port": p, "with": source})
    return conflicts


# -- API ----------------------------------------------------------------


def add_port_state(s, served_ports):
    """Everything about a stack's ports that both the dashboard and the
    stack's own page need: what it publishes, what Serve fronts, what it
    should be publishing but isn't, and what is published so widely that
    Serve can't bind alongside it.

    Shared deliberately - these were computed only for the dashboard at
    first, so the stack page silently had none of them and its warnings
    could never appear."""
    from . import hostcompanion
    ports, bindings = [], []
    if s.get("managed"):
        try:
            cp = compose_path(s["name"])
            compose_text = cp.read_text() if cp.exists() else ""
            envp = stack_dir(s["name"]) / ".env"
            env_text = envp.read_text() if envp.exists() else ""
            bindings = hostcompanion.published_bindings(compose_text, env_text)
            ports = hostcompanion.published_ports(compose_text, env_text)
        except (ValueError, OSError):
            pass
    s["ports"] = ports
    s["served"] = [p for p in ports if p in served_ports]
    s["unpublished"] = _unpublished_ports(s, ports)
    if s["unpublished"] and s.get("status") == "running":
        # Every container is up and healthy, but a port the compose file
        # publishes isn't actually bound - the state a plain `docker
        # start` leaves behind after the daemon has been restarted.
        # Nothing else Dockup looks at can tell: this would otherwise
        # show a green stack nobody can connect to.
        s["status"] = "warning"
    s["wideBind"] = sorted(p for ip, p in bindings if not ip and p in served_ports)
    return s


@bp.get("/stacks")
def api_list():
    from . import hostcompanion, updatecheck
    result, engine_error = list_stacks()
    flags = updatecheck.get_flags()

    # One companion round-trip for the whole list, not one per stack -
    # every card's "open web UI" link and served-port state comes from
    # this single check.
    served_ports, dns_name = [], ""
    try:
        if hostcompanion.is_available():
            served_ports = hostcompanion.tailscale_serve_list().get("ports", [])
            dns_name = hostcompanion.tailscale_status().get("dnsName", "")
    except hostcompanion.CompanionUnavailable:
        pass

    for s in result:
        s["updateAvailable"] = flags.get(s["name"], False)
        add_port_state(s, served_ports)

    rt = runtime.current()
    # Where Dockup's own compose file is, as the host sees it. The page
    # needs it for the one case the install steps do not produce: a
    # Dockup whose folder sits outside the stacks folder, where it can
    # be seen running but not read or edited.
    self_dir = ""
    try:
        self_dir = own_compose_dir(rt.ps())
    except runtime.RuntimeError_:
        pass
    return jsonify({"stacks": result, "engine": rt.ping(), "engineError": engine_error,
                    "dnsName": dns_name, "selfDir": self_dir})


def _unpublished_ports(stack, declared):
    """Ports the compose file publishes that none of this stack's own
    running containers is actually bound to.

    Only meaningful when every container is up: a stopped container is
    supposed to have no bindings. A running one without them is the
    silent failure - `docker start` after a daemon restart brings the
    container back without re-establishing its port mappings, so it
    reports Up and healthy while being unreachable. Only a recreate
    fixes it, which is why this is worth surfacing rather than trusting
    container state alone."""
    if not declared or stack.get("status") != "running":
        return []
    if config.MOCK_MODE:
        return []  # the mock engine reports no bindings at all, which would read as a fault
    bound = set()
    for c in stack.get("containers", []):
        if c.get("state") != "running":
            return []  # not the whole stack up; absent bindings prove nothing
        for m in _LIVE_PORT_RE.finditer(c.get("ports", "")):
            bound.add(int(m.group(1)))
    return sorted(p for p in declared if p not in bound)


def _stack_data_paths(name):
    """Every bind mount and named volume this stack actually uses, for
    showing the user exactly what "also delete this stack's data" would
    remove - same mount parsing stackbackup already relies on, so the
    list here is guaranteed to match what a backup would have covered."""
    from . import stackbackup
    d = stack_dir(name)
    cp = compose_path(name)
    if not cp.exists():
        return []
    compose_text = cp.read_text()
    envp = d / ".env"
    env_text = envp.read_text() if envp.exists() else ""
    mounts = stackbackup._parse_mounts(compose_text, env_text)
    result = []
    for m in mounts:
        if m["type"] == "bind":
            m["source"] = os.path.normpath(stackbackup._resolve_bind_source(m["source"], d))
            if not _deletable_bind(m["source"], m["readonly"]):
                continue
        result.append(m)
    return result


# Top-level host paths that are never a stack's own data, however a
# compose file mounts them. Anything at or under one of these is left
# alone by "also delete this stack's data".
_SYSTEM_PATHS = ("/etc", "/proc", "/sys", "/dev", "/usr", "/bin", "/sbin", "/lib",
                 "/lib64", "/boot", "/root", "/run", "/var/run", "/var/lib/docker",
                 "/var/log", "/tmp")


def _deletable_bind(source: str, readonly: bool) -> bool:
    """Whether a bind mount is plausibly this stack's own data, safe to
    wipe on delete. Monitoring stacks routinely mount /, /proc, /sys,
    /etc/localtime or the Docker socket into their containers; none of
    that is the stack's data, and wiping it would take the host with
    it. Read-only mounts are by definition something the stack only
    looks at. And the root of the filesystem or any top-level folder
    (/, /opt, /home) is far too broad to ever be one stack's data."""
    if readonly:
        return False
    parts = Path(source).parts
    if len(parts) < 3:
        return False
    for sysp in _SYSTEM_PATHS:
        if source == sysp or source.startswith(sysp + "/"):
            return False
    return True


@bp.get("/stacks/<name>")
def api_get(name):
    from . import updatecheck
    d = stack_dir(name)
    cp = compose_path(name)
    envp = d / ".env"
    from . import hostcompanion
    stacks, _ = list_stacks()
    match = next((s for s in stacks if s["name"] == name), None)
    served_ports = []
    try:
        if hostcompanion.is_available():
            served_ports = hostcompanion.tailscale_serve_list().get("ports", [])
    except hostcompanion.CompanionUnavailable:
        pass
    if match:
        add_port_state(match, served_ports)
    return jsonify({
        "name": name,
        "exists": d.exists(),
        "managed": cp.exists(),
        "compose": cp.read_text() if cp.exists() else "",
        "composeFile": cp.name,
        "env": envp.read_text() if envp.exists() else "",
        "status": (match or {}).get("status", "inactive"),
        "containers": (match or {}).get("containers", []),
        "updateAvailable": updatecheck.get_flags().get(name, False),
        "dataPaths": _stack_data_paths(name),
        "ports": (match or {}).get("ports", []),
        "served": (match or {}).get("served", []),
        "unpublished": (match or {}).get("unpublished", []),
        "wideBind": (match or {}).get("wideBind", []),
        # Dockup is a stack like any other, but the page still says which
        # one it is: stopping this one takes the screen with it.
        "isSelf": bool((match or {}).get("isSelf")),
    })


@bp.post("/stacks")
def api_create():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    compose_text = data.get("compose") or ""
    try:
        d = stack_dir(name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if any((d / f).exists() for f in config.COMPOSE_FILENAMES):
        return jsonify({"error": f"A stack called '{name}' already exists"}), 409
    problem = validate_compose(compose_text)
    if problem:
        return jsonify({"error": problem}), 400
    try:
        make_stack_dir(d)
        write_stack_file(d / "compose.yaml", compose_text)
        if data.get("env"):
            write_stack_file(d / ".env", data["env"])
    except FileNotWritable as exc:
        activity.log("error", "stack", f"Creating stack '{name}' FAILED", str(exc))
        return jsonify({"error": str(exc)}), 500
    activity.log("info", "stack", f"Created stack '{name}'")
    return jsonify({"ok": True, "name": name})


@bp.put("/stacks/<name>")
def api_save(name):
    data = request.get_json(force=True)
    try:
        d = stack_dir(name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not d.exists():
        return jsonify({"error": "Stack not found"}), 404
    if "compose" in data:
        problem = validate_compose(data["compose"])
        if problem:
            return jsonify({"error": problem}), 400
        try:
            write_stack_file(compose_path(name), data["compose"])
        except FileNotWritable as exc:
            activity.log("error", "stack", f"Saving '{name}' FAILED", str(exc))
            return jsonify({"error": str(exc)}), 500
    if "env" in data:
        envp = d / ".env"
        try:
            if data["env"]:
                write_stack_file(envp, data["env"])
            elif envp.exists():
                envp.unlink()
        except (FileNotWritable, OSError) as exc:
            message = str(exc) if isinstance(exc, FileNotWritable) else _not_writable_message(envp)
            activity.log("error", "stack", f"Saving '{name}' FAILED", message)
            return jsonify({"error": message}), 500
    activity.log("info", "stack", f"Saved changes to '{name}'")
    return jsonify({"ok": True})


_STARTING_ACTIONS = {"up", "start", "restart", "update", "redeploy"}
_PORT_CONFLICT_RE = re.compile(r"bind host port (?:[\w.:]+:)?(\d+)/tcp: address already in use")


def _tailscale_pause_ports(name, d, available):
    """Before a stack binds its ports, pause any Tailscale Serve rule
    already holding one of them - the exact cause of "address already
    in use" when a stack is deleted and recreated while its old Serve
    mapping is still live (Tailscale's own listener keeps the port even
    though nothing is using it anymore). Returns the ports paused, to
    resume after; does nothing if the companion isn't installed."""
    if not available:
        return []
    from . import hostcompanion
    try:
        cp = compose_path(name)
        compose_text = cp.read_text() if cp.exists() else ""
        envp = d / ".env"
        env_text = envp.read_text() if envp.exists() else ""
        published = hostcompanion.published_ports(compose_text, env_text)
        served = hostcompanion.tailscale_serve_list().get("ports", [])
    except (hostcompanion.CompanionUnavailable, OSError):
        return []
    paused = []
    for port in published:
        if port not in served:
            continue
        try:
            hostcompanion.tailscale_serve(port, False)
            paused.append(port)
        except hostcompanion.CompanionUnavailable:
            pass
    return paused


def _tailscale_resume_ports(ports):
    if not ports:
        return
    from . import hostcompanion
    for port in ports:
        try:
            hostcompanion.tailscale_serve(port, True)
        except hostcompanion.CompanionUnavailable:
            pass


def _port_conflict_hint(line, available):
    """If this line is Docker's own port-bind-conflict error, translate
    it into a sentinel the frontend renders as a clear explanation
    instead of raw stderr. Fires even when the companion auto-paused
    Serve above - e.g. a second, unrelated process could hold the port -
    so it's a general safety net, not just a companion-missing fallback."""
    m = _PORT_CONFLICT_RE.search(line)
    if not m:
        return None
    return f"[dockup-hint:tailscale-port-conflict:{m.group(1)}:{1 if available else 0}]"


_NETWORK_LABEL_RE = re.compile(r"network (\S+) was found but has incorrect label com\.docker\.compose\.network")


def _compose_with_network_fix(rt, name, d, action):
    """Run one compose action, and if it failed only because compose
    tried to take ownership of a network that predates Dockup managing
    this stack, declare that network external and retry once.

    Yields ('line', text) for every output line of both attempts,
    ('notice', text) once for the retry explanation, and finally
    ('done', ok). Callers decide what to surface - the streaming action
    panel prints lines, hints and the notice; Update all captures a port
    conflict from the lines and stays silent otherwise. Keeping the
    run-once-retry-once logic here means both can't drift apart."""
    local_ok = True
    fix_network = None
    for line in rt.compose_stream(str(d), name, action):
        if line.startswith("[dockup-exit:"):
            local_ok = line == "[dockup-exit:0]"
        else:
            yield ("line", line)
            m = _NETWORK_LABEL_RE.search(line)
            if m:
                fix_network = m.group(1)
    if not local_ok and fix_network and _fix_unlabeled_network(name, fix_network):
        yield ("notice", f"Network '{fix_network}' predates Dockup managing this stack - declaring it "
                         f"external so compose reuses it instead of trying to own it, and retrying...")
        local_ok = True
        for line in rt.compose_stream(str(d), name, action):
            if line.startswith("[dockup-exit:"):
                local_ok = line == "[dockup-exit:0]"
            else:
                yield ("line", line)
    yield ("done", local_ok)


def _builds_its_own_image(name: str) -> bool:
    """True if any service in this stack is built here rather than fetched.

    Not everything self-hosted comes from a registry. A stack can hold a
    company's own books, or anything else there is no good reason to publish,
    and be built on the machine that runs it instead. Update used to go
    looking for those in a registry and come back with "access denied", and
    once the compose file was corrected to say `build:`, Update stopped
    failing but also stopped doing anything.
    """
    cp = compose_path(name)
    if not cp.exists():
        return False
    try:
        doc = yaml.safe_load(cp.read_text()) or {}
    except yaml.YAMLError:
        return False
    services = doc.get("services")
    if not isinstance(services, dict):
        return False
    return any(isinstance(s, dict) and s.get("build") for s in services.values())


def _fetch_source(name: str, d: Path):
    """If the stack folder is a git clone, bring it up to date first.

    Yields output lines. A stack that builds from source is only as new as
    the source sitting in its folder, so without this Update would rebuild
    the same code for ever and report success every time.

    --ff-only on purpose: if somebody has edited the code in place, stopping
    and saying so is better than a merge nobody asked for.
    """
    if not (d / ".git").exists():
        return
    yield "Fetching the newest source...\n"
    try:
        proc = subprocess.run(
            ["git", "-c", "safe.directory=*", "pull", "--ff-only", "--quiet"],
            cwd=str(d), capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        yield f"Could not fetch the source ({exc}) - building what is already here.\n"
        return
    if proc.returncode == 0:
        yield "Source up to date.\n"
    else:
        # Not fatal. The rebuild is also what keeps the base image patched,
        # and a repository that cannot be reached for an evening must not
        # stop that happening.
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        yield f"Could not fetch the source: {detail[-1] if detail else 'unknown'}\n"
        yield "Building what is already here.\n"


def _fix_unlabeled_network(project: str, network_name: str) -> bool:
    """One-time repair for a network that predates Dockup managing this
    stack (or any docker-compose lifecycle ownership) - a real, common
    state for anything adopted from a previous manager. Labels can't be
    edited on an existing network, so the only fix is telling compose
    to treat it as external and just use it as-is, instead of trying
    to validate ownership it was never given."""
    cp = compose_path(project)
    if not cp.exists():
        return False
    text = cp.read_text()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    if not isinstance(doc, dict):
        return False
    networks = doc.get("networks")
    if isinstance(networks, dict):
        # A networks: block already exists (composegen's output, or a user's).
        # If one entry declares this network by name but lacks external: true,
        # patch it in place rather than appending a second networks: key.
        # Anything else in the file (comments, key order, unrelated entries) is
        # left alone.
        for key, val in networks.items():
            if isinstance(val, dict) and val.get("name") == network_name:
                if val.get("external"):
                    return False  # already external - a different failure
                val["external"] = True
                cp.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
                return True
        return False  # networks: exists but none of it is this network
    key = network_name[len(project) + 1:] if network_name.startswith(project + "_") else network_name
    if not text.endswith("\n"):
        text += "\n"
    text += f"\nnetworks:\n  {key}:\n    name: {network_name}\n    external: true\n"
    cp.write_text(text)
    return True


@bp.post("/stacks/<name>/action/<action>")
def api_action(name, action):
    if action not in ("up", "down", "stop", "start", "restart", "update", "redeploy", "delete"):
        return jsonify({"error": "Unknown action"}), 400
    try:
        d = stack_dir(name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not d.exists() and action != "delete":
        if name == own_compose_project_safe():
            return jsonify({"error": "Dockup's own compose file is outside the stacks folder, so it "
                                     "cannot be acted on from here. Move that folder into the stacks "
                                     "folder, keeping compose.yaml, .env and data together."}), 404
        return jsonify({"error": f"'{name}' hasn't been adopted into the stacks folder yet"}), 404
    rt = runtime.current()
    delete_data = action == "delete" and request.args.get("deleteData") == "1"
    # Fail closed. Running the ordinary compose path against Dockup's own
    # stack stops the process doing it, so if the engine cannot be asked
    # who Dockup is, nothing is done rather than guessing "someone else".
    try:
        is_self = name == own_compose_project(rt.ps())
    except runtime.RuntimeError_ as exc:
        return jsonify({"error": "Dockup could not reach the engine to tell whether this is "
                                 f"its own stack, so nothing was done ({exc})."}), 502
    if is_self and action in ("stop", "down", "delete"):
        # Each of these ends the container running this request, and
        # nothing is left alive to start it again.
        return jsonify({"error": "This is Dockup itself. Stopping, taking down or deleting it "
                                 "would end this screen, and nothing would be left running to "
                                 "bring it back. Use Redeploy or Update, or do it from a "
                                 "terminal on the machine."}), 400

    def generate():
        # One action per stack at a time, and Dockup restarting itself
        # also takes the lock shared with backup and restore. Taken here,
        # inside the stream, so it is released by the finally even when
        # the client goes away.
        wanted = [locks.stack(name)] + ([locks.MAINTENANCE] if is_self else [])
        taken = []
        for key in wanted:
            if not locks.try_acquire(key, f"{action.capitalize()} on {name}"):
                for t in taken:
                    locks.release(t)
                yield (f"{locks.what_is_running(key) or 'Another action'} is already running. "
                       "Wait for it to finish, then try again.\n")
                yield "[dockup-done:error]\n"
                return
            taken.append(key)
        try:
            yield from _generate_locked()
        finally:
            for t in taken:
                locks.release(t)

    def _generate_locked():
        ok = True
        from . import hostcompanion
        companion_available = (action in _STARTING_ACTIONS or action == "delete") and hostcompanion.is_available()
        paused_ports = []

        # The one thing Dockup cannot do to itself is replace its own
        # container: the process running compose is gone the moment that
        # happens. For its own stack the step goes to a short-lived helper in
        # the host's namespaces, which finishes after Dockup has gone. Every
        # other stack runs the ordinary way.
        def run_self_compose():
            nonlocal ok
            host_dir = own_compose_dir(rt.ps())
            if not host_dir:
                yield "Dockup cannot find its own compose folder, so it cannot restart itself.\n"
                ok = False
                return
            # The page watches for this marker so it can wait for Dockup
            # to come back instead of reporting the dropped connection as
            # a network error.
            yield "[dockup-restarting]\n"
            yield ("Dockup is restarting itself. This page will go quiet for a few seconds "
                   "and come back on its own.\n")
            action_ok = True
            for line in rt.self_recreate_stream(host_dir):
                if line.startswith("[dockup-exit:"):
                    action_ok = line == "[dockup-exit:0]"
                else:
                    yield line + "\n"
            ok = ok and action_ok

        def run_compose(sub_action):
            nonlocal ok
            if is_self and sub_action in ("up", "restart", "stop", "down"):
                yield from run_self_compose()
                return
            action_ok = True
            for kind, text in _compose_with_network_fix(rt, name, d, sub_action):
                if kind == "line":
                    yield text + "\n"
                    hint = _port_conflict_hint(text, companion_available)
                    if hint:
                        yield hint + "\n"
                elif kind == "notice":
                    yield text + "\n"
                elif kind == "done":
                    action_ok = text
            ok = ok and action_ok

        try:
            # Not for Dockup's own stack. Serve is paused so a stack can bind a
            # port Serve is holding, and resumed in the finally below, but when the
            # stack is Dockup the container running that finally is replaced first,
            # so Serve would stay off and Dockup's own address would stop working.
            # There is nothing to avoid anyway: Dockup already holds the port and
            # hands it straight to its replacement.
            if action in _STARTING_ACTIONS and not is_self:
                paused_ports = _tailscale_pause_ports(name, d, companion_available)
                if paused_ports:
                    yield (f"Pausing Tailscale Serve on port(s) {', '.join(map(str, paused_ports))} "
                           f"so this stack can bind them...\n")
            if action == "update":
                if _builds_its_own_image(name):
                    yield from _fetch_source(name, d)
                    yield from run_compose("build")
                yield from run_compose("pull")
                yield from run_compose("up")
            elif action == "delete":
                if d.exists():
                    # Grab the compose text and data-mount list before the
                    # folder is gone - both the image purge and (if asked)
                    # the data wipe below need it.
                    cp = compose_path(name)
                    compose_text = cp.read_text() if cp.exists() else ""
                    envp = d / ".env"
                    env_text = envp.read_text() if envp.exists() else ""
                    mounts = _stack_data_paths(name) if delete_data else []

                    # Permanent, not a pause: a deleted stack's ports
                    # should stop being served, not just quiet down and
                    # come back - a stale Serve rule left running here is
                    # exactly what caused the "address already in use"
                    # failure this project chased down earlier.
                    cleared_ports = _tailscale_pause_ports(name, d, companion_available)
                    if cleared_ports:
                        yield (f"Turning off Tailscale Serve on port(s) {', '.join(map(str, cleared_ports))} "
                               f"- this stack is being deleted...\n")
                    # 45s cap: a container stuck because its bind-mount
                    # source vanished out from under it (deleted by
                    # hand while running, say) can hang a graceful
                    # `down` forever otherwise - a delete must always
                    # be able to finish, so a hang here falls back to
                    # forcefully removing the containers instead of
                    # leaving the stack stuck in Dockup for good.
                    hung = False
                    for line in rt.compose_stream(str(d), name, "down", timeout=45):
                        if line == "[dockup-timeout]":
                            hung = True
                        elif line.startswith("[dockup-exit:"):
                            ok = hung or line == "[dockup-exit:0]"
                        else:
                            yield line + "\n"
                    if hung:
                        yield ("`down` didn't finish within 45s - forcefully removing "
                               "the container(s) instead...\n")
                        try:
                            rt.force_remove_containers(name)
                            yield "Forced removal complete.\n"
                        except runtime.RuntimeError_ as exc:
                            yield f"Forced removal also failed: {exc}\n"

                    # Every trace of the container/image, unconditionally -
                    # unlike the data below, a cached image is never
                    # something worth keeping around after a deliberate
                    # delete, and re-pulling it later is cheap.
                    removed_images = _purge_images(compose_text, env_text)
                    if removed_images:
                        yield f"Removed image(s): {', '.join(removed_images)}\n"

                    if delete_data and mounts:
                        bind_parents = set()
                        for m in mounts:
                            # Best-effort: a problem wiping one mount
                            # (in use elsewhere, permission denied, a
                            # bug in the runtime backend) shouldn't
                            # abort the delete itself - the stack's
                            # config and containers are already gone
                            # regardless of what happens here.
                            try:
                                if m["type"] == "bind":
                                    src = Path(m["source"])
                                    rt.force_remove_dir(str(src.parent), src.name)
                                    bind_parents.add(src.parent)
                                else:
                                    rt.remove_volume(m["source"])
                                yield f"Deleted data: {m['source']}\n"
                            except Exception as exc:
                                yield f"Couldn't delete data '{m['source']}': {exc}\n"
                        # Clean up each mount's now-likely-empty parent
                        # too (e.g. /opt/<stack> once data/media/backup
                        # are gone) - rmdir only succeeds if it's
                        # genuinely empty, so this never touches
                        # anything not actually cleared above. Floored
                        # at 3 path parts ('/', 'opt', name) so this can
                        # never reach /opt itself even if something
                        # were misconfigured.
                        for parent in bind_parents:
                            if len(parent.parts) < 3:
                                continue
                            try:
                                rt.rmdir_if_empty(str(parent.parent), parent.name)
                            except Exception:
                                pass

                    try:
                        shutil.rmtree(d)
                    except OSError:
                        # A folder left root-owned by whatever managed it before Dockup can't
                        # be removed by Dockup's own non-root process, so fall back to a
                        # throwaway root container, as backup and restore do.
                        rt.force_remove_dir(str(d.parent), d.name)
                    yield f"Removed {d}\n"
            else:
                yield from run_compose(action)
        except runtime.RuntimeError_ as exc:
            ok = False
            yield f"ERROR: {exc}\n"
        except Exception as exc:
            # Anything unexpected above (not a docker RuntimeError_) would otherwise
            # end the stream mid-flight with no trace: the client just sees the
            # connection die, and this sits above the completion logging, so nothing
            # reaches Activity. It must never be silent.
            ok = False
            yield f"ERROR: unexpected {type(exc).__name__}: {exc}\n"
            activity.log("error", "stack", f"{action.capitalize()} crashed unexpectedly on '{name}'",
                         f"{type(exc).__name__}: {exc}")
        finally:
            if paused_ports:
                yield f"Restoring Tailscale Serve on port(s) {', '.join(map(str, paused_ports))}...\n"
                _tailscale_resume_ports(paused_ports)
        if ok:
            if action == "update":
                from . import updatecheck
                updatecheck.clear_flag(name)
            activity.log("info", "stack", f"{action.capitalize()} completed on '{name}'")
            yield "[dockup-done:ok]\n"
        else:
            activity.log("error", "stack", f"{action.capitalize()} FAILED on '{name}'",
                         "Open the stack's output panel for the full error text.")
            yield "[dockup-done:error]\n"

    # stream_with_context keeps the request and app context alive for the
    # generator's whole lifetime. Without it anything deep in here that needs
    # them (runtime.current() -> settingsvc.get() -> db.get(), activity.log())
    # can fail with "Working outside of application context" mid-stream,
    # after the response has started.
    return Response(stream_with_context(generate()), mimetype="text/plain",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


def _purge_images(compose_text: str, env_text: str = "") -> list:
    """Best-effort: remove every image a compose file references. Used
    when a stack (already container-less) is being deleted for good -
    "nothing left of that" - never blocks on failure, since another
    stack might legitimately share the same image."""
    from . import envsub
    try:
        doc = yaml.safe_load(compose_text) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    env = envsub.parse_env(env_text)
    images, seen = [], set()
    for svc in (doc.get("services") or {}).values():
        if not isinstance(svc, dict) or not svc.get("image"):
            continue
        image = envsub.substitute(str(svc["image"]), env)
        if image not in seen:
            seen.add(image)
            images.append(image)
    rt = runtime.current()
    removed = []
    for image in images:
        try:
            rt.remove_image(image)
            removed.append(image)
        except runtime.RuntimeError_:
            pass  # in use elsewhere, or already gone - fine either way
    return removed


@bp.post("/stacks/<name>/purge")
def api_purge(name):
    """Delete a container-less stack for good, straight from the main
    list (no need to archive first): folder, compose file, and every
    image it referenced - nothing left behind."""
    d = stack_dir(name)
    if not d.exists():
        return jsonify({"error": f"'{name}' doesn't exist"}), 404
    result, _ = list_stacks()
    match = next((s for s in result if s["name"] == name), None)
    if match and match["containers"]:
        return jsonify({"error": "This stack still has containers - stop and remove them first"}), 400
    cp = compose_path(name)
    compose_text = cp.read_text() if cp.exists() else ""
    envp = d / ".env"
    env_text = envp.read_text() if envp.exists() else ""
    removed_images = _purge_images(compose_text, env_text)
    try:
        shutil.rmtree(d)
    except OSError:
        rt = runtime.current()
        rt.force_remove_dir(str(d.parent), d.name)
    detail = f"Removed image(s): {', '.join(removed_images)}" if removed_images else ""
    activity.log("info", "stack", f"Purged '{name}'", detail)
    return jsonify({"ok": True, "removedImages": removed_images})


@bp.post("/stacks/<name>/archive")
def api_archive(name):
    """Archive a container-less stack: keep the folder (compose file,
    .env, anything else in it) but move it out of the main list, so it
    can come back later without re-typing the config from scratch."""
    d = stack_dir(name)
    if not d.exists():
        return jsonify({"error": f"'{name}' doesn't exist"}), 404
    result, _ = list_stacks()
    match = next((s for s in result if s["name"] == name), None)
    if match and match["containers"]:
        return jsonify({"error": "This stack still has containers - stop and remove them first"}), 400
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = archived_stack_dir(name)
    if dest.exists():
        return jsonify({"error": f"An archived stack named '{name}' already exists"}), 400
    try:
        shutil.move(str(d), str(dest))
    except OSError as exc:
        return jsonify({"error": f"Couldn't archive: {exc}"}), 500
    activity.log("info", "stack", f"Archived '{name}'")
    return jsonify({"ok": True})


@bp.get("/archived")
def api_archived_list():
    if not ARCHIVE_DIR.exists():
        return jsonify({"stacks": []})
    names = sorted(p.name for p in ARCHIVE_DIR.iterdir() if p.is_dir())
    return jsonify({"stacks": names})


@bp.post("/archived/<name>/restore")
def api_archived_restore(name):
    src = archived_stack_dir(name)
    if not src.exists():
        return jsonify({"error": f"'{name}' isn't archived"}), 404
    dest = stack_dir(name)
    if dest.exists():
        return jsonify({"error": f"A stack named '{name}' already exists"}), 400
    try:
        shutil.move(str(src), str(dest))
    except OSError as exc:
        return jsonify({"error": f"Couldn't restore: {exc}"}), 500
    activity.log("info", "stack", f"Restored '{name}' from archive")
    return jsonify({"ok": True})


@bp.post("/archived/<name>/purge")
def api_archived_purge(name):
    """Delete an archived stack for good: folder, compose file, and
    every image it referenced - nothing left behind."""
    d = archived_stack_dir(name)
    if not d.exists():
        return jsonify({"error": f"'{name}' isn't archived"}), 404
    cp = None
    for fname in config.COMPOSE_FILENAMES:
        if (d / fname).exists():
            cp = d / fname
            break
    compose_text = cp.read_text() if cp else ""
    envp = d / ".env"
    env_text = envp.read_text() if envp.exists() else ""
    removed_images = _purge_images(compose_text, env_text)
    try:
        shutil.rmtree(d)
    except OSError:
        rt = runtime.current()
        rt.force_remove_dir(str(d.parent), d.name)
    detail = f"Removed image(s): {', '.join(removed_images)}" if removed_images else ""
    activity.log("info", "stack", f"Purged archived stack '{name}'", detail)
    return jsonify({"ok": True, "removedImages": removed_images})


def _update_one(name, d, rt):
    """Pull + redeploy a single stack, output collected rather than
    streamed. Used by Update all, where there's no per-stack output
    panel to stream into; a single stack's update always runs through
    the streaming action so its output is visible."""
    from . import hostcompanion
    companion_available = hostcompanion.is_available()
    paused_ports = _tailscale_pause_ports(name, d, companion_available)
    conflict_port = None
    try:
        ok = True
        for action in ("pull", "up"):
            for kind, text in _compose_with_network_fix(rt, name, d, action):
                if kind == "line" and not conflict_port:
                    m = _PORT_CONFLICT_RE.search(text)
                    if m:
                        conflict_port = m.group(1)
                elif kind == "done":
                    ok = ok and text
        if ok:
            from . import updatecheck
            updatecheck.clear_flag(name)
            activity.log("info", "stack", f"Update completed on '{name}'")
            return True, "Updated"
        if conflict_port:
            detail = (f"Port {conflict_port} was still held by a Tailscale Serve rule from a previous "
                      f"version of this stack." + ("" if companion_available else
                      " Installing the dockup-companion (Settings → Host OS & Tailscale) lets Dockup clear this "
                      "automatically next time."))
            activity.log("error", "stack", f"Update FAILED on '{name}'", detail)
            return False, f"Port {conflict_port} conflict - see Activity for details"
        activity.log("error", "stack", f"Update FAILED on '{name}'",
                     "Open the stack's output panel for the full error text.")
        return False, "Update failed - see Activity for details"
    except runtime.RuntimeError_ as exc:
        activity.log("error", "stack", f"Update FAILED on '{name}'", str(exc))
        return False, str(exc)
    finally:
        _tailscale_resume_ports(paused_ports)


@bp.post("/stacks/update-all")
def api_update_all():
    """Pull + redeploy every managed stack that has an update flagged."""
    from . import updatecheck
    flags = updatecheck.get_flags()
    result, _ = list_stacks()
    due = [s for s in result if s["managed"] and flags.get(s["name"])]
    # Dockup is left out of the batch on purpose. Updating it replaces
    # the container running this very loop, so every stack after it in
    # the list would be skipped without anyone being told. It is updated
    # from its own card instead, which hands the restart to a helper and
    # says what is happening.
    skipped_self = [s["name"] for s in due if s.get("isSelf")]
    due = [s for s in due if not s.get("isSelf")]
    if not due and not skipped_self:
        return jsonify({"results": [], "updated": 0, "total": 0})
    rt = runtime.current()
    results = []
    for s in due:
        ok, message = _update_one(s["name"], stack_dir(s["name"]), rt)
        results.append({"name": s["name"], "ok": ok, "message": message})
    for name in skipped_self:
        results.append({"name": name, "ok": False,
                        "message": "Skipped: update Dockup from its own card"})
    updated = sum(1 for r in results if r["ok"])
    activity.log("info", "stack", f"Update all: {updated}/{len(results)} succeeded")
    return jsonify({"results": results, "updated": updated, "total": len(results)})


@bp.post("/stacks/check-updates")
def api_check_updates_now():
    """Manual override for the 30-minute background check - run one right
    now instead of waiting. Runs in the background since checking every
    stack means a real `docker compose pull` each, which can take a while."""
    from flask import current_app
    from . import updatecheck
    started = updatecheck.trigger_now(current_app._get_current_object())
    if not started:
        return jsonify({"ok": True, "started": False, "message": "A check is already running."})
    activity.log("info", "update-check", "Manual update check started")
    return jsonify({"ok": True, "started": True})


@bp.get("/stacks/check-updates/status")
def api_check_updates_status():
    from . import updatecheck
    return jsonify({"checking": updatecheck.is_checking()})


@bp.post("/stacks/<name>/check-update")
def api_check_update_one(name):
    """Force an update check for just this stack, from its own detail
    page - a real `docker compose pull` against this stack only, not
    the full sweep."""
    from . import updatecheck
    try:
        d = stack_dir(name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not d.exists():
        return jsonify({"error": f"'{name}' hasn't been adopted into the stacks folder yet"}), 404
    try:
        available = updatecheck.check_one(name)
    except runtime.RuntimeError_ as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True, "available": available})


@bp.get("/discover")
def api_discover():
    """What's running on this system that Dockup doesn't manage yet?"""
    projects, standalone, error = discover()
    if error:
        return jsonify({"error": error}), 502
    return jsonify({"projects": projects, "standalone": standalone})


def discover():
    """Everything running that Dockup doesn't manage yet, minus anything
    under an excluded path (another tool's territory - see settingsvc
    adopt.exclude_paths). Returns (projects, standalone, error)."""
    from . import settingsvc
    rt = runtime.current()
    try:
        containers = rt.ps()
    except runtime.RuntimeError_ as exc:
        return [], [], str(exc)
    managed = {d.name for d in config.STACKS_DIR.iterdir()
               if d.is_dir() and any((d / f).exists() for f in config.COMPOSE_FILENAMES)} \
        if config.STACKS_DIR.exists() else set()

    # Dockup has its own card on the dashboard already. It must never
    # turn up here as something to adopt either: adopting copies a
    # compose file into the stacks folder, and a second copy of Dockup's
    # own compose without its .env beside it would lose the one secret
    # it needs to start.
    own_project = own_compose_project(containers)

    exclude = [p.strip() for p in (settingsvc.get("adopt.exclude_paths") or "").split(",") if p.strip()]

    projects: dict[str, dict] = {}
    standalone = []
    for c in containers:
        if c["project"]:
            if c["project"] in managed or c["project"] == own_project:
                continue
            working_dir = c.get("workingDir", "")
            if any(working_dir.startswith(prefix) for prefix in exclude):
                continue
            entry = projects.setdefault(c["project"], {
                "name": c["project"],
                "workingDir": working_dir,
                "configFiles": c.get("configFiles", ""),
                "containers": [],
            })
            entry["containers"].append(c["name"])
        else:
            standalone.append({"name": c["name"], "image": c["image"], "state": c["state"]})
    for p in projects.values():
        cf = (p["configFiles"] or "").split(",")[0]
        p["fileReadable"] = bool(cf) and Path(cf).is_file()
    return list(projects.values()), standalone, None


def _adopt_one(kind, name, workingDir="", configFiles=""):
    """Core of adopting a single project or standalone container. Returns
    (result_dict, error_message) - exactly one is set."""
    rt = runtime.current()
    try:
        if kind == "project":
            target = stack_dir(_safe_project(name))
        else:
            target = stack_dir(composegen._safe(name).lower())
    except ValueError as exc:
        return None, str(exc)
    if any((target / f).exists() for f in config.COMPOSE_FILENAMES):
        return None, f"'{target.name}' already exists in the stacks folder"

    try:
        if kind == "project":
            config_file = (configFiles or "").split(",")[0]
            if config_file and Path(config_file).is_file():
                text = Path(config_file).read_text()
                text = composegen.rewrite_relative_binds(text, workingDir or str(Path(config_file).parent))
                note = "Adopted from its original compose file"
                env_src = Path(config_file).parent / ".env"
            else:
                containers = [c["name"] for c in rt.ps() if c["project"] == name]
                text = composegen.containers_to_compose(rt.inspect(containers))
                note = "Original file wasn't readable - rebuilt from the running containers"
                env_src = None
        elif kind == "container":
            text = composegen.containers_to_compose(rt.inspect([name]))
            note = "Rebuilt from the running container"
            env_src = None
        else:
            return None, "Unknown adopt type"
    except runtime.RuntimeError_ as exc:
        activity.log("error", "adopt", f"Adopting '{name}' FAILED", str(exc))
        return None, str(exc)

    problem = validate_compose(text)
    if problem:
        activity.log("error", "adopt", f"Adopting '{name}' FAILED", problem)
        return None, f"The rebuilt compose file didn't validate: {problem}"
    try:
        make_stack_dir(target)
        write_stack_file(target / "compose.yaml", text)
        if env_src and env_src.is_file():
            write_stack_file(target / ".env", env_src.read_text())
    except FileNotWritable as exc:
        activity.log("error", "adopt", f"Adopting '{name}' FAILED", str(exc))
        return None, str(exc)
    activity.log("info", "adopt", f"Adopted '{name}' into stacks/{target.name}", note)
    return {"ok": True, "name": target.name, "note": note}, None


@bp.post("/adopt")
def api_adopt():
    """Bring an existing compose project or standalone container under
    Dockup's wing: its compose file lands in the stacks folder."""
    data = request.get_json(force=True)
    result, error = _adopt_one(
        data.get("kind"), (data.get("name") or "").strip(),
        data.get("workingDir", ""), data.get("configFiles", ""),
    )
    if error:
        return jsonify({"error": error}), 400 if "already exists" in error or "Unknown" in error else 502
    return jsonify(result)


@bp.post("/adopt/all")
def api_adopt_all():
    """Adopt everything currently discoverable in one pass. Running two
    Docker managers pointed at the same containers can fight over the same
    files, so this only ever touches things nothing else is already
    managing (see adopt.exclude_paths in Settings)."""
    projects, standalone, error = discover()
    if error:
        return jsonify({"error": error}), 502
    results = []
    for p in projects:
        result, err = _adopt_one("project", p["name"], p.get("workingDir", ""), p.get("configFiles", ""))
        results.append({"name": p["name"], "ok": result is not None, "message": err or result["note"]})
    for c in standalone:
        result, err = _adopt_one("container", c["name"])
        results.append({"name": c["name"], "ok": result is not None, "message": err or result["note"]})
    ok_count = sum(1 for r in results if r["ok"])
    activity.log("info", "adopt", f"Bulk adopt: {ok_count}/{len(results)} succeeded")
    return jsonify({"results": results, "adopted": ok_count, "total": len(results)})


def _safe_project(name):
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.lower()).strip("-")
    return cleaned or "adopted"


@bp.post("/convert")
def api_convert():
    data = request.get_json(force=True)
    try:
        return jsonify({"compose": composeconv.docker_run_to_compose(data.get("command", ""))})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.post("/validate")
def api_validate():
    data = request.get_json(force=True)
    compose_text = data.get("compose", "")
    problem = validate_compose(compose_text)
    conflicts = []
    if problem is None:
        conflicts = check_port_conflicts(data.get("name", ""), compose_text, data.get("env", ""))
    return jsonify({"ok": problem is None, "error": problem, "portConflicts": conflicts})


@bp.get("/stacks/<name>/backups")
def api_stack_backups_list(name):
    from . import stackbackup
    try:
        stack_dir(name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"backups": stackbackup.list_backups(name)})


@bp.post("/stacks/<name>/backups")
def api_stack_backup_run(name):
    from . import stackbackup
    try:
        path = stackbackup.backup_stack(name)
    except (ValueError, runtime.RuntimeError_) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "name": path.name})


@bp.post("/stacks/<name>/backups/<backup_name>/restore")
def api_stack_backup_restore(name, backup_name):
    from . import stackbackup
    try:
        message = stackbackup.restore_stack(name, backup_name)
    except (ValueError, runtime.RuntimeError_) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "message": message})


_BACKUP_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.tar\.gz$")


@bp.get("/stacks/<name>/backups/<backup_name>/download")
def api_stack_backup_download(name, backup_name):
    if not _BACKUP_FILENAME_RE.match(backup_name):
        return jsonify({"error": "Not found"}), 404
    path = (config.STACK_BACKUP_DIR / backup_name).resolve()
    if path.parent != config.STACK_BACKUP_DIR.resolve() or not path.exists():
        return jsonify({"error": "Not found"}), 404
    from flask import send_file
    return send_file(path, as_attachment=True)


@bp.post("/stacks/<name>/backups/upload")
def api_stack_backup_upload(name):
    """Bring in a backup file from elsewhere - downloaded earlier, or
    moved from another machine - so it can be restored the same way as
    one Dockup made itself."""
    try:
        stack_dir(name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file received"}), 400
    if not f.filename.endswith(".tar.gz"):
        return jsonify({"error": "That doesn't look like a Dockup stack backup (.tar.gz)"}), 400
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = config.STACK_BACKUP_DIR / f"{name}-uploaded-{stamp}.tar.gz"
    f.save(dest)
    try:
        with tarfile.open(dest, "r:gz") as tar:
            if "./manifest.json" not in tar.getnames() and "manifest.json" not in tar.getnames():
                raise ValueError("missing manifest.json")
    except (tarfile.TarError, ValueError):
        dest.unlink(missing_ok=True)
        return jsonify({"error": "That file doesn't look like a Dockup stack backup"}), 400
    activity.log("info", "backup", f"Backup file uploaded for '{name}'", dest.name)
    return jsonify({"ok": True, "name": dest.name})
