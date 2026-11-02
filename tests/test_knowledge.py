"""Behavioral checks for local document ingestion and retrieval."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cpi_local.knowledge import CHUNK_SIZE, DocumentError, KnowledgeBase


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.knowledge = KnowledgeBase(self.root / "index")

    def write(self, name, text, encoding="utf-8"):
        path = self.root / name
        path.write_text(text, encoding=encoding)
        return path

    def test_document_filter_and_source_citations(self):
        order = self.knowledge.ingest_file(
            self.write("orders.md", "Order processing\nIFLOW_Order_V2 sends orders to SAP.\nRetries use exponential backoff."),
            metadata={"iflow": "IFLOW_Order_V2", "environment": "PROD", "version": "2.0"},
        )
        invoice = self.knowledge.ingest_file(self.write("invoice.md", "Invoice processing sends invoices to SAP."))
        results = self.knowledge.search("How does IFLOW_Order_V2 process orders?")
        self.assertEqual(results[0]["document_id"], order["id"])
        self.assertEqual(results[0]["metadata"]["environment"], "PROD")
        self.assertIn("IFLOW_Order_V2 sends orders to SAP.", results[0]["text"])
        self.assertIn("Line 1", results[0]["location"])
        self.assertEqual(self.knowledge.get_chunk(results[0]["id"])["text"], results[0]["text"])
        restricted = self.knowledge.search("SAP", document_id=invoice["id"])
        self.assertTrue(restricted)
        self.assertTrue(all(item["document_id"] == invoice["id"] for item in restricted))
        self.assertEqual(self.knowledge.search("exponential", document_id=invoice["id"]), [])
        self.assertEqual(self.knowledge.search("unknownuniqueword"), [])

    def test_exact_punctuated_identifier_preferred(self):
        loose = self.write("generic.txt", "Integration IFLOW ORDER 42 supplies reference data.")
        exact = self.write("specific.txt", "Integration IFLOW_ORDER_42 supplies reference data.")
        self.knowledge.ingest_file(loose)
        target = self.knowledge.ingest_file(exact)
        results = self.knowledge.search("Tell me about IFLOW_ORDER_42")
        self.assertEqual(results[0]["document_id"], target["id"])

    def test_unchanged_metadata_reindex_delete_and_never_reuse_chunk_ids(self):
        path = self.write("flow.txt", "InitialUniqueMarker source mapping")
        first = self.knowledge.ingest_file(path, metadata={"environment": "DEV"})
        old_chunk = self.knowledge.search("InitialUniqueMarker")[0]["id"]
        with patch("cpi_local.knowledge._fragments", side_effect=AssertionError("unchanged files must not be parsed")):
            unchanged = self.knowledge.ingest_file(path, display_name="Renamed flow", metadata={"environment": "PROD"})
        self.assertEqual(unchanged["status"], "unchanged")
        self.assertEqual(self.knowledge.get_chunk(old_chunk)["metadata"]["environment"], "PROD")
        self.assertTrue(self.knowledge.search("PROD"))
        self.assertEqual(self.knowledge.search("DEV"), [])
        path.write_text("ReplacementUniqueMarker destination mapping", encoding="utf-8")
        replacement = self.knowledge.ingest_file(path)
        self.assertEqual(replacement["id"], first["id"])
        self.assertEqual(replacement["status"], "indexed")
        self.assertIsNone(self.knowledge.get_chunk(old_chunk))
        self.assertEqual(self.knowledge.search("InitialUniqueMarker"), [])
        new_chunk = self.knowledge.search("ReplacementUniqueMarker")[0]
        self.assertGreater(new_chunk["id"], old_chunk)
        self.assertEqual(new_chunk["metadata"], {"environment": "PROD"})
        self.assertEqual(self.knowledge.stats()["documents"], 1)
        self.assertEqual(self.knowledge.stats()["bytes"], path.stat().st_size)
        self.assertEqual(self.knowledge.list_documents()[0]["name"], "Renamed flow")
        self.assertTrue(self.knowledge.delete_document(first["id"]))
        self.assertFalse(self.knowledge.delete_document(first["id"]))
        self.assertEqual(self.knowledge.search("ReplacementUniqueMarker"), [])
        self.assertEqual(self.knowledge.stats(), {"documents": 0, "chunks": 0, "bytes": 0})
        again = self.knowledge.ingest_file(path)
        self.assertGreater(next(self.knowledge.iter_chunks(again["id"]))["id"], new_chunk["id"])

    def test_late_decode_failure_retains_old_index(self):
        path = self.write("changing.txt", "PreviouslyIndexedMarker remains available")
        original = self.knowledge.ingest_file(path)
        before = self.knowledge.stats()
        path.write_bytes(b"NewInvalidData " * 2000 + b"\xff\xfe")
        with self.assertRaisesRegex(DocumentError, "decode"):
            self.knowledge.ingest_file(path)
        self.assertEqual(self.knowledge.stats(), before)
        self.assertTrue(self.knowledge.search("PreviouslyIndexedMarker"))
        self.assertEqual(self.knowledge.search("NewInvalidData"), [])
        self.assertEqual(self.knowledge.list_documents()[0]["id"], original["id"])
        self.assertEqual(list((self.root / "index").glob(".ingest-*")), [])

    def test_binary_empty_and_unsupported_files_rejected(self):
        binary = self.root / "binary.txt"
        binary.write_bytes(b"This looks like text\x00but is binary")
        for path in (binary, self.write("empty.txt", " \n\t"), self.write("archive.zip", "unsupported")):
            with self.subTest(path=path.name), self.assertRaises(DocumentError):
                self.knowledge.ingest_file(path)
        self.assertEqual(self.knowledge.stats()["documents"], 0)

    def test_bom_encodings_preserve_unicode(self):
        for number, encoding in enumerate(("utf-8-sig", "utf-16", "utf-32")):
            with self.subTest(encoding=encoding):
                text = f"UnicodeMarker{number}: ग्राहक café π"
                result = self.knowledge.ingest_file(self.write(f"unicode{number}.txt", text, encoding=encoding))
                chunks = list(self.knowledge.iter_chunks(result["id"]))
                self.assertEqual(chunks[0]["text"], text)

    def test_long_line_is_streamed_and_chunks_are_bounded(self):
        path = self.root / "large.txt"
        with path.open("w", encoding="utf-8") as source:
            for _ in range(20000):
                source.write("singlelonglineword ")
            source.write("TailMarkerUnique")
        result = self.knowledge.ingest_file(path)
        self.assertGreater(result["chunks"], 100)
        chunks = list(self.knowledge.iter_chunks(result["id"]))
        self.assertTrue(all(0 < len(chunk["text"]) <= CHUNK_SIZE for chunk in chunks))
        self.assertTrue(all(chunk["location"] == "Line 1" for chunk in chunks))
        self.assertTrue(self.knowledge.search("TailMarkerUnique"))
        chosen = [chunks[-1]["id"], chunks[0]["id"], -1]
        self.assertEqual([item["id"] for item in self.knowledge.get_chunks(chosen)], chosen[:2])

    def test_html_excludes_scripts_and_preserves_visible_text(self):
        path = self.write("flow.html", "<html><style>HiddenStyleMarker</style><body><h1>VisibleTitle</h1><p>Route &amp; transform.</p><script>HiddenScriptMarker</script><table><tr><td>MappingField</td><td>Target</td></tr></table></body></html>")
        self.knowledge.ingest_file(path)
        results = self.knowledge.search("MappingField")
        self.assertIn("Route & transform.", results[0]["text"])
        self.assertIn("Extracted line", results[0]["location"])
        self.assertEqual(self.knowledge.search("HiddenScriptMarker"), [])
        self.assertEqual(self.knowledge.search("HiddenStyleMarker"), [])

    def test_docx_paragraphs_tables_and_corruption(self):
        path = self.root / "flow.docx"
        xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
        <w:p><w:r><w:t>CustomerIntegrationMarker</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>SourceField maps to DestinationField</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>'''
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", xml)
        self.knowledge.ingest_file(path)
        result = self.knowledge.search("DestinationField")[0]
        self.assertIn("CustomerIntegrationMarker", result["text"])
        self.assertIn("Paragraph 1", result["location"])
        self.assertIn("Paragraph 2", result["location"])
        path.write_bytes(b"not a zip archive")
        with self.assertRaises(DocumentError):
            self.knowledge.ingest_file(path)
        self.assertTrue(self.knowledge.search("DestinationField"))

    def test_docx_entity_declaration_rejected(self):
        path = self.root / "entities.docx"
        xml = '''<?xml version="1.0"?><!DOCTYPE doc [<!ENTITY content "EXPANSION">]><doc>&content;</doc>'''
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml)
        with self.assertRaisesRegex(DocumentError, "entity/DTD"):
            self.knowledge.ingest_file(path)

    def test_fts_query_syntax_is_treated_as_text(self):
        self.knowledge.ingest_file(self.write("flow.txt", "A searchable integration mapping exists."))
        for query in ('"', "***", "NEAR(", "{table}:*", "mapping OR NOT \"exists", "'; DROP TABLE chunks; --"):
            with self.subTest(query=query):
                self.assertIsInstance(self.knowledge.search(query), list)
        self.assertTrue(self.knowledge.search("searchable"))

    def test_connections_work_from_multiple_threads(self):
        paths = [self.write(f"document{number}.txt", f"ConcurrentMarker{number} integration") for number in range(6)]
        with ThreadPoolExecutor(max_workers=3) as executor:
            imported = list(executor.map(self.knowledge.ingest_file, paths))
            results = list(executor.map(self.knowledge.search, [f"ConcurrentMarker{number}" for number in range(6)]))
        self.assertEqual(self.knowledge.stats()["documents"], 6)
        self.assertEqual(len({item["id"] for item in imported}), 6)
        self.assertTrue(all(len(items) == 1 for items in results))


if __name__ == "__main__":
    unittest.main()
