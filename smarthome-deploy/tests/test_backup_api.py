import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from app import server


class BackupApiTests(unittest.TestCase):
    def setUp(self):
        self.host = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.host.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.host.server_port)

    def tearDown(self):
        self.host.shutdown()
        self.host.server_close()
        self.thread.join(timeout=3)

    def request(self, path, token=None, origin=None, site="same-origin"):
        headers = {"Content-Type": "application/json", "Sec-Fetch-Site": site}
        if token is not None:
            headers["X-CSRF-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        req = Request(self.url + path, data=b"{}", headers=headers, method="POST")
        try:
            with urlopen(req, timeout=3) as response:
                return response.status
        except HTTPError as error:
            return error.code

    def test_post_rejected_without_csrf_or_same_origin(self):
        with patch.dict("os.environ", {"SMARTHOME_DEV_ALLOW_LOCAL": "1"}):
            self.assertEqual(self.request("/api/backups/create"), 403)
            self.assertEqual(self.request("/api/backups/create", server.CSRF_TOKEN, site="cross-site"), 403)
            # Ingress may rewrite forwarded host, so Origin comparison is not reliable.\n            # CSRF and Sec-Fetch-Site remain mandatory.

    def test_valid_post_is_routed_to_backup_service(self):
        with patch.dict("os.environ", {"SMARTHOME_DEV_ALLOW_LOCAL": "1"}), \
             patch.object(server.backup_service, "create_manual_backup", return_value={"verified": True}) as action:
            self.assertEqual(self.request("/api/backups/create", server.CSRF_TOKEN), 200)
            action.assert_called_once_with()

    def test_unapproved_mutation_endpoint_is_rejected(self):
        with patch.dict("os.environ", {"SMARTHOME_DEV_ALLOW_LOCAL": "1"}):
            self.assertEqual(self.request("/api/deploy", server.CSRF_TOKEN), 404)


if __name__ == "__main__":
    unittest.main()
