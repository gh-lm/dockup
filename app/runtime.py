"""The engine layer. Everything Dockup does to containers goes through here.

Dockup talks to the Docker command interface, so one code path
serves both: the docker CLI (and its compose plugin) is pointed at whichever
socket the settings choose. Swapping engine = swapping socket path.

DOCKUP_MOCK=1 replaces the whole layer with an in-memory pretend engine so
the full UI can be exercised on a machine with no container runtime.
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from . import config, settingsvc

_DOCKER_BIN = shutil.which("docker") or "docker"

# The throwaway helper image every backup/restore/delete/install helper
# runs in. Pinned to a release line rather than the floating `latest`,
# so a changed tag on the registry can't silently swap in something
# else under a container that mounts host paths as root.
# Pinned by digest so every install runs the same helper. Alpine's own busybox
# provides everything the helpers use, so nothing is downloaded when one runs.
# To take a newer Alpine, change the digest on purpose.
HELPER_IMAGE = "alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8"

COMPOSE_ACTIONS = {
    "up": ["up", "-d", "--remove-orphans"],
    "down": ["down"],
    "stop": ["stop"],
    "start": ["start"],
    "restart": ["restart"],
    # Deliberately no --ignore-pull-failures here: a real pull failure
    # (bad tag, auth, registry down) should hard-stop Update rather than
    # silently redeploy the old image and report success. A stack whose
    # image genuinely isn't in a registry needs fixing at the source
    # (its own compose file), not tolerated here where it'd mask a real
    # failure for every other stack too.
    "pull": ["pull"],
    # Recreates every container from the compose file and whatever
    # image is already pulled - unlike restart (which just restarts the
    # same container process), this tears down and rebuilds it, which
    # is what actually clears a container stuck in a bad state that a
    # plain restart doesn't fix. No image pull, unlike Update.
    "redeploy": ["up", "-d", "--force-recreate", "--remove-orphans"],
    # For a stack that builds its own image rather than fetching one.
    # --pull so the base image underneath it is refreshed too, which is
    # where most of a rebuild's security value actually comes from.
    "build": ["build", "--pull"],
}

PRUNE_TARGETS = {
    "images": ["image", "prune", "-af"],
    "containers": ["container", "prune", "-f"],
    "networks": ["network", "prune", "-f"],
    # -a is required: since API 1.42 a bare `volume prune` removes only
    # anonymous volumes, so named ones were listed in the preview and
    # then silently left behind ("0B reclaimed").
    "volumes": ["volume", "prune", "-af"],
    "buildcache": ["builder", "prune", "-af"],
}


class RuntimeError_(Exception):
    pass


def current():
    if config.MOCK_MODE:
        from . import runtime_mock
        return runtime_mock.MockRuntime.instance()
    return Runtime(settingsvc.get("runtime.engine"), settingsvc.get("runtime.socket"))


class Runtime:
    def __init__(self, engine: str, socket_path: str):
        self.engine = engine
        self.socket_path = socket_path

    def _env(self):
        env = dict(os.environ)
        env["DOCKER_HOST"] = f"unix://{self.socket_path}"
        # Private-registry credentials (registries.py). Set here and in
        # _compose_env, which between them carry every docker and compose
        # call Dockup makes - pulls, Update, the scheduled new-image check,
        # the self-update - so all of them authenticate the same way.
        env["DOCKER_CONFIG"] = str(config.DOCKER_CONFIG_DIR)
        return env

    def _compose_env(self):
        """The environment `docker compose` runs with: only the few
        variables in config.COMPOSE_PASSTHROUGH, plus DOCKER_HOST.

        Compose substitutes a stack's compose.yaml variables from its .env
        file, but the environment it runs in always wins over that file. So
        every variable Dockup passes down silently overrides the stack's own
        value of the same name - Dockup's SECRET_KEY became the secret key
        of any stack referencing ${SECRET_KEY}, PATH broke stacks defining
        their own, and TZ would quietly reset a stack's timezone. An
        allowlist is the only version of this that stays fixed as Dockup
        gains environment variables of its own."""
        env = {k: v for k, v in os.environ.items() if k in config.COMPOSE_PASSTHROUGH}
        env["DOCKER_HOST"] = f"unix://{self.socket_path}"
        env["DOCKER_CONFIG"] = str(config.DOCKER_CONFIG_DIR)
        return env

    def registry_test(self, host: str, username: str, token: str) -> tuple[bool, str]:
        """Log in to a registry with these credentials and report whether it
        worked. Uses a throwaway config directory, so a failed test leaves
        the real saved credentials untouched, and the token goes in on
        stdin rather than the command line where `ps` could read it."""
        with tempfile.TemporaryDirectory(prefix="dockup-regtest-") as tmp:
            env = self._env()
            env["DOCKER_CONFIG"] = tmp
            try:
                proc = subprocess.run(
                    [_DOCKER_BIN, "login", host, "-u", username, "--password-stdin"],
                    input=token, capture_output=True, text=True, timeout=30, env=env)
            except FileNotFoundError:
                return False, "The docker CLI is not installed in the Dockup container"
            except subprocess.TimeoutExpired:
                return False, f"{host} didn't answer within 30 seconds."
        if proc.returncode == 0:
            return True, f"Signed in to {host}."
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else f"Couldn't sign in to {host}."

    def _run(self, args, timeout=60, cwd=None, compose=False):
        try:
            proc = subprocess.run(
                [_DOCKER_BIN, *args], capture_output=True, text=True,
                timeout=timeout, env=self._compose_env() if compose else self._env(), cwd=cwd,
            )
        except FileNotFoundError:
            raise RuntimeError_("The docker CLI is not installed in the Dockup container")
        except subprocess.TimeoutExpired:
            raise RuntimeError_(f"Command timed out: docker {' '.join(args[:3])}...")
        if proc.returncode != 0:
            raise RuntimeError_((proc.stderr or proc.stdout or "command failed").strip())
        return proc.stdout

    # -- status ----------------------------------------------------------

    def ping(self) -> dict:
        try:
            out = self._run(["version", "--format", "{{json .Server}}"], timeout=10)
            server = json.loads(out) or {}
            name = "Docker"
            return {"ok": True, "engine": name, "version": server.get("Version", "?")}
        except (RuntimeError_, json.JSONDecodeError) as exc:
            return {"ok": False, "engine": self.engine, "error": str(exc)}

    def ps(self) -> list[dict]:
        out = self._run(["ps", "-a", "--no-trunc", "--format", "{{json .}}"])
        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            labels = {}
            for pair in (c.get("Labels") or "").split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    labels[k] = v
            rows.append({
                "id": (c.get("ID") or "")[:12],
                "name": c.get("Names", ""),
                "image": c.get("Image", ""),
                "state": c.get("State", ""),
                "status": c.get("Status", ""),
                "project": labels.get("com.docker.compose.project", ""),
                "service": labels.get("com.docker.compose.service", ""),
                "workingDir": labels.get("com.docker.compose.project.working_dir", ""),
                "configFiles": labels.get("com.docker.compose.project.config_files", ""),
                "ports": c.get("Ports", ""),
            })
        return rows

    def inspect(self, names: list[str]) -> list[dict]:
        if not names:
            return []
        out = self._run(["inspect", *names], timeout=60)
        return json.loads(out)

    def remove_image(self, image: str):
        self._run(["rmi", image], timeout=60)

    def remove_volume(self, name: str):
        self._run(["volume", "rm", "-f", name], timeout=30)

    # -- compose ---------------------------------------------------------

    def compose_stream(self, stack_dir, project, action, extra_args=None, timeout=None):
        """Run a compose action, yielding output lines as they arrive.

        If timeout (seconds) is given and the process is still running
        when it elapses, it's killed and a [dockup-timeout] marker is
        yielded before the exit line - a container stuck because its
        bind-mount source vanished out from under it (e.g. deleted by
        hand while running) can hang `compose down` indefinitely
        otherwise, and a caller that needs this to always finish (like
        deleting a stack) can fall back to a more forceful cleanup on
        seeing that marker instead of hanging the whole request."""
        args = [_DOCKER_BIN, "compose", "-p", project, *COMPOSE_ACTIONS[action]]
        if extra_args:
            args += extra_args
        proc = subprocess.Popen(
            args, cwd=stack_dir, env=self._compose_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True,  # own process group, so a timeout kill
            # below can take out the whole tree - killing just the direct
            # child leaves any grandchild still holding the stdout pipe
            # open, which would keep this generator blocked forever
            # despite the "kill" appearing to have happened.
        )
        timed_out = [False]
        timer = None
        if timeout:
            def _on_timeout():
                timed_out[0] = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # already exited between the timer firing and this running
            timer = threading.Timer(timeout, _on_timeout)
            timer.start()
        try:
            for line in proc.stdout:
                yield line.rstrip("\n")
        finally:
            if timer:
                timer.cancel()
        proc.wait()
        if timed_out[0]:
            yield "[dockup-timeout]"
        yield f"[dockup-exit:{proc.returncode}]"

    def force_remove_containers(self, project: str):
        """Forcefully remove every container belonging to this compose
        project, bypassing graceful stop entirely - the fallback when
        `compose down` can't finish on its own. Also drops the
        project's default network, same as a normal `down` would."""
        names = [c["name"] for c in self.ps() if c["project"] == project]
        if names:
            self._run(["rm", "-f", *names], timeout=30)
        try:
            self._run(["network", "rm", f"{project}_default"], timeout=15)
        except RuntimeError_:
            pass  # never created, external, or already gone - all fine

    def logs_process(self, stack_dir, project, tail=200):
        """A Popen streaming `compose logs -f` for the websocket to relay."""
        return subprocess.Popen(
            [_DOCKER_BIN, "compose", "-p", project, "logs", "-f", "--tail", str(tail)],
            cwd=stack_dir, env=self._compose_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )

    def exec_to_file(self, container: str, command: list, out, timeout=1200,
                     max_bytes=None):
        """Run a command inside a container and stream its output into a
        file. Used for the before-backup dumps, where the application
        writes a consistent copy of itself to stdout and Dockup catches
        it. Streamed rather than captured: these archives can be large
        and holding one in memory to write it straight out again would
        be a waste at best. The error output goes to a scratch file and
        only its tail is read, and a dump that grows past max_bytes is
        stopped, so a runaway command cannot fill the disk or the memory."""
        args = [_DOCKER_BIN, "exec", container, *command]
        deadline = time.monotonic() + timeout
        with tempfile.TemporaryFile() as err_file:
            try:
                proc = subprocess.Popen(args, stdout=out, stderr=err_file, env=self._env())
            except FileNotFoundError:
                raise RuntimeError_("The docker CLI is not installed in the Dockup container")
            try:
                while True:
                    try:
                        proc.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if time.monotonic() > deadline:
                        proc.kill()
                        proc.wait()
                        raise RuntimeError_(f"The dump in '{container}' ran past its time limit")
                    if max_bytes and os.fstat(out.fileno()).st_size > max_bytes:
                        proc.kill()
                        proc.wait()
                        raise RuntimeError_(f"The dump in '{container}' grew past the "
                                            f"{max_bytes // 2**30} GB limit and was stopped")
            finally:
                if proc.poll() is None:
                    proc.kill()
            if proc.returncode != 0:
                err_file.seek(0, os.SEEK_END)
                err_file.seek(max(0, err_file.tell() - 4096))
                err = err_file.read().decode("utf-8", "replace").strip()
                raise RuntimeError_(err or f"The dump in '{container}' failed")

    def exec_argv(self, container: str) -> list[str]:
        """argv for an interactive shell in a container, run under a PTY."""
        return [
            _DOCKER_BIN, "exec", "-it", container,
            "sh", "-c", "command -v bash >/dev/null 2>&1 && exec bash || exec sh",
        ]

    def exec_env(self):
        return self._env()

    # -- maintenance -----------------------------------------------------

    def disk_usage(self) -> list[dict]:
        out = self._run(["system", "df", "--format", "{{json .}}"], timeout=120)
        rows = []
        for line in out.splitlines():
            if line.strip():
                d = json.loads(line)
                rows.append({
                    "type": d.get("Type", ""),
                    "total": d.get("TotalCount", ""),
                    "active": d.get("Active", ""),
                    "size": d.get("Size", ""),
                    "reclaimable": d.get("Reclaimable", ""),
                })
        return rows

    def dangling_volumes(self) -> list[dict]:
        """Unused volumes, with sizes so the confirmation step can show what
        is actually at stake. Names come from `volume ls` because that is the
        same set `volume prune -a` acts on; sizes are best-effort."""
        out = self._run(["volume", "ls", "-f", "dangling=true", "--format", "{{.Name}}"])
        names = [v for v in out.splitlines() if v.strip()]
        sizes = {}
        try:
            raw = self._run(["system", "df", "-v", "--format", "{{json .Volumes}}"], timeout=120)
            for vol in json.loads(raw or "[]"):
                sizes[vol.get("Name", "")] = vol.get("Size", "")
        except (RuntimeError_, ValueError):
            pass
        return [{"name": n, "size": sizes.get(n, "")} for n in names]

    def prune(self, target: str) -> str:
        out = self._run(PRUNE_TARGETS[target], timeout=600)
        for line in reversed(out.splitlines()):
            if "reclaimed" in line.lower():
                return line.strip()
        return "Nothing to remove"

    def check_stack_update(self, stack_dir, project) -> bool:
        """Quietly pull each service's image and compare against what's
        actually running. True if anything's out of date."""
        # --ignore-pull-failures: locally-built services (no registry image
        # to pull) shouldn't abort the update check for the rest of the
        # stack's services.
        self._run(["compose", "-p", project, "pull", "-q", "--ignore-pull-failures"],
                   timeout=300, cwd=stack_dir, compose=True)

        # `compose ps`'s "Image" column is not reliable here: for some images
        # it reports the digest the container was created from rather than the
        # tag, so it only ever compares against itself. The compose file is the
        # source of truth for what the tag means.
        #
        # Read as JSON, service by service. Zipping `config --services` against
        # `config --images` assumes the two lists come back in the same order,
        # which compose does not promise; a mismatch compares each service
        # against another service's image and flags the stack for ever.
        raw = self._run(["compose", "-p", project, "config", "--format", "json"],
                        timeout=30, cwd=stack_dir, compose=True)
        try:
            parsed = json.loads(raw).get("services") or {}
        except (json.JSONDecodeError, AttributeError) as exc:
            # Saying "no update" here would be a lie that never corrects
            # itself: a stack whose compose output cannot be read would
            # look up to date for ever.
            raise RuntimeError_(f"Could not read the services of '{project}' to check for "
                                f"updates ({exc})") from exc
        service_images = {name: svc.get("image") for name, svc in parsed.items()
                          if isinstance(svc, dict) and svc.get("image")}

        out = self._run(["compose", "-p", project, "ps", "-a", "--format", "{{json .}}"],
                        timeout=30, cwd=stack_dir, compose=True)
        for line in out.splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            name, service = c.get("Name"), c.get("Service")
            image_ref = service_images.get(service)
            if not name or not image_ref:
                continue
            try:
                running_id = self._run(["inspect", name, "--format", "{{.Image}}"], timeout=15).strip()
                latest_id = self._run(["image", "inspect", image_ref, "--format", "{{.Id}}"], timeout=15).strip()
            except RuntimeError_:
                continue
            if running_id and latest_id and running_id != latest_id:
                return True
        return False

    def force_remove_dir(self, parent_host_path: str, dirname: str):
        """Remove a stack's folder via a throwaway root container when
        Dockup's own non-root process can't - a real, hit-in-production
        case for stacks adopted from a previous manager (Arcane) that
        left the folder root-owned. /opt/stacks is mounted at the same
        path in both Dockup's container and the host, so no host-path
        translation is needed here unlike the backup helpers below."""
        self._run(["run", "--rm", "-v", f"{parent_host_path}:/target",
                   HELPER_IMAGE, "rm", "-rf", f"/target/{dirname}"], timeout=60)

    def rmdir_if_empty(self, parent_host_path: str, dirname: str):
        """Remove a directory only if it's genuinely empty - `rmdir`
        fails safely otherwise, unlike force_remove_dir's rm -rf. Used
        after deleting a stack's declared data mounts, to clean up the
        now-likely-empty parent (e.g. /opt/<stack>) those mounts lived
        under - otherwise it's left behind as an empty husk, which
        Docker itself creates as a side effect of bind-mounting a path
        that didn't exist yet (a real case: the mount's own parent can
        vanish - deleted by hand, or never created - between when the
        stack stopped and when this cleanup runs)."""
        self._run(["run", "--rm", "-v", f"{parent_host_path}:/target",
                   HELPER_IMAGE, "rmdir", f"/target/{dirname}"], timeout=30)

    # -- per-stack data backup/restore ------------------------------------
    # A short-lived helper container does the actual file access, mounting
    # the source and Dockup's own backup folder side by side - the daemon
    # resolves both against the real host filesystem, so this reaches
    # bind-mount paths Dockup's own container can't see directly, and
    # named volumes wherever Docker actually stores them. Nothing is ever
    # relocated - everything restores to exactly the path it came from.

    def _require_host_path(self):
        if not config.DATA_HOST_PATH:
            raise RuntimeError_(
                "DOCKUP_DATA_HOST_PATH isn't set, so Dockup doesn't know its own "
                "real path on the host - see the runbook to set it in compose.yaml."
            )
        return f"{config.DATA_HOST_PATH.rstrip('/')}/stack-backups"

    def archive_path_to_backup(self, host_source: str, dest_filename: str):
        dest_host_dir = self._require_host_path()
        self._run(["run", "--rm", "-v", f"{host_source}:/src:ro", "-v", f"{dest_host_dir}:/dest",
                   HELPER_IMAGE, "tar", "czf", f"/dest/{dest_filename}", "-C", "/src", "."], timeout=900)

    def archive_volume_to_backup(self, volume_name: str, dest_filename: str):
        dest_host_dir = self._require_host_path()
        self._run(["run", "--rm", "-v", f"{volume_name}:/src:ro", "-v", f"{dest_host_dir}:/dest",
                   HELPER_IMAGE, "tar", "czf", f"/dest/{dest_filename}", "-C", "/src", "."], timeout=900)

    def restore_path_from_backup(self, host_dest: str, src_filename: str):
        dest_host_dir = self._require_host_path()
        # No shell: src_filename comes from an uploaded manifest, and a
        # bind mount's source directory is created by the daemon anyway
        # when it doesn't exist yet, so tar can run as a plain argv.
        self._run(["run", "--rm", "-v", f"{host_dest}:/dest", "-v", f"{dest_host_dir}:/src:ro",
                   HELPER_IMAGE, "tar", "xzf", f"/src/{src_filename}", "-C", "/dest"], timeout=900)

    def restore_volume_from_backup(self, volume_name: str, src_filename: str):
        dest_host_dir = self._require_host_path()
        self._run(["run", "--rm", "-v", f"{volume_name}:/dest", "-v", f"{dest_host_dir}:/src:ro",
                   HELPER_IMAGE, "tar", "xzf", f"/src/{src_filename}", "-C", "/dest"], timeout=900)

    # -- one-click companion install --------------------------------------
    # `install.sh` writes to /etc/systemd/system, creates a group, and runs
    # `systemctl enable --now`: real root-on-host actions. A chroot is not
    # enough, because systemd checks the caller's actual PID namespace and
    # chroot only changes how paths resolve. So the helper joins the host's
    # namespaces from inside a privileged container with --pid=host.
    # Still a single short-lived container, with nothing left running
    # afterwards.

    def install_companion_stream(self, staging_host_dir: str):
        args = [
            _DOCKER_BIN, "run", "--rm", "--privileged", "--pid=host",
            "-v", "/:/host", "-v", f"{staging_host_dir}:/staging:ro",
            HELPER_IMAGE, "sh", "-c",
            # A fresh private folder (mode 700, a name nobody can guess), so
            # another user on the host cannot pre-create it or swap the
            # script between the copy and the run.
            'd=$(mktemp -d /host/tmp/dockup-companion.XXXXXX) && '
            'cp /staging/dockup-companion.py /staging/dockup-companion.service /staging/install.sh "$d"/ && '
            'chmod +x "$d/install.sh" && '
            'nsenter -t 1 -m -u -i -n -p -- sh "${d#/host}/install.sh"; '
            'status=$?; rm -rf "$d"; exit $status',
        ]
        proc = subprocess.Popen(args, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            yield line.rstrip("\n")
        proc.wait()
        yield f"[dockup-exit:{proc.returncode}]"

    def reconnect_companion_stream(self, compose_host_path: str, compose_host_dir: str):
        """Uncomment the companion socket line in Dockup's own compose.yaml
        and run `docker compose up -d` for real, via the host's own
        namespaces - not a bind-mounted view from inside Dockup's own
        container, where compose's relative-path resolution (./data
        etc) would produce the wrong absolute host paths. This
        recreates Dockup's own container, so the stream (and the HTTP
        request carrying it) ends abruptly partway through - expected,
        not a failure. The daemon completes the restart independently
        of whether anything is still reading this output."""
        compose_file = shlex.quote('/host' + compose_host_path)
        sock_line = "- /run/dockup-companion.sock:/run/dockup-companion.sock"
        # The line has to be there afterwards, whether it was just
        # uncommented or already was. If it is missing altogether, say so
        # instead of recreating Dockup with nothing changed.
        script = (
            f"sed -i 's|# {sock_line}|{sock_line}|' {compose_file} && "
            f"{{ grep -q '^ *{sock_line}' {compose_file} || "
            "{ echo 'The companion line is not in the compose file, so nothing was changed.'; "
            "exit 1; }; } && "
            "nsenter -t 1 -m -u -i -n -p -- "
            f"sh -c 'cd {shlex.quote(compose_host_dir)} && docker compose config --quiet' && "
            "nsenter -t 1 -m -u -i -n -p -- "
            f"sh -c 'cd {shlex.quote(compose_host_dir)} && docker compose up -d'"
        )
        args = [_DOCKER_BIN, "run", "--rm", "--privileged", "--pid=host", "-v", "/:/host",
                HELPER_IMAGE, "sh", "-c", script]
        proc = subprocess.Popen(args, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            yield line.rstrip("\n")
        proc.wait()
        yield f"[dockup-exit:{proc.returncode}]"

    # -- restarting Dockup itself -----------------------------------------
    # Dockup cannot recreate its own container from inside it: `compose up`
    # stops that container, which kills the process running the command, so
    # the "start it again" half never happens. The work is done from a
    # short-lived container that is not the one being replaced, entering the
    # host's namespaces as the companion install does.

    def _host_nsenter(self, inner_script: str) -> str:
        return ("nsenter -t 1 -m -u -i -n -p -- "
                f"sh -c {shlex.quote(inner_script)}")

    def self_recreate_stream(self, compose_host_dir: str):
        """Bring Dockup's own stack back up from its compose file.

        Dockup is a container like any other, but it cannot recreate
        itself: the moment `docker compose up` replaces its container,
        the process running that command is gone. So this one job is
        handed to a short-lived helper container in the host's own
        namespaces, which survives Dockup going away and finishes the
        work. The stream ends abruptly partway through - that is the
        container being replaced, not a failure."""
        d = shlex.quote(compose_host_dir)
        script = " && ".join([
            self._host_nsenter(f"cd {d} && docker compose config --quiet"),
            self._host_nsenter(f"cd {d} && docker compose up -d"),
        ])
        args = [_DOCKER_BIN, "run", "--rm", "--privileged", "--pid=host", "-v", "/:/host",
                HELPER_IMAGE, "sh", "-c", script]
        proc = subprocess.Popen(args, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            yield line.rstrip("\n")
        proc.wait()
        yield f"[dockup-exit:{proc.returncode}]"
