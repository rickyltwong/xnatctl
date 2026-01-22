"""Common CLI utilities, decorators, and helpers."""

from __future__ import annotations

import json
import sys
from functools import wraps
from typing import Any, Callable, Optional, TypeVar

import click

from xnatctl.core.auth import AuthManager
from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config, get_credentials, get_token
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
        self.config: Optional[Config] = None
        self.client: Optional[XNATClient] = None
        self.profile_name: Optional[str] = None
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
        except ProfileNotFoundError:
            raise ConfigurationError(
                f"Profile '{self.profile_name or 'default'}' not found. "
                "Run 'xnatctl config init' to create one."
            )

        # Get credentials
        username, password = get_credentials()
        token = self.auth_manager.get_session_token(profile.url)

        self.client = XNATClient(
            base_url=profile.url,
            username=username,
            password=password,
            session_token=token,
            timeout=profile.timeout,
            verify_ssl=profile.verify_ssl,
        )

        return self.client


pass_context = click.make_pass_decorator(Context, ensure=True)


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
        profile: Optional[str],
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

        # Try to authenticate if no session token
        if not client.is_authenticated:
            username, password = get_credentials()
            if username and password:
                try:
                    token = client.authenticate()
                    # Cache the session
                    ctx.auth_manager.save_session(
                        token=token,
                        url=client.base_url,
                        username=username,
                    )
                except AuthenticationError as e:
                    raise click.ClickException(str(e))
            else:
                raise click.ClickException(
                    "Not authenticated. Run 'xnatctl auth login' or set XNAT_USER/XNAT_PASS."
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
    def wrapper(*args: Any, batch: Optional[str], **kwargs: Any) -> Any:
        """Load batch IDs from file and inject into kwargs."""
        if batch:
            with open(batch) as file:
                content = file.read().strip()
                if content.startswith("["):
                    kwargs["ids"] = json.loads(content)
                else:
                    kwargs["ids"] = [
                        line.strip() for line in content.splitlines() if line.strip()
                    ]
        return f(*args, **kwargs)

    return wrapper  # type: ignore


def parallel_options(f: F) -> F:
    """Add parallel execution options."""

    @click.option(
        "--parallel/--no-parallel",
        default=True,
        help="Enable parallel execution",
    )
    @click.option(
        "--workers",
        type=int,
        default=4,
        help="Max parallel workers",
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
