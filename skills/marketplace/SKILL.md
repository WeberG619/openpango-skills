---
name: marketplace
description: "Decentralized skill registry and marketplace for the OpenPango A2A Economy. Agents publish, discover, and download skills via a lightweight HTTP registry with versioning and dependency resolution."
version: "1.0.0"
user-invocable: true
metadata:
  capabilities:
    - protocol/skill-discovery
    - protocol/skill-publishing
    - protocol/skill-versioning
    - protocol/dependency-resolution
  author: "WeberG619"
  license: "MIT"
---

# Skill Marketplace & Registry

The core protocol layer for the OpenPango Agent-to-Agent (A2A) Economy. Agents
publish capabilities, search for skills, download bundles, and resolve transitive
dependencies — all via a self-hosted, offline-capable registry server.

## Architecture

```
skills/marketplace/
├── SKILL.md            — This file (frontmatter metadata)
├── __init__.py         — Package exports
├── models.py           — Data models: SkillRecord, SkillVersion, PublishRequest, etc.
├── storage.py          — Storage backend (JSON + flat zip archive tree)
├── registry_server.py  — Stdlib HTTP server (no external deps required)
├── registry_client.py  — Client SDK (local SQLite cache + remote sync)
└── test_registry.py    — Full test suite
```

## Running the Server

```bash
# Start with defaults (port 8765, ~/.openclaw/skill-registry)
python3 skills/marketplace/registry_server.py

# Custom bind address, port, and data root
python3 skills/marketplace/registry_server.py \
  --host 0.0.0.0 --port 9000 --root /data/registry
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/skills/publish` | Publish a new skill version (JSON or multipart with zip) |
| `GET`  | `/skills/search?q=&cap=&tag=` | Search skills by keyword, capability, or tag |
| `GET`  | `/skills/<name>` | Full metadata for a skill |
| `GET`  | `/skills/<name>/download[?version=X]` | Download zip bundle |
| `GET`  | `/skills/<name>/versions` | List all versions |
| `GET`  | `/skills/<name>/dependencies[?version=X]` | Resolved dependency install order |
| `GET`  | `/health` | Server health + stats |

## Publish a Skill (JSON)

```bash
curl -X POST http://localhost:8765/skills/publish \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-scraper",
    "version": "1.0.0",
    "author": "AgentX",
    "description": "Bypasses JS challenges for web scraping",
    "capabilities": ["web/scraping", "security/bypass"],
    "dependencies": [{"name": "browser", "version": "1.0.0"}],
    "tags": ["web", "scraping"]
  }'
```

## Publish with a Bundle (multipart)

```bash
curl -X POST http://localhost:8765/skills/publish \
  -F 'metadata={"name":"my-scraper","version":"1.0.0","author":"AgentX","description":"Scraper","capabilities":["web/scraping"]}' \
  -F 'bundle=@my-scraper.zip'
```

## Search Skills

```bash
# Keyword search
curl "http://localhost:8765/skills/search?q=scraper"

# By capability
curl "http://localhost:8765/skills/search?cap=web/scraping"
```

## Python Client Usage

```python
from skills.marketplace.registry_client import SkillRegistryClient

client = SkillRegistryClient()

# Search (remote-first, local cache fallback)
results = client.search("captcha solver")
for r in results:
    print(r["name"], r["latest_version"])

# Publish
client.publish(
    name="my-skill",
    version="1.0.0",
    author="AgentX-99",
    description="Does cool things",
    capabilities=["web/scraping"],
    dependencies=[{"name": "browser", "version": "1.0.0"}],
)

# Download bundle
zip_bytes = client.download("my-skill")

# List versions
versions = client.list_versions("my-skill")

# Resolve dependencies (install order)
deps = client.resolve_dependencies("my-skill")
```

## CLI Usage

```bash
# Search
python3 skills/marketplace/registry_client.py search "web scraper"

# Publish
python3 skills/marketplace/registry_client.py publish \
  --name my-skill --version 1.0.0 --author AgentX --desc "Does stuff"

# Download
python3 skills/marketplace/registry_client.py download my-skill --out skill.zip

# List versions
python3 skills/marketplace/registry_client.py versions my-skill

# Resolve dependencies
python3 skills/marketplace/registry_client.py deps my-skill
```

## Running Tests

```bash
# From repo root
python3 -m pytest skills/marketplace/test_registry.py -v

# Or directly
cd skills/marketplace && python3 -m pytest test_registry.py -v
```

## Storage Layout

```
~/.openclaw/skill-registry/
├── registry.json          # All skill metadata (atomic JSON writes)
└── bundles/
    └── <skill-name>/
        └── <version>.zip  # Uploaded bundles
```

## Features

- **Zero external dependencies** — runs on stdlib only (no FastAPI, SQLAlchemy, etc.)
- **Versioning** — full semver, multiple versions per skill, latest auto-promoted
- **Dependency resolution** — recursive graph traversal with circular dependency detection
- **Offline-capable client** — SQLite local cache with remote sync
- **Atomic writes** — registry.json updated via temp file + rename (no partial writes)
- **Bundle validation** — ZIP magic bytes checked on upload, SHA-256 checksum stored
- **CORS headers** — ready for browser/agent UI consumption
