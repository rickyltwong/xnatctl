from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from xnatctl.core.auth import SESSION_CACHE_VERSION, AuthManager, CachedSession
from xnatctl.core.fsutil import exclusive_lock


class TestCachedSession:
    def test_create_session(self):
        now = datetime.now()
        session = CachedSession(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
            created_at=now,
            expires_at=now + timedelta(hours=12),
        )
        assert session.token == "test-token"
        assert session.url == "https://xnat.example.org"
        assert session.username == "testuser"
        assert session.created_at == now

    def test_is_expired_false(self):
        now = datetime.now()
        session = CachedSession(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
            created_at=now,
            expires_at=now + timedelta(hours=12),
        )
        assert session.is_expired() is False

    def test_is_expired_true(self):
        now = datetime.now()
        session = CachedSession(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
            created_at=now - timedelta(hours=24),
            expires_at=now - timedelta(hours=12),
        )
        assert session.is_expired() is True

    def test_is_expired_no_expiry(self):
        session = CachedSession(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
            created_at=datetime.now(),
            expires_at=None,
        )
        assert session.is_expired() is False

    def test_to_dict(self):
        now = datetime.now()
        expires = now + timedelta(hours=12)
        session = CachedSession(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
            created_at=now,
            expires_at=expires,
        )
        data = session.to_dict()
        assert data["token"] == "test-token"
        assert data["url"] == "https://xnat.example.org"
        assert data["username"] == "testuser"
        assert data["created_at"] == now.isoformat()
        assert data["expires_at"] == expires.isoformat()

    def test_to_dict_no_expiry(self):
        session = CachedSession(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
            created_at=datetime.now(),
            expires_at=None,
        )
        data = session.to_dict()
        assert data["expires_at"] is None

    def test_from_dict(self):
        now = datetime.now()
        expires = now + timedelta(hours=12)
        data = {
            "token": "test-token",
            "url": "https://xnat.example.org",
            "username": "testuser",
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }
        session = CachedSession.from_dict(data)
        assert session.token == "test-token"
        assert session.url == "https://xnat.example.org"
        assert session.username == "testuser"

    def test_from_dict_no_expiry(self):
        data = {
            "token": "test-token",
            "url": "https://xnat.example.org",
            "username": "testuser",
            "created_at": datetime.now().isoformat(),
            "expires_at": None,
        }
        session = CachedSession.from_dict(data)
        assert session.expires_at is None

    def test_roundtrip(self):
        now = datetime.now()
        original = CachedSession(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
            created_at=now,
            expires_at=now + timedelta(hours=12),
        )
        data = original.to_dict()
        restored = CachedSession.from_dict(data)
        assert restored.token == original.token
        assert restored.url == original.url
        assert restored.username == original.username


class TestAuthManager:
    def test_init_default_cache_file(self):
        manager = AuthManager()
        assert manager.cache_file.name == ".session"

    def test_init_custom_cache_file(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)
        assert manager.cache_file == cache_file

    def test_get_credentials_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("XNAT_USER", "envuser")
        monkeypatch.setenv("XNAT_PASS", "envpass")

        manager = AuthManager()
        username, password = manager.get_credentials()
        assert username == "envuser"
        assert password == "envpass"

    def test_get_credentials_not_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("XNAT_USER", raising=False)
        monkeypatch.delenv("XNAT_PASS", raising=False)

        manager = AuthManager()
        username, password = manager.get_credentials()
        assert username is None
        assert password is None

    def test_get_token_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("XNAT_TOKEN", "env-token")

        manager = AuthManager()
        token = manager.get_token_from_env()
        assert token == "env-token"

    def test_get_token_from_env_not_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("XNAT_TOKEN", raising=False)

        manager = AuthManager()
        token = manager.get_token_from_env()
        assert token is None

    def test_save_session(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        session = manager.save_session(
            "prod",
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
        )

        assert session.token == "test-token"
        assert session.url == "https://xnat.example.org"
        assert session.username == "testuser"
        assert session.expires_at is not None
        payload = json.loads(cache_file.read_text())
        assert payload["version"] == SESSION_CACHE_VERSION
        assert payload["sessions"]["prod"]["token"] == "test-token"

    def test_load_session(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        manager.save_session(
            "prod", token="test-token", url="https://xnat.example.org", username="testuser"
        )

        session = manager.load_session("prod", "https://xnat.example.org", "testuser")
        assert session is not None
        assert session.token == "test-token"
        assert session.url == "https://xnat.example.org"

    def test_load_session_no_cache(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        session = manager.load_session("prod", "https://xnat.example.org", "testuser")
        assert session is None

    def test_load_session_url_mismatch(self, temp_dir: Path):
        """A profile whose URL changed since it logged in misses."""
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        manager.save_session(
            "prod", token="test-token", url="https://xnat.example.org", username="testuser"
        )

        assert manager.load_session("prod", "https://xnat.example.org", "testuser") is not None
        assert manager.load_session("prod", "https://other.example.org", "testuser") is None

    def test_load_session_expired(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        now = datetime.now()
        expired_session = CachedSession(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
            created_at=now - timedelta(hours=24),
            expires_at=now - timedelta(hours=12),
        )
        _write_cache(cache_file, {"prod": expired_session})

        session = manager.load_session("prod", "https://xnat.example.org", "testuser")
        assert session is None
        # The only entry is gone, and with it the file
        assert not cache_file.exists()

    def test_load_session_invalid_json(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        with open(cache_file, "w") as f:
            f.write("not valid json")

        session = manager.load_session("prod", "https://xnat.example.org", "testuser")
        assert session is None
        assert not cache_file.exists()

    def test_clear_session(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        manager.save_session(
            "prod", token="test-token", url="https://xnat.example.org", username="testuser"
        )
        assert cache_file.exists()

        result = manager.clear_session("prod")
        assert result is True
        assert not cache_file.exists()

    def test_clear_session_no_cache(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        result = manager.clear_session("prod")
        assert result is False

    def test_get_session_token_from_env(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("XNAT_TOKEN", "env-token")

        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        manager.save_session(
            "prod", token="cached-token", url="https://xnat.example.org", username="testuser"
        )

        token = manager.get_session_token("prod", "https://xnat.example.org", "testuser")
        assert token == "env-token"

    def test_get_session_token_from_cache(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("XNAT_TOKEN", raising=False)

        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        manager.save_session(
            "prod", token="cached-token", url="https://xnat.example.org", username="testuser"
        )

        token = manager.get_session_token("prod", "https://xnat.example.org", "testuser")
        assert token == "cached-token"

    def test_get_session_token_none(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("XNAT_TOKEN", raising=False)

        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        token = manager.get_session_token("prod", "https://xnat.example.org", "testuser")
        assert token is None

    def test_get_session_info(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        info = manager.get_session_info("prod", "https://xnat.example.org", "testuser")
        assert info is None

        manager.save_session(
            "prod", token="test-token", url="https://xnat.example.org", username="testuser"
        )

        info = manager.get_session_info("prod", "https://xnat.example.org", "testuser")
        assert info is not None
        assert info["url"] == "https://xnat.example.org"
        assert info["username"] == "testuser"
        assert "created_at" in info
        assert "expires_at" in info
        assert info["is_expired"] is False


# =============================================================================
# One session per profile
# =============================================================================

_URL = "https://xnat.example.org"


class TestSessionPerProfile:
    """Profiles on one server keep separate sessions, each tied to its user."""

    def test_profiles_on_one_server_keep_their_own_sessions(self, temp_dir: Path):
        """Logging in as a second profile neither evicts nor replaces the first's."""
        manager = AuthManager(cache_file=temp_dir / ".session")
        manager.save_session("admin-profile", "ADMIN_TOKEN", _URL, "admin_user")
        manager.save_session("user-profile", "USER_TOKEN", _URL, "non_admin_user")

        admin = manager.load_session("admin-profile", _URL, "admin_user")
        user = manager.load_session("user-profile", _URL, "non_admin_user")

        assert admin is not None and admin.token == "ADMIN_TOKEN"
        assert user is not None and user.token == "USER_TOKEN"

    def test_another_profiles_session_is_not_handed_out(self, temp_dir: Path):
        manager = AuthManager(cache_file=temp_dir / ".session")
        manager.save_session("admin-profile", "ADMIN_TOKEN", _URL, "admin_user")

        assert manager.load_session("user-profile", _URL, "non_admin_user") is None

    def test_a_session_for_another_user_misses_and_is_kept(self, temp_dir: Path):
        """An XNAT_USER override or an edited profile username must not run as
        the user the session was created for. The entry stays: it is still
        that user's session, and the next login for the profile replaces it.
        """
        manager = AuthManager(cache_file=temp_dir / ".session")
        manager.save_session("prod", "ALICE_TOKEN", _URL, "alice")

        assert manager.load_session("prod", _URL, "bob") is None
        kept = manager.load_session("prod", _URL, "alice")
        assert kept is not None and kept.token == "ALICE_TOKEN"

    def test_a_profile_naming_no_user_uses_its_own_session(self, temp_dir: Path):
        """A profile with no username (and no XNAT_USER) can only have been
        logged in with ``auth login``, so that session is the profile's.
        """
        manager = AuthManager(cache_file=temp_dir / ".session")
        manager.save_session("prod", "ALICE_TOKEN", _URL, "alice")

        session = manager.load_session("prod", _URL, None)

        assert session is not None and session.token == "ALICE_TOKEN"

    def test_clearing_one_profile_keeps_the_others(self, temp_dir: Path):
        manager = AuthManager(cache_file=temp_dir / ".session")
        manager.save_session("admin-profile", "ADMIN_TOKEN", _URL, "admin_user")
        manager.save_session("user-profile", "USER_TOKEN", _URL, "non_admin_user")

        assert manager.clear_session("user-profile") is True

        assert manager.load_session("user-profile", _URL, "non_admin_user") is None
        assert manager.load_session("admin-profile", _URL, "admin_user") is not None

    def test_an_expired_session_is_removed_without_touching_the_others(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        now = datetime.now()
        _write_cache(
            cache_file,
            {
                "stale": CachedSession(
                    token="OLD",
                    url=_URL,
                    username="alice",
                    created_at=now - timedelta(hours=2),
                    expires_at=now - timedelta(hours=1),
                ),
                "fresh": CachedSession(
                    token="NEW",
                    url=_URL,
                    username="bob",
                    created_at=now,
                    expires_at=now + timedelta(minutes=15),
                ),
            },
        )
        manager = AuthManager(cache_file=cache_file)

        assert manager.load_session("stale", _URL, "alice") is None

        assert set(json.loads(cache_file.read_text())["sessions"]) == {"fresh"}

    def test_replace_token_renews_only_the_session_that_held_it(self, temp_dir: Path):
        """A worker's re-login renews the session it was using. A second
        profile on the same account logged in on its own and keeps that
        session, so logging one out never revokes the other.
        """
        manager = AuthManager(cache_file=temp_dir / ".session")
        manager.save_session("alice-1", "ALICE_OLD", _URL, "alice")
        manager.save_session("alice-2", "ALICE_OWN", _URL, "alice")
        manager.save_session("bob", "BOB_TOKEN", _URL, "bob")

        manager.replace_token("ALICE_OLD", "ALICE_NEW")

        tokens = {
            name: entry["token"]
            for name, entry in json.loads(manager.cache_file.read_text())["sessions"].items()
        }
        assert tokens == {"alice-1": "ALICE_NEW", "alice-2": "ALICE_OWN", "bob": "BOB_TOKEN"}

    def test_replace_token_without_a_matching_entry_writes_nothing(self, temp_dir: Path):
        manager = AuthManager(cache_file=temp_dir / ".session")
        manager.save_session("prod", "TOKEN", _URL, "alice")
        before = manager.cache_file.read_text()

        manager.replace_token("UNKNOWN", "NEW")

        assert manager.cache_file.read_text() == before

    def test_a_version_1_cache_is_discarded(self, temp_dir: Path):
        """The single-session layout matched by URL alone is what let profiles
        run as each other's user; this build never reads it.
        """
        cache_file = temp_dir / ".session"
        old = CachedSession(token="TOK", url=_URL, username="admin_user", created_at=datetime.now())
        cache_file.write_text(json.dumps({"version": 1, **old.to_dict()}))
        manager = AuthManager(cache_file=cache_file)

        assert manager.load_session("user-profile", _URL, None) is None
        assert not cache_file.exists()


class TestCacheLock:
    """Every read-modify-write holds an interprocess lock, so overlapping
    commands cannot write back a stale copy over each other's change.
    """

    @pytest.fixture
    def held_lock(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
        """Hold the cache lock as another process would, with a short timeout.

        The lock belongs to an open file, so holding it through a second open
        excludes the manager even within one process.
        """
        monkeypatch.setattr("xnatctl.core.auth.SESSION_CACHE_LOCK_TIMEOUT_SECONDS", 0.2)
        manager = AuthManager(cache_file=temp_dir / ".session")
        manager.save_session("prod", "TOKEN", _URL, "alice")
        with exclusive_lock(temp_dir / ".session.lock", timeout=0):
            yield manager

    def test_a_write_waits_for_the_lock(self, held_lock: AuthManager):
        with pytest.raises(TimeoutError):
            held_lock.save_session("other", "OTHER", _URL, "bob")

        assert set(json.loads(held_lock.cache_file.read_text())["sessions"]) == {"prod"}

    def test_a_lookup_that_cannot_get_the_lock_logs_in_without_the_cache(
        self, held_lock: AuthManager, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger="xnatctl.core.auth"):
            assert held_lock.load_session("prod", _URL, "alice") is None

        assert any("Could not read the session cache" in r.message for r in caplog.records)

    def test_a_logout_that_cannot_get_the_lock_says_so(self, held_lock: AuthManager):
        assert held_lock.clear_session("prod") is False

    def test_concurrent_logins_for_different_profiles_all_survive(self, temp_dir: Path):
        manager = AuthManager(cache_file=temp_dir / ".session")
        names = [f"profile-{i}" for i in range(16)]
        barrier = threading.Barrier(len(names))

        def login(name: str) -> None:
            barrier.wait()
            AuthManager(cache_file=manager.cache_file).save_session(name, name, _URL, name)

        threads = [threading.Thread(target=login, args=(name,)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert set(json.loads(manager.cache_file.read_text())["sessions"]) == set(names)


def _write_cache(cache_file: Path, sessions: dict[str, CachedSession]) -> None:
    """Write a current-version cache file holding *sessions*."""
    cache_file.write_text(
        json.dumps(
            {
                "version": SESSION_CACHE_VERSION,
                "sessions": {name: s.to_dict() for name, s in sessions.items()},
            }
        )
    )
