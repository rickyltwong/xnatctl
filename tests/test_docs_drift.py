from __future__ import annotations

import re
from pathlib import Path

import click

from xnatctl.cli.main import cli

CommandPath = tuple[str, ...]

CLI_REFERENCE = Path(__file__).parent.parent / "docs" / "cli-reference.rst"

_LITERAL_RE = re.compile(r"``([a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)*)``")


def _walk(command: click.Command, path: CommandPath = ()) -> list[CommandPath]:
    if not isinstance(command, click.Group):
        return [path]
    found = [path] if path else []
    for name, sub in command.commands.items():
        if sub.hidden or name.startswith("_"):
            continue
        found.extend(_walk(sub, (*path, name)))
    return found


def _real_command_paths(root: click.Command = cli) -> set[CommandPath]:
    return {path for path in _walk(root) if path}


def _is_word_boundary_match(text: str, phrase: str) -> bool:
    pattern = r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])"
    return re.search(pattern, text) is not None


def _missing_commands(real_paths: set[CommandPath], text: str) -> set[str]:
    return {
        " ".join(path) for path in real_paths if not _is_word_boundary_match(text, " ".join(path))
    }


def _command_literals(text: str, top_level_names: set[str]) -> set[CommandPath]:
    literals: set[CommandPath] = set()
    for match in _LITERAL_RE.finditer(text):
        tokens = tuple(match.group(1).split(" "))
        if tokens[0] in top_level_names:
            literals.add(tokens)
    return literals


def _stale_literals(real_paths: set[CommandPath], text: str) -> set[str]:
    stale: set[str] = set()
    top_level_names = {path[0] for path in real_paths if len(path) == 1}
    for tokens in _command_literals(text, top_level_names):
        prefix = tokens[: min(2, len(tokens))]
        if prefix not in real_paths:
            stale.add(" ".join(tokens))
    return stale


class TestCliReferenceCoverage:
    def test_every_command_is_documented(self) -> None:
        text = CLI_REFERENCE.read_text(encoding="utf-8")
        missing = _missing_commands(_real_command_paths(), text)

        assert not missing, (
            f"commands missing from docs/cli-reference.rst: {sorted(missing)}. "
            "Add a bullet (and ideally an example) for each."
        )

    def test_no_documented_command_is_stale(self) -> None:
        text = CLI_REFERENCE.read_text(encoding="utf-8")
        stale = _stale_literals(_real_command_paths(), text)

        assert not stale, (
            f"docs/cli-reference.rst references commands that no longer exist: "
            f"{sorted(stale)}. Update or remove the reference."
        )


class TestDriftDetectionItself:
    def test_an_undocumented_command_is_flagged(self) -> None:
        real = {("foo",), ("foo", "bar")}
        text = "Only ``foo`` is mentioned here."

        assert _missing_commands(real, text) == {"foo bar"}

    def test_a_prefix_substring_does_not_hide_a_missing_command(self) -> None:
        real = {("admin",), ("admin", "refresh-catalogs")}
        text = "Run ``admin refresh`` to start."

        assert _missing_commands(real, text) == {"admin refresh-catalogs"}

    def test_a_shorter_command_is_not_satisfied_by_its_longer_sibling(self) -> None:
        real = {("admin",), ("admin", "refresh"), ("admin", "refresh-catalogs")}
        text = "Run ``admin refresh-catalogs`` nightly."

        assert _missing_commands(real, text) == {"admin refresh"}

    def test_a_fabricated_third_token_under_a_real_group_is_not_caught(self) -> None:
        real = {("admin",), ("admin", "user")}
        text = "``admin user made-up-command`` is invented."

        assert _stale_literals(real, text) == set()

    def test_a_literal_with_a_trailing_argument_is_not_flagged_as_stale(self) -> None:
        real = {("project",), ("project", "show")}
        text = "``project show demo-project`` prints one project's detail."

        assert _stale_literals(real, text) == set()

    def test_a_reference_to_a_command_that_never_existed_is_stale(self) -> None:
        real = {("project",), ("project", "show")}
        text = "``project archive`` is not a real command."

        assert _stale_literals(real, text) == {"project archive"}

    def test_a_fully_removed_groups_leftover_prose_is_not_caught_here(self) -> None:
        real = {("project",), ("project", "show")}
        text = "``xsync sync`` triggers a run."

        assert _stale_literals(real, text) == set()
