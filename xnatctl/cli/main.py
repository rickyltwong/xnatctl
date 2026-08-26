"""Main CLI entry point for xnatctl."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import click
from click.shell_completion import get_completion_class

from xnatctl import __version__
from xnatctl.cli import (
    custom_vars as _custom_vars,  # noqa: F401  # registers vars/vars-set on the subject/session groups
)
from xnatctl.cli import (
    session_labels as _session_labels,  # noqa: F401  # registers normalize-labels on the session group
)
from xnatctl.cli import (
    session_query as _session_query,  # noqa: F401  # registers list/show commands on the session group
)
from xnatctl.cli import (
    session_upload as _session_upload,  # noqa: F401  # registers upload commands on the session group
)
from xnatctl.cli import (
    sharing as _sharing,  # noqa: F401  # registers share/unshare on the subject/session groups
)
from xnatctl.cli.admin import admin
from xnatctl.cli.anon import anon
from xnatctl.cli.api import api
from xnatctl.cli.auth import auth
from xnatctl.cli.command_cmd import command
from xnatctl.cli.common import Context, ExitCode, global_options, handle_errors, render_cli_error

# Import command groups
from xnatctl.cli.config_cmd import config
from xnatctl.cli.container import container
from xnatctl.cli.dicom_cmd import dicom
from xnatctl.cli.event import event
from xnatctl.cli.local import local
from xnatctl.cli.pipeline import pipeline
from xnatctl.cli.prearchive import prearchive
from xnatctl.cli.project import project
from xnatctl.cli.resource import resource
from xnatctl.cli.scan import scan
from xnatctl.cli.scp import scp
from xnatctl.cli.search import search
from xnatctl.cli.session import session
from xnatctl.cli.subject import subject
from xnatctl.cli.upgrade import cleanup_stale_backup, upgrade
from xnatctl.cli.wrapper import wrapper
from xnatctl.cli.xsync import xsync
from xnatctl.core import update_check
from xnatctl.core.logging import (
    install_log_file,
    new_correlation_id,
    remove_log_file,
    setup_logging,
)
from xnatctl.core.output import (
    OutputFormat,
    no_color_requested,
    print_error,
    print_output,
    print_success,
    set_no_color,
    set_no_headers,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Main CLI Group
# =============================================================================


@click.group()
@click.version_option(version=__version__, prog_name="xnatctl")
@click.option(
    "--profile",
    "-p",
    envvar="XNAT_PROFILE",
    default=None,
    help="Config profile to use (inherited by subcommands when not overridden).",
)
@click.option(
    "--output",
    "-o",
    "output_format",
    type=click.Choice(["json", "table", "tsv"]),
    default=None,
    help="Output format (inherited by subcommands when not overridden).",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    default=False,
    help="Minimal output (inherited by subcommands when not overridden).",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable verbose output (inherited by subcommands when not overridden).",
)
@click.option(
    "--no-color",
    is_flag=True,
    default=False,
    help="Disable colored output (inherited by subcommands; also honors "
    "NO_COLOR/CLICOLOR=0 env vars).",
)
@click.option(
    "--no-headers",
    is_flag=True,
    default=False,
    help="Omit the header line/row from table and tsv output (inherited by "
    "subcommands when not overridden; ignored by json/quiet).",
)
@click.option(
    "--log-file",
    envvar="XNATCTL_LOG_FILE",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write this invocation's full-detail diagnostics to PATH as JSON "
    "lines, regardless of --quiet/--verbose (inherited by subcommands when "
    "not overridden). Off by default.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    profile: str | None,
    output_format: str | None,
    quiet: bool,
    verbose: bool,
    no_color: bool,
    no_headers: bool,
    log_file: str | None,
) -> None:
    """Xnatctl - A CLI for standardized XNAT REST workflows.

    Manage XNAT projects, subjects, sessions, scans, and resources.
    Supports parallel uploads/downloads, batch operations, and admin tasks.

    Get started:

      xnatctl config init        # Create config file

      xnatctl auth login         # Authenticate

      xnatctl project list       # List projects

    Use --help on any command for more information.

    \b
    Exit codes:
      0  success             4  network error
      1  general error       5  not found
      2  usage error (Click) 6  permission denied
      3  auth error          7  user cancelled
    """
    # One id per process invocation, minted here because this is the one call
    # site every command passes through exactly once before any subcommand
    # logic runs (see new_correlation_id's docstring). Must run before any
    # logging setup below, so every record -- including ones logged while
    # loading config -- carries it.
    new_correlation_id()

    # Cheap and guarded: a frozen (PyInstaller) binary's own upgrade command
    # can't delete its pre-upgrade binary on Windows while this process is
    # still running from it (renamed aside to `.bak` instead -- see
    # cli/upgrade.py's replace_frozen_binary), so a fresh launch is what
    # cleans that leftover copy up. `getattr` makes the check itself a single
    # attribute lookup for every non-frozen invocation -- the overwhelming
    # majority -- so this never shows up as measurable overhead.
    if getattr(sys, "frozen", False):
        cleanup_stale_backup()

    cli_ctx = ctx.ensure_object(Context)

    # Stash root-group values on the shared Context so the per-subcommand
    # ``@global_options`` decorator can inherit them when the subcommand
    # received the Click default for the same flag.
    cli_ctx.root_profile_name = profile
    cli_ctx.root_output_format = (
        OutputFormat.from_string(output_format) if output_format is not None else None
    )
    cli_ctx.root_quiet = quiet
    cli_ctx.root_verbose = verbose
    cli_ctx.root_no_color = no_color
    cli_ctx.root_no_headers = no_headers
    cli_ctx.root_log_file = log_file

    # Seed the effective context with root values so commands that don't carry
    # the ``@global_options`` decorator (e.g. ``whoami``) still see verbose/
    # quiet/profile from the root group.
    if profile is not None:
        cli_ctx.profile_name = profile
    if output_format is not None:
        cli_ctx.output_format = OutputFormat.from_string(output_format)
    if quiet:
        cli_ctx.quiet = True
    if verbose:
        cli_ctx.verbose = True

    if quiet or verbose:
        setup_logging(quiet=quiet, verbose=verbose)

    # Diagnostics log file: the --log-file flag and XNATCTL_LOG_FILE env var
    # are resolved and activated HERE, unconditionally -- this is the ONLY
    # call site every invocation passes through regardless of the
    # subcommand, which is what makes those two tiers work for the commands
    # that carry no @global_options at all (config init/use-context/...,
    # every dicom subcommand, every completion subcommand, project
    # transfer-init, local extract). `log_file` already reflects Click's own
    # flag > env resolution for this option; nothing further to compute here.
    #
    # The THIRD tier -- config.yaml's `log_file:` key -- is deliberately NOT
    # resolved here, even though that would seem to complete the coverage:
    # it would mean an unconditional Config.load() on every invocation,
    # which breaks two things tests already pin down elsewhere --
    # (1) `auth login --password` (and friends) rely on their eager,
    # is_eager=True argv-rejection callback running before config is ever
    # touched, and (2) a broken config.yaml is meant to warn exactly once
    # per invocation, not once here and once more in @global_options's own
    # (real, error-raising) load. @global_options reuses ITS OWN
    # Config.load() for the config tier instead (see there) -- decorated
    # commands pay for exactly one load either way; the handful of
    # undecorated commands above simply do not get the config tier, which is
    # an accepted, narrow gap (documented in docs/debugging.rst).
    #
    # remove_log_file() runs first, UNCONDITIONALLY, so a previous
    # in-process invocation's handler (repeated CliRunner.invoke() calls, or
    # a library caller driving the CLI programmatically more than once) never
    # leaks into this one -- every invocation starts from a clean slate,
    # matching new_correlation_id() minting a fresh id above.
    #
    # @global_options may still install a MORE SPECIFIC override afterward --
    # a --log-file given literally on the command line for the subcommand
    # itself, as opposed to one merely inherited from this env resolution --
    # and install_log_file() always drops any other path's handler first, so
    # only one is ever active no matter which layer made the final call.
    remove_log_file()
    cli_ctx.log_file = log_file
    if log_file:
        install_log_file(log_file)

    # Checked here too (not just at console-construction time) so NO_COLOR/
    # CLICOLOR=0 take effect for this invocation, and so a subcommand lacking
    # ``@global_options`` (there currently is none, but nothing enforces it)
    # still gets the flag/env honored from the root group alone.
    effective_no_color = no_color or no_color_requested()
    if effective_no_color:
        cli_ctx.no_color = True
    set_no_color(effective_no_color)

    # Header suppression, same shape as the no-color handling above: set
    # UNCONDITIONALLY so a previous in-process invocation's value never leaks
    # into this one, then possibly overridden by @global_options' own
    # effective computation for decorated subcommands.
    if no_headers:
        cli_ctx.no_headers = True
    set_no_headers(no_headers)


# =============================================================================
# Register Command Groups
# =============================================================================

cli.add_command(config)
cli.add_command(auth)
cli.add_command(project)
cli.add_command(subject)
cli.add_command(session)
cli.add_command(scan)
cli.add_command(resource)
cli.add_command(prearchive)
cli.add_command(pipeline)
cli.add_command(admin)
cli.add_command(api)
cli.add_command(dicom)
cli.add_command(local)
cli.add_command(xsync)
cli.add_command(upgrade)
cli.add_command(command)
cli.add_command(wrapper)
cli.add_command(container)
cli.add_command(anon)
cli.add_command(scp)
cli.add_command(search)
cli.add_command(event)


# =============================================================================
# Update-Availability Notice
# =============================================================================


def _stderr_is_tty() -> bool:
    """Whether stderr is a terminal. A seam so tests can force either state."""
    return sys.stderr.isatty()


def _maybe_notify_update(ctx_obj: Context | None) -> None:
    """Print an update-availability notice, then kick a background cache refresh.

    Called from the root group's result_callback, which only fires once a
    subcommand's callback returns without raising -- a failed command never
    gets this glued onto it.

    The notice and the refresh are two independent decisions. Given a loaded
    config, the refresh always fires (subject only to the opt-out check), so
    the cache stays warm even on a quiet/-o json/non-tty run; the notice on
    top of that additionally requires the output to be something a human is
    reading. ``check_for_update`` only reads the local cache, so this never
    waits on the network -- the refresh that might touch the network runs in
    a detached child process (see ``update_check.refresh_cache_async``) and
    is never awaited here.

    Both are gated on a loaded config actually being present on ``ctx_obj``
    -- i.e. this command carries ``@global_options``. A command that never
    loads one (shell completion, ``local extract``, ``config init``, every
    ``dicom`` subcommand, ...) neither notifies nor kicks the background
    refresh: it touches no server and reads no config, so a detached PyPI
    fetch firing behind it anyway would be a surprising side effect, and
    there is also nowhere for such a command to have read an `update_check:
    false` opt-out from in the first place.
    """
    config = getattr(ctx_obj, "config", None)
    if config is None:
        return

    config_update_check = getattr(config, "update_check", None)
    if update_check.update_check_disabled(config_update_check=config_update_check):
        return

    latest = update_check.check_for_update(__version__)

    quiet = bool(getattr(ctx_obj, "quiet", False))
    output_format = getattr(ctx_obj, "output_format", OutputFormat.TABLE)
    if latest and not quiet and output_format is not OutputFormat.JSON and _stderr_is_tty():
        click.echo(
            f"A new xnatctl release is available: {__version__} -> {latest}. "
            "Run 'xnatctl upgrade'.",
            err=True,
        )

    update_check.refresh_cache_async()


@cli.result_callback()
def _after_command(result: Any, **kwargs: Any) -> None:
    """Result callback for the root group: fires once, after a successful run."""
    del result, kwargs
    try:
        click_ctx = click.get_current_context(silent=True)
        ctx_obj = click_ctx.obj if click_ctx is not None else None
        _maybe_notify_update(ctx_obj)
    except Exception as e:  # noqa: BLE001 -- pragma: no cover - update-check courtesy, never the command's failure
        # This notice is a courtesy, never a reason a command's own success
        # or exit code changes.
        logger.debug("Update-availability notice failed: %s", e)


# =============================================================================
# Top-Level Commands
# =============================================================================


@cli.command()
@global_options
@handle_errors
def whoami(ctx: Context) -> None:
    """Show current user and authentication context.

    Honors the global --profile/-o/-q flags, so `xnatctl -p prod whoami`
    reports prod's server and user (not the default profile's).
    """
    cfg = ctx.config
    assert cfg is not None

    client = ctx.get_client()

    if client.is_authenticated or (client.username and client.password):
        if not client.is_authenticated:
            client.authenticate()

        user_info = client.whoami()
        profile = cfg.get_profile(ctx.profile_name)

        output = {
            "username": user_info.username,
            "server": client.base_url,
            "profile": ctx.profile_name or cfg.default_profile,
            "default_project": profile.default_project or "-",
            "auth_mode": "session" if client.session_token else "basic",
        }

        print_output(
            output,
            format=ctx.output_format,
            column_labels={
                "username": "User",
                "server": "Server",
                "profile": "Profile",
                "default_project": "Default Project",
                "auth_mode": "Auth Mode",
            },
            quiet=ctx.quiet,
            id_field="username",
        )
    else:
        print_error(
            "Not authenticated. Run 'xnatctl auth login', set XNAT_USER/XNAT_PASS, "
            "or set username/password in the profile config."
        )
        # AUTH_ERROR (not Click's usage-error code 2). SystemExit is a
        # BaseException, so @handle_errors (except Exception) passes it through.
        raise SystemExit(ExitCode.AUTH_ERROR)


@cli.group()
def health() -> None:
    """Server health and connectivity checks."""
    pass


@health.command("ping")
@global_options
@handle_errors
def health_ping(ctx: Context) -> None:
    """Check server connectivity and authentication.

    Honors the global --profile/-o flags; unauthenticated pings still report
    reachability (auth status is a field, not a precondition).
    """
    client = ctx.get_client()
    # Model -> dict at the call site so the rendered shapes (JSON keys, table
    # rows, quiet id_field) stay exactly what they were before ping() was typed.
    result = client.ping().model_dump()

    result["authenticated"] = client.is_authenticated or bool(client.username and client.password)

    if ctx.output_format == OutputFormat.JSON:
        print_output(result, format=OutputFormat.JSON)
    elif ctx.quiet:
        # Minimal: just the reachable URL on stdout; the exit code carries health.
        print_output(result, format=ctx.output_format, quiet=True, id_field="url")
    else:
        print_success(f"Server reachable: {result['url']}")
        # format=ctx.output_format (not a hardcoded TABLE): this is the only
        # data this command prints to stdout in non-JSON/non-quiet mode, so
        # `-o tsv` must reach it too, not silently fall back to a Rich table.
        print_output(
            {
                "status": result["status"],
                "version": result["version"],
                "latency": f"{result['latency_ms']}ms",
                "authenticated": result["authenticated"],
            },
            format=ctx.output_format,
        )


@cli.group()
def completion() -> None:
    """Generate shell completion scripts."""
    pass


def _render_completion(shell: str) -> str:
    """Render Click's official completion script for *shell*.

    Generated from the INSTALLED Click version so the emitted completion protocol
    always matches the runtime. The old hand-rolled bash script emitted the
    Click 7 raw-``COMPREPLY`` form, which under Click 8 produced literal
    ``plain,project`` completions instead of ``project``; delegating to Click's
    own generator fixes that and removes the drift risk for zsh/fish too.
    """
    comp_cls = get_completion_class(shell)
    if comp_cls is None:  # pragma: no cover - bash/zsh/fish all ship with Click
        raise click.ClickException(f"No completion support for shell: {shell}")
    return comp_cls(cli, {}, "xnatctl", "_XNATCTL_COMPLETE").source()


@completion.command("bash")
def completion_bash() -> None:
    """Generate bash completion script.

    Install with:
      xnatctl completion bash > ~/.local/share/bash-completion/completions/xnatctl
    """
    click.echo(_render_completion("bash"))


@completion.command("zsh")
def completion_zsh() -> None:
    """Generate zsh completion script.

    Install with:
      xnatctl completion zsh > ~/.zfunc/_xnatctl
    """
    click.echo(_render_completion("zsh"))


@completion.command("fish")
def completion_fish() -> None:
    """Generate fish completion script.

    Install with:
      xnatctl completion fish > ~/.config/fish/completions/xnatctl.fish
    """
    click.echo(_render_completion("fish"))


# =============================================================================
# Entry Point
# =============================================================================


def main() -> None:
    """Main entry point.

    Last-resort guard so no expected failure class (bad profile, unreachable
    server, expired session) ever reaches the user as a raw Python traceback.
    Commands normally render these via ``@handle_errors``; this catches anything
    raised OUTSIDE a decorated command body too -- notably setup-phase failures
    inside ``@global_options`` (config load, ``XNAT_TIMEOUT`` parsing), which the
    per-command ``@handle_errors`` wrapper never sees. Uses the same
    ``render_cli_error`` policy so ``XNATCTL_DEBUG=1`` yields a traceback here as
    well. Click's own ``ClickException``/``Abort`` are handled by ``cli()`` in
    standalone mode and surface as ``SystemExit``, which passes straight through.

    Checked before any of that: the detached update-check refresh child that
    ``update_check._spawn_refresh_subprocess`` launches against a frozen
    (PyInstaller) binary re-invokes this same binary with
    ``update_check.FROZEN_REFRESH_ENV_VAR`` set, since ``sys.executable`` is
    the binary itself there rather than an interpreter Click could hand a
    ``-m`` module argument to. Recognizing that signal here and running the
    refresh directly means it never has to survive a round trip through
    Click's argument parsing.

    That branch requires BOTH the signal and ``sys.frozen``, and bare argv.
    The variable is private and nothing should ever set it, but environments
    leak: inherited from a parent process alone, it would turn every
    ordinary ``xnatctl ...`` invocation into a silent update fetch that
    ignores its arguments and exits 0. Requiring the frozen build the signal
    only exists for, plus the empty argv its spawner actually passes, means
    a stray variable cannot swallow a real command.
    """
    if (
        os.environ.get(update_check.FROZEN_REFRESH_ENV_VAR)
        and getattr(sys, "frozen", False)
        and len(sys.argv) <= 1
    ):
        update_check.main()
        return

    try:
        cli()
    except Exception as e:  # noqa: BLE001 -- process-level last-resort handler outside @handle_errors
        sys.exit(render_cli_error(e))


if __name__ == "__main__":
    main()
