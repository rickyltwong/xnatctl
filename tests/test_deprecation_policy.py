"""The deprecation policy is only worth anything if it is enforced.

Before this, three deprecated flags were accepted in total silence and a
fourth warned through ``warnings.warn(DeprecationWarning)`` -- which Python
hides by default, so the user saw nothing while the docs promised removal.
Nothing stopped the next one going the same way.

These tests walk the real command tree, so a deprecation added later without
registering it fails here rather than surfacing as a broken script after the
flag is deleted. The policy itself is written up in ``docs/stability.rst``.
"""

from __future__ import annotations

import re

import click
import pytest
from click.testing import CliRunner

from xnatctl.cli.common import DEPRECATED_FLAGS, deprecation_message
from xnatctl.cli.main import cli

CommandPath = tuple[str, ...]


def _walk(
    command: click.Command, path: CommandPath = ()
) -> list[tuple[CommandPath, click.Command]]:
    """Yield every leaf command in the tree with the path used to invoke it."""
    if isinstance(command, click.Group):
        found: list[tuple[CommandPath, click.Command]] = []
        for name, sub in command.commands.items():
            found.extend(_walk(sub, (*path, name)))
        return found
    return [(path, command)]


def _deprecated_options() -> list[tuple[CommandPath, click.Parameter, str]]:
    """Find every option the CLI presents as deprecated.

    Detection is by the flag's own help text and hidden state rather than by
    the registry, so the registry cannot be the thing that decides what counts
    as deprecated -- otherwise an unregistered flag would simply be invisible
    to the check it is supposed to fail.
    """
    found: list[tuple[CommandPath, click.Parameter, str]] = []
    for path, command in _walk(cli):
        for param in command.params:
            help_text = getattr(param, "help", None) or ""
            if "deprecat" not in help_text.lower():
                continue
            # The long form is what a user types and what the registry keys on.
            long_opts = [o for o in param.opts if o.startswith("--")]
            if long_opts:
                found.append((path, param, long_opts[0]))
    return found


def _wired_alias_params() -> list[tuple[CommandPath, click.Parameter, str]]:
    """Every option whose callback closure carries a baked deprecation warning.

    Most deprecated aliases carry no help at all (they are hidden), so the
    help-text sweep above cannot see them. The callback closure can: the
    message it was built with names the flag. Detection is by the closure's
    literal ``Warning: `` string, NOT by consulting the registry -- built
    the other way around (iterating the registry and looking for its
    messages), an alias wired with an unregistered flag or hand-baked text
    would simply be invisible to the checks it is supposed to fail.
    """
    found: list[tuple[CommandPath, click.Parameter, str]] = []
    for path, command in _walk(cli):
        for param in command.params:
            callback = getattr(param, "callback", None)
            closure = getattr(callback, "__closure__", None)
            if not closure:
                continue
            for cell in closure:
                value = cell.cell_contents
                if isinstance(value, str) and value.startswith("Warning: "):
                    found.append((path, param, value))
    return found


def _alias_callback_flags() -> set[str]:
    """Registered flags whose exact canonical warning is actually wired up.

    This matches by the *whole* canonical message (``deprecation_message``
    built fresh from each registry entry), not by picking the flag name back
    out of free text with a regex. A first-token regex assumed every
    registered key was itself the literal flag text with no internal
    whitespace, which holds for a full long-flag rename (``--file``) but
    not for a short-flag-only retirement scoped to one command (e.g. ``-f
    (api post/put)``, needed because ``-f`` means something different --
    and gets a different replacement -- in another command). Matching on the
    full message avoids that assumption, and is strictly stronger: it also
    catches a callback whose baked-in text has drifted from what the
    registry would produce today.
    """
    wired_messages = {message for _path, _param, message in _wired_alias_params()}
    return {flag for flag in DEPRECATED_FLAGS if deprecation_message(flag) in wired_messages}


class TestRegistryCoverage:
    def test_every_deprecated_flag_is_registered(self) -> None:
        """An ad-hoc deprecation fails here instead of surprising a user later."""
        unregistered = {
            f"{flag} ({' '.join(path)})"
            for path, _param, flag in _deprecated_options()
            if flag not in DEPRECATED_FLAGS
        }

        assert not unregistered, (
            f"deprecated flags missing from DEPRECATED_FLAGS: {sorted(unregistered)}. "
            "Register them in xnatctl/cli/common.py so the warning can name a "
            "removal release."
        )

    def test_every_wired_warning_matches_a_registry_entry(self) -> None:
        """A baked warning that no registry entry would produce today is drift.

        Covers both an alias wired around the registry entirely (hand-baked
        text) and one whose text was built from the registry once but no
        longer matches it. Comparing wired -> registry, not the reverse:
        the reverse is a subset of the registry by construction and can
        never fail.
        """
        canonical = {deprecation_message(flag) for flag in DEPRECATED_FLAGS}
        stray = {msg for _path, _param, msg in _wired_alias_params() if msg not in canonical}

        assert not stray, sorted(stray)

    def test_alias_detection_sees_the_current_aliases(self) -> None:
        """The closure sweep must keep finding the aliases that exist.

        If a refactor changed how alias callbacks bake their message, every
        check built on ``_wired_alias_params`` would silently pass on an
        empty list; pin the current alias count so that failure mode is
        loud instead.
        """
        assert len(_wired_alias_params()) >= len(DEPRECATED_FLAGS)

    def test_the_registry_has_no_dead_entries(self) -> None:
        """A flag deleted from the CLI should not linger in the table."""
        live = {flag for _path, _param, flag in _deprecated_options()} | _alias_callback_flags()

        assert not set(DEPRECATED_FLAGS) - live, sorted(set(DEPRECATED_FLAGS) - live)

    def test_removal_releases_are_versions(self) -> None:
        for flag, entry in DEPRECATED_FLAGS.items():
            assert re.fullmatch(r"\d+\.\d+\.\d+", entry.removed_in), f"{flag}: {entry.removed_in}"
            assert re.fullmatch(r"\d+\.\d+\.\d+", entry.deprecated_in), (
                f"{flag}: {entry.deprecated_in}"
            )

    def test_removal_is_at_least_two_minor_releases_after_deprecation(self) -> None:
        """The policy promises a window, anchored to the deprecating release.

        Anchoring to the CURRENT version instead would be wrong twice over: an
        intermediate release (0.3.0 deprecates for 0.5.0, then 0.4.0 ships)
        would spuriously fail even though the promised window is intact, and
        the policy says the removal release is decided at deprecation time and
        does not move.
        """
        for flag, entry in DEPRECATED_FLAGS.items():
            d_major, d_minor, _ = (int(p) for p in entry.deprecated_in.split("."))
            r_major, r_minor, _ = (int(p) for p in entry.removed_in.split("."))
            assert (r_major, r_minor) >= (d_major, d_minor + 2), (
                f"{flag} was deprecated in {entry.deprecated_in} but is removed in "
                f"{entry.removed_in}, less than two MINOR releases later"
            )

    def test_no_flag_outlives_its_named_removal_release(self) -> None:
        """Reaching the named release with the flag still present fails the gate.

        This is what forces the actual deletion when the removal release is
        being cut: the version bump lands, and any flag whose ``removed_in``
        is now due turns the suite red until the flag (and its entry) go.
        """
        from importlib.metadata import version

        current = tuple(int(p) for p in version("xnatctl").split(".")[:3])

        for flag, entry in DEPRECATED_FLAGS.items():
            removal = tuple(int(p) for p in entry.removed_in.split("."))
            assert current < removal, (
                f"{flag} was scheduled for removal in {entry.removed_in}, but "
                f"{'.'.join(str(p) for p in current)} still carries it"
            )


class TestMessage:
    def test_a_replacement_is_named(self) -> None:
        message = deprecation_message("--file")

        assert "--file is deprecated" in message
        assert "use --output-file instead" in message

    def test_the_removal_release_is_named(self) -> None:
        """A user reading one warning line should know how long they have."""
        assert "will be removed in 0.7.0" in deprecation_message("--file")

    def test_an_unregistered_flag_raises(self) -> None:
        """Caught at import time, since the factories build the message eagerly."""
        with pytest.raises(KeyError):
            deprecation_message("--never-existed")


class TestWarningsAreVisible:
    """Warnings go to stderr, and only when the flag is actually used.

    The callbacks are exercised on a throwaway command rather than a real one
    so the assertions are about the deprecation mechanism and not about
    whichever command happens to host a flag today.
    """

    @staticmethod
    def _harness() -> click.Command:
        from xnatctl.cli.common import _make_alias_cb, _make_forwarding_alias_cb

        @click.command()
        @click.option("--follow", is_flag=True, default=False)
        @click.option("--output-file", default=None)
        @click.option(
            "-f",
            "legacy_follow_f",
            is_flag=True,
            hidden=True,
            expose_value=False,
            callback=_make_alias_cb("-f (container logs)", "follow", True),
        )
        @click.option(
            "--file",
            "legacy_file_flag",
            hidden=True,
            expose_value=False,
            callback=_make_forwarding_alias_cb("--file", "output_file"),
        )
        def command(follow: bool, output_file: str | None) -> None:
            click.echo(f"follow={follow} output_file={output_file}")

        return command

    def test_the_warning_is_printed(self) -> None:
        result = CliRunner().invoke(self._harness(), ["-f"])

        assert deprecation_message("-f (container logs)") in result.stderr

    def test_the_warning_lands_on_stderr_not_stdout(self) -> None:
        """Polluting stdout breaks a piped --output json or --quiet result."""
        result = CliRunner().invoke(self._harness(), ["-f"])

        assert "deprecated" not in result.stdout

    def test_an_unused_deprecated_flag_stays_quiet(self) -> None:
        """Merely defining the alias must not warn on every invocation."""
        result = CliRunner().invoke(self._harness(), [])

        assert result.stderr == ""

    def test_an_alias_still_does_what_it_used_to(self) -> None:
        """A deprecation that also broke the flag would be a removal in disguise."""
        result = CliRunner().invoke(self._harness(), ["-f"])

        assert "follow=True" in result.stdout

    def test_a_forwarding_alias_carries_the_value(self) -> None:
        result = CliRunner().invoke(self._harness(), ["--file", "out.zip"])

        assert "output_file=out.zip" in result.stdout
        assert deprecation_message("--file") in result.stderr

    def test_deprecated_flags_are_hidden_from_help(self) -> None:
        """Both detection paths: help-marked options AND hidden alias callbacks.

        The alias sweep is what gives this test teeth at HEAD -- the
        surviving aliases carry no help text, so ``_deprecated_options()``
        alone would iterate zero times and the test would assert nothing.
        """
        for path, param, flag in _deprecated_options():
            assert getattr(param, "hidden", False), (
                f"{flag} on '{' '.join(path)}' is deprecated but still advertised in --help"
            )
        for path, param, _message in _wired_alias_params():
            assert getattr(param, "hidden", False), (
                f"deprecated alias {param.opts} on '{' '.join(path)}' is advertised in --help"
            )
