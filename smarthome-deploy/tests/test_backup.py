import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from app import backup, check


class BackupTests(unittest.TestCase):
    def _setup(self, directory):
        root = Path(directory)
        ha = root / "ha"
        archives = root / "appdata" / "backups"
        stage = root / "appdata" / "restore-rehearsals"
        (ha / "www").mkdir(parents=True)
        (ha / "packages").mkdir()
        return ha, archives, stage

    def test_snapshot_verify_and_restore_to_staging_only(self):
        with tempfile.TemporaryDirectory() as directory:
            ha, archives, stage = self._setup(directory)
            original = b"const value = 1;\r\n"
            (ha / "www" / "house-card.js").write_bytes(original)
            with patch.object(check, "CONFIG_ROOT", ha), \
                 patch.object(backup, "BACKUP_ROOT", archives), \
                 patch.object(backup, "RESTORE_ROOT", stage):
                info = backup.create_snapshot(["www/house-card.js"], "a" * 40)
                self.assertEqual(info["file_count"], 1)
                self.assertEqual(info["bytes"], len(original))
                self.assertTrue((archives / info["backup"]).is_file())
                (ha / "www" / "house-card.js").write_bytes(b"NEW VERSION")
                manifest, files = backup.verify_snapshot(info["backup"])
                self.assertEqual(files["www/house-card.js"], original)
                result = backup.rehearse_restore(info["backup"], stage / "rehearsal-1")
                self.assertTrue(result["verified"])
                self.assertEqual((stage / "rehearsal-1" / "www" / "house-card.js").read_bytes(), original)
                self.assertEqual((ha / "www" / "house-card.js").read_bytes(), b"NEW VERSION")
                with self.assertRaises(ValueError):
                    backup.rehearse_restore(info["backup"], ha)

    def test_invalid_paths_and_duplicate_names_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            ha, archives, stage = self._setup(directory)
            (ha / "www" / "house-card.js").write_bytes(b"good")
            (ha / "www" / "bad.js").symlink_to(ha / "www" / "house-card.js")
            with patch.object(check, "CONFIG_ROOT", ha), patch.object(backup, "BACKUP_ROOT", archives):
                for files in (["secrets.yaml"], ["../outside.yaml"], ["www/house-card.js"] * 2,
                              ["www/bad.js"], ["www/missing.js"]):
                    with self.subTest(files=files), self.assertRaises(ValueError):
                        backup.create_snapshot(files, "a" * 40)

    def test_archive_integrity_and_tampering_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            ha, archives, stage = self._setup(directory)
            (ha / "packages" / "g45.yaml").write_bytes(b"mode: home\n")
            with patch.object(check, "CONFIG_ROOT", ha), patch.object(backup, "BACKUP_ROOT", archives):
                info = backup.create_snapshot(["packages/g45.yaml"], "c" * 40)
                path = archives / info["backup"]
                # Replace archive payload with different bytes, keep the original manifest.
                with zipfile.ZipFile(path, "r") as archive:
                    manifest = archive.read("manifest.json")
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("manifest.json", manifest)
                    archive.writestr("files/packages/g45.yaml", b"tampered")
                with self.assertRaises(ValueError):
                    backup.verify_snapshot(info["backup"])

    def test_no_uncontrolled_staging_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            ha, archives, stage = self._setup(directory)
            (ha / "www" / "house-card.js").write_bytes(b"old")
            with patch.object(check, "CONFIG_ROOT", ha), \
                 patch.object(backup, "BACKUP_ROOT", archives), \
                 patch.object(backup, "RESTORE_ROOT", stage):
                info = backup.create_snapshot(["www/house-card.js"], "a" * 40)
                for destination in (ha, archives, stage, Path(directory) / "other"):
                    with self.subTest(destination=destination), self.assertRaises(ValueError):
                        backup.rehearse_restore(info["backup"], destination)


if __name__ == "__main__":
    unittest.main()
