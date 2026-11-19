import hashlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from app import check as module

class ComparisonTests(unittest.TestCase):
    def test_managed_paths(self):
        positives = ["configuration.yaml", "packages/g45.yaml", "www/house-dash.js", "www/plans/g45.json"]
        for path in positives:
            self.assertEqual(module.managed_path("home-assistant/" + path), path)
        for path in ["docs/a.md", "home-assistant/secrets.yaml",
                     "home-assistant/www/plans/../secrets.yaml",
                     "home-assistant/dashboards/server.yaml", "home-assistant/packages/nested/file.yaml",
                     "frigate/config.yaml"]:
            self.assertIsNone(module.managed_path(path))

    def test_git_blob_hash_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configuration.yaml").write_bytes(b"hello\n")
            expected = hashlib.sha1(b"blob 6\0hello\n").hexdigest()
            with patch.object(module, "CONFIG_ROOT", root):
                self.assertEqual(module.git_blob_sha(root / "configuration.yaml"), expected)
                self.assertEqual(module.safe_local_file("configuration.yaml"), root / "configuration.yaml")
                (root / "bad.yaml").symlink_to(root / "configuration.yaml")
                with self.assertRaises(ValueError):
                    module.safe_local_file("bad.yaml")
                with self.assertRaises(ValueError):
                    module.safe_local_file("../other")

    def test_missing_token_returns_unconfigured_without_network(self):
        with patch.object(module, "load_options", return_value={"repository":"PheronIIx/SmartHome", "branch":"main", "github_token":""}):
            with patch.object(module, "github_json", side_effect=AssertionError("must not call")):
                self.assertFalse(module.check()["configured"])

    def test_list_and_compare_with_mock_github(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root / "configuration.yaml").write_bytes(b"hi")
            digest = hashlib.sha1(b"blob 2\0hi").hexdigest()
            commit={"sha":"a"*40,"commit":{"tree":{"sha":"b"*40}}}
            tree={"tree":[{"type":"blob","path":"home-assistant/configuration.yaml","sha":digest},
                         {"type":"blob","path":"home-assistant/packages/g45.yaml","sha":"c"*40},
                         {"type":"blob","path":"home-assistant/secrets.yaml","sha":"d"*40}],"truncated":False}
            options={"repository":"PheronIIx/SmartHome","branch":"main","github_token":"fake"}
            with patch.object(module,"CONFIG_ROOT",root),patch.object(module,"load_options",return_value=options):
                with patch.object(module,"github_json",side_effect=[commit,tree]):
                    result=module.check()
            self.assertEqual(result["totals"],{"same":1,"modified":0,"missing":1,"error":0})
            self.assertEqual(len(result["files"]),2)

if __name__ == "__main__":
    unittest.main()
