import base64
import hashlib
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from app import plan


def blob_sha(value):
    return hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest()


class PlannerTests(TestCase):
    def test_approved_baseline_and_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "packages").mkdir()
            (root / "packages" / "g45.yaml").write_bytes(b"baseline\r\n")
            (root / "packages" / "infra.yaml").write_bytes(b"local override\n")
            old = b"baseline\n"
            old_sha = blob_sha(old)
            new_sha = blob_sha(b"new version\n")
            original = {"packages/g45.yaml":old_sha, "packages/infra.yaml":old_sha}
            target = {"packages/g45.yaml":new_sha,"packages/infra.yaml":new_sha,
                      "packages/notify.yaml":blob_sha(b"new file")}
            opts = {"repository":"PheronIIx/SmartHome", "branch":"main",
                    "github_token":"fake", "baseline_commit":"a"*40}
            def fake_blob(repository, token, sha):
                self.assertEqual(sha, old_sha)
                return old
            def fake_api(path, token):
                self.assertIn("/commits/", path)
                return {"sha":"b"*40}
            with patch.object(plan, "CONFIG_ROOT", root), patch.object(plan,"load_options",return_value=opts):
                with patch.object(plan, "_managed_tree",side_effect=[original,target]):
                    with patch.object(plan, "_load_blob_for_diff",side_effect=fake_blob):
                        with patch.object(plan, "github_json",side_effect=fake_api):
                            result=plan.prepare_plan()
            actions={x["path"]:x["action"] for x in result["files"]}
            self.assertEqual(actions["packages/g45.yaml"],"candidate_review")
            self.assertEqual(actions["packages/infra.yaml"],"blocked_local_drift")
            self.assertEqual(actions["packages/notify.yaml"],"new_file_review")
            self.assertFalse(result["ready_to_install"])

    def test_unset_baseline_requires_configuration(self):
        opts={"repository":"PheronIIx/SmartHome","branch":"main","github_token":"fake","baseline_commit":""}
        with patch.object(plan,"load_options",return_value=opts):
            self.assertFalse(plan.prepare_plan()["configured"])

    def test_symlink_is_blocked_as_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"packages").mkdir()
            (root/"packages"/"g45.yaml").symlink_to(root/"outside")
            with patch.object(plan, "CONFIG_ROOT", root):
                with patch.object(plan, "safe_local_file", side_effect=ValueError("symlink")):
                    with self.assertRaises(ValueError):
                        plan._classify_local("packages/g45.yaml", "a"*40,"PheronIIx/SmartHome","fake")
