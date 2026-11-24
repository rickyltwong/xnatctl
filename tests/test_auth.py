from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from xnatctl.core.auth import AuthManager, CachedSession


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
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
        )

        assert session.token == "test-token"
        assert session.url == "https://xnat.example.org"
        assert session.username == "testuser"
        assert session.expires_at is not None
        assert cache_file.exists()

    def test_load_session(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        manager.save_session(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
        )

        session = manager.load_session()
        assert session is not None
        assert session.token == "test-token"
        assert session.url == "https://xnat.example.org"

    def test_load_session_no_cache(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        session = manager.load_session()
        assert session is None

    def test_load_session_url_match(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        manager.save_session(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
        )

        session = manager.load_session(url="https://xnat.example.org")
        assert session is not None

        session = manager.load_session(url="https://other.example.org")
        assert session is None

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
        with open(cache_file, "w") as f:
            json.dump(expired_session.to_dict(), f)

        session = manager.load_session()
        assert session is None
        assert not cache_file.exists()

    def test_load_session_invalid_json(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        with open(cache_file, "w") as f:
            f.write("not valid json")

        session = manager.load_session()
        assert session is None

    def test_clear_session(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        manager.save_session(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
        )
        assert cache_file.exists()

        result = manager.clear_session()
        assert result is True
        assert not cache_file.exists()

    def test_clear_session_no_cache(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        result = manager.clear_session()
        assert result is False

    def test_has_valid_session(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        assert manager.has_valid_session() is False

        manager.save_session(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
        )

        assert manager.has_valid_session() is True
        assert manager.has_valid_session(url="https://xnat.example.org") is True
        assert manager.has_valid_session(url="https://other.example.org") is False

    def test_get_session_token_from_env(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("XNAT_TOKEN", "env-token")

        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        manager.save_session(
            token="cached-token",
            url="https://xnat.example.org",
            username="testuser",
        )

        token = manager.get_session_token()
        assert token == "env-token"

    def test_get_session_token_from_cache(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("XNAT_TOKEN", raising=False)

        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        manager.save_session(
            token="cached-token",
            url="https://xnat.example.org",
            username="testuser",
        )

        token = manager.get_session_token()
        assert token == "cached-token"

    def test_get_session_token_none(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("XNAT_TOKEN", raising=False)

        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        token = manager.get_session_token()
        assert token is None

    def test_get_session_info(self, temp_dir: Path):
        cache_file = temp_dir / ".session"
        manager = AuthManager(cache_file=cache_file)

        info = manager.get_session_info()
        assert info is None

        manager.save_session(
            token="test-token",
            url="https://xnat.example.org",
            username="testuser",
        )

        info = manager.get_session_info()
        assert info is not None
        assert info["url"] == "https://xnat.example.org"
        assert info["username"] == "testuser"
        assert "created_at" in info
        assert "expires_at" in info
        assert info["is_expired"] is False
