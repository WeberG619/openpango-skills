---
name: memory
description: "Event-sourced task memory (Beads) + semantic vector search for long-term agent recall."
version: "2.0.0"
user-invocable: true
metadata: {"openclaw":{"emoji":"🧠","skillKey":"openpango-memory"}}
---

## Cross-Skill Integration

This skill integrates with the Openpango ecosystem:
- **Orchestration**: Tracks tasks using this memory skill.
- **Self-Improvement**: Can create tasks for tracking new learnings.
- **Persistent State**: Shared workspace files at `~/.openclaw/workspace/` (AGENTS.md, SOUL.md, TOOLS.md, .learnings/).
- **Semantic Recall**: Agent outputs, user inputs, and research are embedded and stored in `~/.openclaw/workspace/vectors.json`.
- **Real-time Coordination**: OpenClaw sessions API (sessions_send, sessions_spawn) referenced in orchestration SKILL.md.

---

# Beads Memory Architecture

You are interacting with the "Beads" memory architecture. This is a robust, event-sourced memory system designed for long-horizon task management in an OpenClaw AI environment.

## The Paradigm

- **Event-Sourced (Git-backed JSONL)**: Every state change (creating a task, updating status, linking dependencies) is appended as an immutable event in a JSONL file. This allows for seamless Git synchronization and conflict resolution without database locking.
- **SQLite Read-Cache**: To perform fast relational queries (like finding unblocked tasks), the system rebuilds a local SQLite database from the JSONL event log on each operation. You don't query the JSONL file manually.

## Workflow & Guidelines

1. **Break Down Complexity**: When given a large objective, break it down into atomic tasks using `create_task`.
2. **Map Dependencies**: Use `link_dependency` to model the graph. If Task A cannot be started before Task B, link them (`task_id`=A, `depends_on_id`=B).
3. **Find Work**: Always use `get_ready_tasks` to identify the next actionable items. A task is only "ready" if its status is `todo` and all of its dependencies are marked as `done`.
4. **Inspect State**: Use `list_tasks` to see everything, or `get_task {id}` for a single task's full details including what it depends on and what it blocks.
5. **Update State**: As work progresses, accurately reflect reality using `update_status`.
   - `in_progress`: When a sub-agent is actively working on it.
   - `blocked`: When an external factor prevents progress.
   - `done`: When work is complete and verified. (This will automatically unblock dependent tasks!).

## Example Workflow

All commands return JSON for easy parsing.

```bash
# 1. Create the foundational task
python3 skills/memory/memory_manager.py create_task "Setup DB" "Install and configure PostgreSQL"
# Output: {"task_id": "abc-123...", "status": "todo", "message": "Task created successfully."}

# 2. Create the dependent task
python3 skills/memory/memory_manager.py create_task "Build API" "Create REST endpoints"
# Output: {"task_id": "def-456...", "status": "todo", "message": "Task created successfully."}

# 3. Link them: API depends on DB (use the task_ids from above)
python3 skills/memory/memory_manager.py link_dependency def-456... abc-123...

# 4. Check ready tasks (Only "Setup DB" will appear — "Build API" is blocked)
python3 skills/memory/memory_manager.py get_ready_tasks

# 5. Finish DB task
python3 skills/memory/memory_manager.py update_status abc-123... done

# 6. Check ready tasks again ("Build API" is now unblocked and ready!)
python3 skills/memory/memory_manager.py get_ready_tasks

# 7. See the full picture
python3 skills/memory/memory_manager.py list_tasks
```

---

# Semantic Memory (Vector Search)

The semantic layer enables **long-term recall** of past conversations, agent outputs, research notes, and code using vector similarity search. It works with zero external dependencies.

## Architecture

| Component | File | Role |
|-----------|------|------|
| Chunker + Embedder | `embeddings.py` | Split text into ~500-char chunks, embed with TF-IDF (default) or external API |
| Vector Store | `vector_store.py` | JSON-backed storage with cosine similarity search |
| Semantic API | `semantic_search.py` | `ingest()` and `recall()` functions + CLI |

**Storage:** `~/.openclaw/workspace/vectors.json`

## Embedding Backends

| Backend | Activation | Quality | External Dep? |
|---------|-----------|---------|---------------|
| TF-IDF (default) | No config needed | Good for keyword overlap | None — pure stdlib |
| Ollama | `EMBEDDING_BACKEND=ollama` | Excellent | Local Ollama server |
| OpenAI | `EMBEDDING_BACKEND=openai` + `OPENAI_API_KEY` | Best | OpenAI API call |

The system gracefully falls back: `openai -> ollama -> tfidf`.

## Python API

```python
from skills.memory.semantic_search import ingest, recall

# Ingest any text (chunked automatically at ~500 chars with 80-char overlap)
ingest(
    "We discovered that using async generators reduces memory usage by 40%.",
    source="agent_output",       # label: user_input | agent_output | research | code | conversation
    session_id="session-abc",    # optional — for grouping or filtering
    tags=["performance", "async"]
)

# Recall the top-5 most relevant chunks for a query
results = recall(
    "memory optimization techniques",
    top_k=5,
    min_score=0.1,          # optional cosine similarity threshold
    source_filter="research" # optional — restrict to a source type
)

for r in results["results"]:
    print(f"[{r['score']:.3f}] {r['text'][:120]}")
    print(f"  source={r['metadata']['source']}  ts={r['metadata']['timestamp']}")
```

## CLI

```bash
# Ingest text
python3 skills/memory/semantic_search.py ingest "Agent found rate limit on API." --source agent_output

# Recall
python3 skills/memory/semantic_search.py recall "rate limit" --top-k 5

# Filter by source
python3 skills/memory/semantic_search.py recall "async generators" --source research --min-score 0.2

# Statistics
python3 skills/memory/semantic_search.py stats

# Clear all vectors
python3 skills/memory/semantic_search.py clear
```

## When to Use Semantic Recall

- Before starting a task: `recall("task description")` — surface prior knowledge
- After completing work: `ingest(output, source="agent_output")` — persist findings
- At conversation start: `ingest(user_message, source="user_input")` — build context
- Research phase: `ingest(findings, source="research", tags=["domain"])` — index learnings

## Running Tests

```bash
python3 skills/memory/test_semantic.py
# Runs 57 tests covering chunking, embeddings, vector store, ingest, recall, CLI
```
