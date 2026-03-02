"""
OpenPango Skill Marketplace — Registry Client
=============================================

Provides two usage modes:

1. **Local cache** (always available, offline-capable)
   - Backed by SQLite for fast local skill discovery.
   - Auto-seeded with OpenPango core skills.

2. **Remote registry** (optional, graceful fallback)
   - Connects to the registry server (see registry_server.py).
   - Syncs search results and publishes new skills upstream.

Usage
-----
::

    from skills.marketplace.registry_client import SkillRegistryClient

    client = SkillRegistryClient()

    # Search
    results = client.search("captcha solver")
    for r in results:
        print(r["name"], r["latest_version"])

    # Publish
    client.publish(
        name="my-scraper",
        version="1.0.0",
        author="AgentX",
        description="Bypasses JS challenges",
        capabilities=["web/scraping"],
    )

    # Download a bundle
    zip_bytes = client.download("my-scraper", version="1.0.0")

    # List versions
    versions = client.list_versions("my-scraper")

    # Resolve dependencies
    deps = client.resolve_dependencies("my-scraper")
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("marketplace.client")


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_REGISTRY_URL = "http://localhost:8765"
DEFAULT_DB_PATH = Path.home() / ".openclaw" / "workspace" / "registry_cache.sqlite"

_CORE_SKILLS = [
    {
        "id": "openpango/browser",
        "name": "browser",
        "description": "Playwright-based persistent browser daemon for web interaction.",
        "version": "1.0.0",
        "author": "openpango",
        "install_uri": "openpango://browser@1.0.0",
        "capabilities": ["web/browser", "web/interaction"],
    },
    {
        "id": "openpango/memory",
        "name": "memory",
        "description": "Event-sourced long-horizon task graph and memory management.",
        "version": "1.0.0",
        "author": "openpango",
        "install_uri": "openpango://memory@1.0.0",
        "capabilities": ["core/memory", "core/state"],
    },
    {
        "id": "openpango/marketplace",
        "name": "marketplace",
        "description": "Decentralized skill registry and marketplace protocol.",
        "version": "1.0.0",
        "author": "openpango",
        "install_uri": "openpango://marketplace@1.0.0",
        "capabilities": ["protocol/skill-discovery", "protocol/skill-publishing"],
    },
    {
        "id": "moth-asa/figma",
        "name": "figma",
        "description": "Figma Design-to-Code API bridge.",
        "version": "1.0.0",
        "author": "moth-asa",
        "install_uri": "openpango://figma@1.0.0",
        "capabilities": ["design/figma", "code/css"],
    },
]


# ---------------------------------------------------------------------------
# Local SQLite cache
# ---------------------------------------------------------------------------

class _LocalCache:
    """Thread-safe SQLite-backed local skill cache."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skills (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    description TEXT,
                    version     TEXT,
                    author      TEXT,
                    install_uri TEXT NOT NULL,
                    capabilities TEXT,
                    tags        TEXT,
                    last_updated TIMESTAMP
                )
            """)
            cursor = conn.execute("SELECT count(*) FROM skills")
            if cursor.fetchone()[0] == 0:
                self._seed(conn)
            conn.commit()

    def _seed(self, conn: sqlite3.Connection) -> None:
        now = datetime.utcnow().isoformat() + "Z"
        for s in _CORE_SKILLS:
            conn.execute(
                """
                INSERT OR IGNORE INTO skills
                    (id, name, description, version, author, install_uri, capabilities, tags, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    s["id"], s["name"], s["description"], s["version"],
                    s["author"], s["install_uri"],
                    json.dumps(s.get("capabilities", [])),
                    json.dumps([]),
                    now,
                ),
            )
        logger.info("Seeded local cache with %d core skills", len(_CORE_SKILLS))

    def search(self, query: str = "", capability: str = "") -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM skills WHERE 1=1"
            params: list = []
            if query:
                sql += " AND (name LIKE ? OR description LIKE ?)"
                params.extend([f"%{query}%", f"%{query}%"])
            if capability:
                sql += " AND capabilities LIKE ?"
                params.append(f"%{capability}%")
            rows = conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            r = dict(row)
            try:
                r["capabilities"] = json.loads(r.get("capabilities") or "[]")
            except json.JSONDecodeError:
                r["capabilities"] = []
            results.append(r)
        return results

    def upsert(self, skill: Dict) -> None:
        now = datetime.utcnow().isoformat() + "Z"
        skill_id = f"{skill.get('author', 'unknown')}/{skill['name']}".lower()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO skills
                    (id, name, description, version, author, install_uri, capabilities, tags, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_id,
                    skill["name"],
                    skill.get("description", ""),
                    skill.get("version", "1.0.0"),
                    skill.get("author", ""),
                    skill.get("install_uri", f"openpango://{skill['name']}"),
                    json.dumps(skill.get("capabilities", [])),
                    json.dumps(skill.get("tags", [])),
                    now,
                ),
            )
            conn.commit()
        logger.debug("Cached skill '%s' locally", skill["name"])


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 5) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.debug("Remote GET %s failed: %s", url, exc)
        return None


def _http_post_json(url: str, payload: dict, timeout: int = 10) -> Optional[dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.debug("Remote POST %s failed: %s", url, exc)
        return None


def _http_get_bytes(url: str, timeout: int = 30) -> Optional[bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as exc:
        logger.debug("Remote GET (bytes) %s failed: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------

class SkillRegistryClient:
    """
    Client for the OpenPango Decentralized Skill Registry.

    Operates in local-first mode with optional remote registry sync.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        registry_url: Optional[str] = None,
    ) -> None:
        self._cache = _LocalCache(Path(db_path) if db_path else DEFAULT_DB_PATH)
        self.registry_url = (registry_url or os.environ.get("PANGO_REGISTRY_URL", DEFAULT_REGISTRY_URL)).rstrip("/")

    # ------------------------------------------------------------------ #
    # Search                                                               #
    # ------------------------------------------------------------------ #

    def search(self, query: str = "", capability: str = "") -> List[Dict]:
        """
        Search for skills.

        Tries the remote registry first (for freshest results), falls back
        to the local SQLite cache if the server is unreachable.
        """
        remote_url = f"{self.registry_url}/skills/search?{urllib.parse.urlencode({'q': query, 'cap': capability})}"
        remote = _http_get(remote_url)

        if remote and "results" in remote:
            results = remote["results"]
            # Sync remote results into local cache
            for r in results:
                try:
                    self._cache.upsert(r)
                except Exception:
                    pass
            logger.info("Search '%s' returned %d remote results", query, len(results))
            return results

        # Fallback: local cache
        logger.info("Remote registry unreachable, using local cache for search '%s'", query)
        return self._cache.search(query=query, capability=capability)

    # ------------------------------------------------------------------ #
    # Publish                                                              #
    # ------------------------------------------------------------------ #

    def publish(
        self,
        name: str,
        version: str,
        author: str,
        description: str,
        capabilities: Optional[List[str]] = None,
        dependencies: Optional[List[Dict[str, str]]] = None,
        tags: Optional[List[str]] = None,
        install_uri: Optional[str] = None,
    ) -> Dict:
        """
        Publish a skill to the registry (remote, then cache it locally).

        Returns the server response dict, or a local-only confirmation if
        the remote is unavailable.
        """
        payload = {
            "name": name,
            "version": version,
            "author": author,
            "description": description,
            "capabilities": capabilities or [],
            "dependencies": dependencies or [],
            "tags": tags or [],
            "install_uri": install_uri or f"openpango://{name}@{version}",
        }

        # Local cache first (always succeeds)
        self._cache.upsert({**payload, "install_uri": payload["install_uri"]})

        # Attempt remote publish
        remote = _http_post_json(f"{self.registry_url}/skills/publish", payload)
        if remote:
            logger.info("Published '%s@%s' to remote registry", name, version)
            return remote

        logger.warning(
            "Remote registry unavailable — '%s@%s' cached locally only", name, version
        )
        return {
            "status": "cached_locally",
            "name": name,
            "version": version,
            "install_uri": payload["install_uri"],
        }

    # ------------------------------------------------------------------ #
    # Download                                                             #
    # ------------------------------------------------------------------ #

    def download(self, name: str, version: Optional[str] = None) -> Optional[bytes]:
        """
        Download the zip bundle for a skill.

        Returns raw bytes of the zip archive, or None if unavailable.
        """
        url = f"{self.registry_url}/skills/{urllib.parse.quote(name)}/download"
        if version:
            url += f"?version={urllib.parse.quote(version)}"
        data = _http_get_bytes(url)
        if data:
            logger.info("Downloaded '%s%s' (%d bytes)", name, f"@{version}" if version else "", len(data))
        else:
            logger.warning("Could not download '%s%s' from registry", name, f"@{version}" if version else "")
        return data

    # ------------------------------------------------------------------ #
    # Versions                                                             #
    # ------------------------------------------------------------------ #

    def list_versions(self, name: str) -> List[Dict]:
        """Return all published versions of a skill."""
        url = f"{self.registry_url}/skills/{urllib.parse.quote(name)}/versions"
        remote = _http_get(url)
        if remote and "versions" in remote:
            return remote["versions"]
        logger.warning("Could not retrieve versions for '%s'", name)
        return []

    # ------------------------------------------------------------------ #
    # Dependencies                                                         #
    # ------------------------------------------------------------------ #

    def resolve_dependencies(self, name: str, version: Optional[str] = None) -> List[Dict]:
        """
        Resolve the full dependency install order for a skill.

        Returns a list of dicts {"name": ..., "version": ...} in install order.
        """
        url = f"{self.registry_url}/skills/{urllib.parse.quote(name)}/dependencies"
        if version:
            url += f"?version={urllib.parse.quote(version)}"
        remote = _http_get(url)
        if remote and "install_order" in remote:
            return remote["install_order"]
        logger.warning("Could not resolve dependencies for '%s'", name)
        return []

    # ------------------------------------------------------------------ #
    # Metadata                                                             #
    # ------------------------------------------------------------------ #

    def get_skill(self, name: str) -> Optional[Dict]:
        """Fetch full metadata for a single skill."""
        url = f"{self.registry_url}/skills/{urllib.parse.quote(name)}"
        return _http_get(url)


# ---------------------------------------------------------------------------
# Backwards-compatible alias
# ---------------------------------------------------------------------------

# Keep the original class name for any existing imports
SkillRegistry = SkillRegistryClient


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="OpenPango Skill Marketplace CLI")
    parser.add_argument("--registry", default=None, help="Registry server URL")
    sub = parser.add_subparsers(dest="command")

    s_search = sub.add_parser("search", help="Search for skills")
    s_search.add_argument("query", help="Search term")
    s_search.add_argument("--cap", default="", help="Filter by capability")

    s_pub = sub.add_parser("publish", help="Publish a skill")
    s_pub.add_argument("--name", required=True)
    s_pub.add_argument("--version", default="1.0.0")
    s_pub.add_argument("--author", required=True)
    s_pub.add_argument("--desc", required=True)
    s_pub.add_argument("--cap", action="append", default=[], dest="capabilities")
    s_pub.add_argument("--tag", action="append", default=[], dest="tags")

    s_dl = sub.add_parser("download", help="Download a skill bundle")
    s_dl.add_argument("name")
    s_dl.add_argument("--version", default=None)
    s_dl.add_argument("--out", default=None, help="Output file path")

    s_ver = sub.add_parser("versions", help="List versions of a skill")
    s_ver.add_argument("name")

    s_dep = sub.add_parser("deps", help="Resolve dependencies for a skill")
    s_dep.add_argument("name")
    s_dep.add_argument("--version", default=None)

    args = parser.parse_args()
    client = SkillRegistryClient(registry_url=args.registry)

    if args.command == "search":
        results = client.search(query=args.query, capability=args.cap)
        print(json.dumps(results, indent=2))

    elif args.command == "publish":
        result = client.publish(
            name=args.name,
            version=args.version,
            author=args.author,
            description=args.desc,
            capabilities=args.capabilities,
            tags=args.tags,
        )
        print(json.dumps(result, indent=2))

    elif args.command == "download":
        data = client.download(args.name, args.version)
        if data is None:
            print("ERROR: download failed", file=sys.stderr)
            sys.exit(1)
        out_path = args.out or f"{args.name}.zip"
        Path(out_path).write_bytes(data)
        print(f"Saved to {out_path} ({len(data)} bytes)")

    elif args.command == "versions":
        versions = client.list_versions(args.name)
        print(json.dumps(versions, indent=2))

    elif args.command == "deps":
        deps = client.resolve_dependencies(args.name, args.version)
        print(json.dumps(deps, indent=2))

    else:
        parser.print_help()
