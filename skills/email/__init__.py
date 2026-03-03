"""skills/email - Native email management with IMAP/SMTP and thread tracking.

Public API:
  EmailHandler   — unified IMAP+SMTP facade (context manager supported)
  ThreadTracker  — SQLite-backed conversation thread tracking
  CredentialStore — secure per-account credential storage
"""

from .email_handler import EmailHandler
from .thread_tracker import ThreadTracker
from .credentials import CredentialStore

__all__ = ["EmailHandler", "ThreadTracker", "CredentialStore"]
__version__ = "1.0.0"
