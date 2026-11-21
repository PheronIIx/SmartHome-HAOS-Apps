"""Read-only DEV-07 status snapshot for Home Assistant.

Only the agreed runtime JSON is written. No install/rollback APIs are invoked.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import secrets
import threading
from pathlib import Path

from . import check, sync_plan

INTERVAL_SECONDS = 6 * 60 * 60
STATUS_NAME = "status.json"
STATUS_DIR = ".smarthome-deploy"
MAX_ROWS = 300
_LOCK = threading.Lock()
CANDIDATES = frozenset({"candidate_review", "new_file_review"})
BLOCKED = frozenset({
    "blocked_local_drift", "blocked_unverified", "new_file_conflict", "deletion_review"
})
IGNORED = frozenset({"no_change", "format_only", "new_file_present"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def group_for(path: str) -> tuple[str, str]:
    if path.startswith("www/") and path.endswith(".js"):
        return "cards", "Карточки"
    if path.startswith("www/plans/"):
        return "plans", "Планы"
    if path.startswith("packages/"):
        return "packages", "Пакеты HA"
    return "configuration", "Конфигурация HA"


def snapshot(plan: dict, at: str | None = None) -> dict:
    """Normalize the existing per-file planner; never infer updates from a commit alone."""
    at = at or utc_now()
    result = {
        "schema_version": 1,
        "status": "unconfigured",
        "checked_at": at,
        "target_commit": None,
        "available_count": None,
        "blocked_count": None,
        "groups": [],
        "files": [],
        "message": None,
    }
    if not plan.get("configured"):
        result["message"] = "Манифест или параметры Deploy не настроены"
        return result

    target = plan.get("target")
    rows = plan.get("files")
    if not isinstance(target, str) or not check.GIT_SHA_RE.fullmatch(target):
        raise ValueError("Invalid pinned target commit")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("Invalid plan file count")

    files = []
    groups = {}
    available = blocked = 0
    for row in rows:
        path = row.get("path")
        action = row.get("action")
        if check.managed_path("home-assistant/" + str(path)) != path:
            raise ValueError("Unexpected managed file")
        if action in IGNORED:
            continue
        if action not in CANDIDATES | BLOCKED:
            raise ValueError("Unexpected plan action")
        group_id, label = group_for(path)
        # Only JS is installable in the approved Deploy architecture.
        installable = path.startswith("www/") and path.endswith(".js") and action in CANDIDATES
        state = "available" if installable else "blocked"
        if installable:
            available += 1
        else:
            blocked += 1
        files.append({"path": path, "group": group_id, "action": action, "status": state})
        if group_id not in groups:
            groups[group_id] = {"id": group_id, "label": label, "count": 0, "blocked_count": 0}
        groups[group_id]["count" if installable else "blocked_count"] += 1

    result.update({
        "status": "blocked" if blocked else "ok",
        "target_commit": target,
        "available_count": available,
        "blocked_count": blocked,
        "groups": list(groups.values()),
        "files": files,
        "message": "Есть конфликтующие или требующие ручной установки файлы" if blocked else None,
    })
    return result


def failure(at: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "status": "error",
        "checked_at": None,
        "attempted_at": at or utc_now(),
        "target_commit": None,
        "available_count": None,
        "blocked_count": None,
        "groups": [],
        "files": [],
        "message": "Проверка GitHub/манифеста не удалась; количество обновлений неизвестно",
    }


def publish(payload: dict) -> None:
    """Create the single runtime JSON atomically within the HA configuration mount."""
    data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > 128 * 1024:
        raise ValueError("Status exceeds size limit")

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    root_fd = os.open(check.CONFIG_ROOT, flags)
    dir_fd = None
    tmp = ".status-" + secrets.token_hex(12) + ".tmp"
    try:
        try:
            os.mkdir(STATUS_DIR, 0o755, dir_fd=root_fd)
        except FileExistsError:
            pass
        dir_fd = os.open(STATUS_DIR, flags, dir_fd=root_fd)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, STATUS_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
        finally:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
        os.close(root_fd)


def publish_plan(plan: dict) -> dict:
    payload = snapshot(plan)
    with _LOCK:
        publish(payload)
    return payload


def refresh() -> dict:
    """Background/explicit check: fail closed and never trigger installation."""
    with _LOCK:
        try:
            result = snapshot(sync_plan.prepare_sync_plan())
        except Exception as exc:
            print("Deploy background check failed:", type(exc).__name__, flush=True)
            result = failure()
        try:
            publish(result)
        except (OSError, ValueError) as exc:
            # Last good file will be marked stale by HA; no false zero is emitted.
            print("Deploy status publication failed:", type(exc).__name__, flush=True)
        return result


def run_periodically() -> None:
    """Start once at add-on startup, then every six hours."""
    while True:
        refresh()
        threading.Event().wait(INTERVAL_SECONDS)
