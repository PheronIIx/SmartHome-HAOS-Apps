"""Read-only multi-file synchronization planner based on per-file installed baselines."""
from __future__ import annotations

import difflib
import hashlib

from . import baseline, check, plan


def _local_bytes(relative: str) -> bytes | None:
    path = check.safe_local_file(relative)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > check.MAX_DIFF_BYTES:
        raise ValueError("Local file is not a regular file within the diff size limit")
    data = path.read_bytes()
    if len(data) > check.MAX_DIFF_BYTES:
        raise ValueError("Local file exceeds diff size limit")
    return data


def _classify_against_baseline(relative: str, entry: dict, repository: str, token: str) -> str:
    local = _local_bytes(relative)
    if local is None:
        return "local_missing"
    if hashlib.sha256(local).hexdigest() == entry["sha256"]:
        return "baseline_exact"
    try:
        old = check._load_blob_for_diff(repository, token, entry["blob_sha"])
    except (ValueError, RuntimeError):
        return "unverified"
    return "baseline_format_only" if check._normalize_line_ends(local) == check._normalize_line_ends(old) else "local_drift"


def _classify_new_file(relative: str, target_sha: str, repository: str, token: str) -> str:
    local = _local_bytes(relative)
    if local is None:
        return "local_missing"
    try:
        remote = check._load_blob_for_diff(repository, token, target_sha)
    except (ValueError, RuntimeError):
        return "unverified"
    if local == remote:
        return "target_exact"
    if check._normalize_line_ends(local) == check._normalize_line_ends(remote):
        return "target_format_only"
    return "untracked_local"


def prepare_sync_plan() -> dict:
    options = check.load_options()
    repository = str(options.get("repository", "")).strip()
    branch = str(options.get("branch", "")).strip()
    token = str(options.get("github_token", "")).strip()
    if not check.REPOSITORY_RE.fullmatch(repository) or not token:
        raise ValueError("GitHub is not configured")
    if not check.BRANCH_RE.fullmatch(branch) or ".." in branch or branch.startswith("/") or branch.endswith("/"):
        raise ValueError("Invalid branch")
    if not check.CONFIG_ROOT.is_dir():
        raise RuntimeError("Home Assistant configuration mount is unavailable")

    installed = baseline.load_manifest(options)
    if installed is None:
        return {
            "configured": False, "requires_initialization": True,
            "message": "Сначала явно инициализируйте манифест установленной версии. Запись в HA не выполняется.",
            "files": [],
        }

    from urllib.parse import quote
    head = check.github_json(
        "repos/" + quote(repository, safe="/") + "/commits/" + quote(branch, safe=""), token
    )
    target_commit = head.get("sha")
    if not isinstance(target_commit, str) or not check.GIT_SHA_RE.fullmatch(target_commit):
        raise RuntimeError("Invalid target commit")
    target = plan._managed_tree(repository, token, target_commit)
    previous = installed["files"]
    rows = []

    for relative in sorted(set(previous) | set(target)):
        old = previous.get(relative)
        new_sha = target.get(relative)
        if old is None:
            local = _classify_new_file(relative, new_sha, repository, token)
            if local == "local_missing":
                action = "new_file_review"
            elif local in ("target_exact", "target_format_only"):
                action = "new_file_present"
            else:
                action = "new_file_conflict"
            rows.append({
                "path": relative, "action": action, "local": local,
                "baseline_sha": None, "target_sha": new_sha,
            })
            continue

        try:
            local = _classify_against_baseline(relative, old, repository, token)
        except (OSError, ValueError):
            local = "unverified"
        if new_sha is None:
            action = "blocked_local_drift" if local in ("local_drift", "local_missing", "unverified") else "deletion_review"
        elif local in ("local_drift", "local_missing", "unverified"):
            action = "blocked_local_drift"
        elif new_sha == old["blob_sha"]:
            action = "no_change"
        else:
            action = "candidate_review"
        rows.append({
            "path": relative, "action": action, "local": local,
            "baseline_sha": old["blob_sha"], "baseline_commit": old["source_commit"],
            "target_sha": new_sha,
        })

    rows.sort(key=lambda item: item["path"])
    totals = {name: sum(row["action"] == name for row in rows) for name in (
        "no_change", "candidate_review", "blocked_local_drift", "new_file_review",
        "new_file_present", "new_file_conflict", "deletion_review",
    )}
    return {
        "configured": True, "mode": "read-only-per-file-plan",
        "baseline_files": len(previous), "target": target_commit,
        "initialized_from_commit": installed["initialized_from_commit"],
        "totals": totals, "files": rows, "ready_to_install": False,
        "message": "Предварительный план только для просмотра. Многофайловая установка ещё не включена.",
    }


def _diff(local: bytes | None, remote: bytes | None, local_name: str, remote_name: str) -> dict:
    if remote is None:
        return {"kind": "remote_missing", "diff": [], "truncated": False}
    if local is None:
        local = b""
        kind = "local_missing"
    elif local == remote:
        return {"kind": "same", "diff": [], "truncated": False}
    elif check._normalize_line_ends(local) == check._normalize_line_ends(remote):
        return {"kind": "format_only", "diff": [], "truncated": False}
    else:
        kind = "content_changed"
    try:
        left = local.decode("utf-8").replace("\r\n", "\n").splitlines(keepends=True)
        right = remote.decode("utf-8").replace("\r\n", "\n").splitlines(keepends=True)
    except UnicodeDecodeError:
        return {"kind": "binary_or_non_utf8", "diff": [], "truncated": False}
    result = []
    truncated = False
    for index, line in enumerate(difflib.unified_diff(left, right, fromfile=local_name, tofile=remote_name, n=3)):
        if index >= check.MAX_DIFF_LINES:
            truncated = True
            break
        result.append(line.rstrip("\r\n"))
    return {"kind": kind, "diff": result, "truncated": truncated}


def file_diff(relative: str, target_commit: str) -> dict:
    if check.managed_path("home-assistant/" + relative) != relative:
        raise ValueError("File is not allowlisted")
    if not check.GIT_SHA_RE.fullmatch(target_commit):
        raise ValueError("Invalid target commit")
    options = check.load_options()
    repository = str(options.get("repository", "")).strip()
    token = str(options.get("github_token", "")).strip()
    if not check.REPOSITORY_RE.fullmatch(repository) or not token:
        raise ValueError("GitHub is not configured")
    installed = baseline.load_manifest(options)
    if installed is None:
        raise ValueError("Installed manifest is not initialized")
    local = _local_bytes(relative)
    old = installed["files"].get(relative)
    baseline_bytes = check._load_blob_for_diff(repository, token, old["blob_sha"]) if old else None
    target_tree = plan._managed_tree(repository, token, target_commit)
    target_sha = target_tree.get(relative)
    target_bytes = check._load_blob_for_diff(repository, token, target_sha) if target_sha else None
    return {
        "path": relative, "target_commit": target_commit,
        "baseline_commit": old["source_commit"] if old else None,
        "baseline": _diff(local, baseline_bytes, "HA/" + relative, "Installed baseline/" + relative)
            if old else {"kind": "not_tracked", "diff": [], "truncated": False},
        "target": _diff(local, target_bytes, "HA/" + relative, "GitHub/" + relative),
    }
