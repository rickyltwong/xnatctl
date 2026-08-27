"""Terminal accessibility: NO_COLOR / CLICOLOR=0 / --no-color, and color-only status signaling.

Covers two things: xnatctl honors the NO_COLOR and CLICOLOR=0 environment-variable
conventions plus its own ``--no-color`` flag, and no CLI status is conveyed by color
alone (a word or symbol always ships alongside any styling).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from conftest import AuthenticatedCLI
from rich.console import ColorSystem, Console

from xnatctl.core import output as output_module
from xnatctl.core.output import no_color_requested, set_no_color
from xnatctl.models.info import UserInfo

ESCAPE = "\x1b["


# =============================================================================
# no_color_requested() -- pure env parsing
# =============================================================================


class TestNoColorRequested:
    """NO_COLOR: any non-empty value disables color. CLICOLOR=0 disables it too."""

    def test_no_env_vars_means_color_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("CLICOLOR", raising=False)
        assert no_color_requested() is False

    @pytest.mark.parametrize("value", ["1", "true", "0", "anything"])
    def test_no_color_any_nonempty_value_disables(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        # Per https://no-color.org, NO_COLOR=0 still counts as "set" -- unlike
        # CLICOLOR, its presence (not its value) is the signal.
        monkeypatch.setenv("NO_COLOR", value)
        monkeypatch.delenv("CLICOLOR", raising=False)
        assert no_color_requested() is True

    def test_no_color_empty_string_does_not_disable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "")
        monkeypatch.delenv("CLICOLOR", raising=False)
        assert no_color_requested() is False

    def test_clicolor_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("CLICOLOR", "0")
        assert no_color_requested() is True

    @pytest.mark.parametrize("value", ["1", ""])
    def test_clicolor_other_values_do_not_disable(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("CLICOLOR", value)
        assert no_color_requested() is False


# =============================================================================
# set_no_color() -- mutates both singletons in place
# =============================================================================


def test_set_no_color_mutates_both_consoles() -> None:
    set_no_color(True)
    assert output_module.console.no_color is True
    assert output_module.err_console.no_color is True

    set_no_color(False)
    assert output_module.console.no_color is False
    assert output_module.err_console.no_color is False


# =============================================================================
# CliRunner integration: non-TTY output never carries escapes
# =============================================================================


def _seed_project_list(authenticated_cli: AuthenticatedCLI) -> None:
    authenticated_cli.client.get_json.return_value = [
        {
            "ID": "PROJ1",
            "name": "Project One",
            "pi_lastname": "Smith",
            "description": "Test project",
        }
    ]


def test_table_command_no_tty_has_no_escapes(authenticated_cli: AuthenticatedCLI) -> None:
    """A CliRunner invocation is never a real terminal, so Rich emits no ANSI
    codes at all -- the baseline every other test in this file builds on.
    """
    _seed_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["project", "list"])
    assert result.exit_code == 0
    assert ESCAPE not in result.output


# =============================================================================
# NO_COLOR / CLICOLOR=0 / --no-color must beat a real, color-capable terminal
# =============================================================================


@pytest.fixture
def force_terminal_colors(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Simulate a real, color-capable terminal for console/err_console.

    conftest's autouse ``disable_rich_colour`` forces no_color=True and
    _force_terminal=False on every test so plain-text assertions elsewhere in
    the suite are independent of the developer's terminal. These tests exist
    to prove the *opposite* -- that NO_COLOR/CLICOLOR/--no-color suppress
    color that would otherwise render -- so they need a real positive control
    first: a console that would emit ANSI codes absent that handling.

    Detection has to be pinned, not just seeded: every invocation runs
    ``set_no_color``, which re-derives ``_color_system`` via
    ``_detect_color_system()`` from the live environment -- so a value set
    here directly would be clobbered mid-invoke, and the control would then
    depend on the runner's real ``TERM``. Patching the detection method
    keeps the control deterministic on any platform while still exercising
    the real re-detection code path.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(Console, "_detect_color_system", lambda self: ColorSystem.STANDARD)
    for con in (output_module.console, output_module.err_console):
        monkeypatch.setattr(con, "_force_terminal", True)
        monkeypatch.setattr(con, "_color_system", ColorSystem.STANDARD)
        monkeypatch.setattr(con, "no_color", False)
    yield


def test_positive_control_color_renders_by_default(
    authenticated_cli: AuthenticatedCLI,
    force_terminal_colors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check for the fixture above, and proof the CLI does not disable
    color when nothing asked for it: with NO_COLOR/CLICOLOR cleared and no
    --no-color flag, the table's bold header still renders as an escape.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    _seed_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["project", "list"])
    assert result.exit_code == 0
    assert ESCAPE in result.output


def test_no_color_env_suppresses_color(
    authenticated_cli: AuthenticatedCLI,
    force_terminal_colors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLICOLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    _seed_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["project", "list"])
    assert result.exit_code == 0
    assert ESCAPE not in result.output


def test_clicolor_zero_suppresses_color(
    authenticated_cli: AuthenticatedCLI,
    force_terminal_colors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("CLICOLOR", "0")
    _seed_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["project", "list"])
    assert result.exit_code == 0
    assert ESCAPE not in result.output


def test_no_color_flag_suppresses_color(
    authenticated_cli: AuthenticatedCLI,
    force_terminal_colors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    _seed_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["--no-color", "project", "list"])
    assert result.exit_code == 0
    assert ESCAPE not in result.output


def test_no_color_flag_after_subcommand_suppresses_color(
    authenticated_cli: AuthenticatedCLI,
    force_terminal_colors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-color is declared on every subcommand too, so it also works when
    placed after the subcommand name (matching --quiet/--verbose).
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    _seed_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["project", "list", "--no-color"])
    assert result.exit_code == 0
    assert ESCAPE not in result.output


# =============================================================================
# Color-only signaling: a failure row must carry a word, not just styling
# =============================================================================


def test_failure_status_row_carries_a_literal_word(
    authenticated_cli: AuthenticatedCLI,
) -> None:
    """Project transfer-check reports per-check status as literal OK/FAIL text
    in the table cell, not as color alone -- verified with color rendering
    off (the CliRunner default), where a color-only signal would be invisible.
    """
    from unittest.mock import MagicMock, patch

    authenticated_cli.client.ping.side_effect = RuntimeError("unreachable")
    authenticated_cli.client.whoami.return_value = UserInfo(username="srcuser", enabled=True)

    dest_client = MagicMock()
    dest_client.authenticate.side_effect = RuntimeError("bad creds")

    with patch("xnatctl.cli.project.create_dest_client", return_value=dest_client):
        result = authenticated_cli.invoke(
            [
                "project",
                "transfer-check",
                "-P",
                "SRC",
                "--dest-project",
                "DST",
                "--dest-profile",
                "staging",
            ]
        )

    assert result.exit_code != 0
    assert ESCAPE not in result.output
    assert "FAIL" in result.output
