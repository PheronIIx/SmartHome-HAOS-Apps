import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import backup, backup_service, check


class BackupServiceTests(unittest.TestCase):
    def test_manual_backup_creates_verified_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ha = root / "ha"
            (ha / "www").mkdir(parents=True)
            (ha / "www" / "house-card.js").write_bytes(b"local\r\n")
            archives = root / "data" / "backups"
            staging = root / "data" / "restore-rehearsals"
            status = {"configured": True, "commit": "a" * 40,
                      "files": [{"path": "www/house-card.js", "status": "format_only"}]}
            with patch.object(check, "CONFIG_ROOT", ha), \
                 patch.object(backup, "BACKUP_ROOT", archives), \
                 patch.object(backup, "RESTORE_ROOT", staging), \
                 patch.object(backup_service.check, "check", return_value=status), \
                 patch.object(backup_service.check, "load_options", return_value={"repository":"PheronIIx/SmartHome","branch":"main"}), \
                 patch.object(backup_service.baseline, "load_manifest", return_value=None):
                result = backup_service.create_manual_backup()
                self.assertTrue(result["verified"])
                self.assertEqual(result["file_count"], 1)
                self.assertEqual(len(backup_service.list_backups()["backups"]), 1)
                verified = backup_service.verify_existing_backup(result["backup"])
                self.assertEqual(verified["bytes"], len(b"local\r\n"))
                restored = backup_service.rehearse_existing_backup(result["backup"])
                self.assertTrue(restored["verified"])
                self.assertEqual((ha / "www" / "house-card.js").read_bytes(), b"local\r\n")
                self.assertEqual(len(list(staging.glob("test-*"))), 1)

    def test_unverified_managed_file_blocks_backup(self):
        with patch.object(backup_service.check, "check", return_value={
            "configured": True, "commit": "a" * 40,
            "files": [{"path": "www/missing.js", "status": "missing"}]
        }), patch.object(backup_service.check, "load_options", return_value={}), \
             patch.object(backup_service.baseline, "load_manifest", return_value=None):
            with self.assertRaises(ValueError):
                backup_service.create_manual_backup()

    def test_new_uninstalled_github_file_is_skipped_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ha = root / "ha"
            (ha / "www").mkdir(parents=True)
            (ha / "www" / "house-card.js").write_bytes(b"local\n")
            archives = root / "data" / "backups"
            status = {"configured": True, "commit": "a" * 40, "files": [
                {"path":"www/house-card.js","status":"same"},
                {"path":"packages/infra.yaml","status":"missing"},
            ]}
            manifest = {"files":{"www/house-card.js":{"sha256":"0"*64}}}
            with patch.object(check, "CONFIG_ROOT", ha), \
                 patch.object(backup, "BACKUP_ROOT", archives), \
                 patch.object(backup_service.check, "check", return_value=status), \
                 patch.object(backup_service.check, "load_options", return_value={"repository":"PheronIIx/SmartHome","branch":"main"}), \
                 patch.object(backup_service.baseline, "load_manifest", return_value=manifest):
                result = backup_service.create_manual_backup()
                _, files = backup.verify_snapshot(result["backup"])
            self.assertEqual(result["skipped_uninstalled"], ["packages/infra.yaml"])
            self.assertEqual(set(files), {"www/house-card.js"})

    def test_missing_previously_installed_file_blocks_backup(self):
        status = {"configured": True, "commit": "a" * 40,
                  "files": [{"path":"packages/g45.yaml","status":"missing"}]}
        manifest = {"files":{"packages/g45.yaml":{"sha256":"0"*64}}}
        with patch.object(backup_service.check, "check", return_value=status), \
             patch.object(backup_service.check, "load_options", return_value={"repository":"PheronIIx/SmartHome","branch":"main"}), \
             patch.object(backup_service.baseline, "load_manifest", return_value=manifest):
            with self.assertRaisesRegex(ValueError, "packages/g45.yaml"):
                backup_service.create_manual_backup()

    def test_locally_installed_file_removed_from_github_is_still_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ha = root / "ha"
            (ha / "www").mkdir(parents=True)
            (ha / "packages").mkdir()
            (ha / "www" / "house-card.js").write_bytes(b"card\n")
            (ha / "packages" / "g45.yaml").write_bytes(b"package: local\n")
            archives = root / "data" / "backups"
            status = {"configured": True, "commit": "a" * 40,
                      "files": [{"path":"www/house-card.js","status":"same"}]}
            manifest = {"files":{
                "www/house-card.js":{"sha256":"0"*64},
                "packages/g45.yaml":{"sha256":"1"*64},
            }}
            with patch.object(check, "CONFIG_ROOT", ha), \
                 patch.object(backup, "BACKUP_ROOT", archives), \
                 patch.object(backup_service.check, "check", return_value=status), \
                 patch.object(backup_service.check, "load_options", return_value={"repository":"PheronIIx/SmartHome","branch":"main"}), \
                 patch.object(backup_service.baseline, "load_manifest", return_value=manifest):
                result = backup_service.create_manual_backup()
                _, files = backup.verify_snapshot(result["backup"])
            self.assertEqual(set(files), {"www/house-card.js", "packages/g45.yaml"})

    def test_only_server_generated_rehearsal_paths(self):
        with self.assertRaises(ValueError):
            backup_service.rehearse_existing_backup("../../config/secrets.yaml")


if __name__ == "__main__":
    unittest.main()
