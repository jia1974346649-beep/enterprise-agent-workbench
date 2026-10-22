"""Loopback fake-server protocol tests; no real model or external service."""

import contextlib
import io
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from workbench.providers import OfflineProvider, OllamaProvider, ProviderError, provider_from_environment


@contextlib.contextmanager
def fake_model(body=None, status=200, location=None, raw=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            requests.append({"path": self.path, "body": json.loads(self.rfile.read(length)), "headers": dict(self.headers)})
            payload = raw if raw is not None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ProviderTests(unittest.TestCase):
    def test_default_provider_does_not_require_model_or_network(self):
        self.assertIsInstance(provider_from_environment({}), OfflineProvider)
        self.assertIsInstance(provider_from_environment({"WORKBENCH_MODE": "offline"}), OfflineProvider)

    def test_configuration_is_explicit_and_model_name_required(self):
        for config in ({"WORKBENCH_MODE": "remote"}, {"WORKBENCH_MODE": "ollama"}):
            with self.assertRaises(ProviderError):
                provider_from_environment(config)
        configured = provider_from_environment({"WORKBENCH_MODE": "ollama", "WORKBENCH_OLLAMA_MODEL": "demo-model:latest"})
        self.assertEqual(configured.mode, "ollama")

    def test_remote_credentials_paths_and_non_http_endpoints_rejected(self):
        urls = ["https://127.0.0.1", "http://example.invalid", "file:///tmp/model", "http://127.0.0.1.evil.invalid",
                "http://user:password@localhost:11434", "http://localhost:11434/private", "http://localhost:11434?x=1",
                "http://localhost:11434#x", "http://0.0.0.0", "http://localhost:0", "http://localhost:bad", None, {}]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ProviderError):
                OllamaProvider("demo", url)

    def test_model_name_and_timeout_are_bounded(self):
        for name in ("", "a" * 121, "model\nvalue", "--flag", None):
            with self.assertRaises(ProviderError):
                OllamaProvider(name)
        for timeout in (0, 31, True, float("nan"), "15"):
            with self.assertRaises(ProviderError):
                OllamaProvider("demo", timeout=timeout)

    def test_native_ollama_tool_call_protocol_round_trip(self):
        response = {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "list_tasks", "arguments": {"status": "open"}}}]}}
        with fake_model(response) as (url, requests):
            provider = OllamaProvider("demo:tools", url)
            messages = [{"role": "user", "content": "合成任务"}]
            tools = [{"type": "function", "function": {"name": "list_tasks"}}]
            result = provider.next(messages, tools)
        self.assertEqual(result["tool_calls"][0]["name"], "list_tasks")
        self.assertEqual(result["tool_calls"][0]["arguments"], {"status": "open"})
        self.assertEqual(requests[0]["path"], "/api/chat")
        self.assertEqual(requests[0]["body"]["messages"], messages)
        self.assertEqual(requests[0]["body"]["tools"], tools)
        self.assertFalse(requests[0]["body"]["stream"])
        self.assertFalse(any(name.lower() == "authorization" for name in requests[0]["headers"]))

    def test_plain_final_response_is_supported(self):
        with fake_model({"message": {"content": "合成答案"}}) as (url, _):
            result = OllamaProvider("demo", url).next([], [])
        self.assertEqual(result, {"content": "合成答案", "tool_calls": []})

    def test_malformed_tool_arguments_and_empty_content_fail_closed(self):
        samples = [[], {}, {"message": {}}, {"message": {"content": 5}},
                   {"message": {"content": "x", "tool_calls": {}}},
                   {"message": {"tool_calls": [{"function": {"name": "list_tasks", "arguments": "{}"}}]}},
                   {"message": {"tool_calls": [{"function": {"name": "list_tasks", "arguments": []}}]}},
                   {"message": {"tool_calls": [None]}},
                   {"message": {"content": "x" * 8001}},
                   {"message": {"tool_calls": [{}] * 5}}]
        for body in samples:
            with self.subTest(body=body):
                with fake_model(body) as (url, _), self.assertRaises(ProviderError):
                    OllamaProvider("demo", url).next([], [])

    def test_invalid_json_encoding_and_oversized_response_are_rejected(self):
        for raw in (b"not json", b"\xff", b" " * (OllamaProvider.MAX_RESPONSE_BYTES + 1)):
            with fake_model(raw=raw) as (url, _), self.assertRaises(ProviderError):
                OllamaProvider("demo", url).next([], [])

    def test_redirect_is_never_followed_even_to_another_loopback(self):
        with fake_model({"message": {"content": "should not be reached"}}) as (target, requests):
            with fake_model({}, status=307, location=target + "/api/chat") as (source, _):
                with self.assertRaises(ProviderError):
                    OllamaProvider("demo", source).next([], [])
            self.assertEqual(requests, [])

    def test_network_failure_does_not_expose_endpoint_details(self):
        with fake_model({"internal": "DO-NOT-EXPOSE"}, status=500) as (url, _):
            with self.assertRaises(ProviderError) as caught:
                OllamaProvider("demo", url).next([], [])
        self.assertEqual(caught.exception.code, "local_model_unavailable")
        self.assertNotIn("DO-NOT-EXPOSE", str(caught.exception))

    def test_failed_http_responses_are_closed(self):
        for status in (307, 500):
            payload = io.BytesIO(b"internal response")
            error = urllib.error.HTTPError("http://127.0.0.1", status, "failed", {}, payload)
            provider = OllamaProvider("demo")
            with patch.object(provider._opener, "open", side_effect=error):
                with self.assertRaises(ProviderError):
                    provider.next([], [])
            self.assertTrue(payload.closed)


if __name__ == "__main__":
    unittest.main()
