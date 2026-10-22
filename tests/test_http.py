"""Exercise the HTTP contract and local transport boundary, not mocks of routes."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from workbench.domain import Workbench
from workbench.server import make_server


class HttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(port=0, app=Workbench())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, path, payload=None, actor="alpha-member", extra=None, raw=None):
        headers = {"X-Demo-Actor": actor}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = raw
        headers.update(extra or {})
        req = urllib.request.Request(self.url + path, data=data, headers=headers)
        try:
            response = self.opener.open(req, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            result = json.loads(body) if response.headers.get_content_type() == "application/json" else body
            return response.status, result, response.headers

    def test_bootstrap_contains_only_synthetic_current_tenant_material(self):
        status, body, _ = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(body["actor"]["id"], "alpha-member")
        self.assertEqual(body["mode"], "offline")
        self.assertTrue(body["tools"])
        self.assertTrue(body["documents"])

    def test_health_and_static_page(self):
        status, body, _ = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ready"])
        status, body, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!", body[:100])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_approval_executes_once_over_real_http(self):
        self.request("/api/reset", {}, actor="alpha-owner")
        _, before, _ = self.request("/api/state")
        status, run, _ = self.request("/api/run", {"message": "创建任务：整理演示材料", "scenario": "create_task"})
        self.assertEqual(status, 200)
        self.assertEqual(run["status"], "approval_required")
        approval = run["approval"]["id"]
        status, finished, _ = self.request("/api/approvals/" + approval, {"decision": "approve"})
        self.assertEqual(status, 200)
        self.assertEqual(finished["status"], "completed")
        self.request("/api/approvals/" + approval, {"decision": "approve"})
        _, after, _ = self.request("/api/state")
        self.assertEqual(len(after["tasks"]), len(before["tasks"]) + 1)

    def test_cross_actor_approval_cannot_take_over(self):
        _, run, _ = self.request("/api/run", {"message": "创建任务：绑定身份测试", "scenario": "create_task"})
        approval = run["approval"]["id"]
        status, body, _ = self.request("/api/approvals/" + approval, {"decision": "approve"}, actor="beta-owner")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_viewer_reset_denied(self):
        status, body, _ = self.request("/api/reset", {}, actor="alpha-viewer")
        self.assertEqual(status, 403)
        self.assertIn("error", body)

    def test_invalid_actor_rejected(self):
        status, body, _ = self.request("/api/state", actor="unknown-actor")
        self.assertIn(status, (400, 401, 403))
        self.assertIn("error", body)

    def test_unknown_json_fields_not_ignored(self):
        status, body, _ = self.request("/api/run", {"message": "查询规则", "tenant": "beta"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "unknown_field")

    def test_malformed_json_and_non_object_rejected(self):
        status, _, _ = self.request("/api/run", raw=b"{", extra={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, _, _ = self.request("/api/run", raw=b"[]", extra={"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_wrong_content_type_and_large_input_rejected(self):
        status, _, _ = self.request("/api/run", raw=b"message=hello")
        self.assertEqual(status, 415)
        status, _, _ = self.request("/api/run", {"message": "x" * 18000})
        self.assertEqual(status, 413)

    def test_cross_origin_write_rejected(self):
        status, _, _ = self.request("/api/run", {"message": "查询规则"}, extra={"Origin": "https://example.org"})
        self.assertEqual(status, 403)

    def test_malformed_origin_is_a_structured_denial(self):
        status, body, _ = self.request("/api/run", {"message": "查询规则"}, extra={"Origin": "http://["})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "origin_not_allowed")

    def test_deep_json_is_a_structured_error(self):
        raw = b'{"message":' + b'[' * 5000 + b'0' + b']' * 5000 + b'}'
        status, body, _ = self.request("/api/run", raw=raw, extra={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_unexpected_host_and_path_traversal_rejected(self):
        status, _, _ = self.request("/api/state", extra={"Host": "example.org"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("/%2e%2e/workbench/domain.py")
        self.assertEqual(status, 404)

    def test_external_bind_disabled(self):
        with self.assertRaises(ValueError):
            make_server("0.0.0.0", 0)


if __name__ == "__main__":
    unittest.main()
