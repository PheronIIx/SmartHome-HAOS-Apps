"""DEV-07: safe JSON status, classification, and scheduler regression tests."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import status_export as status


SHA = "a" * 40


def row(path, action):
    return {"path": path, "action": action}


class StatusExportTests(unittest.TestCase):
    def test_empty_is_zero_not_unknown(self):
        result = status.snapshot({"configured": True, "target": SHA, "files": [
            row("www/house-card.js", "no_change"),
            row("www/dacha-tiles.js", "format_only")
        ]}, "2026-11-21T10:00:00+00:00")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["available_count"], 0)
        self.assertEqual(result["blocked_count"], 0)

    def test_js_updates_and_file_groups(self):
        result = status.snapshot({"configured": True, "target": SHA, "files": [
            row("www/house-card.js", "candidate_review"),
            row("www/dacha-tiles.js", "new_file_review"),
            row("packages/infra.yaml", "candidate_review"),
            row("www/plans/g45.json", "new_file_conflict")
        ]})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["available_count"], 2)
        self.assertEqual(result["blocked_count"], 2)
        self.assertEqual({x["group"] for x in result["files"]}, {"cards", "packages", "plans"})

    def test_conflicts_and_deletion_never_marked_installable(self):
        actions = ["blocked_local_drift", "blocked_unverified", "new_file_conflict", "deletion_review"]
        result = status.snapshot({"configured": True, "target": SHA, "files": [
            row("www/house-card.js", action) for action in actions
        ]})
        self.assertEqual(result["available_count"], 0)
        self.assertEqual(result["blocked_count"], len(actions))
        self.assertEqual(result["status"], "blocked")

    def test_unconfigured_and_failure_are_not_zero(self):
        result = status.snapshot({"configured": False, "files": []})
        self.assertIsNone(result["available_count"])
        self.assertEqual(result["status"], "unconfigured")
        result = status.failure()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["available_count"])
        self.assertIsNone(result["checked_at"])

    def test_unexpected_action_or_path_fails_closed(self):
        for candidate in [row("www/house-card.js", "mystery"),
                          row("../secrets.yaml", "candidate_review")]:
            with self.assertRaises(ValueError):
                status.snapshot({"configured": True, "target": SHA, "files": [candidate]})

    def test_atomic_publication_and_no_tmp_leftovers(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(status.check, "CONFIG_ROOT", Path(directory)):
                first = {"status": "ok", "available_count": 1}
                second = {"status": "error", "available_count": None}
                status.publish(first)
                status.publish(second)
                dest = Path(directory) / ".smarthome-deploy" / "status.json"
                self.assertEqual(json.loads(dest.read_text()), second)
                self.assertEqual([x.name for x in dest.parent.iterdir()], ["status.json"])

    def test_refresh_failure_writes_error_without_install(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(status.check, "CONFIG_ROOT", Path(directory)), \
                 patch.object(status.sync_plan, "prepare_sync_plan", side_effect=RuntimeError("offline")):
                result = status.refresh()
                self.assertEqual(result["status"], "error")
                output = Path(directory) / ".smarthome-deploy" / "status.json"
                self.assertEqual(json.loads(output.read_text())["status"], "error")

    def test_schedule_is_six_hours(self):
        self.assertEqual(status.INTERVAL_SECONDS, 21600)


if __name__ == "__main__":
    unittest.main()
