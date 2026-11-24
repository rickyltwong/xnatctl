"""Common CLI utilities, decorators, and helpers.

This is a package rather than a single module because it grew past the
repo's 1000-line-per-file cap. Its public surface is unchanged: every name a
CLI command imports from ``xnatctl.cli.common`` is re-exported here, from
whichever submodule now defines it. The pieces that a handful of tests patch
by dotted path (``xnatctl.cli.common.XNATClient``, ``...AuthManager``,
``...Config``, ``...get_audit_logger``) are kept defined directly in this
file rather than in a submodule -- a whole-object patch like
``patch("xnatctl.cli.common.AuthManager", ...)`` only rebinds the name in
*this* module's namespace, so code that resolves that name via a different
module's globals (even one that re-exports the same object) would silently
keep using the real thing. ``Context.get_client``, ``create_dest_client``,
and the audit-write path all construct or reference one of those patched
objects, so they stay here. Everything else (batch parsing, the deprecated
flag table, list-control helpers, and error rendering) has no such
constraint and lives in its own focused submodule.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import click

from xnatctl.core.auth import AuthManager
from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config, Profile, get_credentials
from xnatctl.core.connect import resolve_client_params
from xnatctl.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    InputValidationError,
    OperationCancelledError,
    PermissionDeniedError,
    ProfileNotFoundError,
    ResourceNotFoundError,
    XNATConnectionError,
    XNATCtlError,
)
from xnatctl.core.logging import (
    FILE_ONLY_ATTR,
    debug_env_enabled,
    get_audit_logger,
    install_log_file,
    setup_logging,
)
from xnatctl.core.output import (
    OutputFormat,
    no_color_requested,
    print_error,
    print_hint,
    print_warning,
    set_no_color,
    set_no_headers,
)
from xnatctl.core.redact import SECRET_QUERY_KEYS, redact_url_query

from .batch import _parse_batch_text as _parse_batch_text
from .batch import batch_option
from .credentials import dest_profile_options, read_password_stdin, reject_argv_password
from .deprecation import (
    DEPRECATED_FLAGS,
    DeprecatedFlag,
    deprecation_message,
    parallel_options,
)
from .deprecation import _flag_given as _flag_given
from .deprecation import _make_alias_cb as _make_alias_cb
from .deprecation import _make_forwarding_alias_cb as _make_forwarding_alias_cb
from .errors import (
    AUDIT_ERROR_KEY,
    ExitCode,
    exit_code_for,
    handle_errors,
    render_cli_error,
)
from .listing import apply_filter, apply_sort_limit, list_options, resolve_columns
from .profile_defaults import (
    default_project_from_context,
    get_profile,
    require_project_from_context,
    resolve_archive_mode_from_context,
    resolve_direct_archive_from_context,
    resolve_overwrite_from_context,
    resolve_workers_from_context,
)
from .validators import reject_blank_option_value, validate_local_path_option_cb

__all__ = [
    "AUDIT_ERROR_KEY",
    "AuthenticationError",
    "Config",
    "ConfigurationError",
    "Context",
    "DEPRECATED_FLAGS",
    "DeprecatedFlag",
    "ExitCode",
    "FILE_ONLY_ATTR",
    "InputValidationError",
    "OperationCancelledError",
    "OutputFormat",
    "PermissionDeniedError",
    "Profile",
    "ProfileNotFoundError",
    "ResourceNotFoundError",
    "XNATClient",
    "XNATConnectionError",
    "XNATCtlError",
    "apply_filter",
    "apply_sort_limit",
    "batch_option",
    "confirm_destructive",
    "confirm_destructive_when",
    "create_dest_client",
    "debug_env_enabled",
    "default_project_from_context",
    "dest_profile_options",
    "deprecation_message",
    "exit_code_for",
    "get_credentials",
    "get_profile",
    "global_options",
    "handle_errors",
    "list_options",
    "parallel_options",
    "pass_context",
    "print_error",
    "print_hint",
    "read_password_stdin",
    "reject_argv_password",
    "reject_blank_option_value",
    "render_cli_error",
    "require_auth",
    "require_project_from_context",
    "resolve_archive_mode_from_context",
    "resolve_columns",
    "resolve_direct_archive_from_context",
    "resolve_overwrite_from_context",
    "resolve_workers_from_context",
    "validate_local_path_option_cb",
]

F = TypeVar("F", bound=Callable[..., Any])


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
        self.root_profile_name: str | None = None
        self.root_output_format: OutputFormat | None = None
        self.root_quiet: bool = False
        self.root_verbose: bool = False
        self.root_no_color: bool = False
        self.no_color: bool = False
        self.root_no_headers: bool = False
        self.no_headers: bool = False
        self.root_log_file: str | None = None
        self.log_file: str | None = None

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

        profile = self.config.get_profile(self.profile_name)

        params = resolve_client_params(
            self.profile_name,
            config=self.config,
            auth_manager=self.auth_manager,
        )

        if not profile.verify_ssl and not profile.ca_bundle:
            print_warning(
                redact_url_query(
                    f"TLS certificate verification is DISABLED for {profile.url}. "
                    "Prefer 'ca_bundle' in the profile for self-signed certs."
                )
            )

        self.client = XNATClient(**params)

        return self.client


pass_context = click.make_pass_decorator(Context, ensure=True)


def global_options(f: F) -> F:
    """Add global options to a command.

    The shared flags (``--profile``, ``--output``, ``--quiet``,
    ``--verbose``, ``--no-color``, ``--no-headers``) are also declared on the
    root ``cli`` group. When the
    subcommand received Click's default value for one of these flags AND a
    root-group value is set on the shared :class:`Context`, the root-group
    value wins. Explicit subcommand-level values always beat inherited root
    values.
    """

    @click.option(
        "--profile",
        "-p",
        envvar="XNAT_PROFILE",
        default=None,
        help="Config profile to use",
    )
    @click.option(
        "--output",
        "-o",
        "output_format",
        type=click.Choice(["json", "table", "tsv"]),
        default=None,
        help="Output format",
    )
    @click.option(
        "--quiet",
        "-q",
        is_flag=True,
        default=False,
        help="Minimal output (IDs only)",
    )
    @click.option(
        "--verbose",
        "-v",
        is_flag=True,
        default=False,
        help="Enable verbose output",
    )
    @click.option(
        "--no-color",
        is_flag=True,
        default=False,
        help="Disable colored output (also honors NO_COLOR/CLICOLOR=0 env vars)",
    )
    @click.option(
        "--no-headers",
        is_flag=True,
        default=False,
        help="Omit the header line/row from table and tsv output (ignored by json/quiet)",
    )
    @click.option(
        "--log-file",
        envvar="XNATCTL_LOG_FILE",
        default=None,
        type=click.Path(dir_okay=False),
        help="Write this invocation's full-detail diagnostics to PATH as JSON "
        "lines, regardless of --quiet/--verbose. Off by default.",
    )
    @pass_context
    @wraps(f)
    def wrapper(
        ctx: Context,
        profile: str | None,
        output_format: str | None,
        quiet: bool,
        verbose: bool,
        no_color: bool,
        no_headers: bool,
        log_file: str | None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        click_ctx = click.get_current_context()

        def _was_explicit(param_name: str) -> bool:
            source = click_ctx.get_parameter_source(param_name)
            return source is not None and source != click.core.ParameterSource.DEFAULT

        if _was_explicit("profile"):
            effective_profile = profile
        elif ctx.root_profile_name is not None:
            effective_profile = ctx.root_profile_name
        else:
            effective_profile = profile

        if _was_explicit("output_format") and output_format is not None:
            effective_format = OutputFormat.from_string(output_format)
        elif ctx.root_output_format is not None:
            effective_format = ctx.root_output_format
        elif output_format is not None:
            effective_format = OutputFormat.from_string(output_format)
        else:
            effective_format = OutputFormat.TABLE

        effective_quiet = quiet if _was_explicit("quiet") else (quiet or ctx.root_quiet)
        effective_verbose = verbose if _was_explicit("verbose") else (verbose or ctx.root_verbose)

        effective_no_color = (
            no_color if _was_explicit("no_color") else (no_color or ctx.root_no_color)
        ) or no_color_requested()

        effective_no_headers = (
            no_headers if _was_explicit("no_headers") else (no_headers or ctx.root_no_headers)
        )

        if (
            log_file
            and click_ctx.get_parameter_source("log_file") == click.core.ParameterSource.COMMANDLINE
        ):
            ctx.log_file = log_file
            install_log_file(log_file)

        ctx.profile_name = effective_profile
        ctx.output_format = effective_format
        ctx.quiet = effective_quiet
        ctx.verbose = effective_verbose
        ctx.no_color = effective_no_color
        ctx.no_headers = effective_no_headers

        setup_logging(quiet=effective_quiet, verbose=effective_verbose)
        set_no_color(effective_no_color)
        set_no_headers(effective_no_headers)

        ctx.config = Config.load()

        if not ctx.log_file:
            config_log_file = getattr(ctx.config, "log_file", None)
            if isinstance(config_log_file, str) and config_log_file:
                ctx.log_file = config_log_file
                install_log_file(config_log_file)

        return f(ctx, *args, **kwargs)

    return wrapper  # type: ignore


def require_auth(f: F) -> F:
    """Ensure user is authenticated before running command."""

    @wraps(f)
    def wrapper(ctx: Context, *args: Any, **kwargs: Any) -> Any:
        client = ctx.get_client()
        had_session = client.is_authenticated
        # The name get_client resolved the profile (and its cached session) by.
        profile_name = ctx.profile_name or (ctx.config.default_profile if ctx.config else "default")

        if client.is_authenticated:
            # Only the session check may lead to a re-login. The command runs
            # outside this try: an AuthenticationError it raises (a 403 is one)
            # may come after it already changed something, and running it
            # again from the top would repeat that change.
            try:
                client.whoami()
            except AuthenticationError:
                ctx.auth_manager.clear_session(profile_name)
                client.session_token = None
            else:
                return f(ctx, *args, **kwargs)

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
                        profile_name,
                        token=token,
                        url=client.base_url,
                        username=username,
                    )
                except AuthenticationError as e:
                    raise click.ClickException(str(e)) from e
            else:
                prefix = "Session expired" if had_session else "Not authenticated"
                raise click.ClickException(
                    f"{prefix}. "
                    f"Profile: '{profile_name}'. "
                    "Run 'xnatctl auth login', set XNAT_USER/XNAT_PASS, "
                    "or set username/password in the profile config."
                )

        return f(ctx, *args, **kwargs)

    return wrapper  # type: ignore


_AUDIT_SECRET_PARAMS = SECRET_QUERY_KEYS | {"dest_pass", "dest_password", "passphrase"}

_AUDIT_MASKED_PARAMS = frozenset({"value"})

_AUDIT_UNINTERESTING_PARAMS = frozenset(
    {
        "yes",
        "dry_run",
        "output_format",
        "quiet",
        "verbose",
        "profile",
        "no_color",
        "no_headers",
        "log_file",
    }
)


def _audit_details(params: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value is False:
            continue
        if key in _AUDIT_SECRET_PARAMS or key in _AUDIT_UNINTERESTING_PARAMS:
            continue
        if key in _AUDIT_MASKED_PARAMS:
            details[key] = "***"
            continue
        details[key] = value
    return details


def _record_audit(
    *, dry_run: bool, confirmed: bool, params: dict[str, Any], error: str | None
) -> None:
    try:
        click_ctx = click.get_current_context(silent=True)
        ctx_obj = click_ctx.obj if click_ctx is not None else None
        command = click_ctx.command_path if click_ctx is not None else None

        config = getattr(ctx_obj, "config", None)
        profile_name = getattr(ctx_obj, "profile_name", None) or getattr(
            config, "default_profile", None
        )

        client = getattr(ctx_obj, "client", None)
        profile = get_profile(ctx_obj) if isinstance(ctx_obj, Context) else None
        server = getattr(client, "base_url", None) or getattr(profile, "url", None)
        user = getattr(client, "username", None) or getattr(profile, "username", None)

        get_audit_logger().log_operation(
            operation=command or "unknown",
            command=command,
            profile=profile_name,
            server=server,
            user=user,
            project=params.get("project"),
            subject=params.get("subject_id") or params.get("subject"),
            session=params.get("experiment") or params.get("session_id"),
            success=error is None,
            error=error,
            dry_run=dry_run,
            details={**_audit_details(params), "confirmed": confirmed},
        )
    except Exception as e:  # noqa: BLE001 -- pragma: no cover - audit write must never break the command it records
        logging.getLogger(__name__).warning("Could not record audit entry: %s", e)


def confirm_destructive(message: str) -> Callable[[F], F]:
    """Require confirmation for destructive operations.

    Also the audit seam: carrying this decorator is what marks a
    command as state-changing, so the record is written here rather than in
    each command. That keeps coverage automatic -- a new destructive command is
    audited by virtue of being declared destructive.
    """

    def decorator(f: F) -> F:
        @click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
        @click.option("--dry-run", is_flag=True, help="Preview without making changes")
        @wraps(f)
        def wrapper(*args: Any, yes: bool, dry_run: bool, **kwargs: Any) -> Any:
            if dry_run:
                click.echo("[DRY-RUN] Preview mode - no changes will be made", err=True)
                kwargs["dry_run"] = True
            elif not yes:
                click.confirm(message, abort=True, err=True)
                kwargs["dry_run"] = False
            else:
                kwargs["dry_run"] = False

            error: str | None = None
            try:
                return f(*args, **kwargs)
            except BaseException as exc:
                click_ctx = click.get_current_context(silent=True)
                stashed = click_ctx.meta.get(AUDIT_ERROR_KEY) if click_ctx else None
                error = stashed or type(exc).__name__
                raise
            finally:
                _record_audit(dry_run=dry_run, confirmed=yes, params=dict(kwargs), error=error)

        return wrapper  # type: ignore

    return decorator


def confirm_destructive_when(
    predicate: Callable[[dict[str, Any]], bool], message: str
) -> Callable[[F], F]:
    """Like :func:`confirm_destructive`, gated on whether this invocation mutates.

    Some commands are a plain read in their default form and only become
    destructive when a specific option is present (``project access PROJECT``
    is a GET; ``project access PROJECT --set public`` is a PUT). ``--yes`` and
    ``--dry-run`` are still added unconditionally, so the flags exist and
    behave predictably in ``--help``, but the confirmation prompt, the
    dry-run notice, and the audit write are all skipped when
    ``predicate(kwargs)`` is False -- a read is not a destructive operation
    and must not demand ``--yes`` or grow an audit entry.

    Args:
        predicate: Called with the command's keyword arguments (after
            ``yes``/``dry_run`` are pulled out); True means this invocation
            mutates and should be gated like :func:`confirm_destructive`.
        message: Confirmation prompt shown when the predicate is True.
    """

    def decorator(f: F) -> F:
        @click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
        @click.option("--dry-run", is_flag=True, help="Preview without making changes")
        @wraps(f)
        def wrapper(*args: Any, yes: bool, dry_run: bool, **kwargs: Any) -> Any:
            if not predicate(kwargs):
                kwargs["dry_run"] = False
                return f(*args, **kwargs)

            if dry_run:
                click.echo("[DRY-RUN] Preview mode - no changes will be made", err=True)
                kwargs["dry_run"] = True
            elif not yes:
                click.confirm(message, abort=True, err=True)
                kwargs["dry_run"] = False
            else:
                kwargs["dry_run"] = False

            error: str | None = None
            try:
                return f(*args, **kwargs)
            except BaseException as exc:
                click_ctx = click.get_current_context(silent=True)
                stashed = click_ctx.meta.get(AUDIT_ERROR_KEY) if click_ctx else None
                error = stashed or type(exc).__name__
                raise
            finally:
                _record_audit(dry_run=dry_run, confirmed=yes, params=dict(kwargs), error=error)

        return wrapper  # type: ignore

    return decorator


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
        if not profile.verify_ssl and not profile.ca_bundle:
            print_warning(
                redact_url_query(
                    f"TLS certificate verification is DISABLED for the transfer "
                    f"destination {profile.url}. Prefer 'ca_bundle' in the profile "
                    "for self-signed certs."
                )
            )
        return XNATClient(
            base_url=profile.url,
            username=username,
            password=password,
            timeout=profile.timeout,
            verify_ssl=profile.verify_ssl,
            ca_bundle=profile.ca_bundle,
            auto_reauth=True,
        )
    if dest_url:
        return XNATClient(
            base_url=dest_url,
            username=dest_user,
            password=dest_pass,
            auto_reauth=True,
        )
    raise ConfigurationError("Destination not specified. Use --dest-profile or --dest-url.")
