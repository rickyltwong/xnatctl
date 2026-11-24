from __future__ import annotations

from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner
from conftest import config_seam

from xnatctl.cli.common import ExitCode, exit_code_for, handle_errors
from xnatctl.core.exceptions import (
    AuthenticationError,
    ClientRequestError,
    ConfigurationError,
    InputValidationError,
    NetworkError,
    PermissionDeniedError,
    ProfileNotFoundError,
    RequestTimeoutError,
    ResourceNotFoundError,
    RetryExhaustedError,
    ServerError,
    ServerUnreachableError,
    SessionExpiredError,
    XNATConnectionError,
)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (SessionExpiredError(), ExitCode.AUTH_ERROR),
        (AuthenticationError("https://x"), ExitCode.AUTH_ERROR),
        (PermissionDeniedError("proj", "delete"), ExitCode.PERMISSION_ERROR),
        (ResourceNotFoundError("session", "X"), ExitCode.NOT_FOUND),
        (NetworkError("https://x"), ExitCode.NETWORK_ERROR),
        (RequestTimeoutError("https://x", 5), ExitCode.NETWORK_ERROR),
        (RetryExhaustedError("request", 3), ExitCode.NETWORK_ERROR),
        (ServerUnreachableError("https://x"), ExitCode.NETWORK_ERROR),
        (XNATConnectionError("boom"), ExitCode.NETWORK_ERROR),
        (ClientRequestError(409, "POST", "/x"), ExitCode.GENERAL_ERROR),
        (ServerError(500, "GET", "/x"), ExitCode.GENERAL_ERROR),
        (ConfigurationError("bad"), ExitCode.GENERAL_ERROR),
        (ProfileNotFoundError("nope"), ExitCode.GENERAL_ERROR),
        (InputValidationError("bad"), ExitCode.GENERAL_ERROR),
        (click.Abort(), ExitCode.USER_CANCELLED),
        (ValueError("unexpected"), ExitCode.GENERAL_ERROR),
    ],
)
def test_exit_code_for(exc: BaseException, expected: int) -> None:
    assert exit_code_for(exc) == expected


def test_permission_denied_beats_auth_error() -> None:
    assert isinstance(PermissionDeniedError("r"), AuthenticationError)
    assert exit_code_for(PermissionDeniedError("r")) == ExitCode.PERMISSION_ERROR


def test_code_2_is_reserved_for_click() -> None:
    assert 2 not in {int(c) for c in ExitCode}


def _cmd(exc: BaseException | None) -> click.Command:
    @click.command()
    @handle_errors
    def cmd() -> None:
        if exc is not None:
            raise exc
        click.echo("ok")

    return cmd


@pytest.mark.parametrize(
    "exc,code",
    [
        (ResourceNotFoundError("session", "X"), 5),
        (SessionExpiredError(), 3),
        (PermissionDeniedError("p", "delete"), 6),
        (NetworkError("https://x"), 4),
        (click.Abort(), 7),
        (KeyboardInterrupt(), 7),
        (ValueError("boom"), 1),
    ],
)
def test_handle_errors_exit_codes(exc: BaseException, code: int) -> None:
    result = CliRunner().invoke(_cmd(exc))
    assert result.exit_code == code


def test_successful_command_exits_zero() -> None:
    result = CliRunner().invoke(_cmd(None))
    assert result.exit_code == 0
    assert "ok" in result.output


def test_confirm_decline_exits_user_cancelled() -> None:
    from xnatctl.cli.common import confirm_destructive

    @click.command()
    @handle_errors
    @confirm_destructive("Delete it?")
    def cmd(dry_run: bool) -> None:
        click.echo("deleted")

    result = CliRunner().invoke(cmd, input="n\n")
    assert result.exit_code == 7
    assert "deleted" not in result.output


def test_unexpected_error_no_traceback_by_default(monkeypatch) -> None:
    monkeypatch.delenv("XNATCTL_DEBUG", raising=False)
    result = CliRunner().invoke(_cmd(ValueError("boom")))
    assert result.exit_code == 1
    assert "Unexpected error" in result.output
    assert "Run with --verbose for a full traceback." in result.output
    assert "Traceback (most recent call last)" not in result.output


def test_unexpected_error_traceback_with_debug_env(monkeypatch) -> None:
    monkeypatch.setenv("XNATCTL_DEBUG", "1")
    result = CliRunner().invoke(_cmd(ValueError("boom")))
    assert result.exit_code == 1
    assert "Traceback (most recent call last)" in result.output
    assert "test_exit_codes.py" in result.output
    assert "Run with --verbose for a full traceback." not in result.output


def test_xnatctl_error_traceback_with_debug_env(monkeypatch) -> None:
    monkeypatch.setenv("XNATCTL_DEBUG", "1")
    result = CliRunner().invoke(_cmd(ResourceNotFoundError("session", "X")))
    assert result.exit_code == 5
    assert "Traceback (most recent call last)" in result.output


def test_xnatctl_error_no_hint_or_traceback_by_default(monkeypatch) -> None:
    monkeypatch.delenv("XNATCTL_DEBUG", raising=False)
    result = CliRunner().invoke(_cmd(ResourceNotFoundError("session", "X")))
    assert result.exit_code == 5
    assert "Traceback (most recent call last)" not in result.output
    assert "Run with --verbose for a full traceback." not in result.output


def test_verbose_flag_on_context_enables_traceback(monkeypatch) -> None:
    monkeypatch.delenv("XNATCTL_DEBUG", raising=False)

    from xnatctl.cli.common import Context, global_options

    @click.command()
    @global_options
    @handle_errors
    def cmd(ctx: Context) -> None:
        raise ValueError("boom")

    with config_seam(object()):
        result = CliRunner().invoke(cmd, ["--verbose"])
    assert result.exit_code == 1
    assert "Traceback (most recent call last)" in result.output


@pytest.mark.parametrize("falsey", ["0", "false", "FALSE", "no", "off", " ", ""])
def test_debug_env_falsey_values_do_not_enable_traceback(monkeypatch, falsey) -> None:
    monkeypatch.setenv("XNATCTL_DEBUG", falsey)
    result = CliRunner().invoke(_cmd(ValueError("boom")))
    assert result.exit_code == 1
    assert "Traceback (most recent call last)" not in result.output
    assert "Run with --verbose for a full traceback." in result.output


def test_debug_env_arbitrary_truthy_value_enables_traceback(monkeypatch) -> None:
    monkeypatch.setenv("XNATCTL_DEBUG", "api")
    result = CliRunner().invoke(_cmd(ValueError("boom")))
    assert "Traceback (most recent call last)" in result.output


def _run_main_raising(exc: BaseException) -> int:
    from xnatctl.cli import main as main_mod

    def _boom() -> None:
        raise exc

    with patch.object(main_mod, "cli", _boom):
        try:
            main_mod.main()
        except SystemExit as se:
            return int(se.code or 0)
    return 0


def test_main_guard_renders_xnatctl_error_without_traceback(monkeypatch, capsys) -> None:
    monkeypatch.delenv("XNATCTL_DEBUG", raising=False)
    code = _run_main_raising(ResourceNotFoundError("session", "X"))
    captured = capsys.readouterr()
    assert code == 5
    assert "Traceback (most recent call last)" not in (captured.out + captured.err)


def test_main_guard_renders_unexpected_error_with_hint(monkeypatch, capsys) -> None:
    monkeypatch.delenv("XNATCTL_DEBUG", raising=False)
    code = _run_main_raising(ValueError("bad XNAT_TIMEOUT"))
    captured = capsys.readouterr()
    assert code == 1
    assert "Unexpected error" in (captured.out + captured.err)
    assert "Run with --verbose for a full traceback." in (captured.out + captured.err)
    assert "Traceback (most recent call last)" not in (captured.out + captured.err)


def test_main_guard_shows_traceback_under_debug_env(monkeypatch, capsys) -> None:
    monkeypatch.setenv("XNATCTL_DEBUG", "1")
    code = _run_main_raising(ValueError("bad XNAT_TIMEOUT"))
    captured = capsys.readouterr()
    assert code == 1
    assert "Traceback (most recent call last)" in (captured.out + captured.err)


def test_interrupt_is_distinguishable_from_a_general_error() -> None:
    result = CliRunner().invoke(_cmd(KeyboardInterrupt()))

    assert result.exit_code == ExitCode.USER_CANCELLED
    assert result.exit_code != ExitCode.GENERAL_ERROR
    assert "Cancelled." in result.output


def test_keyboard_interrupt_is_not_an_exception_subclass() -> None:
    assert not isinstance(KeyboardInterrupt(), Exception)
    assert isinstance(KeyboardInterrupt(), BaseException)
