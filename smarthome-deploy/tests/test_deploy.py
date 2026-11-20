import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import backup, backup_service, check, deploy, plan


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.ha = root / "ha"
        (self.ha / "www").mkdir(parents=True)
        self.file = self.ha / "www" / "house-card.js"
        self.baseline = b"customElements.define('house-card', HouseCard);\r\n"
        self.target = self.baseline.replace(b"\r\n", b"\n") + deploy.MARKER + b"\n"
        self.file.write_bytes(self.baseline)
        self.options = {"enable_test_install": True, "repository": "PheronIIx/SmartHome",
                        "github_token": "test", "baseline_commit": "a" * 40}
        self.patchers = [
            patch.object(check, "CONFIG_ROOT", self.ha),
            patch.object(backup, "BACKUP_ROOT", root / "backups"),
            patch.object(backup, "RESTORE_ROOT", root / "staging"),
            patch.object(deploy, "HISTORY_ROOT", root / "history"),
            patch.object(check, "load_options", return_value=self.options),
            patch.object(plan, "load_options", return_value=self.options),
            patch.object(deploy.plan, "prepare_plan", return_value={
                "configured": True, "baseline": "a" * 40, "target": "b" * 40,
                "files": [{"path": deploy.TEST_PATH, "action": "candidate_review",
                           "local": "baseline_format_only", "baseline_sha": blob_sha(self.baseline),
                           "target_sha": blob_sha(self.target)}]
            }),
            patch.object(check, "_load_blob_for_diff", side_effect=lambda repo, token, sha:
                         {blob_sha(self.baseline): self.baseline,
                          blob_sha(self.target): self.target}[sha]),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        deploy._PENDING.clear()

    def test_install_backup_and_rollback(self):
        prepared = deploy.prepare()
        self.assertEqual(prepared["path"], deploy.TEST_PATH)
        self.assertEqual(self.file.read_bytes(), self.baseline)
        result = deploy.install(prepared["plan_id"], "INSTALL house-card.js")
        self.assertEqual(result["status"], "installed")
        self.assertEqual(self.file.read_bytes(), self.target)
        manifest, contents = backup.verify_snapshot(result["backup"])
        self.assertEqual(contents[deploy.TEST_PATH], self.baseline)
        self.assertEqual(deploy.history()["operations"][0]["status"], "installed")
        undone = deploy.rollback(result["id"], "ROLLBACK house-card.js")
        self.assertEqual(undone["status"], "rolled_back")
        self.assertEqual(self.file.read_bytes(), self.baseline)
        self.assertTrue(deploy._load(result["id"])["rollback_backup"])

    def test_drift_blocks_install_and_preserves_original(self):
        prepared = deploy.prepare()
        self.file.write_bytes(b"LOCAL CHANGE\n")
        with self.assertRaises(ValueError):
            deploy.install(prepared["plan_id"], "INSTALL house-card.js")
        self.assertEqual(self.file.read_bytes(), b"LOCAL CHANGE\n")
        self.assertFalse(deploy.history()["operations"])

    def test_unexpected_github_change_blocks_preview(self):
        self.target = self.target + b"malicious\n"
        with self.assertRaises(ValueError):
            deploy.prepare()

    def test_disabled_install_blocks_preview(self):
        self.options["enable_test_install"] = False
        with self.assertRaises(ValueError):
            deploy.prepare()

    def test_confirmation_required(self):
        prepared = deploy.prepare()
        with self.assertRaises(ValueError):
            deploy.install(prepared["plan_id"], "yes")
        self.assertEqual(self.file.read_bytes(), self.baseline)

    def test_rollback_blocks_local_drift(self):
        prepared = deploy.prepare()
        result = deploy.install(prepared["plan_id"], "INSTALL house-card.js")
        self.file.write_bytes(b"UNEXPECTED\n")
        with self.assertRaises(ValueError):
            deploy.rollback(result["id"], "ROLLBACK house-card.js")
        self.assertEqual(self.file.read_bytes(), b"UNEXPECTED\n")

    def test_history_symlink_is_not_followed(self):
        prepared = deploy.prepare()
        result = deploy.install(prepared["plan_id"], "INSTALL house-card.js")
        path = deploy.HISTORY_ROOT / (result["id"] + ".json")
        path.unlink()
        path.symlink_to(self.file)
        with self.assertRaises(ValueError):
            deploy._load(result["id"])

    def test_symlinked_house_card_blocks(self):
        self.file.unlink()
        self.file.symlink_to(Path(self.temp.name) / "other")
        with self.assertRaises(OSError):
            deploy.prepare()


if __name__ == "__main__":
    unittest.main()
