from __future__ import annotations

from collections.abc import Iterator

import pytest
from conftest import AuthenticatedCLI
from rich.console import ColorSystem, Console

from xnatctl.core import output as output_module
from xnatctl.core.output import no_color_requested, set_no_color
from xnatctl.models.info import UserInfo

ESCAPE = "\x1b["


class TestNoColorRequested:
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


def test_set_no_color_mutates_both_consoles() -> None:
    set_no_color(True)
    assert output_module.console.no_color is True
    assert output_module.err_console.no_color is True

    set_no_color(False)
    assert output_module.console.no_color is False
    assert output_module.err_console.no_color is False


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
    _seed_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["project", "list"])
    assert result.exit_code == 0
    assert ESCAPE not in result.output


@pytest.fixture
def force_terminal_colors(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    _seed_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["project", "list", "--no-color"])
    assert result.exit_code == 0
    assert ESCAPE not in result.output


def test_failure_status_row_carries_a_literal_word(
    authenticated_cli: AuthenticatedCLI,
) -> None:
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
