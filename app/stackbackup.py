"""Per-stack backup: archives one stack's compose config plus its actual
data - bind-mount directories and named volumes - and can restore both
back to exactly where they came from. Nothing is ever relocated into a
different layout; a helper container does the real file access so this
reaches paths Dockup's own container can't see directly (see runtime.py).
"""

import json
import os
import re
import shutil
import tarfile
import time
from pathlib import Path

import yaml

from . import activity, config, envsub, locks, runtime, stacks


def _parse_mounts(compose_text: str, env_text: str = "") -> list:
    """Every bind mount and named volume referenced by any service,
    de-duplicated (several services can share one volume)."""
    try:
        doc = yaml.safe_load(compose_text) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    env = envsub.parse_env(env_text)
    mounts, seen = [], set()
    for service_name, svc in (doc.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        for v in svc.get("volumes") or []:
            if isinstance(v, str):
                parts = v.split(":")
                source = envsub.substitute(parts[0], env)
                kind = "bind" if source.startswith(("/", "./", "../", "~")) else "volume"
                readonly = len(parts) > 2 and "ro" in parts[2].split(",")
            elif isinstance(v, dict) and v.get("type") in ("bind", "volume"):
                kind, source = v["type"], envsub.substitute(v.get("source", ""), env)
                readonly = bool(v.get("read_only"))
            else:
                continue
            if not source or (kind, source) in seen:
                continue
            seen.add((kind, source))
            mounts.append({"service": service_name, "type": kind, "source": source,
                           "readonly": readonly})
    return mounts


_BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.tar\.gz$")
_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _resolve_bind_source(source: str, stack_dir: Path) -> str:
    return source if source.startswith("/") else str((stack_dir / source).resolve())


def _belongs_to(name: str, filename: str) -> bool:
    """A file name Dockup gives a stack's backup: the stack's name, then a
    timestamp, with "uploaded-" between for one brought in from elsewhere.
    A longer name that merely starts with this one ("app-2-...") is another
    stack's."""
    return re.fullmatch(rf"{re.escape(name)}-(uploaded-)?\d{{8}}-\d{{6}}\.tar\.gz", filename) is not None


def list_backups(name: str) -> list:
    files = sorted((f for f in config.STACK_BACKUP_DIR.glob(f"{name}-*.tar.gz")
                    if _belongs_to(name, f.name)), reverse=True)
    return [{"name": f.name, "size": f.stat().st_size,
             "made": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))}
            for f in files]


def backup_stack(name: str) -> Path:
    stacks.stack_dir(name)  # validates the name
    with locks.held(locks.MAINTENANCE, "A backup, restore or export"):
        with locks.held(locks.stack(name), f"An action on '{name}'"):
            return _backup_locked(name)


def _backup_locked(name: str) -> Path:
    d = stacks.stack_dir(name)
    compose_path = stacks.compose_path(name)
    if not compose_path.exists():
        raise ValueError(f"'{name}' has no compose file to back up")
    compose_text = compose_path.read_text()
    env_path = d / ".env"
    env_text = env_path.read_text() if env_path.exists() else ""
    mounts = _parse_mounts(compose_text, env_text)
    rt = runtime.current()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    work_name = f".work-{name}-{stamp}"
    work_dir = config.STACK_BACKUP_DIR / work_name
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        (work_dir / "compose.yaml").write_text(compose_text)
        if env_text:
            (work_dir / ".env").write_text(env_text)

        manifest = {"stack": name, "made": stamp, "mounts": []}
        errors = []
        for i, m in enumerate(mounts):
            piece = f"data-{i}.tar.gz"
            try:
                if m["type"] == "bind":
                    rt.archive_path_to_backup(_resolve_bind_source(m["source"], d), f"{work_name}/{piece}")
                else:
                    rt.archive_volume_to_backup(m["source"], f"{work_name}/{piece}")
                manifest["mounts"].append({**m, "archive": piece, "ok": True})
            except runtime.RuntimeError_ as exc:
                errors.append(f"{m['type']} '{m['source']}': {exc}")
                manifest["mounts"].append({**m, "archive": None, "ok": False, "error": str(exc)})
        (work_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

        final_path = config.STACK_BACKUP_DIR / f"{name}-{stamp}.tar.gz"
        with tarfile.open(final_path, "w:gz") as tar:
            tar.add(work_dir, arcname=".")

        if errors:
            note = f"{len(errors)} of {len(mounts)} data location(s) failed: " + "; ".join(errors)
            activity.log("error", "backup", f"Stack backup of '{name}' had problems", note)
        else:
            activity.log("info", "backup",
                         f"Backed up '{name}'" + (f" ({len(mounts)} data location(s))" if mounts else " (config only)"))
        return final_path
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


class NeedsConfirmation(ValueError):
    """The restore is possible, but the person has to see where it writes
    and say it is wanted first."""


def _read_member(tar: tarfile.TarFile, name: str) -> str | None:
    """One small text file from the archive, without unpacking the rest."""
    for candidate in (name, f"./{name}"):
        try:
            member = tar.getmember(candidate)
        except KeyError:
            continue
        if member.isfile() and member.size < 4 * 1024 * 1024:
            return tar.extractfile(member).read().decode("utf-8", "replace")
    return None


def _risky_settings(compose_text: str) -> list:
    """Things in the compose file being restored that give a container more
    reach than usual. The restore does not run them, but the person is told
    before the file replaces the current one."""
    try:
        doc = yaml.safe_load(compose_text) or {}
    except yaml.YAMLError:
        return []
    found = []
    for svc_name, svc in ((doc.get("services") or {}) if isinstance(doc, dict) else {}).items():
        if not isinstance(svc, dict):
            continue
        if svc.get("privileged"):
            found.append(f"{svc_name}: privileged")
        for key in ("pid", "ipc", "network_mode", "userns_mode"):
            if svc.get(key) == "host":
                found.append(f"{svc_name}: {key} host")
        if svc.get("cap_add"):
            found.append(f"{svc_name}: extra capabilities")
        if svc.get("devices"):
            found.append(f"{svc_name}: host devices")
        if any("docker.sock" in str(v) for v in svc.get("volumes") or []):
            found.append(f"{svc_name}: the Docker socket")
    return found


def _restore_targets(compose_text: str, env_text: str, d: Path) -> list:
    """Where each data piece would be written, worked out from the compose
    file being restored and never from the manifest, which anyone who can
    hand over a backup file can write. A folder that is a system folder, or
    that holds the stacks folder, is refused outright: the helper that
    writes into it runs as root."""
    from . import backup
    stacks_root = os.path.normpath(str(config.STACKS_DIR))
    own = backup.own_folder_in_stacks()
    targets = []
    for i, m in enumerate(_parse_mounts(compose_text, env_text)):
        if m["type"] == "bind":
            source = os.path.normpath(_resolve_bind_source(m["source"], d))
            if m["readonly"]:
                continue  # something the stack only reads, never restored into
            if not stacks._deletable_bind(source, False):
                raise ValueError(f"The backup would write into {source}, which is a system "
                                 "folder or too broad to be one stack's data. Nothing has been changed.")
            if stacks._overlaps(source, stacks_root):
                # Inside the stacks folder, only this stack's own folder is
                # fair game: another stack's compose file is something Dockup
                # goes on to run, and Dockup's own folder holds its database.
                inside_own = source == str(d) or source.startswith(str(d) + "/")
                if not inside_own or (own and stacks._overlaps(source, str(own))):
                    raise ValueError(f"The backup would write into {source}, which holds or "
                                     "belongs to another part of the stacks folder. "
                                     "Nothing has been changed.")
        else:
            source = m["source"]
            if not _VOLUME_RE.match(source):
                raise ValueError(f"The backup names a volume ('{source}') that is not a valid "
                                 "volume name. Nothing has been changed.")
        targets.append({"index": i, "type": m["type"], "source": source})
    return targets


def restore_stack(name: str, backup_filename: str, force: bool = False) -> str:
    from . import backup
    stacks.stack_dir(name)  # validates the name
    with locks.held(locks.MAINTENANCE, "A backup, restore or export"):
        with locks.held(locks.stack(name), f"An action on '{name}'"):
            return _restore_locked(name, backup_filename, force, backup)


def _restore_locked(name: str, backup_filename: str, force: bool, backup) -> str:
    if not _belongs_to(name, backup_filename):
        raise ValueError("That backup file doesn't exist for this stack")
    path = (config.STACK_BACKUP_DIR / backup_filename).resolve()
    if path.parent != config.STACK_BACKUP_DIR.resolve() or not path.exists():
        raise ValueError("That backup file doesn't exist")

    if name == stacks.own_compose_project_safe():
        raise ValueError("That is Dockup's own stack, which is not restored from inside Dockup.")
    if name in backup._running_stack_names():
        raise ValueError(f"Stop '{name}' first, then restore. A restore replaces its files, and "
                         "a running database would lose them from under it. Nothing has been changed.")

    d = stacks.stack_dir(name)
    try:
        with tarfile.open(path, "r:gz") as tar:
            compose_text = _read_member(tar, "compose.yaml")
            env_text = _read_member(tar, ".env") or ""
            if compose_text is None or _read_member(tar, "manifest.json") is None:
                raise ValueError("That archive doesn't look like a Dockup stack backup")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError(f"That backup file is damaged and can't be opened ({exc}). "
                         "Nothing has been changed.") from exc

    targets = _restore_targets(compose_text, env_text, d)
    risky = _risky_settings(compose_text)
    if (targets or risky) and not force:
        listing = ", ".join(f"{t['source']}" + (" (volume)" if t["type"] == "volume" else "")
                            for t in targets) or "nowhere"
        warning = (f" The compose file in the backup also gives containers extra reach: "
                   f"{'; '.join(risky)}." if risky else "")
        raise NeedsConfirmation(
            f"Restoring '{name}' replaces its compose file and .env (the current ones are kept "
            f"beside them as .before-restore) and writes the backup's data into: {listing}."
            f"{warning} Restore only if every one of those is right.")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    work_name = f".restore-{name}-{stamp}"
    work_dir = config.STACK_BACKUP_DIR / work_name
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall(work_dir, filter="data")
        d.mkdir(parents=True, exist_ok=True)
        # Keep what is there before it is replaced, so a restore can be undone.
        for fname in ("compose.yaml", ".env"):
            current = d / fname
            if current.exists():
                kept = d / f"{fname}.before-restore"
                shutil.copy2(current, kept)
                os.chmod(kept, 0o600)
        (d / "compose.yaml").write_text(compose_text)
        env_src = work_dir / ".env"
        if env_src.exists():
            (d / ".env").write_text(env_src.read_text())

        errors = []
        for t in targets:
            piece = f"data-{t['index']}.tar.gz"
            if not (work_dir / piece).exists():
                continue  # this location failed when the backup was made
            piece_rel = f"{work_name}/{piece}"
            try:
                if t["type"] == "bind":
                    rt_restore = runtime.current().restore_path_from_backup
                else:
                    rt_restore = runtime.current().restore_volume_from_backup
                rt_restore(t["source"], piece_rel)
            except runtime.RuntimeError_ as exc:
                errors.append(f"{t['type']} '{t['source']}': {exc}")

        if errors:
            note = f"{len(errors)} data location(s) failed to restore: " + "; ".join(errors)
            activity.log("error", "backup", f"Restore of '{name}' had problems", note)
            raise ValueError(note)
        activity.log("info", "backup", f"Restored '{name}' from {backup_filename}")
        return f"Restored '{name}'. Config and data went back to exactly where they were."
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
