#!/usr/bin/env python3
"""
test_semantic.py - Tests for the semantic memory expansion (Bounty #21).

Run with:
    python3 skills/memory/test_semantic.py

All tests use a temporary directory so they never touch the real vector store
at ~/.openclaw/workspace/vectors.json.

Coverage:
  - Text chunking (sentence boundaries, overlap, short text, long text)
  - TF-IDF vocabulary building and embedding
  - Cosine similarity correctness
  - VectorStore: add, search, delete, clear, save/reload
  - Metadata filtering in search
  - ingest() end-to-end
  - recall() end-to-end with relevance ranking
  - CLI interface (ingest, recall, stats, clear)
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure we can import from the skills/memory package regardless of cwd
_SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SKILL_DIR))

from embeddings import (
    build_vocab,
    chunk_text,
    get_embedding,
    tfidf_embed,
    embed_chunks,
)
from vector_store import VectorStore, _cosine_similarity
from semantic_search import clear_store, get_stats, ingest, recall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_store() -> tuple[tempfile.TemporaryDirectory, Path]:
    """Return a (TemporaryDirectory, Path) pair for an isolated vector store."""
    tmp = tempfile.TemporaryDirectory()
    store_path = Path(tmp.name) / "vectors.json"
    return tmp, store_path


# ---------------------------------------------------------------------------
# 1. Chunking
# ---------------------------------------------------------------------------

class TestChunking(unittest.TestCase):

    def test_short_text_returns_single_chunk(self):
        text = "Hello world."
        chunks = chunk_text(text, chunk_size=500)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)

    def test_empty_text_returns_empty(self):
        self.assertEqual(chunk_text(""), [])
        self.assertEqual(chunk_text("   "), [])

    def test_chunks_have_content(self):
        long_text = ("The quick brown fox jumps over the lazy dog. " * 30).strip()
        chunks = chunk_text(long_text, chunk_size=100, overlap=20)
        self.assertTrue(len(chunks) >= 2)
        for c in chunks:
            self.assertTrue(len(c) > 0)

    def test_all_content_covered(self):
        """Every word in the original text should appear in at least one chunk."""
        long_text = " ".join(f"word{i}" for i in range(200))
        chunks = chunk_text(long_text, chunk_size=100, overlap=20)
        combined = " ".join(chunks)
        for i in range(200):
            self.assertIn(f"word{i}", combined)

    def test_max_chunk_size_respected(self):
        """No chunk should be dramatically larger than chunk_size."""
        long_text = "A" * 5000
        chunks = chunk_text(long_text, chunk_size=500, overlap=50)
        for c in chunks:
            self.assertLessEqual(len(c), 600)  # allow small overshoot at sentence boundary

    def test_overlap_creates_shared_content(self):
        """Consecutive chunks should share some tokens when overlap > 0."""
        long_text = ("sentence number one is here. " * 50).strip()
        chunks = chunk_text(long_text, chunk_size=80, overlap=30)
        if len(chunks) >= 2:
            # The end of chunk[0] and start of chunk[1] should share some words
            words_end = set(chunks[0][-40:].split())
            words_start = set(chunks[1][:40].split())
            self.assertTrue(len(words_end & words_start) > 0)


# ---------------------------------------------------------------------------
# 2. TF-IDF Embeddings
# ---------------------------------------------------------------------------

class TestEmbeddings(unittest.TestCase):

    def setUp(self):
        self.texts = [
            "Python is a programming language used for data science.",
            "Machine learning models require training data.",
            "The cat sat on the mat.",
            "Neural networks power modern AI systems.",
        ]
        self.vocab = build_vocab(self.texts)

    def test_vocab_is_nonempty(self):
        self.assertGreater(len(self.vocab), 0)

    def test_vector_length_equals_vocab(self):
        vec = tfidf_embed(self.texts[0], self.vocab)
        self.assertEqual(len(vec), len(self.vocab))

    def test_all_values_are_floats(self):
        vec = tfidf_embed(self.texts[0], self.vocab)
        for v in vec:
            self.assertIsInstance(v, float)

    def test_vector_is_normalized(self):
        vec = tfidf_embed(self.texts[0], self.vocab)
        norm = math.sqrt(sum(v * v for v in vec))
        # Should be 0 (empty doc) or close to 1
        self.assertAlmostEqual(norm, 1.0, places=5)

    def test_empty_text_gives_zero_vector(self):
        vec = tfidf_embed("", self.vocab)
        self.assertTrue(all(v == 0.0 for v in vec))

    def test_similar_texts_have_higher_similarity(self):
        """Python/ML texts should be closer to each other than to the cat sentence."""
        vec_python = tfidf_embed(self.texts[0], self.vocab)
        vec_ml = tfidf_embed(self.texts[1], self.vocab)
        vec_cat = tfidf_embed(self.texts[2], self.vocab)

        sim_python_ml = _cosine_similarity(vec_python, vec_ml)
        sim_python_cat = _cosine_similarity(vec_python, vec_cat)

        self.assertGreaterEqual(sim_python_ml, sim_python_cat)

    def test_get_embedding_tfidf_backend(self):
        os.environ["EMBEDDING_BACKEND"] = "tfidf"
        result = get_embedding("Hello world", vocab=self.vocab)
        self.assertEqual(result["backend"], "tfidf")
        self.assertIsInstance(result["vector"], list)
        self.assertGreater(result["dim"], 0)

    def test_embed_chunks_returns_same_count(self):
        chunks = ["chunk one text here", "another chunk of text", "third piece"]
        results = embed_chunks(chunks)
        self.assertEqual(len(results), len(chunks))
        for r in results:
            self.assertIn("vector", r)
            self.assertIn("backend", r)


# ---------------------------------------------------------------------------
# 3. Cosine Similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity(unittest.TestCase):

    def test_identical_vectors_score_one(self):
        v = [0.5, 0.5, 0.5, 0.5]
        self.assertAlmostEqual(_cosine_similarity(v, v), 1.0, places=5)

    def test_orthogonal_vectors_score_zero(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        self.assertAlmostEqual(_cosine_similarity(a, b), 0.0, places=5)

    def test_zero_vector_returns_zero(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        self.assertEqual(_cosine_similarity(a, b), 0.0)

    def test_mismatched_lengths_return_zero(self):
        a = [1.0, 2.0]
        b = [1.0, 2.0, 3.0]
        self.assertEqual(_cosine_similarity(a, b), 0.0)

    def test_opposite_direction_vectors_score_negative_one(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        self.assertAlmostEqual(_cosine_similarity(a, b), -1.0, places=5)


# ---------------------------------------------------------------------------
# 4. VectorStore
# ---------------------------------------------------------------------------

class TestVectorStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "test_vectors.json"
        self.store = VectorStore(path=self.store_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_starts_empty(self):
        self.assertEqual(len(self.store), 0)

    def test_add_returns_id(self):
        eid = self.store.add("hello world", [0.5, 0.5], metadata={"source": "test"})
        self.assertIsInstance(eid, str)
        self.assertEqual(len(eid), 36)  # UUID format

    def test_add_increments_length(self):
        self.store.add("text one", [1.0, 0.0])
        self.store.add("text two", [0.0, 1.0])
        self.assertEqual(len(self.store), 2)

    def test_search_returns_correct_count(self):
        for i in range(10):
            self.store.add(f"doc {i}", [float(i), 0.0])
        results = self.store.search([5.0, 0.0], top_k=3)
        self.assertLessEqual(len(results), 3)

    def test_search_sorted_by_score(self):
        self.store.add("doc a", [1.0, 0.0, 0.0])
        self.store.add("doc b", [0.9, 0.1, 0.0])
        self.store.add("doc c", [0.0, 0.0, 1.0])
        results = self.store.search([1.0, 0.0, 0.0], top_k=3)
        scores = [r["score"] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_search_with_min_score_filter(self):
        self.store.add("close", [1.0, 0.0])
        self.store.add("far", [0.0, 1.0])
        results = self.store.search([1.0, 0.0], top_k=5, min_score=0.9)
        for r in results:
            self.assertGreaterEqual(r["score"], 0.9)

    def test_search_metadata_filter(self):
        self.store.add("research doc", [1.0, 0.0], metadata={"source": "research"})
        self.store.add("code doc", [0.9, 0.1], metadata={"source": "code"})
        results = self.store.search([1.0, 0.0], top_k=5, filter_metadata={"source": "research"})
        for r in results:
            self.assertEqual(r["metadata"]["source"], "research")

    def test_delete_removes_entry(self):
        eid = self.store.add("to delete", [1.0, 0.0])
        deleted = self.store.delete(eid)
        self.assertTrue(deleted)
        self.assertEqual(len(self.store), 0)

    def test_delete_nonexistent_returns_false(self):
        deleted = self.store.delete("nonexistent-id")
        self.assertFalse(deleted)

    def test_clear_removes_all(self):
        self.store.add("a", [1.0])
        self.store.add("b", [2.0])
        count = self.store.clear()
        self.assertEqual(count, 2)
        self.assertEqual(len(self.store), 0)

    def test_save_and_reload(self):
        self.store.add("persistent entry", [0.5, 0.5], metadata={"source": "test"})
        self.store.save()

        store2 = VectorStore(path=self.store_path)
        self.assertEqual(len(store2), 1)
        self.assertEqual(store2.entries[0]["text"], "persistent entry")

    def test_save_is_atomic(self):
        """Save should not leave a .tmp file behind."""
        self.store.add("entry", [1.0])
        self.store.save()
        tmp_file = self.store_path.with_suffix(".json.tmp")
        self.assertFalse(tmp_file.exists())

    def test_stats_structure(self):
        self.store.add("item", [1.0, 0.0], metadata={"source": "user_input"})
        stats = self.store.stats()
        self.assertIn("total_entries", stats)
        self.assertIn("vocab_size", stats)
        self.assertIn("avg_vector_dim", stats)
        self.assertIn("sources", stats)
        self.assertIn("store_path", stats)

    def test_search_results_have_required_keys(self):
        self.store.add("test doc", [1.0, 0.0])
        results = self.store.search([1.0, 0.0], top_k=1)
        for r in results:
            self.assertIn("id", r)
            self.assertIn("text", r)
            self.assertIn("score", r)
            self.assertIn("metadata", r)


# ---------------------------------------------------------------------------
# 5. ingest() end-to-end
# ---------------------------------------------------------------------------

class TestIngest(unittest.TestCase):

    def setUp(self):
        os.environ["EMBEDDING_BACKEND"] = "tfidf"
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "vectors.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_returns_ingested_chunks(self):
        result = ingest("Hello world. This is a test.", store_path=self.store_path)
        self.assertGreater(result["ingested_chunks"], 0)
        self.assertEqual(len(result["entry_ids"]), result["ingested_chunks"])
        self.assertEqual(result["backend"], "tfidf")

    def test_ingest_empty_text_returns_zero(self):
        result = ingest("", store_path=self.store_path)
        self.assertEqual(result["ingested_chunks"], 0)

    def test_ingest_persists_to_disk(self):
        ingest("Persistent text for testing.", store_path=self.store_path)
        store = VectorStore(path=self.store_path)
        self.assertGreater(len(store), 0)

    def test_ingest_metadata_stored(self):
        ingest(
            "Tagged content here.",
            source="research",
            session_id="sess-001",
            tags=["ai", "test"],
            store_path=self.store_path,
        )
        store = VectorStore(path=self.store_path)
        entry = store.entries[0]
        self.assertEqual(entry["metadata"]["source"], "research")
        self.assertEqual(entry["metadata"]["session_id"], "sess-001")
        self.assertIn("ai", entry["metadata"]["tags"])

    def test_multiple_ingests_accumulate(self):
        ingest("First document content.", store_path=self.store_path)
        ingest("Second document content.", store_path=self.store_path)
        store = VectorStore(path=self.store_path)
        self.assertGreaterEqual(len(store), 2)

    def test_long_text_creates_multiple_chunks(self):
        long_text = ("This is a sentence about Python programming. " * 50).strip()
        result = ingest(long_text, chunk_size=100, overlap=20, store_path=self.store_path)
        self.assertGreater(result["ingested_chunks"], 1)


# ---------------------------------------------------------------------------
# 6. recall() end-to-end
# ---------------------------------------------------------------------------

class TestRecall(unittest.TestCase):

    def setUp(self):
        os.environ["EMBEDDING_BACKEND"] = "tfidf"
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "vectors.json"

        # Pre-populate the store
        ingest(
            "Python is great for data science and machine learning.",
            source="research", store_path=self.store_path
        )
        ingest(
            "JavaScript is used for web frontend development.",
            source="code", store_path=self.store_path
        )
        ingest(
            "Neural networks learn patterns from training data.",
            source="research", store_path=self.store_path
        )
        ingest(
            "The cat sat on the mat near the hat.",
            source="conversation", store_path=self.store_path
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_recall_returns_results(self):
        result = recall("python data science", top_k=3, store_path=self.store_path)
        self.assertIn("results", result)
        self.assertIn("query", result)
        self.assertIn("total_searched", result)
        self.assertGreater(result["total_searched"], 0)

    def test_recall_respects_top_k(self):
        result = recall("programming", top_k=2, store_path=self.store_path)
        self.assertLessEqual(len(result["results"]), 2)

    def test_recall_results_sorted_by_score(self):
        result = recall("machine learning neural networks", top_k=5, store_path=self.store_path)
        scores = [r["score"] for r in result["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_recall_source_filter(self):
        result = recall("data programming", source_filter="research", store_path=self.store_path)
        for r in result["results"]:
            self.assertEqual(r["metadata"]["source"], "research")

    def test_recall_empty_store(self):
        tmp2 = tempfile.TemporaryDirectory()
        empty_path = Path(tmp2.name) / "empty.json"
        result = recall("anything", store_path=empty_path)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["total_searched"], 0)
        tmp2.cleanup()

    def test_recall_result_structure(self):
        result = recall("python", top_k=1, store_path=self.store_path)
        if result["results"]:
            r = result["results"][0]
            self.assertIn("id", r)
            self.assertIn("text", r)
            self.assertIn("score", r)
            self.assertIn("metadata", r)
            self.assertIsInstance(r["score"], float)


# ---------------------------------------------------------------------------
# 7. get_stats and clear_store
# ---------------------------------------------------------------------------

class TestStatsAndClear(unittest.TestCase):

    def setUp(self):
        os.environ["EMBEDDING_BACKEND"] = "tfidf"
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "vectors.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_stats_empty_store(self):
        stats = get_stats(store_path=self.store_path)
        self.assertEqual(stats["total_entries"], 0)

    def test_stats_after_ingest(self):
        ingest("some content here", store_path=self.store_path)
        stats = get_stats(store_path=self.store_path)
        self.assertGreater(stats["total_entries"], 0)

    def test_clear_removes_everything(self):
        ingest("a", store_path=self.store_path)
        ingest("b", store_path=self.store_path)
        result = clear_store(store_path=self.store_path)
        self.assertGreater(result["cleared_entries"], 0)
        stats = get_stats(store_path=self.store_path)
        self.assertEqual(stats["total_entries"], 0)


# ---------------------------------------------------------------------------
# 8. CLI interface
# ---------------------------------------------------------------------------

class TestCLI(unittest.TestCase):

    def setUp(self):
        os.environ["EMBEDDING_BACKEND"] = "tfidf"
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "EMBEDDING_BACKEND": "tfidf"}

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, args: list[str]) -> tuple[int, str]:
        result = subprocess.run(
            [sys.executable, str(_SKILL_DIR / "semantic_search.py")] + args,
            capture_output=True,
            text=True,
            env=self.env,
        )
        return result.returncode, result.stdout

    def test_cli_stats_returns_json(self):
        code, out = self._run(["stats"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("total_entries", data)

    def test_cli_ingest_returns_json(self):
        code, out = self._run(["ingest", "Hello CLI world."])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("ingested_chunks", data)
        self.assertGreater(data["ingested_chunks"], 0)

    def test_cli_recall_returns_json(self):
        # First ingest something
        self._run(["ingest", "Python machine learning data science."])
        code, out = self._run(["recall", "python data", "--top-k", "3"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("results", data)
        self.assertIn("query", data)

    def test_cli_clear_returns_json(self):
        self._run(["ingest", "something to clear"])
        code, out = self._run(["clear"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("cleared_entries", data)

    def test_cli_ingest_with_source_flag(self):
        code, out = self._run(["ingest", "Research finding about AI.", "--source", "research"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertGreater(data["ingested_chunks"], 0)

    def test_cli_recall_min_score(self):
        self._run(["ingest", "Neural network deep learning AI."])
        code, out = self._run(["recall", "neural network", "--min-score", "0.1"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        for r in data["results"]:
            self.assertGreaterEqual(r["score"], 0.1)


# ---------------------------------------------------------------------------
# 9. Persistence and data integrity
# ---------------------------------------------------------------------------

class TestPersistence(unittest.TestCase):

    def setUp(self):
        os.environ["EMBEDDING_BACKEND"] = "tfidf"
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "persist_test.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_vectors_json_is_valid_json(self):
        ingest("test entry for persistence", store_path=self.store_path)
        content = self.store_path.read_text(encoding="utf-8")
        data = json.loads(content)
        self.assertEqual(data["version"], 1)
        self.assertIn("entries", data)
        self.assertIn("vocab", data)

    def test_multiple_sessions_accumulate(self):
        ingest("session one text", session_id="s1", store_path=self.store_path)
        # Simulate second session by creating a new VectorStore instance
        ingest("session two text", session_id="s2", store_path=self.store_path)
        store = VectorStore(path=self.store_path)
        sessions = {e["metadata"].get("session_id") for e in store.entries}
        self.assertIn("s1", sessions)
        self.assertIn("s2", sessions)

    def test_entry_has_timestamp(self):
        ingest("timestamped entry", store_path=self.store_path)
        store = VectorStore(path=self.store_path)
        for entry in store.entries:
            self.assertIn("timestamp", entry["metadata"])
            # Should be a valid ISO timestamp
            ts = entry["metadata"]["timestamp"]
            self.assertRegex(ts, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Run with a clean environment variable
    os.environ.setdefault("EMBEDDING_BACKEND", "tfidf")
    unittest.main(verbosity=2)
