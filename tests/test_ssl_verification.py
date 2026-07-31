"""TLS verification must warn loudly, parse strictly, and support ca_bundle."""

from __future__ import annotations

import logging
import ssl

import certifi
import pytest

from xnatctl.core.client import XNATClient
from xnatctl.core.config import ENV_VERIFY_SSL, Profile, _parse_bool_env
from xnatctl.core.exceptions import ConfigurationError


def test_disabled_tls_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    client = XNATClient(base_url="https://xnat.example.org", verify_ssl=False)
    with caplog.at_level(logging.WARNING, logger="xnatctl.core.client"):
        client._get_client()
    assert any("DISABLED" in r.message for r in caplog.records)


def test_ca_bundle_suppresses_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A ca_bundle is a secure path -- no scary warning even with verify_ssl False."""
    client = XNATClient(
        base_url="https://xnat.example.org", verify_ssl=False, ca_bundle=certifi.where()
    )
    with caplog.at_level(logging.WARNING, logger="xnatctl.core.client"):
        client._get_client()
    assert not any("DISABLED" in r.message for r in caplog.records)


def test_ca_bundle_flows_to_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeHttpxClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("xnatctl.core.client.httpx.Client", FakeHttpxClient)
    XNATClient(base_url="https://xnat.example.org", ca_bundle=certifi.where())._get_client()
    # httpx 0.28 deprecates a bare path; we hand it a verified SSLContext instead.
    assert isinstance(captured["verify"], ssl.SSLContext)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("True ", True),  # surrounding whitespace is stripped, not rejected
        ("false", False),
        ("0", False),
        ("no", False),
    ],
)
def test_verify_ssl_env_valid(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv(ENV_VERIFY_SSL, raw)
    assert _parse_bool_env(ENV_VERIFY_SSL, True) is expected


@pytest.mark.parametrize("raw", ["on", "off", "", "garbage", "banana"])
def test_verify_ssl_env_invalid_raises(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(ENV_VERIFY_SSL, raw)
    with pytest.raises(ConfigurationError):
        _parse_bool_env(ENV_VERIFY_SSL, True)


def test_verify_ssl_env_unset_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VERIFY_SSL, raising=False)
    assert _parse_bool_env(ENV_VERIFY_SSL, True) is True
    assert _parse_bool_env(ENV_VERIFY_SSL, False) is False


def test_ca_bundle_profile_roundtrip() -> None:
    profile = Profile(url="https://xnat.example.org", ca_bundle="/etc/ssl/ca.pem")
    restored = Profile.from_dict(profile.to_dict())
    assert restored.ca_bundle == "/etc/ssl/ca.pem"


def test_no_ca_bundle_absent_from_serialization() -> None:
    profile = Profile(url="https://xnat.example.org")
    assert "ca_bundle" not in profile.to_dict()
