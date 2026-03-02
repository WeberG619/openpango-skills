#!/usr/bin/env python3
"""
embeddings.py - Text chunking and embedding generation for semantic memory.

Default backend: TF-IDF bag-of-words (zero external dependencies).
Optional backends: Ollama (local LLM), OpenAI API — activated via env vars.

Environment variables:
    EMBEDDING_BACKEND   - "tfidf" (default), "ollama", or "openai"
    OLLAMA_HOST         - Ollama host URL (default: http://localhost:11434)
    OLLAMA_MODEL        - Ollama embedding model (default: nomic-embed-text)
    OPENAI_API_KEY      - OpenAI API key (enables OpenAI backend)
    OPENAI_EMBED_MODEL  - OpenAI embedding model (default: text-embedding-3-small)
"""
import os
import re
import json
import math
import urllib.request
import urllib.error
from typing import Optional
from collections import Counter


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

CHUNK_SIZE = 500      # target chars per chunk
CHUNK_OVERLAP = 80    # overlap chars between consecutive chunks


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split text into overlapping chunks of approximately chunk_size characters.

    Splits preferentially at sentence boundaries ('. ', '! ', '? ', '\n\n')
    to keep coherent spans together.

    Args:
        text: Input text to chunk.
        chunk_size: Target characters per chunk.
        overlap: Characters of overlap between consecutive chunks.

    Returns:
        List of text chunks.
    """
    text = text.strip()
    if not text:
        return []

    # If text is short enough, return as-is
    if len(text) <= chunk_size:
        return [text]

    # Split on natural sentence/paragraph boundaries first
    sentence_ends = re.compile(r'(?<=[.!?])\s+|\n\n+')
    sentences = sentence_ends.split(text)

    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        candidate = (current + " " + sentence).strip() if current else sentence

        if len(candidate) <= chunk_size:
            current = candidate
        else:
            # Flush current chunk if it has content
            if current:
                chunks.append(current)
                # Carry overlap into next chunk
                overlap_text = current[-overlap:] if len(current) > overlap else current
                current = (overlap_text + " " + sentence).strip()
            else:
                # Single sentence is larger than chunk_size — hard split
                while len(sentence) > chunk_size:
                    chunks.append(sentence[:chunk_size])
                    sentence = sentence[max(0, chunk_size - overlap):]
                current = sentence

    if current:
        chunks.append(current)

    return chunks


# ---------------------------------------------------------------------------
# TF-IDF embedding (zero-dependency default)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, split on whitespace."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return [t for t in text.split() if len(t) > 1]


# Minimal English stopwords to reduce noise without hurting zero-dep goal
_STOPWORDS = {
    "the", "a", "an", "is", "it", "in", "on", "at", "to", "of", "for",
    "and", "or", "but", "not", "with", "this", "that", "was", "are",
    "be", "as", "by", "from", "have", "has", "had", "do", "does", "did",
    "so", "if", "then", "than", "into", "about", "which", "who", "when",
    "where", "what", "how", "all", "more", "also", "can", "will", "would",
    "could", "should", "may", "might", "i", "we", "you", "he", "she",
    "they", "my", "our", "your", "its", "their",
}


def _compute_tfidf_vector(text: str, vocab: list[str]) -> list[float]:
    """
    Compute a TF-IDF-inspired vector for a single document against a fixed vocab.

    Uses term frequency normalized by document length. IDF is not computed
    globally (no corpus needed) — instead uses log(1 + count) to dampen
    high-frequency terms. This gives a reasonable similarity signal with
    zero additional state.
    """
    tokens = [t for t in _tokenize(text) if t not in _STOPWORDS]
    if not tokens:
        return [0.0] * len(vocab)

    counts = Counter(tokens)
    total = len(tokens)
    vocab_set = {w: i for i, w in enumerate(vocab)}

    vec = [0.0] * len(vocab)
    for term, count in counts.items():
        if term in vocab_set:
            tf = count / total
            # Sublinear TF scaling: log(1 + tf * total) / log(total + 1)
            vec[vocab_set[term]] = math.log(1 + tf * total) / math.log(total + 1)

    # L2 normalize
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]

    return vec


def build_vocab(texts: list[str], max_vocab: int = 2048) -> list[str]:
    """
    Build a vocabulary from a list of texts. Keeps the top max_vocab terms
    by document frequency (terms appearing in the most documents).
    """
    doc_freq: Counter = Counter()
    for text in texts:
        tokens = set(t for t in _tokenize(text) if t not in _STOPWORDS)
        doc_freq.update(tokens)

    # Pick top-N by frequency
    return [term for term, _ in doc_freq.most_common(max_vocab)]


def tfidf_embed(text: str, vocab: list[str]) -> list[float]:
    """Embed text using TF-IDF against a provided vocabulary."""
    return _compute_tfidf_vector(text, vocab)


# ---------------------------------------------------------------------------
# Ollama backend (optional)
# ---------------------------------------------------------------------------

def _ollama_embed(text: str) -> Optional[list[float]]:
    """
    Call Ollama embedding endpoint.
    Returns None if Ollama is unavailable (falls back to TF-IDF).
    """
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "nomic-embed-text")
    url = f"{host}/api/embeddings"

    payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("embedding")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        return None


# ---------------------------------------------------------------------------
# OpenAI backend (optional)
# ---------------------------------------------------------------------------

def _openai_embed(text: str) -> Optional[list[float]]:
    """
    Call OpenAI embedding endpoint.
    Returns None if the API key is missing or the call fails.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None

    model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    url = "https://api.openai.com/v1/embeddings"

    payload = json.dumps({"input": text, "model": model}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data["data"][0]["embedding"]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Public embedding API
# ---------------------------------------------------------------------------

def get_embedding(text: str, vocab: Optional[list[str]] = None) -> dict:
    """
    Generate an embedding for text using the configured backend.

    Backends (in priority order based on EMBEDDING_BACKEND env var):
      - "openai"  : OpenAI text-embedding-3-small
      - "ollama"  : Local Ollama (nomic-embed-text by default)
      - "tfidf"   : Built-in TF-IDF (default, zero deps)

    Args:
        text: Text to embed.
        vocab: Vocabulary list required for TF-IDF backend. If None and
               the TF-IDF backend is active, the vocab is built on the fly
               from the single document (limited quality but functional).

    Returns:
        dict with keys:
            "vector"  : list[float]
            "backend" : str name of backend used
            "dim"     : int vector dimension
    """
    backend = os.environ.get("EMBEDDING_BACKEND", "tfidf").lower()

    vector: Optional[list[float]] = None

    if backend == "openai":
        vector = _openai_embed(text)
        if vector is not None:
            return {"vector": vector, "backend": "openai", "dim": len(vector)}
        # Fall through to ollama or tfidf

    if backend in ("openai", "ollama"):
        vector = _ollama_embed(text)
        if vector is not None:
            return {"vector": vector, "backend": "ollama", "dim": len(vector)}
        # Fall through to tfidf

    # TF-IDF fallback (always works)
    if vocab is None:
        vocab = build_vocab([text])

    vector = tfidf_embed(text, vocab)
    return {"vector": vector, "backend": "tfidf", "dim": len(vector)}


def embed_chunks(chunks: list[str], vocab: Optional[list[str]] = None) -> list[dict]:
    """
    Embed a list of text chunks.

    For TF-IDF backend, builds a shared vocabulary from all chunks so that
    vectors are comparable. For external backends each chunk is embedded
    independently.

    Args:
        chunks: List of text strings.
        vocab: Optional pre-built vocabulary (TF-IDF only).

    Returns:
        List of embedding dicts, one per chunk.
    """
    if not chunks:
        return []

    backend = os.environ.get("EMBEDDING_BACKEND", "tfidf").lower()

    # For TF-IDF: build shared vocab across all chunks for comparable vectors
    if backend == "tfidf" and vocab is None:
        vocab = build_vocab(chunks)

    return [get_embedding(chunk, vocab=vocab) for chunk in chunks]
