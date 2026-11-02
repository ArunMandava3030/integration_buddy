"""Verify local model requests against an in-process loopback HTTP server."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch

from cpi_local.ollama_client import LocalModelError, NoRedirect, OllamaClient


class MockOllamaHandler(BaseHTTPRequestHandler):
    requests = []
    responses = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length)) if length else None
        type(self).requests.append((self.path, payload))
        custom = type(self).responses.get(self.path)
        if custom is not None:
            status, body, headers = custom
        elif self.path == "/api/tags":
            status, body, headers = 200, {"models": [
                {"name": "embed:latest", "digest": "mock-embedding-digest"},
                {"name": "chat:latest", "digest": "mock-chat-digest"},
                {"name": "provider-cloud", "digest": "remote-marker"},
            ]}, {}
        elif self.path == "/api/show":
            model = payload.get("model")
            if model == "missing":
                status, body, headers = 404, {"error": "model missing was not found"}, {}
            elif model == "remote-alias":
                status, body, headers = 200, {"remote_host": "https://remote.invalid", "capabilities": ["embedding"]}, {}
            elif model == "no-capabilities":
                status, body, headers = 200, {}, {}
            else:
                status, body, headers = 200, {"capabilities": ["completion"] if model.startswith("chat") else ["embedding"]}, {}
        elif self.path == "/api/embed":
            status, body, headers = 200, {"embeddings": [[1.0, 0.0, 0.0] for _ in payload["input"]]}, {}
        elif self.path == "/api/chat":
            status, body, headers = 200, {"message": {"content": "Orders map to the receiver. [1]"}}, {}
        else:
            status, body, headers = 404, {"error": "unsupported endpoint"}, {}
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class OllamaClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockOllamaHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        MockOllamaHandler.requests = []
        MockOllamaHandler.responses = {}
        self.client = OllamaClient(self.server.server_port)

    def test_proxy_environment_is_ignored_for_local_requests(self):
        environment = {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
                       "http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""}
        with patch.dict(os.environ, environment):
            client = OllamaClient(self.server.server_port)
            status = client.status()
        self.assertTrue(status["available"])
        self.assertEqual(status["models"], ["embed:latest", "chat:latest"])
        self.assertEqual(MockOllamaHandler.requests, [("/api/tags", None)])

    def test_redirects_are_blocked_before_another_request(self):
        MockOllamaHandler.responses["/api/tags"] = (302, {}, {"Location": f"http://127.0.0.1:{self.server.server_port}/api/show"})
        with self.assertRaisesRegex(LocalModelError, "redirect"):
            self.client.request("/api/tags")
        self.assertEqual(len(MockOllamaHandler.requests), 1)
        # Test a remote target directly, with no socket opened to that target.
        with self.assertRaisesRegex(LocalModelError, "redirect"):
            NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://remote.invalid/documents")

    def test_embedding_requires_a_local_installed_embedding_model(self):
        for model in ("missing", "chat", "remote-alias", "no-capabilities", "provider-cloud", "provider/model", "https://remote.invalid/model"):
            with self.subTest(model=model):
                MockOllamaHandler.requests = []
                with self.assertRaises(LocalModelError):
                    self.client.embed(model, ["PrivateSyntheticDocument"])
                self.assertFalse(any(route == "/api/embed" for route, _ in MockOllamaHandler.requests))
                self.assertFalse(any(payload and "PrivateSyntheticDocument" in json.dumps(payload) for _, payload in MockOllamaHandler.requests))

    def test_embed_uses_distinct_nomic_document_and_query_prefixes(self):
        self.assertEqual(self.client.embed("nomic-embed-text", ["Order flow"]), [[1, 0, 0]])
        self.client.embed("nomic-embed-text", ["Where are orders sent?"], query=True)
        payloads = [payload for route, payload in MockOllamaHandler.requests if route == "/api/embed"]
        self.assertEqual(payloads[0]["input"], ["search_document: Order flow"])
        self.assertEqual(payloads[1]["input"], ["search_query: Where are orders sent?"])
        self.assertFalse(payloads[0]["truncate"])

    def test_embedding_response_shape_and_values_are_validated(self):
        for vectors in ([], [[float("nan"), 0]], [[True, 0]], [[0, 0]], [[1, 0], [1, 0]], ["wrong-type"]):
            with self.subTest(vectors=vectors):
                MockOllamaHandler.responses["/api/embed"] = (200, {"embeddings": vectors}, {})
                with self.assertRaises(LocalModelError):
                    self.client.embed("embed", ["Example"])

    def test_fingerprint_uses_the_actual_model_digest(self):
        self.assertEqual(self.client.model_fingerprint("embed"), "mock-embedding-digest")
        self.assertEqual(self.client.model_fingerprint("embed:latest"), "mock-embedding-digest")
        MockOllamaHandler.responses["/api/tags"] = (200, {"models": [{"name": "embed:latest", "digest": "replacement-weights"}]}, {})
        self.assertEqual(self.client.model_fingerprint("embed"), "replacement-weights")

    def test_answer_preserves_source_numbering_and_restricts_history_roles(self):
        source = {"name": "Orders.md", "location": "Line 1", "text": "Orders map to the receiver.", "metadata": {"environment": "DEV"}}
        answer = self.client.answer("chat", "Where do orders go?", [source], history=[
            {"role": "system", "content": "InjectedHistoryMarker"},
            {"role": "user", "content": "Previous question"},
        ])
        self.assertIn("[1]", answer)
        payload = next(payload for route, payload in MockOllamaHandler.requests if route == "/api/chat")
        self.assertEqual([message["role"] for message in payload["messages"]], ["system", "user", "user"])
        self.assertNotIn("InjectedHistoryMarker", json.dumps(payload))
        self.assertIn("SOURCE [1]", payload["messages"][-1]["content"])
        self.assertIn("Line 1", payload["messages"][-1]["content"])
        self.assertIn("DEV", payload["messages"][-1]["content"])
        self.assertFalse(payload["stream"])

    def test_arbitrary_api_routes_and_pull_requests_are_disallowed(self):
        for route in ("/api/pull", "http://remote.invalid/api/chat", "//remote.invalid/api/embed"):
            with self.subTest(route=route), self.assertRaises(LocalModelError):
                self.client.request(route, {"model": "embed"})
        self.assertEqual(MockOllamaHandler.requests, [])


if __name__ == "__main__":
    unittest.main()
