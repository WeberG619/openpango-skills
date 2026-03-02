#!/usr/bin/env python3
"""
semantic_search.py - Semantic recall for the OpenClaw memory skill.

Provides two entry points:

  1. Python API (import and call directly):
       from skills.memory.semantic_search import ingest, recall

  2. CLI:
       python3 skills/memory/semantic_search.py ingest  "text" --source user_input
       python3 skills/memory/semantic_search.py recall  "query" --top-k 5
       python3 skills/memory/semantic_search.py stats
       python3 skills/memory/semantic_search.py clear

All output is JSON.

Storage: ~/.openclaw/workspace/vectors.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from embeddings import (
    build_vocab,
    chunk_text,
    embed_chunks,
    get_embedding,
    tfidf_embed,
)
from vector_store import VectorStore, STORE_PATH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rebuild_tfidf_vocab(store: VectorStore) -> list[str]:
    """
    Rebuild the shared TF-IDF vocabulary from all texts in the store.
    Called after ingesting new entries so that re-scoring is possible.
    """
    texts = [e["text"] for e in store.entries]
    if not texts:
        return []
    return build_vocab(texts)


def _re_embed_store_tfidf(store: VectorStore) -> None:
    """
    Re-embed all entries in the store using the current vocabulary.
    This is only needed for the TF-IDF backend where vectors must share
    a common vocab to be comparable.
    """
    backend = os.environ.get("EMBEDDING_BACKEND", "tfidf").lower()
    if backend != "tfidf":
        return  # External embeddings are already in a fixed space

    vocab = _rebuild_tfidf_vocab(store)
    if not vocab:
        return

    store.vocab = vocab
    for entry in store.entries:
        entry["vector"] = tfidf_embed(entry["text"], vocab)


# ---------------------------------------------------------------------------
# Public Python API
# ---------------------------------------------------------------------------

def ingest(
    text: str,
    source: str = "agent_output",
    session_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
    chunk_size: int = 500,
    overlap: int = 80,
    store_path: Optional[Path] = None,
) -> dict:
    """
    Chunk and embed text, then persist it to the vector store.

    Args:
        text: Raw text to ingest (conversation turn, code output, research note, etc.).
        source: Label for the source type, e.g. "user_input", "agent_output",
                "research", "code", "conversation".
        session_id: Optional identifier to group entries by session.
        tags: Optional list of string tags for filtering.
        chunk_size: Target characters per chunk (default 500).
        overlap: Overlap characters between consecutive chunks (default 80).
        store_path: Override the default store file path.

    Returns:
        dict with keys:
            "ingested_chunks": int   — number of chunks stored
            "entry_ids": list[str]   — IDs of the new entries
            "backend": str           — embedding backend used
    """
    text = text.strip()
    if not text:
        return {"ingested_chunks": 0, "entry_ids": [], "backend": "none"}

    store = VectorStore(path=store_path)
    backend = os.environ.get("EMBEDDING_BACKEND", "tfidf").lower()

    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
    metadata_base = {
        "source": source,
        "session_id": session_id,
        "tags": tags or [],
        "ingested_at": _now_iso(),
    }

    if backend == "tfidf":
        # Build vocab from existing entries + new chunks for a unified space
        existing_texts = [e["text"] for e in store.entries]
        all_texts = existing_texts + chunks
        vocab = build_vocab(all_texts)
        store.vocab = vocab

        entry_ids = []
        for chunk in chunks:
            vec = tfidf_embed(chunk, vocab)
            eid = store.add(chunk, vec, metadata={**metadata_base})
            entry_ids.append(eid)

        # Re-embed existing entries with the updated vocab
        for entry in store.entries:
            if entry["id"] not in entry_ids:
                entry["vector"] = tfidf_embed(entry["text"], vocab)

        used_backend = "tfidf"

    else:
        # External embedding (ollama / openai) — fixed embedding space
        embeddings = embed_chunks(chunks)
        entry_ids = []
        used_backend = "unknown"
        for chunk, emb in zip(chunks, embeddings):
            eid = store.add(chunk, emb["vector"], metadata={**metadata_base})
            entry_ids.append(eid)
            used_backend = emb["backend"]

    store.save()

    return {
        "ingested_chunks": len(entry_ids),
        "entry_ids": entry_ids,
        "backend": used_backend,
    }


def recall(
    query: str,
    top_k: int = 5,
    min_score: float = 0.0,
    source_filter: Optional[str] = None,
    session_filter: Optional[str] = None,
    store_path: Optional[Path] = None,
) -> dict:
    """
    Semantic recall: find the most relevant stored chunks for a query.

    Args:
        query: Natural language query string.
        top_k: Number of results to return (default 5).
        min_score: Minimum cosine similarity threshold (0.0–1.0).
        source_filter: Restrict results to a specific source label.
        session_filter: Restrict results to a specific session_id.
        store_path: Override the default store file path.

    Returns:
        dict with keys:
            "query": str
            "results": list of {
                "id": str,
                "text": str,
                "score": float,
                "metadata": dict
            }
            "total_searched": int
            "backend": str
    """
    store = VectorStore(path=store_path)
    backend = os.environ.get("EMBEDDING_BACKEND", "tfidf").lower()

    if len(store) == 0:
        return {
            "query": query,
            "results": [],
            "total_searched": 0,
            "backend": backend,
        }

    # Build the query vector
    if backend == "tfidf":
        vocab = store.vocab
        if not vocab:
            # No vocab yet — build from current entries
            vocab = _rebuild_tfidf_vocab(store)
        query_vec = tfidf_embed(query, vocab)
        used_backend = "tfidf"
    else:
        emb = get_embedding(query)
        query_vec = emb["vector"]
        used_backend = emb["backend"]

    # Apply metadata filters
    filter_meta: dict = {}
    if source_filter:
        filter_meta["source"] = source_filter
    if session_filter:
        filter_meta["session_id"] = session_filter

    results = store.search(
        query_vec,
        top_k=top_k,
        min_score=min_score,
        filter_metadata=filter_meta or None,
    )

    return {
        "query": query,
        "results": results,
        "total_searched": len(store),
        "backend": used_backend,
    }


def get_stats(store_path: Optional[Path] = None) -> dict:
    """Return statistics about the current vector store."""
    store = VectorStore(path=store_path)
    return store.stats()


def clear_store(store_path: Optional[Path] = None) -> dict:
    """Clear all entries from the vector store."""
    store = VectorStore(path=store_path)
    count = store.clear()
    store.save()
    return {"cleared_entries": count, "message": "Vector store cleared."}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_ingest(args) -> None:
    result = ingest(
        text=args.text,
        source=args.source,
        session_id=args.session_id,
        tags=args.tags,
        chunk_size=args.chunk_size,
        overlap=args.overlap,
    )
    print(json.dumps(result, indent=2))


def _cli_recall(args) -> None:
    result = recall(
        query=args.query,
        top_k=args.top_k,
        min_score=args.min_score,
        source_filter=args.source,
        session_filter=args.session_id,
    )
    print(json.dumps(result, indent=2))


def _cli_stats(_args) -> None:
    result = get_stats()
    print(json.dumps(result, indent=2))


def _cli_clear(_args) -> None:
    result = clear_store()
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Semantic memory — chunk, embed, and recall text via vector search.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Ingest a piece of text
  python3 semantic_search.py ingest "The quick brown fox..." --source research

  # Recall relevant chunks
  python3 semantic_search.py recall "fox animals" --top-k 3

  # Show store statistics
  python3 semantic_search.py stats

  # Clear the store
  python3 semantic_search.py clear

Environment variables:
  EMBEDDING_BACKEND   tfidf (default) | ollama | openai
  OLLAMA_HOST         http://localhost:11434
  OLLAMA_MODEL        nomic-embed-text
  OPENAI_API_KEY      <your key>
  OPENAI_EMBED_MODEL  text-embedding-3-small
        """,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- ingest ---
    p_ingest = subparsers.add_parser("ingest", help="Chunk and store text in the vector store")
    p_ingest.add_argument("text", type=str, help="Text to ingest")
    p_ingest.add_argument(
        "--source", type=str, default="agent_output",
        help="Source label (user_input, agent_output, research, code, conversation)"
    )
    p_ingest.add_argument("--session-id", type=str, default=None, dest="session_id",
                          help="Session identifier for grouping entries")
    p_ingest.add_argument("--tags", nargs="*", default=[],
                          help="Optional tags for filtering")
    p_ingest.add_argument("--chunk-size", type=int, default=500, dest="chunk_size",
                          help="Target chars per chunk (default: 500)")
    p_ingest.add_argument("--overlap", type=int, default=80,
                          help="Overlap chars between chunks (default: 80)")

    # --- recall ---
    p_recall = subparsers.add_parser("recall", help="Semantic recall: find relevant chunks for a query")
    p_recall.add_argument("query", type=str, help="Natural language query")
    p_recall.add_argument("--top-k", type=int, default=5, dest="top_k",
                          help="Number of results (default: 5)")
    p_recall.add_argument("--min-score", type=float, default=0.0, dest="min_score",
                          help="Minimum similarity score 0.0–1.0 (default: 0.0)")
    p_recall.add_argument("--source", type=str, default=None,
                          help="Filter results by source label")
    p_recall.add_argument("--session-id", type=str, default=None, dest="session_id",
                          help="Filter results by session ID")

    # --- stats ---
    subparsers.add_parser("stats", help="Show vector store statistics")

    # --- clear ---
    subparsers.add_parser("clear", help="Clear all entries from the vector store")

    args = parser.parse_args()

    dispatch = {
        "ingest": _cli_ingest,
        "recall": _cli_recall,
        "stats": _cli_stats,
        "clear": _cli_clear,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
