"""
Data models for the OpenPango Skill Marketplace.

Defines the core data structures used across the registry server,
client, and storage backend.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional, Dict, Any


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$')
_VERSION_RE = re.compile(r'^\d+\.\d+\.\d+$')


def _validate_slug(value: str, label: str = "name") -> str:
    """Ensure value is a lowercase slug (letters, digits, hyphens)."""
    if not value or not isinstance(value, str):
        raise ValueError(f"{label} must be a non-empty string")
    value = value.strip().lower()
    if not _SLUG_RE.match(value):
        raise ValueError(
            f"{label} '{value}' must be lowercase letters, digits, and hyphens "
            "(no spaces or special characters, cannot start/end with a hyphen)"
        )
    return value


def _validate_version(version: str) -> str:
    """Ensure version follows semver MAJOR.MINOR.PATCH format."""
    if not version or not isinstance(version, str):
        raise ValueError("version must be a non-empty string")
    version = version.strip()
    if not _VERSION_RE.match(version):
        raise ValueError(f"version '{version}' must follow semver format X.Y.Z (e.g. 1.0.0)")
    return version


# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------

@dataclass
class SkillDependency:
    """A declared dependency on another skill."""
    name: str          # slug of the required skill
    version: str       # minimum version required (semver)

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillDependency":
        return cls(name=data["name"], version=data["version"])

    def validate(self) -> None:
        self.name = _validate_slug(self.name, "dependency name")
        self.version = _validate_version(self.version)


@dataclass
class SkillVersion:
    """
    A single published version of a skill.

    The bundle_path is a filesystem path to the stored .zip archive on the
    registry server. install_uri is what consumers see (an opaque reference).
    """
    version: str
    author: str
    description: str
    capabilities: List[str] = field(default_factory=list)
    dependencies: List[SkillDependency] = field(default_factory=list)
    bundle_path: Optional[str] = None       # server-side path to .zip
    install_uri: Optional[str] = None       # public install reference
    published_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    checksum: Optional[str] = None          # SHA-256 hex digest of bundle

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["dependencies"] = [dep.to_dict() for dep in self.dependencies]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillVersion":
        deps = [SkillDependency.from_dict(d) for d in data.get("dependencies", [])]
        return cls(
            version=data["version"],
            author=data["author"],
            description=data["description"],
            capabilities=data.get("capabilities", []),
            dependencies=deps,
            bundle_path=data.get("bundle_path"),
            install_uri=data.get("install_uri"),
            published_at=data.get("published_at", datetime.utcnow().isoformat() + "Z"),
            checksum=data.get("checksum"),
        )

    def validate(self) -> None:
        self.version = _validate_version(self.version)
        if not self.author or not isinstance(self.author, str):
            raise ValueError("author must be a non-empty string")
        if not self.description or not isinstance(self.description, str):
            raise ValueError("description must be a non-empty string")
        for dep in self.dependencies:
            dep.validate()


@dataclass
class SkillRecord:
    """
    The canonical record for a skill in the registry.

    A skill is identified by its slug (name). Multiple SkillVersion entries
    may be stored under a single record — one per published version.
    """
    name: str                              # unique slug, e.g. "captcha-solver"
    latest_version: str                    # semver string of the current latest
    versions: Dict[str, SkillVersion] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    download_count: int = 0

    # ------------------------------------------------------------------ #
    # Convenience properties                                               #
    # ------------------------------------------------------------------ #

    @property
    def latest(self) -> Optional[SkillVersion]:
        """Return the SkillVersion object for the current latest version."""
        return self.versions.get(self.latest_version)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "latest_version": self.latest_version,
            "versions": {v: sv.to_dict() for v, sv in self.versions.items()},
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "download_count": self.download_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillRecord":
        versions = {
            v: SkillVersion.from_dict(sv)
            for v, sv in data.get("versions", {}).items()
        }
        return cls(
            name=data["name"],
            latest_version=data["latest_version"],
            versions=versions,
            tags=data.get("tags", []),
            created_at=data.get("created_at", datetime.utcnow().isoformat() + "Z"),
            updated_at=data.get("updated_at", datetime.utcnow().isoformat() + "Z"),
            download_count=data.get("download_count", 0),
        )

    def validate(self) -> None:
        self.name = _validate_slug(self.name)


# ---------------------------------------------------------------------------
# Request / Response helpers (plain dicts — no external framework needed)
# ---------------------------------------------------------------------------

@dataclass
class PublishRequest:
    """
    Parsed payload for POST /skills/publish.

    The bundle (zip bytes) is handled separately by the server layer and is
    not part of this model — it arrives as multipart form data.
    """
    name: str
    version: str
    author: str
    description: str
    capabilities: List[str] = field(default_factory=list)
    dependencies: List[SkillDependency] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    install_uri: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PublishRequest":
        deps = [SkillDependency.from_dict(d) for d in data.get("dependencies", [])]
        return cls(
            name=data.get("name", ""),
            version=data.get("version", ""),
            author=data.get("author", ""),
            description=data.get("description", ""),
            capabilities=data.get("capabilities", []),
            dependencies=deps,
            tags=data.get("tags", []),
            install_uri=data.get("install_uri"),
        )

    def validate(self) -> None:
        self.name = _validate_slug(self.name)
        self.version = _validate_version(self.version)
        if not self.author or not isinstance(self.author, str):
            raise ValueError("author must be a non-empty string")
        if not self.description or not isinstance(self.description, str):
            raise ValueError("description must be a non-empty string")
        for dep in self.dependencies:
            dep.validate()


@dataclass
class SearchResult:
    """A lightweight summary of a skill returned in search results."""
    name: str
    latest_version: str
    author: str
    description: str
    capabilities: List[str]
    tags: List[str]
    download_count: int
    published_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_skill_record(cls, record: SkillRecord) -> "SearchResult":
        latest = record.latest
        return cls(
            name=record.name,
            latest_version=record.latest_version,
            author=latest.author if latest else "",
            description=latest.description if latest else "",
            capabilities=latest.capabilities if latest else [],
            tags=record.tags,
            download_count=record.download_count,
            published_at=latest.published_at if latest else record.created_at,
        )
