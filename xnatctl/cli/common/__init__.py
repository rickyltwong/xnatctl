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

# The imports above are `xnatctl.cli.common`'s importable surface: the
# project symbols (the exceptions, FILE_ONLY_ATTR, debug_env_enabled,
# print_error, print_hint) are real API that callers and tests import from
# here even though they are defined in submodules. Stdlib and typing names
# (`enum`, `fnmatch`, `json`, `NamedTuple`, `Sequence`, `overload`,
# `traceback`) are deliberately NOT part of that surface: nobody should
# import `json` from a CLI helper module.
#
# Below, the `X as X` redundant-alias idiom marks private helpers that
# nonetheless cross module boundaries -- the deprecated-flag callbacks are
# shared by four command modules, and `_parse_batch_text`/`_flag_given` are
# patched by dotted name in the tests. With an `__all__` present, mypy's
# strict no-implicit-reexport treats anything not aliased this way as private
# to the module, so a plain import would make every consumer's import an
# error. The idiom says "deliberately re-exported" without promoting these
# into the package's public surface; `services/downloads/__init__.py` uses the
# same pattern for the same reason.
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
    # This list is the package's public surface: names a CLI module (or a
    # test) is meant to import from `xnatctl.cli.common`. Underscore-prefixed
    # helpers (`_audit_details`, `_flag_given`, `_make_alias_cb`, etc.) are
    # deliberately excluded -- they stay importable by dotted name for the
    # handful of call sites and tests that use them, but they are private
    # implementation detail, not the package's API.
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
        # Root-group values populated by ``xnatctl/cli/main.py``. Used by
        # the per-subcommand ``@global_options`` decorator to fall back to
        # the root-group value when the subcommand received Click's default
        # for the same flag. ``None`` means "no root value was supplied".
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

        # ProfileNotFoundError now carries its own next-step hint, so it is left
        # to propagate rather than being restated as a ConfigurationError -- that
        # wrapper duplicated the hint text and discarded the more specific type
        # . Both map to the same exit code.
        profile = self.config.get_profile(self.profile_name)

        # Credential resolution (env > profile, cached/env token, username hint,
        # auto_reauth) lives in the shared resolver so a library user
        # (XNATClient.from_profile) gets the exact same client this command
        # would. Resolved BEFORE the TLS warning prints, so a resolver failure
        # raises without emitting output -- the pre-refactor order.
        params = resolve_client_params(
            self.profile_name,
            config=self.config,
            auth_manager=self.auth_manager,
        )

        # Make a disabled-TLS profile impossible to miss for interactive users
        # (the client-layer logger.warning only shows under --verbose). This is
        # CLI rendering, so it stays here rather than in build_client_from_profile
        # -- the builder is shared with library callers and must not print.
        if not profile.verify_ssl and not profile.ca_bundle:
            print_warning(
                redact_url_query(
                    f"TLS certificate verification is DISABLED for {profile.url}. "
                    "Prefer 'ca_bundle' in the profile for self-signed certs."
                )
            )

        # Construction stays here, through the module-level XNATClient, so it
        # is the one client every command shares (and tests can patch).
        self.client = XNATClient(**params)

        return self.client


pass_context = click.make_pass_decorator(Context, ensure=True)


# =============================================================================
# Global Options
# =============================================================================


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
        """Populate context from global options and invoke the command."""
        click_ctx = click.get_current_context()

        def _was_explicit(param_name: str) -> bool:
            """Return True only if the user (or env var) set the option."""
            source = click_ctx.get_parameter_source(param_name)
            return source is not None and source != click.core.ParameterSource.DEFAULT

        # Profile: subcommand explicit > root > None.
        if _was_explicit("profile"):
            effective_profile = profile
        elif ctx.root_profile_name is not None:
            effective_profile = ctx.root_profile_name
        else:
            effective_profile = profile

        # Output format: subcommand explicit > root > table default.
        if _was_explicit("output_format") and output_format is not None:
            effective_format = OutputFormat.from_string(output_format)
        elif ctx.root_output_format is not None:
            effective_format = ctx.root_output_format
        elif output_format is not None:
            effective_format = OutputFormat.from_string(output_format)
        else:
            effective_format = OutputFormat.TABLE

        # Quiet / verbose are booleans: an explicit subcommand-level flip
        # (the only way to leave the False default) wins; otherwise inherit
        # the root-group value.
        effective_quiet = quiet if _was_explicit("quiet") else (quiet or ctx.root_quiet)
        effective_verbose = verbose if _was_explicit("verbose") else (verbose or ctx.root_verbose)

        # No-color: subcommand explicit > root flag > NO_COLOR/CLICOLOR=0 env,
        # checked fresh here (not just at console-construction time) so the
        # env vars take effect for this invocation regardless of import order.
        effective_no_color = (
            no_color if _was_explicit("no_color") else (no_color or ctx.root_no_color)
        ) or no_color_requested()

        # No-headers: same boolean-flag rule as quiet/verbose/no-color --
        # an explicit subcommand-level flip wins, otherwise inherit the root
        # group's value.
        effective_no_headers = (
            no_headers if _was_explicit("no_headers") else (no_headers or ctx.root_no_headers)
        )

        # Log file: the root callback (cli/main.py) already resolved and
        # activated the flag/env tiers for THIS invocation, unconditionally --
        # that is what makes those two tiers work for commands that carry no
        # @global_options at all. Two things are still this decorator's job:
        #
        # 1. A MORE SPECIFIC override: a --log-file given literally on the
        #    command line for THIS subcommand, as opposed to one merely
        #    re-resolved from the same env var the root callback already saw.
        #    Checked as COMMANDLINE specifically, NOT via the ``_was_explicit``
        #    helper above (which also treats ENVIRONMENT as "explicit"): with
        #    XNATCTL_LOG_FILE set, Click resolves THIS subcommand's own
        #    ``log_file`` parameter from the env var too, independently of
        #    the root's -- so "explicit incl. env" would make a plain env var
        #    look like a subcommand override and beat a genuine
        #    ``xnatctl --log-file X <command>`` root flag, which must win.
        if (
            log_file
            and click_ctx.get_parameter_source("log_file") == click.core.ParameterSource.COMMANDLINE
        ):
            ctx.log_file = log_file
            install_log_file(log_file)
        # else: leave whatever the root callback already resolved/installed
        # (ctx.log_file was already set there) untouched.

        ctx.profile_name = effective_profile
        ctx.output_format = effective_format
        ctx.quiet = effective_quiet
        ctx.verbose = effective_verbose
        ctx.no_color = effective_no_color
        ctx.no_headers = effective_no_headers

        # Setup logging
        setup_logging(quiet=effective_quiet, verbose=effective_verbose)
        set_no_color(effective_no_color)
        set_no_headers(effective_no_headers)

        # Load config
        ctx.config = Config.load()

        # 2. The config `log_file:` tier -- only reachable once ``ctx.config``
        # exists, and only when NEITHER the flag nor the env var resolved
        # anything above (a truthy ctx.log_file already wins). This is the
        # one Config.load() call the whole feature needs for a decorated
        # command: it is not a second load added for this feature, it is the
        # load @global_options already makes for every command, reused. The
        # root callback deliberately does not attempt this tier itself (see
        # its own comment) -- an unconditional second Config.load() there
        # would run before an eager argv-validation callback like
        # `auth login --password`'s ever gets a chance to reject, and would
        # warn twice on one broken config.yaml instead of once. getattr, not
        # `ctx.config.log_file`: a few tests patch the ``Config.load`` seam
        # with a bare stand-in object that carries none of Config's fields.
        if not ctx.log_file:
            config_log_file = getattr(ctx.config, "log_file", None)
            # isinstance, not truthiness alone: a test replacing the
            # Config.load seam with a MagicMock auto-creates a truthy Mock
            # for .log_file, and installing that literally created a
            # MagicMock/ directory tree in the working dir.
            if isinstance(config_log_file, str) and config_log_file:
                ctx.log_file = config_log_file
                install_log_file(config_log_file)

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


# Parameter names whose values must never reach the audit log. Built from the
# canonical secret-shaped key set plus the CLI's own credential flags.
_AUDIT_SECRET_PARAMS = SECRET_QUERY_KEYS | {"dest_pass", "dest_password", "passphrase"}

# Parameters whose value is replaced with "***" rather than omitted outright --
# the audit record should show that something was set without ever revealing
# what. "value" covers `admin site-config set KEY VALUE`: VALUE can be an
# arbitrary site-config setting (an SMTP password, an OAuth client secret,
# ...), and the KEY name that would hint at that is unpredictable across
# deployments, so it is always masked rather than guessed at from the key.
_AUDIT_MASKED_PARAMS = frozenset({"value"})

# Parameters that describe *how* a command ran rather than *what* it touched.
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
    """Reduce a command's parameters to the identifiers worth recording.

    A denylist rather than an allowlist: the interesting fields differ per
    command, and an allowlist would silently record nothing for any command
    added later. Secret-shaped names are dropped outright, masked names are
    replaced with "***" (so the record shows something was set without
    revealing what), and every surviving string is redacted on the way out.
    """
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
    """Append one audit record for a destructive command. Never raises."""
    try:
        click_ctx = click.get_current_context(silent=True)
        ctx_obj = click_ctx.obj if click_ctx is not None else None
        command = click_ctx.command_path if click_ctx is not None else None

        # profile_name is None whenever the user relied on the default profile,
        # which is the common case -- fall back so the field is not usually blank.
        config = getattr(ctx_obj, "config", None)
        profile_name = getattr(ctx_obj, "profile_name", None) or getattr(
            config, "default_profile", None
        )

        # Read the client only if one was already built; never construct one
        # just to log. A --dry-run never builds a client, so fall back to the
        # profile -- "which server was this aimed at" is exactly what the record
        # is for.
        client = getattr(ctx_obj, "client", None)
        profile = get_profile(ctx_obj) if isinstance(ctx_obj, Context) else None
        server = getattr(client, "base_url", None) or getattr(profile, "url", None)
        user = getattr(client, "username", None) or getattr(profile, "username", None)

        get_audit_logger().log_operation(
            # The command path is the operation identifier: unambiguous, and it
            # cannot drift out of sync with a hand-written name.
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
        # Auditing must never be the reason a delete fails.
        logging.getLogger(__name__).warning("Could not record audit entry: %s", e)


def confirm_destructive(message: str) -> Callable[[F], F]:
    """Require confirmation for destructive operations.

    Also the audit seam: carrying this decorator is what marks a
    command as state-changing, so the record is written here rather than in
    each command. That keeps coverage automatic -- a new destructive command is
    audited by virtue of being declared destructive.
    """

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
                # An aborted confirmation is not an audited event: nothing was
                # attempted, and logging it would bury real changes in noise.
                click.confirm(message, abort=True, err=True)
                kwargs["dry_run"] = False
            else:
                kwargs["dry_run"] = False

            error: str | None = None
            try:
                return f(*args, **kwargs)
            except BaseException as exc:
                # handle_errors converts failures to SystemExit, so the useful
                # class name is the one it stashed rather than SystemExit itself.
                click_ctx = click.get_current_context(silent=True)
                stashed = click_ctx.meta.get(AUDIT_ERROR_KEY) if click_ctx else None
                error = stashed or type(exc).__name__
                raise
            finally:
                # In a finally so a command that exits mid-way is still recorded.
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
        """Wrap a command to conditionally enforce confirmation/dry-run behavior."""

        @click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
        @click.option("--dry-run", is_flag=True, help="Preview without making changes")
        @wraps(f)
        def wrapper(*args: Any, yes: bool, dry_run: bool, **kwargs: Any) -> Any:
            """Gate on the predicate, then behave exactly like confirm_destructive."""
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


# =============================================================================
# Destination Profile Helpers
# =============================================================================


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
    # Built like the source client in Context.get_client, and for the same
    # reasons. Dropping either of the settings below makes the destination
    # of a cross-server transfer strictly weaker than its source: a
    # destination behind a private CA could not verify TLS at all (pushing
    # people towards verify_ssl: false), and the destination side of a
    # multi-hour transfer would never re-authenticate after its session
    # expired.
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
