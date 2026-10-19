"""Things that must never run twice at once.

Dockup runs as a single process with many threads, so a plain in-memory
set is enough. Two overlapping requests to restart Dockup would each start
a privileged helper racing on the same `compose up`, a backup running
while a restore deletes the files it is reading would archive half a
stack, and a scheduled and a manual backup in the same second used to
share one partial file name.

Everything here is non-blocking: if something is already running the
answer is "already running", said plainly, not a queue of requests that
pile up behind a long job.
"""

import threading
from contextlib import contextmanager

_guard = threading.Lock()
_held: dict[str, str] = {}

# One lock for everything that touches the stacks folder as a whole or
# restarts Dockup itself: backup, restore, export, and replacing its own
# container.
MAINTENANCE = "maintenance"


class Busy(Exception):
    """Raised when the thing asked for is already running."""


def stack(name: str) -> str:
    return f"stack:{name}"


def try_acquire(key: str, what: str = "something") -> bool:
    with _guard:
        if key in _held:
            return False
        _held[key] = what
        return True


def release(key: str) -> None:
    with _guard:
        _held.pop(key, None)


def what_is_running(key: str) -> str:
    with _guard:
        return _held.get(key, "")


@contextmanager
def held(key: str, what: str):
    """Hold `key` for the duration of the block, or raise Busy."""
    if not try_acquire(key, what):
        raise Busy(f"{what_is_running(key) or 'Something'} is already running. "
                   "Wait for it to finish, then try again.")
    try:
        yield
    finally:
        release(key)
