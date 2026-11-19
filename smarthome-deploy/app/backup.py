"""Secure backup and isolated restore rehearsal for Home Assistant managed files.

No writes to the mounted Home Assistant configuration are performed here.
Backups live solely in the add-on /data directory. Restore targets must be an
explicit, separate staging directory (never HA /config).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import zipfile

from .check import MAX_DIFF_BYTES, managed_path, safe_local_file

BACKUP_ROOT = Path(os.environ.get("SMARTHOME_BACKUP_ROOT", "/data/backups"))
RESTORE_ROOT = Path(os.environ.get("SMARTHOME_RESTORE_ROOT", "/data/restore-rehearsals"))
BACKUP_NAME = re.compile(r"^[0-9a-f]{24}\.zip$")
MAX_FILES = 100
MAX_TOTAL_BYTES = 16 * 1024 * 1024


def _valid_path(relative: str) -> None:
    if managed_path("home-assistant/" + relative) != relative:
        raise ValueError("Path is not allowlisted")
    if relative.startswith("/") or "\\" in relative or ".." in Path(relative).parts:
        raise ValueError("Invalid path")


def _read_local(relative: str) -> bytes:
    _valid_path(relative)
    file = safe_local_file(relative)
    if not file.is_file() or file.is_symlink():
        raise ValueError("Local source missing, not regular or symlinked")
    size = file.stat().st_size
    if size > MAX_DIFF_BYTES:
        raise ValueError("Source exceeds single file backup limit")
    with file.open("rb") as handle:
        data = handle.read(MAX_DIFF_BYTES + 1)
    if len(data) > MAX_DIFF_BYTES:
        raise ValueError("Source changed size during read")
    return data


def _validate_backup_dir() -> Path:
    root = BACKUP_ROOT
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Backup directory must be real")
    os.chmod(root, 0o700)
    return root


def _manifest_bytes(files: dict[str, bytes], source_commit: str) -> bytes:
    entries = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
               for name, data in sorted(files.items())]
    return json.dumps({"version": 1, "source_commit": source_commit, "files": entries},
                      ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def create_snapshot(paths: list[str], source_commit: str) -> dict:
    """Create an atomic zip backup from an explicit, strictly allowlisted file list."""
    if not isinstance(paths, list) or not paths or len(paths) > MAX_FILES or len(set(paths)) != len(paths):
        raise ValueError("Expected a unique, non-empty managed file list")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("An immutable source commit SHA is required")
    files = {}
    total = 0
    for path in sorted(paths):
        data = _read_local(path)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Backup exceeds overall size limit")
        files[path] = data
    # After reading every file, re-read and compare to block inconsistent snapshots
    # if any managed file changes concurrently.
    for path, data in files.items():
        if _read_local(path) != data:
            raise RuntimeError("File changed while preparing snapshot; aborted")

    manifest = _manifest_bytes(files, source_commit)
    destination = _validate_backup_dir()
    name = secrets.token_hex(12) + ".zip"
    target = destination / name
    handle = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=destination)
        handle = Path(tmp_name)
        with os.fdopen(fd, "wb") as output:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                z.writestr("manifest.json", manifest)
                for path, data in sorted(files.items()):
                    z.writestr("files/" + path, data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(handle, 0o600)
        os.replace(handle, target)
        # Snapshot is complete, but its contents must still be verified before
        # restore. Files in /data are not an off-host disaster-recovery backup.
        return {"backup": name, "source_commit": source_commit,
                "file_count": len(files), "bytes": total}
    finally:
        if handle is not None:
            handle.unlink(missing_ok=True)


def verify_snapshot(name: str) -> tuple[dict, dict[str, bytes]]:
    """Validate all archive entries and hashes without extracting paths."""
    if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
        raise ValueError("Invalid backup identifier")
    root = BACKUP_ROOT
    if root.is_symlink():
        raise ValueError("Backup root is symlinked")
    location = root / name
    if location.is_symlink() or not location.is_file():
        raise ValueError("Backup does not exist")
    if location.stat().st_size > MAX_TOTAL_BYTES + 1024 * 1024:
        raise ValueError("Backup exceeds size limit")
    with zipfile.ZipFile(location, "r") as archive:
        names = archive.namelist()
        if len(names) > MAX_FILES + 1 or len(names) != len(set(names)) or "manifest.json" not in names:
            raise ValueError("Invalid archive members")
        raw_manifest = archive.read("manifest.json")
        if len(raw_manifest) > 100 * 1024:
            raise ValueError("Manifest exceeds size limit")
        manifest = json.loads(raw_manifest)
        if manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
            raise ValueError("Unsupported manifest")
        if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_commit"))):
            raise ValueError("Invalid commit")
        expected = ["manifest.json"]
        data = {}
        total = 0
        for entry in manifest["files"]:
            path = entry.get("path")
            if not isinstance(path, str):
                raise ValueError("Invalid file path")
            _valid_path(path)
            if path in data:
                raise ValueError("Duplicate file in manifest")
            info = archive.getinfo("files/" + path)
            if info.file_size > MAX_DIFF_BYTES:
                raise ValueError("File too large")
            content = archive.read(info)
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("Archive payload too large")
            if len(content) != entry.get("size") or hashlib.sha256(content).hexdigest() != entry.get("sha256"):
                raise ValueError("Backup integrity check failed")
            data[path] = content
            expected.append("files/" + path)
        if set(names) != set(expected):
            raise ValueError("Unexpected archive members")
    return manifest, data


def rehearse_restore(name: str, target_directory: Path) -> dict:
    """Verify then restore to an isolated empty staging directory, never /config."""
    manifest, data = verify_snapshot(name)
    target = Path(target_directory)
    if target.is_symlink():
        raise ValueError("Symlinked staging destination")
    restore_root = RESTORE_ROOT.resolve(strict=False)
    target_abs = target.resolve(strict=False)
    if not target_abs.is_relative_to(restore_root) or target_abs == restore_root:
        raise ValueError("Restore destination must be inside isolated staging root")
    from . import check
    ha_root = check.CONFIG_ROOT.resolve(strict=False)
    if target_abs == ha_root or target_abs.is_relative_to(ha_root):
        raise ValueError("Cannot restore into Home Assistant config")
    if target_abs == BACKUP_ROOT.resolve(strict=False) or target_abs.is_relative_to(BACKUP_ROOT.resolve(strict=False)):
        raise ValueError("Cannot restore into backup archive directory")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("Staging directory must be empty")
    target.mkdir(parents=True, exist_ok=True)
    for path, contents in data.items():
        destination = target / path
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
    for path, contents in data.items():
        if (target / path).read_bytes() != contents:
            raise RuntimeError("Staging restore verification failed")
    return {"backup": name, "file_count": len(data), "verified": True,
            "source_commit": manifest["source_commit"]}
