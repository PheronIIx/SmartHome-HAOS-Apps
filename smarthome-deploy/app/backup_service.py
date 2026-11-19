"""UI-facing backup operations, intentionally without HA configuration writes.

All snapshot paths come from the GitHub-managed allowlist reported by check().
The browser may select a backup ID for verification, never filesystem paths.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading

from . import backup, check

_LOCK = threading.Lock()
MAX_LISTED = 100


def create_manual_backup() -> dict:
    if not _LOCK.acquire(blocking=False):
        raise RuntimeError("Another backup operation is running")
    try:
        status = check.check()
        if not status.get("configured"):
            raise ValueError("GitHub configuration is not ready")
        rows = status["files"]
        # Do not silently omit missing or unverified files: this backup is a
        # complete snapshot of currently managed files, not a partial archive.
        allowed = {"same", "format_only", "modified"}
        if not rows or any(row["status"] not in allowed for row in rows):
            raise ValueError("Some managed files are absent or unverified; backup blocked")
        paths = [row["path"] for row in rows]
        result = backup.create_snapshot(paths, status["commit"])
        manifest, data = backup.verify_snapshot(result["backup"])
        if len(data) != len(paths) or sorted(data) != sorted(paths):
            raise RuntimeError("Snapshot verification does not match requested file list")
        return {**result, "verified": True,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    finally:
        _LOCK.release()


def list_backups() -> dict:
    directory = backup.BACKUP_ROOT
    if directory.is_symlink():
        raise ValueError("Invalid backup directory")
    if not directory.exists():
        return {"backups": []}
    if not directory.is_dir():
        raise ValueError("Invalid backup directory")
    paths = [p for p in directory.iterdir()
             if backup.BACKUP_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()]
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for path in paths[:MAX_LISTED]:
        stat = path.stat()
        result.append({"backup": path.name,
                       "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                       "archive_bytes": stat.st_size,
                       "verified": False})
    return {"backups": result, "truncated": len(paths) > MAX_LISTED}


def verify_existing_backup(name: str) -> dict:
    manifest, data = backup.verify_snapshot(name)
    return {"backup": name, "verified": True, "file_count": len(data),
            "bytes": sum(map(len, data.values())),
            "source_commit": manifest["source_commit"]}


def rehearse_existing_backup(name: str) -> dict:
    if not _LOCK.acquire(blocking=False):
        raise RuntimeError("Another backup operation is running")
    try:
        if not isinstance(name, str) or not backup.BACKUP_NAME.fullmatch(name):
            raise ValueError("Invalid backup identifier")
        # Fixed server-side root: no arbitrary target path accepted via HTTP.
        # Fresh directory for each rehearsal; do not overwrite earlier results.
        from secrets import token_hex
        target = backup.RESTORE_ROOT / ("test-" + token_hex(8))
        return backup.rehearse_restore(name, target)
    finally:
        _LOCK.release()
