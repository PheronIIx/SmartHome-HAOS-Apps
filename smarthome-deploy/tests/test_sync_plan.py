import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import baseline, check, sync_plan


def blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class SyncPlanTests(unittest.TestCase):
    def test_per_file_baseline_distinguishes_github_updates_from_local_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"packages").mkdir()
            (root/"packages"/"g45.yaml").write_bytes(b"old\n")
            (root/"packages"/"infra.yaml").write_bytes(b"local edit\n")
            old=b"old\n"
            new=b"new\n"
            old_sha,new_sha=blob(old),blob(new)
            manifest={"schema_version":1,"repository":"PheronIIx/SmartHome","branch":"main",
                      "initialized_from_commit":"a"*40,"files":{
                "packages/g45.yaml":{"source_commit":"a"*40,"blob_sha":old_sha,
                    "sha256":hashlib.sha256(old).hexdigest(),"size":len(old)},
                "packages/infra.yaml":{"source_commit":"a"*40,"blob_sha":old_sha,
                    "sha256":hashlib.sha256(old).hexdigest(),"size":len(old)},
            }}
            options={"repository":"PheronIIx/SmartHome","branch":"main","github_token":"fake"}
            def api(path,token):
                if "/commits/main" in path:
                    return {"sha":"b"*40}
                if "/git/blobs/" in path:
                    sha=path.rsplit("/",1)[-1]
                    data={old_sha:old,new_sha:new}[sha]
                    return {"encoding":"base64","size":len(data),"content":base64.b64encode(data).decode()}
                if "/commits/" in path:
                    return {"sha":"b"*40,"commit":{"tree":{"sha":"c"*40}}}
                if "/git/trees/" in path:
                    return {"truncated":False,"tree":[
                        {"type":"blob","path":"home-assistant/packages/g45.yaml","sha":new_sha},
                        {"type":"blob","path":"home-assistant/packages/infra.yaml","sha":new_sha},
                    ]}
                raise AssertionError(path)
            with patch.object(check,"CONFIG_ROOT",root), \
                 patch.object(check,"load_options",return_value=options), \
                 patch.object(baseline,"load_manifest",return_value=manifest), \
                 patch.object(sync_plan.plan,"_managed_tree",return_value={"packages/g45.yaml":new_sha,"packages/infra.yaml":new_sha}), \
                 patch.object(check,"github_json",side_effect=api):
                result=sync_plan.prepare_sync_plan()
            actions={row["path"]:row["action"] for row in result["files"]}
            self.assertEqual(actions["packages/g45.yaml"],"candidate_review")
            self.assertEqual(actions["packages/infra.yaml"],"blocked_local_drift")
            self.assertEqual(result["totals"]["candidate_review"],1)

    def test_line_ending_only_github_change_is_not_an_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"www").mkdir()
            local=b"const card = true;\\r\\n"
            old=b"const card = true;\\n"
            target=b"const card = true;\\r\\n\\r\\n"
            (root/"www"/"house-card.js").write_bytes(local)
            old_sha,target_sha=blob(old),blob(target)
            manifest={"schema_version":1,"repository":"PheronIIx/SmartHome","branch":"main",
                      "initialized_from_commit":"a"*40,"files":{
                "www/house-card.js":{"source_commit":"a"*40,"blob_sha":old_sha,
                    "sha256":hashlib.sha256(old).hexdigest(),"size":len(old)}}}
            options={"repository":"PheronIIx/SmartHome","branch":"main","github_token":"fake"}
            def api(path,token):
                if "/git/blobs/" in path:
                    sha=path.rsplit("/",1)[-1]
                    data={old_sha:old,target_sha:target}[sha]
                    return {"encoding":"base64","size":len(data),"content":base64.b64encode(data).decode()}
                raise AssertionError(path)
            with patch.object(check,"CONFIG_ROOT",root), \\
                 patch.object(check,"load_options",return_value=options), \\
                 patch.object(baseline,"load_manifest",return_value=manifest), \\
                 patch.object(sync_plan.plan,"_managed_tree",return_value={"www/house-card.js":target_sha}), \\
                 patch.object(check,"github_json",side_effect=lambda path,token: {"sha":"b"*40}):
                result=sync_plan.prepare_sync_plan()
            self.assertEqual(result["totals"]["format_only"],1)
            self.assertEqual(result["totals"]["candidate_review"],0)
            self.assertEqual(result["files"][0]["action"],"format_only")

    def test_real_content_change_remains_update_when_local_has_only_line_ending_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"www").mkdir()
            local=b"const card = true;\\r\\n"
            old=b"const card = true;\\n"
            target=b"const card = false;\\n"
            (root/"www"/"house-card.js").write_bytes(local)
            old_sha,target_sha=blob(old),blob(target)
            manifest={"schema_version":1,"repository":"PheronIIx/SmartHome","branch":"main",
                      "initialized_from_commit":"a"*40,"files":{
                "www/house-card.js":{"source_commit":"a"*40,"blob_sha":old_sha,
                    "sha256":hashlib.sha256(old).hexdigest(),"size":len(old)}}}
            options={"repository":"PheronIIx/SmartHome","branch":"main","github_token":"fake"}
            def api(path,token):
                if "/git/blobs/" in path:
                    sha=path.rsplit("/",1)[-1]
                    data={old_sha:old,target_sha:target}[sha]
                    return {"encoding":"base64","size":len(data),"content":base64.b64encode(data).decode()}
                raise AssertionError(path)
            with patch.object(check,"CONFIG_ROOT",root), \\
                 patch.object(check,"load_options",return_value=options), \\
                 patch.object(baseline,"load_manifest",return_value=manifest), \\
                 patch.object(sync_plan.plan,"_managed_tree",return_value={"www/house-card.js":target_sha}), \\
                 patch.object(check,"github_json",side_effect=lambda path,token: {"sha":"b"*40}):
                result=sync_plan.prepare_sync_plan()
            self.assertEqual(result["totals"]["candidate_review"],1)
            self.assertEqual(result["totals"]["format_only"],0)
            self.assertEqual(result["files"][0]["action"],"candidate_review")

    def test_new_server_dashboard_is_untracked_conflict_when_local_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"dashboards").mkdir()
            (root/"dashboards"/"server.yaml").write_text("local: edit\n")
            target=b"server: true\n"
            target_sha=blob(target)
            manifest={"schema_version":1,"repository":"PheronIIx/SmartHome","branch":"main",
                      "initialized_from_commit":"a"*40,"files":{
                "packages/g45.yaml":{"source_commit":"a"*40,"blob_sha":"d"*40,
                    "sha256":"e"*64,"size":0}}}
            options={"repository":"PheronIIx/SmartHome","branch":"main","github_token":"fake"}
            def api(path,token):
                if "/commits/main" in path:
                    return {"sha":"b"*40}
                if "/commits/" in path:
                    return {"sha":"b"*40,"commit":{"tree":{"sha":"c"*40}}}
                if "/git/trees/" in path:
                    return {"truncated":False,"tree":[
                        {"type":"blob","path":"home-assistant/dashboards/server.yaml","sha":target_sha}]}
                if "/git/blobs/" in path:
                    return {"encoding":"base64","size":len(target),"content":base64.b64encode(target).decode()}
                raise AssertionError(path)
            with patch.object(check,"CONFIG_ROOT",root), \
                 patch.object(check,"load_options",return_value=options), \
                 patch.object(baseline,"load_manifest",return_value=manifest), \
                 patch.object(sync_plan.plan,"_managed_tree",return_value={"dashboards/server.yaml":target_sha}), \
                 patch.object(check,"github_json",side_effect=api):
                result=sync_plan.prepare_sync_plan()
            row=next(x for x in result["files"] if x["path"]=="dashboards/server.yaml")
            self.assertEqual(row["action"],"new_file_conflict")

    def test_diff_returns_both_installed_baseline_and_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"packages").mkdir()
            (root/"packages"/"g45.yaml").write_text("local\n")
            old,new=b"old\n",b"new\n"
            old_sha,new_sha=blob(old),blob(new)
            manifest={"schema_version":1,"repository":"PheronIIx/SmartHome","branch":"main",
                      "initialized_from_commit":"a"*40,"files":{
                "packages/g45.yaml":{"source_commit":"a"*40,"blob_sha":old_sha,
                    "sha256":hashlib.sha256(old).hexdigest(),"size":len(old)}}}
            options={"repository":"PheronIIx/SmartHome","branch":"main","github_token":"fake"}
            def api(path,token):
                if "/commits/" in path:
                    return {"sha":"b"*40,"commit":{"tree":{"sha":"c"*40}}}
                if "/git/trees/" in path:
                    return {"truncated":False,"tree":[
                        {"type":"blob","path":"home-assistant/packages/g45.yaml","sha":new_sha}]}
                if "/git/blobs/" in path:
                    sha=path.rsplit("/",1)[-1]
                    data={old_sha:old,new_sha:new}[sha]
                    return {"encoding":"base64","size":len(data),"content":base64.b64encode(data).decode()}
                raise AssertionError(path)
            with patch.object(check,"CONFIG_ROOT",root), \
                 patch.object(check,"load_options",return_value=options), \
                 patch.object(baseline,"load_manifest",return_value=manifest), \
                 patch.object(sync_plan.plan,"_managed_tree",return_value={"packages/g45.yaml":new_sha}), \
                 patch.object(check,"github_json",side_effect=api):
                result=sync_plan.file_diff("packages/g45.yaml","b"*40)
            self.assertEqual(result["baseline"]["kind"],"content_changed")
            self.assertEqual(result["target"]["kind"],"content_changed")


if __name__ == "__main__":
    unittest.main()
