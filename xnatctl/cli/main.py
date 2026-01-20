"""Main CLI entry point for xnatctl."""

from __future__ import annotations

import click

from xnatctl import __version__

# Import command groups
from xnatctl.cli.config_cmd import config
from xnatctl.cli.auth import auth
from xnatctl.cli.project import project
from xnatctl.cli.subject import subject
from xnatctl.cli.session import session
from xnatctl.cli.scan import scan
from xnatctl.cli.resource import resource
from xnatctl.cli.prearchive import prearchive
from xnatctl.cli.pipeline import pipeline
from xnatctl.cli.admin import admin
from xnatctl.cli.api import api
from xnatctl.cli.dicom_cmd import dicom


# =============================================================================
# Main CLI Group
# =============================================================================


@click.group()
@click.version_option(version=__version__, prog_name="xnatctl")
def cli() -> None:
    """xnatctl - A CLI for standardized XNAT REST workflows.

    Manage XNAT projects, subjects, sessions, scans, and resources.
    Supports parallel uploads/downloads, batch operations, and admin tasks.

    Get started:

      xnatctl config init        # Create config file

      xnatctl auth login         # Authenticate

      xnatctl project list       # List projects

    Use --help on any command for more information.
    """
    pass


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


# =============================================================================
# Top-Level Commands
# =============================================================================


@cli.command()
@click.pass_context
def whoami(ctx: click.Context) -> None:
    """Show current user and authentication context."""
    from xnatctl.cli.common import Context, pass_context, global_options
    from xnatctl.core.output import print_output, print_error, OutputFormat

    # Create context manually since we're not using decorators
    cli_ctx = Context()
    cli_ctx.config = cli_ctx.config or __import__("xnatctl.core.config", fromlist=["Config"]).Config.load()

    try:
        client = cli_ctx.get_client()

        # Try to get user info
        if client.is_authenticated or (client.username and client.password):
            if not client.is_authenticated:
                client.authenticate()

            user_info = client.whoami()
            profile = cli_ctx.config.get_profile(cli_ctx.profile_name)

            output = {
                "username": user_info.get("username", "unknown"),
                "server": client.base_url,
                "profile": cli_ctx.profile_name or cli_ctx.config.default_profile,
                "default_project": profile.default_project or "-",
                "auth_mode": "session" if client.session_token else "basic",
            }

            print_output(
                output,
                format=OutputFormat.TABLE,
                column_labels={
                    "username": "User",
                    "server": "Server",
                    "profile": "Profile",
                    "default_project": "Default Project",
                    "auth_mode": "Auth Mode",
                },
            )
        else:
            print_error("Not authenticated. Run 'xnatctl auth login' first.")
            ctx.exit(2)

    except Exception as e:
        print_error(str(e))
        ctx.exit(1)


@cli.group()
def health() -> None:
    """Server health and connectivity checks."""
    pass


@health.command("ping")
@click.option("--output", "-o", type=click.Choice(["json", "table"]), default="table")
@click.pass_context
def health_ping(ctx: click.Context, output: str) -> None:
    """Check server connectivity and authentication."""
    from xnatctl.cli.common import Context
    from xnatctl.core.output import print_output, print_error, print_success, OutputFormat

    cli_ctx = Context()
    cli_ctx.config = cli_ctx.config or __import__("xnatctl.core.config", fromlist=["Config"]).Config.load()

    try:
        client = cli_ctx.get_client()
        result = client.ping()

        result["authenticated"] = client.is_authenticated or bool(
            client.username and client.password
        )

        if output == "json":
            print_output(result, format=OutputFormat.JSON)
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

    except Exception as e:
        print_error(str(e))
        ctx.exit(1)


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
    import os
    import sys

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
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
