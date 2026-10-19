"""Per-stack backup: archives one stack's compose config plus its actual
data - bind-mount directories and named volumes - and can restore both
back to exactly where they came from. Nothing is ever relocated into a
different layout; a helper container does the real file access so this
reaches paths Dockup's own container can't see directly (see runtime.py).
"""

import json
import re
import shutil
import tarfile
import time
from pathlib import Path

import yaml

from . import activity, config, envsub, runtime, stacks


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


_ARCHIVE_RE = re.compile(r"^data-\d+\.tar\.gz$")
_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _resolve_bind_source(source: str, stack_dir: Path) -> str:
    return source if source.startswith("/") else str((stack_dir / source).resolve())


def list_backups(name: str) -> list:
    files = sorted(config.STACK_BACKUP_DIR.glob(f"{name}-*.tar.gz"), reverse=True)
    return [{"name": f.name, "size": f.stat().st_size,
             "made": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))}
            for f in files]


def backup_stack(name: str) -> Path:
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


def restore_stack(name: str, backup_filename: str) -> str:
    path = (config.STACK_BACKUP_DIR / backup_filename).resolve()
    if path.parent != config.STACK_BACKUP_DIR.resolve() or not path.exists():
        raise ValueError("That backup file doesn't exist")
    rt = runtime.current()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    work_name = f".restore-{name}-{stamp}"
    work_dir = config.STACK_BACKUP_DIR / work_name
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall(work_dir, filter="data")
        manifest_path = work_dir / "manifest.json"
        if not manifest_path.exists():
            raise ValueError("That archive doesn't look like a Dockup stack backup")
        manifest = json.loads(manifest_path.read_text())

        d = stacks.stack_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "compose.yaml").write_text((work_dir / "compose.yaml").read_text())
        env_src = work_dir / ".env"
        if env_src.exists():
            (d / ".env").write_text(env_src.read_text())

        errors = []
        for m in manifest.get("mounts", []):
            if not m.get("ok") or not m.get("archive"):
                continue
            # The archive name is read from an uploaded, attacker-editable
            # manifest and ends up in a helper container's argv - a real
            # backup only ever names its pieces "data-<n>.tar.gz", so
            # anything else is refused rather than trusted.
            if not _ARCHIVE_RE.match(m["archive"]):
                errors.append(f"{m.get('type', '?')} '{m.get('source', '?')}': suspicious archive name in manifest")
                continue
            if m["type"] == "volume" and not _VOLUME_RE.match(str(m.get("source", ""))):
                errors.append(f"volume '{m.get('source', '?')}': invalid volume name in manifest")
                continue
            piece_rel = f"{work_name}/{m['archive']}"
            try:
                if m["type"] == "bind":
                    rt.restore_path_from_backup(_resolve_bind_source(m["source"], d), piece_rel)
                else:
                    rt.restore_volume_from_backup(m["source"], piece_rel)
            except runtime.RuntimeError_ as exc:
                errors.append(f"{m['type']} '{m['source']}': {exc}")

        if errors:
            note = f"{len(errors)} data location(s) failed to restore: " + "; ".join(errors)
            activity.log("error", "backup", f"Restore of '{name}' had problems", note)
            raise ValueError(note)
        activity.log("info", "backup", f"Restored '{name}' from {backup_filename}")
        return f"Restored '{name}'. Config and data went back to exactly where they were."
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
