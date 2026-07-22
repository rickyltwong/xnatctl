"""Help-text rendering guard (CLI-05).

Click rewraps a docstring paragraph into one block unless a line containing only
``\\b`` precedes it. Without those markers, multi-line ``Example:`` blocks
collapse into a run-on paragraph (``xnatctl a    xnatctl b``). This meta-test
walks the whole command tree and fails if any rendered help line carries two
command invocations, so the formatting cannot silently rot again.
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli


def _iter_commands(command: click.BaseCommand, path: list[str]):
    yield path, command
    if isinstance(command, click.Group):
        for name, sub in command.commands.items():
            yield from _iter_commands(sub, [*path, name])


def _all_commands() -> list[tuple[list[str], click.BaseCommand]]:
    return list(_iter_commands(cli, ["xnatctl"]))


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_no_rendered_help_line_carries_two_invocations(runner) -> None:
    """No rendered --help line may contain two `xnatctl ` command invocations."""
    offenders: list[str] = []
    for path, _command in _all_commands():
        result = runner.invoke(cli, [*path[1:], "--help"])
        if result.exit_code != 0:
            continue
        for line in result.output.splitlines():
            if line.count("xnatctl ") > 1:
                offenders.append(f"{' '.join(path)} :: {line.strip()}")

    assert not offenders, "Example blocks missing a \\b no-rewrap marker:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "args",
    [
        ["session", "download", "--help"],
        ["session", "upload-exam", "--help"],
        ["project", "list", "--help"],
    ],
)
def test_spot_check_examples_each_on_own_line(runner, args) -> None:
    """Each `xnatctl ` example in these commands starts its own rendered line."""
    result = runner.invoke(cli, args)
    assert result.exit_code == 0
    for line in result.output.splitlines():
        assert line.count("xnatctl ") <= 1, f"mashed example line in {' '.join(args)}: {line!r}"
