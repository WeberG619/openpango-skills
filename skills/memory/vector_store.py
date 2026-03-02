#!/usr/bin/env python3
"""
vector_store.py - Local vector storage backend for semantic memory.

Stores document chunks and their embeddings as JSON at:
    ~/.openclaw/workspace/vectors.json

Uses cosine similarity for nearest-neighbor search. Pure Python stdlib —
no external dependencies required.

File format (vectors.json):
    {
        "version": 1,
        "vocab": [...],          # shared TF-IDF vocabulary (may be empty for external embeddings)
        "entries": [
            {
                "id": "uuid",
                "text": "chunk text",
                "vector": [0.1, 0.2, ...],
                "metadata": {
                    "source": "user_input" | "agent_output" | "research" | ...,
                    "session_id": "...",
                    "timestamp": "ISO-8601",
                    "tags": [...]
                }
            },
            ...
        ]
    }
"""
import json
import math
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional


STORE_PATH = Path.home() / ".openclaw" / "workspace" / "vectors.json"
STORE_VERSION = 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length vectors."""
    if len(a) != len(b):
        return 0.0

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (norm_a * norm_b)


def _load_store(path: Path) -> dict:
    """Load the vector store from disk, or return a fresh empty store."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == STORE_VERSION:
                return data
        except (json.JSONDecodeError, OSError):
            pass

    return {"version": STORE_VERSION, "vocab": [], "entries": []}


def _save_store(store: dict, path: Path) -> None:
    """Persist the vector store to disk atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# VectorStore class
# ---------------------------------------------------------------------------

class VectorStore:
    """
    Lightweight local vector store backed by a JSON file.

    Usage:
        store = VectorStore()
        store.add("Hello world", {"source": "user_input"})
        results = store.search("hello", top_k=3)
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or STORE_PATH
        self._store = _load_store(self.path)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def vocab(self) -> list[str]:
        return self._store.get("vocab", [])

    @vocab.setter
    def vocab(self, value: list[str]) -> None:
        self._store["vocab"] = value

    @property
    def entries(self) -> list[dict]:
        return self._store.get("entries", [])

    def __len__(self) -> int:
        return len(self.entries)

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def add(
        self,
        text: str,
        vector: list[float],
        metadata: Optional[dict] = None,
        entry_id: Optional[str] = None,
    ) -> str:
        """
        Add a document chunk and its embedding to the store.

        Args:
            text: The raw text chunk.
            vector: Pre-computed embedding vector (list of floats).
            metadata: Optional metadata dict (source, session_id, tags, etc.).
            entry_id: Optional explicit UUID. Generated if not provided.

        Returns:
            The entry ID string.
        """
        entry_id = entry_id or str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat()

        entry = {
            "id": entry_id,
            "text": text,
            "vector": vector,
            "metadata": {
                "timestamp": ts,
                **(metadata or {}),
            },
        }
        self._store["entries"].append(entry)
        return entry_id

    def search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        min_score: float = 0.0,
        filter_metadata: Optional[dict] = None,
    ) -> list[dict]:
        """
        Find the top_k most similar entries to a query vector.

        Args:
            query_vector: Embedding vector for the query.
            top_k: Maximum number of results to return.
            min_score: Minimum cosine similarity threshold (0.0–1.0).
            filter_metadata: Optional dict of metadata key-value pairs that
                             entries must match (exact equality).

        Returns:
            List of result dicts, sorted by descending similarity score:
                [{
                    "id": str,
                    "text": str,
                    "score": float,
                    "metadata": dict
                }, ...]
        """
        results = []

        for entry in self.entries:
            # Apply metadata filter if provided
            if filter_metadata:
                meta = entry.get("metadata", {})
                if not all(meta.get(k) == v for k, v in filter_metadata.items()):
                    continue

            score = _cosine_similarity(query_vector, entry["vector"])
            if score >= min_score:
                results.append({
                    "id": entry["id"],
                    "text": entry["text"],
                    "score": round(score, 6),
                    "metadata": entry.get("metadata", {}),
                })

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:top_k]

    def delete(self, entry_id: str) -> bool:
        """
        Remove an entry by ID.

        Returns:
            True if the entry was found and removed, False otherwise.
        """
        before = len(self._store["entries"])
        self._store["entries"] = [
            e for e in self._store["entries"] if e["id"] != entry_id
        ]
        return len(self._store["entries"]) < before

    def clear(self) -> int:
        """
        Remove all entries from the store.

        Returns:
            Number of entries removed.
        """
        count = len(self._store["entries"])
        self._store["entries"] = []
        self._store["vocab"] = []
        return count

    def save(self) -> None:
        """Persist all in-memory changes to disk."""
        _save_store(self._store, self.path)

    def reload(self) -> None:
        """Reload the store from disk, discarding unsaved in-memory changes."""
        self._store = _load_store(self.path)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return summary statistics about the store."""
        entries = self.entries
        sources: dict[str, int] = {}
        for e in entries:
            src = e.get("metadata", {}).get("source", "unknown")
            sources[src] = sources.get(src, 0) + 1

        avg_dim = 0
        if entries:
            dims = [len(e["vector"]) for e in entries if e.get("vector")]
            avg_dim = round(sum(dims) / len(dims)) if dims else 0

        return {
            "total_entries": len(entries),
            "vocab_size": len(self.vocab),
            "avg_vector_dim": avg_dim,
            "sources": sources,
            "store_path": str(self.path),
        }
