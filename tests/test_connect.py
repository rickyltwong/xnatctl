"""Tests for the one-call library facade: build_client_from_profile + accessors.

Covers the credential-resolution precedence the CLI relies on (extracted from
``Context.get_client``), the ``XNATClient.from_profile`` classmethod, the
lazily-cached service accessors, and the context-manager login behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from xnatctl.core.client import XNATClient
from xnatctl.core.connect import build_client_from_profile
from xnatctl.core.exceptions import ProfileNotFoundError
from xnatctl.services.admin import AdminService
from xnatctl.services.downloads import DownloadService
from xnatctl.services.exam_upload import ExamUploadService
from xnatctl.services.hierarchy import HierarchyService
from xnatctl.services.pipelines import PipelineService
from xnatctl.services.prearchive import PrearchiveService
from xnatctl.services.projects import ProjectService
from xnatctl.services.resources import ResourceService
from xnatctl.services.scans import ScanService
from xnatctl.services.sessions import SessionService
from xnatctl.services.subjects import SubjectService
from xnatctl.services.uploads import UploadService

_URL = "https://xnat.example.org"


@dataclass
class _FakeSession:
    token: str
    username: str
    url: str = _URL


@dataclass
class _FakeAuthManager:
    """Stand-in for AuthManager with controllable token/session resolution."""

    env_token: str | None = None
    session: _FakeSession | None = None

    def get_token_from_env(self) -> str | None:
        return self.env_token

    def load_session(self, url: str | None = None) -> _FakeSession | None:
        return self.session


@pytest.fixture(autouse=True)
def _clear_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient XNAT_* variable may leak into resolution under test."""
    for var in ("XNAT_URL", "XNAT_USER", "XNAT_PASS", "XNAT_TOKEN", "XNAT_PROFILE"):
        monkeypatch.delenv(var, raising=False)


def _write_config(tmp_path: Path, *, verify_ssl: bool = True, timeout: int = 42) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "default_profile: prod\n"
        "profiles:\n"
        "  prod:\n"
        f"    url: {_URL}\n"
        "    username: alice\n"
        "    password: profile-pw\n"
        f"    verify_ssl: {'true' if verify_ssl else 'false'}\n"
        f"    timeout: {timeout}\n"
    )
    return path


# =============================================================================
# Credential-resolution precedence
# =============================================================================


def test_all_sources_present_resolves_the_full_param_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With env creds, profile creds, an env token, AND a cached session all
    present at once, the complete resolved dict follows the precedence rules:
    env creds beat profile creds, the env token beats the cached token, and the
    env username (not the cached session's) is the hint.
    """
    from xnatctl.core.connect import resolve_client_params

    monkeypatch.setenv("XNAT_USER", "env-user")
    monkeypatch.setenv("XNAT_PASS", "env-pw")
    auth = _FakeAuthManager(env_token="ENV_TOKEN", session=_FakeSession("CACHED", "cached-bob"))

    params = resolve_client_params("prod", config_path=_write_config(tmp_path), auth_manager=auth)

    assert params == {
        "base_url": _URL,
        "username": "env-user",
        "password": "env-pw",
        "session_token": "ENV_TOKEN",
        "timeout": 42,
        "verify_ssl": True,
        "ca_bundle": None,
        "auto_reauth": True,
    }


def test_env_token_wins_over_cached_session_and_profile(tmp_path: Path) -> None:
    auth = _FakeAuthManager(env_token="ENV_TOKEN", session=_FakeSession("CACHED", "bob"))
    client = build_client_from_profile(
        "prod", config_path=_write_config(tmp_path), auth_manager=auth
    )

    assert client.session_token == "ENV_TOKEN"


def test_cached_session_wins_over_profile_password(tmp_path: Path) -> None:
    auth = _FakeAuthManager(env_token=None, session=_FakeSession("CACHED", "bob"))
    client = build_client_from_profile(
        "prod", config_path=_write_config(tmp_path), auth_manager=auth
    )

    assert client.session_token == "CACHED"
    # The cached username backfills the hint; the profile password stays wired
    # so a mid-operation reauth can still self-heal.
    assert client.username == "alice"  # profile username takes precedence over session
    assert client.password == "profile-pw"


def test_profile_password_used_when_no_token(tmp_path: Path) -> None:
    auth = _FakeAuthManager(env_token=None, session=None)
    client = build_client_from_profile(
        "prod", config_path=_write_config(tmp_path), auth_manager=auth
    )

    assert client.session_token is None
    assert client.password == "profile-pw"
    assert client.auto_reauth is True


def test_session_username_backfills_when_profile_has_none(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"default_profile: prod\nprofiles:\n  prod:\n    url: {_URL}\n    verify_ssl: true\n"
    )
    auth = _FakeAuthManager(env_token=None, session=_FakeSession("CACHED", "cached-user"))
    client = build_client_from_profile("prod", config_path=path, auth_manager=auth)

    assert client.username == "cached-user"


# =============================================================================
# Profile lookup + from_profile threading
# =============================================================================


def test_missing_profile_raises_with_hint(tmp_path: Path) -> None:
    auth = _FakeAuthManager()
    with pytest.raises(ProfileNotFoundError) as exc:
        build_client_from_profile("ghost", config_path=_write_config(tmp_path), auth_manager=auth)

    assert exc.value.hint is not None
    assert "config show" in exc.value.hint


def test_from_profile_reads_profile_fields(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, verify_ssl=False, timeout=99)
    client = XNATClient.from_profile("prod", config_path=config_path)

    assert client.base_url == _URL
    assert client.timeout == 99
    assert client.verify_ssl is False
    assert client.auto_reauth is True


# =============================================================================
# Service accessors
# =============================================================================


@pytest.mark.parametrize(
    ("attr", "service_type"),
    [
        ("projects", ProjectService),
        ("subjects", SubjectService),
        ("sessions", SessionService),
        ("scans", ScanService),
        ("resources", ResourceService),
        ("prearchive", PrearchiveService),
        ("pipelines", PipelineService),
        ("admin", AdminService),
        ("hierarchy", HierarchyService),
        ("downloads", DownloadService),
        ("uploads", UploadService),
        ("exam_uploads", ExamUploadService),
    ],
)
def test_accessor_returns_type_and_caches(attr: str, service_type: type) -> None:
    client = XNATClient(base_url=_URL)

    service = getattr(client, attr)
    assert isinstance(service, service_type)
    assert service.client is client
    # Cached: a second access returns the very same object.
    assert getattr(client, attr) is service


# =============================================================================
# Context-manager authentication
# =============================================================================


def _login_transport(calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="JSESSION_TOKEN")

    return httpx.MockTransport(handler)


def test_enter_authenticates_with_credentials_and_no_token() -> None:
    calls: list[httpx.Request] = []
    client = XNATClient(
        base_url=_URL,
        username="alice",
        password="pw",
        transport=_login_transport(calls),
    )

    with client as entered:
        assert entered is client
        assert client.session_token == "JSESSION_TOKEN"

    assert len(calls) == 1
    assert calls[0].url.path == "/data/JSESSION"


def test_enter_is_noop_with_existing_token() -> None:
    calls: list[httpx.Request] = []
    client = XNATClient(
        base_url=_URL,
        username="alice",
        password="pw",
        session_token="PRESET",
        transport=_login_transport(calls),
    )

    with client:
        pass

    assert client.session_token == "PRESET"
    assert calls == []


def test_enter_is_noop_without_credentials() -> None:
    calls: list[httpx.Request] = []
    client = XNATClient(base_url=_URL, transport=_login_transport(calls))

    with client:
        pass

    assert client.session_token is None
    assert calls == []
