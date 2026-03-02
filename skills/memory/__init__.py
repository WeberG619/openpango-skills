"""
skills/memory - OpenClaw Memory Skill

Provides two complementary memory systems:

1. Beads (task graph) — event-sourced, Git-backed task graph via memory_manager.py
2. Semantic search   — vector-based recall of past conversations, code, and research

Quick start:
    from skills.memory.semantic_search import ingest, recall

    # Store an agent output
    ingest("The API returned a 429 rate limit error.", source="agent_output")

    # Recall relevant context
    results = recall("rate limit errors", top_k=3)
"""
from .semantic_search import ingest, recall, get_stats, clear_store

__all__ = ["ingest", "recall", "get_stats", "clear_store"]
