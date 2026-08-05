"""Common CLI utilities, decorators, and helpers."""

from __future__ import annotations

import enum
import json
import logging
import sys
import traceback
from collections.abc import Callable
from functools import wraps
from typing import Any, NamedTuple, TypeVar

import click

from xnatctl.core.auth import AuthManager
from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config, Profile, get_credentials
from xnatctl.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    OperationCancelledError,
    PermissionDeniedError,
    ProfileNotFoundError,
    ResourceNotFoundError,
    XNATCtlError,
)
from xnatctl.core.exceptions import (
    ConnectionError as XNATConnectionError,
)
from xnatctl.core.logging import debug_env_enabled, get_audit_logger, setup_logging
from xnatctl.core.output import OutputFormat, print_error, print_hint, print_warning
from xnatctl.core.redact import SECRET_QUERY_KEYS, redact_url_query

F = TypeVar("F", bound=Callable[..., Any])

# Click context key used to pass the real exception class from handle_errors
# to the audit record, across the SystemExit that separates them.
AUDIT_ERROR_KEY = "xnatctl.audit_error"


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

        # Get credentials (env vars > profile config). If we are using a cached
        # session token, keep the cached username as a hint for current-user
        # lookups on servers where /data/user returns a user listing.
        username, password = get_credentials(profile)
        session = self.auth_manager.load_session(profile.url)
        token = self.auth_manager.get_token_from_env() or (session.token if session else None)
        username_hint = username or (session.username if session else None)

        # Make a disabled-TLS profile impossible to miss for interactive users
        # (the client-layer logger.warning only shows under --verbose).
        if not profile.verify_ssl and not profile.ca_bundle:
            print_warning(
                redact_url_query(
                    f"TLS certificate verification is DISABLED for {profile.url}. "
                    "Prefer 'ca_bundle' in the profile for self-signed certs."
                )
            )

        self.client = XNATClient(
            base_url=profile.url,
            username=username_hint,
            password=password,
            session_token=token,
            timeout=profile.timeout,
            verify_ssl=profile.verify_ssl,
            ca_bundle=profile.ca_bundle,
            # Transparently re-authenticate on a mid-command 401 so a session
            # that expires during a slow mutating operation (e.g. a large
            # prearchive archive that outlasts the ~15-min JSESSIONID) does not
            # surface a successful operation as a failure (see issue #20). This
            # only engages when a password is available; token-only clients fall
            # back to raising SessionExpiredError as before.
            auto_reauth=True,
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
    """Add global options to a command.

    The four shared flags (``--profile``, ``--output``, ``--quiet``,
    ``--verbose``) are also declared on the root ``cli`` group. When the
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
        type=click.Choice(["json", "table"]),
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
    @pass_context
    @wraps(f)
    def wrapper(
        ctx: Context,
        profile: str | None,
        output_format: str | None,
        quiet: bool,
        verbose: bool,
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

        ctx.profile_name = effective_profile
        ctx.output_format = effective_format
        ctx.quiet = effective_quiet
        ctx.verbose = effective_verbose

        # Setup logging
        setup_logging(quiet=effective_quiet, verbose=effective_verbose)

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


# Parameter names whose values must never reach the audit log. Built from the
# canonical secret-shaped key set plus the CLI's own credential flags.
_AUDIT_SECRET_PARAMS = SECRET_QUERY_KEYS | {"dest_pass", "dest_password", "passphrase"}

# Parameters that describe *how* a command ran rather than *what* it touched.
_AUDIT_UNINTERESTING_PARAMS = frozenset(
    {"yes", "dry_run", "output_format", "quiet", "verbose", "profile"}
)


def _audit_details(params: dict[str, Any]) -> dict[str, Any]:
    """Reduce a command's parameters to the identifiers worth recording.

    A denylist rather than an allowlist: the interesting fields differ per
    command, and an allowlist would silently record nothing for any command
    added later. Secret-shaped names are dropped outright, and every surviving
    string is redacted on the way out.
    """
    details: dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value is False:
            continue
        if key in _AUDIT_SECRET_PARAMS or key in _AUDIT_UNINTERESTING_PARAMS:
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
    except Exception as e:  # pragma: no cover - defensive
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


class DeprecatedFlag(NamedTuple):
    """A CLI flag that still works but is scheduled for removal."""

    replacement: str
    """What to use instead. Empty when the flag simply no longer does anything."""

    removed_in: str
    """The release that deletes the flag."""


DEPRECATED_FLAGS: dict[str, DeprecatedFlag] = {
    "--no-parallel": DeprecatedFlag("--workers 1", "0.5.0"),
    "--parallel": DeprecatedFlag("", "0.5.0"),
    "--unzip": DeprecatedFlag("--extract", "0.5.0"),
    "--no-unzip": DeprecatedFlag("--no-extract", "0.5.0"),
    "--cleanup": DeprecatedFlag("", "0.5.0"),
    "--no-cleanup": DeprecatedFlag("--extract --keep-zips", "0.5.0"),
    "--include-resources": DeprecatedFlag("--session-resources", "0.5.0"),
    "--session": DeprecatedFlag("--experiment", "0.5.0"),
    "--gradual": DeprecatedFlag("--mode gradual", "0.5.0"),
    "--archive-format": DeprecatedFlag("--mode", "0.5.0"),
}
"""Every deprecated flag, what replaces it, and when it goes away.

Registering a flag here is what dates the deprecation: the warning text names
the removal release from this table, so nobody has to read the changelog to
find out how long their script has. ``tests/test_deprecation_policy.py`` walks
the whole command tree and fails on any deprecated option missing from it,
which is what stops a flag being quietly retired without notice.

The removal release is at least two MINOR releases out from the one that
deprecated the flag -- see ``docs/stability.rst``.
"""


def deprecation_message(old_flag: str) -> str:
    """Build the stderr warning for a deprecated flag.

    Args:
        old_flag: The deprecated flag name, as registered in DEPRECATED_FLAGS.

    Returns:
        The full warning line.

    Raises:
        KeyError: If the flag is not registered.
    """
    entry = DEPRECATED_FLAGS[old_flag]
    guidance = f"use {entry.replacement} instead" if entry.replacement else "it has no effect"
    return (
        f"Warning: {old_flag} is deprecated and will be removed in {entry.removed_in}; {guidance}"
    )


def _flag_given(ctx: click.Context, param: click.Parameter) -> bool:
    """Report whether the user actually typed this option."""
    return bool(
        param.name
        and ctx.get_parameter_source(param.name) == click.core.ParameterSource.COMMANDLINE
    )


def _make_alias_cb(
    old_flag: str,
    target_param: str,
    target_value: Any,
) -> Callable[[click.Context, click.Parameter, Any], Any]:
    """Create a Click callback that warns on a deprecated flag and sets a fixed value.

    Args:
        old_flag: The deprecated flag name (e.g., "--unzip"). Must be registered
            in DEPRECATED_FLAGS; the lookup happens here, at import time, so an
            unregistered flag breaks the test suite instead of a user's command.
        target_param: The Click parameter name to set on ctx.params.
        target_value: The fixed value to set (NOT the raw flag value).

    Returns:
        A Click callback function.
    """
    message = deprecation_message(old_flag)

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
        if _flag_given(ctx, param):
            click.echo(message, err=True)
            ctx.params[target_param] = target_value
        return value

    return callback


def _make_forwarding_alias_cb(
    old_flag: str,
    target_param: str,
) -> Callable[[click.Context, click.Parameter, Any], Any]:
    """Create a Click callback that warns and forwards the user's raw value.

    Unlike ``_make_alias_cb`` which sets a fixed value, this forwards whatever
    the user provided (useful for value-taking options like ``--session LABEL``).

    Args:
        old_flag: The deprecated flag name. Must be registered in DEPRECATED_FLAGS.
        target_param: The Click parameter name to set on ctx.params.

    Returns:
        A Click callback function.
    """
    message = deprecation_message(old_flag)

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
        if value is not None and _flag_given(ctx, param):
            click.echo(message, err=True)
            ctx.params[target_param] = value
        return value

    return callback


def _make_noop_cb(old_flag: str) -> Callable[[click.Context, click.Parameter, Any], Any]:
    """Create a Click callback for a deprecated flag that no longer does anything.

    Accepting the flag in silence looks like it still works. Warning says so.

    Args:
        old_flag: The deprecated flag name. Must be registered in DEPRECATED_FLAGS.

    Returns:
        A Click callback function.
    """
    message = deprecation_message(old_flag)

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
        if _flag_given(ctx, param):
            click.echo(message, err=True)
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
        callback=_make_alias_cb("--no-parallel", "workers", 1),
    )
    @click.option(
        "--parallel",
        is_flag=True,
        hidden=True,
        expose_value=False,
        callback=_make_noop_cb("--parallel"),
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


def _debug_enabled() -> bool:
    """Return True when tracebacks should be surfaced.

    Two independent opt-ins: the ``--verbose`` flag (stashed on the shared
    :class:`Context` as ``verbose``) and the ``XNATCTL_DEBUG`` env var (mirrors
    ``gh``'s ``GH_DEBUG``, so a traceback is obtainable even for failures that
    occur before ``--verbose`` is parsed). ``XNATCTL_DEBUG=0``/``false``/``off``
    counts as OFF -- an explicit falsey value must not enable tracebacks.

    The env var's spelling is parsed by :func:`debug_env_enabled` so this policy
    and the logging tiers it turns on cannot drift apart.
    """
    if debug_env_enabled():
        return True
    click_ctx = click.get_current_context(silent=True)
    ctx_obj = click_ctx.obj if click_ctx is not None else None
    return bool(getattr(ctx_obj, "verbose", False))


def render_cli_error(exc: BaseException) -> int:
    """Render a redacted one-line error (+ optional traceback) and return its exit code.

    Shared by :func:`handle_errors` and the ``main()`` last-resort guard so the
    traceback-under-debug policy lives in exactly one place. ``main()`` needs it
    too because setup-phase failures (config load / env parsing inside
    ``global_options``) are raised OUTSIDE the ``handle_errors`` wrapper.

    Must be called from within an ``except`` block: it relies on
    ``traceback.format_exc()`` reflecting the exception currently being handled.
    """
    debug = _debug_enabled()

    if isinstance(exc, XNATCtlError):
        # Defensive: print_error already redacts, but we redact here too so
        # future direct callers cannot bypass the invariant.
        print_error(redact_url_query(str(exc)))
        if exc.hint:
            print_hint(exc.hint)
        if debug and exc.details:
            # The details dict used to be glued onto every message. It is real
            # diagnostic data, so it stays -- just behind --verbose, where the
            # reader asked for it.
            detail_str = ", ".join(f"{k}={v}" for k, v in exc.details.items())
            click.echo(redact_url_query(f"Details: {detail_str}"), err=True)
    else:
        print_error(redact_url_query(f"Unexpected error: {exc}"))

    if debug:
        # Redact the whole traceback: its final line echoes the exception
        # message, which may carry a URL query string.
        click.echo(redact_url_query(traceback.format_exc()), err=True)
    elif not isinstance(exc, XNATCtlError):
        # A clean XNATCtlError is already actionable; only the opaque
        # "Unexpected error" line benefits from the verbose hint.
        click.echo("Run with --verbose for a full traceback.", err=True)

    return exit_code_for(exc)


def handle_errors(f: F) -> F:
    """Handle common errors and convert to CLI exceptions.

    Under ``--verbose`` or ``XNATCTL_DEBUG=1`` a full (redacted) traceback is
    printed to stderr before exiting; otherwise unexpected errors print a single
    line plus a hint. Tracebacks are never shown by default.
    """

    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        """Capture errors and exit with consistent messaging."""
        try:
            return f(*args, **kwargs)
        except click.Abort:
            # User declined a destructive-op confirmation (Ctrl+C / "n").
            click.echo("Aborted!", err=True)
            sys.exit(ExitCode.USER_CANCELLED)
        except KeyboardInterrupt:
            # Ctrl+C mid-operation. Caught here rather than left to Click,
            # which converts it to Abort and exits 1 -- indistinguishable from
            # a general error, despite the taxonomy defining a code for exactly
            # this. Catching it inside the command body also keeps the exit
            # code consistent with the confirmation-prompt Abort above.
            #
            # KeyboardInterrupt is not an Exception subclass, so the broad
            # handler below would never have seen it.
            click.echo("Cancelled.", err=True)
            sys.exit(ExitCode.USER_CANCELLED)
        except click.ClickException:
            raise
        except Exception as e:
            # Stash the real class before collapsing to SystemExit, so the
            # audit record names the actual failure.
            click_ctx = click.get_current_context(silent=True)
            if click_ctx is not None:
                click_ctx.meta[AUDIT_ERROR_KEY] = type(e).__name__
            sys.exit(render_cli_error(e))

    return wrapper  # type: ignore


# =============================================================================
# Destination Profile Helpers
# =============================================================================


def reject_argv_password(
    alternatives: str,
) -> Callable[[click.Context, click.Parameter, Any], None]:
    """Build a Click callback that rejects an argv-supplied password.

    A password on argv is visible in ``ps``, ``/proc/*/cmdline``, and shell
    history. The option this is wired to survives only as a deterrent that
    surfaces the secret-sourcing contract in ``--help``; supplying a value is
    unconditionally a :class:`click.UsageError` (exit 2) raised at parse time,
    before any authentication or context decorator runs. The callback never
    propagates a value into the command body.

    Args:
        alternatives: One sentence naming the supported sources, appended to
            the refusal message (e.g. "Use --password-stdin, ...").
    """

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> None:
        del ctx  # The rejection is unconditional.
        if value is not None:
            option = (param.opts or [param.name])[0]
            raise click.UsageError(
                f"Refusing to read {option} from argv (visible in ps, "
                f"/proc/*/cmdline, and shell history). {alternatives}"
            )
        return

    return callback


def read_password_stdin(flag_name: str) -> str:
    """Read one password line from stdin for a ``--*-stdin`` flag.

    ``readline`` rather than ``read`` so a trailing newline (common with
    ``echo``) is stripped without consuming any further stdin bytes -- the
    command may still need the rest of the stream.

    Raises:
        click.UsageError: If stdin yields an empty line.
    """
    secret = sys.stdin.readline()
    if secret.endswith("\n"):
        secret = secret[:-1]
    if not secret:
        raise click.UsageError(f"{flag_name} was set but stdin was empty.")
    return secret


def dest_profile_options(f: F) -> F:
    """Add destination profile options for transfer commands.

    ``--dest-pass`` is argv-rejected; the wrapper resolves
    ``--dest-pass-stdin`` here and injects the secret as ``dest_pass``, so the
    wrapped commands keep their existing signatures.
    """

    @click.option("--dest-profile", help="Destination config profile name")
    @click.option("--dest-url", hidden=True, help="Destination XNAT URL (inline)")
    @click.option("--dest-user", hidden=True, help="Destination username (inline)")
    @click.option(
        "--dest-pass",
        hidden=True,
        expose_value=False,
        is_eager=True,
        callback=reject_argv_password(
            "Use --dest-pass-stdin, or --dest-profile with stored credentials."
        ),
        help="REFUSED: use --dest-pass-stdin or --dest-profile",
    )
    @click.option(
        "--dest-pass-stdin",
        is_flag=True,
        help="Read the destination password from stdin (one line)",
    )
    @wraps(f)
    def wrapper(*args: Any, dest_pass_stdin: bool = False, **kwargs: Any) -> Any:
        kwargs["dest_pass"] = read_password_stdin("--dest-pass-stdin") if dest_pass_stdin else None
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
    if dest_url:
        return XNATClient(
            base_url=dest_url,
            username=dest_user,
            password=dest_pass,
        )
    raise ConfigurationError("Destination not specified. Use --dest-profile or --dest-url.")


# =============================================================================
# Exit Codes
# =============================================================================


class ExitCode(enum.IntEnum):
    """Documented, differentiated CLI exit codes.

    Code 2 is intentionally skipped: Click reserves it for usage errors, so
    reusing it would make "wrong flags" indistinguishable from an auth failure.
    Codes only ever become MORE specific than the old blanket 1, so scripts that
    test ``!= 0`` keep working.
    """

    SUCCESS = 0
    GENERAL_ERROR = 1
    # 2 is reserved for Click usage errors (do not assign it here).
    AUTH_ERROR = 3
    NETWORK_ERROR = 4
    NOT_FOUND = 5
    PERMISSION_ERROR = 6
    USER_CANCELLED = 7


def exit_code_for(exc: BaseException) -> int:
    """Map an exception to its documented CLI exit code (see :class:`ExitCode`).

    Order matters: ``PermissionDeniedError`` and ``SessionExpiredError`` subclass
    ``AuthenticationError``, so the most specific classes are checked first.
    """
    if isinstance(exc, click.Abort | OperationCancelledError):
        return ExitCode.USER_CANCELLED
    if isinstance(exc, PermissionDeniedError):
        return ExitCode.PERMISSION_ERROR
    if isinstance(exc, AuthenticationError):  # incl. SessionExpiredError
        return ExitCode.AUTH_ERROR
    if isinstance(exc, ResourceNotFoundError):
        return ExitCode.NOT_FOUND
    if isinstance(exc, XNATConnectionError):
        # NetworkError, TimeoutError, RetryExhaustedError, ServerUnreachableError
        return ExitCode.NETWORK_ERROR
    # ClientRequestError/ServerError ("server said no") and everything else.
    return ExitCode.GENERAL_ERROR
