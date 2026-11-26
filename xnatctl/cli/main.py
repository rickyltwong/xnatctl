"""Main CLI entry point for xnatctl."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import click
from click.shell_completion import get_completion_class

from xnatctl import __version__
from xnatctl.cli.admin import admin
from xnatctl.cli.admin_user import user
from xnatctl.cli.anon import anon
from xnatctl.cli.api import api
from xnatctl.cli.auth import auth
from xnatctl.cli.command_cmd import command
from xnatctl.cli.common import Context, ExitCode, global_options, handle_errors, render_cli_error
from xnatctl.cli.config_cmd import config
from xnatctl.cli.container import container
from xnatctl.cli.custom_vars import session_vars, subject_vars
from xnatctl.cli.dicom_cmd import dicom
from xnatctl.cli.event import event
from xnatctl.cli.local import local
from xnatctl.cli.pipeline import pipeline
from xnatctl.cli.prearchive import prearchive
from xnatctl.cli.project import project
from xnatctl.cli.resource import resource
from xnatctl.cli.scan import scan
from xnatctl.cli.scan_download import scan_download
from xnatctl.cli.scp import scp
from xnatctl.cli.search import search
from xnatctl.cli.session import session
from xnatctl.cli.session_labels import session_normalize_labels
from xnatctl.cli.session_query import session_list, session_show
from xnatctl.cli.session_upload import session_upload, session_upload_dicom, session_upload_exam
from xnatctl.cli.sharing import session_share, session_unshare, subject_share, subject_unshare
from xnatctl.cli.subject import subject
from xnatctl.cli.upgrade import cleanup_stale_backup, upgrade
from xnatctl.cli.wrapper import wrapper
from xnatctl.cli.xsync import xsync
from xnatctl.core import update_check
from xnatctl.core.logging import (
    FILE_ONLY_ATTR,
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
    new_correlation_id()

    if getattr(sys, "frozen", False):
        cleanup_stale_backup()

    cli_ctx = ctx.ensure_object(Context)

    cli_ctx.root_profile_name = profile
    cli_ctx.root_output_format = (
        OutputFormat.from_string(output_format) if output_format is not None else None
    )
    cli_ctx.root_quiet = quiet
    cli_ctx.root_verbose = verbose
    cli_ctx.root_no_color = no_color
    cli_ctx.root_no_headers = no_headers
    cli_ctx.root_log_file = log_file

    if profile is not None:
        cli_ctx.profile_name = profile
    if output_format is not None:
        cli_ctx.output_format = OutputFormat.from_string(output_format)
    if quiet:
        cli_ctx.quiet = True
    if verbose:
        cli_ctx.verbose = True

    # Commands without @global_options never call setup_logging themselves;
    # WARNING matches what Python's last-resort handler showed them before.
    setup_logging(logging.WARNING, quiet=quiet, verbose=verbose)

    remove_log_file()
    cli_ctx.log_file = log_file
    if log_file:
        install_log_file(log_file)

    effective_no_color = no_color or no_color_requested()
    if effective_no_color:
        cli_ctx.no_color = True
    set_no_color(effective_no_color)

    if no_headers:
        cli_ctx.no_headers = True
    set_no_headers(no_headers)


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

admin.add_command(user)
scan.add_command(scan_download)
session.add_command(session_list)
session.add_command(session_show)
session.add_command(session_upload)
session.add_command(session_upload_exam)
session.add_command(session_upload_dicom)
session.add_command(session_normalize_labels)
session.add_command(session_share)
session.add_command(session_unshare)
session.add_command(session_vars)
subject.add_command(subject_share)
subject.add_command(subject_unshare)
subject.add_command(subject_vars)


def _stderr_is_tty() -> bool:
    return sys.stderr.isatty()


def _maybe_notify_update(ctx_obj: Context | None) -> None:
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
    del result, kwargs
    try:
        click_ctx = click.get_current_context(silent=True)
        ctx_obj = click_ctx.obj if click_ctx is not None else None
        _maybe_notify_update(ctx_obj)
    except Exception as e:
        logger.exception("Update-availability notice failed", extra={FILE_ONLY_ATTR: True})
        logger.debug("Update-availability notice failed: %s", e)


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
    result = client.ping().model_dump()

    result["authenticated"] = client.is_authenticated or bool(client.username and client.password)

    if ctx.output_format == OutputFormat.JSON:
        print_output(result, format=OutputFormat.JSON)
    elif ctx.quiet:
        print_output(result, format=ctx.output_format, quiet=True, id_field="url")
    else:
        print_success(f"Server reachable: {result['url']}")
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
    comp_cls = get_completion_class(shell)
    assert comp_cls is not None
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
    except Exception as e:
        raise SystemExit(render_cli_error(e)) from e


if __name__ == "__main__":
    main()
