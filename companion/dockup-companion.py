#!/usr/bin/env python3
"""dockup-companion: a small, narrowly-scoped root-level helper that runs
directly on the host (never in a container), so Dockup can do the two
things Docker access alone can't reach: check/apply host OS updates,
and manage Tailscale Serve.

Deliberately NOT a general remote-shell. Every request is one of a
fixed, hardcoded set of commands below - there is no "run this string"
command, and there never should be. Each command is a small, explicit
function that builds its own argv list; nothing here is ever passed
through a shell.

Talks newline-delimited JSON over a Unix socket. One request per
connection: read one line, write one line, close.
"""

import json
import os
import re
import socketserver
import subprocess

SOCKET_PATH = "/run/dockup-companion.sock"
VERSION = "1.0.0"


def cmd_ping(_req):
    return {"ok": True, "version": VERSION}


def cmd_os_info(_req):
    info = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if "=" in line:
                    k, _, v = line.strip().partition("=")
                    info[k] = v.strip('"')
    except OSError:
        pass
    os_id = info.get("ID", "")
    return {"ok": True, "id": os_id, "name": info.get("PRETTY_NAME", os_id),
            "supported": os_id in ("debian", "ubuntu")}


def cmd_os_update_check(_req):
    info = cmd_os_info(_req)
    if not info["supported"]:
        return {"ok": False, "error": f"Host updates aren't supported on '{info['id'] or 'this OS'}' - Debian/Ubuntu only."}
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    proc = subprocess.run(["apt-get", "update"], capture_output=True, text=True, timeout=180, env=env)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:400] or "apt-get update failed"}
    proc = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=60, env=env)
    lines = [l for l in proc.stdout.splitlines() if l and not l.startswith("Listing")]
    return {"ok": True, "upgradable": len(lines), "packages": lines[:50]}


def cmd_os_update_apply(_req):
    info = cmd_os_info(_req)
    if not info["supported"]:
        return {"ok": False, "error": f"Host updates aren't supported on '{info['id'] or 'this OS'}' - Debian/Ubuntu only."}
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    proc = subprocess.run(["apt-get", "update"], capture_output=True, text=True, timeout=180, env=env)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:400] or "apt-get update failed"}
    proc = subprocess.run(["apt-get", "-y", "upgrade"], capture_output=True, text=True, timeout=1800, env=env)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:800] or "apt-get upgrade failed"}
    return {"ok": True, "message": "Host packages updated.", "log": proc.stdout[-2000:]}


def _tailscale_installed():
    return subprocess.run(["which", "tailscale"], capture_output=True).returncode == 0


def cmd_tailscale_status(_req):
    if not _tailscale_installed():
        return {"ok": True, "installed": False}
    proc = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        return {"ok": True, "installed": True, "running": False, "error": proc.stderr.strip()[:300]}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": True, "installed": True, "running": False}
    self_info = data.get("Self", {})
    dns_name = (self_info.get("DNSName") or "").rstrip(".")
    return {"ok": True, "installed": True, "running": bool(data.get("BackendState") == "Running"),
            "dnsName": dns_name, "backendState": data.get("BackendState", "")}


def cmd_tailscale_install(_req):
    if _tailscale_installed():
        return {"ok": True, "message": "Tailscale is already installed."}
    # Tailscale's own official installer - one well-known trusted source,
    # no arbitrary URL ever accepted from a request.
    proc = subprocess.run(["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:800] or "Install script failed"}
    return {"ok": True, "message": "Tailscale installed. Run 'tailscale up' on the host once to authenticate it."}


def cmd_tailscale_serve_list(_req):
    if not _tailscale_installed():
        return {"ok": True, "installed": False, "ports": []}
    proc = subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        return {"ok": True, "installed": True, "ports": []}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": True, "installed": True, "ports": []}
    ports = []
    for web_key in (data.get("Web") or {}):
        # keys look like "hostname:443", the served HTTPS port
        try:
            ports.append(int(web_key.rsplit(":", 1)[1]))
        except (ValueError, IndexError):
            pass
    return {"ok": True, "installed": True, "ports": sorted(set(ports))}


_PORT_RE = re.compile(r"^\d{2,5}$")


def cmd_tailscale_serve(req):
    if not _tailscale_installed():
        return {"ok": False, "error": "Tailscale isn't installed on this host yet."}
    port = str(req.get("port", ""))
    if not _PORT_RE.match(port) or not (1 <= int(port) <= 65535):
        return {"ok": False, "error": "Invalid port"}
    on = bool(req.get("on"))
    if on:
        argv = ["tailscale", "serve", "--bg", f"--https={port}", f"http://127.0.0.1:{port}"]
    else:
        argv = ["tailscale", "serve", f"--https={port}", "off"]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:400] or "tailscale serve failed"}
    return {"ok": True, "message": f"Serve {'enabled' if on else 'disabled'} for port {port}."}


def cmd_reboot(_req):
    # systemctl reboot returns as soon as it's handed the request to
    # PID 1 - the actual shutdown happens a moment later, so there's
    # time for this response to reach Dockup before the host goes down.
    proc = subprocess.run(["systemctl", "reboot"], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:400] or "systemctl reboot failed"}
    return {"ok": True, "message": "Rebooting now."}


def cmd_docker_restart(_req):
    # Containers with restart:unless-stopped (including Dockup itself)
    # stay up through this - modern Docker keeps containerd running
    # independently of the dockerd/API layer being restarted.
    proc = subprocess.run(["systemctl", "restart", "docker"], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:400] or "systemctl restart docker failed"}
    return {"ok": True, "message": "Docker restarted."}


COMMANDS = {
    "ping": cmd_ping,
    "os_info": cmd_os_info,
    "os_update_check": cmd_os_update_check,
    "os_update_apply": cmd_os_update_apply,
    "tailscale_status": cmd_tailscale_status,
    "tailscale_install": cmd_tailscale_install,
    "tailscale_serve": cmd_tailscale_serve,
    "tailscale_serve_list": cmd_tailscale_serve_list,
    "reboot": cmd_reboot,
    "docker_restart": cmd_docker_restart,
}


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            data = b""
            while not data.endswith(b"\n"):
                chunk = self.request.recv(65536)
                if not chunk:
                    return
                data += chunk
                if len(data) > 1_000_000:
                    return
            req = json.loads(data.decode())
            fn = COMMANDS.get(req.get("cmd", ""))
            resp = fn(req) if fn else {"ok": False, "error": "Unknown command"}
        except Exception as exc:
            resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.request.sendall((json.dumps(resp) + "\n").encode())


class UnixSocketServer(socketserver.UnixStreamServer):
    allow_reuse_address = True


def main():
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    # group-readable/writable only - the group is what Dockup's
    # container joins at startup (matches the docker.sock pattern).
    # The umask is set before the socket is created so it is never
    # world-accessible, not even for the instant before the chmod.
    os.umask(0o117)
    server = UnixSocketServer(SOCKET_PATH, Handler)
    os.chmod(SOCKET_PATH, 0o660)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    main()
