"""Rendering tests for the status-message helpers.

Square brackets in a message used to be parsed as Rich markup and silently
dropped, so an install hint like ``pip install 'xnatctl[keyring]'`` reached the
user as ``pip install 'xnatctl'`` -- the actionable half deleted. Found while
wiring the keyring install-hint error.
"""

from __future__ import annotations

import re

import pytest

from xnatctl.core.output import print_error, print_success, print_warning

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """Strip Rich's colour codes and soft-wrap newlines."""
    return _ANSI_RE.sub("", text).replace("\n", "")


def test_error_keeps_square_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    print_error("Install it with: pip install 'xnatctl[keyring]'")

    assert "xnatctl[keyring]" in plain(capsys.readouterr().err)


def test_warning_keeps_square_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    print_warning("Try 'xnatctl[dicom]' for DICOM support")

    assert "xnatctl[dicom]" in plain(capsys.readouterr().err)


def test_success_keeps_square_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    print_success("Wrote profile [prod]")

    # Success is status commentary, so it lives on stderr.
    assert "[prod]" in plain(capsys.readouterr().err)


def test_unclosed_bracket_does_not_raise(capsys: pytest.CaptureFixture[str]) -> None:
    """Exception text is arbitrary; an unbalanced bracket must not blow up the
    error path itself."""
    print_error("Unexpected token [ in response")

    assert "[ in response" in plain(capsys.readouterr().err)


def test_error_still_redacts(capsys: pytest.CaptureFixture[str]) -> None:
    """Escaping must not displace the redaction it wraps."""
    print_error("GET https://xnat.example.org/x?token=s3cret failed")

    err = plain(capsys.readouterr().err)
    assert "s3cret" not in err
    assert "token=***" in err
