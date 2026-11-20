import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import check, sync_preflight


TARGET = "b" * 40
OLD = "a" * 40
BLOB = "c" * 40
OPTIONS = {"repository": "PheronIIx/SmartHome", "branch": "main", "github_token": "fake"}


def result(rows):
    return {"configured": True, "target": TARGET, "files": rows}


class MultiFilePreflightTests(unittest.TestCase):
    def _run(self, root, rows, data=b"updated"):
        with patch.object(check, "CONFIG_ROOT", root), \
             patch.object(check, "load_options", return_value=OPTIONS), \
             patch.object(sync_preflight.sync_plan, "prepare_sync_plan", return_value=result(rows)), \
             patch.object(check, "_load_blob_for_diff", return_value=data):
            return sync_preflight.prepare_preflight()

    def test_format_only_does_not_create_installation_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "www").mkdir()
            local = b"const a = 1;\r\n"
            (root / "www" / "house-card.js").write_bytes(local)
            rows = [{"path": "www/house-card.js", "action": "format_only",
                     "baseline_sha": OLD, "target_sha": BLOB, "local": "baseline_format_only"}]
            plan = self._run(root, rows)
            self.assertEqual(plan["counts"]["update"], 0)
            self.assertEqual(plan["counts"]["skip"], 1)
            self.assertFalse(plan["write_permitted"])
            self.assertEqual(plan["files"][0]["local"]["sha256"], hashlib.sha256(local).hexdigest())
            self.assertEqual(len(plan["plan_fingerprint"]), 64)

    def test_multiple_files_report_risk_and_blockers_without_mutating_ha(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "www").mkdir()
            (root / "packages").mkdir()
            old_js = b"original js\n"
            old_yaml = b"original yaml\n"
            (root / "www" / "house-card.js").write_bytes(old_js)
            (root / "packages" / "infra.yaml").write_bytes(old_yaml)
            rows = [
                {"path": "www/house-card.js", "action": "candidate_review",
                 "baseline_sha": OLD, "target_sha": BLOB, "local": "baseline_exact"},
                {"path": "packages/infra.yaml", "action": "blocked_local_drift",
                 "baseline_sha": OLD, "target_sha": BLOB, "local": "local_drift"},
            ]
            plan = self._run(root, rows)
            self.assertEqual(plan["counts"]["update"], 1)
            self.assertEqual(plan["counts"]["blocked"], 1)
            self.assertTrue(plan["requires_explicit_conflict_consent"])
            self.assertTrue(plan["requires_fresh_full_haos_backup"])
            self.assertTrue(plan["requires_yaml_validation"])
            self.assertFalse(plan["ready_to_install"])
            self.assertEqual((root / "www" / "house-card.js").read_bytes(), old_js)
            self.assertEqual((root / "packages" / "infra.yaml").read_bytes(), old_yaml)

    def test_new_missing_file_is_add_candidate_without_creating_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "www").mkdir()
            rows = [{"path": "www/house-dash.js", "action": "new_file_review",
                     "baseline_sha": None, "target_sha": BLOB, "local": "local_missing"}]
            plan = self._run(root, rows, b"new file\n")
            self.assertEqual(plan["counts"]["add"], 1)
            self.assertFalse(plan["files"][0]["local"]["exists"])
            self.assertFalse((root / "www" / "house-dash.js").exists())

    def test_deleted_yaml_is_review_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "packages").mkdir()
            (root / "packages" / "g45.yaml").write_bytes(b"yaml: value\n")
            rows = [{"path": "packages/g45.yaml", "action": "deletion_review",
                     "baseline_sha": OLD, "target_sha": None, "local": "baseline_exact"}]
            plan = self._run(root, rows)
            self.assertEqual(plan["counts"]["delete_review"], 1)
            self.assertTrue(plan["requires_yaml_validation"])
            self.assertTrue((root / "packages" / "g45.yaml").exists())

    def test_symlinked_source_is_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "www").mkdir()
            destination = root / "real.js"
            destination.write_bytes(b"do not read via symlink")
            (root / "www" / "house-card.js").symlink_to(destination)
            rows = [{"path": "www/house-card.js", "action": "candidate_review",
                     "baseline_sha": OLD, "target_sha": BLOB, "local": "baseline_exact"}]
            plan = self._run(root, rows)
            self.assertEqual(plan["counts"]["blocked"], 1)
            self.assertIsNone(plan["files"][0]["local"])
            self.assertEqual(plan["files"][0]["snapshot_error"], "OSError")

    def test_unknown_or_non_allowlisted_plan_rows_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for path, action in (("secrets.yaml", "candidate_review"), ("www/house-card.js", "unrecognized")):
                rows = [{"path": path, "action": action,
                         "baseline_sha": OLD, "target_sha": BLOB}]
                with self.assertRaises(ValueError):
                    self._run(root, rows)

    def test_no_initialized_manifest_requires_no_write(self):
        with patch.object(sync_preflight.sync_plan, "prepare_sync_plan", return_value={
            "configured": False, "message": "Initialize baseline", "files": []}):
            plan = sync_preflight.prepare_preflight()
        self.assertFalse(plan["configured"])
        self.assertFalse(plan["ready_to_install"])


if __name__ == "__main__":
    unittest.main()
