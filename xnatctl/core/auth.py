"""Authentication management for xnatctl.

Handles credential storage and session token caching.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from xnatctl.core.config import CONFIG_DIR, ENV_PASS, ENV_TOKEN, ENV_USER
from xnatctl.core.fsutil import (
    atomic_private_write,
    ensure_private_dir,
    exclusive_lock,
    restrict_permissions,
)
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
# table: a cached token is disposable, so any mismatch (older, newer, missing,
# wrong type, or unparseable) just discards the cache and forces a fresh login --
# simpler and just as safe as migrating a value that carries no user data.
#
# Version 2 holds one session per profile. Version 1 held a single session
# matched by server URL alone, so two profiles on one server with different
# users ran as whichever user logged in last; discarding a version-1 file is
# the whole transition.
SESSION_CACHE_VERSION = 2

# How long a cache operation waits for another xnatctl process to finish its
# own. Each holds the lock for one small file read and write, so a wait this
# long means that process is stuck.
SESSION_CACHE_LOCK_TIMEOUT_SECONDS = 10


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

        Raises:
            KeyError: If a required field is missing.
            TypeError: If a field has the wrong type.
            ValueError: If a timestamp does not parse.
        """
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

    def _lock(self) -> AbstractContextManager[None]:
        """The interprocess lock held across each read-modify-write of the cache.

        Every write replaces the whole file from the copy its process read, so
        without the lock two overlapping commands drop each other's change: a
        touch could restore a profile another process just logged out, or one
        of two concurrent logins could vanish. Entering it raises
        ``TimeoutError`` (an ``OSError``) after
        :data:`SESSION_CACHE_LOCK_TIMEOUT_SECONDS`.
        """
        return exclusive_lock(
            self.cache_file.with_name(self.cache_file.name + ".lock"),
            SESSION_CACHE_LOCK_TIMEOUT_SECONDS,
        )

    def _read_cache(self) -> dict[str, CachedSession]:
        """Return the cached sessions, keyed by profile name.

        Call with :meth:`_lock` held.

        A cache that cannot be read as the current version -- bad JSON, a
        missing field, or a version this build does not write -- is deleted
        and reads as empty: the caller then logs in again.
        """
        if not self.cache_file.exists():
            return {}

        try:
            with open(self.cache_file) as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
                raise ValueError("Session cache is not a version-2 payload")
            # The type check matters: JSON ``true`` and ``2.0`` both equal 2
            # under ``==``, and a cache is not meant to survive on that.
            version = data.get("version")
            if type(version) is not int or version != SESSION_CACHE_VERSION:
                raise ValueError(f"Unsupported session cache version: {version!r}")
            return {
                name: CachedSession.from_dict(entry) for name, entry in data["sessions"].items()
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Session cache unreadable (%s); clearing", type(e).__name__)
            try:
                self.cache_file.unlink(missing_ok=True)
            except OSError as unlink_error:
                logger.warning(
                    "Could not clear cached session at %s: %s", self.cache_file, unlink_error
                )
            return {}

    def _write_cache(self, sessions: dict[str, CachedSession]) -> None:
        """Replace the cache with *sessions*; delete the file once none is left.

        Call with :meth:`_lock` held, inside the same hold as the read that
        produced *sessions*.

        Raises:
            OSError: If the file cannot be written or deleted.
        """
        if not sessions:
            self.cache_file.unlink(missing_ok=True)
            return
        # Write through a private temp file so the token is never observable
        # under the process umask, not even for the duration of the write.
        with atomic_private_write(self.cache_file) as f:
            json.dump(
                {
                    "version": SESSION_CACHE_VERSION,
                    "sessions": {name: s.to_dict() for name, s in sessions.items()},
                },
                f,
            )

    def save_session(
        self,
        profile: str,
        token: str,
        url: str,
        username: str,
        expiry_minutes: int = SESSION_EXPIRY_MINUTES,
    ) -> CachedSession:
        """Cache a session token for one profile, leaving other profiles' alone.

        Args:
            profile: Name of the profile the session was created for.
            token: Session token (JSESSIONID).
            url: XNAT server URL.
            username: The name the session logged in with -- the one the
                profile resolves to, not a server-reported alias, so that
                :meth:`load_session` can compare like with like.
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
        with self._lock():
            sessions = self._read_cache()
            sessions[profile] = session
            self._write_cache(sessions)

        # The replace already carries the temp file's 0600 mode; this is
        # belt-and-braces for filesystems that do not, and it warns rather than
        # silently leaving the token readable.
        restrict_permissions(self.cache_file)

        logger.debug(
            "Cached session for %s (profile %s) at %s (expires %s)",
            username,
            profile,
            self.cache_file,
            session.expires_at.isoformat() if session.expires_at else "never",
        )

        return session

    def load_session(self, profile: str, url: str, username: str | None) -> CachedSession | None:
        """Load a profile's cached session, if it still belongs to that profile.

        The session is used only while it matches who the profile resolves
        to now: the same server URL and, when *username* is given, the same
        user. A profile edited to another URL or user, or an ``XNAT_USER``
        override, therefore misses rather than running as the old account.

        Args:
            profile: Profile name the session was cached under.
            url: The profile's server URL.
            username: Who the profile authenticates as (env vars > profile
                config). ``None`` when neither names a user: the only session
                such a profile can have is one ``auth login`` created for it,
                and that one is used.

        Returns:
            Cached session if valid, None otherwise.
        """
        if not self.cache_file.exists():
            logger.debug("Session cache miss: %s does not exist", self.cache_file)
            return None
        try:
            with self._lock():
                return self._load_locked(profile, url, username)
        except OSError as e:
            # The cache only saves a login; failing to read it is not worth
            # failing the command over.
            logger.warning(
                "Could not read the session cache at %s (%s); logging in without it",
                self.cache_file,
                e,
            )
            return None

    def _load_locked(self, profile: str, url: str, username: str | None) -> CachedSession | None:
        """:meth:`load_session`'s lookup, run with the cache lock held."""
        sessions = self._read_cache()
        session = sessions.get(profile)
        if session is None:
            logger.debug(
                "Session cache miss: nothing cached for profile %s in %s", profile, self.cache_file
            )
            return None

        if session.url != url:
            logger.debug(
                "Session cache miss: profile %s cached for %s, wanted %s",
                profile,
                redact_url_query(session.url),
                redact_url_query(url),
            )
            return None

        if username and session.username != username:
            logger.debug(
                "Session cache miss: profile %s cached for user %s, wanted %s",
                profile,
                session.username,
                username,
            )
            return None

        if session.is_expired():
            logger.debug(
                "Session cache expired at %s; clearing",
                session.expires_at.isoformat() if session.expires_at else "unknown",
            )
            del sessions[profile]
            try:
                self._write_cache(sessions)
            except OSError as e:
                logger.warning("Could not clear cached session at %s: %s", self.cache_file, e)
            return None

        # Handing the token out counts as activity, so restart the idle
        # clock. Without this the "sliding" window would never actually
        # slide -- the file would keep its original deadline. Best-effort: the
        # token in hand is still valid, and the worst case is the next
        # invocation seeing a staler deadline and logging in again.
        session.touch()
        try:
            self._write_cache(sessions)
        except OSError as e:
            logger.debug("Could not refresh session cache expiry: %s", e)

        logger.debug(
            "Session cache hit for profile %s: %s (user %s)",
            profile,
            redact_url_query(session.url),
            session.username,
        )
        return session

    def replace_token(self, old_token: str, new_token: str) -> None:
        """Swap a renewed session's new token into the entry that held the old one.

        For a re-login made outside a profile lookup, such as a long upload's
        worker refresh: a concurrent process using that profile then skips a
        login. Only the entry holding *old_token* changes. A profile that logged
        in on its own, even as the same account, keeps its own session, so
        logging one of them out never revokes the other.

        Raises:
            OSError: If the cache cannot be locked or written.
        """
        if not self.cache_file.exists():
            return
        with self._lock():
            sessions = self._read_cache()
            renewed = [name for name, s in sessions.items() if s.token == old_token]
            if not renewed:
                return
            now = datetime.now()
            for name in renewed:
                old = sessions[name]
                sessions[name] = CachedSession(
                    token=new_token,
                    url=old.url,
                    username=old.username,
                    created_at=now,
                    expires_at=now + timedelta(minutes=SESSION_EXPIRY_MINUTES),
                )
            self._write_cache(sessions)

    def clear_session(self, profile: str) -> bool:
        """Remove one profile's cached session.

        Returns:
            True if a session was cached for the profile and is now gone.
        """
        if not self.cache_file.exists():
            return False
        try:
            with self._lock():
                sessions = self._read_cache()
                if sessions.pop(profile, None) is None:
                    return False
                self._write_cache(sessions)
        except OSError as e:
            logger.warning("Could not clear cached session at %s: %s", self.cache_file, e)
            return False
        return True

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    def get_session_token(self, profile: str, url: str, username: str | None) -> str | None:
        """Get session token from cache or environment.

        Priority:
        1. Environment variable (XNAT_TOKEN)
        2. The profile's cached session (see :meth:`load_session`)

        Args:
            profile: Profile name.
            url: The profile's server URL.
            username: Who the profile authenticates as, or None.

        Returns:
            Session token if available.
        """
        # Check environment first
        if token := self.get_token_from_env():
            logger.debug("Using session token from %s", ENV_TOKEN)
            return token

        # Check cache
        if session := self.load_session(profile, url, username):
            return session.token

        return None

    def get_session_info(
        self, profile: str, url: str, username: str | None
    ) -> dict[str, Any] | None:
        """Get session information for display.

        Args:
            profile: Profile name.
            url: The profile's server URL.
            username: Who the profile authenticates as, or None.

        Returns:
            Dict with session info or None.
        """
        session = self.load_session(profile, url, username)
        if not session:
            return None

        return {
            "url": session.url,
            "username": session.username,
            "created_at": session.created_at.isoformat(),
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
            "is_expired": session.is_expired(),
        }
