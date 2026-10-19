"""Client for the optional dockup-companion host service (see companion/
in the repo) - the narrow, fixed-command-set helper that runs directly
on the host for the two things the Docker socket alone can't reach:
host OS updates and Tailscale Serve. Entirely optional; every call here
reports a plain "not available" rather than erroring if it isn't
installed.
"""

import json
import socket

import yaml

from . import config, envsub

SOCKET_PATH = "/run/dockup-companion.sock"


class CompanionUnavailable(Exception):
    pass


def _call(cmd: str, timeout=20, **kwargs) -> dict:
    if config.MOCK_MODE:
        return _mock_call(cmd, **kwargs)
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCKET_PATH)
    except OSError as exc:
        raise CompanionUnavailable(
            "dockup-companion isn't reachable - not installed, or its "
            "socket isn't mounted into Dockup's container yet. See the runbook."
        ) from exc
    try:
        payload = {"cmd": cmd, **kwargs}
        s.sendall((json.dumps(payload) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        if not data:
            raise CompanionUnavailable("dockup-companion closed the connection without responding")
        return json.loads(data.decode())
    except (OSError, json.JSONDecodeError) as exc:
        raise CompanionUnavailable(f"dockup-companion didn't respond properly: {exc}") from exc
    finally:
        s.close()


def is_available() -> bool:
    try:
        return bool(_call("ping", timeout=3).get("ok"))
    except CompanionUnavailable:
        return False


def os_info() -> dict:
    return _call("os_info")


def os_update_check() -> dict:
    return _call("os_update_check", timeout=120)


def os_update_apply() -> dict:
    return _call("os_update_apply", timeout=1800)


def tailscale_status() -> dict:
    return _call("tailscale_status", timeout=15)


def tailscale_install() -> dict:
    return _call("tailscale_install", timeout=300)


def tailscale_serve(port: int, on: bool) -> dict:
    return _call("tailscale_serve", port=port, on=on)


def tailscale_serve_list() -> dict:
    return _call("tailscale_serve_list", timeout=15)


def reboot() -> dict:
    return _call("reboot", timeout=15)


def docker_restart() -> dict:
    return _call("docker_restart", timeout=70)


def published_ports(compose_text: str, env_text: str = "") -> list:
    """Host-side ports a stack publishes, straight off its own `ports:`
    list - what Tailscale Serve would actually front."""
    seen, ports = set(), []
    for _ip, port in published_bindings(compose_text, env_text):
        if port not in seen:
            seen.add(port)
            ports.append(port)
    return ports


def published_bindings(compose_text: str, env_text: str = "") -> list:
    """(host address, port) for everything a stack publishes. The address
    is "" when the compose file names none, which means every interface -
    the case that stops Tailscale Serve getting a certificate for that
    port, since Docker's wildcard bind covers the tailnet address too.
    Resolves ${VAR}-style ports (e.g. stirling-pdf's "${PORT}:8080")
    against the stack's .env, the same convention Arcane-managed stacks
    use for bind-mount paths elsewhere."""
    try:
        doc = yaml.safe_load(compose_text) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    env = envsub.parse_env(env_text)
    out, seen = [], set()
    for svc in (doc.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        for p in svc.get("ports") or []:
            host_port, host_ip = None, ""
            if isinstance(p, dict):
                host_port = p.get("published")
                host_ip = str(p.get("host_ip") or "")
            elif isinstance(p, (str, int)):
                left = envsub.substitute(str(p), env).split("/")[0].split(":")
                # "80" (no colon) publishes on the same port as target;
                # "8080:80" or "127.0.0.1:8080:80" - host port is the
                # second-to-last segment, and anything before it is the
                # address. An IPv6 literal is bracketed, so the only
                # colons that survive this split belong to the mapping.
                host_port = left[-2] if len(left) > 1 else left[0]
                if len(left) > 2:
                    host_ip = ":".join(left[:-2]).strip("[]")
            try:
                port = int(host_port)
            except (TypeError, ValueError):
                continue
            if (host_ip, port) not in seen:
                seen.add((host_ip, port))
                out.append((host_ip, port))
    return out


# -- mock support, for development without the companion installed -----

def _mock_call(cmd: str, **kwargs) -> dict:
    if cmd == "ping":
        return {"ok": True, "version": "mock"}
    if cmd == "os_info":
        return {"ok": True, "id": "debian", "name": "Debian GNU/Linux 12 (mock)", "supported": True}
    if cmd == "os_update_check":
        return {"ok": True, "upgradable": 4, "packages": ["libc6/stable 2.36 amd64", "openssl/stable 3.0.11 amd64"]}
    if cmd == "os_update_apply":
        return {"ok": True, "message": "Host packages updated (mock).", "log": "(mock) 4 packages upgraded."}
    if cmd == "tailscale_status":
        return {"ok": True, "installed": True, "running": True,
                "dnsName": "server.example.ts.net", "backendState": "Running"}
    if cmd == "tailscale_install":
        return {"ok": True, "message": "Tailscale installed (mock)."}
    if cmd == "tailscale_serve":
        port, on = kwargs.get("port"), kwargs.get("on")
        if on and port not in _mock_call.served_ports:
            _mock_call.served_ports.append(port)
        elif not on and port in _mock_call.served_ports:
            _mock_call.served_ports.remove(port)
        return {"ok": True, "message": f"Serve {'enabled' if on else 'disabled'} for port {port} (mock)."}
    if cmd == "tailscale_serve_list":
        return {"ok": True, "installed": True, "ports": list(_mock_call.served_ports)}
    if cmd == "reboot":
        return {"ok": True, "message": "Rebooting now (mock)."}
    if cmd == "docker_restart":
        return {"ok": True, "message": "Docker restarted (mock)."}
    return {"ok": False, "error": "Unknown command"}


_mock_call.served_ports = []
