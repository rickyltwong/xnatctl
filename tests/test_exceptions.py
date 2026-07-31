"""Error-rendering contract for the exception hierarchy.

Two changes are pinned here. ``str(exc)`` is now the message alone -- it used
to append the details dict, so users saw
``Profile not found: prod (field=profile, value='prod')`` where the suffix only
restated the message. And exceptions with a clear next step now carry a
``hint``, rendered as a dimmed ``Try:`` line instead of being hand-written at
whichever call site happened to think of it.
"""

from __future__ import annotations

import re

import pytest

from xnatctl.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    NetworkError,
    PermissionDeniedError,
    ProfileNotFoundError,
    ResourceNotFoundError,
    ServerUnreachableError,
    SessionExpiredError,
    ValidationError,
    XNATCtlError,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """Strip Rich colour codes and soft-wrap newlines."""
    return _ANSI_RE.sub("", text).replace("\n", " ")


# =============================================================================
# str() is the message, nothing else
# =============================================================================


def test_profile_not_found_has_no_details_suffix() -> None:
    assert str(ProfileNotFoundError("nonexistent")) == "Profile not found: nonexistent"


def test_resource_not_found_has_no_details_suffix() -> None:
    assert str(ResourceNotFoundError("subject", "SUB001")) == "subject not found: SUB001"


def test_session_expired_does_not_repeat_the_url() -> None:
    """It read `Authentication failed for https://x: Session expired ... (url=https://x)`
    -- the URL twice, once as noise."""
    message = str(SessionExpiredError("https://xnat.example.org"))

    assert message.count("https://xnat.example.org") == 1
    assert "url=" not in message


def test_details_survive_for_verbose_output() -> None:
    """Dropping the suffix must not drop the data behind it."""
    error = ProfileNotFoundError("nonexistent")

    assert error.details == {"field": "profile", "value": "'nonexistent'"}


def test_message_attribute_and_str_agree() -> None:
    error = ValidationError("bad input", field="name", value="x")

    assert str(error) == error.message == "bad input"


# =============================================================================
# Hints
# =============================================================================


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProfileNotFoundError("x"), "xnatctl config show"),
        (AuthenticationError("https://x.example.org"), "xnatctl auth login"),
        (SessionExpiredError("https://x.example.org"), "xnatctl auth login"),
        (PermissionDeniedError("SUB001", "delete"), "role"),
        (ResourceNotFoundError("subject", "SUB001"), "-P/--project"),
        (ServerUnreachableError("https://x.example.org"), "VPN"),
    ],
)
def test_hint_names_a_next_step(error: XNATCtlError, expected: str) -> None:
    assert error.hint is not None
    assert expected in error.hint


def test_errors_without_a_clear_next_step_have_no_hint() -> None:
    """A vague hint is worse than none, so the default stays None."""
    assert XNATCtlError("something went wrong").hint is None
    assert ConfigurationError("malformed YAML").hint is None
    assert NetworkError("https://x.example.org", "connection reset").hint is None


def test_instance_can_override_the_class_hint() -> None:
    error = XNATCtlError("boom", hint="Do the specific thing instead.")

    assert error.hint == "Do the specific thing instead."


def test_session_expired_hint_is_more_specific_than_its_parent() -> None:
    assert SessionExpiredError("https://x").hint != AuthenticationError("https://x").hint


# =============================================================================
# Rendering
# =============================================================================


def test_error_and_hint_are_two_clean_lines(capsys: pytest.CaptureFixture[str]) -> None:
    from xnatctl.cli.common import render_cli_error

    try:
        raise ProfileNotFoundError("nonexistent")
    except ProfileNotFoundError as e:
        render_cli_error(e)

    err = plain(capsys.readouterr().err)
    assert "Error: Profile not found: nonexistent" in err
    assert "Try: Run 'xnatctl config show'" in err
    assert "field=profile" not in err, "details must not appear without --verbose"


def test_details_appear_under_debug(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from xnatctl.cli.common import render_cli_error

    monkeypatch.setenv("XNATCTL_DEBUG", "1")

    try:
        raise ProfileNotFoundError("nonexistent")
    except ProfileNotFoundError as e:
        render_cli_error(e)

    err = plain(capsys.readouterr().err)
    assert "Details:" in err
    assert "field=profile" in err


def test_hintless_error_renders_only_the_message(capsys: pytest.CaptureFixture[str]) -> None:
    from xnatctl.cli.common import render_cli_error

    try:
        raise ConfigurationError("malformed YAML at line 3")
    except ConfigurationError as e:
        render_cli_error(e)

    err = plain(capsys.readouterr().err)
    assert "malformed YAML at line 3" in err
    assert "Try:" not in err


def test_hint_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    """Hints are ordinary strings and could carry a URL."""
    from xnatctl.core.output import print_hint

    print_hint("Retry against https://xnat.example.org/x?token=s3cret")

    err = plain(capsys.readouterr().err)
    assert "s3cret" not in err
    assert "token=***" in err
