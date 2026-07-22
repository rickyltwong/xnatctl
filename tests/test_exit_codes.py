"""CLI-04: differentiated, documented exit codes via exit_code_for + handle_errors."""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from xnatctl.cli.common import ExitCode, exit_code_for, handle_errors
from xnatctl.core.exceptions import (
    AuthenticationError,
    ClientRequestError,
    ConfigurationError,
    NetworkError,
    PermissionDeniedError,
    ProfileNotFoundError,
    ResourceNotFoundError,
    RetryExhaustedError,
    ServerError,
    ServerUnreachableError,
    SessionExpiredError,
    ValidationError,
)
from xnatctl.core.exceptions import (
    ConnectionError as XNATConnectionError,
)
from xnatctl.core.exceptions import (
    TimeoutError as XNATTimeoutError,
)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (SessionExpiredError(), ExitCode.AUTH_ERROR),
        (AuthenticationError("https://x"), ExitCode.AUTH_ERROR),
        (PermissionDeniedError("proj", "delete"), ExitCode.PERMISSION_ERROR),
        (ResourceNotFoundError("session", "X"), ExitCode.NOT_FOUND),
        (NetworkError("https://x"), ExitCode.NETWORK_ERROR),
        (XNATTimeoutError("https://x", 5), ExitCode.NETWORK_ERROR),
        (RetryExhaustedError("request", 3), ExitCode.NETWORK_ERROR),
        (ServerUnreachableError("https://x"), ExitCode.NETWORK_ERROR),
        (XNATConnectionError("boom"), ExitCode.NETWORK_ERROR),
        (ClientRequestError(409, "POST", "/x"), ExitCode.GENERAL_ERROR),
        (ServerError(500, "GET", "/x"), ExitCode.GENERAL_ERROR),
        (ConfigurationError("bad"), ExitCode.GENERAL_ERROR),
        (ProfileNotFoundError("nope"), ExitCode.GENERAL_ERROR),
        (ValidationError("bad"), ExitCode.GENERAL_ERROR),
        (click.Abort(), ExitCode.USER_CANCELLED),
        (ValueError("unexpected"), ExitCode.GENERAL_ERROR),
    ],
)
def test_exit_code_for(exc: BaseException, expected: int) -> None:
    assert exit_code_for(exc) == expected


def test_permission_denied_beats_auth_error() -> None:
    """PermissionDeniedError subclasses AuthenticationError; the specific code wins."""
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
    """Declining a destructive-op confirmation maps to USER_CANCELLED (7)."""
    from xnatctl.cli.common import confirm_destructive

    @click.command()
    @handle_errors
    @confirm_destructive("Delete it?")
    def cmd(dry_run: bool) -> None:
        click.echo("deleted")

    result = CliRunner().invoke(cmd, input="n\n")
    assert result.exit_code == 7
    assert "deleted" not in result.output
