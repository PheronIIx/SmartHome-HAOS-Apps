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
                 patch.object(backup_service.check, "check", return_value=status):
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
        }):
            with self.assertRaises(ValueError):
                backup_service.create_manual_backup()

    def test_only_server_generated_rehearsal_paths(self):
        with self.assertRaises(ValueError):
            backup_service.rehearse_existing_backup("../../config/secrets.yaml")


if __name__ == "__main__":
    unittest.main()
