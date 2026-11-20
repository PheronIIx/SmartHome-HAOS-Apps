"""UI-facing backup operations, intentionally without HA configuration writes.

All snapshot paths come from the GitHub-managed allowlist reported by check().
The browser may select a backup ID for verification, never filesystem paths.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading

from . import backup, baseline, check

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
        installed = baseline.load_manifest(check.load_options())
        installed_paths = set(installed["files"]) if installed else set()
        allowed = {"same", "format_only", "modified"}
        paths, seen, skipped_uninstalled, blocked = set(), set(), [], []

        for row in rows:
            relative, state = row.get("path"), row.get("status")
            if not isinstance(relative, str):
                blocked.append("invalid managed path")
                continue
            seen.add(relative)
            if state in allowed:
                paths.add(relative)
            elif state == "missing" and installed is not None and relative not in installed_paths:
                skipped_uninstalled.append(relative)
            elif state == "missing":
                blocked.append(f"{relative} (missing; installed baseline is unknown or file was installed)")
            else:
                blocked.append(f"{relative} ({state or 'unknown'})")

        # Keep previously installed files in the archive even if GitHub removed them.
        for relative in sorted(installed_paths - seen):
            try:
                local = check.safe_local_file(relative)
                if not local.exists():
                    blocked.append(f"{relative} (missing installed file)")
                elif not local.is_file():
                    blocked.append(f"{relative} (not a regular file)")
                else:
                    paths.add(relative)
            except (OSError, ValueError):
                blocked.append(f"{relative} (local file cannot be verified)")

        if blocked:
            details = "; ".join(blocked[:10])
            if len(blocked) > 10:
                details += f"; and {len(blocked) - 10} more"
            raise ValueError("Backup blocked; managed files need attention: " + details)
        if not paths:
            raise ValueError("No installed managed files are available to back up")
        ordered_paths = sorted(paths)
        result = backup.create_snapshot(ordered_paths, status["commit"])
        manifest, data = backup.verify_snapshot(result["backup"])
        if len(data) != len(ordered_paths) or sorted(data) != ordered_paths:
            raise RuntimeError("Snapshot verification does not match requested file list")
        return {**result, "verified": True, "skipped_uninstalled": sorted(skipped_uninstalled),
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
