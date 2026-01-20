"""Config commands for xnatctl."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from xnatctl.core.config import Config, CONFIG_FILE, Profile
from xnatctl.core.output import print_output, print_success, print_error, print_key_value, OutputFormat
from xnatctl.core.validation import validate_server_url


@click.group()
def config() -> None:
    """Manage xnatctl configuration."""
    pass


@config.command("init")
@click.option("--url", prompt="XNAT server URL", help="XNAT server URL")
@click.option("--profile", default="default", help="Profile name")
@click.option("--project", default=None, help="Default project ID")
@click.option("--force", is_flag=True, help="Overwrite existing config")
def config_init(url: str, profile: str, project: Optional[str], force: bool) -> None:
    """Create configuration file with a new profile.

    Example:
        xnatctl config init --url https://xnat.example.org
    """
    # Validate URL
    try:
        url = validate_server_url(url)
    except Exception as e:
        print_error(str(e))
        raise SystemExit(1)

    # Check if config exists
    if CONFIG_FILE.exists() and not force:
        cfg = Config.load()
        if cfg.has_profile(profile):
            print_error(
                f"Profile '{profile}' already exists. Use --force to overwrite."
            )
            raise SystemExit(1)
    else:
        cfg = Config()

    # Add profile
    cfg.add_profile(
        name=profile,
        url=url,
        default_project=project,
    )

    # Set as default if it's the first profile
    if len(cfg.profiles) == 1:
        cfg.default_profile = profile

    # Save config
    cfg.save()

    print_success(f"Configuration saved to {CONFIG_FILE}")
    print_key_value({
        "profile": profile,
        "url": url,
        "default_project": project or "-",
    })


@config.command("show")
@click.option("--output", "-o", type=click.Choice(["json", "table"]), default="table")
def config_show(output: str) -> None:
    """Show current configuration."""
    try:
        cfg = Config.load()
    except Exception as e:
        print_error(f"Failed to load config: {e}")
        raise SystemExit(1)

    if not cfg.profiles:
        print_error(f"No configuration found. Run 'xnatctl config init' first.")
        raise SystemExit(1)

    data = {
        "config_file": str(CONFIG_FILE),
        "default_profile": cfg.default_profile,
        "output_format": cfg.output_format,
        "profiles": list(cfg.profiles.keys()),
    }

    if output == "json":
        # Include full profile details in JSON
        data["profile_details"] = {
            name: p.to_dict() for name, p in cfg.profiles.items()
        }
        print_output(data, format=OutputFormat.JSON)
    else:
        print_key_value(data, title="Configuration")

        # Show profile details
        click.echo()
        for name, profile in cfg.profiles.items():
            marker = " (default)" if name == cfg.default_profile else ""
            click.echo(f"Profile: {name}{marker}")
            print_key_value(
                {
                    "url": profile.url,
                    "verify_ssl": profile.verify_ssl,
                    "timeout": f"{profile.timeout}s",
                    "default_project": profile.default_project or "-",
                },
            )
            click.echo()


@config.command("use-context")
@click.argument("profile")
def config_use_context(profile: str) -> None:
    """Switch the active profile.

    Example:
        xnatctl config use-context production
    """
    try:
        cfg = Config.load()
    except Exception as e:
        print_error(f"Failed to load config: {e}")
        raise SystemExit(1)

    if not cfg.has_profile(profile):
        print_error(f"Profile '{profile}' not found.")
        click.echo(f"Available profiles: {', '.join(cfg.profiles.keys())}")
        raise SystemExit(1)

    cfg.set_default_profile(profile)
    cfg.save()

    print_success(f"Switched to profile '{profile}'")


@config.command("current-context")
def config_current_context() -> None:
    """Show the current active profile."""
    try:
        cfg = Config.load()
    except Exception as e:
        print_error(f"Failed to load config: {e}")
        raise SystemExit(1)

    if not cfg.profiles:
        print_error("No configuration found.")
        raise SystemExit(1)

    click.echo(cfg.default_profile)


@config.command("add-profile")
@click.argument("name")
@click.option("--url", required=True, help="XNAT server URL")
@click.option("--project", default=None, help="Default project ID")
@click.option("--timeout", type=int, default=30, help="Request timeout in seconds")
@click.option("--no-verify-ssl", is_flag=True, help="Disable SSL verification")
def config_add_profile(
    name: str,
    url: str,
    project: Optional[str],
    timeout: int,
    no_verify_ssl: bool,
) -> None:
    """Add a new profile.

    Example:
        xnatctl config add-profile dev --url https://xnat-dev.example.org
    """
    try:
        url = validate_server_url(url)
    except Exception as e:
        print_error(str(e))
        raise SystemExit(1)

    cfg = Config.load()

    if cfg.has_profile(name):
        print_error(f"Profile '{name}' already exists.")
        raise SystemExit(1)

    cfg.add_profile(
        name=name,
        url=url,
        default_project=project,
        timeout=timeout,
        verify_ssl=not no_verify_ssl,
    )
    cfg.save()

    print_success(f"Profile '{name}' added")


@config.command("remove-profile")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def config_remove_profile(name: str, yes: bool) -> None:
    """Remove a profile.

    Example:
        xnatctl config remove-profile dev
    """
    cfg = Config.load()

    if not cfg.has_profile(name):
        print_error(f"Profile '{name}' not found.")
        raise SystemExit(1)

    if name == cfg.default_profile:
        print_error(f"Cannot remove the default profile. Switch to another profile first.")
        raise SystemExit(1)

    if not yes:
        click.confirm(f"Remove profile '{name}'?", abort=True)

    cfg.remove_profile(name)
    cfg.save()

    print_success(f"Profile '{name}' removed")
