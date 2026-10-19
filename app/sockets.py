"""Websockets: live compose logs and the interactive terminal.

Both check the login session before doing anything - a websocket is a
door too. Browsers don't apply same-origin protection to WebSockets the
way they do to normal requests, so a same-origin check is done here
explicitly: without it, a page on another site could open one of these
connections in a visitor's browser and ride their session cookie in.
"""

import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
from urllib.parse import urlparse

from flask import request, session
from flask_sock import Sock

from . import runtime, stacks

sock = Sock()

# Docker's own container-name rule: must start alphanumeric, so a value
# like "--help" can never be read as a flag by `docker logs`/`docker exec`.
_CONTAINER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def _authed():
    # Mirror require_login's HTTP check: a session for a since-deleted
    # user must not keep a socket open either.
    from . import auth
    return bool(session.get("uid")) and auth.current_user() is not None


def _same_origin():
    origin = request.headers.get("Origin")
    if not origin:
        return True  # non-browser clients don't send one; nothing to spoof
    return urlparse(origin).netloc == request.host


def _relay(ws, proc):
    """Pump a log process's output down the socket until either end goes
    away, then make sure the process is stopped - a reader that quietly
    stayed alive after its browser tab closed would keep a `logs -f`
    running forever."""
    stop = threading.Event()

    def watch_client():
        # receive() returns None when the client goes away
        try:
            while ws.receive() is not None:
                pass
        except Exception:
            pass
        stop.set()
        try:
            proc.terminate()
        except Exception:
            pass

    threading.Thread(target=watch_client, daemon=True).start()
    try:
        for line in proc.stdout:
            if stop.is_set():
                break
            ws.send(line.rstrip("\n"))
    except Exception:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


@sock.route("/ws/logs/<name>")
def ws_logs(ws, name):
    if not _authed() or not _same_origin():
        ws.close()
        return
    try:
        d = stacks.stack_dir(name)
    except ValueError:
        ws.close()
        return
    if not d.exists():
        ws.send(f"'{name}' hasn't been adopted into the stacks folder yet - nothing to stream.")
        ws.close()
        return
    rt = runtime.current()
    try:
        proc = rt.logs_process(str(d), name)
    except OSError:
        ws.send("Could not start the log stream for this stack.")
        ws.close()
        return
    _relay(ws, proc)


@sock.route("/ws/terminal/<container>")
def ws_terminal(ws, container):
    if not _authed() or not _same_origin():
        ws.close()
        return
    if not _CONTAINER_RE.match(container):
        ws.close()
        return
    rt = runtime.current()
    argv = rt.exec_argv(container)

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        argv, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        env=rt.exec_env(), preexec_fn=os.setsid, close_fds=True,
    )
    os.close(slave_fd)
    stop = threading.Event()

    def pump_output():
        try:
            while not stop.is_set():
                r, _w, _x = select.select([master_fd], [], [], 0.5)
                if master_fd in r:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    ws.send(data.decode(errors="replace"))
        except Exception:
            pass
        finally:
            stop.set()
            try:
                ws.close()
            except Exception:
                pass

    threading.Thread(target=pump_output, daemon=True).start()
    try:
        while not stop.is_set():
            msg = ws.receive()
            if msg is None:
                break
            if msg.startswith("\x00resize:"):
                try:
                    cols, rows = msg[8:].split("x")
                    winsize = struct.pack("HHHH", int(rows), int(cols), 0, 0)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                except (ValueError, OSError):
                    pass
            else:
                os.write(master_fd, msg.encode())
    except Exception:
        pass
    finally:
        stop.set()
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
