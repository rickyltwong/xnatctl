"""Authentication commands for xnatctl.

These commands sit on the same decorator stack as every other command
(``@global_options`` + ``@handle_errors``). They used to declare their own
``-p``/``-o`` -- with the output choices in the opposite order, and no ``-q``
or ``-v`` at all -- and hand-roll ``print_error`` + ``SystemExit``. That last
part meant the actionable hints attached to the exceptions never
rendered here, because nothing on this path went through ``render_cli_error``.

``auth login`` legitimately skips ``@require_auth``: it is what establishes the
session that decorator requires.
"""

from __future__ import annotations

import click

from xnatctl.cli.common import (
    Context,
    global_options,
    handle_errors,
    read_password_stdin,
    reject_argv_password,
)
from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import ConfigurationError
from xnatctl.core.output import (
    OutputFormat,
    print_json,
    print_key_value,
    print_success,
    print_warning,
)


@click.group()
def auth() -> None:
    """Manage authentication credentials."""
    pass


def do_login(
    ctx: Context,
    profile: Profile,
    username: str | None = None,
    password: str | None = None,
    profile_name: str | None = None,
) -> dict[str, object]:
    """Authenticate against ``profile`` and cache the session.

    Factored out of the ``auth login`` command so ``config init`` can offer to
    continue straight into a login, rather than leaving the user at a cliff
    between two commands.

    Args:
        ctx: CLI context, used for the auth manager and output format.
        profile: Profile to authenticate against.
        username: Explicit username, else env vars, else the profile, else prompt.
        password: Explicit password, else env vars, else the profile, else prompt.
        profile_name: Name of the profile, for messages.

    Returns:
        A summary dict describing the established session.

    Raises:
        AuthenticationError: If the server rejects the credentials.
    """
    auth_mgr = ctx.auth_manager

    # Credentials: CLI args > env vars > profile config > prompt.
    env_user, env_pass = auth_mgr.get_credentials()
    user = username or env_user or profile.username
    pwd = password or env_pass or profile.resolve_password()

    if not user:
        user = click.prompt("Username")
    if not pwd:
        pwd = click.prompt("Password", hide_input=True)

    # Progress chatter belongs on stderr so it never contaminates -o json.
    click.echo(f"Authenticating with {profile.url}...", err=True)

    client = XNATClient(
        base_url=profile.url,
        username=user,
        password=pwd,
        verify_ssl=profile.verify_ssl,
        ca_bundle=profile.ca_bundle,
        timeout=profile.timeout,
    )

    try:
        token = client.authenticate()

        # Prefer the username the server reports: XNAT may authenticate an
        # alias to a different account.
        actual_user = user
        try:
            user_info = client.whoami()
            if user_info.get("username"):
                actual_user = user_info["username"]
        except Exception:
            pass

        session = auth_mgr.save_session(token=token, url=profile.url, username=actual_user)
    finally:
        client.close()

    return {
        "status": "authenticated",
        "username": actual_user,
        "requested_username": user,
        "url": profile.url,
        "profile": profile_name,
        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
    }


def report_login(ctx: Context, result: dict[str, object]) -> None:
    """Render the outcome of :func:`do_login` in the requested format."""
    if ctx.output_format == OutputFormat.JSON:
        print_json(result)
        return

    print_success(f"Logged in to {result['url']} as {result['username']}")
    if result["username"] != result["requested_username"]:
        print_warning(
            f"Credentials authenticated as {result['username']} "
            f"(requested username was {result['requested_username']})"
        )
    if result["expires_at"]:
        click.echo(f"Session expires at {str(result['expires_at'])[11:16]} (15m)", err=True)


@auth.command("login")
@click.option("--username", "-u", help="Username")
@click.option(
    "--password",
    expose_value=False,
    is_eager=True,
    callback=reject_argv_password(
        "Use --password-stdin, set XNAT_PASS, or let the command prompt."
    ),
    help="REFUSED: use --password-stdin, XNAT_PASS, or the prompt",
)
@click.option(
    "--password-stdin",
    is_flag=True,
    help="Read the password from stdin (one line)",
)
@global_options
@handle_errors
def auth_login(ctx: Context, username: str | None, password_stdin: bool) -> None:
    """Login and create a session.

    Authenticates with the XNAT server and caches the session token.
    Credentials come from --password-stdin, environment variables
    (XNAT_USER, XNAT_PASS), the selected profile config, or an interactive
    prompt. A password on argv is refused: it would be visible in ps and
    shell history.

    \b
    Example:
        xnatctl auth login
        xnatctl auth login --profile myserver
        xnatctl auth login -u admin
        echo "$PASS" | xnatctl auth login -u admin --password-stdin
    """
    config = ctx.config or Config.load()
    profile = config.get_profile(ctx.profile_name)

    password = read_password_stdin("--password-stdin") if password_stdin else None

    result = do_login(
        ctx,
        profile,
        username=username,
        password=password,
        profile_name=ctx.profile_name or config.default_profile,
    )
    report_login(ctx, result)


@auth.command("logout")
@global_options
@handle_errors
def auth_logout(ctx: Context) -> None:
    """Clear cached session.

    \b
    Example:
        xnatctl auth logout
        xnatctl auth logout --profile myserver
    """
    config = ctx.config or Config.load()
    profile = config.get_profile(ctx.profile_name)
    auth_mgr = ctx.auth_manager

    session = auth_mgr.load_session(profile.url)
    if session:
        client = XNATClient(
            base_url=profile.url,
            session_token=session.token,
            verify_ssl=profile.verify_ssl,
            ca_bundle=profile.ca_bundle,
        )
        client.invalidate_session()
        client.close()

    if auth_mgr.clear_session():
        print_success("Logged out")
    else:
        print_warning("No cached session found")


@auth.command("status")
@global_options
@handle_errors
def auth_status(ctx: Context) -> None:
    """Check authentication status.

    \b
    Example:
        xnatctl auth status
        xnatctl auth status --profile myserver
    """
    config = ctx.config or Config.load()
    profile = config.get_profile(ctx.profile_name)
    auth_mgr = ctx.auth_manager

    session_info = auth_mgr.get_session_info(profile.url)
    env_user, env_pass = auth_mgr.get_credentials()
    env_token = auth_mgr.get_token_from_env()

    status: dict[str, object] = {
        "url": profile.url,
        "env_username": env_user or "(not set)",
        "env_password": "(set)" if env_pass else "(not set)",
        "env_token": "(set)" if env_token else "(not set)",
        "session_cached": session_info is not None,
    }

    if session_info:
        status.update(
            {
                "session_username": session_info["username"],
                "session_created": session_info["created_at"],
                "session_expires": session_info["expires_at"],
                "session_expired": session_info["is_expired"],
            }
        )

    if ctx.output_format == OutputFormat.JSON:
        print_json(status)
    else:
        print_key_value(
            status,
            title=f"Auth Status: {ctx.profile_name or config.default_profile}",
        )


@auth.command("test")
@global_options
@handle_errors
def auth_test(ctx: Context) -> None:
    """Test authentication by connecting to server.

    \b
    Example:
        xnatctl auth test
        xnatctl auth test --profile myserver
    """
    config = ctx.config or Config.load()
    profile = config.get_profile(ctx.profile_name)
    auth_mgr = ctx.auth_manager

    # Session token first, then credentials (env vars > profile config).
    session_token = auth_mgr.get_session_token(profile.url)
    env_user, env_pass = auth_mgr.get_credentials()
    user = env_user or profile.username
    pwd = env_pass or profile.resolve_password()

    if session_token:
        click.echo("Testing with cached session...", err=True)
        client = XNATClient(
            base_url=profile.url,
            session_token=session_token,
            verify_ssl=profile.verify_ssl,
            ca_bundle=profile.ca_bundle,
            timeout=profile.timeout,
        )
    elif user and pwd:
        click.echo("Testing with credentials...", err=True)
        client = XNATClient(
            base_url=profile.url,
            username=user,
            password=pwd,
            verify_ssl=profile.verify_ssl,
            ca_bundle=profile.ca_bundle,
            timeout=profile.timeout,
        )
        client.authenticate()
    else:
        raise ConfigurationError(
            "No credentials found for this profile.",
            field="credentials",
            hint=(
                "Run 'xnatctl auth login', set XNAT_USER and XNAT_PASS, "
                "or store a password with 'xnatctl config set-password'."
            ),
        )

    try:
        user_info = client.whoami()

        if ctx.output_format == OutputFormat.JSON:
            print_json({"status": "authenticated", "url": profile.url, **user_info})
        else:
            print_success("Authentication successful")
            click.echo(f"User: {user_info.get('username', 'unknown')}")
            name = f"{user_info.get('firstname', '')} {user_info.get('lastname', '')}".strip()
            if name:
                click.echo(f"Name: {name}")
            if user_info.get("email"):
                click.echo(f"Email: {user_info['email']}")
    finally:
        client.close()
