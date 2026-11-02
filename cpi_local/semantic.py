"""Optional local vector index. Stored on disk and queried inside SQLite."""
import contextlib
import math
import sqlite3
from pathlib import Path

try:
    import sqlite_vec
except ImportError:
    sqlite_vec = None


class SemanticIndex:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "semantic.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.available = sqlite_vec is not None
        self.error = None
        if self.available:
            try:
                with self.connection() as db:
                    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            except (sqlite3.Error, OSError, AttributeError) as exc:
                self.available = False
                self.error = str(exc)

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def configuration(self):
        if not self.available:
            return {}
        with self.connection() as db:
            return dict(db.execute("SELECT key, value FROM settings"))

    def configure(self, model, dimension, fingerprint=None):
        if isinstance(dimension, bool) or not isinstance(dimension, int) or not 1 <= dimension <= 8192:
            raise ValueError("Invalid embedding dimensions")
        fingerprint = fingerprint or model
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = dict(db.execute("SELECT key, value FROM settings"))
            if (settings.get("model") != model or settings.get("dimension") != str(dimension)
                    or settings.get("fingerprint") != fingerprint):
                db.execute("DROP TABLE IF EXISTS vectors")
                db.execute(f"CREATE VIRTUAL TABLE vectors USING vec0(chunk_id INTEGER PRIMARY KEY, document_id INTEGER, embedding FLOAT[{dimension}] distance_metric=cosine)")
                db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)", [("model", model), ("dimension", str(dimension)), ("fingerprint", fingerprint)])

    @staticmethod
    def _validate_vectors(vectors, expected_count):
        if not isinstance(vectors, (list, tuple)) or len(vectors) != expected_count or not vectors:
            raise ValueError("Embedding batch returned an unexpected number of vectors")
        first = vectors[0]
        if not isinstance(first, (list, tuple)) or not 1 <= len(first) <= 8192:
            raise ValueError("Invalid embedding dimensions")
        dimension = len(first)
        for vector in vectors:
            if not isinstance(vector, (list, tuple)) or len(vector) != dimension:
                raise ValueError("Embedding dimensions differ within the batch")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
                raise ValueError("Embedding contains invalid or non-finite values")
            if not any(vector):
                raise ValueError("Embedding has zero length and cannot use cosine similarity")
        return dimension

    def index_document(self, knowledge, document_id, client, model, progress=None):
        if not self.available:
            return 0
        fingerprint = client.model_fingerprint(model) if callable(getattr(client, "model_fingerprint", None)) else model
        # Only missing chunk IDs are embedded. Re-importing unchanged files is inexpensive.
        settings = self.configuration()
        known = set()
        if settings.get("model") == model and settings.get("fingerprint") == fingerprint:
            with self.connection() as db:
                known = {r[0] for r in db.execute("SELECT chunk_id FROM vectors WHERE document_id=?", (document_id,))}
        batch = []
        total = 0

        def write(items):
            nonlocal total
            embeddings = client.embed(model, [item["text"] for item in items])
            dimension = self._validate_vectors(embeddings, len(items))
            current = self.configuration()
            if (current.get("model") == model and current.get("fingerprint") == fingerprint
                    and current.get("dimension") != str(dimension)):
                raise ValueError("Embedding dimensions changed for the same model; rebuild its index")
            rows = []
            for item, vector in zip(items, embeddings):
                rows.append((item["id"], document_id, sqlite_vec.serialize_float32(vector)))
            self.configure(model, dimension, fingerprint=fingerprint)
            with self.connection() as db:
                db.executemany("INSERT INTO vectors(chunk_id,document_id,embedding) VALUES (?,?,?)", rows)
            total += len(items)
            if progress:
                progress(total)

        for chunk in knowledge.iter_chunks(document_id):
            if chunk["id"] not in known:
                batch.append(chunk)
            if len(batch) == 8:
                write(batch)
                batch = []
        if batch:
            write(batch)
        return total

    def delete_document(self, document_id):
        if self.configuration().get("model"):
            with self.connection() as db:
                db.execute("DELETE FROM vectors WHERE document_id=?", (document_id,))

    def search(self, vector, model, limit=10, document_id=None, fingerprint=None):
        settings = self.configuration()
        if (settings.get("model") != model or settings.get("dimension") != str(len(vector))
                or fingerprint is not None and settings.get("fingerprint") != fingerprint or limit <= 0):
            return []
        self._validate_vectors([vector], 1)
        statement = "SELECT chunk_id, distance FROM vectors WHERE embedding MATCH ? AND k = ?"
        params = [sqlite_vec.serialize_float32(vector), limit]
        if document_id is not None:
            statement += " AND document_id = ?"
            params.append(document_id)
        with self.connection() as db:
            rows = db.execute(statement, params).fetchall()
        return [{"id": row[0], "similarity": 1 - row[1]} for row in rows if row[1] <= 0.35]

    def stats(self):
        if not self.available:
            return {"available": False, "chunks": 0, "error": self.error or "Install sqlite-vec using setup.ps1"}
        settings = self.configuration()
        with self.connection() as db:
            count = db.execute("SELECT count(*) FROM vectors").fetchone()[0] if settings.get("model") else 0
        return {"available": True, "chunks": count, "model": settings.get("model")}
