"""
Tests for the OpenPango Skill Marketplace Registry.

Covers:
- Data models (validation, serialization)
- Storage backend (CRUD, search, bundles, dependencies)
- HTTP server endpoints (publish, search, download, versions, deps, health)
- Registry client (local cache, CLI helpers)

Run with:
    python3 -m pytest skills/marketplace/test_registry.py -v
or:
    cd skills/marketplace && python3 -m pytest test_registry.py -v
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

# Make local imports work whether run from repo root or the marketplace dir
_HERE = Path(__file__).parent
for _p in [str(_HERE), str(_HERE.parent.parent)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import (
    PublishRequest,
    SearchResult,
    SkillDependency,
    SkillRecord,
    SkillVersion,
)
from storage import RegistryStorage
from registry_client import SkillRegistryClient
from registry_server import create_server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_zip(name: str = "skill.py", content: str = "# skill") -> bytes:
    """Create a minimal in-memory zip file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    return buf.getvalue()


def _free_port() -> int:
    """Find a free TCP port."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port: int, root: str) -> threading.Thread:
    """Start the registry server in a daemon thread. Returns the thread."""
    server = create_server(host="127.0.0.1", port=port, root=root)

    def _run():
        server.serve_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Give the server a moment to bind
    time.sleep(0.15)
    return t


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestSkillDependency(unittest.TestCase):
    def test_valid_dependency(self):
        dep = SkillDependency(name="browser", version="1.0.0")
        dep.validate()  # should not raise

    def test_invalid_name_spaces(self):
        dep = SkillDependency(name="my skill", version="1.0.0")
        with self.assertRaises(ValueError):
            dep.validate()

    def test_invalid_version(self):
        dep = SkillDependency(name="browser", version="latest")
        with self.assertRaises(ValueError):
            dep.validate()

    def test_round_trip(self):
        dep = SkillDependency(name="memory", version="2.1.0")
        restored = SkillDependency.from_dict(dep.to_dict())
        self.assertEqual(dep.name, restored.name)
        self.assertEqual(dep.version, restored.version)


class TestSkillVersion(unittest.TestCase):
    def _make(self, **kwargs) -> SkillVersion:
        defaults = dict(
            version="1.0.0",
            author="TestAgent",
            description="A test skill",
            capabilities=["test/unit"],
        )
        defaults.update(kwargs)
        return SkillVersion(**defaults)

    def test_valid_version(self):
        sv = self._make()
        sv.validate()

    def test_missing_author(self):
        sv = self._make(author="")
        with self.assertRaises(ValueError):
            sv.validate()

    def test_missing_description(self):
        sv = self._make(description="")
        with self.assertRaises(ValueError):
            sv.validate()

    def test_invalid_semver(self):
        sv = self._make(version="v1.0")
        with self.assertRaises(ValueError):
            sv.validate()

    def test_round_trip(self):
        sv = self._make(dependencies=[SkillDependency("browser", "1.0.0")])
        restored = SkillVersion.from_dict(sv.to_dict())
        self.assertEqual(sv.version, restored.version)
        self.assertEqual(len(restored.dependencies), 1)
        self.assertEqual(restored.dependencies[0].name, "browser")


class TestSkillRecord(unittest.TestCase):
    def _make_record(self) -> SkillRecord:
        sv = SkillVersion(
            version="1.0.0",
            author="TestAgent",
            description="Testing record",
        )
        record = SkillRecord(name="test-skill", latest_version="1.0.0")
        record.versions["1.0.0"] = sv
        return record

    def test_latest_property(self):
        record = self._make_record()
        self.assertIsNotNone(record.latest)
        self.assertEqual(record.latest.version, "1.0.0")

    def test_validate_slug(self):
        record = self._make_record()
        record.name = "Valid Skill!"
        with self.assertRaises(ValueError):
            record.validate()

    def test_round_trip(self):
        record = self._make_record()
        restored = SkillRecord.from_dict(record.to_dict())
        self.assertEqual(record.name, restored.name)
        self.assertIn("1.0.0", restored.versions)


class TestPublishRequest(unittest.TestCase):
    def _valid_payload(self) -> dict:
        return {
            "name": "my-skill",
            "version": "1.2.3",
            "author": "weber",
            "description": "Does cool things",
            "capabilities": ["cool/thing"],
        }

    def test_valid(self):
        req = PublishRequest.from_dict(self._valid_payload())
        req.validate()

    def test_bad_name(self):
        payload = self._valid_payload()
        payload["name"] = "My Skill"
        req = PublishRequest.from_dict(payload)
        with self.assertRaises(ValueError):
            req.validate()

    def test_bad_version(self):
        payload = self._valid_payload()
        payload["version"] = "1.0"
        req = PublishRequest.from_dict(payload)
        with self.assertRaises(ValueError):
            req.validate()


# ---------------------------------------------------------------------------
# Storage tests
# ---------------------------------------------------------------------------

class TestRegistryStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = RegistryStorage(root=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_record(self, name: str = "test-skill", version: str = "1.0.0") -> SkillRecord:
        sv = SkillVersion(
            version=version,
            author="TestAgent",
            description="Test skill for unit tests",
            capabilities=["testing/unit", "data/process"],
        )
        record = SkillRecord(name=name, latest_version=version, tags=["testing"])
        record.versions[version] = sv
        return record

    def test_save_and_get(self):
        record = self._make_record()
        self.storage.save_skill(record)
        fetched = self.storage.get_skill("test-skill")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.name, "test-skill")

    def test_get_nonexistent(self):
        result = self.storage.get_skill("ghost-skill")
        self.assertIsNone(result)

    def test_skill_exists(self):
        self.storage.save_skill(self._make_record())
        self.assertTrue(self.storage.skill_exists("test-skill"))
        self.assertTrue(self.storage.skill_exists("test-skill", "1.0.0"))
        self.assertFalse(self.storage.skill_exists("test-skill", "9.9.9"))

    def test_list_skills(self):
        self.storage.save_skill(self._make_record("skill-a"))
        self.storage.save_skill(self._make_record("skill-b"))
        skills = self.storage.list_skills()
        names = [s.name for s in skills]
        self.assertIn("skill-a", names)
        self.assertIn("skill-b", names)

    def test_persistence(self):
        """Data should survive a storage reload."""
        self.storage.save_skill(self._make_record("persistent-skill"))
        reloaded = RegistryStorage(root=self.tmp.name)
        fetched = reloaded.get_skill("persistent-skill")
        self.assertIsNotNone(fetched)

    def test_search_by_query(self):
        self.storage.save_skill(self._make_record("data-cruncher"))
        results = self.storage.search(query="crunch")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "data-cruncher")

    def test_search_by_capability(self):
        self.storage.save_skill(self._make_record())
        results = self.storage.search(capability="testing/unit")
        self.assertEqual(len(results), 1)

    def test_search_no_results(self):
        results = self.storage.search(query="this-does-not-exist-42")
        self.assertEqual(results, [])

    def test_search_by_tag(self):
        self.storage.save_skill(self._make_record())
        results = self.storage.search(tag="testing")
        self.assertEqual(len(results), 1)

    def test_store_and_retrieve_bundle(self):
        bundle = _make_zip()
        path, checksum = self.storage.store_bundle("test-skill", "1.0.0", bundle)
        self.assertIsNotNone(path)
        self.assertEqual(len(checksum), 64)  # SHA-256 hex
        retrieved = self.storage.get_bundle_path("test-skill", "1.0.0")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.read_bytes(), bundle)

    def test_bundle_not_found(self):
        result = self.storage.get_bundle_path("ghost-skill", "1.0.0")
        self.assertIsNone(result)

    def test_delete_bundle(self):
        bundle = _make_zip()
        self.storage.store_bundle("test-skill", "1.0.0", bundle)
        deleted = self.storage.delete_bundle("test-skill", "1.0.0")
        self.assertTrue(deleted)
        self.assertIsNone(self.storage.get_bundle_path("test-skill", "1.0.0"))

    def test_increment_download(self):
        self.storage.save_skill(self._make_record())
        self.storage.increment_download("test-skill")
        self.storage.increment_download("test-skill")
        record = self.storage.get_skill("test-skill")
        self.assertEqual(record.download_count, 2)

    def test_dependency_resolution_no_deps(self):
        self.storage.save_skill(self._make_record())
        deps = self.storage.resolve_dependencies("test-skill")
        self.assertEqual(deps, [])

    def test_dependency_resolution_chain(self):
        """A -> B -> C should return [C, B] for A."""
        skill_c = self._make_record("skill-c")
        skill_b = self._make_record("skill-b")
        skill_b.versions["1.0.0"].dependencies = [SkillDependency("skill-c", "1.0.0")]
        skill_a = self._make_record("skill-a")
        skill_a.versions["1.0.0"].dependencies = [SkillDependency("skill-b", "1.0.0")]

        for s in [skill_c, skill_b, skill_a]:
            self.storage.save_skill(s)

        deps = self.storage.resolve_dependencies("skill-a")
        names = [d["name"] for d in deps]
        self.assertIn("skill-c", names)
        self.assertIn("skill-b", names)
        # C must come before B
        self.assertLess(names.index("skill-c"), names.index("skill-b"))

    def test_circular_dependency_raises(self):
        skill_a = self._make_record("circ-a")
        skill_b = self._make_record("circ-b")
        skill_a.versions["1.0.0"].dependencies = [SkillDependency("circ-b", "1.0.0")]
        skill_b.versions["1.0.0"].dependencies = [SkillDependency("circ-a", "1.0.0")]
        self.storage.save_skill(skill_a)
        self.storage.save_skill(skill_b)

        with self.assertRaises(ValueError, msg="Circular dependency should raise"):
            self.storage.resolve_dependencies("circ-a")

    def test_missing_dependency_raises(self):
        skill = self._make_record("needs-ghost")
        skill.versions["1.0.0"].dependencies = [SkillDependency("ghost-dep", "1.0.0")]
        self.storage.save_skill(skill)
        with self.assertRaises(ValueError):
            self.storage.resolve_dependencies("needs-ghost")


# ---------------------------------------------------------------------------
# HTTP server integration tests
# ---------------------------------------------------------------------------

class TestRegistryServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        _start_server(cls.port, cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # helpers

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_json(self, path: str, payload: dict, expect_status: Optional[int] = None) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            if expect_status is not None and exc.code != expect_status:
                self.fail(f"Expected HTTP {expect_status}, got {exc.code}: {body}")
            return body

    def _valid_publish_payload(self, name: str = "test-skill", version: str = "1.0.0") -> dict:
        return {
            "name": name,
            "version": version,
            "author": "testbot",
            "description": "Auto-generated test skill",
            "capabilities": ["testing/integration"],
            "tags": ["test"],
        }

    # --- health ---

    def test_health(self):
        result = self._get("/health")
        self.assertEqual(result["status"], "ok")

    # --- publish ---

    def test_publish_json(self):
        payload = self._valid_publish_payload("json-skill")
        result = self._post_json("/skills/publish", payload)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["name"], "json-skill")

    def test_publish_duplicate_version_conflicts(self):
        payload = self._valid_publish_payload("dup-skill", "1.0.0")
        self._post_json("/skills/publish", payload)
        # Second publish of same version should return 409
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url}/skills/publish",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
            self.fail("Expected 409 Conflict")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 409)

    def test_publish_invalid_name(self):
        payload = self._valid_publish_payload()
        payload["name"] = "Invalid Name!"
        result = self._post_json("/skills/publish", payload)
        self.assertIn("error", result)

    def test_publish_bad_content_type(self):
        req = urllib.request.Request(
            f"{self.base_url}/skills/publish",
            data=b"hello",
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5):
                self.fail("Expected error response")
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, [400, 415])

    # --- search ---

    def test_search_finds_published(self):
        self._post_json("/skills/publish", self._valid_publish_payload("searchable-skill"))
        result = self._get("/skills/search?q=searchable")
        self.assertGreaterEqual(result["total"], 1)
        names = [r["name"] for r in result["results"]]
        self.assertIn("searchable-skill", names)

    def test_search_empty_query_returns_all(self):
        result = self._get("/skills/search")
        self.assertIn("results", result)

    def test_search_no_match(self):
        result = self._get("/skills/search?q=xyzzy-nonexistent-42")
        self.assertEqual(result["total"], 0)

    # --- versions ---

    def test_list_versions(self):
        self._post_json("/skills/publish", self._valid_publish_payload("versioned-skill", "1.0.0"))
        self._post_json("/skills/publish", self._valid_publish_payload("versioned-skill", "1.1.0"))
        result = self._get("/skills/versioned-skill/versions")
        vers = [v["version"] for v in result["versions"]]
        self.assertIn("1.0.0", vers)
        self.assertIn("1.1.0", vers)

    def test_versions_not_found(self):
        try:
            self._get("/skills/ghost-skill-xyz/versions")
            self.fail("Expected 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

    # --- download ---

    def test_download_with_bundle(self):
        bundle = _make_zip("main.py", "print('hello')")
        name = "downloadable-skill"
        metadata = json.dumps(self._valid_publish_payload(name)).encode("utf-8")
        boundary = b"----testboundary"

        body = (
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="metadata"\r\n\r\n'
            + metadata + b"\r\n"
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="bundle"; filename="skill.zip"\r\n'
            b"Content-Type: application/zip\r\n\r\n"
            + bundle + b"\r\n"
            b"--" + boundary + b"--\r\n"
        )

        req = urllib.request.Request(
            f"{self.base_url}/skills/publish",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            pub_result = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(pub_result["status"], "published")

        # Download it back
        dl_url = f"{self.base_url}/skills/{name}/download"
        with urllib.request.urlopen(dl_url, timeout=5) as resp:
            downloaded = resp.read()
        self.assertEqual(downloaded, bundle)

    def test_download_no_bundle(self):
        self._post_json("/skills/publish", self._valid_publish_payload("nobundle-skill"))
        try:
            with urllib.request.urlopen(
                f"{self.base_url}/skills/nobundle-skill/download", timeout=5
            ):
                self.fail("Expected 404 — no bundle was uploaded")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

    # --- dependencies ---

    def test_dependency_endpoint_no_deps(self):
        self._post_json("/skills/publish", self._valid_publish_payload("nodep-skill"))
        result = self._get("/skills/nodep-skill/dependencies")
        self.assertEqual(result["install_order"], [])

    def test_unknown_route(self):
        try:
            self._get("/not-a-real-path")
            self.fail("Expected 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)


# ---------------------------------------------------------------------------
# Registry client tests (local cache, no server needed)
# ---------------------------------------------------------------------------

class TestRegistryClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tmp.name, "test_cache.sqlite")
        # Point to a registry URL that doesn't exist so all calls fall back
        self.client = SkillRegistryClient(
            db_path=db_path,
            registry_url="http://127.0.0.1:1",  # unreachable port
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_cache_seeded(self):
        results = self.client.search(query="Playwright")
        self.assertGreater(len(results), 0)

    def test_publish_falls_back_to_cache(self):
        result = self.client.publish(
            name="my-new-skill",
            version="1.0.0",
            author="TestBot",
            description="A test skill",
            capabilities=["web/testing"],
        )
        self.assertIn("name", result)
        self.assertEqual(result["name"], "my-new-skill")

    def test_search_local_after_publish(self):
        self.client.publish(
            name="cache-search-skill",
            version="1.0.0",
            author="TestBot",
            description="Fully local skill for caching test",
        )
        results = self.client.search(query="cache-search")
        names = [r["name"] for r in results]
        self.assertIn("cache-search-skill", names)

    def test_download_unavailable_returns_none(self):
        data = self.client.download("ghost-skill-xyz")
        self.assertIsNone(data)

    def test_list_versions_unavailable_returns_empty(self):
        versions = self.client.list_versions("ghost-skill-xyz")
        self.assertEqual(versions, [])

    def test_resolve_deps_unavailable_returns_empty(self):
        deps = self.client.resolve_dependencies("ghost-skill-xyz")
        self.assertEqual(deps, [])


# ---------------------------------------------------------------------------
# Legacy SkillRegistry compatibility (test_registry.py original contract)
# ---------------------------------------------------------------------------

class TestLegacySkillRegistry(unittest.TestCase):
    """Ensure the original SkillRegistry interface still works after the update."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tmp.name, "legacy.sqlite")
        from registry_client import SkillRegistry
        self.registry = SkillRegistry(
            db_path=db_path,
            registry_url="http://127.0.0.1:1",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_database_initialization(self):
        import sqlite3 as _sq
        db_path = os.path.join(self.tmp.name, "legacy.sqlite")
        with _sq.connect(db_path) as conn:
            cursor = conn.execute("SELECT count(*) FROM skills")
            count = cursor.fetchone()[0]
            self.assertGreater(count, 0)

    def test_publish_and_search(self):
        self.registry.publish(
            name="test-scraper",
            description="Extracts data from test sites",
            version="1.0.0",
            author="TestAgent",
            install_uri="github.com/testagent/scraper",
            capabilities=["web/testing", "data/extract"],
        )
        results = self.registry.search(query="scraper")
        self.assertGreater(len(results), 0)
        names = [r["name"] for r in results]
        self.assertIn("test-scraper", names)

    def test_search_core_skills(self):
        results = self.registry.search(query="Playwright")
        self.assertGreater(len(results), 0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
