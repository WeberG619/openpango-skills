"""
Storage backend for the OpenPango Skill Marketplace Registry.

Uses a local JSON file as the single source of truth for skill metadata and
a flat directory tree for skill bundle archives (zip files).

Design principles:
- Zero external dependencies (stdlib only).
- Thread-safe via a file-level lock (os-level write through a tmp + rename).
- Atomic writes — the registry JSON is never partially written.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models import SkillRecord, SkillVersion, SearchResult

logger = logging.getLogger("marketplace.storage")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to path atomically using a sibling temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp_registry_")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)   # atomic on POSIX; best-effort on Windows
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# RegistryStorage
# ---------------------------------------------------------------------------

class RegistryStorage:
    """
    Persists skill records to a JSON file and bundles to a directory tree.

    Directory layout::

        <root>/
            registry.json          # all skill metadata
            bundles/
                <name>/
                    <version>.zip  # uploaded skill archives

    All public methods are thread-safe.
    """

    REGISTRY_FILENAME = "registry.json"

    def __init__(self, root: str | Path = None):
        if root is None:
            root = Path.home() / ".openclaw" / "skill-registry"
        self.root = Path(root)
        self.bundles_dir = self.root / "bundles"
        self.registry_path = self.root / self.REGISTRY_FILENAME

        self.root.mkdir(parents=True, exist_ok=True)
        self.bundles_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._data: Dict[str, dict] = {}   # raw dicts, parsed on demand
        self._load()

    # ------------------------------------------------------------------ #
    # Internal persistence                                                 #
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        """Load registry.json into memory. Creates it if absent."""
        with self._lock:
            if self.registry_path.exists():
                try:
                    raw = self.registry_path.read_text(encoding="utf-8")
                    self._data = json.loads(raw)
                    logger.info(
                        "Loaded registry from %s (%d skills)",
                        self.registry_path,
                        len(self._data),
                    )
                except (json.JSONDecodeError, OSError) as exc:
                    logger.error("Failed to load registry: %s — starting fresh", exc)
                    self._data = {}
            else:
                self._data = {}
                self._flush()

    def _flush(self) -> None:
        """Persist the in-memory registry dict to disk (caller holds lock)."""
        _atomic_write_json(self.registry_path, self._data)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get_skill(self, name: str) -> Optional[SkillRecord]:
        """Return the SkillRecord for *name*, or None if it doesn't exist."""
        with self._lock:
            raw = self._data.get(name)
            if raw is None:
                return None
            return SkillRecord.from_dict(raw)

    def list_skills(self) -> List[SkillRecord]:
        """Return all skills as SkillRecord objects."""
        with self._lock:
            return [SkillRecord.from_dict(v) for v in self._data.values()]

    def save_skill(self, record: SkillRecord) -> None:
        """Upsert a SkillRecord and persist to disk."""
        with self._lock:
            record.updated_at = datetime.utcnow().isoformat() + "Z"
            self._data[record.name] = record.to_dict()
            self._flush()
            logger.debug("Saved skill record: %s", record.name)

    def skill_exists(self, name: str, version: Optional[str] = None) -> bool:
        """Check existence of a skill (and optionally a specific version)."""
        with self._lock:
            raw = self._data.get(name)
            if raw is None:
                return False
            if version is None:
                return True
            return version in raw.get("versions", {})

    # ------------------------------------------------------------------ #
    # Bundle (zip archive) management                                      #
    # ------------------------------------------------------------------ #

    def store_bundle(self, name: str, version: str, bundle_bytes: bytes) -> Tuple[str, str]:
        """
        Save a skill bundle (zip) and return (bundle_path, sha256_checksum).

        The bundle is stored at::

            <root>/bundles/<name>/<version>.zip
        """
        skill_dir = self.bundles_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = skill_dir / f"{version}.zip"

        checksum = _sha256(bundle_bytes)
        bundle_path.write_bytes(bundle_bytes)
        logger.info("Stored bundle %s@%s (%d bytes, sha256=%s)", name, version, len(bundle_bytes), checksum[:12])
        return str(bundle_path), checksum

    def get_bundle_path(self, name: str, version: str) -> Optional[Path]:
        """Return the Path to the stored bundle, or None if not found."""
        path = self.bundles_dir / name / f"{version}.zip"
        return path if path.exists() else None

    def delete_bundle(self, name: str, version: str) -> bool:
        """Remove a bundle file. Returns True if removed, False if not found."""
        path = self.bundles_dir / name / f"{version}.zip"
        if path.exists():
            path.unlink()
            logger.info("Deleted bundle %s@%s", name, version)
            return True
        return False

    # ------------------------------------------------------------------ #
    # Search                                                               #
    # ------------------------------------------------------------------ #

    def search(self, query: str = "", capability: str = "", tag: str = "") -> List[SearchResult]:
        """
        Keyword search across name, description, capabilities, and tags.

        All filter parameters are optional; providing none returns all skills.
        Matching is case-insensitive substring.
        """
        query = query.lower().strip()
        capability = capability.lower().strip()
        tag = tag.lower().strip()

        results: List[SearchResult] = []

        with self._lock:
            records = [SkillRecord.from_dict(v) for v in self._data.values()]

        for record in records:
            latest = record.latest
            if latest is None:
                continue

            if query:
                haystack = (
                    record.name
                    + " " + latest.description
                    + " " + " ".join(latest.capabilities)
                    + " " + " ".join(record.tags)
                ).lower()
                if query not in haystack:
                    continue

            if capability:
                caps_lower = [c.lower() for c in latest.capabilities]
                if not any(capability in c for c in caps_lower):
                    continue

            if tag:
                tags_lower = [t.lower() for t in record.tags]
                if not any(tag in t for t in tags_lower):
                    continue

            results.append(SearchResult.from_skill_record(record))

        # Sort by download count descending, then alphabetically
        results.sort(key=lambda r: (-r.download_count, r.name))
        return results

    # ------------------------------------------------------------------ #
    # Dependency resolution                                                #
    # ------------------------------------------------------------------ #

    def resolve_dependencies(
        self,
        name: str,
        version: Optional[str] = None,
        _visited: Optional[set] = None,
    ) -> List[Dict[str, str]]:
        """
        Recursively resolve the dependency graph for a skill version.

        Returns a flat, ordered list of dicts {"name": ..., "version": ...}
        where order respects install sequence (leaves first).

        Raises ValueError on circular dependencies or missing skills.
        """
        if _visited is None:
            _visited = set()

        key = f"{name}@{version or 'latest'}"
        if key in _visited:
            raise ValueError(f"Circular dependency detected: {key}")
        _visited.add(key)

        record = self.get_skill(name)
        if record is None:
            raise ValueError(f"Skill '{name}' not found in registry")

        target_version = version or record.latest_version
        skill_version = record.versions.get(target_version)
        if skill_version is None:
            raise ValueError(f"Version '{target_version}' of skill '{name}' not found")

        install_order: List[Dict[str, str]] = []
        seen_names: set = set()

        for dep in skill_version.dependencies:
            sub_deps = self.resolve_dependencies(dep.name, dep.version, _visited.copy())
            for sd in sub_deps:
                if sd["name"] not in seen_names:
                    install_order.append(sd)
                    seen_names.add(sd["name"])
            if dep.name not in seen_names:
                install_order.append({"name": dep.name, "version": dep.version})
                seen_names.add(dep.name)

        return install_order

    # ------------------------------------------------------------------ #
    # Stats                                                                #
    # ------------------------------------------------------------------ #

    def increment_download(self, name: str) -> None:
        """Atomically increment the download counter for a skill."""
        with self._lock:
            raw = self._data.get(name)
            if raw is not None:
                raw["download_count"] = raw.get("download_count", 0) + 1
                self._flush()
