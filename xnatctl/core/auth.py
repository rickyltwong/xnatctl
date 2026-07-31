"""Authentication management for xnatctl.

Handles credential storage and session token caching.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from xnatctl.core.config import CONFIG_DIR, ENV_PASS, ENV_TOKEN, ENV_USER
from xnatctl.core.fsutil import atomic_private_write, ensure_private_dir, restrict_permissions
from xnatctl.core.redact import redact_url_query

# =============================================================================
# Constants
# =============================================================================

logger = logging.getLogger(__name__)

# Every log line in this module reports the *source* or *presence* of a
# credential, never its value. "Which credential did xnatctl actually use?" is
# the question `-v` has to answer here; the answer must not be the secret
# itself.

SESSION_CACHE_FILE = CONFIG_DIR / ".session"
SESSION_EXPIRY_MINUTES = 15  # XNAT JSESSION expires after 15 minutes of inactivity by default


# =============================================================================
# Session Cache
# =============================================================================


@dataclass
class CachedSession:
    """Cached session token with metadata."""

    token: str
    url: str
    username: str
    created_at: datetime
    expires_at: datetime | None = None

    def is_expired(self) -> bool:
        """Check if session has expired."""
        if self.expires_at:
            return datetime.now() >= self.expires_at
        return False

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "token": self.token,
            "url": self.url,
            "username": self.username,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> CachedSession:
        """Create from dictionary."""
        return cls(
            token=data["token"],
            url=data["url"],
            username=data["username"],
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=(
                datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None
            ),
        )


# =============================================================================
# AuthManager
# =============================================================================


class AuthManager:
    """Manages authentication credentials and session tokens."""

    def __init__(self, cache_file: Path | None = None):
        """Initialize auth manager.

        Args:
            cache_file: Path to session cache file.
        """
        self.cache_file = cache_file or SESSION_CACHE_FILE

    # =========================================================================
    # Credential Access
    # =========================================================================

    def get_credentials(self) -> tuple[str | None, str | None]:
        """Get credentials from environment variables.

        Returns:
            Tuple of (username, password).
        """
        username = os.getenv(ENV_USER)
        password = os.getenv(ENV_PASS)
        logger.debug(
            "Environment credentials: %s=%s, %s=%s",
            ENV_USER,
            "set" if username else "unset",
            ENV_PASS,
            "set" if password else "unset",
        )
        return username, password

    def get_token_from_env(self) -> str | None:
        """Get session token from environment variable.

        Returns:
            Token if set.
        """
        return os.getenv(ENV_TOKEN)

    # =========================================================================
    # Session Cache
    # =========================================================================

    def save_session(
        self,
        token: str,
        url: str,
        username: str,
        expiry_minutes: int = SESSION_EXPIRY_MINUTES,
    ) -> CachedSession:
        """Save session token to cache.

        Args:
            token: Session token (JSESSIONID).
            url: XNAT server URL.
            username: Username used for authentication.
            expiry_minutes: Minutes until session is considered expired.

        Returns:
            Cached session object.
        """
        now = datetime.now()
        session = CachedSession(
            token=token,
            url=url,
            username=username,
            created_at=now,
            expires_at=now + timedelta(minutes=expiry_minutes),
        )

        # Ensure the directory exists and is owner-only.
        ensure_private_dir(self.cache_file.parent)

        # Write through a private temp file so the token is never observable
        # under the process umask, not even for the duration of the write.
        with atomic_private_write(self.cache_file) as f:
            json.dump(session.to_dict(), f)

        # The replace already carries the temp file's 0600 mode; this is
        # belt-and-braces for filesystems that do not, and it warns rather than
        # silently leaving the token readable.
        restrict_permissions(self.cache_file)

        logger.debug(
            "Cached session for %s at %s (expires %s)",
            username,
            self.cache_file,
            session.expires_at.isoformat() if session.expires_at else "never",
        )

        return session

    def load_session(self, url: str | None = None) -> CachedSession | None:
        """Load cached session token.

        Args:
            url: Optional URL to match. If provided, only returns session for that URL.

        Returns:
            Cached session if valid, None otherwise.
        """
        if not self.cache_file.exists():
            logger.debug("Session cache miss: %s does not exist", self.cache_file)
            return None

        try:
            with open(self.cache_file) as f:
                data = json.load(f)

            session = CachedSession.from_dict(data)

            # Check URL match
            if url and session.url != url:
                logger.debug(
                    "Session cache miss: cached for %s, wanted %s",
                    redact_url_query(session.url),
                    redact_url_query(url),
                )
                return None

            # Check expiry
            if session.is_expired():
                logger.debug(
                    "Session cache expired at %s; clearing",
                    session.expires_at.isoformat() if session.expires_at else "unknown",
                )
                self.clear_session()
                return None

            logger.debug("Session cache hit for %s (user %s)", session.url, session.username)
            return session

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # Invalid cache file
            logger.debug("Session cache unreadable (%s); clearing", type(e).__name__)
            self.clear_session()
            return None

    def clear_session(self) -> bool:
        """Clear cached session.

        Returns:
            True if cache was cleared.
        """
        if self.cache_file.exists():
            try:
                self.cache_file.unlink()
                return True
            except OSError:
                pass
        return False

    def has_valid_session(self, url: str | None = None) -> bool:
        """Check if there's a valid cached session.

        Args:
            url: Optional URL to match.

        Returns:
            True if valid session exists.
        """
        session = self.load_session(url)
        return session is not None and not session.is_expired()

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    def get_session_token(self, url: str | None = None) -> str | None:
        """Get session token from cache or environment.

        Priority:
        1. Environment variable (XNAT_TOKEN)
        2. Cached session

        Args:
            url: Optional URL to match for cached session.

        Returns:
            Session token if available.
        """
        # Check environment first
        if token := self.get_token_from_env():
            logger.debug("Using session token from %s", ENV_TOKEN)
            return token

        # Check cache
        if session := self.load_session(url):
            return session.token

        return None

    def get_session_info(self, url: str | None = None) -> dict | None:
        """Get session information for display.

        Args:
            url: Optional URL to match.

        Returns:
            Dict with session info or None.
        """
        session = self.load_session(url)
        if not session:
            return None

        return {
            "url": session.url,
            "username": session.username,
            "created_at": session.created_at.isoformat(),
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
            "is_expired": session.is_expired(),
        }
