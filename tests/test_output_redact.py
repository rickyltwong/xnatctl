"""Tests for ``xnatctl.core.redact.redact_url_query``."""

from __future__ import annotations

from xnatctl.core.redact import SECRET_QUERY_KEYS, redact_url_query


def test_redacts_password_query_value() -> None:
    """A ``password=`` query value is replaced with ``***``."""
    out = redact_url_query("https://xnat.example.org/x?password=hunter2")
    assert "hunter2" not in out
    assert "password=***" in out


def test_redaction_is_case_insensitive_on_key() -> None:
    """Key match is case-insensitive; value is rewritten regardless of case."""
    out = redact_url_query("https://xnat.example.org/x?Password=hunter2&TOKEN=abc")
    assert "hunter2" not in out
    assert "abc" not in out
    assert "Password=***" in out
    assert "TOKEN=***" in out


def test_multiple_secret_keys_redacted_in_one_url() -> None:
    """Every secret-shaped key in a single URL is redacted."""
    url = "https://xnat.example.org/x?password=hunter2&token=abc&api_key=xyz&authorization=basic"
    out = redact_url_query(url)
    for leaked in ("hunter2", "abc", "xyz", "basic"):
        assert leaked not in out
    assert out.count("=***") == 4


def test_non_secret_keys_preserved() -> None:
    """Non-secret keys keep their original value verbatim."""
    out = redact_url_query(
        "https://xnat.example.org/x?username=admin&password=hunter2&columns=ID,name"
    )
    assert "username=admin" in out
    assert "columns=ID%2Cname" in out or "columns=ID,name" in out
    assert "password=***" in out
    assert "hunter2" not in out


def test_text_without_url_unchanged() -> None:
    """Free-form text without any URL is returned byte-for-byte."""
    text = "password=hunter2 is not in a URL, leave me alone"
    assert redact_url_query(text) == text


def test_url_without_secret_keys_unchanged() -> None:
    """A URL with only non-secret keys is returned byte-for-byte."""
    text = "GET https://xnat.example.org/data/projects?columns=ID,name failed"
    assert redact_url_query(text) == text


def test_multiple_urls_in_one_string_all_redacted() -> None:
    """Each URL substring is independently scrubbed."""
    text = "first https://a.example.org/x?password=one then https://b.example.org/y?token=two end"
    out = redact_url_query(text)
    assert "one" not in out
    assert "two" not in out
    assert "password=***" in out
    assert "token=***" in out


def test_url_embedded_in_error_message_redacted() -> None:
    """A URL embedded inside a longer error message is still scrubbed."""
    msg = (
        "Server returned 415 for "
        "https://xnat.example.org/xapi/x?username=admin&password=hunter2 "
        "(check your credentials)."
    )
    out = redact_url_query(msg)
    assert "hunter2" not in out
    assert "username=admin" in out
    assert "password=***" in out
    assert out.startswith("Server returned 415 for ")
    assert out.endswith("(check your credentials).")


def test_secret_query_keys_membership() -> None:
    """The canonical set matches the contract exactly."""
    expected = {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
    }
    assert SECRET_QUERY_KEYS == expected
