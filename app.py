"""Single-user, loopback-only CPI knowledge application. Run with Python 3.10+."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cpi_local.knowledge import KnowledgeBase, SUPPORTED_EXTENSIONS
from cpi_local.ollama_client import OllamaClient, LocalModelError
from cpi_local.semantic import SemanticIndex

ROOT = Path(__file__).resolve().parent
DEFAULTS = {"documents_folder": "", "auto_import": True, "model": "qwen3:4b", "embedding_model": "nomic-embed-text", "semantic_search": True}
LOG = logging.getLogger("cpi-local")


class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Application:
    def __init__(self, data_dir=None, config_path=None, client=None):
        self.data_dir = Path(data_dir or ROOT / ".data").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir = self.data_dir / "uploads"
        self.upload_dir.mkdir(exist_ok=True)
        self.config_path = Path(config_path or ROOT / "config.json")
        self.settings = DEFAULTS.copy()
        if self.config_path.exists():
            config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict):
                raise ValueError("config.json must contain an object")
            self.settings.update(config)
        self.knowledge = KnowledgeBase(self.data_dir)
        self.semantic = SemanticIndex(self.data_dir)
        self.client = client or OllamaClient()
        self.csrf_token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.generation_lock = threading.Lock()
        self.job = None
        self.staged = {}
        self.model_status = None
        self.model_status_at = 0
        self.startup_notice = None

    def status(self):
        now = time.monotonic()
        with self.lock:
            if self.model_status is None or now - self.model_status_at > 5:
                self.model_status = self.client.status()
                self.model_status_at = now
            model_status = {key: value for key, value in self.model_status.items() if key != "records"}
            # Embedding-only models do not belong in the chat model picker.
            model_status["models"] = [name for name in model_status["models"] if "embed" not in name.lower()]
            job = dict(self.job) if self.job else None
            settings = self.settings.copy()
        return {"stats": self.knowledge.stats(), "ollama": model_status, "settings": settings,
                "semantic": self.semantic.stats(), "job": job, "csrf_token": self.csrf_token,
                "notice": self.startup_notice, "supported_extensions": sorted(SUPPORTED_EXTENSIONS)}

    def save_settings(self, changes):
        allowed = {"model", "embedding_model", "documents_folder", "auto_import", "semantic_search"}
        if set(changes) - allowed:
            raise APIError("Unknown setting")
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise APIError("Wait for the current import before changing settings.", 409)
            for key, value in changes.items():
                if key in ("auto_import", "semantic_search"):
                    if not isinstance(value, bool):
                        raise APIError(f"{key} must be true or false")
                elif not isinstance(value, str) or len(value) > 1000:
                    raise APIError(f"Invalid {key}")
                if key in ("model", "embedding_model") and self.client.is_cloud_name(value):
                    raise APIError("Only local model names are accepted")
            self.settings.update(changes)
            temporary = self.config_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.settings, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.config_path)
        return self.settings.copy()

    def reload_config(self):
        config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise APIError("config.json must contain an object")
        settings = self.save_settings({key: value for key, value in config.items() if key in DEFAULTS})
        job = None
        if settings.get("auto_import") and settings.get("documents_folder"):
            job = self.import_folder(settings["documents_folder"])
        return {"settings": settings, "job": job}

    @staticmethod
    def validate_metadata(metadata):
        if metadata is None:
            return None
        if not isinstance(metadata, dict) or len(metadata) > 12:
            raise APIError("Metadata must be an object with up to 12 fields")
        if any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 64 or len(v) > 500 for k, v in metadata.items()):
            raise APIError("Metadata fields must be short text values")
        return metadata

    def import_folder(self, path, metadata=None):
        if not isinstance(path, str) or not path.strip():
            raise APIError("Enter the full path to a local documents folder.")
        folder = Path(path.strip().strip('"')).expanduser().resolve()
        if not folder.is_dir():
            raise APIError("That folder does not exist or is not accessible.")
        metadata = self.validate_metadata(metadata)

        def files():
            ignored = {".git", ".venv", ".runtime", ".data", ".sf", "node_modules", "__pycache__"}
            for directory, names, filenames in os.walk(folder, followlinks=False):
                names[:] = [name for name in names if name not in ignored and not name.startswith(".")
                            and not (Path(directory) / name).is_symlink()
                            and not getattr(Path(directory) / name, "is_junction", lambda: False)()]
                for filename in sorted(filenames):
                    file = Path(directory) / filename
                    if file.suffix.lower() in SUPPORTED_EXTENSIONS and not file.is_symlink():
                        yield file, str(file.relative_to(folder)), metadata
        return self.start_job(files, f"Importing {folder.name}")

    def import_uploaded(self, ids, metadata=None):
        if not isinstance(ids, list) or not ids or len(ids) > 20000:
            raise APIError("Select one or more uploaded documents.")
        metadata = self.validate_metadata(metadata)
        with self.lock:
            if any(not isinstance(item, str) or item not in self.staged for item in ids):
                raise APIError("An uploaded file was not found. Upload it again.")
            selected = [(self.staged[item]["path"], self.staged[item]["name"], metadata) for item in dict.fromkeys(ids)]
            job = self.start_job(lambda: iter(selected), "Importing uploaded documents")
            for item in ids:
                self.staged.pop(item, None)
        return job

    def start_job(self, files, message):
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise APIError("An import is already running. Please wait for it to finish.", 409)
            self.job = {"id": secrets.token_hex(8), "state": "running", "processed": 0, "total": 0,
                        "indexed": 0, "unchanged": 0, "failed": 0, "current": "", "errors": [], "message": message}
            threading.Thread(target=self.run_import, args=(files,), daemon=True).start()
            return dict(self.job)

    def update_job(self, **values):
        with self.lock:
            self.job.update(values)

    def run_import(self, files):
        embedding_enabled = self.settings.get("semantic_search", True) and self.semantic.available
        embedding_model = self.settings["embedding_model"]
        warnings = []
        try:
            selected = list(files())  # Paths only; document contents are processed one at a time.
            self.update_job(total=len(selected))
            if embedding_enabled:
                try:
                    self.client.require_local(embedding_model, "embedding")
                except LocalModelError as exc:
                    embedding_enabled = False
                    warnings.append("Keyword search is ready; semantic indexing was skipped because its local model is unavailable.")
                    LOG.info("Embedding setup: %s", exc)
            for path, name, metadata in selected:
                self.update_job(current=name, message="Reading and indexing document")
                try:
                    result = self.knowledge.ingest_file(path, display_name=name, metadata=metadata)
                    if result["status"] == "indexed":
                        self.semantic.delete_document(result["id"])
                    with self.lock:
                        key = "unchanged" if result["status"] == "unchanged" else "indexed"
                        self.job[key] += 1
                    if embedding_enabled:
                        self.update_job(message="Creating local search embeddings")
                        try:
                            self.semantic.index_document(self.knowledge, result["id"], self.client, embedding_model,
                                progress=lambda count: self.update_job(message=f"Created {count} local passage embeddings"))
                        except Exception:
                            # Text remains available even if an optional model/extension fails.
                            embedding_enabled = False
                            warnings.append("Semantic indexing paused; keyword search is available. Re-import after resolving the local model error.")
                            LOG.exception("Semantic indexing failed")
                except Exception as exc:
                    LOG.exception("Document import failed: %s", name)
                    with self.lock:
                        self.job["failed"] += 1
                        if len(self.job["errors"]) < 30:
                            self.job["errors"].append(f"{name}: {exc}")
                finally:
                    with self.lock:
                        self.job["processed"] += 1
            with self.lock:
                self.job["state"] = "completed"
                self.job["current"] = ""
                self.job["message"] = "Import finished." if selected else "No supported text documents were found in that folder."
                if warnings:
                    self.job["message"] += " " + " ".join(dict.fromkeys(warnings))
        except Exception as exc:
            LOG.exception("Import failed")
            self.update_job(state="failed", message=str(exc))

    def chat(self, request):
        question = request.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 3000:
            raise APIError("Enter a question of 1–3,000 characters.")
        question = question.strip()
        document_id = request.get("document_id")
        if document_id is not None and (type(document_id) is not int or document_id < 1):
            raise APIError("Invalid document selection")
        history = request.get("history", [])
        if not isinstance(history, list) or len(history) > 100 or any(not isinstance(item, dict) for item in history):
            raise APIError("Invalid chat history")
        model = request.get("model") or self.settings["model"]
        if not isinstance(model, str) or len(model) > 200:
            raise APIError("Invalid model")
        if not self.generation_lock.acquire(blocking=False):
            raise APIError("The local model is answering another question. Please try again shortly.", 429)
        try:
            query = question
            if re.search(r"\b(it|its|that|those|they|their|this)\b", question, re.I):
                prior = next((item.get("content", "") for item in reversed(history) if item.get("role") == "user"), "")
                if isinstance(prior, str):
                    query = prior[-600:] + " " + question
            lexical = self.knowledge.search(query, limit=12, document_id=document_id)
            sources = lexical[:6]
            notice = None
            if self.settings.get("semantic_search") and self.semantic.stats()["chunks"]:
                try:
                    embedding_model = self.settings["embedding_model"]
                    fingerprint = self.client.model_fingerprint(embedding_model)
                    vector = self.client.embed(embedding_model, [query], query=True)[0]
                    semantic_hits = self.semantic.search(vector, embedding_model, limit=12, document_id=document_id, fingerprint=fingerprint)
                    by_id = {source["id"]: source for source in lexical}
                    ranks = {source["id"]: 1.0 / (60 + rank) for rank, source in enumerate(lexical, 1)}
                    for rank, hit in enumerate(semantic_hits, 1):
                        source = self.knowledge.get_chunk(hit["id"])
                        if source and (document_id is None or source["document_id"] == document_id):
                            by_id[source["id"]] = source
                            ranks[source["id"]] = ranks.get(source["id"], 0) + 1.0 / (60 + rank)
                    sources = [dict(by_id[key], score=ranks[key]) for key in sorted(ranks, key=ranks.get, reverse=True)[:6]]
                except Exception:
                    LOG.exception("Semantic query unavailable; using keyword results")
                    notice = "Semantic search is unavailable; these results use keyword search."
            if not sources:
                return {"answer": "I could not find that in the indexed documents. Try an iFlow ID or a specific error message, or import the relevant documentation.", "sources": [], "mode": "search", "notice": notice}
            try:
                answer = self.client.answer(model, question, sources, history)
                cited = {int(value) for value in re.findall(r"\[(\d+)\]", answer)}
                if not cited or any(value < 1 or value > len(sources) for value in cited):
                    notice = "The model did not provide reliable source numbering. Check the retrieved passages below."
                return {"answer": answer, "sources": sources, "mode": "generated", "notice": notice}
            except LocalModelError as exc:
                answer = "Here are matching passages from your documents:\n\n" + "\n\n".join(
                    f"[{i}] {source['name']} — {source['location']}\n{source['text'][:700]}" for i, source in enumerate(sources, 1))
                return {"answer": answer, "sources": sources, "mode": "search", "notice": str(exc)}
        finally:
            self.generation_lock.release()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, application):
        super().__init__(address, Handler)
        self.application = application


class Handler(BaseHTTPRequestHandler):
    server_version = "CPI-Local/1.0"

    @property
    def application(self):
        return self.server.application

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def log_message(self, fmt, *args):
        # No queries, document text, or uploaded file names in access logs.
        return

    def guarded(self, operation):
        try:
            port = self.server.server_port
            allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host", "") not in allowed_hosts:
                raise APIError("This application accepts only local requests.", 403)
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{host}" for host in allowed_hosts}:
                raise APIError("Cross-origin requests are blocked.", 403)
            if self.command in ("POST", "DELETE") and not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.application.csrf_token):
                raise APIError("Refresh the page before making this request.", 403)
            operation()
        except APIError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            LOG.exception("Request failed")
            self.send_json({"error": "The request failed. Check the local app console for details."}, 500)

    def send_bytes(self, content, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, value, status=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def read_json(self):
        length = self.content_length(256 * 1024)
        if self.headers.get_content_type() != "application/json":
            raise APIError("Use application/json for this request.", 415)
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise APIError("The request body must be an object.")
        return data

    def content_length(self, maximum):
        if self.headers.get("Transfer-Encoding"):
            raise APIError("Chunked uploads are not supported.")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > maximum:
            raise APIError(f"Request size must be between 1 and {maximum} bytes.", 413)
        return length

    def do_GET(self):
        self.guarded(self.get_route)

    def get_route(self):
        route = urllib.parse.urlsplit(self.path).path
        if route == "/api/status":
            return self.send_json(self.application.status())
        if route == "/api/documents":
            return self.send_json({"documents": self.application.knowledge.list_documents()})
        if re.fullmatch(r"/api/sources/\d+", route):
            chunk = self.application.knowledge.get_chunk(int(route.rsplit("/", 1)[1]))
            if not chunk:
                raise APIError("This source was removed or updated. Ask the question again to retrieve the current document.", 404)
            return self.send_json(chunk)
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
                 "/app.css": ("app.css", "text/css; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8")}
        if route in files:
            name, mime = files[route]
            return self.send_bytes((ROOT / "web" / name).read_bytes(), mime)
        raise APIError("Not found", 404)

    def do_POST(self):
        self.guarded(self.post_route)

    def post_route(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/api/upload":
            return self.upload(parts.query)
        data = self.read_json()
        if parts.path == "/api/import-folder":
            return self.send_json({"job": self.application.import_folder(data.get("path"), data.get("metadata"))}, 202)
        if parts.path == "/api/import-uploaded":
            return self.send_json({"job": self.application.import_uploaded(data.get("ids"), data.get("metadata"))}, 202)
        if parts.path == "/api/chat":
            return self.send_json(self.application.chat(data))
        if parts.path == "/api/settings":
            return self.send_json({"settings": self.application.save_settings(data)})
        if parts.path == "/api/reload-config":
            return self.send_json(self.application.reload_config())
        raise APIError("Not found", 404)

    def upload(self, query):
        name = urllib.parse.parse_qs(query).get("name", [""])[0].replace("\\", "/")
        if not name or len(name) > 1000 or name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/")):
            raise APIError("Invalid file name")
        if any(ord(char) < 32 for char in name) or Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise APIError("Unsupported file type or file name")
        size = self.content_length(256 * 1024 * 1024)
        token = secrets.token_hex(16)
        target = self.application.upload_dir / (hashlib.sha256(name.encode()).hexdigest() + Path(name).suffix.lower())
        temporary = target.with_name(token + ".partial")
        try:
            remaining = size
            with temporary.open("wb") as output:
                while remaining:
                    block = self.rfile.read(min(1024 * 1024, remaining))
                    if not block:
                        raise APIError("Upload ended before all bytes arrived")
                    output.write(block)
                    remaining -= len(block)
            with self.application.lock:
                if self.application.job and self.application.job["state"] == "running":
                    raise APIError("Wait for the current import before uploading more files.", 409)
                if any(item["path"] == target for item in self.application.staged.values()):
                    raise APIError("A file with this name is already queued. Import the queued files before uploading another version.", 409)
                temporary.replace(target)
                self.application.staged[token] = {"path": target, "name": name}
        finally:
            temporary.unlink(missing_ok=True)
        self.send_json({"id": token, "name": name}, 201)

    def do_DELETE(self):
        self.guarded(self.delete_route)

    def delete_route(self):
        route = urllib.parse.urlsplit(self.path).path
        if not re.fullmatch(r"/api/documents/\d+", route):
            raise APIError("Not found", 404)
        document_id = int(route.rsplit("/", 1)[1])
        with self.application.lock:
            if self.application.job and self.application.job["state"] == "running":
                raise APIError("Wait for the import before removing a document.", 409)
            deleted = self.application.knowledge.delete_document(document_id)
            self.application.semantic.delete_document(document_id)
        if not deleted:
            raise APIError("Document not found", 404)
        self.send_json({"deleted": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    application = Application(data_dir=args.data_dir)
    server = LocalServer(("127.0.0.1", args.port), application)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"CPI Local is ready at {url}", flush=True)
    print("Documents, search, and model requests stay on this computer. Ctrl+C stops the app.", flush=True)
    if application.settings.get("auto_import") and application.settings.get("documents_folder"):
        try:
            application.import_folder(application.settings["documents_folder"])
        except (APIError, OSError) as exc:
            application.startup_notice = str(exc)
    if args.open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
