"""HTTP integration checks using temporary documents and a mocked local model."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.parse

from app import Application, LocalServer
from cpi_local.ollama_client import LocalModelError, OllamaClient


class ApplicationHTTPTests(unittest.TestCase):
    def setUp(self):
        scratch_root = Path(__file__).resolve().parents[1] / ".runtime" / "test-http"
        scratch_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="app-", dir=scratch_root)
        self.directory = Path(self.temporary.name).resolve()
        self.assertEqual(self.directory.parent, scratch_root.resolve())
        self.addCleanup(self.temporary.cleanup)
        self.config_path = self.directory / "config.json"
        self.config_path.write_text(json.dumps({"semantic_search": False, "auto_import": False}), encoding="utf-8")
        self.client = mock.Mock(spec=OllamaClient)
        self.client.status.return_value = {
            "available": True,
            "models": ["qwen3:4b", "nomic-embed-text:latest"],
            "records": [],
        }
        self.client.is_cloud_name.side_effect = OllamaClient.is_cloud_name
        self.client.answer.side_effect = LocalModelError("The local chat model is unavailable for this test.")
        self.application = Application(self.directory / "data", self.config_path, client=self.client)
        self.server = LocalServer(("127.0.0.1", 0), self.application)
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        code, status, _ = self.request("GET", "/api/status", csrf=False)
        self.assertEqual(code, 200)
        self.token = status["csrf_token"]

    def stop_server(self):
        deadline = time.monotonic() + 5
        while self.application.job and self.application.job["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.client.embed.assert_not_called()

    def request(self, method, path, payload=None, body=None, headers=None, csrf=True):
        request_headers = {"Origin": self.origin}
        if csrf and method in {"POST", "DELETE"}:
            request_headers["X-CSRF-Token"] = self.token
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif body is not None:
            request_headers["Content-Type"] = "application/octet-stream"
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            content = response.read()
            return response.status, json.loads(content), dict(response.getheaders())
        finally:
            connection.close()

    def upload(self, name, content):
        if isinstance(content, str):
            content = content.encode("utf-8")
        query = urllib.parse.urlencode({"name": name})
        code, result, _ = self.request("POST", "/api/upload?" + query, body=content)
        self.assertEqual(code, 201, result)
        self.assertEqual(result["name"], name)
        return result["id"]

    def wait_for_import(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            code, status, _ = self.request("GET", "/api/status")
            self.assertEqual(code, 200, status)
            job = status["job"]
            if job and job["state"] != "running":
                self.assertEqual(job["state"], "completed", job)
                return job
            time.sleep(0.01)
        self.fail("The import worker did not finish within ten seconds.")

    def import_uploaded(self, tokens, metadata=None):
        request = {"ids": tokens}
        if metadata is not None:
            request["metadata"] = metadata
        code, accepted, _ = self.request("POST", "/api/import-uploaded", request)
        self.assertEqual(code, 202, accepted)
        self.assertIn("id", accepted["job"])
        return self.wait_for_import()

    def documents(self):
        code, result, _ = self.request("GET", "/api/documents")
        self.assertEqual(code, 200, result)
        return result["documents"]

    def test_local_host_origin_and_csrf_protection(self):
        cases = [
            ("GET", "/api/status", None, {"Host": f"attacker.example:{self.port}"}, False),
            ("GET", "/api/status", None, {"Origin": "https://attacker.example"}, False),
            ("POST", "/api/settings", {"model": "changed"}, {}, False),
            ("POST", "/api/settings", {"model": "changed"}, {"X-CSRF-Token": "wrong"}, True),
            ("DELETE", "/api/documents/1", None, {}, False),
        ]
        for method, path, payload, headers, csrf in cases:
            with self.subTest(method=method, headers=headers, csrf=csrf):
                code, result, _ = self.request(method, path, payload, headers=headers, csrf=csrf)
                self.assertEqual(code, 403, result)
        self.assertEqual(self.application.settings["model"], "qwen3:4b")
        code, status, headers = self.request("GET", "/api/status", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(status["ollama"]["models"], ["qwen3:4b"])

    def test_upload_import_source_and_document_answer_fallback(self):
        token = self.upload("customer-prod.md", "# Customer replication\nDuplicate customer events are skipped using EventId.\n")
        metadata = {"environment": "PROD", "iflow": "CUSTOMER_REPLICATION"}
        job = self.import_uploaded([token], metadata)
        self.assertEqual((job["processed"], job["indexed"], job["failed"]), (1, 1, 0))
        documents = self.documents()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["metadata"], metadata)
        code, answer, _ = self.request("POST", "/api/chat", {"question": "How are duplicate customer events handled?"})
        self.assertEqual(code, 200, answer)
        self.assertEqual(answer["mode"], "search")
        self.assertIn("EventId", answer["answer"])
        self.assertIn("unavailable", answer["notice"])
        self.assertEqual(answer["sources"][0]["document_id"], documents[0]["id"])
        self.client.answer.assert_called_once()
        source = answer["sources"][0]
        code, loaded_source, _ = self.request("GET", f"/api/sources/{source['id']}")
        self.assertEqual(code, 200)
        self.assertEqual(loaded_source["text"], source["text"])
        self.assertEqual(loaded_source["metadata"], metadata)
        code, status, _ = self.request("GET", "/api/status")
        self.assertEqual(status["stats"]["documents"], 1)
        self.assertGreater(status["stats"]["chunks"], 0)

    def test_no_evidence_does_not_call_the_model(self):
        token = self.upload("customer.md", "Customer events use EventId for duplicate detection.")
        self.import_uploaded([token])
        code, answer, _ = self.request("POST", "/api/chat", {"question": "Photosynthesis chlorophyll wavelength?"})
        self.assertEqual(code, 200)
        self.assertEqual(answer["sources"], [])
        self.assertEqual(answer["mode"], "search")
        self.assertIn("could not find", answer["answer"])
        self.client.answer.assert_not_called()

    def test_document_filter_keeps_evidence_in_selected_document(self):
        production = self.upload("production.md", "Production retry attempts are limited to three. The receiver waits thirty seconds.")
        development = self.upload("development.md", "Development retry attempts are limited to one. The receiver waits two seconds.")
        self.import_uploaded([production, development])
        documents = {document["name"]: document for document in self.documents()}
        selected_id = documents["development.md"]["id"]
        code, answer, _ = self.request("POST", "/api/chat", {
            "question": "How many retry attempts are allowed?", "document_id": selected_id,
        })
        self.assertEqual(code, 200)
        self.assertTrue(answer["sources"])
        self.assertTrue(all(source["document_id"] == selected_id for source in answer["sources"]))
        self.assertIn("limited to one", answer["answer"])
        self.assertNotIn("limited to three", answer["answer"])
        code, answer, _ = self.request("POST", "/api/chat", {"question": "retry attempts", "document_id": 999999})
        self.assertEqual(code, 200)
        self.assertEqual(answer["sources"], [])

    def test_delete_removes_search_results_and_previous_source(self):
        token = self.upload("orders.md", "Orders use a unique order number for duplicate detection.")
        self.import_uploaded([token])
        document = self.documents()[0]
        _, answer, _ = self.request("POST", "/api/chat", {"question": "order number"})
        source_id = answer["sources"][0]["id"]
        code, result, _ = self.request("DELETE", f"/api/documents/{document['id']}")
        self.assertEqual((code, result), (200, {"deleted": True}))
        self.assertEqual(self.documents(), [])
        code, _, _ = self.request("GET", f"/api/sources/{source_id}")
        self.assertEqual(code, 404)
        code, answer, _ = self.request("POST", "/api/chat", {"question": "order number"})
        self.assertEqual(code, 200)
        self.assertEqual(answer["sources"], [])
        code, _, _ = self.request("DELETE", f"/api/documents/{document['id']}")
        self.assertEqual(code, 404)

    def test_reupload_updates_one_document_and_invalidates_old_source(self):
        first = self.upload("retry.md", "Retry policy permits three attempts.")
        self.import_uploaded([first])
        document_id = self.documents()[0]["id"]
        _, answer, _ = self.request("POST", "/api/chat", {"question": "retry policy"})
        previous_source = answer["sources"][0]["id"]
        replacement = self.upload("retry.md", "Retry policy now permits five attempts.")
        self.import_uploaded([replacement])
        documents = self.documents()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["id"], document_id)
        code, _, _ = self.request("GET", f"/api/sources/{previous_source}")
        self.assertEqual(code, 404)
        code, answer, _ = self.request("POST", "/api/chat", {"question": "retry policy"})
        self.assertEqual(code, 200)
        self.assertIn("five attempts", answer["answer"])
        self.assertNotIn("three attempts", answer["answer"])

    def test_duplicate_pending_upload_preserves_first_content(self):
        first = self.upload("retry.md", "The first retry policy allows three attempts.")
        code, result, _ = self.request("POST", "/api/upload?name=retry.md", body=b"Replacement retry policy allows five attempts.")
        self.assertEqual(code, 409, result)
        self.assertIn("already queued", result["error"])
        self.import_uploaded([first])
        self.assertEqual(len(self.documents()), 1)
        code, answer, _ = self.request("POST", "/api/chat", {"question": "retry policy"})
        self.assertEqual(code, 200)
        self.assertIn("three attempts", answer["answer"])
        self.assertNotIn("five attempts", answer["answer"])

    def test_reload_config_applies_external_folder_change_and_imports(self):
        folder = self.directory / "configured-documents"
        folder.mkdir()
        (folder / "delivery.md").write_text("Delivery confirmation uses the TrackingId field.", encoding="utf-8")
        configuration = self.application.settings.copy()
        configuration.update(documents_folder=str(folder), auto_import=True, model="test-local:latest")
        self.config_path.write_text(json.dumps(configuration), encoding="utf-8")
        code, result, _ = self.request("POST", "/api/reload-config", {}, csrf=False)
        self.assertEqual(code, 403, result)
        self.assertEqual(self.application.settings["documents_folder"], "")
        code, result, _ = self.request("POST", "/api/reload-config", {})
        self.assertEqual(code, 200, result)
        self.assertEqual(result["settings"]["documents_folder"], str(folder))
        self.assertEqual(result["settings"]["model"], "test-local:latest")
        self.assertIsNotNone(result["job"])
        job = self.wait_for_import()
        self.assertEqual((job["indexed"], job["failed"]), (1, 0))
        self.assertEqual([item["name"] for item in self.documents()], ["delivery.md"])
        self.assertTrue((folder / "delivery.md").is_file())

    def test_missing_folder_and_failed_document_are_reported(self):
        missing = self.directory / "missing-documents"
        code, result, _ = self.request("POST", "/api/import-folder", {"path": str(missing)})
        self.assertEqual(code, 400, result)
        self.assertIn("does not exist", result["error"])
        self.assertIsNone(self.application.job)
        broken = self.upload("broken.txt", b"\x00\x00Binary content")
        valid = self.upload("valid.md", "Valid invoice routing uses the CompanyCode field.")
        with self.assertLogs("cpi-local", level="ERROR"):
            job = self.import_uploaded([broken, valid])
        self.assertEqual((job["processed"], job["indexed"], job["failed"]), (2, 1, 1))
        self.assertTrue(any("broken.txt" in error for error in job["errors"]))
        self.assertEqual([item["name"] for item in self.documents()], ["valid.md"])

    def test_folder_import_recurses_and_skips_unchanged_files(self):
        folder = self.directory / "source-documents"
        (folder / "nested").mkdir(parents=True)
        (folder / ".hidden").mkdir()
        (folder / "overview.md").write_text("Invoice routing uses company code.", encoding="utf-8")
        (folder / "nested" / "mapping.groovy").write_text("// CountryCode maps to the country field", encoding="utf-8")
        (folder / ".hidden" / "ignored.md").write_text("This hidden folder should not be imported.", encoding="utf-8")
        (folder / "unsupported.bin").write_bytes(b"unsupported")
        code, accepted, _ = self.request("POST", "/api/import-folder", {
            "path": str(folder), "metadata": {"environment": "UAT"},
        })
        self.assertEqual(code, 202, accepted)
        job = self.wait_for_import()
        self.assertEqual((job["indexed"], job["failed"]), (2, 0))
        documents = self.documents()
        self.assertEqual({Path(item["name"]).as_posix() for item in documents}, {"overview.md", "nested/mapping.groovy"})
        self.assertTrue(all(item["metadata"] == {"environment": "UAT"} for item in documents))
        code, accepted, _ = self.request("POST", "/api/import-folder", {"path": str(folder)})
        self.assertEqual(code, 202, accepted)
        job = self.wait_for_import()
        self.assertEqual((job["indexed"], job["unchanged"], job["failed"]), (0, 2, 0))
        self.assertTrue((folder / "overview.md").is_file())
        self.assertTrue(all(item["metadata"] == {"environment": "UAT"} for item in self.documents()))

    def test_upload_rejects_traversal_unsupported_type_and_empty_body(self):
        for name in ("../outside.txt", "folder/../outside.txt", "program.exe", "/absolute.txt"):
            with self.subTest(name=name):
                query = urllib.parse.urlencode({"name": name})
                code, result, _ = self.request("POST", "/api/upload?" + query, body=b"content")
                self.assertEqual(code, 400, result)
        code, result, _ = self.request("POST", "/api/upload?name=empty.txt", body=b"")
        self.assertEqual(code, 413, result)
        self.assertEqual(list(self.application.upload_dir.iterdir()), [])

    def test_running_import_blocks_settings_and_second_import(self):
        token = self.upload("slow.md", "Invoice processing uses the company code.")
        entered = threading.Event()
        release = threading.Event()
        ingest = self.application.knowledge.ingest_file

        def paused_ingest(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("Test did not release import worker")
            return ingest(*args, **kwargs)

        with mock.patch.object(self.application.knowledge, "ingest_file", side_effect=paused_ingest):
            try:
                code, accepted, _ = self.request("POST", "/api/import-uploaded", {"ids": [token]})
                self.assertEqual(code, 202, accepted)
                self.assertTrue(entered.wait(timeout=5))
                code, status, _ = self.request("GET", "/api/status")
                self.assertEqual(status["job"]["state"], "running")
                code, result, _ = self.request("POST", "/api/settings", {"model": "another-local-model"})
                self.assertEqual(code, 409, result)
                code, result, _ = self.request("POST", "/api/reload-config", {})
                self.assertEqual(code, 409, result)
                code, result, _ = self.request("POST", "/api/import-folder", {"path": str(self.directory)})
                self.assertEqual(code, 409, result)
                code, result, _ = self.request("DELETE", "/api/documents/1")
                self.assertEqual(code, 409, result)
            finally:
                release.set()
            self.assertEqual(self.wait_for_import()["indexed"], 1)


if __name__ == "__main__":
    unittest.main()
