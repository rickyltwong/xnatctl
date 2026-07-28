"""Tests for ``xnatctl.core.redact.redact_url_query``."""

from __future__ import annotations

from xnatctl.core.redact import SECRET_QUERY_KEYS, redact_url_query, redact_url_userinfo


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


# =============================================================================
# URL userinfo (SEC-09)
# =============================================================================


def test_userinfo_password_redacted_without_query_string() -> None:
    """The gap SEC-09 opened on: credentials in the authority passed straight
    through, because the helper only ever looked at query strings."""
    out = redact_url_query("https://admin:s3cret@xnat.example.org/data/projects")
    assert "s3cret" not in out
    assert out == "https://admin:***@xnat.example.org/data/projects"


def test_userinfo_and_query_secret_both_redacted() -> None:
    out = redact_url_query("https://admin:s3cret@xnat.example.org/x?token=abc")
    assert "s3cret" not in out
    assert "abc" not in out
    assert out == "https://admin:***@xnat.example.org/x?token=***"


def test_userinfo_password_containing_at_sign_fully_redacted() -> None:
    """The authority runs to its *last* ``@``; a naive split would leave the
    tail of the password in place."""
    out = redact_url_query("https://u:p@ss@host/x")
    assert "p@ss" not in out
    assert "ss@host" not in out
    assert out == "https://u:***@host/x"


def test_bare_username_in_url_is_preserved() -> None:
    """``user@host`` carries no password, and usernames are not secrets here --
    the query pass keeps ``username=admin`` for the same reason."""
    text = "https://user@xnat.example.org/x"
    assert redact_url_query(text) == text


def test_userinfo_redacted_inside_a_longer_message() -> None:
    msg = "Connection to https://admin:s3cret@xnat.example.org failed, retrying."
    out = redact_url_query(msg)
    assert "s3cret" not in out
    assert out.startswith("Connection to ")
    assert out.endswith(" failed, retrying.")


def test_redact_url_userinfo_is_scheme_agnostic() -> None:
    """``redact_url_query`` only scans http(s); validation errors can carry a
    rejected value of any scheme."""
    assert redact_url_userinfo("ftp://admin:s3cret@host/x") == "ftp://admin:***@host/x"


def test_redact_url_userinfo_leaves_credential_free_urls_alone() -> None:
    for url in ("https://xnat.example.org/x", "https://user@host/x", "not a url at all"):
        assert redact_url_userinfo(url) == url


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
