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
    offenders: list[str] = []
    for path, _command in _all_commands():
        result = runner.invoke(cli, [*path[1:], "--help"])
        if result.exit_code != 0:
            continue
        for line in result.output.splitlines():
            if line.count("xnatctl ") > 1 and " | " not in line:
                offenders.append(f"{' '.join(path)} :: {line.strip()}")

    assert not offenders, "Example blocks missing a \\b no-rewrap marker:\n" + "\n".join(offenders)


def test_every_command_answers_help_without_a_profile(runner) -> None:
    # No profile is configured and no network is reachable: --help needs neither.
    failures = [
        f"{' '.join(path)}: {result.output.strip()}"
        for path, _command in _all_commands()
        if (result := runner.invoke(cli, [*path[1:], "--help"])).exit_code != 0
    ]

    assert not failures, "\n".join(failures)


@pytest.mark.parametrize(
    "args",
    [
        ["session", "download", "--help"],
        ["session", "upload-exam", "--help"],
        ["project", "list", "--help"],
    ],
)
def test_spot_check_examples_each_on_own_line(runner, args) -> None:
    result = runner.invoke(cli, args)
    assert result.exit_code == 0
    for line in result.output.splitlines():
        assert line.count("xnatctl ") <= 1, f"mashed example line in {' '.join(args)}: {line!r}"
