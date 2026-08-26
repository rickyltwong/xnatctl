"""Config commands for xnatctl."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click
import yaml

from xnatctl.cli.common import Context, confirm_destructive, global_options, handle_errors
from xnatctl.core.config import (
    CONFIG_FILE,
    CURRENT_CONFIG_VERSION,
    KEYRING_SERVICE,
    PASSWORD_SOURCE_KEYRING,
    Config,
    keyring_key,
    load_keyring,
)
from xnatctl.core.exceptions import ConfigurationError, InvalidURLError, XNATCtlError
from xnatctl.core.output import (
    OutputFormat,
    print_error,
    print_hint,
    print_key_value,
    print_output,
    print_success,
    print_warning,
)
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS
from xnatctl.core.validation import validate_server_url


@click.group()
def config() -> None:
    """Manage xnatctl configuration."""
    pass


def _warn_if_forcing_over_newer_version(path: Path) -> None:
    """Best-effort notice before ``--force`` overwrites an existing config.

    ``--force`` is the one mutating path that deliberately does NOT go
    through :meth:`Config.ensure_saveable` -- it exists specifically to
    recover from a config.yaml this build cannot even parse (see the
    corrupt-file case ``config init --force`` is meant to fix), so it must
    never itself refuse to run. This reads the raw file directly, without
    ``Config.load()``, and only ever warns: a file that can't be read or
    parsed here gets no warning, silently, exactly like any other file
    ``--force`` is allowed to steamroll.
    """
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        version = raw.get("version") if isinstance(raw, dict) else None
    except (OSError, yaml.YAMLError):
        return
    # Accept a quoted integer the same way normal loading normalizes it
    # ("2" means version 2); anything unreadable stays silent.
    if isinstance(version, str):
        try:
            version = int(version)
        except ValueError:
            return
    if (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version > CURRENT_CONFIG_VERSION
    ):
        print_warning(
            f"{path} declares config version {version}, newer than this xnatctl "
            f"understands ({CURRENT_CONFIG_VERSION}). --force will overwrite it anyway."
        )


@config.command("init")
@click.option("--url", prompt="XNAT server URL", help="XNAT server URL")
@click.option("--profile", default="default", help="Profile name")
@click.option("--project", default=None, help="Default project ID")
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Overwrite the existing config -- even one this build cannot parse, or "
        "declares a newer schema version than it understands (every other "
        "mutating config command refuses in that case; --force is the explicit "
        "override)"
    ),
)
@click.option(
    "--login/--no-login",
    "login",
    default=None,
    help="Log in after writing the profile (prompts when not specified)",
)
def config_init(
    url: str,
    profile: str,
    project: str | None,
    force: bool,
    login: bool | None,
) -> None:
    """Create configuration file with a new profile.

    Offers to log in straight afterwards, so a fresh machine gets from nothing
    to an authenticated session in one command. Pass --no-login to skip the
    prompt in scripts.

    \b
    Example:
        xnatctl config init --url https://xnat.example.org
        xnatctl config init --url https://xnat.example.org --no-login
    """
    # Validate URL
    try:
        url = validate_server_url(url)
    except InvalidURLError as e:
        print_error(str(e))
        raise SystemExit(1) from e

    # Check if config exists
    if CONFIG_FILE.exists() and not force:
        cfg = Config.load()
        if cfg.has_profile(profile):
            print_error(f"Profile '{profile}' already exists. Use --force to overwrite.")
            raise SystemExit(1)
    else:
        # --force is the one mutating command that deliberately does NOT
        # load-then-check like every other one (see Config.ensure_saveable):
        # it exists to recover from a config.yaml this build cannot even
        # parse, so it must not itself refuse on a version it CAN parse
        # either -- that would make --force useless for exactly the file it
        # is most needed for. Best-effort warn instead, so overwriting a
        # config from a newer xnatctl is a choice made with eyes open.
        if force and CONFIG_FILE.exists():
            _warn_if_forcing_over_newer_version(CONFIG_FILE)
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
    print_key_value(
        {
            "profile": profile,
            "url": url,
            "default_project": project or "-",
        }
    )

    # Continue into a login rather than stopping at a cliff between two
    # commands. Only prompt on a real terminal: in a pipeline an
    # unanswered prompt is a hang, and --login/--no-login covers scripts.
    if login is None:
        login = sys.stdin.isatty() and click.confirm("Log in now?", default=True)
    if not login:
        click.echo("Run 'xnatctl auth login' when you are ready.", err=True)
        return

    # Deferred: tests patch xnatctl.cli.auth.do_login/report_login to
    # intercept the lookup; a module-scope import would bind the real
    # functions before the patch runs (see tests/test_cli_config_cmd.py).
    from xnatctl.cli.auth import do_login, report_login

    ctx = Context()
    ctx.config = cfg
    ctx.profile_name = profile

    try:
        result = do_login(ctx, cfg.get_profile(profile), profile_name=profile)
    except XNATCtlError as e:
        # The profile is written and valid; only the login failed. Say so
        # rather than implying init itself failed, and keep exit 0.
        print_error(str(e))
        if e.hint:
            print_hint(e.hint)
        click.echo("Profile saved. Retry with 'xnatctl auth login'.", err=True)
        return

    report_login(ctx, result)


@config.command("show")
@global_options
def config_show(ctx: Context) -> None:
    """Show current configuration.

    Without ``--profile/-p``, lists all profiles. With ``--profile/-p NAME``,
    restricts the per-profile sections (and the ``profiles`` / ``profile_details``
    fields in JSON mode) to that single profile. Unknown names exit non-zero
    and list the available profiles.
    """
    # @global_options already loaded this into ctx.config -- a second
    # Config.load() here would re-read the file and double any warning it
    # logs (unknown keys, a too-new version) for no other effect, since the
    # result is identical either way.
    cfg = ctx.config
    assert cfg is not None

    if not cfg.profiles:
        print_error("No configuration found. Run 'xnatctl config init' first.")
        raise SystemExit(1)

    selected_profile: str | None = ctx.profile_name or None
    if selected_profile and selected_profile not in cfg.profiles:
        available = ", ".join(cfg.profiles.keys())
        print_error(f"Profile '{selected_profile}' not found. Available profiles: {available}")
        raise SystemExit(1)

    profile_names = [selected_profile] if selected_profile else list(cfg.profiles.keys())

    data: dict[str, Any] = {
        "config_file": str(CONFIG_FILE),
        "default_profile": cfg.default_profile,
        "output_format": cfg.output_format,
        "profiles": profile_names,
    }

    if ctx.output_format == OutputFormat.JSON:
        # Include full profile details in JSON
        data["profile_details"] = {name: cfg.profiles[name].to_dict() for name in profile_names}
        print_output(data, format=OutputFormat.JSON)
    elif ctx.output_format == OutputFormat.TSV:
        # A flat table of profiles is the TSV-native shape for this data --
        # one row per profile, one field per column -- rather than the N
        # separate Rich key-value blocks the table branch below prints.
        # Routed through print_output like every other TSV render in the CLI.
        print_output(data, format=OutputFormat.TSV)
        profile_rows = [
            {
                "profile": name,
                "default": name == cfg.default_profile,
                "url": cfg.profiles[name].url,
                "verify_ssl": cfg.profiles[name].verify_ssl,
                "timeout": f"{cfg.profiles[name].timeout}s",
                "default_project": cfg.profiles[name].default_project or "-",
            }
            for name in profile_names
        ]
        if profile_rows:
            print()
            print_output(
                profile_rows,
                format=OutputFormat.TSV,
                columns=["profile", "default", "url", "verify_ssl", "timeout", "default_project"],
            )
    else:
        print_key_value(data, title="Configuration")

        # Show profile details
        click.echo()
        for name in profile_names:
            profile = cfg.profiles[name]
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

    \b
    Example:
        xnatctl config use-context production
    """
    try:
        cfg = Config.load()
    except ConfigurationError as e:
        print_error(f"Failed to load config: {e}")
        raise SystemExit(1) from e

    if not cfg.has_profile(profile):
        print_error(f"Profile '{profile}' not found.")
        click.echo(f"Available profiles: {', '.join(cfg.profiles.keys())}", err=True)
        raise SystemExit(1)

    cfg.set_default_profile(profile)
    cfg.save()

    print_success(f"Switched to profile '{profile}'")


@config.command("current-context")
def config_current_context() -> None:
    """Show the current active profile."""
    try:
        cfg = Config.load()
    except ConfigurationError as e:
        print_error(f"Failed to load config: {e}")
        raise SystemExit(1) from e

    if not cfg.profiles:
        print_error("No configuration found.")
        raise SystemExit(1)

    click.echo(cfg.default_profile)


@config.command("add-profile")
@click.argument("name")
@click.option("--url", required=True, help="XNAT server URL")
@click.option("--project", default=None, help="Default project ID")
@click.option(
    "--timeout",
    type=int,
    default=DEFAULT_HTTP_TIMEOUT_SECONDS,
    help="Request timeout in seconds",
)
@click.option(
    "--no-verify-ssl",
    is_flag=True,
    help="Disable SSL verification (INSECURE; prefer --ca-bundle for self-signed certs)",
)
@click.option(
    "--ca-bundle",
    default=None,
    help="Path to a custom CA bundle for TLS verification (secure alternative to --no-verify-ssl)",
)
def config_add_profile(
    name: str,
    url: str,
    project: str | None,
    timeout: int,
    no_verify_ssl: bool,
    ca_bundle: str | None,
) -> None:
    """Add a new profile.

    \b
    Example:
        xnatctl config add-profile dev --url https://xnat-dev.example.org
    """
    try:
        url = validate_server_url(url)
    except InvalidURLError as e:
        print_error(str(e))
        raise SystemExit(1) from e

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
        ca_bundle=ca_bundle,
    )
    cfg.save()

    print_success(f"Profile '{name}' added")


@config.command("remove-profile")
@click.argument("name")
@confirm_destructive("Remove this profile?")
@handle_errors
def config_remove_profile(name: str, dry_run: bool) -> None:
    """Remove a profile.

    \b
    Example:
        xnatctl config remove-profile dev
        xnatctl config remove-profile dev --dry-run
    """
    cfg = Config.load()

    if not cfg.has_profile(name):
        print_error(f"Profile '{name}' not found.")
        raise SystemExit(1)

    if name == cfg.default_profile:
        print_error("Cannot remove the default profile. Switch to another profile first.")
        raise SystemExit(1)

    if dry_run:
        click.echo(f"[DRY-RUN] Would remove profile '{name}'", err=True)
        return

    cfg.remove_profile(name)
    cfg.save()

    print_success(f"Profile '{name}' removed")


@config.command("set-password")
@click.argument("name")
def config_set_password(name: str) -> None:
    """Store a profile's password in the OS keychain.

    Prompts for the password. It is never accepted as an argument, so it cannot
    leak into shell history or the process table. On success the profile records
    only ``password_source: keyring``, and any inline plaintext password is
    dropped from config.yaml.

    \b
    Example:
        xnatctl config set-password prod
    """
    cfg = Config.load()

    # Before any external side effect: writing to the OS keychain below
    # cannot be undone by a failed save. If this config can't be saved back
    # (e.g. it was loaded from a newer-than-understood version), refuse now
    # -- not after the keychain already has a password config.yaml never
    # ends up recording.
    cfg.ensure_saveable()

    if not cfg.has_profile(name):
        print_error(f"Profile '{name}' not found.")
        raise SystemExit(1)

    keyring = load_keyring()
    # Deferred for the same reason as load_keyring() itself (see its
    # docstring): importing the keyring backend eagerly would cost every
    # xnatctl invocation a backend probe almost no command needs.
    from keyring import errors as keyring_errors

    profile = cfg.get_profile(name)
    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)

    try:
        keyring.set_password(KEYRING_SERVICE, keyring_key(name, profile.url), password)
    except keyring_errors.KeyringError as e:
        print_error(f"Could not write to the OS keychain: {e}")
        raise SystemExit(1) from e

    profile.password_source = PASSWORD_SOURCE_KEYRING
    profile.password = None
    cfg.save()

    print_success(f"Password for profile '{name}' stored in the OS keychain")
