"""Main CLI entry point for xnatctl."""

from __future__ import annotations

import click

from xnatctl import __version__
from xnatctl.cli.admin import admin
from xnatctl.cli.api import api
from xnatctl.cli.auth import auth
from xnatctl.cli.common import Context, global_options, handle_errors

# Import command groups
from xnatctl.cli.config_cmd import config
from xnatctl.cli.dicom_cmd import dicom
from xnatctl.cli.pipeline import pipeline
from xnatctl.cli.prearchive import prearchive
from xnatctl.cli.project import project
from xnatctl.cli.resource import resource
from xnatctl.cli.scan import scan
from xnatctl.cli.session import local, session
from xnatctl.cli.subject import subject
from xnatctl.cli.xsync import xsync
from xnatctl.core.logging import setup_logging
from xnatctl.core.output import OutputFormat

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
    type=click.Choice(["json", "table"]),
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
@click.pass_context
def cli(
    ctx: click.Context,
    profile: str | None,
    output_format: str | None,
    quiet: bool,
    verbose: bool,
) -> None:
    """xnatctl - A CLI for standardized XNAT REST workflows.

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
    from xnatctl.cli.common import ExitCode
    from xnatctl.core.output import print_error, print_output

    cfg = ctx.config
    assert cfg is not None

    client = ctx.get_client()

    if client.is_authenticated or (client.username and client.password):
        if not client.is_authenticated:
            client.authenticate()

        user_info = client.whoami()
        profile = cfg.get_profile(ctx.profile_name)

        output = {
            "username": user_info.get("username", "unknown"),
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
    from xnatctl.core.output import print_output, print_success

    client = ctx.get_client()
    result = client.ping()

    result["authenticated"] = client.is_authenticated or bool(client.username and client.password)

    if ctx.output_format == OutputFormat.JSON:
        print_output(result, format=OutputFormat.JSON)
    elif ctx.quiet:
        # Minimal: just the reachable URL on stdout; the exit code carries health.
        print_output(result, format=OutputFormat.TABLE, quiet=True, id_field="url")
    else:
        print_success(f"Server reachable: {result['url']}")
        print_output(
            {
                "status": result["status"],
                "version": result["version"],
                "latency": f"{result['latency_ms']}ms",
                "authenticated": result["authenticated"],
            },
            format=OutputFormat.TABLE,
        )


@cli.group()
def completion() -> None:
    """Generate shell completion scripts."""
    pass


@completion.command("bash")
def completion_bash() -> None:
    """Generate bash completion script.

    Install with:
      xnatctl completion bash > ~/.local/share/bash-completion/completions/xnatctl
    """

    # Get the completion script using Click's built-in support
    prog_name = "xnatctl"
    source = f"""
_xnatctl_completion() {{
    local IFS=$'\\n'
    COMPREPLY=( $( env COMP_WORDS="${{COMP_WORDS[*]}}" \\
                   COMP_CWORD=$COMP_CWORD \\
                   _{prog_name.upper()}_COMPLETE=bash_complete $1 ) )
    return 0
}}

complete -o default -F _xnatctl_completion {prog_name}
"""
    click.echo(source.strip())


@completion.command("zsh")
def completion_zsh() -> None:
    """Generate zsh completion script.

    Install with:
      xnatctl completion zsh > ~/.zfunc/_xnatctl
    """
    prog_name = "xnatctl"
    source = f"""
#compdef {prog_name}

_{prog_name}_completion() {{
    local -a completions
    local -a completions_with_descriptions
    local -a response
    (( ! $+commands[{prog_name}] )) && return 1

    response=("${{(@f)$( env COMP_WORDS="${{words[*]}}" \\
                        COMP_CWORD=$((CURRENT-1)) \\
                        _{prog_name.upper()}_COMPLETE=zsh_complete {prog_name} )}}")

    for key descr in ${{(kv)response}}; do
      if [[ "$descr" == "_" ]]; then
          completions+=("$key")
      else
          completions_with_descriptions+=("$key":"$descr")
      fi
    done

    if [ -n "$completions_with_descriptions" ]; then
        _describe -V unsorted completions_with_descriptions -U
    fi

    if [ -n "$completions" ]; then
        compadd -U -V unsorted -a completions
    fi
}}

compdef _{prog_name}_completion {prog_name}
"""
    click.echo(source.strip())


@completion.command("fish")
def completion_fish() -> None:
    """Generate fish completion script.

    Install with:
      xnatctl completion fish > ~/.config/fish/completions/xnatctl.fish
    """
    prog_name = "xnatctl"
    source = f"""
function _xnatctl_completion
    set -l response (env _{prog_name.upper()}_COMPLETE=fish_complete COMP_WORDS=(commandline -cp) COMP_CWORD=(commandline -t) {prog_name})

    for completion in $response
        set -l metadata (string split "," -- $completion)

        if [ $metadata[1] = "dir" ]
            __fish_complete_directories $metadata[2]
        else if [ $metadata[1] = "file" ]
            __fish_complete_path $metadata[2]
        else if [ $metadata[1] = "plain" ]
            echo $metadata[2]
        end
    end
end

complete --no-files --command {prog_name} --arguments "(_xnatctl_completion)"
"""
    click.echo(source.strip())


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
    """
    import sys

    from xnatctl.cli.common import render_cli_error

    try:
        cli()
    except Exception as e:
        sys.exit(render_cli_error(e))


if __name__ == "__main__":
    main()
