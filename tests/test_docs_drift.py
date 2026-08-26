"""The CLI reference is hand-maintained prose, not generated -- so nothing
stops it drifting from the actual command tree as commands are added, moved,
or removed.

These tests walk the real Click tree (the same technique
``tests/test_deprecation_policy.py`` uses) and cross-check it against
``docs/cli-reference.rst`` in both directions: every real command must be
mentioned there, and every command-shaped reference in there must still be
real. The matching rules themselves have edge cases sharp enough to need
their own tests -- see ``TestDriftDetectionItself`` below.
"""

from __future__ import annotations

import re
from pathlib import Path

import click

from xnatctl.cli.main import cli

CommandPath = tuple[str, ...]

CLI_REFERENCE = Path(__file__).parent.parent / "docs" / "cli-reference.rst"

# Inline literals (``...``) made of nothing but lowercase words and hyphens,
# e.g. ``session upload-exam`` or ``admin site-config get``. This excludes
# flags (``--dry-run``), placeholders (``PROJECT``), shell invocations
# (``xnatctl project list --output json``), and option values (``json``,
# ``DICOM``) without needing to special-case each -- flags start with ``-``,
# placeholders and option values are either uppercase or not top-level
# command names, and a full shell invocation's extra words fail the
# all-lowercase-words-only pattern the moment it hits ``--foo`` or an
# ALL-CAPS argument.
_LITERAL_RE = re.compile(r"``([a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)*)``")


def _walk(command: click.Command, path: CommandPath = ()) -> list[CommandPath]:
    """Every group and leaf command in the tree, as its full invocation path.

    Mirrors ``tests/test_deprecation_policy.py``'s ``_walk``, except it keeps
    the group nodes too (``("admin", "user")``, not just its leaves) since
    the reference documents groups as well as their sub-commands.
    """
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
    """Whether *phrase* appears in *text* as a whole command reference.

    Plain substring containment would let an undocumented ``admin refresh``
    slip through, because it is itself a substring of the real, documented
    ``admin refresh-catalogs``. Requiring no word character or hyphen
    immediately before or after the match rules that out, while still
    matching across the ``` `` ``/whitespace/punctuation that surrounds a
    real reference in prose.
    """
    pattern = r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])"
    return re.search(pattern, text) is not None


def _missing_commands(real_paths: set[CommandPath], text: str) -> set[str]:
    """Real command paths that never appear, as a whole reference, in *text*."""
    return {
        " ".join(path) for path in real_paths if not _is_word_boundary_match(text, " ".join(path))
    }


def _command_literals(text: str, top_level_names: set[str]) -> set[CommandPath]:
    """Command-shaped inline literals (double-backtick spans) found in *text*.

    A literal only counts as a *command* reference -- as opposed to some
    other all-lowercase-hyphenated literal, e.g. an option value -- when its
    first word names a real top-level command; otherwise it is left alone
    rather than guessed at.
    """
    literals: set[CommandPath] = set()
    for match in _LITERAL_RE.finditer(text):
        tokens = tuple(match.group(1).split(" "))
        if tokens[0] in top_level_names:
            literals.add(tokens)
    return literals


def _stale_literals(real_paths: set[CommandPath], text: str) -> set[str]:
    """Command-shaped literals in *text* that don't resolve against *real_paths*.

    Only the first two tokens of a literal have to resolve to a real command
    path -- anything past that is treated as a positional argument the
    example is passing (``project show demo-project``), not more of the
    command name.

    Known boundary: a fabricated third token under a real two-deep group
    (``admin user made-up-command``) is NOT caught here, since the two-token
    prefix ``admin user`` is itself real. Catching that precisely would mean
    reproducing Click's own argument parsing. Likewise, a whole group that
    is deleted outright leaves no trace here: the literal-extraction gate
    above only treats a first word as a command reference when it is a
    CURRENT real top-level command, so old prose about a removed group is
    never even considered a candidate. In practice that class of drift
    surfaces through ``_missing_commands`` instead -- whatever replaces the
    removed group is itself undocumented until the docs are edited, and
    that edit is what removes the stale section.
    """
    stale: set[str] = set()
    top_level_names = {path[0] for path in real_paths if len(path) == 1}
    for tokens in _command_literals(text, top_level_names):
        prefix = tokens[: min(2, len(tokens))]
        if prefix not in real_paths:
            stale.add(" ".join(tokens))
    return stale


class TestCliReferenceCoverage:
    """docs/cli-reference.rst must mention every real command, and only real commands."""

    def test_every_command_is_documented(self) -> None:
        """A command missing from the reference is invisible to users reading it."""
        text = CLI_REFERENCE.read_text(encoding="utf-8")
        missing = _missing_commands(_real_command_paths(), text)

        assert not missing, (
            f"commands missing from docs/cli-reference.rst: {sorted(missing)}. "
            "Add a bullet (and ideally an example) for each."
        )

    def test_no_documented_command_is_stale(self) -> None:
        """A command-shaped reference to something that no longer exists is stale docs."""
        text = CLI_REFERENCE.read_text(encoding="utf-8")
        stale = _stale_literals(_real_command_paths(), text)

        assert not stale, (
            f"docs/cli-reference.rst references commands that no longer exist: "
            f"{sorted(stale)}. Update or remove the reference."
        )


class TestDriftDetectionItself:
    """Pins down the matching rules above against synthetic fixtures.

    Independent of the live CLI tree and the real doc text, so a future
    change to the matching logic can't silently regress on any of these
    edge cases -- including the one it deliberately does not handle.
    """

    def test_an_undocumented_command_is_flagged(self) -> None:
        real = {("foo",), ("foo", "bar")}
        text = "Only ``foo`` is mentioned here."

        assert _missing_commands(real, text) == {"foo bar"}

    def test_a_prefix_substring_does_not_hide_a_missing_command(self) -> None:
        """``admin refresh`` must not satisfy a check for ``admin refresh-catalogs``."""
        real = {("admin",), ("admin", "refresh-catalogs")}
        text = "Run ``admin refresh`` to start."

        assert _missing_commands(real, text) == {"admin refresh-catalogs"}

    def test_a_shorter_command_is_not_satisfied_by_its_longer_sibling(self) -> None:
        """The inverse prefix direction: documented ``admin refresh-catalogs``
        must not satisfy a check for a NEW, undocumented ``admin refresh``.
        """
        real = {("admin",), ("admin", "refresh"), ("admin", "refresh-catalogs")}
        text = "Run ``admin refresh-catalogs`` nightly."

        assert _missing_commands(real, text) == {"admin refresh"}

    def test_a_fabricated_third_token_under_a_real_group_is_not_caught(self) -> None:
        """Documents the second accepted boundary: only the two-token prefix is
        validated, so a made-up third token under a real group reads as an
        argument, not a stale command. See the ``_stale_literals`` docstring.
        """
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
        """Documents the accepted boundary, rather than a bug.

        Once a whole group is gone, its first word is no longer a real
        top-level command, so the literal-extraction gate never treats its
        leftover doc text as a command reference at all. See the
        ``_stale_literals`` docstring for why this is acceptable.
        """
        real = {("project",), ("project", "show")}  # "xsync" no longer exists
        text = "``xsync sync`` triggers a run."

        assert _stale_literals(real, text) == set()
