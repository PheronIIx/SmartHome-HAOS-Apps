import base64
import hashlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from app import check as module

class ComparisonTests(unittest.TestCase):
    def test_managed_paths(self):
        positives = ["configuration.yaml", "packages/g45.yaml", "www/house-dash.js", "www/plans/g45.json", "dashboards/server.yaml"]
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
            self.assertEqual(result["totals"],{"same":1,"format_only":0,"modified":0,"unclassified":0,"missing":1,"error":0})
            self.assertEqual(len(result["files"]),2)


    def test_diff_normalization_and_content(self):
        self.assertEqual(module._normalize_line_ends(b"a\r\nb\r\n"), b"a\nb")
        self.assertEqual(module._normalize_line_ends(b"a\n"), b"a")
        self.assertNotEqual(module._normalize_line_ends(b"a  \n"), b"a")

    def test_pinned_diff_and_no_outside_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = b"line1\r\nline2\r\n"
            remote = b"line1\nline2\n"
            (root / "configuration.yaml").write_bytes(local)
            sha = hashlib.sha1(b"blob " + str(len(remote)).encode() + b"\0" + remote).hexdigest()
            commit_sha = "a" * 40
            tree_sha = "b" * 40
            commit = {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
            tree = {"tree": [{"path":"home-assistant/configuration.yaml",
                               "type":"blob", "mode":"100644", "sha":sha}]}
            blob = {"encoding":"base64", "size":len(remote),
                    "content":base64.b64encode(remote).decode()}
            opts = {"repository":"PheronIIx/SmartHome", "github_token":"fake"}
            with patch.object(module, "CONFIG_ROOT", root), patch.object(module,"load_options",return_value=opts):
                with patch.object(module, "github_json",side_effect=[commit,tree,blob]):
                    result=module.file_diff("configuration.yaml", commit_sha)
                self.assertEqual(result["kind"], "line_endings_only")
                self.assertEqual(result["local_crlf"], 2)
                self.assertEqual(result["github_crlf"], 0)
                with patch.object(module,"github_json",side_effect=AssertionError("network must not be used")):
                    with self.assertRaises(ValueError):
                        module.file_diff("secrets.yaml", commit_sha)
                    with self.assertRaises(ValueError):
                        module.file_diff("../ssh/key", commit_sha)
                    with self.assertRaises(ValueError):
                        module.file_diff("configuration.yaml", "not-a-commit")

    def test_status_classifies_crlf_vs_real_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "packages").mkdir()
            (root / "packages" / "g45.yaml").write_bytes(b"alpha\r\nbeta\r\n")
            (root / "packages" / "notify.yaml").write_bytes(b"old\n")
            remote_a, remote_b = b"alpha\nbeta\n", b"new\n"
            def blob_sha(data):
                return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            sha_a, sha_b = blob_sha(remote_a), blob_sha(remote_b)
            commit = {"sha":"a"*40, "commit":{"tree":{"sha":"b"*40}}}
            tree = {"truncated":False, "tree":[
                {"type":"blob", "path":"home-assistant/packages/g45.yaml", "sha":sha_a},
                {"type":"blob", "path":"home-assistant/packages/notify.yaml", "sha":sha_b}]}
            blobs = {sha_a: remote_a, sha_b:remote_b}
            def mocked_api(path, token):
                if "/git/blobs/" in path:
                    value=blobs[path.rsplit("/",1)[-1]]
                    return {"encoding":"base64", "size":len(value), "content":base64.b64encode(value).decode()}
                if "/git/trees/" in path:
                    return tree
                return commit
            opts={"repository":"PheronIIx/SmartHome", "branch":"main", "github_token":"fake"}
            with patch.object(module,"CONFIG_ROOT",root),patch.object(module,"load_options",return_value=opts):
                with patch.object(module,"github_json",side_effect=mocked_api):
                    result=module.check()
            self.assertEqual(result["totals"]["format_only"],1)
            self.assertEqual(result["totals"]["modified"],1)
            statuses={i["path"]:i["status"] for i in result["files"]}
            self.assertEqual(statuses["packages/g45.yaml"],"format_only")
            self.assertEqual(statuses["packages/notify.yaml"],"modified")

    def test_unified_diff_ignores_crlf_and_shows_real_change_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = b"first\r\nsecond\r\nthird\r\n"
            remote = b"first\nsecond\nthird\n// test marker"
            (root / "configuration.yaml").write_bytes(local)
            sha = hashlib.sha1(b"blob " + str(len(remote)).encode() + b"\0" + remote).hexdigest()
            commit_sha = "a" * 40
            commit = {"sha": commit_sha, "commit": {"tree": {"sha": "b" * 40}}}
            tree = {"tree": [{"path": "home-assistant/configuration.yaml",
                             "type": "blob", "mode": "100644", "sha": sha}]}
            blob = {"encoding": "base64", "size": len(remote),
                    "content": base64.b64encode(remote).decode()}
            options = {"repository": "PheronIIx/SmartHome", "github_token": "fake"}
            with patch.object(module, "CONFIG_ROOT", root), patch.object(module, "load_options", return_value=options):
                with patch.object(module, "github_json", side_effect=[commit, tree, blob]):
                    result = module.file_diff("configuration.yaml", commit_sha)
            self.assertEqual(result["kind"], "content_changed")
            additions = [line for line in result["diff"] if line.startswith("+") and not line.startswith("+++")]
            deletions = [line for line in result["diff"] if line.startswith("-") and not line.startswith("---")]
            self.assertEqual(additions, ["+// test marker"])
            self.assertEqual(deletions, [])

    def test_pinned_diff_is_not_an_arbitrary_remote_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            options = {"repository":"PheronIIx/SmartHome", "github_token":"fake"}
            commit = {"sha":"a"*40,"commit":{"tree":{"sha":"b"*40}}}
            tree = {"tree":[{"type":"blob", "path":"home-assistant/secrets.yaml",
                             "mode":"100644", "sha":"c"*40}]}
            with patch.object(module,"CONFIG_ROOT",Path(directory)),patch.object(module,"load_options",return_value=options):
                with patch.object(module,"github_json",side_effect=[commit,tree]):
                    with self.assertRaises(ValueError):
                        module.file_diff("configuration.yaml","a"*40)

if __name__ == "__main__":
    unittest.main()

