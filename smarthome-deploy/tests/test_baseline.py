import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import baseline, check


def blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class ManifestTests(unittest.TestCase):
    def test_initialize_seeds_legacy_files_and_only_adopts_matching_new_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "www").mkdir()
            (root / "dashboards").mkdir()
            (root / "www" / "house-card.js").write_bytes(b"legacy\r\n")
            (root / "dashboards" / "server.yaml").write_bytes(b"server: true\n")
            old = b"legacy\n"
            server = b"server: true\n"
            old_sha, server_sha = blob(old), blob(server)
            options = {
                "repository":"PheronIIx/SmartHome", "branch":"main",
                "github_token":"fake", "baseline_commit":"a"*40,
            }
            def api(path, token):
                if "/commits/main" in path:
                    return {"sha":"b"*40}
                if "/commits/" in path:
                    commit_id = path.rsplit("/",1)[-1]
                    tree_id = "c"*40 if commit_id == "a"*40 else "d"*40
                    return {"sha":commit_id, "commit":{"tree":{"sha":tree_id}}}
                if "/git/trees/" in path and "recursive" in path:
                    entries = [{"type":"blob","path":"home-assistant/www/house-card.js","sha":old_sha}]
                    if "d"*40 in path:
                        entries.append({"type":"blob","path":"home-assistant/dashboards/server.yaml","sha":server_sha})
                    return {"truncated":False,"tree":entries}
                if "/git/blobs/" in path:
                    sha = path.rsplit("/",1)[-1]
                    data = {old_sha:old,server_sha:server}[sha]
                    return {"encoding":"base64","size":len(data),"content":base64.b64encode(data).decode()}
                raise AssertionError(path)
            with patch.object(check,"CONFIG_ROOT",root), \
                 patch.object(baseline,"MANIFEST_PATH",root/"data"/"installed-manifest.json"), \
                 patch.object(check,"load_options",return_value=options), \
                 patch.object(check,"github_json",side_effect=api):
                result = baseline.initialize_manifest("INITIALIZE MANIFEST")
                manifest = baseline.load_manifest(options)
            self.assertTrue(result["initialized"])
            self.assertIn("www/house-card.js", manifest["files"])
            self.assertIn("dashboards/server.yaml", manifest["files"])
            self.assertEqual(manifest["files"]["dashboards/server.yaml"]["source_commit"],"b"*40)

    def test_new_allowlisted_path_with_local_drift_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"dashboards").mkdir()
            (root/"www").mkdir()
            (root/"www"/"house-card.js").write_text("baseline\n")
            (root/"dashboards"/"server.yaml").write_text("local: edit\n")
            remote=b"server: true\n"
            sha=blob(remote)
            options={"repository":"PheronIIx/SmartHome","branch":"main",
                     "github_token":"fake","baseline_commit":"a"*40}
            def api(path,token):
                if "/commits/main" in path:
                    return {"sha":"b"*40}
                if "/commits/" in path:
                    commit_id = path.rsplit("/",1)[-1]
                    tree_id = "c"*40 if commit_id == "a"*40 else "d"*40
                    return {"sha":commit_id,"commit":{"tree":{"sha":tree_id}}}
                if "/git/trees/" in path:
                    entries = ([{"type":"blob","path":"home-assistant/www/house-card.js","sha":sha}] if "c"*40 in path else [{"type":"blob","path":"home-assistant/dashboards/server.yaml","sha":sha}])
                    return {"truncated":False,"tree":entries}
                if "/git/blobs/" in path:
                    return {"encoding":"base64","size":len(remote),"content":base64.b64encode(remote).decode()}
                raise AssertionError(path)
            with patch.object(check,"CONFIG_ROOT",root), \
                 patch.object(baseline,"MANIFEST_PATH",root/"data"/"installed-manifest.json"), \
                 patch.object(check,"load_options",return_value=options), \
                 patch.object(check,"github_json",side_effect=api):
                result=baseline.initialize_manifest("INITIALIZE MANIFEST")
                manifest=baseline.load_manifest(options)
            self.assertEqual(result["untracked_local_paths"],["dashboards/server.yaml"])
            self.assertNotIn("dashboards/server.yaml",manifest["files"])

    def test_initialization_requires_explicit_confirmation_and_is_one_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"installed-manifest.json"
            with patch.object(baseline,"MANIFEST_PATH",path):
                with self.assertRaises(ValueError):
                    baseline.initialize_manifest("yes")
                path.write_text("{}")
                with self.assertRaises(ValueError):
                    baseline.initialize_manifest("INITIALIZE MANIFEST")

    def test_manifest_rejects_non_allowlisted_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"manifest.json"
            payload={"schema_version":1,"repository":"o/r","branch":"main","files":{
                "../secrets.yaml":{"source_commit":"a"*40,"blob_sha":"b"*40,"sha256":"c"*64,"size":1}}}
            path.write_text(json.dumps(payload))
            with patch.object(baseline,"MANIFEST_PATH",path):
                with self.assertRaises(ValueError):
                    baseline.load_manifest()


if __name__ == "__main__":
    unittest.main()
