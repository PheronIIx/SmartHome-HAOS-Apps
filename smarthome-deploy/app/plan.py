"""Dry-run deployment planner: no writing and no deployment endpoints.

Trust model: a manually approved baseline commit is a reference, not proof
that the entire local filesystem was identical to it.
"""
from __future__ import annotations

from .check import (
    CONFIG_ROOT, GIT_SHA_RE, MAX_DIFF_BYTES, REPOSITORY_RE, _load_blob_for_diff,
    _normalize_line_ends, github_json, load_options, managed_path, safe_local_file,
)
from urllib.parse import quote


def _managed_tree(repository: str, token: str, commit_id: str) -> dict[str, str]:
    base = "repos/" + quote(repository, safe="/")
    commit = github_json(base + "/commits/" + commit_id, token)
    sha = commit.get("sha")
    tree_sha = (commit.get("commit") or {}).get("tree", {}).get("sha")
    if not GIT_SHA_RE.fullmatch(str(sha)) or not GIT_SHA_RE.fullmatch(str(tree_sha)):
        raise RuntimeError("Invalid GitHub commit tree")
    tree = github_json(base + "/git/trees/" + tree_sha + "?recursive=1", token)
    if tree.get("truncated") or not isinstance(tree.get("tree"), list):
        raise RuntimeError("Incomplete GitHub tree; plan blocked")
    result = {}
    for entry in tree["tree"]:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = entry.get("path")
        relative = managed_path(path) if isinstance(path, str) else None
        if relative is None:
            continue
        blob = entry.get("sha")
        if not isinstance(blob, str) or not GIT_SHA_RE.fullmatch(blob):
            raise RuntimeError("Invalid GitHub blob")
        if relative in result:
            raise RuntimeError("Duplicate managed file")
        result[relative] = blob
        if len(result) > 300:
            raise RuntimeError("Too many managed files")
    return result


def _classify_local(path: str, baseline_sha: str, repository: str, token: str) -> str:
    """Classify local content against baseline; no claims for oversized files."""
    file = safe_local_file(path)
    if not file.exists():
        return "local_missing"
    if not file.is_file() or file.stat().st_size > MAX_DIFF_BYTES:
        return "unverified"
    local = file.read_bytes()
    import hashlib
    sha = hashlib.sha1(b"blob " + str(len(local)).encode("ascii") + b"\0" + local).hexdigest()
    if sha == baseline_sha:
        return "baseline_exact"
    try:
        baseline = _load_blob_for_diff(repository, token, baseline_sha)
    except (ValueError, RuntimeError):
        return "unverified"
    if _normalize_line_ends(local) == _normalize_line_ends(baseline):
        return "baseline_format_only"
    return "local_drift"


def prepare_plan() -> dict:
    options = load_options()
    repository = str(options.get("repository", "")).strip()
    branch = str(options.get("branch", "")).strip()
    token = str(options.get("github_token", "")).strip()
    baseline_commit = str(options.get("baseline_commit", "")).strip()
    if not REPOSITORY_RE.fullmatch(repository) or not token:
        raise ValueError("GitHub is not configured")
    if not GIT_SHA_RE.fullmatch(baseline_commit):
        return {"configured": False, "message": "Укажите подтверждённый базовый коммит (40 символов SHA) в настройках приложения.", "files": []}
    from .check import BRANCH_RE
    if not BRANCH_RE.fullmatch(branch) or ".." in branch or branch.startswith("/") or branch.endswith("/"):
        raise ValueError("Invalid branch")
    if not CONFIG_ROOT.is_dir():
        raise RuntimeError("HA configuration mount is unavailable")
    baseline = _managed_tree(repository, token, baseline_commit)
    # Resolve branch once, and then use a pinned immutable SHA for the final tree.
    # Target branch can move between API requests, so the returned commit identifies
    # exactly which tree was inspected.
    # _managed_tree accepts a ref; retrieve its actual SHA explicitly for the UI.
    head = github_json("repos/" + quote(repository, safe="/") + "/commits/" + quote(branch, safe=""), token)
    target_sha = head.get("sha")
    if not GIT_SHA_RE.fullmatch(str(target_sha)):
        raise RuntimeError("Invalid target SHA")
    target = _managed_tree(repository, token, target_sha)

    rows = []
    for path in sorted(set(baseline) | set(target)):
        old, new = baseline.get(path), target.get(path)
        if old is None:
            action = "new_file_review"
            local = "not_checked"
        else:
            try:
                local = _classify_local(path, old, repository, token)
            except (OSError, ValueError):
                local = "unverified"
            if new is None:
                action = "deletion_review"
            elif local in ("local_drift", "local_missing", "unverified"):
                action = "blocked_local_drift"
            elif new == old:
                action = "no_change"
            else:
                action = "candidate_review"
        rows.append({"path": path, "action": action, "local": local,
                     "baseline_sha": old, "target_sha": new})
    return {"configured": True, "mode": "read-only-plan",
            "baseline": baseline_commit, "target": target_sha,
            "ready_to_install": False, "files": rows,
            "message": "Это предварительный план: установка, резервирование и запись отключены."}
