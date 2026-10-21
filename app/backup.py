"""Backups: a daily tarball of the stacks folder plus the Dockup database,
kept for a configurable number of days, restorable from the UI, plus a
download-everything zip for portability.
"""

import io
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from . import activity, config, db, locks, settingsvc
from .httputil import json_body

bp = Blueprint("backup", __name__, url_prefix="/api/backup")

_last_backup_day = None

# How many skipped files to name in the activity log before saying
# "and N more" - enough to act on, not so many the entry is unreadable.
_MAX_NAMED_SKIPS = 20

SKIP_EXPLANATION = (
    "Dockup isn't allowed to read these files, so they are the only things missing "
    "from the backup - everything else was saved. This is almost always a file owned "
    "by root with permissions that shut everyone else out, often a leftover like "
    "'.env.before-move' or a database file a container wrote as root. Either delete "
    "the leftover, or let the group Dockup runs as read it. See \"A file keeps being "
    "left out of the backup\" in the runbook."
)


def _same_dir(a, b) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def own_folder_in_stacks() -> Path | None:
    """The folder inside the stacks folder that holds Dockup itself, or
    None when Dockup lives elsewhere. The install puts Dockup's data
    directly under it (<stacks>/dockup/data), so that is the layout
    looked for. Backup and restore both have to leave this folder alone:
    it is the thing doing the work."""
    if not config.STACKS_DIR.exists() or not config.DATA_DIR.exists():
        return None
    try:
        entries = sorted(config.STACKS_DIR.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.is_dir() and (_same_dir(entry / "data", config.DATA_DIR)
                               or _same_dir(entry, config.DATA_DIR)):
            return entry
    return None


def _add_tree(tar, root: Path, arcname: str, skip: list | None = None) -> list[str]:
    """Add a folder to a tarball one entry at a time.

    tarfile's own recursion gives up on the whole folder the moment it
    meets a file it can't open, which turned one unreadable leftover
    into a nightly "backup failed" and no backup at all. Walking it
    ourselves costs that one file and keeps the rest.

    `skip` holds folders to leave out whole. Dockup lives in the stacks
    folder, so without it the backup swept up its own previous backups.

    Folders are matched by identity (device and inode), not by path. In
    the container Dockup's data folder is one directory seen at two
    paths: its own mount at /app/data, and again inside the stacks
    folder. Matching on the path string misses the second.
    """
    skipped = []
    skip = [p for p in (skip or ()) if p is not None and Path(p).exists()]

    def add(path: Path, arc: str):
        try:
            tar.add(path, arcname=arc, recursive=False)
        except OSError as exc:
            skipped.append(f"{path} ({exc.strerror or exc})")

    def cant_list(exc: OSError):
        skipped.append(f"{exc.filename} ({exc.strerror or exc})")

    add(root, arcname)
    base_arc = Path(arcname)
    for dirpath, dirnames, filenames in os.walk(root, onerror=cant_list):
        here = Path(dirpath)
        # Pruning dirnames in place is what stops os.walk descending.
        if skip:
            dirnames[:] = [d for d in dirnames
                           if not any(_same_dir(here / d, s) for s in skip)]
        rel = here.relative_to(root)
        for name in sorted(dirnames) + sorted(filenames):
            add(here / name, str(base_arc / rel / name))
    return skipped


def _report_skips(skipped: list[str], what: str):
    if not skipped:
        return
    named = skipped[:_MAX_NAMED_SKIPS]
    more = len(skipped) - len(named)
    detail = "\n".join(named)
    if more:
        detail += f"\n...and {more} more"
    # A warning, not an error: the backup itself worked, and an error
    # here would email the same alert every single night.
    activity.log("warning", "backup",
                 f"{what} left out {len(skipped)} file(s) it couldn't read",
                 f"{detail}\n\n{SKIP_EXPLANATION}")


def make_backup(reason="scheduled") -> tuple[Path, list[str]]:
    """One backup at a time, and never while a restore or export is
    running: a backup reading files that a restore is replacing archives
    half of each. Raises locks.Busy if something else has the lock."""
    with locks.held(locks.MAINTENANCE, "A backup, restore or export"):
        return _make_backup_locked(reason)


# Name of the note inside an archive listing files the backup could not
# read. A restore replaces whole stack folders, so a backup that is
# missing files would turn "couldn't read" into "now deleted".
SKIPPED_NOTE = "dockup-skipped.txt"


def _make_backup_locked(reason) -> tuple[Path, list[str]]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.BACKUP_DIR / f"dockup-backup-{stamp}.tar.gz"
    # Two backups in the same second must not overwrite each other.
    n = 2
    while path.exists():
        path = config.BACKUP_DIR / f"dockup-backup-{stamp}-{n}.tar.gz"
        n += 1
    # Built under a name the backups list doesn't match, and only moved
    # into place once it is whole. A half-written archive that kept the
    # real name looked like a perfectly good backup in the list, right
    # up until someone restored from it.
    unique = uuid.uuid4().hex[:8]
    partial = config.BACKUP_DIR / f".partial-{stamp}-{unique}.tar.gz"
    tmp_db = config.DATA_DIR / f".backup-db-{stamp}-{unique}.sqlite"
    src = db.connect()
    try:
        dst = sqlite3.connect(tmp_db)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    try:
        skipped = []
        with tarfile.open(partial, "w:gz") as tar:
            if config.STACKS_DIR.exists():
                # Dockup's own data folder is left out of the sweep. The
                # database goes in below as a consistent snapshot, and
                # the rest of that folder is this backup's own output:
                # including it makes every archive hold every archive
                # before it.
                skipped = _add_tree(tar, config.STACKS_DIR, "stacks",
                                    skip=[config.DATA_DIR])
            tar.add(tmp_db, arcname="dockup.db")
            if skipped:
                note = ("\n".join(skipped) + "\n").encode()
                info = tarfile.TarInfo(SKIPPED_NOTE)
                info.size = len(note)
                info.mtime = time.time()
                tar.addfile(info, io.BytesIO(note))
        # A backup holds every stack's .env and the database, so only the
        # owner may read it.
        os.chmod(partial, 0o600)
        if reason == "scheduled":
            _check_size(partial)
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        tmp_db.unlink(missing_ok=True)
    activity.log("info", "backup", f"Backup made ({reason}): {path.name}")
    _report_skips(skipped, "The backup")
    return path, skipped


# A backup that suddenly dwarfs the one before it means something is
# being swept up that should not be. That is exactly how the folder
# filled: nobody looks at the size of a file that writes itself at three
# in the morning. Refusing to keep it turns a silent disk-filler into a
# message on the Activity page.
_SIZE_ALARM_MULTIPLE = 2
_SIZE_ALARM_FLOOR = 64 * 1024 * 1024   # below this, growth means nothing


def _check_size(partial: Path):
    """Refuse a new scheduled backup that has more than doubled.

    Twice the size is the line because that is exactly how the folder
    filled: each night's archive held the one before it, so it doubled.
    A looser limit would have waved it through every night.

    Only the scheduled run is held to it. Real growth happens, such as
    adopting a lot of stacks, and a nightly job that refuses for ever
    with nobody awake to say "that is fine" is worse than the problem.
    Pressing Back up now keeps the file and becomes the new baseline.
    """
    previous = sorted(config.BACKUP_DIR.glob("dockup-backup-*.tar.gz"),
                      key=lambda f: f.stat().st_mtime, reverse=True)
    if not previous:
        return
    last = previous[0].stat().st_size
    new = partial.stat().st_size
    if last < _SIZE_ALARM_FLOOR or new <= last * _SIZE_ALARM_MULTIPLE:
        return
    raise RuntimeError(
        f"This backup came out {new / last:.1f} times the size of the last one "
        f"({_human(new)} against {_human(last)}), so it has not been kept. "
        "Something is being included that should not be. If the growth is real, "
        "press Back up now and that one will be kept.")


def _human(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024


def apply_retention(days: int):
    """Delete backups older than `days`, but never the newest one: with a
    retention of zero or less every file, including the one just made,
    is older than the cutoff, and the nightly job would end by deleting
    its own work."""
    days = max(1, int(days))
    cutoff = time.time() - days * 86400
    backups = sorted(config.BACKUP_DIR.glob("dockup-backup-*.tar.gz"),
                     key=lambda f: f.stat().st_mtime, reverse=True)
    for f in backups[1:]:
        if f.stat().st_mtime < cutoff:
            f.unlink()
    # Half-written archives from a backup that died mid-write, or from a
    # version before those were cleaned up. An hour is far longer than a
    # backup takes, so anything older than that is abandoned.
    for f in config.BACKUP_DIR.glob(".partial-*.tar.gz"):
        if f.stat().st_mtime < time.time() - 3600:
            f.unlink(missing_ok=True)


class NeedsConfirmation(ValueError):
    """The restore is possible but needs the person to say it is wanted."""


def _running_stack_names() -> list[str]:
    """Stacks in the stacks folder with containers running, other than
    Dockup itself. Fails closed: if the engine cannot be asked, nobody
    knows what is running, so nothing is restored."""
    from . import runtime, stacks
    try:
        containers = runtime.current().ps()
    except runtime.RuntimeError_ as exc:
        raise ValueError("Dockup cannot tell which stacks are running, because the engine "
                         f"did not answer ({exc}). Nothing has been changed.") from exc
    own = stacks.own_compose_project(containers)
    live = {c["project"] for c in containers
            if c.get("project") and c.get("state") in ("running", "restarting", "paused")
            and c["project"] != own}
    return sorted(n for n in live if (config.STACKS_DIR / n).is_dir())


def restore_backup(filename: str, force: bool = False) -> str:
    """Restore stacks (and DB to a side file) from a named backup."""
    with locks.held(locks.MAINTENANCE, "A backup, restore or export"):
        return _restore_locked(filename, force)


def _restore_locked(filename: str, force: bool) -> str:
    if not _BACKUP_FILENAME_RE.match(filename):
        raise ValueError("That backup file doesn't exist")
    path = (config.BACKUP_DIR / filename).resolve()
    if path.parent != config.BACKUP_DIR.resolve() or not path.exists():
        raise ValueError("That backup file doesn't exist")

    # A restore replaces whole stack folders. Doing that under a running
    # stack takes its files away from a live database.
    running = _running_stack_names()
    if running:
        raise ValueError("Stop these stacks first, then restore: " + ", ".join(running) + ". "
                         "A restore replaces their files, and a running database would lose "
                         "them from under it. Nothing has been changed.")

    # Dockup's own folder is never touched by a restore. When Dockup sits
    # inside the stacks folder, copying the stacks folder into a safety
    # copy that is itself inside it recurses until the disk is full, and
    # clearing the folder out would delete Dockup's database, its
    # settings and the safety copy together.
    own = own_folder_in_stacks()
    protected = own.name if own else None

    # Staging and the safety copy live next to what they replace, on the
    # same filesystem, so moving a stack folder is a rename. A rename
    # works on files owned by another user and cannot stop half way
    # through a folder, which removing them one by one could.
    work = (own / "data") if own and (own / "data").is_dir() else config.DATA_DIR
    staging = work / ".restore-staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall(staging, filter="data")
    except (tarfile.TarError, EOFError, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError(
            f"That backup file is damaged and can't be opened ({exc}). Nothing has been "
            "changed - pick an earlier backup.") from exc
    restored = staging / "stacks"
    if not restored.exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError("That archive doesn't look like a Dockup backup")

    note = staging / SKIPPED_NOTE
    if note.exists() and not force:
        left_out = [line for line in note.read_text().splitlines() if line.strip()]
        shutil.rmtree(staging, ignore_errors=True)
        if left_out:
            raise NeedsConfirmation(
                f"This backup left out {len(left_out)} file(s) it could not read when it was made. "
                "Restoring replaces each stack folder with the backup's copy, so those files "
                "would be lost from the folders they were in. Restore anyway only if that is "
                "acceptable.")

    # The backup's database is placed alongside, never swapped in live, and
    # is copied before anything is moved so a backup without one cannot
    # fail the restore half way through.
    db_copy = None
    if (staging / "dockup.db").exists():
        db_copy = config.DATA_DIR / f"restored-{filename.replace('.tar.gz', '')}.sqlite"
        shutil.copy2(staging / "dockup.db", db_copy)

    # keep the current state to one side so a restore is itself reversible
    undo = work / "pre-restore-stacks"
    if undo.exists():
        shutil.rmtree(undo, ignore_errors=True)
    undo.mkdir()
    # Move each stack folder aside, then move the restored one in. A
    # failure part way leaves every stack either old or new, never half
    # deleted, and the old ones are in the safety folder.
    for item in config.STACKS_DIR.iterdir():
        if item.name == protected:
            continue
        shutil.move(str(item), str(undo / item.name))
    for item in restored.iterdir():
        if item.name == protected:
            continue
        shutil.move(str(item), str(config.STACKS_DIR / item.name))

    shutil.rmtree(staging, ignore_errors=True)
    activity.log("info", "backup",
                 f"Restored stack files from {filename}",
                 f"The previous stack files were kept at {undo}. "
                 + (f"The backup's database copy is at {db_copy} if ever needed. "
                    if db_copy else "That backup held no database copy. ") +
                 "Restored files belong to the user Dockup runs as.")
    return f"Stack files restored from {filename}. The previous files were kept safe in case you change your mind."


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def scheduler_loop():
    global _last_backup_day
    while True:
        try:
            now = datetime.now()
            con = db.connect()
            try:
                row = con.execute("SELECT value FROM settings WHERE key='backup.hour'").fetchone()
                hour = _int_or(row[0] if row else None, 3)
                row = con.execute("SELECT value FROM settings WHERE key='backup.retention_days'").fetchone()
                keep = _int_or(row[0] if row else None, 14)
            finally:
                con.close()
            if now.hour == hour and _last_backup_day != now.date():
                _last_backup_day = now.date()
                make_backup("scheduled")
                apply_retention(keep)
        except Exception as exc:
            activity.log("error", "backup", "Scheduled backup failed", str(exc))
        time.sleep(60)


def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()


# -- API ----------------------------------------------------------------


@bp.get("/list")
def api_list():
    files = sorted(config.BACKUP_DIR.glob("dockup-backup-*.tar.gz"), reverse=True)
    return jsonify({"backups": [
        {"name": f.name, "size": f.stat().st_size,
         "made": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
        for f in files
    ]})


@bp.post("/run")
def api_run():
    try:
        path, skipped = make_backup("manual")
    except locks.Busy as exc:
        return jsonify({"error": str(exc)}), 409
    apply_retention(_int_or(settingsvc.get("backup.retention_days"), 14))
    message = f"Backup made: {path.name}"
    if skipped:
        message += f" - {len(skipped)} file(s) couldn't be read and were left out, see Activity"
    return jsonify({"ok": True, "name": path.name, "skipped": len(skipped), "message": message})


@bp.post("/restore")
def api_restore():
    body = json_body()
    name = body.get("name", "")
    try:
        message = restore_backup(name, force=bool(body.get("force")))
    except locks.Busy as exc:
        return jsonify({"error": str(exc)}), 409
    except NeedsConfirmation as exc:
        return jsonify({"error": str(exc), "confirm": True}), 409
    except (ValueError, OSError, tarfile.TarError, EOFError) as exc:
        activity.log("error", "backup", "Restore failed", str(exc))
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "message": message})


_BACKUP_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.tar\.gz$")


@bp.get("/download/<name>")
def api_download(name):
    if not _BACKUP_FILENAME_RE.match(name):
        return jsonify({"error": "Not found"}), 404
    path = (config.BACKUP_DIR / name).resolve()
    if path.parent != config.BACKUP_DIR.resolve() or not path.exists():
        return jsonify({"error": "Not found"}), 404
    return send_file(path, as_attachment=True)


@bp.get("/export")
def api_export():
    """Everything as one zip: stacks, database - full portability.

    Built on disk, not in memory: the container is capped well below what
    a real stacks folder weighs, and holding the whole zip in memory
    would get Dockup killed by the first person to press this. Dockup's
    own data folder is left out for the same reason as in a backup: it
    holds the backups, and the database goes in as a snapshot.
    """
    try:
        lock = locks.held(locks.MAINTENANCE, "A backup, restore or export")
        lock.__enter__()
    except locks.Busy as exc:
        return jsonify({"error": str(exc)}), 409
    fd, name = tempfile.mkstemp(prefix=".export-", suffix=".zip", dir=config.DATA_DIR)
    os.close(fd)
    out = Path(name)
    try:
        os.chmod(out, 0o600)
        skipped = []
        skip = [config.DATA_DIR]
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            if config.STACKS_DIR.exists():
                def cant_list(exc):
                    skipped.append(f"{exc.filename} ({exc.strerror or exc})")
                for root, dirs, files in os.walk(config.STACKS_DIR, onerror=cant_list):
                    dirs[:] = [d for d in dirs
                               if not any(_same_dir(Path(root) / d, x) for x in skip)]
                    for fname in sorted(files):
                        full = Path(root) / fname
                        try:
                            z.write(full, Path("stacks") / full.relative_to(config.STACKS_DIR))
                        except OSError as exc:
                            skipped.append(f"{full} ({exc.strerror or exc})")
            tmp_db = config.DATA_DIR / f".export-db-{uuid.uuid4().hex[:8]}.sqlite"
            src = db.connect()
            try:
                dst = sqlite3.connect(tmp_db)
                with dst:
                    src.backup(dst)
                dst.close()
            finally:
                src.close()
            z.write(tmp_db, "dockup.db")
            tmp_db.unlink(missing_ok=True)
    except BaseException:
        out.unlink(missing_ok=True)
        lock.__exit__(None, None, None)
        raise
    activity.log("info", "backup", "Full export downloaded")
    _report_skips(skipped, "The export")
    resp = send_file(out, as_attachment=True, download_name="dockup-export.zip",
                     mimetype="application/zip")
    # The file is already open for sending, so removing its name now
    # leaves the data readable until the download ends and means nothing
    # is left behind if the connection drops half way.
    out.unlink(missing_ok=True)
    resp.call_on_close(lambda: lock.__exit__(None, None, None))
    return resp
