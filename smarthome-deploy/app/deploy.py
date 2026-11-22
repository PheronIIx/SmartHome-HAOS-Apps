"""Legacy single-marker test deploy/rollback retained for compatibility in 1.0.0.

This legacy module is separate from the current JS-only batch writer. Its paths are
hardcoded to www/house-card.js; do not expand this module to YAML by default.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time

from . import backup, backup_service, check, plan

TEST_PATH = "www/house-card.js"
MARKER = b"// SmartHome Deploy dry-run verification, 2026-11-20: non-functional marker; remove after test."
HISTORY_ROOT = Path(os.environ.get("SMARTHOME_HISTORY_ROOT", "/data/deploy-history"))
ID_RE = re.compile(r"^[0-9a-f]{24}$")
PREVIEW_TTL = 300
_PENDING: dict[str, object] = {}


def _enabled() -> None:
    if check.load_options().get("enable_test_install") is not True:
        raise ValueError("Test installation disabled in app options")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").rstrip(b"\n")


def _read_current() -> tuple[bytes, os.stat_result]:
    """Open fixed file through no-follow directory FDs, not user paths."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    root = os.open(check.CONFIG_ROOT, flags | os.O_DIRECTORY)
    try:
        folder = os.open("www", flags | os.O_DIRECTORY, dir_fd=root)
        try:
            fd = os.open("house-card.js", flags, dir_fd=folder)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > check.MAX_DIFF_BYTES:
                    raise ValueError("Local JS file is not a regular file within size limit")
                data = os.read(fd, check.MAX_DIFF_BYTES + 1)
                if len(data) > check.MAX_DIFF_BYTES:
                    raise ValueError("Local JS file exceeds limit")
                now = os.stat("house-card.js", dir_fd=folder, follow_symlinks=False)
                if (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) != (
                    info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
                ):
                    raise ValueError("Local JS file changed during read")
                return data, info
            finally:
                os.close(fd)
        finally:
            os.close(folder)
    finally:
        os.close(root)


def _replace(expected: bytes, replacement: bytes, *, restore: dict | None = None) -> None:
    """Atomic replacement in a fixed no-follow directory; verify before rename."""
    if len(replacement) > check.MAX_DIFF_BYTES:
        raise ValueError("Replacement exceeds size limit")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    root = os.open(check.CONFIG_ROOT, flags | os.O_DIRECTORY)
    try:
        folder = os.open("www", flags | os.O_DIRECTORY, dir_fd=root)
        tmp = ".smarthome-deploy-" + secrets.token_hex(12) + ".tmp"
        created = False
        try:
            current, info = _read_current()
            if current != expected:
                raise ValueError("Local drift: file differs from approved snapshot")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=folder)
            created = True
            try:
                os.fchown(fd, info.st_uid, info.st_gid)
                os.fchmod(fd, stat.S_IMODE(info.st_mode))
                with os.fdopen(fd, "wb") as stream:
                    stream.write(replacement)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                # fdopen owns the fd only after it is entered.
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            if restore is not None:
                os.utime(tmp, ns=(restore["atime_ns"], restore["mtime_ns"]),
                         dir_fd=folder, follow_symlinks=False)
            # Final check immediately before rename. External writers are not
            # coordinated by this process; any detectable drift blocks install.
            now = os.stat("house-card.js", dir_fd=folder, follow_symlinks=False)
            if (now.st_dev, now.st_ino, now.st_mtime_ns, now.st_size) != (
                info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size
            ):
                raise ValueError("Local file changed before atomic replacement")
            os.replace(tmp, "house-card.js", src_dir_fd=folder, dst_dir_fd=folder)
            created = False
            os.fsync(folder)
            written, _ = _read_current()
            if written != replacement:
                raise RuntimeError("Installed bytes do not match approved GitHub blob")
        finally:
            if created:
                try:
                    os.unlink(tmp, dir_fd=folder)
                except FileNotFoundError:
                    pass
            os.close(folder)
    finally:
        os.close(root)


def _history_dir() -> Path:
    root = HISTORY_ROOT
    if root.is_symlink():
        raise ValueError("History directory cannot be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Invalid history directory")
    os.chmod(root, 0o700)
    return root


def _save(record: dict) -> None:
    if not ID_RE.fullmatch(record["id"]):
        raise ValueError("Invalid operation ID")
    root = _history_dir()
    fd, tmp_name = tempfile.mkstemp(prefix=".operation-", suffix=".tmp", dir=root)
    temp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, root / (record["id"] + ".json"))
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)


def _load(operation_id: str) -> dict:
    if not isinstance(operation_id, str) or not ID_RE.fullmatch(operation_id):
        raise ValueError("Invalid operation ID")
    path = _history_dir() / (operation_id + ".json")
    if path.is_symlink():
        raise ValueError("Symlinked history entry")
    with path.open(encoding="utf-8") as stream:
        record = json.load(stream)
    if not isinstance(record, dict) or record.get("id") != operation_id or record.get("path") != TEST_PATH:
        raise ValueError("Invalid operation record")
    return record


def history() -> dict:
    root = _history_dir()
    rows = []
    for path in root.glob("*.json"):
        if not ID_RE.fullmatch(path.stem) or path.is_symlink():
            continue
        try:
            record = _load(path.stem)
            rows.append({key: record.get(key) for key in (
                "id", "path", "created_at", "status", "target_commit", "backup", "rollback_backup"
            )})
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda item: item["created_at"] or "", reverse=True)
    return {"operations": rows[:30]}


def _no_unfinished() -> None:
    if any(row["status"] in ("installing", "rolling_back", "needs_attention")
           for row in history()["operations"]):
        raise ValueError("Unfinished deployment requires manual review; further writes blocked")


def prepare() -> dict:
    _enabled()
    with backup_service._LOCK:
        _no_unfinished()
        result = plan.prepare_plan()
        if not result.get("configured"):
            raise ValueError("Planner is not configured")
        candidates = [row for row in result["files"] if row["path"] == TEST_PATH]
        if len(candidates) != 1:
            raise ValueError("Approved JS file not found in plan")
        row = candidates[0]
        if row["action"] != "candidate_review" or row["local"] not in (
            "baseline_exact", "baseline_format_only"
        ):
            raise ValueError("Test JS is not a safe update candidate")
        opts = check.load_options()
        repo, token = opts["repository"], opts["github_token"]
        baseline = check._load_blob_for_diff(repo, token, row["baseline_sha"])
        target = check._load_blob_for_diff(repo, token, row["target_sha"])
        if _norm(target) != _norm(baseline) + b"\n" + MARKER:
            raise ValueError("Target contains changes beyond the approved test comment")
        local, _ = _read_current()
        if _norm(local) != _norm(baseline):
            raise ValueError("Local JS changed since approved baseline")
        if _sha(local) == _sha(target):
            raise ValueError("Target version already installed")
        operation_id = secrets.token_hex(12)
        _PENDING.clear()
        _PENDING.update({
            "id": operation_id, "expires": time.monotonic() + PREVIEW_TTL,
            "baseline": result["baseline"], "target_commit": result["target"],
            "baseline_blob": row["baseline_sha"], "target_blob": row["target_sha"],
            "local_sha256": _sha(local), "target_sha256": _sha(target),
            "repository": repo,
        })
        return {"plan_id": operation_id, "path": TEST_PATH, "target_commit": result["target"],
                "local_sha256": _sha(local), "target_sha256": _sha(target),
                "expires_seconds": PREVIEW_TTL,
                "message": "Only the approved non-functional comment will be installed"}


def install(plan_id: str, confirmation: str) -> dict:
    _enabled()
    if confirmation != "INSTALL house-card.js":
        raise ValueError("Explicit installation confirmation required")
    if not backup_service._LOCK.acquire(blocking=False):
        raise ValueError("Another backup/deployment operation is running")
    try:
        _no_unfinished()
        pending = dict(_PENDING)
        _PENDING.clear()  # single use, even when installation fails
        if not isinstance(plan_id, str) or plan_id != pending.get("id") or time.monotonic() > pending.get("expires", 0):
            raise ValueError("Plan expired or not found; prepare again")
        opts = check.load_options()
        if (opts.get("repository") != pending["repository"] or
                opts.get("baseline_commit") != pending["baseline"]):
            raise ValueError("GitHub settings changed since preview")
        target = check._load_blob_for_diff(opts["repository"], opts["github_token"], pending["target_blob"])
        if _sha(target) != pending["target_sha256"]:
            raise ValueError("GitHub target content changed")
        local, info = _read_current()
        if _sha(local) != pending["local_sha256"]:
            raise ValueError("Local JS changed since preview; installation blocked")
        # Recheck baseline, including CRLF-tolerant classification, before write.
        baseline = check._load_blob_for_diff(opts["repository"], opts["github_token"], pending["baseline_blob"])
        if _norm(local) != _norm(baseline) or _norm(target) != _norm(baseline) + b"\n" + MARKER:
            raise ValueError("Baseline or approved test marker changed")
        snapshot = backup.create_snapshot([TEST_PATH], pending["target_commit"])
        _, files = backup.verify_snapshot(snapshot["backup"])
        if files.get(TEST_PATH) != local:
            raise ValueError("Backup differs from current file; installation blocked")
        if _read_current()[0] != local:
            raise ValueError("Local file changed after backup; installation blocked")
        record = {
            "id": secrets.token_hex(12), "path": TEST_PATH,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "installing", "target_commit": pending["target_commit"],
            "backup": snapshot["backup"], "before_sha256": _sha(local),
            "after_sha256": _sha(target),
            "before_metadata": {"atime_ns": info.st_atime_ns, "mtime_ns": info.st_mtime_ns},
        }
        _save(record)  # durable journal before any HA write
        try:
            _replace(local, target)
            record["status"] = "installed"
            _save(record)
        except Exception as error:
            try:
                current, _ = _read_current()
                if current == target:
                    _replace(target, local, restore=record["before_metadata"])
                    record["status"] = "auto_rolled_back"
                elif current == local:
                    record["status"] = "aborted"
                else:
                    record["status"] = "needs_attention"
                _save(record)
            except Exception:
                record["status"] = "needs_attention"
                _save(record)
            raise RuntimeError("Installation failed; inspect operation history and app logs") from error
        return {"id": record["id"], "status": record["status"], "path": TEST_PATH,
                "backup": record["backup"], "target_commit": record["target_commit"]}
    finally:
        backup_service._LOCK.release()


def rollback(operation_id: str, confirmation: str) -> dict:
    _enabled()
    if confirmation != "ROLLBACK house-card.js":
        raise ValueError("Explicit rollback confirmation required")
    if not backup_service._LOCK.acquire(blocking=False):
        raise ValueError("Another backup/deployment operation is running")
    try:
        _no_unfinished()
        record = _load(operation_id)
        if record["status"] != "installed":
            raise ValueError("Only an installed operation may be rolled back")
        _, saved = backup.verify_snapshot(record["backup"])
        original = saved.get(TEST_PATH)
        if original is None or _sha(original) != record["before_sha256"]:
            raise ValueError("Original backup does not match operation journal")
        current, _ = _read_current()
        if _sha(current) != record["after_sha256"]:
            raise ValueError("Current file differs from installed version; rollback blocked")
        snapshot = backup.create_snapshot([TEST_PATH], record["target_commit"])
        _, files = backup.verify_snapshot(snapshot["backup"])
        if files.get(TEST_PATH) != current:
            raise ValueError("Rollback safety backup failed")
        record["rollback_backup"] = snapshot["backup"]
        record["status"] = "rolling_back"
        _save(record)
        try:
            _replace(current, original, restore=record["before_metadata"])
            record["status"] = "rolled_back"
            _save(record)
        except Exception as error:
            record["status"] = "needs_attention"
            _save(record)
            raise RuntimeError("Rollback failed; independent recovery may be required") from error
        return {"id": record["id"], "status": record["status"], "backup": record["backup"]}
    finally:
        backup_service._LOCK.release()
