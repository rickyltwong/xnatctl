from __future__ import annotations

import re

import pytest

from xnatctl.core.output import print_error, print_success, print_warning

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\n", "")


def test_error_keeps_square_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    print_error("Install it with: pip install 'pkg[extra]'")

    assert "pkg[extra]" in plain(capsys.readouterr().err)


def test_warning_keeps_square_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    print_warning("Try 'pkg[extra]' for extra support")

    assert "pkg[extra]" in plain(capsys.readouterr().err)


def test_success_keeps_square_brackets(capsys: pytest.CaptureFixture[str]) -> None:
    print_success("Wrote profile [prod]")

    assert "[prod]" in plain(capsys.readouterr().err)


def test_unclosed_bracket_does_not_raise(capsys: pytest.CaptureFixture[str]) -> None:
    print_error("Unexpected token [ in response")

    assert "[ in response" in plain(capsys.readouterr().err)


def test_error_still_redacts(capsys: pytest.CaptureFixture[str]) -> None:
    print_error("GET https://xnat.example.org/x?token=s3cret failed")

    err = plain(capsys.readouterr().err)
    assert "s3cret" not in err
    assert "token=***" in err
