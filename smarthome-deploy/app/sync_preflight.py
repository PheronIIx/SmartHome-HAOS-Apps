"""Strictly read-only diagnostics for the existing opt-in JS-only transaction.

All outputs are diagnostics, not capabilities or approvals. The JS writer MUST
re-fetch the commit, re-check local bytes and verify backup/restore safeguards.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat

from . import check, sync_plan

CRITICAL_PATHS = frozenset({
    "configuration.yaml", "automations.yaml", "scripts.yaml",
    "packages/infra.yaml", "packages/notify.yaml", "packages/scenarios.yaml",
})
WRITE_ACTIONS = frozenset({"candidate_review", "new_file_review", "deletion_review"})
BLOCKING_ACTIONS = frozenset({
    "blocked_local_drift", "blocked_unverified", "new_file_conflict",
})
NO_WRITE_ACTIONS = frozenset({"no_change", "format_only", "new_file_present"})
MAX_PREFLIGHT_FILES = 100


def _file_snapshot(relative: str) -> dict:
    """Read current bytes by no-follow directory descriptors, never write."""
    if check.managed_path("home-assistant/" + relative) != relative:
        raise ValueError("Not an allowlisted HA path")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    root = os.open(check.CONFIG_ROOT, flags | os.O_DIRECTORY)
    parent = root
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            except FileNotFoundError:
                return {"exists": False, "sha256": None, "size": None}
            if parent != root:
                os.close(parent)
            parent = next_fd
        try:
            fd = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return {"exists": False, "sha256": None, "size": None}
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > check.MAX_DIFF_BYTES:
                raise ValueError("Local file is not a regular file within size limit")
            data = os.read(fd, check.MAX_DIFF_BYTES + 1)
            if len(data) != info.st_size:
                raise ValueError("Local file changed while reading")
            now = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns
            ):
                raise ValueError("Local file changed during preflight")
            return {"exists": True, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        finally:
            os.close(fd)
    finally:
        if parent != root:
            os.close(parent)
        os.close(root)


def _risk(relative: str) -> str:
    if relative in CRITICAL_PATHS:
        return "critical"
    if relative.endswith((".yaml", ".yml")):
        return "yaml"
    return "standard"


def prepare_preflight() -> dict:
    """Collect a pinned, complete preview of possible changes; never permit writes."""
    plan = sync_plan.prepare_sync_plan()
    if not plan.get("configured"):
        return {
            "configured": False, "ready_to_install": False,
            "message": plan.get("message", "Initialize installed baseline first."),
            "files": [],
        }

    options = check.load_options()
    repo = str(options.get("repository", "")).strip()
    token = str(options.get("github_token", "")).strip()
    target = plan["target"]
    if not check.REPOSITORY_RE.fullmatch(repo) or not token or not check.GIT_SHA_RE.fullmatch(target):
        raise ValueError("Invalid pinned source repository/commit")
    rows = plan.get("files", [])
    if not isinstance(rows, list) or len(rows) > MAX_PREFLIGHT_FILES:
        raise ValueError("Invalid or oversized sync plan")

    out = []
    for row in rows:
        relative = row.get("path")
        action = row.get("action")
        if not isinstance(relative, str) or check.managed_path("home-assistant/" + relative) != relative:
            raise ValueError("Unexpected managed path in sync plan")
        if action not in WRITE_ACTIONS | BLOCKING_ACTIONS | NO_WRITE_ACTIONS:
            raise ValueError("Unknown sync action")
        if row.get("target_sha") is not None and not check.GIT_SHA_RE.fullmatch(str(row["target_sha"])):
            raise ValueError("Invalid target blob")
        if row.get("baseline_sha") is not None and not check.GIT_SHA_RE.fullmatch(str(row["baseline_sha"])):
            raise ValueError("Invalid baseline blob")

        risk = _risk(relative)
        snapshot = None
        snapshot_error = None
        try:
            snapshot = _file_snapshot(relative)
        except (OSError, ValueError) as error:
            snapshot_error = type(error).__name__

        if action == "candidate_review":
            disposition = "update"
        elif action == "new_file_review":
            disposition = "add"
        elif action == "deletion_review":
            disposition = "delete_review"
        elif action in BLOCKING_ACTIONS:
            disposition = "blocked"
        else:
            disposition = "skip"

        if snapshot is None:
            disposition = "blocked"
        elif disposition in {"update", "delete_review"} and not snapshot["exists"]:
            disposition = "blocked"
        elif disposition == "add" and snapshot["exists"]:
            disposition = "blocked"

        target_sha256 = None
        target_size = None
        # Pin contents by immutable blob identifier, not by moving branch URL.
        if disposition in {"update", "add"}:
            blob = check._load_blob_for_diff(repo, token, row["target_sha"])
            target_sha256 = hashlib.sha256(blob).hexdigest()
            target_size = len(blob)
        if action in BLOCKING_ACTIONS:
            disposition = "blocked"

        out.append({
            "path": relative, "source_action": action, "disposition": disposition,
            "local_state": row.get("local"), "risk": risk,
            "local": snapshot,
            "snapshot_error": snapshot_error,
            "installed_blob": row.get("baseline_sha"),
            "target_blob": row.get("target_sha"),
            "target_sha256": target_sha256, "target_size": target_size,
            "requires_full_haos_backup": risk == "critical" and disposition != "skip",
            "requires_ha_config_check": risk in {"critical", "yaml"} and disposition != "skip",
        })

    counts = {key: sum(x["disposition"] == key for x in out)
              for key in ("update", "add", "delete_review", "blocked", "skip")}
    has_yaml = any(x["requires_ha_config_check"] for x in out)
    has_critical = any(x["requires_full_haos_backup"] for x in out)
    identity = {
        "target_commit": target,
        "files": [{k: entry[k] for k in (
            "path", "source_action", "disposition", "local",
            "installed_blob", "target_blob", "target_sha256"
        )} for entry in out],
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "configured": True, "mode": "read_only_preflight",
        "target_commit": target, "plan_fingerprint": fingerprint,
        "counts": counts,
        "requires_yaml_validation": has_yaml,
        "requires_fresh_full_haos_backup": has_critical,
        "requires_explicit_conflict_consent": any(
            x["source_action"] in BLOCKING_ACTIONS for x in out
        ),
        "ready_to_install": False, "write_permitted": False,
        "files": out,
        "message": "Предустановочная проверка выполнена без записи в HA. Установка отключена.",
    }
