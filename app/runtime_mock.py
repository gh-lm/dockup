"""Pretend engine for development machines with no container runtime.

Activated by DOCKUP_MOCK=1. Stacks still live as real compose files on
disk, so the editor, converter, backups and file handling are exercised
for real - only the engine responses are simulated.
"""

import random
import threading
import time

_random = random.Random(42)


class _FakeLogsProc:
    """Mimics the bits of Popen the log streamer uses."""

    def __init__(self, project):
        self.project = project
        self._lines = self._generator()
        self.stdout = self
        self._alive = True

    def _generator(self):
        services = ["web", "db"]
        n = 0
        while True:
            n += 1
            svc = _random.choice(services)
            if n % 9 == 0:
                yield f"{svc}-1  | ERROR: connection refused while talking to upstream (attempt {n})"
            elif n % 5 == 0:
                yield f"{svc}-1  | WARN: slow query took 1512ms"
            else:
                yield f'{svc}-1  | 192.168.1.{_random.randint(2, 254)} - "GET /api/item/{n} HTTP/1.1" 200'

    def __iter__(self):
        return self

    def __next__(self):
        if not self._alive:
            raise StopIteration
        time.sleep(_random.uniform(0.2, 0.9))
        return next(self._lines) + "\n"

    def terminate(self):
        self._alive = False

    kill = terminate

    def wait(self, timeout=None):
        return 0


class MockRuntime:
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self.engine = "docker"
        self.socket_path = "/var/run/docker.sock (mock)"
        # project -> state
        self.states = {}

    def ping(self):
        return {"ok": True, "engine": "Docker (mock)", "version": "27.0-mock"}

    def registry_test(self, host, username, token):
        # "bad" as the token stands in for a rejected login, so the
        # failure path can be seen in dev mode without a real registry.
        time.sleep(0.4)
        if token == "bad":
            return False, "Error response from daemon: unauthorized (mock)"
        return True, f"Signed in to {host} (mock)."

    def ps(self):
        rows = []
        for project, state in self.states.items():
            for svc in ["web", "db"]:
                rows.append({
                    "id": f"{abs(hash(project + svc)) % 10**12:012x}"[:12],
                    "name": f"{project}-{svc}-1",
                    "image": "nginx:alpine" if svc == "web" else "postgres:16-alpine",
                    "state": state,
                    "status": "Up 2 hours" if state == "running" else "Exited (0) 10 minutes ago",
                    "project": project,
                    "service": svc,
                    "workingDir": "", "configFiles": "", "ports": "",
                })
        # Dockup's own container, so the dashboard's Dockup card has
        # something real to show in dev mode too.
        rows.append({
            "id": "0d0ckle00001", "name": "dockup",
            "image": "dockup:latest",
            "state": self.states.get("dockup", "running"),
            "status": "Up 2 hours", "project": "dockup", "service": "dockup",
            "workingDir": "/opt/dockup", "configFiles": "/opt/dockup/compose.yaml",
            "ports": "127.0.0.1:4000->5001/tcp",
        })
        if not self.adopted("homeassistant"):
            rows.append({
                "id": "aa11bb22cc33", "name": "homeassistant-app-1",
                "image": "homeassistant/home-assistant:stable", "state": "running",
                "status": "Up 3 days", "project": "homeassistant", "service": "app",
                "workingDir": "/srv/homeassistant", "configFiles": "/srv/homeassistant/docker-compose.yml",
            })
        if not self.adopted("jellyfin"):
            rows.append({
                "id": "dd44ee55ff66", "name": "jellyfin",
                "image": "lscr.io/linuxserver/jellyfin:latest", "state": "running",
                "status": "Up 5 days", "project": "", "service": "",
                "workingDir": "", "configFiles": "",
            })
        return rows

    @staticmethod
    def adopted(name):
        from . import config
        return any((config.STACKS_DIR / name / f).exists()
                   for f in config.COMPOSE_FILENAMES)

    def inspect(self, names):
        out = []
        for name in names:
            out.append({
                "Name": f"/{name}",
                "Image": "sha256:abc",
                "Config": {
                    "Image": "lscr.io/linuxserver/jellyfin:latest",
                    "Env": ["PUID=1000", "PGID=1000", "TZ=Europe/London"],
                    "Labels": {},
                    "WorkingDir": "",
                    "User": "",
                },
                "HostConfig": {
                    "RestartPolicy": {"Name": "unless-stopped"},
                    "PortBindings": {"8096/tcp": [{"HostIp": "", "HostPort": "8096"}]},
                    "NetworkMode": "bridge",
                },
                "Mounts": [
                    {"Type": "bind", "Source": "/srv/jellyfin/config",
                     "Destination": "/config", "RW": True},
                    {"Type": "volume", "Name": "media-cache", "Destination": "/cache", "RW": True},
                ],
            })
        return out

    def remove_image(self, image):
        pass

    def remove_volume(self, name):
        pass

    def compose_stream(self, stack_dir, project, action, extra_args=None, timeout=None):
        steps = {
            "up": ["Network created", "Container web-1  Started", "Container db-1  Started"],
            "down": ["Container web-1  Removed", "Container db-1  Removed", "Network removed"],
            "stop": ["Container web-1  Stopped", "Container db-1  Stopped"],
            "start": ["Container web-1  Started", "Container db-1  Started"],
            "restart": ["Container web-1  Restarted", "Container db-1  Restarted"],
            "redeploy": ["Container web-1  Recreated", "Container db-1  Recreated"],
            "pull": ["web Pulling", "db Pulling", "web Pulled", "db Pulled"],
        }[action]
        for line in steps:
            time.sleep(0.4)
            yield f" {project}: {line}"
        if action in ("up", "start", "restart", "redeploy"):
            self.states[project] = "running"
        elif action in ("down",):
            self.states.pop(project, None)
        elif action == "stop":
            self.states[project] = "exited"
        yield "[dockup-exit:0]"

    def force_remove_containers(self, project):
        self.states.pop(project, None)

    def logs_process(self, stack_dir, project, tail=200):
        return _FakeLogsProc(project)

    def exec_argv(self, container):
        # a real local shell so the terminal is genuinely interactive in dev
        return ["sh", "-i"]

    def exec_env(self):
        import os
        return dict(os.environ)

    def disk_usage(self):
        return [
            {"type": "Images", "total": "12", "active": "5", "size": "3.4GB", "reclaimable": "1.9GB (55%)"},
            {"type": "Containers", "total": "6", "active": "4", "size": "312MB", "reclaimable": "108MB (34%)"},
            {"type": "Local Volumes", "total": "9", "active": "4", "size": "1.2GB", "reclaimable": "460MB (38%)"},
            {"type": "Build Cache", "total": "24", "active": "0", "size": "780MB", "reclaimable": "780MB"},
        ]

    def dangling_volumes(self):
        return [{"name": "old-wordpress_db-data", "size": "412MB"},
                {"name": "test-stack_cache", "size": "38.1MB"},
                {"name": "temp_build_scratch", "size": "0B"}]

    def prune(self, target):
        time.sleep(1.2)
        amounts = {"images": "1.9GB", "containers": "108MB", "networks": "0B",
                   "volumes": "460MB", "buildcache": "780MB"}
        return f"Total reclaimed space: {amounts[target]}"


    def check_stack_update(self, stack_dir, project) -> bool:
        # deterministic per project name, so the dashboard has something
        # to show in mock mode without needing a real registry
        time.sleep(0.3)
        return abs(hash(project)) % 3 == 0

    def rmdir_if_empty(self, parent_host_path, dirname):
        pass

    def force_remove_dir(self, parent_host_path, dirname):
        import shutil as _shutil
        from pathlib import Path as _Path
        _shutil.rmtree(_Path(parent_host_path) / dirname, ignore_errors=True)

    def archive_path_to_backup(self, host_source, dest_filename):
        import tarfile
        from . import config
        with tarfile.open(config.STACK_BACKUP_DIR / dest_filename, "w:gz"):
            pass  # empty archive - mock has no real bind-mount data to read

    def archive_volume_to_backup(self, volume_name, dest_filename):
        self.archive_path_to_backup(volume_name, dest_filename)

    def restore_path_from_backup(self, host_dest, src_filename):
        pass

    def restore_volume_from_backup(self, volume_name, src_filename):
        pass

    def install_companion_stream(self, staging_host_dir):
        yield "Installing dockup-companion (mock)..."
        yield "dockup-companion installed and running (mock)."
        yield "[dockup-exit:0]"

    def reconnect_companion_stream(self, compose_host_path, compose_host_dir):
        yield "Editing compose.yaml (mock)..."
        yield "Validating config (mock)..."
        yield "dockup-companion reconnected (mock) - no real restart in dev mode."
        yield "[dockup-exit:0]"

    _mock_updated = False

    def exec_to_file(self, container, command, out, timeout=1200):
        out.write(f"(mock) dump of {container} by {' '.join(command)}\n".encode())

    def self_recreate_stream(self, compose_host_dir):
        yield "(mock) docker compose up -d"
        time.sleep(0.3)
        yield " dockup: Container dockup  Recreated"
        self._mock_updated = True
        yield "[dockup-exit:0]"
