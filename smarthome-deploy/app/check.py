"""Compare GitHub Git blob IDs against a read-only HA configuration mount."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

CONFIG_ROOT = Path(os.environ.get("SMARTHOME_CONFIG_ROOT", "/homeassistant"))
OPTIONS_FILE = Path(os.environ.get("SMARTHOME_OPTIONS_FILE", "/data/options.json"))
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_TRACKED_FILES = 300
MAX_LOCAL_FILE_BYTES = 8 * 1024 * 1024

def managed_path(repository_path: str) -> str | None:
    prefix = "home-assistant/"
    if not repository_path.startswith(prefix):
        return None
    relative = repository_path[len(prefix):]
    if relative in {"configuration.yaml", "automations.yaml", "scripts.yaml", "scenes.yaml"}:
        return relative
    parts = relative.split("/")
    if len(parts) == 2 and parts[0] == "packages" and parts[1].endswith(".yaml"):
        return relative
    if len(parts) == 2 and parts[0] == "www" and parts[1].endswith(".js"):
        return relative
    if len(parts) == 3 and parts[0:2] == ["www", "plans"] and parts[2].endswith(".json"):
        return relative
    return None

def safe_local_file(relative: str) -> Path:
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("invalid managed path")
    base = CONFIG_ROOT
    if base.is_symlink():
        raise ValueError("configuration root is a symlink")
    current = base
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink is not allowed")
    if not current.resolve(strict=False).is_relative_to(base.resolve(strict=False)):
        raise ValueError("path escapes configuration root")
    return current

def git_blob_sha(file: Path) -> str:
    size = file.stat().st_size
    if size > MAX_LOCAL_FILE_BYTES:
        raise ValueError("local file exceeds comparison limit")
    digest = hashlib.sha1(f"blob {size}\0".encode("ascii"))
    with file.open("rb") as stream:
        while data := stream.read(65536):
            digest.update(data)
    return digest.hexdigest()

def github_json(path: str, token: str) -> dict:
    request = Request("https://api.github.com/" + path, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "SmartHome-Deploy-ReadOnly/0.1",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": "Bearer " + token,
    })
    try:
        with urlopen(request, timeout=12) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("GitHub response exceeded size limit")
        result = json.loads(data)
    except HTTPError as error:
        raise RuntimeError(f"GitHub HTTP {error.code}; check token and repository access") from None
    except (URLError, TimeoutError):
        raise RuntimeError("GitHub connection failed") from None
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected GitHub response")
    return result

def load_options() -> dict:
    with OPTIONS_FILE.open(encoding="utf-8") as handle:
        return json.load(handle)

def check() -> dict:
    options = load_options()
    repository = str(options.get("repository", "")).strip()
    branch = str(options.get("branch", "")).strip()
    token = str(options.get("github_token", "")).strip()
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError("Invalid repository option (expected owner/name)")
    if not BRANCH_RE.fullmatch(branch) or ".." in branch or branch.startswith("/") or branch.endswith("/"):
        raise ValueError("Invalid branch option")
    if not token:
        return {"configured": False, "repository": repository, "branch": branch,
                "message": "Укажите токен GitHub с правом Contents: Read в настройках приложения.",
                "files": []}
    if not CONFIG_ROOT.is_dir():
        raise RuntimeError("Read-only Home Assistant configuration mount is unavailable")
    base_url = "repos/" + quote(repository, safe="/")
    commit = github_json(base_url + "/commits/" + quote(branch, safe=""), token)
    commit_sha = commit.get("sha")
    tree_sha = (commit.get("commit") or {}).get("tree", {}).get("sha")
    if not GIT_SHA_RE.fullmatch(str(commit_sha)) or not GIT_SHA_RE.fullmatch(str(tree_sha)):
        raise RuntimeError("Invalid GitHub commit/tree response")
    tree = github_json(base_url + "/git/trees/" + tree_sha + "?recursive=1", token)
    if tree.get("truncated"):
        raise RuntimeError("GitHub tree was truncated; comparison blocked")
    entries = tree.get("tree")
    if not isinstance(entries, list):
        raise RuntimeError("Missing GitHub file tree")
    files = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        source_path = entry.get("path")
        relative = managed_path(source_path) if isinstance(source_path, str) else None
        if relative is None:
            continue
        remote_sha = entry.get("sha")
        if not isinstance(remote_sha, str) or not GIT_SHA_RE.fullmatch(remote_sha):
            raise RuntimeError("Unexpected managed blob identifier")
        if len(files) >= MAX_TRACKED_FILES:
            raise RuntimeError("Too many managed files to compare safely")
        record = {"path": relative, "github_sha": remote_sha, "status": "unknown"}
        try:
            file = safe_local_file(relative)
            if not file.exists():
                record["status"] = "missing"
            elif not file.is_file():
                record["status"] = "error"
                record["detail"] = "Not a regular file"
            else:
                record["status"] = "same" if git_blob_sha(file) == remote_sha else "modified"
        except (OSError, ValueError) as error:
            record["status"] = "error"
            record["detail"] = str(error)
        files.append(record)
    files.sort(key=lambda item: item["path"])
    totals = {status: sum(f["status"] == status for f in files)
              for status in ("same", "modified", "missing", "error")}
    return {"configured": True, "repository": repository, "branch": branch,
            "commit": commit_sha, "files": files, "totals": totals,
            "mode": "read-only", "deploy_enabled": False}
