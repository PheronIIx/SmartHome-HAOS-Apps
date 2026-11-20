"""Persistent per-file installed baseline manifest.

Initialization is explicit and never writes to Home Assistant. New allowlist
paths are adopted only when local bytes already match the current GitHub file.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote

from . import check

MANIFEST_PATH = Path(os.environ.get("SMARTHOME_MANIFEST_PATH", "/data/installed-manifest.json"))
SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024


def _tree(repository: str, token: str, commit_id: str) -> dict[str, str]:
    base = "repos/" + quote(repository, safe="/")
    commit = check.github_json(base + "/commits/" + quote(commit_id, safe=""), token)
    actual = commit.get("sha")
    tree_sha = (commit.get("commit") or {}).get("tree", {}).get("sha")
    if actual != commit_id or not check.GIT_SHA_RE.fullmatch(str(tree_sha)):
        raise RuntimeError("Invalid baseline commit/tree")
    result = check.github_json(base + "/git/trees/" + tree_sha + "?recursive=1", token)
    if result.get("truncated") or not isinstance(result.get("tree"), list):
        raise RuntimeError("Incomplete GitHub tree; manifest initialization blocked")
    files = {}
    for item in result["tree"]:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = item.get("path")
        relative = check.managed_path(path) if isinstance(path, str) else None
        if relative is None:
            continue
        sha = item.get("sha")
        if not isinstance(sha, str) or not check.GIT_SHA_RE.fullmatch(sha):
            raise RuntimeError("Invalid managed blob SHA")
        if relative in files:
            raise RuntimeError("Duplicate managed path in Git tree")
        files[relative] = sha
        if len(files) > check.MAX_TRACKED_FILES:
            raise RuntimeError("Too many managed files")
    return files


def _head(repository: str, branch: str, token: str) -> str:
    value = check.github_json(
        "repos/" + quote(repository, safe="/") + "/commits/" + quote(branch, safe=""), token
    ).get("sha")
    if not isinstance(value, str) or not check.GIT_SHA_RE.fullmatch(value):
        raise RuntimeError("Invalid target branch SHA")
    return value


def _local_bytes(relative: str) -> bytes | None:
    path = check.safe_local_file(relative)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > check.MAX_DIFF_BYTES:
        raise ValueError("Local file is not a regular file within the manifest size limit")
    data = path.read_bytes()
    if len(data) > check.MAX_DIFF_BYTES:
        raise ValueError("Local file exceeds manifest size limit")
    return data


def _entry(source_commit: str, blob_sha: str, data: bytes) -> dict:
    return {
        "source_commit": source_commit,
        "blob_sha": blob_sha,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _atomic_write(payload: dict) -> None:
    target = MANIFEST_PATH
    parent = target.parent
    if parent.is_symlink():
        raise ValueError("Manifest directory cannot be a symlink")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("Manifest cannot be a symlink")
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest exceeds size limit")
    fd, temporary = tempfile.mkstemp(prefix=".installed-manifest-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_manifest(options: dict | None = None) -> dict | None:
    target = MANIFEST_PATH
    if target.is_symlink():
        raise ValueError("Manifest cannot be a symlink")
    if not target.exists():
        return None
    if not target.is_file() or target.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Invalid manifest file")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Manifest is not valid JSON") from None
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported installed manifest schema")
    if not isinstance(payload.get("files"), dict) or not payload["files"]:
        raise ValueError("Manifest has no managed files")
    if len(payload["files"]) > check.MAX_TRACKED_FILES:
        raise ValueError("Manifest contains too many files")
    if options is not None:
        if payload.get("repository") != options.get("repository") or payload.get("branch") != options.get("branch"):
            raise ValueError("Manifest repository/branch differs from current app settings")
    for relative, entry in payload["files"].items():
        if check.managed_path("home-assistant/" + relative) != relative or not isinstance(entry, dict):
            raise ValueError("Manifest contains a non-allowlisted path")
        if not check.GIT_SHA_RE.fullmatch(str(entry.get("source_commit", ""))):
            raise ValueError("Invalid per-file source commit")
        if not check.GIT_SHA_RE.fullmatch(str(entry.get("blob_sha", ""))):
            raise ValueError("Invalid per-file Git blob SHA")
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            raise ValueError("Invalid per-file SHA-256")
        size = entry.get("size")
        if not isinstance(size, int) or size < 0 or size > check.MAX_DIFF_BYTES:
            raise ValueError("Invalid per-file size")
    return payload


def initialize_manifest(confirmation: str) -> dict:
    """Create the first manifest from the configured legacy baseline."""
    if confirmation != "INITIALIZE MANIFEST":
        raise ValueError("Explicit manifest initialization confirmation required")
    options = check.load_options()
    repository = str(options.get("repository", "")).strip()
    branch = str(options.get("branch", "")).strip()
    token = str(options.get("github_token", "")).strip()
    baseline_commit = str(options.get("baseline_commit", "")).strip()
    if not check.REPOSITORY_RE.fullmatch(repository) or not token:
        raise ValueError("GitHub is not configured")
    if not check.BRANCH_RE.fullmatch(branch) or ".." in branch or branch.startswith("/") or branch.endswith("/"):
        raise ValueError("Invalid branch")
    if not check.GIT_SHA_RE.fullmatch(baseline_commit):
        raise ValueError("A confirmed legacy baseline_commit is required")
    if MANIFEST_PATH.exists() or MANIFEST_PATH.is_symlink():
        raise ValueError("Installed manifest already exists; initialization is one-time")

    baseline = _tree(repository, token, baseline_commit)
    target_commit = _head(repository, branch, token)
    target = _tree(repository, token, target_commit)
    files = {}
    adopted = []
    untracked_local = []
    untracked_missing = []

    for relative, blob_sha in baseline.items():
        contents = check._load_blob_for_diff(repository, token, blob_sha)
        files[relative] = _entry(baseline_commit, blob_sha, contents)

    # A newly allowlisted path is adopted only when HA already matches main.
    for relative, blob_sha in target.items():
        if relative in files:
            continue
        local = _local_bytes(relative)
        if local is None:
            untracked_missing.append(relative)
            continue
        remote = check._load_blob_for_diff(repository, token, blob_sha)
        if check._normalize_line_ends(local) == check._normalize_line_ends(remote):
            files[relative] = _entry(target_commit, blob_sha, remote)
            adopted.append(relative)
        else:
            untracked_local.append(relative)

    if not files:
        raise RuntimeError("No allowlisted files found in the approved baseline")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "branch": branch,
        "initialized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "initialized_from_commit": baseline_commit,
        "target_at_initialization": target_commit,
        "files": dict(sorted(files.items())),
    }
    _atomic_write(payload)
    return {
        "initialized": True,
        "manifest": MANIFEST_PATH.name,
        "file_count": len(files),
        "adopted_new_paths": adopted,
        "untracked_local_paths": untracked_local,
        "untracked_missing_paths": untracked_missing,
        "message": "Манифест создан в /data. Конфигурация Home Assistant не изменялась.",
    }
