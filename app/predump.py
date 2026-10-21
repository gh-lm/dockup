"""Consistent database dumps, written ahead of whatever copies this
machine overnight.

A file copied while a database is being written to is not reliably
restorable. If the overnight copy takes live files, anything that cannot
be snapshotted safely has to be dumped first, by something that
understands the application.

That is what this does. Each job runs a command inside a container that
makes the application write a consistent copy of itself, lands it in one
folder (data/pre-backup) that a copy of the stacks folder already
includes, and records what happened in a status file beside it. Whatever
reads that file can tell a dump that silently stopped working from a
fresh one, rather than finding out on the day somebody needs to restore.

Two rules matter more than anything else here:

  A failed dump never replaces a good one. The old file stays exactly
  where it is and the status says so, with its age. Overwriting a known
  good archive with an empty one is the worst thing this could do.

  The status file is written last, and atomically. A reader that finds
  it can trust that everything it describes is already on disk.
"""

import hashlib
import json
import os
import shlex
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import activity, config, db, runtime

# The dumps start on the hour set in Settings and have until half past it
# to finish, which leaves the rest of the hour as a margin before a copy
# that is scheduled shortly afterwards.
DEFAULT_RUN_HOUR = 2
RUN_MINUTE = 0
DEADLINE_MINUTE = 30

STATUS_NAME = "status.json"
_DEFAULT_TIMEOUT = 20 * 60
# A dump that grows past this is stopped rather than left to fill the disk.
_MAX_DUMP_BYTES = 50 * 2**30

_running = threading.Lock()


def run_hour() -> int:
    """The hour the dumps start, from Settings, always 0 to 23."""
    con = db.connect()
    try:
        row = con.execute("SELECT value FROM settings WHERE key='predump.hour'").fetchone()
    finally:
        con.close()
    try:
        hour = int(row[0]) if row and row[0] not in (None, "") else DEFAULT_RUN_HOUR
    except (TypeError, ValueError):
        return DEFAULT_RUN_HOUR
    return hour if 0 <= hour <= 23 else DEFAULT_RUN_HOUR


def folder() -> Path:
    return config.DATA_DIR / "pre-backup"


def validate_jobs(text: str) -> list[dict]:
    """Parse and check the saved list. Raises ValueError, in words a
    person can act on, so Settings can show it under the box when they
    save - rather than the list failing quietly at two in the morning.

    A dump is written to a file name taken from this list, so the name
    has to be a plain file name and nothing that climbs out of the
    folder. A container name starting with a dash would be read by
    docker as an option rather than a name.
    """
    text = (text or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"That is not valid JSON ({exc.msg}, line {exc.lineno}).") from exc
    if not isinstance(parsed, list):
        raise ValueError("It needs to be a list: square brackets with one entry per database.")
    for n, job in enumerate(parsed, 1):
        where = f"Entry {n}"
        if not isinstance(job, dict):
            raise ValueError(f"{where} needs to be an object with container, command and file.")
        for field in ("container", "command", "file"):
            if not job.get(field):
                raise ValueError(f"{where} is missing \"{field}\".")
        if str(job["container"]).startswith("-"):
            raise ValueError(f"{where}: a container name cannot start with a dash.")
        try:
            parts = shlex.split(job["command"]) if isinstance(job["command"], str) else job["command"]
        except ValueError as exc:
            raise ValueError(f"{where}: the command has a quoting mistake ({exc}).") from exc
        if not isinstance(parts, list) or not parts or not all(isinstance(x, str) and x for x in parts):
            raise ValueError(f"{where}: \"command\" needs to be a list of words, such as "
                             "[\"forgejo\", \"dump\"], or one string.")
        name = str(job["file"])
        if name != Path(name).name or name.startswith(".") or name == STATUS_NAME:
            raise ValueError(f"{where}: \"file\" has to be a plain file name such as "
                             "dump.zip, not a path, not starting with a dot, and not "
                             f"{STATUS_NAME}.")
    files = [j["file"] for j in parsed]
    if len(set(files)) != len(files):
        raise ValueError("Two entries write to the same file name. Each needs its own.")
    return parsed


def load_jobs() -> tuple[list[dict], str]:
    """The configured jobs and, if the saved list is bad, why.

    Stored as JSON in one setting rather than a table: this is a short
    list that changes rarely, and a bad value should leave the rest of
    Dockup working. A bad list is an error to be reported, not an empty
    list: reading "nothing to do" as "all well" is how a broken setup
    looks healthy.
    """
    con = db.connect()
    try:
        row = con.execute("SELECT value FROM settings WHERE key='predump.jobs'").fetchone()
    finally:
        con.close()
    try:
        return validate_jobs(row[0] if row else ""), ""
    except ValueError as exc:
        activity.log("error", "pre-backup",
                     "The list of before-backup dumps is not valid, so none were run", str(exc))
        return [], str(exc)


def jobs() -> list[dict]:
    return load_jobs()[0]


def _seconds_to_deadline() -> int:
    """Seconds left before the deadline tonight; zero or less when past."""
    now = datetime.now()
    deadline = now.replace(hour=run_hour(), minute=DEADLINE_MINUTE, second=0, microsecond=0)
    return int((deadline - now).total_seconds())


def _past_deadline() -> bool:
    now = datetime.now()
    return now.hour > run_hour() or (now.hour == run_hour() and now.minute >= DEADLINE_MINUTE)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _age_words(path: Path) -> str:
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    if age < timedelta(hours=1):
        return f"{int(age.total_seconds() // 60)} minutes old"
    if age < timedelta(days=1):
        return f"{int(age.total_seconds() // 3600)} hours old"
    return f"{age.days} days old"


def _run_one(job: dict, out: Path, limit: int | None = None) -> dict:
    """One dump. Returns the entry that goes in the status file.

    The command writes to a temporary name. Only once it has finished
    cleanly and produced something non-empty does that file take the
    real name, so a half-written or failed dump can never be mistaken
    for a good one.
    """
    name = job.get("name") or job["container"]
    final = out / job["file"]
    temp = out / f".partial-{job['file']}"
    command = job.get("command")
    try:
        if isinstance(command, str):
            command = shlex.split(command)
    except ValueError as exc:
        return _failed(name, final, f"The command has a quoting mistake ({exc})")
    if not command:
        return _failed(name, final, "No command is set for this dump")

    rt = runtime.current()
    temp.unlink(missing_ok=True)
    try:
        with temp.open("wb") as f:
            # A scheduled dump is also held to the time left before the
            # deadline, so a job that starts at 02:29 cannot run on past a
            # copy that begins shortly after.
            timeout = int(job.get("timeout") or _DEFAULT_TIMEOUT)
            if limit is not None:
                timeout = max(1, min(timeout, limit))
            rt.exec_to_file(job["container"], command, f, timeout=timeout,
                            max_bytes=_MAX_DUMP_BYTES)
    except Exception as exc:
        temp.unlink(missing_ok=True)
        return _failed(name, final, str(exc))

    if not temp.exists() or temp.stat().st_size == 0:
        temp.unlink(missing_ok=True)
        return _failed(name, final, "The command produced nothing")

    os.chmod(temp, 0o600)
    os.replace(temp, final)
    return {
        "name": name,
        "file": final.name,
        "size": final.stat().st_size,
        "sha256": _sha256(final),
        "ok": True,
    }


def _failed(name: str, final: Path, why: str) -> dict:
    """A failure keeps whatever good copy is already there and says how
    old it is, so the backup server can tell 'no dump' from 'yesterday's
    dump' without guessing."""
    entry = {"name": name, "file": final.name, "ok": False, "error": why}
    if final.exists():
        entry["kept_previous"] = True
        entry["previous_age"] = _age_words(final)
        entry["size"] = final.stat().st_size
    else:
        entry["kept_previous"] = False
    return entry


def run_all(reason="scheduled") -> dict:
    """Run every configured dump and write the status file last."""
    if not _running.acquire(blocking=False):
        raise RuntimeError("A set of before-backup dumps is already running")
    try:
        out = folder()
        out.mkdir(parents=True, exist_ok=True)
        entries = []
        job_list, config_error = load_jobs()
        for job in job_list:
            if reason == "scheduled" and _past_deadline():
                # Whatever copies this machine overnight starts at a fixed
                # time. A dump that finishes after it is a dump the copy
                # may take half-written, so it is left for tomorrow and
                # the last good one stays in place.
                entries.append(_failed(job.get("name") or job["container"], out / job["file"],
                                       f"Not started: it was already past {run_hour():02d}:"
                                       f"{DEADLINE_MINUTE:02d}"))
                continue
            limit = _seconds_to_deadline() if reason == "scheduled" else None
            entries.append(_run_one(job, out, limit))
        status = {
            "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ok": (not config_error) and (all(e["ok"] for e in entries) if entries else True),
            "reason": reason,
            "stacks": entries,
        }
        if config_error:
            status["config_error"] = config_error
        # Written last and renamed into place: anything that reads the
        # status file can trust that what it describes is already there.
        temp = out / f".{STATUS_NAME}.partial"
        temp.write_text(json.dumps(status, indent=2) + "\n")
        os.chmod(temp, 0o600)
        os.replace(temp, out / STATUS_NAME)

        failed = [e["name"] for e in entries if not e["ok"]]
        if failed:
            activity.log("error", "pre-backup",
                         f"Before-backup dump failed for {', '.join(failed)}",
                         "\n".join(f"{e['name']}: {e.get('error', '')}"
                                   for e in entries if not e["ok"]))
        elif entries:
            activity.log("info", "pre-backup",
                         f"Before-backup dumps written ({reason}): {len(entries)}")
        return status
    finally:
        _running.release()


_last_run_day = None


def scheduler_loop():
    global _last_run_day
    while True:
        try:
            now = datetime.now()
            due = (now.hour == run_hour() and now.minute >= RUN_MINUTE
                   and _last_run_day != now.date())
            if due:
                _last_run_day = now.date()
                # Run even with no jobs set up if the saved list is bad,
                # so the status file says so instead of staying silent.
                if load_jobs()[0] or load_jobs()[1]:
                    run_all("scheduled")
        except Exception as exc:
            activity.log("error", "pre-backup", "Scheduled before-backup dumps failed", str(exc))
        time.sleep(60)


def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()
