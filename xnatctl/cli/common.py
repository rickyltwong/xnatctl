"""Common CLI utilities, decorators, and helpers."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import click

from xnatctl.core.auth import AuthManager
from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config, Profile, get_credentials
from xnatctl.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ProfileNotFoundError,
    XNATCtlError,
)
from xnatctl.core.logging import setup_logging
from xnatctl.core.output import OutputFormat, print_error

F = TypeVar("F", bound=Callable[..., Any])


# =============================================================================
# Context Object
# =============================================================================


class Context:
    """CLI context object passed to commands."""

    def __init__(self) -> None:
        self.config: Config | None = None
        self.client: XNATClient | None = None
        self.profile_name: str | None = None
        self.output_format: OutputFormat = OutputFormat.TABLE
        self.quiet: bool = False
        self.verbose: bool = False
        self.auth_manager: AuthManager = AuthManager()

    def get_client(self) -> XNATClient:
        """Get or create authenticated client.

        Returns:
            Authenticated XNATClient.

        Raises:
            ConfigurationError: If no profile configured.
            AuthenticationError: If authentication fails.
        """
        if self.client is not None:
            return self.client

        if self.config is None:
            self.config = Config.load()

        try:
            profile = self.config.get_profile(self.profile_name)
        except ProfileNotFoundError as e:
            raise ConfigurationError(
                f"Profile '{self.profile_name or 'default'}' not found. "
                "Run 'xnatctl config show' to list profiles or 'xnatctl config init' to create one."
            ) from e

        # Get credentials (env vars > profile config). If we are using a cached
        # session token, keep the cached username as a hint for current-user
        # lookups on servers where /data/user returns a user listing.
        username, password = get_credentials(profile)
        session = self.auth_manager.load_session(profile.url)
        token = self.auth_manager.get_token_from_env() or (session.token if session else None)
        username_hint = username or (session.username if session else None)

        self.client = XNATClient(
            base_url=profile.url,
            username=username_hint,
            password=password,
            session_token=token,
            timeout=profile.timeout,
            verify_ssl=profile.verify_ssl,
        )

        return self.client


pass_context = click.make_pass_decorator(Context, ensure=True)


def get_profile(ctx: Context) -> Profile | None:
    """Return the active profile, if configured and resolvable."""

    if ctx.config is None:
        return None

    try:
        return ctx.config.get_profile(ctx.profile_name)
    except ProfileNotFoundError:
        return None


def default_project_from_context(ctx: Context) -> str | None:
    """Return the profile default project if available."""

    profile = get_profile(ctx)
    return profile.default_project if profile else None


def require_project_from_context(ctx: Context, project: str | None) -> str:
    """Return an explicit or default project, or raise a Click error."""

    resolved_project = project or default_project_from_context(ctx)
    if resolved_project:
        return resolved_project

    profile_name = ctx.profile_name or (ctx.config.default_profile if ctx.config else "default")
    raise click.ClickException(
        f"Project required. Pass --project/-P or set default_project in profile '{profile_name}'."
    )


def resolve_workers_from_context(ctx: Context, workers: int | None, default: int = 4) -> int:
    """Resolve worker count from explicit option, profile, or a default."""

    if workers is not None:
        return workers

    profile = get_profile(ctx)
    if profile and profile.workers is not None:
        return profile.workers

    return default


# =============================================================================
# Global Options
# =============================================================================


def global_options(f: F) -> F:
    """Add global options to a command."""

    @click.option(
        "--profile",
        "-p",
        envvar="XNAT_PROFILE",
        help="Config profile to use",
    )
    @click.option(
        "--output",
        "-o",
        "output_format",
        type=click.Choice(["json", "table"]),
        default="table",
        help="Output format",
    )
    @click.option(
        "--quiet",
        "-q",
        is_flag=True,
        help="Minimal output (IDs only)",
    )
    @click.option(
        "--verbose",
        "-v",
        is_flag=True,
        help="Enable verbose output",
    )
    @pass_context
    @wraps(f)
    def wrapper(
        ctx: Context,
        profile: str | None,
        output_format: str,
        quiet: bool,
        verbose: bool,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Populate context from global options and invoke the command."""
        ctx.profile_name = profile
        ctx.output_format = OutputFormat.from_string(output_format)
        ctx.quiet = quiet
        ctx.verbose = verbose

        # Setup logging
        setup_logging(quiet=quiet, verbose=verbose)

        # Load config
        ctx.config = Config.load()

        return f(ctx, *args, **kwargs)

    return wrapper  # type: ignore


# =============================================================================
# Authentication Decorators
# =============================================================================


def require_auth(f: F) -> F:
    """Ensure user is authenticated before running command."""

    @wraps(f)
    def wrapper(ctx: Context, *args: Any, **kwargs: Any) -> Any:
        """Ensure the context client is authenticated before running."""
        client = ctx.get_client()
        had_session = client.is_authenticated

        if client.is_authenticated:
            try:
                client.whoami()
                return f(ctx, *args, **kwargs)
            except AuthenticationError:
                ctx.auth_manager.clear_session()
                client.session_token = None

        profile = None
        if ctx.config is not None:
            try:
                profile = ctx.config.get_profile(ctx.profile_name)
            except ProfileNotFoundError:
                profile = None

        username, password = get_credentials(profile)

        if not client.is_authenticated:
            if username and password:
                try:
                    token = client.authenticate()
                    ctx.auth_manager.save_session(
                        token=token,
                        url=client.base_url,
                        username=username,
                    )
                except AuthenticationError as e:
                    raise click.ClickException(str(e)) from e
            else:
                profile_name = ctx.profile_name or (
                    ctx.config.default_profile if ctx.config else "default"
                )
                prefix = "Session expired" if had_session else "Not authenticated"
                raise click.ClickException(
                    f"{prefix}. "
                    f"Profile: '{profile_name}'. "
                    "Run 'xnatctl auth login', set XNAT_USER/XNAT_PASS, "
                    "or set username/password in the profile config."
                )

        return f(ctx, *args, **kwargs)

    return wrapper  # type: ignore


# =============================================================================
# Destructive Operation Decorators
# =============================================================================


def confirm_destructive(message: str) -> Callable[[F], F]:
    """Require confirmation for destructive operations."""

    def decorator(f: F) -> F:
        """Wrap a command to enforce confirmation/dry-run behavior."""

        @click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
        @click.option("--dry-run", is_flag=True, help="Preview without making changes")
        @wraps(f)
        def wrapper(*args: Any, yes: bool, dry_run: bool, **kwargs: Any) -> Any:
            """Handle yes/dry-run flags and invoke the command."""
            if dry_run:
                click.echo("[DRY-RUN] Preview mode - no changes will be made", err=True)
                kwargs["dry_run"] = True
            elif not yes:
                click.confirm(message, abort=True)
                kwargs["dry_run"] = False
            else:
                kwargs["dry_run"] = False

            return f(*args, **kwargs)

        return wrapper  # type: ignore

    return decorator


# =============================================================================
# Batch Operations
# =============================================================================


def batch_option(f: F) -> F:
    """Add --batch option for bulk operations."""

    @click.option(
        "--batch",
        type=click.Path(exists=True),
        help="File with IDs (one per line) or JSON array",
    )
    @wraps(f)
    def wrapper(*args: Any, batch: str | None, **kwargs: Any) -> Any:
        """Load batch IDs from file and inject into kwargs."""
        if batch:
            with open(batch) as file:
                content = file.read().strip()
                if content.startswith("["):
                    kwargs["ids"] = json.loads(content)
                else:
                    kwargs["ids"] = [line.strip() for line in content.splitlines() if line.strip()]
        return f(*args, **kwargs)

    return wrapper  # type: ignore


def _make_alias_cb(
    old_flag: str,
    new_flag: str,
    target_param: str,
    target_value: Any,
) -> Callable[[click.Context, click.Parameter, Any], Any]:
    """Create a Click callback that warns on deprecated flag and sets a fixed value.

    Args:
        old_flag: The deprecated flag name (e.g., "--unzip").
        new_flag: The replacement flag name (e.g., "--extract").
        target_param: The Click parameter name to set on ctx.params.
        target_value: The fixed value to set (NOT the raw flag value).

    Returns:
        A Click callback function.
    """

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
        if (
            param.name
            and ctx.get_parameter_source(param.name) == click.core.ParameterSource.COMMANDLINE
        ):
            click.echo(
                f"Warning: {old_flag} is deprecated, use {new_flag} instead",
                err=True,
            )
            ctx.params[target_param] = target_value
        return value

    return callback


def _make_forwarding_alias_cb(
    old_flag: str,
    new_flag: str,
    target_param: str,
) -> Callable[[click.Context, click.Parameter, Any], Any]:
    """Create a Click callback that warns and forwards the user's raw value.

    Unlike ``_make_alias_cb`` which sets a fixed value, this forwards whatever
    the user provided (useful for value-taking options like ``--session LABEL``).

    Args:
        old_flag: The deprecated flag name.
        new_flag: The replacement flag name.
        target_param: The Click parameter name to set on ctx.params.

    Returns:
        A Click callback function.
    """

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
        if (
            value is not None
            and param.name
            and ctx.get_parameter_source(param.name) == click.core.ParameterSource.COMMANDLINE
        ):
            click.echo(
                f"Warning: {old_flag} is deprecated, use {new_flag} instead",
                err=True,
            )
            ctx.params[target_param] = value
        return value

    return callback


def parallel_options(f: F) -> F:
    """Add parallel execution options.

    Injects ``--workers`` (default resolved from profile or 4).
    Hidden ``--no-parallel`` alias sets workers to 1 with a deprecation warning.
    """

    @click.option(
        "--workers",
        "-w",
        type=int,
        default=None,
        show_default="4 (or profile)",
        help="Parallel workers (1 = sequential)",
    )
    @click.option(
        "--no-parallel",
        is_flag=True,
        hidden=True,
        expose_value=False,
        callback=_make_alias_cb("--no-parallel", "--workers 1", "workers", 1),
    )
    @click.option(
        "--parallel",
        is_flag=True,
        hidden=True,
        expose_value=False,
        help="Deprecated: parallel is the default",
    )
    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        """Pass through parallel options to the command."""
        return f(*args, **kwargs)

    return wrapper  # type: ignore


# =============================================================================
# Error Handling
# =============================================================================


def handle_errors(f: F) -> F:
    """Handle common errors and convert to CLI exceptions."""

    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        """Capture errors and exit with consistent messaging."""
        try:
            return f(*args, **kwargs)
        except XNATCtlError as e:
            print_error(str(e))
            sys.exit(1)
        except click.ClickException:
            raise
        except Exception as e:
            print_error(f"Unexpected error: {e}")
            sys.exit(1)

    return wrapper  # type: ignore


# =============================================================================
# Destination Profile Helpers
# =============================================================================


def dest_profile_options(f: F) -> F:
    """Add destination profile options for transfer commands."""

    @click.option("--dest-profile", help="Destination config profile name")
    @click.option("--dest-url", hidden=True, help="Destination XNAT URL (inline)")
    @click.option("--dest-user", hidden=True, help="Destination username (inline)")
    @click.option("--dest-pass", hidden=True, help="Destination password (inline)")
    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return f(*args, **kwargs)

    return wrapper  # type: ignore


def create_dest_client(
    ctx: Context,
    dest_profile: str | None = None,
    dest_url: str | None = None,
    dest_user: str | None = None,
    dest_pass: str | None = None,
) -> XNATClient:
    """Create an XNATClient for the destination server.

    Args:
        ctx: CLI context.
        dest_profile: Profile name to load from config.
        dest_url: Inline destination URL.
        dest_user: Inline destination username.
        dest_pass: Inline destination password.

    Returns:
        Configured XNATClient (not yet authenticated).

    Raises:
        ConfigurationError: If no destination specified.
    """
    if dest_profile:
        config = ctx.config or Config.load()
        profile = config.get_profile(dest_profile)
        username, password = get_credentials(profile)
        return XNATClient(
            base_url=profile.url,
            username=username,
            password=password,
            timeout=profile.timeout,
            verify_ssl=profile.verify_ssl,
        )
    elif dest_url:
        return XNATClient(
            base_url=dest_url,
            username=dest_user,
            password=dest_pass,
        )
    else:
        raise ConfigurationError("Destination not specified. Use --dest-profile or --dest-url.")


# =============================================================================
# Exit Codes
# =============================================================================


class ExitCode:
    """Standard exit codes."""

    SUCCESS = 0
    GENERAL_ERROR = 1
    AUTH_ERROR = 2
    NETWORK_ERROR = 3
    PERMISSION_ERROR = 4
    USER_CANCELLED = 5
