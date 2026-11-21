"""HTTP guard regression tests for opt-in JS batch API."""
import io
import json
import unittest
from unittest.mock import patch

from app import server

class FakeHandler(server.Handler):
    def __init__(self, path, payload=None, *, token=None, address=server.INGRESS_PROXY,
                 content_type="application/json", fetch_site="same-origin"):
        self.path = path
        self.client_address = (address, 12345)
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(json.dumps(payload or {}).encode())),
            "X-CSRF-Token": token if token is not None else server.CSRF_TOKEN,
            "Sec-Fetch-Site": fetch_site,
        }
        self.rfile = io.BytesIO(json.dumps(payload or {}).encode())
        self.status = None
        self.result = None

    def _send(self, status, content_type, payload):
        self.status = status
        self.result = json.loads(payload)

    def send_error(self, status):
        self.status = status


class JsBatchApiTests(unittest.TestCase):
    def invoke(self, path, body=None, **kwargs):
        handler = FakeHandler(path, body, **kwargs)
        with patch.dict("os.environ", {"SMARTHOME_DEV_ALLOW_LOCAL": "0"}):
            handler.do_POST()
        return handler

    def test_prepare_forwards_only_empty_body(self):
        with patch.object(server.multifile, "prepare", return_value={"plan_id": "abc"}) as operation:
            response = self.invoke("/api/multifile/prepare", {})
            self.assertEqual(response.status, 200)
            operation.assert_called_once_with()
            self.assertEqual(response.result["plan_id"], "abc")

    def test_install_requires_exact_confirmation_shape(self):
        body = {"plan_id": "abc", "confirmation": "INSTALL JS BATCH"}
        with patch.object(server.multifile, "install", return_value={"status": "installed"}) as operation:
            response = self.invoke("/api/multifile/install", body)
            self.assertEqual(response.status, 200)
            operation.assert_called_once_with("abc", "INSTALL JS BATCH")
            invalid = self.invoke("/api/multifile/install", {**body, "extra": "x"})
            self.assertEqual(invalid.status, 400)
            self.assertEqual(operation.call_count, 1)

    def test_rollback_routes_to_engine(self):
        body = {"operation_id": "a" * 24, "confirmation": "ROLLBACK JS BATCH"}
        with patch.object(server.multifile, "rollback", return_value={"status": "rolled_back"}) as operation:
            response = self.invoke("/api/multifile/rollback", body)
            self.assertEqual(response.status, 200)
            operation.assert_called_once_with(body["operation_id"], "ROLLBACK JS BATCH")

    def test_missing_csrf_denied_before_install(self):
        with patch.object(server.multifile, "install") as operation:
            response = self.invoke("/api/multifile/install",
                {"plan_id": "abc", "confirmation": "INSTALL JS BATCH"}, token="invalid")
            self.assertEqual(response.status, 403)
            operation.assert_not_called()

    def test_non_ingress_denied_before_install(self):
        with patch.object(server.multifile, "install") as operation:
            response = self.invoke("/api/multifile/install",
                {"plan_id": "abc", "confirmation": "INSTALL JS BATCH"}, address="127.0.0.1")
            self.assertEqual(response.status, 403)
            operation.assert_not_called()

    def test_cross_site_denied(self):
        with patch.object(server.multifile, "install") as operation:
            response = self.invoke("/api/multifile/install",
                {"plan_id": "abc", "confirmation": "INSTALL JS BATCH"}, fetch_site="cross-site")
            self.assertEqual(response.status, 403)
            operation.assert_not_called()

    def test_read_only_api_requires_ingress_proxy(self):
        with patch.dict("os.environ", {"SMARTHOME_DEV_ALLOW_LOCAL": "0"}), \
             patch.object(server.sync_plan, "prepare_sync_plan") as operation:
            handler = FakeHandler("/api/sync/plan", address="127.0.0.1")
            handler.do_GET()
            self.assertEqual(handler.status, 403)
            operation.assert_not_called()

    def test_read_only_api_from_ingress(self):
        with patch.dict("os.environ", {"SMARTHOME_DEV_ALLOW_LOCAL": "0"}), \
             patch.object(server.sync_plan, "prepare_sync_plan", return_value={
                 "configured": True, "target": "a" * 40, "files": []
             }), patch.object(server.status_export, "publish_plan") as publication:
            handler = FakeHandler("/api/sync/plan")
            handler.do_GET()
            self.assertEqual(handler.status, 200)
            publication.assert_called_once()

    def test_engine_gate_is_disabled_by_default(self):
        from app import multifile
        with patch.object(multifile.check, "load_options", return_value={"enable_multifile_install": False}):
            with self.assertRaisesRegex(ValueError, "disabled"):
                multifile.prepare()

    def test_unknown_endpoint_not_forwarded(self):
        response = self.invoke("/api/multifile/delete", {})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
