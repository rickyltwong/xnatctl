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
    if isinstance(command, click.Group):
        found: list[tuple[CommandPath, click.Command]] = []
        for name, sub in command.commands.items():
            found.extend(_walk(sub, (*path, name)))
        return found
    return [(path, command)]


def _deprecated_options() -> list[tuple[CommandPath, click.Parameter, str]]:
    found: list[tuple[CommandPath, click.Parameter, str]] = []
    for path, command in _walk(cli):
        for param in command.params:
            help_text = getattr(param, "help", None) or ""
            if "deprecat" not in help_text.lower():
                continue
            long_opts = [o for o in param.opts if o.startswith("--")]
            if long_opts:
                found.append((path, param, long_opts[0]))
    return found


def _wired_alias_params() -> list[tuple[CommandPath, click.Parameter, str]]:
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
    wired_messages = {message for _path, _param, message in _wired_alias_params()}
    return {flag for flag in DEPRECATED_FLAGS if deprecation_message(flag) in wired_messages}


class TestRegistryCoverage:
    def test_every_deprecated_flag_is_registered(self) -> None:
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
        canonical = {deprecation_message(flag) for flag in DEPRECATED_FLAGS}
        stray = {msg for _path, _param, msg in _wired_alias_params() if msg not in canonical}

        assert not stray, sorted(stray)

    def test_the_wired_alias_sites_are_exactly_the_expected_ones(self) -> None:
        expected = {
            (("scan", "delete"), "-s"),
            (("scan", "download"), "-s"),
            (("resource", "download"), "--file"),
            (("pipeline", "run"), "-e"),
            (("pipeline", "jobs"), "-e"),
            (("admin", "refresh-catalogs"), "-e"),
            (("api", "post"), "-f (api post/put)"),
            (("api", "put"), "-f (api post/put)"),
            (("container", "logs"), "-f (container logs)"),
        }
        canonical = {deprecation_message(flag): flag for flag in DEPRECATED_FLAGS}
        actual = {
            (path, canonical.get(message, message))
            for path, _param, message in _wired_alias_params()
        }

        assert actual == expected

    def test_the_registry_has_no_dead_entries(self) -> None:
        live = {flag for _path, _param, flag in _deprecated_options()} | _alias_callback_flags()

        assert not set(DEPRECATED_FLAGS) - live, sorted(set(DEPRECATED_FLAGS) - live)

    def test_removal_releases_are_versions(self) -> None:
        for flag, entry in DEPRECATED_FLAGS.items():
            assert re.fullmatch(r"\d+\.\d+\.\d+", entry.removed_in), f"{flag}: {entry.removed_in}"
            assert re.fullmatch(r"\d+\.\d+\.\d+", entry.deprecated_in), (
                f"{flag}: {entry.deprecated_in}"
            )

    def test_removal_is_at_least_two_minor_releases_after_deprecation(self) -> None:
        for flag, entry in DEPRECATED_FLAGS.items():
            d_major, d_minor, _ = (int(p) for p in entry.deprecated_in.split("."))
            r_major, r_minor, _ = (int(p) for p in entry.removed_in.split("."))
            assert (r_major, r_minor) >= (d_major, d_minor + 2), (
                f"{flag} was deprecated in {entry.deprecated_in} but is removed in "
                f"{entry.removed_in}, less than two MINOR releases later"
            )

    def test_no_flag_outlives_its_named_removal_release(self) -> None:
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
        assert "will be removed in 0.7.0" in deprecation_message("--file")

    def test_an_unregistered_flag_raises(self) -> None:
        with pytest.raises(KeyError):
            deprecation_message("--never-existed")


class TestWarningsAreVisible:
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
        result = CliRunner().invoke(self._harness(), ["-f"])

        assert "deprecated" not in result.stdout

    def test_an_unused_deprecated_flag_stays_quiet(self) -> None:
        result = CliRunner().invoke(self._harness(), [])

        assert result.stderr == ""

    def test_an_alias_still_does_what_it_used_to(self) -> None:
        result = CliRunner().invoke(self._harness(), ["-f"])

        assert "follow=True" in result.stdout

    def test_a_forwarding_alias_carries_the_value(self) -> None:
        result = CliRunner().invoke(self._harness(), ["--file", "out.zip"])

        assert "output_file=out.zip" in result.stdout
        assert deprecation_message("--file") in result.stderr

    def test_deprecated_flags_are_hidden_from_help(self) -> None:
        for path, param, flag in _deprecated_options():
            assert getattr(param, "hidden", False), (
                f"{flag} on '{' '.join(path)}' is deprecated but still advertised in --help"
            )
        for path, param, _message in _wired_alias_params():
            assert getattr(param, "hidden", False), (
                f"deprecated alias {param.opts} on '{' '.join(path)}' is advertised in --help"
            )
