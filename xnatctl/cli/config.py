"""Configuration management commands for xnatctl."""

from __future__ import annotations

from typing import Optional

import click

from xnatctl.core.config import (
    Config,
    Profile,
    load_config,
    save_config,
    get_config_dir,
    get_config_path,
)
from xnatctl.core.output import print_output, print_error, print_success, OutputFormat
from xnatctl.core.validation import validate_server_url


@click.group()
def config() -> None:
    """Manage xnatctl configuration and profiles."""
    pass


@config.command("show")
@click.option("--profile", "-p", "profile_name", help="Show specific profile")
def config_show(profile_name: Optional[str]) -> None:
    """Show current configuration."""
    cfg = load_config()

    if profile_name:
        try:
            profile = cfg.get_profile(profile_name)
            print_output(
                {
                    "name": profile.name,
                    "server": profile.server,
                    "username": profile.username or "(not set)",
                    "verify_tls": profile.verify_tls,
                    "timeout": profile.timeout,
                    "default_project": profile.default_project or "(not set)",
                },
                format=OutputFormat.TABLE,
            )
        except ValueError as e:
            print_error(str(e))
            raise SystemExit(1) from e
    else:
        output = {
            "config_dir": str(get_config_dir()),
            "config_file": str(get_config_path()),
            "default_profile": cfg.default_profile or "(not set)",
            "output_format": cfg.output_format,
            "profiles": list(cfg.profiles.keys()),
        }
        print_output(output, format=OutputFormat.TABLE)


@config.command("list")
def config_list() -> None:
    """List all profiles."""
    cfg = load_config()

    if not cfg.profiles:
        click.echo("No profiles configured. Use 'xnatctl config add' to create one.")
        return

    profiles = []
    for name, profile in cfg.profiles.items():
        profiles.append({
            "name": name,
            "server": profile.server,
            "username": profile.username or "",
            "default": "*" if name == cfg.default_profile else "",
        })

    print_output(
        profiles,
        format=OutputFormat.TABLE,
        columns=["name", "server", "username", "default"],
        column_labels={
            "name": "Profile",
            "server": "Server",
            "username": "Username",
            "default": "Default",
        },
    )


@config.command("add")
@click.argument("name")
@click.option("--server", "-s", required=True, help="XNAT server URL")
@click.option("--username", "-u", help="Username")
@click.option("--no-verify-tls", is_flag=True, help="Disable TLS verification")
@click.option("--timeout", type=float, default=30.0, help="Request timeout (seconds)")
@click.option("--default", "set_default", is_flag=True, help="Set as default profile")
def config_add(
    name: str,
    server: str,
    username: Optional[str],
    no_verify_tls: bool,
    timeout: float,
    set_default: bool,
) -> None:
    """Add or update a profile."""
    try:
        server = validate_server_url(server)
    except ValueError as e:
        print_error(str(e))
        raise SystemExit(1) from e

    cfg = load_config()

    profile = Profile(
        name=name,
        server=server,
        username=username,
        verify_tls=not no_verify_tls,
        timeout=timeout,
    )

    cfg.add_profile(profile, set_default=set_default)
    save_config(cfg)

    print_success(f"Added profile: {name}")
    if set_default:
        click.echo(f"Set as default profile")


@config.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def config_remove(name: str, yes: bool) -> None:
    """Remove a profile."""
    cfg = load_config()

    if name not in cfg.profiles:
        print_error(f"Profile not found: {name}")
        raise SystemExit(1)

    if not yes:
        if not click.confirm(f"Remove profile '{name}'?"):
            click.echo("Cancelled")
            return

    cfg.remove_profile(name)
    save_config(cfg)

    print_success(f"Removed profile: {name}")


@config.command("set-default")
@click.argument("name")
def config_set_default(name: str) -> None:
    """Set the default profile."""
    cfg = load_config()

    if name not in cfg.profiles:
        print_error(f"Profile not found: {name}")
        raise SystemExit(1)

    cfg.default_profile = name
    save_config(cfg)

    print_success(f"Default profile set to: {name}")


@config.command("init")
@click.option("--force", "-f", is_flag=True, help="Overwrite existing config")
def config_init(force: bool) -> None:
    """Initialize configuration interactively."""
    config_path = get_config_path()

    if config_path.exists() and not force:
        print_error(f"Config file already exists: {config_path}")
        click.echo("Use --force to overwrite")
        raise SystemExit(1)

    # Interactive setup
    click.echo("Setting up xnatctl configuration\n")

    name = click.prompt("Profile name", default="default")
    server = click.prompt("XNAT server URL")
    username = click.prompt("Username (optional)", default="", show_default=False)
    verify_tls = click.confirm("Verify TLS certificates?", default=True)

    try:
        server = validate_server_url(server)
    except ValueError as e:
        print_error(str(e))
        raise SystemExit(1) from e

    profile = Profile(
        name=name,
        server=server,
        username=username if username else None,
        verify_tls=verify_tls,
    )

    cfg = Config()
    cfg.add_profile(profile, set_default=True)
    save_config(cfg)

    print_success(f"Configuration saved to: {config_path}")
    click.echo(f"\nNext steps:")
    click.echo(f"  xnatctl auth login    # Store credentials securely")
    click.echo(f"  xnatctl whoami        # Verify setup")
