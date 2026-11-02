"""Local, disk-backed document ingestion and keyword retrieval.

Documents are identified by their resolved filesystem path. An import stages all
new chunks on disk before replacing the old index in one SQLite transaction.
No document content is sent to a network service by this module.
"""

from __future__ import annotations

from collections import deque
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Iterable, Iterator
import xml.etree.ElementTree as ET
import zipfile


CHUNK_SIZE = 2400
CHUNK_OVERLAP = 300
READ_SIZE = 8192
MAX_DOCX_UNCOMPRESSED = 64 * 1024 * 1024
MAX_DOCX_XML = 32 * 1024 * 1024
MAX_PDF_BYTES = 128 * 1024 * 1024
SUPPORTED_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".xml", ".groovy", ".xsl", ".xslt", ".xsd", ".wsdl",
    ".properties", ".log", ".ini", ".conf", ".py", ".js", ".java", ".sql",
    ".iflw", ".bpmn", ".html", ".htm", ".docx", ".pdf",
})

_STOP_WORDS = frozenset("""
    a an and are as at be been but by can could describe did do does explain for
    from give had has have how i in into is it its me my of on or our please show
    tell than that the their them then there these they this to us was we were
    what when where which who why will with would you your
""".split())
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_IDENTIFIER = re.compile(r"(?<!\w)[^\W_]+(?:[_.:/-][^\W_]+)+(?!\w)", re.UNICODE)


class DocumentError(ValueError):
    """A document cannot be safely or meaningfully extracted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _metadata_json(metadata: dict) -> str:
    if not isinstance(metadata, dict):
        raise ValueError("Document metadata must be a JSON object.")
    try:
        return json.dumps(metadata, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Document metadata must contain JSON-compatible values.") from exc


def _search_terms(name: str, metadata: dict) -> str:
    return name + "\n" + "\n".join(str(value) for value in metadata.values())


def _file_signature(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def _text_reader(path: Path):
    with path.open("rb") as raw:
        prefix = raw.read(4)
        raw.seek(0)
        if prefix.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            encoding = "utf-32"
        elif prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        elif prefix.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        else:
            encoding = "utf-8"
        with io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline=None) as text:
            try:
                yield text
            except UnicodeError as exc:
                raise DocumentError(
                    "Cannot decode this document. Save it as UTF-8 or BOM-marked UTF-16 text."
                ) from exc


def _validate_text(text: str) -> None:
    if "\x00" in text:
        raise DocumentError("The document contains binary/NUL data, not supported text.")
    controls = sum(ord(char) < 32 and char not in "\t\n\r\f" for char in text)
    if controls > max(2, len(text) // 100):
        raise DocumentError("The document appears to contain binary data.")


def _plain_fragments(path: Path) -> Iterator[tuple[str, str]]:
    line_number = 1
    with _text_reader(path) as source:
        for block in iter(lambda: source.read(READ_SIZE), ""):
            _validate_text(block)
            for fragment in block.splitlines(keepends=True):
                yield f"Line {line_number}", fragment
                line_number += fragment.count("\n")


class _HTMLText(HTMLParser):
    _BREAK_TAGS = frozenset({
        "p", "div", "section", "article", "header", "footer", "li", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "pre", "table",
    })

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.hidden.append(tag)
        if not self.hidden:
            if tag in self._BREAK_TAGS:
                self.output.append("\n")
            elif tag in {"td", "th"}:
                self.output.append("\t")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in self._BREAK_TAGS:
            self.output.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.output.append(data)


def _html_fragments(path: Path) -> Iterator[tuple[str, str]]:
    parser = _HTMLText()
    line_number = 1
    with _text_reader(path) as source:
        for block in iter(lambda: source.read(READ_SIZE), ""):
            _validate_text(block)
            parser.feed(block)
            if len(parser.rawdata) > 1024 * 1024:
                raise DocumentError("HTML contains an excessively large or unfinished tag.")
            extracted = "".join(parser.output)
            parser.output.clear()
            for fragment in extracted.splitlines(keepends=True):
                yield f"Extracted line {line_number}", fragment
                line_number += fragment.count("\n")
        parser.close()
        for fragment in "".join(parser.output).splitlines(keepends=True):
            yield f"Extracted line {line_number}", fragment
            line_number += fragment.count("\n")


class _SafeXMLReader:
    """Reject XML DTD/entity declarations, including across parser read calls."""

    def __init__(self, source):
        self.source = source
        self.tail = b""
        self.total = 0

    def read(self, size=-1):
        data = self.source.read(size)
        self.total += len(data)
        if self.total > MAX_DOCX_XML:
            raise DocumentError("DOCX document XML exceeds the 32 MiB extraction limit.")
        scan = (self.tail + data).replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in scan or b"<!ENTITY" in scan:
            raise DocumentError("DOCX XML entity/DTD declarations are not supported.")
        self.tail = data[-64:]
        return data


def _docx_fragments(path: Path) -> Iterator[tuple[str, str]]:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(item.file_size for item in entries) > MAX_DOCX_UNCOMPRESSED:
                raise DocumentError("DOCX exceeds the 64 MiB uncompressed extraction limit.")
            try:
                document = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise DocumentError("DOCX is missing word/document.xml.") from exc
            if document.file_size > MAX_DOCX_XML:
                raise DocumentError("DOCX document XML exceeds the 32 MiB extraction limit.")
            with archive.open(document) as source:
                paragraph_number = 0
                for _, element in ET.iterparse(_SafeXMLReader(source), events=("end",)):
                    if element.tag != ns + "p":
                        continue
                    paragraph_number += 1
                    pieces = []
                    for node in element.iter():
                        if node.tag == ns + "t" and node.text:
                            pieces.append(node.text)
                        elif node.tag == ns + "tab":
                            pieces.append("\t")
                        elif node.tag in {ns + "br", ns + "cr"}:
                            pieces.append("\n")
                    text = "".join(pieces)
                    _validate_text(text)
                    yield f"Paragraph {paragraph_number}", text + "\n"
                    element.clear()
    except DocumentError:
        raise
    except (zipfile.BadZipFile, ET.ParseError, RuntimeError, NotImplementedError, EOFError) as exc:
        raise DocumentError(f"Cannot extract DOCX: {exc}") from exc


def _pdf_fragments(path: Path) -> Iterator[tuple[str, str]]:
    if path.stat().st_size > MAX_PDF_BYTES:
        raise DocumentError("PDF exceeds the 128 MiB per-file extraction limit; split it first.")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentError("PDF support requires the optional 'pypdf' package. Install it or export this PDF as text.") from exc
    try:
        with path.open("rb") as source:
            reader = PdfReader(source)
            if reader.is_encrypted:
                raise DocumentError("Encrypted PDFs are not supported. Export an unencrypted document first.")
            if len(reader.pages) > 10000:
                raise DocumentError("PDF exceeds the 10,000-page extraction limit.")
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if len(text) > 8 * 1024 * 1024:
                    raise DocumentError(f"PDF page {number} exceeds the text extraction limit.")
                _validate_text(text)
                yield f"Page {number}", text + "\n"
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"Cannot extract PDF: {exc}") from exc


def _fragments(path: Path) -> Iterator[tuple[str, str]]:
    extension = path.suffix.lower()
    if extension in {".html", ".htm"}:
        yield from _html_fragments(path)
    elif extension == ".docx":
        yield from _docx_fragments(path)
    elif extension == ".pdf":
        yield from _pdf_fragments(path)
    else:
        yield from _plain_fragments(path)


def _split_point(text: str) -> int:
    lower_bound = CHUNK_SIZE * 2 // 3
    for delimiter in ("\n", " ", "\t"):
        position = text.rfind(delimiter, lower_bound, CHUNK_SIZE)
        if position >= 0:
            return position + 1
    return CHUNK_SIZE


def _chunk_fragments(fragments: Iterable[tuple[str, str]]) -> Iterator[tuple[str, str]]:
    buffer = ""
    spans: deque[list] = deque()

    def location(length: int) -> str:
        remaining = length
        labels = []
        for count, label in spans:
            if remaining <= 0:
                break
            labels.append(label)
            remaining -= count
        return labels[0] if labels[0] == labels[-1] else f"{labels[0]} – {labels[-1]}"

    def discard(length: int) -> None:
        while length and spans:
            count, label = spans[0]
            if length >= count:
                length -= count
                spans.popleft()
            else:
                spans[0][0] -= length
                length = 0

    for label, fragment in fragments:
        for offset in range(0, len(fragment), READ_SIZE):
            piece = fragment[offset:offset + READ_SIZE]
            buffer += piece
            if spans and spans[-1][1] == label:
                spans[-1][0] += len(piece)
            else:
                spans.append([len(piece), label])
            while len(buffer) > CHUNK_SIZE:
                cut = _split_point(buffer)
                content = buffer[:cut].strip()
                if content:
                    yield location(cut), content
                advance = cut - CHUNK_OVERLAP
                buffer = buffer[advance:]
                discard(advance)
    if buffer.strip():
        yield location(len(buffer)), buffer.strip()


class KnowledgeBase:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.data_dir / "knowledge.sqlite3"
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"Unsupported knowledge database version: {version}")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    location TEXT NOT NULL,
                    text TEXT NOT NULL,
                    terms TEXT NOT NULL,
                    UNIQUE(document_id, sequence)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text, terms, content='chunks', content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, text, terms) VALUES (new.id, new.text, new.terms);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text, terms)
                    VALUES ('delete', old.id, old.text, old.terms);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text, terms)
                    VALUES ('delete', old.id, old.text, old.terms);
                    INSERT INTO chunks_fts(rowid, text, terms) VALUES (new.id, new.text, new.terms);
                END;
                PRAGMA user_version=1;
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    @contextmanager
    def _transaction(connection):
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _chunk_result(row) -> dict:
        return {
            "id": row["id"], "document_id": row["document_id"],
            "name": row["name"], "location": row["location"], "text": row["text"],
            "score": float(row["score"]) if "score" in row.keys() else 0.0,
            "metadata": json.loads(row["metadata"]),
        }

    def _unchanged(self, connection, row, name, metadata) -> dict:
        encoded = _metadata_json(metadata)
        if name != row["name"] or encoded != row["metadata"]:
            connection.execute(
                "UPDATE documents SET name=?, metadata=?, updated_at=? WHERE id=?",
                (name, encoded, _now(), row["id"]),
            )
            connection.execute(
                "UPDATE chunks SET terms=? WHERE document_id=?",
                (_search_terms(name, metadata), row["id"]),
            )
        count = connection.execute(
            "SELECT count(*) FROM chunks WHERE document_id=?", (row["id"],)
        ).fetchone()[0]
        return {"status": "unchanged", "id": row["id"], "chunks": count, "name": name}

    def ingest_file(self, path: Path, display_name: str | None = None,
                    metadata: dict | None = None) -> dict:
        """Import a document; supplied metadata replaces, omitted metadata retains.

        Supported text is decoded strictly. Empty, corrupt, unsupported, or
        changing files raise DocumentError without replacing existing content.
        """
        path = Path(path).expanduser().resolve()
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise DocumentError(f"Unsupported document type: {path.suffix or '(no extension)'}. Fetch/export plain text instead.")
        if not path.is_file():
            raise DocumentError(f"Document does not exist or is not a file: {path}")
        if metadata is not None:
            _metadata_json(metadata)
        source_path = os.path.normcase(str(path))
        signature = _file_signature(path)
        content_hash = _file_hash(path)
        if _file_signature(path) != signature:
            raise DocumentError("The source file changed during import. Retry after saving finishes.")
        with closing(self._connect()) as connection:
            with self._transaction(connection):
                existing = connection.execute(
                    "SELECT * FROM documents WHERE source_path=?", (source_path,)
                ).fetchone()
                name = str(display_name) if display_name is not None else (existing["name"] if existing else path.name)
                current_metadata = metadata if metadata is not None else (json.loads(existing["metadata"]) if existing else {})
                if existing and existing["content_hash"] == content_hash:
                    return self._unchanged(connection, existing, name, current_metadata)

        # Extraction writes to a separate temporary database, avoiding a long
        # main-database write lock and keeping arbitrarily large text on disk.
        with tempfile.TemporaryDirectory(prefix=".ingest-", dir=self.data_dir) as staging_directory:
            staging_path = Path(staging_directory) / "chunks.sqlite3"
            with closing(sqlite3.connect(staging_path)) as staging:
                staging.execute("PRAGMA journal_mode=OFF")
                staging.execute("PRAGMA synchronous=OFF")
                staging.execute("CREATE TABLE chunks(sequence INTEGER, location TEXT, text TEXT)")
                count = 0
                for count, (location, text) in enumerate(_chunk_fragments(_fragments(path)), 1):
                    staging.execute("INSERT INTO chunks VALUES (?, ?, ?)", (count, location, text))
                if not count:
                    raise DocumentError("No extractable text was found. Scanned PDFs require OCR before importing.")
                staging.commit()
            if _file_signature(path) != signature:
                raise DocumentError("The source file changed during import. Retry after saving finishes.")

            with closing(self._connect()) as connection:
                connection.execute("ATTACH DATABASE ? AS staged", (str(staging_path),))
                with self._transaction(connection):
                    existing = connection.execute(
                        "SELECT * FROM documents WHERE source_path=?", (source_path,)
                    ).fetchone()
                    name = str(display_name) if display_name is not None else (existing["name"] if existing else path.name)
                    current_metadata = metadata if metadata is not None else (json.loads(existing["metadata"]) if existing else {})
                    if existing and existing["content_hash"] == content_hash:
                        return self._unchanged(connection, existing, name, current_metadata)
                    encoded = _metadata_json(current_metadata)
                    if existing:
                        document_id = existing["id"]
                        connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
                        connection.execute(
                            "UPDATE documents SET name=?, size=?, content_hash=?, updated_at=?, metadata=? WHERE id=?",
                            (name, signature[0], content_hash, _now(), encoded, document_id),
                        )
                    else:
                        document_id = connection.execute(
                            "INSERT INTO documents(source_path, name, size, content_hash, updated_at, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                            (source_path, name, signature[0], content_hash, _now(), encoded),
                        ).lastrowid
                    connection.execute(
                        "INSERT INTO chunks(document_id, sequence, location, text, terms) "
                        "SELECT ?, sequence, location, text, ? FROM staged.chunks ORDER BY sequence",
                        (document_id, _search_terms(name, current_metadata)),
                    )
                return {"status": "indexed", "id": document_id, "chunks": count, "name": name}

    def search(self, query: str, limit: int = 6, document_id: int | None = None) -> list[dict]:
        """Return ranked keyword matches. Scores are relative, not probabilities."""
        if not query or limit <= 0:
            return []
        limit = min(int(limit), 100)
        tokens = list(dict.fromkeys(
            word.casefold() for word in _WORD.findall(query[:4000])
            if word.casefold() not in _STOP_WORDS
        ))[:40]
        if not tokens:
            return []
        # Every token is quoted; user input can never become an FTS operator.
        match_query = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        parameters: list = [match_query]
        restriction = ""
        if document_id is not None:
            restriction = " AND c.document_id=?"
            parameters.append(int(document_id))
        parameters.append(max(40, limit * 10))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT c.id, c.document_id, c.location, c.text, d.name, d.metadata, "
                "-bm25(chunks_fts, 1.0, 2.0) AS score "
                "FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
                "JOIN documents d ON d.id=c.document_id "
                "WHERE chunks_fts MATCH ?" + restriction +
                " ORDER BY bm25(chunks_fts, 1.0, 2.0), c.id LIMIT ?", parameters,
            ).fetchall()
        identifiers = list(dict.fromkeys(value.casefold() for value in _IDENTIFIER.findall(query[:4000])))
        results = []
        for row in rows:
            result = self._chunk_result(row)
            haystack = (result["text"] + "\n" + _search_terms(result["name"], result["metadata"])).casefold()
            present = set(_WORD.findall(haystack))
            coverage = sum(token in present for token in tokens) / len(tokens)
            exact = sum(bool(re.search(r"(?<!\w)" + re.escape(identifier) + r"(?!\w)", haystack)) for identifier in identifiers)
            result["score"] += coverage + exact * 3
            results.append(result)
        results.sort(key=lambda item: (-item["score"], item["id"]))
        return results[:limit]

    def list_documents(self) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT d.id, d.name, d.size, d.updated_at, d.metadata, count(c.id) AS chunks "
                "FROM documents d LEFT JOIN chunks c ON c.document_id=d.id "
                "GROUP BY d.id ORDER BY d.name COLLATE NOCASE, d.id"
            ).fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]

    def stats(self) -> dict:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT count(*) AS documents, coalesce(sum(size), 0) AS bytes, "
                "(SELECT count(*) FROM chunks) AS chunks FROM documents"
            ).fetchone()
            return dict(row)

    def get_chunk(self, id: int) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT c.id, c.document_id, c.location, c.text, d.name, d.metadata "
                "FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.id=?", (int(id),)
            ).fetchone()
        return self._chunk_result(row) if row else None

    def get_chunks(self, ids: list[int]) -> list[dict]:
        """Return existing chunks in the requested order, omitting missing IDs."""
        identifiers = [int(value) for value in ids]
        found = {}
        with closing(self._connect()) as connection:
            for offset in range(0, len(identifiers), 900):
                batch = identifiers[offset:offset + 900]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    "SELECT c.id, c.document_id, c.location, c.text, d.name, d.metadata "
                    "FROM chunks c JOIN documents d ON d.id=c.document_id "
                    f"WHERE c.id IN ({placeholders})", batch,
                ).fetchall()
                found.update((row["id"], self._chunk_result(row)) for row in rows)
        return [found[value] for value in identifiers if value in found]

    def iter_chunks(self, document_id: int | None = None) -> Iterator[dict]:
        """Stream chunks in document/sequence order using one private connection."""
        with closing(self._connect()) as connection:
            sql = ("SELECT c.id, c.document_id, c.location, c.text, d.name, d.metadata "
                   "FROM chunks c JOIN documents d ON d.id=c.document_id")
            parameters = ()
            if document_id is not None:
                sql += " WHERE c.document_id=?"
                parameters = (int(document_id),)
            sql += " ORDER BY c.document_id, c.sequence"
            for row in connection.execute(sql, parameters):
                yield self._chunk_result(row)

    def delete_document(self, id: int) -> bool:
        with closing(self._connect()) as connection:
            with self._transaction(connection):
                result = connection.execute("DELETE FROM documents WHERE id=?", (int(id),))
                return result.rowcount > 0
