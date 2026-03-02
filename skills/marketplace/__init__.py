"""
OpenPango Skill Marketplace — Registry Package
==============================================

Exposes the core client and server components for the decentralized
skill registry protocol.

Quick start::

    from skills.marketplace import SkillRegistryClient, RegistryStorage

    client = SkillRegistryClient()
    results = client.search("web scraper")
"""
from .models import (
    SkillDependency,
    SkillVersion,
    SkillRecord,
    PublishRequest,
    SearchResult,
)
from .storage import RegistryStorage
from .registry_client import SkillRegistryClient, SkillRegistry

__all__ = [
    "SkillRegistryClient",
    "SkillRegistry",          # backwards-compatible alias
    "RegistryStorage",
    "SkillDependency",
    "SkillVersion",
    "SkillRecord",
    "PublishRequest",
    "SearchResult",
]

__version__ = "1.0.0"
__author__ = "WeberG619"
