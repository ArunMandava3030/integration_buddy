"""Local-only vector-index checks using deterministic mock embeddings."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cpi_local.knowledge import KnowledgeBase
from cpi_local.semantic import SemanticIndex, sqlite_vec


class FakeEmbeddingClient:
    def __init__(self):
        self.fingerprint = "mock-weights-v1"
        self.calls = []
        self.fixed_vectors = None

    def model_fingerprint(self, model):
        return self.fingerprint

    def embed(self, model, texts, query=False):
        self.calls.append((model, texts))
        if self.fixed_vectors is not None:
            return self.fixed_vectors
        return [[0.0, 1.0, 0.0] if "invoice" in text else [1.0, 0.0, 0.0] for text in texts]


@unittest.skipUnless(sqlite_vec is not None, "Optional sqlite-vec is not installed")
class SemanticIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.knowledge = KnowledgeBase(self.root / "data")
        self.index = SemanticIndex(self.root / "data")
        if not self.index.available:
            self.skipTest(self.index.error)
        self.client = FakeEmbeddingClient()

    def document(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return self.knowledge.ingest_file(path)

    def test_search_filters_document_before_returning_results(self):
        dev = self.document("dev.txt", "orders integration in DEV")
        prod = self.document("prod.txt", "orders integration in PROD")
        invoice = self.document("invoice.txt", "invoice integration")
        for document in (dev, prod, invoice):
            self.index.index_document(self.knowledge, document["id"], self.client, "embed")
        all_results = self.index.search([1, 0, 0], "embed", fingerprint=self.client.fingerprint)
        self.assertEqual(len(all_results), 2)
        results = self.index.search([1, 0, 0], "embed", document_id=prod["id"], fingerprint=self.client.fingerprint)
        self.assertEqual(len(results), 1)
        self.assertEqual(self.knowledge.get_chunk(results[0]["id"])["document_id"], prod["id"])
        self.assertEqual(self.index.search([1, 0, 0], "embed", document_id=invoice["id"]), [])

    def test_unchanged_chunks_are_not_embedded_again(self):
        document = self.document("orders.txt", "orders integration")
        progress = []
        self.assertEqual(self.index.index_document(self.knowledge, document["id"], self.client, "embed", progress.append), 1)
        self.assertEqual(progress, [1])
        self.assertEqual(self.index.index_document(self.knowledge, document["id"], self.client, "embed"), 0)
        self.assertEqual(len(self.client.calls), 1)

    def test_delete_reindex_uses_new_chunk_ids_without_stale_results(self):
        document = self.document("changing.txt", "orders integration")
        self.index.index_document(self.knowledge, document["id"], self.client, "embed")
        old_id = self.index.search([1, 0, 0], "embed")[0]["id"]
        changed = self.document("changing.txt", "invoice replacement integration")
        self.assertEqual(changed["id"], document["id"])
        self.index.delete_document(document["id"])
        self.index.index_document(self.knowledge, document["id"], self.client, "embed")
        self.assertEqual(self.index.search([1, 0, 0], "embed"), [])
        new_results = self.index.search([0, 1, 0], "embed")
        self.assertEqual(len(new_results), 1)
        self.assertGreater(new_results[0]["id"], old_id)
        self.index.delete_document(document["id"])
        self.assertEqual(self.index.stats()["chunks"], 0)

    def test_query_model_dimension_and_fingerprint_mismatches_are_rejected(self):
        document = self.document("orders.txt", "orders integration")
        self.index.index_document(self.knowledge, document["id"], self.client, "embed")
        self.assertEqual(self.index.search([1, 0, 0], "another-model"), [])
        self.assertEqual(self.index.search([1, 0], "embed"), [])
        self.assertEqual(self.index.search([1, 0, 0], "embed", fingerprint="different-weights"), [])
        self.assertEqual(self.index.search([1, 0, 0], "embed", limit=0), [])
        for vector in ([float("nan"), 0, 0], [0, 0, 0], [True, 0, 0]):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                self.index.search(vector, "embed")

    def test_replaced_model_tag_rebuilds_its_vector_space(self):
        first = self.document("first.txt", "orders first integration")
        second = self.document("second.txt", "orders second integration")
        for document in (first, second):
            self.index.index_document(self.knowledge, document["id"], self.client, "embed")
        self.assertEqual(self.index.stats()["chunks"], 2)
        self.client.fingerprint = "mock-weights-v2"
        count = self.index.index_document(self.knowledge, first["id"], self.client, "embed")
        self.assertEqual(count, 1)
        self.assertEqual(self.index.stats()["chunks"], 1)
        self.assertEqual(self.index.configuration()["fingerprint"], "mock-weights-v2")
        self.assertEqual(self.index.search([1, 0, 0], "embed", fingerprint="mock-weights-v1"), [])

    def test_invalid_embedding_batches_preserve_the_existing_index(self):
        first = self.document("first.txt", "orders first integration")
        second = self.document("second.txt", "orders second integration")
        self.index.index_document(self.knowledge, first["id"], self.client, "embed")
        for vectors in ([], [[float("inf"), 0, 0]], [[True, 0, 0]], [[0, 0, 0]], [[1, 0, 0], [1, 0, 0]], [[1, 0]]):
            with self.subTest(vectors=vectors):
                self.client.fixed_vectors = vectors
                with self.assertRaises(ValueError):
                    self.index.index_document(self.knowledge, second["id"], self.client, "embed")
                self.assertEqual(self.index.stats()["chunks"], 1)
                self.assertTrue(self.index.search([1, 0, 0], "embed"))


class SemanticUnavailableTests(unittest.TestCase):
    def test_missing_extension_degrades_without_a_network_request(self):
        with tempfile.TemporaryDirectory() as directory, patch("cpi_local.semantic.sqlite_vec", None):
            index = SemanticIndex(Path(directory))
            self.assertFalse(index.stats()["available"])
            self.assertEqual(index.search([1, 0], "embed"), [])
            self.assertEqual(index.index_document(None, 1, None, "embed"), 0)


if __name__ == "__main__":
    unittest.main()
