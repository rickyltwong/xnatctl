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
from typing import Any

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

# Session cache payload version. Unlike config.yaml, there is no migration
# table: a cached token is disposable, so any mismatch (older, newer, wrong
# type, or unparseable) just discards the cache and forces a fresh login --
# simpler and just as safe as migrating a value that carries no user data. A
# *missing* version is the one non-mismatch: it predates the field and is
# treated as version 1, same as an explicit `"version": 1`, so an old cache
# keeps loading (see CachedSession.from_dict).
SESSION_CACHE_VERSION = 1


# =============================================================================
# Session Cache
# =============================================================================


@dataclass
class CachedSession:
    """Cached session token with metadata.

    Expiry slides with use, because that is what the server does. XNAT retires
    a JSESSIONID after a period of *inactivity*, so pinning the deadline to
    creation time threw away tokens that were still perfectly good: a session
    used every minute for an hour was declared dead at minute 15, forcing a
    fresh login on a live session.

    ``last_used_at`` is the reference point, and :meth:`touch` moves it. The
    server remains the authority -- a token it has already retired still comes
    back 401, which the client handles by re-authenticating -- so sliding can
    only avoid needless logins, never extend a session past its real life.
    """

    token: str
    url: str
    username: str
    created_at: datetime
    expires_at: datetime | None = None
    #: When this token was last handed out. Defaults to ``created_at`` for
    #: caches written before the field existed.
    last_used_at: datetime | None = None

    @property
    def idle_since(self) -> datetime:
        """The point from which the inactivity window is measured."""
        return self.last_used_at or self.created_at

    def is_expired(self, expiry_minutes: int = SESSION_EXPIRY_MINUTES) -> bool:
        """Whether the token has gone unused for longer than the window."""
        if self.expires_at is None:
            return False
        return datetime.now() >= self.idle_since + timedelta(minutes=expiry_minutes)

    def touch(self, expiry_minutes: int = SESSION_EXPIRY_MINUTES) -> None:
        """Record that the token was just used, restarting the idle clock."""
        now = datetime.now()
        self.last_used_at = now
        self.expires_at = now + timedelta(minutes=expiry_minutes)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "version": SESSION_CACHE_VERSION,
            "token": self.token,
            "url": self.url,
            "username": self.username,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_used_at": self.idle_since.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CachedSession:
        """Create from dictionary.

        A missing ``version`` key means the cache predates the field and is
        version 1.

        Raises:
            ValueError: If the payload's version is not exactly the int
                ``SESSION_CACHE_VERSION``. Callers (``AuthManager.load_session``)
                treat this like any other unreadable cache: discard it and
                fall through to a fresh login. The type check matters: JSON
                ``true`` and ``1.0`` both equal ``1`` under ``==``, and a
                cache is not meant to survive on a coincidence like that.
        """
        version = data.get("version", 1)
        if type(version) is not int or version != SESSION_CACHE_VERSION:
            raise ValueError(f"Unsupported session cache version: {version!r}")

        created_at = datetime.fromisoformat(data["created_at"])
        last_used = data.get("last_used_at")
        return cls(
            token=data["token"],
            url=data["url"],
            username=data["username"],
            created_at=created_at,
            expires_at=(
                datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None
            ),
            # Caches written before this field existed fall back to creation
            # time.
            last_used_at=datetime.fromisoformat(last_used) if last_used else created_at,
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

    def _persist_touch(self, session: CachedSession) -> None:
        """Write back a slid expiry, best-effort.

        A cache that cannot be refreshed is not worth failing a command over:
        the token in hand is still valid, and the worst case is the next
        invocation seeing a staler deadline and logging in again.
        """
        try:
            with atomic_private_write(self.cache_file) as f:
                json.dump(session.to_dict(), f)
        except OSError as e:
            logger.debug("Could not refresh session cache expiry: %s", e)

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

            # Handing the token out counts as activity, so restart the idle
            # clock. Without this the "sliding" window would never actually
            # slide -- the file would keep its original deadline.
            session.touch()
            self._persist_touch(session)

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
        try:
            self.cache_file.unlink()
            return True
        except FileNotFoundError:
            # Nothing cached, or another process cleared it first -- either
            # way the goal state (no cache file) holds; not a warning.
            return False
        except OSError as e:
            logger.warning("Could not clear cached session at %s: %s", self.cache_file, e)
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

    def get_session_info(self, url: str | None = None) -> dict[str, Any] | None:
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
