"""Tests for the ServerInfo/UserInfo introspection models.

These pin the two contracts the models exist for: the alias round-trip
(``model_validate`` on the raw wire payload, ``model_dump(by_alias=True)``
reproducing it) and the plain ``model_dump()`` matching the historical whoami/
ping dict shapes the CLI renders.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import ValidationError

from xnatctl.models.info import ServerInfo, UserInfo


class TestUserInfo:
    def test_alias_round_trip_reproduces_wire_keys(self) -> None:
        """model_validate(wire) -> model_dump(by_alias=True) == wire."""
        wire = {
            "username": "jdoe",
            "firstName": "Jane",
            "lastName": "Doe",
            "email": "jdoe@example.org",
            "enabled": True,
        }

        info = UserInfo.model_validate(wire)

        assert info.model_dump(by_alias=True) == wire

    def test_plain_dump_matches_historical_whoami_shape(self) -> None:
        """model_dump() carries the exact keys the whoami dict always had."""
        info = UserInfo(username="jdoe", firstname="Jane", lastname="Doe")

        assert info.model_dump() == {
            "username": "jdoe",
            "firstname": "Jane",
            "lastname": "Doe",
            "email": "",
            "enabled": False,
        }

    def test_accepts_field_names_too(self) -> None:
        """populate_by_name: the plain field names validate as well."""
        info = UserInfo.model_validate({"username": "jdoe", "firstname": "Jane"})

        assert info.firstname == "Jane"

    def test_extra_wire_keys_are_ignored(self) -> None:
        """Real /xapi/users payloads carry more keys (id, secured, ...)."""
        info = UserInfo.model_validate({"username": "jdoe", "id": 7, "secured": True})

        assert info.username == "jdoe"

    def test_frozen(self) -> None:
        info = UserInfo(username="jdoe")

        with pytest.raises(ValidationError):
            info.username = "other"  # type: ignore[misc]

    def test_explicit_null_name_and_email_fields_normalize_to_empty_string(self) -> None:
        """XNAT sends explicit ``null`` (not an absent key) for unset
        firstName/lastName/email on service/API accounts -- normal, not
        malformed. A default only applies to an absent key, so without the
        field validator this payload would raise ``ValidationError``.
        """
        info = UserInfo.model_validate(
            {
                "username": "svc_account",
                "firstName": None,
                "lastName": None,
                "email": None,
                "enabled": False,
            }
        )

        assert info.firstname == ""
        assert info.lastname == ""
        assert info.email == ""
        assert info.enabled is False

    def test_non_string_junk_in_name_fields_normalizes_to_empty_string(self) -> None:
        """A non-string, non-null value in an optional name/email field is
        absorbed the same way ``None`` is, rather than failing validation.
        """
        info = UserInfo.model_validate({"username": "jdoe", "firstName": 12345, "email": []})

        assert info.firstname == ""
        assert info.email == ""


class TestServerInfo:
    def test_dump_matches_historical_ping_shape(self) -> None:
        info = ServerInfo(
            url="https://xnat.example.org", status="ok", version="1.8.10", latency_ms=12
        )

        assert info.model_dump() == {
            "url": "https://xnat.example.org",
            "status": "ok",
            "version": "1.8.10",
            "latency_ms": 12,
        }

    def test_round_trip(self) -> None:
        payload = {
            "url": "https://xnat.example.org",
            "status": "ok",
            "version": None,
            "latency_ms": 3,
        }

        assert ServerInfo.model_validate(payload).model_dump() == payload

    def test_frozen(self) -> None:
        info = ServerInfo(url="https://xnat.example.org", status="ok", latency_ms=1)

        with pytest.raises(ValidationError):
            info.status = "down"  # type: ignore[misc]


def test_info_models_resolve_lazily_from_the_package_root() -> None:
    """`from xnatctl import ServerInfo, UserInfo` goes through the PEP 562 hook.

    Subprocess for the same reason as tests/test_lazy_imports.py: the exports
    must resolve without eagerly importing the heavy stack on bare
    ``import xnatctl``.
    """
    code = (
        "import sys\n"
        "import xnatctl\n"
        "heavy = [m for m in ('rich', 'click', 'httpx') if m in sys.modules]\n"
        "assert heavy == [], heavy\n"
        "from xnatctl import ServerInfo, UserInfo\n"
        "assert ServerInfo.__name__ == 'ServerInfo'\n"
        "assert UserInfo.__name__ == 'UserInfo'\n"
        "assert 'ServerInfo' in xnatctl.__all__ and 'UserInfo' in xnatctl.__all__\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.stdout.strip() == "ok"
