#!/usr/bin/env python3
"""
OpenPango Decentralized Skill Marketplace — Registry Server
===========================================================

A lightweight HTTP server (stdlib http.server) that acts as the backend
for the OpenPango skill registry. No third-party dependencies required.

Endpoints
---------
POST   /skills/publish              Publish a new skill version (multipart or JSON)
GET    /skills/search?q=&cap=&tag=  Search skills
GET    /skills/<name>/download      Download the latest bundle
GET    /skills/<name>/download?version=<v>  Download a specific version
GET    /skills/<name>/versions      List all versions of a skill
GET    /skills/<name>               Get full metadata for a skill
GET    /health                      Health check

Running
-------
    python3 registry_server.py [--host 0.0.0.0] [--port 8765] [--root /path/to/data]
"""
from __future__ import annotations

import argparse
import cgi
import io
import json
import logging
import os
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Ensure the marketplace package is importable when run directly
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from models import PublishRequest, SearchResult, SkillRecord, SkillVersion
from storage import RegistryStorage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("marketplace.server")

# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _json_response(handler: "RegistryHandler", status: int, body: Any) -> None:
    """Write a JSON HTTP response."""
    payload = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(payload)


def _ok(handler: "RegistryHandler", body: Any) -> None:
    _json_response(handler, HTTPStatus.OK, body)


def _created(handler: "RegistryHandler", body: Any) -> None:
    _json_response(handler, HTTPStatus.CREATED, body)


def _error(handler: "RegistryHandler", status: int, message: str) -> None:
    _json_response(handler, status, {"error": message})


def _binary_response(handler: "RegistryHandler", data: bytes, filename: str) -> None:
    """Write a binary (zip) HTTP response."""
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(data)


# ---------------------------------------------------------------------------
# Request parsing helpers
# ---------------------------------------------------------------------------

def _parse_qs(query_string: str) -> Dict[str, str]:
    """Parse a URL query string into a flat dict (first value wins)."""
    parsed = urllib.parse.parse_qs(query_string or "")
    return {k: v[0] for k, v in parsed.items() if v}


def _read_body(handler: "RegistryHandler") -> bytes:
    """Read the full request body."""
    length = int(handler.headers.get("Content-Length", 0))
    return handler.rfile.read(length) if length > 0 else b""


def _parse_json_body(handler: "RegistryHandler") -> Tuple[Optional[dict], Optional[str]]:
    """
    Parse a JSON request body.

    Returns (data_dict, None) on success or (None, error_message) on failure.
    """
    raw = _read_body(handler)
    if not raw:
        return None, "Request body is empty"
    try:
        return json.loads(raw.decode("utf-8")), None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"Invalid JSON: {exc}"


def _parse_multipart(
    handler: "RegistryHandler",
) -> Tuple[Optional[dict], Optional[bytes], Optional[str]]:
    """
    Parse a multipart/form-data request.

    Expects:
      - 'metadata' field: JSON string with skill metadata
      - 'bundle' field: zip file bytes (optional)

    Returns (metadata_dict, bundle_bytes_or_None, error_message_or_None).
    """
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        return None, None, "Expected multipart/form-data"

    # cgi.FieldStorage needs a proper environment dict
    environ = {
        "REQUEST_METHOD": "POST",
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": handler.headers.get("Content-Length", "0"),
    }
    raw = _read_body(handler)
    form = cgi.FieldStorage(
        fp=io.BytesIO(raw),
        headers=handler.headers,
        environ=environ,
    )

    # --- metadata field ---
    if "metadata" not in form:
        return None, None, "Missing 'metadata' field in multipart form"

    try:
        metadata = json.loads(form["metadata"].value)
    except (json.JSONDecodeError, AttributeError) as exc:
        return None, None, f"Invalid metadata JSON: {exc}"

    # --- optional bundle field ---
    bundle_bytes = None
    if "bundle" in form:
        bundle_field = form["bundle"]
        bundle_bytes = bundle_field.file.read() if bundle_field.file else bundle_field.value
        if isinstance(bundle_bytes, str):
            bundle_bytes = bundle_bytes.encode("latin-1")

    return metadata, bundle_bytes, None


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class RegistryHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler for the skill marketplace registry.

    The storage attribute is injected via a factory at server construction time
    (see _make_handler_class below).
    """

    # The storage backend — set by the factory below
    storage: RegistryStorage

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        """CORS pre-flight."""
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = _parse_qs(parsed.query)
        parts = [p for p in path.split("/") if p]

        try:
            if path == "/health":
                self._handle_health()
            elif path == "/skills/search":
                self._handle_search(qs)
            elif len(parts) == 2 and parts[0] == "skills":
                # /skills/<name>
                self._handle_get_skill(parts[1])
            elif len(parts) == 3 and parts[0] == "skills" and parts[2] == "versions":
                # /skills/<name>/versions
                self._handle_list_versions(parts[1])
            elif len(parts) == 3 and parts[0] == "skills" and parts[2] == "download":
                # /skills/<name>/download[?version=X]
                self._handle_download(parts[1], qs.get("version"))
            elif len(parts) == 3 and parts[0] == "skills" and parts[2] == "dependencies":
                # /skills/<name>/dependencies[?version=X]
                self._handle_dependencies(parts[1], qs.get("version"))
            else:
                _error(self, HTTPStatus.NOT_FOUND, f"No route matched: {self.path}")
        except Exception as exc:
            logger.exception("Unhandled error in GET %s", self.path)
            _error(self, HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            if path == "/skills/publish":
                self._handle_publish()
            else:
                _error(self, HTTPStatus.NOT_FOUND, f"No route matched: {self.path}")
        except Exception as exc:
            logger.exception("Unhandled error in POST %s", self.path)
            _error(self, HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    # ------------------------------------------------------------------
    # GET handlers
    # ------------------------------------------------------------------

    def _handle_health(self) -> None:
        skills = self.storage.list_skills()
        _ok(self, {
            "status": "ok",
            "total_skills": len(skills),
            "registry_path": str(self.storage.registry_path),
        })

    def _handle_search(self, qs: Dict[str, str]) -> None:
        query = qs.get("q", "")
        capability = qs.get("cap", "")
        tag = qs.get("tag", "")

        results = self.storage.search(query=query, capability=capability, tag=tag)
        _ok(self, {
            "query": query,
            "total": len(results),
            "results": [r.to_dict() for r in results],
        })

    def _handle_get_skill(self, name: str) -> None:
        record = self.storage.get_skill(name)
        if record is None:
            _error(self, HTTPStatus.NOT_FOUND, f"Skill '{name}' not found")
            return
        _ok(self, record.to_dict())

    def _handle_list_versions(self, name: str) -> None:
        record = self.storage.get_skill(name)
        if record is None:
            _error(self, HTTPStatus.NOT_FOUND, f"Skill '{name}' not found")
            return
        versions = []
        for ver_str, sv in sorted(record.versions.items()):
            versions.append({
                "version": ver_str,
                "author": sv.author,
                "published_at": sv.published_at,
                "description": sv.description,
                "capabilities": sv.capabilities,
                "checksum": sv.checksum,
            })
        _ok(self, {
            "name": name,
            "latest_version": record.latest_version,
            "versions": versions,
        })

    def _handle_download(self, name: str, version: Optional[str]) -> None:
        record = self.storage.get_skill(name)
        if record is None:
            _error(self, HTTPStatus.NOT_FOUND, f"Skill '{name}' not found")
            return

        target_version = version or record.latest_version
        if target_version not in record.versions:
            _error(
                self,
                HTTPStatus.NOT_FOUND,
                f"Version '{target_version}' of skill '{name}' not found",
            )
            return

        bundle_path = self.storage.get_bundle_path(name, target_version)
        if bundle_path is None:
            _error(
                self,
                HTTPStatus.NOT_FOUND,
                f"Bundle for '{name}@{target_version}' not found on server",
            )
            return

        data = bundle_path.read_bytes()
        self.storage.increment_download(name)
        _binary_response(self, data, f"{name}-{target_version}.zip")
        logger.info("Downloaded %s@%s (%d bytes)", name, target_version, len(data))

    def _handle_dependencies(self, name: str, version: Optional[str]) -> None:
        try:
            deps = self.storage.resolve_dependencies(name, version)
        except ValueError as exc:
            _error(self, HTTPStatus.UNPROCESSABLE_ENTITY, str(exc))
            return
        _ok(self, {
            "name": name,
            "version": version or "latest",
            "install_order": deps,
        })

    # ------------------------------------------------------------------
    # POST handlers
    # ------------------------------------------------------------------

    def _handle_publish(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        bundle_bytes: Optional[bytes] = None

        if "multipart/form-data" in content_type:
            raw_meta, bundle_bytes, err = _parse_multipart(self)
            if err:
                _error(self, HTTPStatus.BAD_REQUEST, err)
                return
            if raw_meta is None:
                _error(self, HTTPStatus.BAD_REQUEST, "Missing metadata in multipart")
                return
        elif "application/json" in content_type:
            raw_meta, err = _parse_json_body(self)
            if err:
                _error(self, HTTPStatus.BAD_REQUEST, err)
                return
        else:
            _error(
                self,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json or multipart/form-data",
            )
            return

        # Validate request
        try:
            req = PublishRequest.from_dict(raw_meta)
            req.validate()
        except (ValueError, KeyError) as exc:
            _error(self, HTTPStatus.BAD_REQUEST, f"Validation error: {exc}")
            return

        # Check for duplicate version
        if self.storage.skill_exists(req.name, req.version):
            _error(
                self,
                HTTPStatus.CONFLICT,
                f"Skill '{req.name}@{req.version}' already exists. Bump the version.",
            )
            return

        # Store bundle if provided
        bundle_path_str: Optional[str] = None
        checksum: Optional[str] = None
        if bundle_bytes:
            if not bundle_bytes[:2] == b"PK":  # ZIP magic bytes
                _error(self, HTTPStatus.BAD_REQUEST, "Bundle must be a valid ZIP file")
                return
            bundle_path_str, checksum = self.storage.store_bundle(
                req.name, req.version, bundle_bytes
            )

        # Build SkillVersion
        new_version = SkillVersion(
            version=req.version,
            author=req.author,
            description=req.description,
            capabilities=req.capabilities,
            dependencies=req.dependencies,
            bundle_path=bundle_path_str,
            install_uri=req.install_uri or f"openpango://{req.name}@{req.version}",
            checksum=checksum,
        )

        # Upsert SkillRecord
        existing = self.storage.get_skill(req.name)
        if existing is None:
            record = SkillRecord(
                name=req.name,
                latest_version=req.version,
                tags=req.tags,
            )
        else:
            record = existing
            record.tags = req.tags or record.tags
            # Only promote latest if the new version is lexicographically newer
            # (simple semver comparison by tuple)
            if _semver_gt(req.version, record.latest_version):
                record.latest_version = req.version

        record.versions[req.version] = new_version
        self.storage.save_skill(record)

        logger.info("Published %s@%s by %s", req.name, req.version, req.author)
        _created(self, {
            "status": "published",
            "name": req.name,
            "version": req.version,
            "install_uri": new_version.install_uri,
            "checksum": checksum,
        })

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        logger.info("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args: Any) -> None:  # noqa: N802
        logger.error("%s - %s", self.address_string(), fmt % args)


# ---------------------------------------------------------------------------
# Semver comparison (no external deps)
# ---------------------------------------------------------------------------

def _semver_tuple(version: str) -> Tuple[int, int, int]:
    """Parse a semver string into a comparable tuple of ints."""
    try:
        parts = version.split(".")
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return (0, 0, 0)


def _semver_gt(a: str, b: str) -> bool:
    """Return True if semver a > semver b."""
    return _semver_tuple(a) > _semver_tuple(b)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------

def _make_handler_class(storage: RegistryStorage) -> type:
    """
    Create a RegistryHandler subclass with the storage instance injected.

    This avoids using global state while still being compatible with
    http.server's class-based handler interface.
    """

    class BoundHandler(RegistryHandler):
        pass

    BoundHandler.storage = storage
    return BoundHandler


def create_server(
    host: str = "0.0.0.0",
    port: int = 8765,
    root: Optional[str] = None,
) -> HTTPServer:
    """Create and return a configured HTTPServer (not yet started)."""
    storage = RegistryStorage(root=root)
    handler_class = _make_handler_class(storage)
    server = HTTPServer((host, port), handler_class)
    logger.info("Skill Registry listening on http://%s:%d", host, port)
    logger.info("Registry data root: %s", storage.root)
    return server


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenPango Decentralized Skill Marketplace Registry Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    parser.add_argument(
        "--root",
        default=None,
        help="Registry data root directory (default: ~/.openclaw/skill-registry)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (default: INFO)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    server = create_server(host=args.host, port=args.port, root=args.root)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down registry server")
        server.server_close()


if __name__ == "__main__":
    main()
