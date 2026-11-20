import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import backup, baseline, check, multifile, sync_plan


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
PATHS = ["www/house-card.js", "www/house-dash.js"]


def blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.ha = self.base / "ha"
        (self.ha / "www").mkdir(parents=True)
        self.data = self.base / "data"
        self.data.mkdir()
        self.original = {p: ("old " + p + "\n").encode() for p in PATHS}
        self.updated = {p: ("new " + p + "\n").encode() for p in PATHS}
        self.opts = {"repository": "PheronIIx/SmartHome", "branch": "main",
                     "github_token": "fake", "enable_multifile_install": True}
        self.manifest = {"schema_version": 1, "repository": self.opts["repository"],
                         "branch": "main", "initialized_from_commit": SHA_A,
                         "files": {p: baseline._entry(SHA_A, blob(self.original[p]), self.original[p])
                                   for p in PATHS}}
        self.manifest_path = self.data / "installed-manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest, sort_keys=True, indent=2))
        for p in PATHS:
            (self.ha / p).write_bytes(self.original[p])
        self.sha_blobs = {blob(v): v for v in list(self.original.values()) + list(self.updated.values())}
        self.patches = [
            patch.object(check, "CONFIG_ROOT", self.ha),
            patch.object(check, "load_options", side_effect=lambda: dict(self.opts)),
            patch.object(baseline, "MANIFEST_PATH", self.manifest_path),
            patch.object(backup, "BACKUP_ROOT", self.data / "backups"),
            patch.object(multifile, "HISTORY_ROOT", self.data / "history"),
            patch.object(check, "_load_blob_for_diff",
                         side_effect=lambda repo, token, sha: self.sha_blobs[sha]),
            patch.object(baseline, "_head", return_value=SHA_B),
            patch.object(multifile.deploy, "_no_unfinished", return_value=None),
            patch.object(sync_plan, "prepare_sync_plan", side_effect=self.sync_plan),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        multifile._PENDING.clear()

    def sync_plan(self):
        return {"configured": True, "target": SHA_B,
                "files": [{"path": p, "action": "candidate_review", "local": "baseline_exact",
                           "baseline_sha": blob(self.original[p]),
                           "target_sha": blob(self.updated[p])} for p in PATHS]}

    def test_gate_is_disabled_by_default(self):
        self.opts["enable_multifile_install"] = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            multifile.prepare()
        with self.assertRaisesRegex(ValueError, "disabled"):
            multifile.install("x", "INSTALL JS BATCH")

    def test_install_and_manual_rollback_restores_manifest_and_files(self):
        initial_manifest = self.manifest_path.read_bytes()
        preview = multifile.prepare()
        self.assertEqual(len(preview["files"]), 2)
        self.assertFalse(preview["write_permitted"])
        installed = multifile.install(preview["plan_id"], "INSTALL JS BATCH")
        self.assertEqual(installed["status"], "installed")
        for p in PATHS:
            self.assertEqual((self.ha / p).read_bytes(), self.updated[p])
            self.assertEqual(baseline.load_manifest(self.opts)["files"][p]["blob_sha"],
                             blob(self.updated[p]))
        self.assertEqual(multifile.history()["operations"][0]["status"], "installed")
        rolled = multifile.rollback(installed["id"], "ROLLBACK JS BATCH")
        self.assertEqual(rolled["status"], "rolled_back")
        for p in PATHS:
            self.assertEqual((self.ha / p).read_bytes(), self.original[p])
        self.assertEqual(json.loads(self.manifest_path.read_bytes()),
                         json.loads(initial_manifest))

    def test_local_edit_after_preview_blocks_every_write(self):
        preview = multifile.prepare()
        (self.ha / PATHS[1]).write_bytes(b"independent edit")
        with self.assertRaisesRegex(ValueError, "Local content changed"):
            multifile.install(preview["plan_id"], "INSTALL JS BATCH")
        self.assertEqual((self.ha / PATHS[0]).read_bytes(), self.original[PATHS[0]])
        self.assertEqual(multifile.history()["operations"], [])

    def test_wrong_confirmation_does_not_consume_plan(self):
        preview = multifile.prepare()
        with self.assertRaisesRegex(ValueError, "confirmation"):
            multifile.install(preview["plan_id"], "wrong")
        self.assertEqual(multifile.install(preview["plan_id"], "INSTALL JS BATCH")["status"],
                         "installed")

    def test_mid_batch_failure_restores_all_previous_files(self):
        preview = multifile.prepare()
        original_replace = multifile._replace
        calls = []
        def fail_second(path, before, after, metadata=None):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("injected write failure")
            return original_replace(path, before, after, metadata)
        with patch.object(multifile, "_replace", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "installation failed"):
                multifile.install(preview["plan_id"], "INSTALL JS BATCH")
        for p in PATHS:
            self.assertEqual((self.ha / p).read_bytes(), self.original[p])
        self.assertEqual(multifile.history()["operations"][0]["status"], "auto_rolled_back")

    def test_symlinked_existing_file_cannot_be_written(self):
        preview = multifile.prepare()
        outside = self.base / "outside.js"
        outside.write_bytes(b"external")
        target = self.ha / PATHS[0]
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaises(OSError):
            multifile.install(preview["plan_id"], "INSTALL JS BATCH")
        self.assertEqual(outside.read_bytes(), b"external")

    def test_yaml_or_deletion_is_refused_even_when_gate_enabled(self):
        with patch.object(sync_plan, "prepare_sync_plan", return_value={
            "configured": True, "target": SHA_B,
            "files": [{"path": "packages/infra.yaml", "action": "candidate_review",
                       "local": "baseline_exact", "baseline_sha": SHA_C, "target_sha": SHA_D}]
        }):
            with self.assertRaisesRegex(ValueError, "YAML"):
                multifile.prepare()
        with patch.object(sync_plan, "prepare_sync_plan", return_value={
            "configured": True, "target": SHA_B,
            "files": [{"path": PATHS[0], "action": "deletion_review",
                       "local": "baseline_exact", "baseline_sha": SHA_C, "target_sha": None}]
        }):
            with self.assertRaisesRegex(ValueError, "YAML"):
                multifile.prepare()

    def test_unsafe_manifest_change_blocks_install(self):
        preview = multifile.prepare()
        self.manifest_path.write_text('{"external":"changed"}')
        with self.assertRaisesRegex(ValueError, "Manifest changed"):
            multifile.install(preview["plan_id"], "INSTALL JS BATCH")
        self.assertEqual((self.ha / PATHS[0]).read_bytes(), self.original[PATHS[0]])


    def _new_js_plan(self, *, mixed=False):
        new = "www/smarthome-updates.js"
        body = b"customElements.define('smarthome-updates-card', class extends HTMLElement {});\n"
        self.sha_blobs[blob(body)] = body
        self.addCleanup(patch.stopall) if False else None
        allow = patch.object(check, "MANAGED_PATHS", check.MANAGED_PATHS | {new})
        allow.start()
        self.addCleanup(allow.stop)
        rows = [{"path": new, "action": "new_file_review", "local": "local_missing",
                 "baseline_sha": None, "target_sha": blob(body)}]
        if mixed:
            rows.insert(0, {"path": PATHS[0], "action": "candidate_review",
                            "local": "baseline_exact", "baseline_sha": blob(self.original[PATHS[0]]),
                            "target_sha": blob(self.updated[PATHS[0]])})
        override = patch.object(sync_plan, "prepare_sync_plan", return_value={
            "configured": True, "target": SHA_B, "files": rows})
        override.start()
        self.addCleanup(override.stop)
        return new, body

    def test_add_only_creates_file_then_rollback_restores_absence(self):
        path, contents = self._new_js_plan()
        initial = self.manifest_path.read_bytes()
        preview = multifile.prepare()
        self.assertEqual(preview["files"][0]["action"], "add")
        self.assertIsNone(preview["files"][0]["before_sha256"])
        installed = multifile.install(preview["plan_id"], "INSTALL JS BATCH")
        self.assertEqual((self.ha / path).read_bytes(), contents)
        self.assertEqual(baseline.load_manifest(self.opts)["files"][path]["sha256"],
                         hashlib.sha256(contents).hexdigest())
        self.assertEqual(backup.verify_snapshot(installed["backup"], allow_empty=True)[1], {})
        with self.assertRaisesRegex(ValueError, "Invalid number"):
            backup.verify_snapshot(installed["backup"])
        multifile.rollback(installed["id"], "ROLLBACK JS BATCH")
        self.assertFalse((self.ha / path).exists())
        self.assertEqual(json.loads(self.manifest_path.read_bytes()), json.loads(initial))

    def test_add_missing_path_must_not_overwrite_local_file(self):
        path, _ = self._new_js_plan()
        preview = multifile.prepare()
        (self.ha / path).write_text("local author")
        with self.assertRaisesRegex(ValueError, "appeared"):
            multifile.install(preview["plan_id"], "INSTALL JS BATCH")
        self.assertEqual((self.ha / path).read_text(), "local author")

    def test_add_missing_path_must_not_follow_symlink(self):
        path, _ = self._new_js_plan()
        preview = multifile.prepare()
        target = self.base / "outside.js"
        target.write_text("outside")
        (self.ha / path).symlink_to(target)
        with self.assertRaisesRegex(ValueError, "appeared"):
            multifile.install(preview["plan_id"], "INSTALL JS BATCH")
        self.assertEqual(target.read_text(), "outside")

    def test_rollback_does_not_remove_modified_added_file(self):
        path, _ = self._new_js_plan()
        preview = multifile.prepare()
        installed = multifile.install(preview["plan_id"], "INSTALL JS BATCH")
        (self.ha / path).write_bytes(b"user edit")
        with self.assertRaisesRegex(RuntimeError, "manual recovery"):
            multifile.rollback(installed["id"], "ROLLBACK JS BATCH")
        self.assertEqual((self.ha / path).read_bytes(), b"user edit")
        self.assertEqual(multifile.history()["operations"][0]["status"], "needs_attention")

    def test_mixed_add_and_update_rolls_back_both_on_failure(self):
        path, _ = self._new_js_plan(mixed=True)
        preview = multifile.prepare()
        original_replace = multifile._replace
        with patch.object(multifile, "_replace", side_effect=OSError("injected update failure")):
            with self.assertRaisesRegex(RuntimeError, "installation failed"):
                multifile.install(preview["plan_id"], "INSTALL JS BATCH")
        self.assertFalse((self.ha / path).exists())
        self.assertEqual((self.ha / PATHS[0]).read_bytes(), self.original[PATHS[0]])
        self.assertNotIn(path, baseline.load_manifest(self.opts)["files"])
        self.assertEqual(multifile.history()["operations"][0]["status"], "auto_rolled_back")


if __name__ == "__main__":
    unittest.main()
