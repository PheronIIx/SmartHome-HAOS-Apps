"""Opt-in JS-only multi-file transaction engine.

Allowlisted www/*.js may be updated or added; conflicts, YAML and deletion are forbidden.
The enable_multifile_install option must be explicitly enabled.
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

from . import backup, backup_service, baseline, check, deploy, sync_plan

HISTORY_ROOT = Path(os.environ.get("SMARTHOME_MULTIFILE_HISTORY", "/data/multifile-history"))
ID_RE = re.compile(r"^[0-9a-f]{24}$")
MAX_BATCH = 5
TTL = 300
_PENDING = {}


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _enabled():
    if check.load_options().get("enable_multifile_install") is not True:
        raise ValueError("Multi-file installation is disabled")


def _allowed(path):
    return isinstance(path, str) and path.startswith("www/") and path.endswith(".js") and (
        check.managed_path("home-assistant/" + path) == path
    )


def _directory(path):
    if not _allowed(path):
        raise ValueError("Only existing allowlisted JS files may be written")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    root = os.open(check.CONFIG_ROOT, flags)
    try:
        folder = os.open("www", flags, dir_fd=root)
    finally:
        os.close(root)
    return folder, path[4:]

def _absent(path):
    """Check absence without following even broken symlinks."""
    folder, name = _directory(path)
    try:
        try:
            os.stat(name, dir_fd=folder, follow_symlinks=False)
            return False
        except FileNotFoundError:
            return True
    finally:
        os.close(folder)


def _create(path, data):
    """Atomically create an absent JS without clobbering a newly appeared path."""
    if len(data) > check.MAX_DIFF_BYTES:
        raise ValueError("New JS exceeds size limit")
    folder, name = _directory(path)
    tmp = ".smarthome-batch-" + secrets.token_hex(12) + ".tmp"
    created = False
    try:
        if not _absent(path):
            raise ValueError("New JS path appeared")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=folder)
        created = True
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try: os.close(fd)
            except OSError: pass
            raise
        os.link(tmp, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
        os.fsync(folder)
        if _read(path)[0] != data:
            raise RuntimeError("New JS verification failed")
    finally:
        if created:
            try:
                os.unlink(tmp, dir_fd=folder)
                os.fsync(folder)
            except FileNotFoundError:
                pass
        os.close(folder)


def _remove_created(path, after_sha):
    """Only remove the still-unchanged file installed by this transaction."""
    folder, name = _directory(path)
    try:
        try:
            content, _ = _read(path)
        except FileNotFoundError:
            return
        if _hash(content) != after_sha:
            raise ValueError("Created JS changed; manual recovery required")
        os.unlink(name, dir_fd=folder)
        os.fsync(folder)
        if not _absent(path):
            raise RuntimeError("Created JS remains after rollback")
    finally:
        os.close(folder)


def _read(path):
    folder, name = _directory(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_size > check.MAX_DIFF_BYTES:
                raise ValueError("JS file is not a regular file within size limit")
            data = os.read(fd, check.MAX_DIFF_BYTES + 1)
            now = os.stat(name, dir_fd=folder, follow_symlinks=False)
            if len(data) != meta.st_size or (
                now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns
            ) != (meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns):
                raise ValueError("Local file changed during inspection")
            return data, meta
        finally:
            os.close(fd)
    finally:
        os.close(folder)


def _replace(path, old, new, metadata=None):
    if len(new) > check.MAX_DIFF_BYTES:
        raise ValueError("Replacement exceeds limit")
    folder, name = _directory(path)
    tmp = ".smarthome-batch-" + secrets.token_hex(12) + ".tmp"
    created = False
    try:
        current, meta = _read(path)
        if current != old:
            raise ValueError("Local file changed after approval")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=folder)
        created = True
        try:
            os.fchown(fd, meta.st_uid, meta.st_gid)
            os.fchmod(fd, stat.S_IMODE(meta.st_mode))
            with os.fdopen(fd, "wb") as stream:
                stream.write(new)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        if metadata:
            os.utime(tmp, ns=(metadata["atime_ns"], metadata["mtime_ns"]),
                     dir_fd=folder, follow_symlinks=False)
        latest = os.stat(name, dir_fd=folder, follow_symlinks=False)
        if (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns) != (
            meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns
        ):
            raise ValueError("Local file changed before rename")
        os.replace(tmp, name, src_dir_fd=folder, dst_dir_fd=folder)
        created = False
        os.fsync(folder)
        if _read(path)[0] != new:
            raise RuntimeError("Written content mismatch")
    finally:
        if created:
            try:
                os.unlink(tmp, dir_fd=folder)
            except FileNotFoundError:
                pass
        os.close(folder)


def _journal_root():
    if HISTORY_ROOT.is_symlink():
        raise ValueError("Symlinked history directory")
    HISTORY_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if HISTORY_ROOT.is_symlink() or not HISTORY_ROOT.is_dir():
        raise ValueError("Invalid history directory")
    os.chmod(HISTORY_ROOT, 0o700)
    return HISTORY_ROOT


def _save(record):
    if not ID_RE.fullmatch(str(record.get("id", ""))):
        raise ValueError("Invalid transaction id")
    root = _journal_root()
    destination = root / (record["id"] + ".json")
    if destination.is_symlink():
        raise ValueError("Symlinked journal")
    fd, temporary = tempfile.mkstemp(dir=root, prefix=".batch-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _load(operation_id):
    if not isinstance(operation_id, str) or not ID_RE.fullmatch(operation_id):
        raise ValueError("Invalid transaction id")
    file = _journal_root() / (operation_id + ".json")
    if file.is_symlink():
        raise ValueError("Symlinked journal")
    record = json.loads(file.read_text(encoding="utf-8"))
    rows = record.get("files")
    if record.get("id") != operation_id or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_BATCH:
        raise ValueError("Invalid journal")
    if any(not isinstance(row, dict) or not _allowed(row.get("path")) for row in rows):
        raise ValueError("Journal contains invalid paths")
    if len({row["path"] for row in rows}) != len(rows):
        raise ValueError("Journal contains duplicate paths")
    return record


def history():
    rows = []
    for item in _journal_root().iterdir():
        if item.suffix != ".json" or item.is_symlink() or not ID_RE.fullmatch(item.stem):
            raise ValueError("Invalid entry in transaction journal")
        record = _load(item.stem)
        rows.append({"id": record["id"], "status": record["status"],
                     "target_commit": record["target_commit"],
                     "paths": [r["path"] for r in record["files"]]})
    return {"operations": rows}


def _no_unfinished():
    deploy._no_unfinished()
    if any(r["status"] in ("installing", "rolling_back", "needs_attention")
           for r in history()["operations"]):
        raise ValueError("Unfinished JS batch transaction blocks writes")


def _manifest_raw():
    path = baseline.MANIFEST_PATH
    if path.is_symlink() or not path.is_file():
        raise ValueError("Installed manifest missing")
    data = path.read_bytes()
    if not 0 < len(data) <= baseline.MAX_MANIFEST_BYTES:
        raise ValueError("Invalid manifest size")
    return data


def _manifest_digest(payload):
    return _hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))


def prepare():
    """Prepare a short-lived JS-only plan; no HA file writes."""
    _enabled()
    with backup_service._LOCK:
        _no_unfinished()
        plan = sync_plan.prepare_sync_plan()
        if not plan.get("configured"):
            raise ValueError("Initialize installed manifest")
        options = check.load_options()
        installed = baseline.load_manifest(options)
        changes = [r for r in plan["files"] if r["action"] not in (
            "no_change", "format_only"
        )]
        if not changes or len(changes) > MAX_BATCH:
            raise ValueError("No JS changes or batch exceeds safe limit")
        if any(not _allowed(r["path"]) or not (
            (r["action"] == "candidate_review" and r["local"] in ("baseline_exact", "baseline_format_only")) or
            (r["action"] == "new_file_review" and r["local"] == "local_missing")
        ) for r in changes):
            raise ValueError("Only safe JS updates and missing-path additions are allowed")
        if not check.GIT_SHA_RE.fullmatch(plan["target"]):
            raise ValueError("Invalid target commit")
        selected = []
        for row in sorted(changes, key=lambda x: x["path"]):
            path = row["path"]
            is_add = row["action"] == "new_file_review"
            if is_add:
                if path in installed["files"] or not _absent(path):
                    raise ValueError("New JS is tracked or already exists")
                local, original = None, None
            else:
                if installed["files"][path]["blob_sha"] != row["baseline_sha"]:
                    raise ValueError("Baseline mismatch")
                local, _ = _read(path)
                original = check._load_blob_for_diff(options["repository"], options["github_token"], row["baseline_sha"])
                if check._normalize_line_ends(local) != check._normalize_line_ends(original):
                    raise ValueError("Local edits detected")
            target = check._load_blob_for_diff(options["repository"], options["github_token"], row["target_sha"])
            if not is_add and check._normalize_line_ends(original) == check._normalize_line_ends(target):
                raise ValueError("Formatting-only change")
            selected.append({"path": path, "action": "add" if is_add else "update",
                             "before_sha256": None if is_add else _hash(local),
                             "after_sha256": _hash(target),
                             "baseline_blob": None if is_add else row["baseline_sha"],
                             "target_blob": row["target_sha"], "target_size": len(target)})
        plan_id = secrets.token_hex(12)
        _PENDING.clear()
        _PENDING.update({"id": plan_id, "expires": time.monotonic() + TTL,
                         "repository": options["repository"], "branch": options["branch"],
                         "target_commit": plan["target"], "manifest_hash": _hash(_manifest_raw()),
                         "files": selected})
        return {"plan_id": plan_id, "target_commit": plan["target"],
                "files": selected, "expires_seconds": TTL, "write_permitted": False}


def _revert(record, saved):
    for entry in reversed(record["files"]):
        path = entry["path"]
        if entry.get("action", "update") == "add":
            if path in saved or entry["before_sha256"] is not None:
                raise ValueError("Unexpected backup for added JS")
            _remove_created(path, entry["after_sha256"])
            continue
        current, _ = _read(path)
        original = saved[path]
        if _hash(original) != entry["before_sha256"]:
            raise ValueError("Backup differs from journal")
        if current == original:
            continue
        if _hash(current) != entry["after_sha256"]:
            raise ValueError("Unexpected external file change; manual recovery required")
        _replace(path, current, original, entry["metadata"])
    manifest_hash = _hash(_manifest_raw())
    old = record["manifest_before"]
    new = record["manifest_after"]
    if manifest_hash == _manifest_digest(old):
        return
    if manifest_hash != _manifest_digest(new):
        raise ValueError("Installed manifest changed unexpectedly")
    baseline._atomic_write(old)


def install(plan_id, confirmation):
    """Install exactly one reviewed and pinned JS batch through the guarded API."""
    _enabled()
    if confirmation != "INSTALL JS BATCH":
        raise ValueError("Explicit install confirmation required")
    if not backup_service._LOCK.acquire(blocking=False):
        raise ValueError("Another operation is running")
    try:
        _no_unfinished()
        pending = dict(_PENDING)
        _PENDING.clear()
        if not isinstance(plan_id, str) or plan_id != pending.get("id") or time.monotonic() > pending.get("expires", 0):
            raise ValueError("Stale or invalid plan")
        options = check.load_options()
        if options["repository"] != pending["repository"] or options["branch"] != pending["branch"]:
            raise ValueError("Repository settings changed")
        if baseline._head(options["repository"], options["branch"], options["github_token"]) != pending["target_commit"]:
            raise ValueError("GitHub main changed")
        if _hash(_manifest_raw()) != pending["manifest_hash"]:
            raise ValueError("Manifest changed")
        before = baseline.load_manifest(options)
        originals, targets, metadata = {}, {}, {}
        for row in pending["files"]:
            path = row["path"]
            is_add = row.get("action") == "add"
            if is_add:
                if path in before["files"] or not _absent(path):
                    raise ValueError("New JS appeared after preview")
                metadata[path] = None
            else:
                content, info = _read(path)
                if _hash(content) != row["before_sha256"]:
                    raise ValueError("Local content changed after preview")
                if before["files"][path]["blob_sha"] != row["baseline_blob"]:
                    raise ValueError("Installed baseline changed")
                originals[path] = content
                metadata[path] = {"atime_ns": info.st_atime_ns, "mtime_ns": info.st_mtime_ns}
            target = check._load_blob_for_diff(options["repository"], options["github_token"], row["target_blob"])
            if _hash(target) != row["after_sha256"] or len(target) != row["target_size"]:
                raise ValueError("Target blob differs from approved plan")
            targets[path] = target

        paths = sorted(targets)
        snapshot = backup.create_snapshot(sorted(originals), pending["target_commit"], allow_empty=True)
        _, saved = backup.verify_snapshot(snapshot["backup"], allow_empty=True)
        if saved != originals:
            raise ValueError("Backup does not match input")
        if any(_read(p)[0] != originals[p] for p in originals) or any(
            not _absent(p) for p in targets if p not in originals
        ):
            raise ValueError("Local files changed after backup")
        after = json.loads(json.dumps(before))
        for row in pending["files"]:
            after["files"][row["path"]] = baseline._entry(
                pending["target_commit"], row["target_blob"], targets[row["path"]]
            )
        record = {"id": secrets.token_hex(12), "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "status": "installing", "backup": snapshot["backup"], "target_commit": pending["target_commit"],
                  "manifest_before": before, "manifest_after": after,
                  "files": [{**r, "metadata": metadata[r["path"]]} for r in pending["files"]]}
        _save(record)
        try:
            actions = {row["path"]: row.get("action", "update") for row in pending["files"]}
            for path in paths:
                if actions[path] == "add":
                    _create(path, targets[path])
                else:
                    _replace(path, originals[path], targets[path])
            if any(_read(p)[0] != targets[p] for p in paths):
                raise RuntimeError("Post-write verification failed")
            if _hash(_manifest_raw()) != pending["manifest_hash"]:
                raise RuntimeError("Manifest raced with install")
            baseline._atomic_write(after)
            if _hash(_manifest_raw()) != _manifest_digest(after):
                raise RuntimeError("Committed manifest cannot be verified")
            record["status"] = "installed"
            _save(record)
        except Exception as error:
            try:
                _revert(record, saved)
                record["status"] = "auto_rolled_back"
                _save(record)
            except Exception:
                record["status"] = "needs_attention"
                _save(record)
            raise RuntimeError("Batch installation failed; inspect journal") from error
        return {"id": record["id"], "status": "installed", "paths": paths,
                "backup": record["backup"], "target_commit": record["target_commit"]}
    finally:
        backup_service._LOCK.release()


def rollback(operation_id, confirmation):
    _enabled()
    if confirmation != "ROLLBACK JS BATCH":
        raise ValueError("Explicit rollback confirmation required")
    if not backup_service._LOCK.acquire(blocking=False):
        raise ValueError("Another operation is running")
    try:
        deploy._no_unfinished()
        record = _load(operation_id)
        if record["status"] not in ("installed", "installing", "needs_attention"):
            raise ValueError("Operation is not eligible for rollback")
        if any(r["id"] != operation_id and r["status"] in ("installing", "rolling_back", "needs_attention")
               for r in history()["operations"]):
            raise ValueError("Another unfinished transaction exists")
        _, saved = backup.verify_snapshot(record["backup"], allow_empty=True)
        if set(saved) != {r["path"] for r in record["files"] if r.get("action", "update") != "add"}:
            raise ValueError("Backup file list differs from journal")
        record["status"] = "rolling_back"
        _save(record)
        try:
            _revert(record, saved)
            record["status"] = "rolled_back"
            _save(record)
        except Exception as error:
            record["status"] = "needs_attention"
            _save(record)
            raise RuntimeError("Rollback needs manual recovery") from error
        return {"id": record["id"], "status": record["status"], "backup": record["backup"]}
    finally:
        backup_service._LOCK.release()
