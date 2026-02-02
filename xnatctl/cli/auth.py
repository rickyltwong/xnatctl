"""Authentication commands for xnatctl."""

from __future__ import annotations

import click

from xnatctl.core.auth import AuthManager
from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config
from xnatctl.core.exceptions import (
    AuthenticationError,
    ProfileNotFoundError,
)
from xnatctl.core.output import (
    print_error,
    print_json,
    print_key_value,
    print_success,
    print_warning,
)


@click.group()
def auth() -> None:
    """Manage authentication credentials."""
    pass


@auth.command("login")
@click.option("--profile", "-p", "profile_name", help="Profile to authenticate")
@click.option("--username", "-u", help="Username")
@click.option("--password", help="Password (will prompt if not provided)")
@click.option("--output", "-o", type=click.Choice(["table", "json"]), default="table")
def auth_login(
    profile_name: str | None,
    username: str | None,
    password: str | None,
    output: str,
) -> None:
    """Login and create a session.

    Authenticates with the XNAT server and caches the session token.
    Credentials come from environment variables (XNAT_USER, XNAT_PASS).

    Example:
        xnatctl auth login
        xnatctl auth login --profile myserver
        xnatctl auth login -u admin
    """
    config = Config.load()
    auth_mgr = AuthManager()

    try:
        profile = config.get_profile(profile_name)
    except ProfileNotFoundError as e:
        print_error(str(e))
        raise SystemExit(1) from e

    # Get credentials: CLI args > env vars > profile config > prompt
    env_user, env_pass = auth_mgr.get_credentials()
    user = username or env_user or profile.username
    pwd = password or env_pass or profile.password

    if not user:
        user = click.prompt("Username")

    if not pwd:
        pwd = click.prompt("Password", hide_input=True)

    # Authenticate
    click.echo(f"Authenticating with {profile.url}...")

    client = XNATClient(
        base_url=profile.url,
        username=user,
        password=pwd,
        verify_ssl=profile.verify_ssl,
        timeout=profile.timeout,
    )

    try:
        token = client.authenticate()
        actual_user = user
        try:
            user_info = client.whoami()
            if user_info.get("username"):
                actual_user = user_info["username"]
        except Exception:
            user_info = None

        # Cache session with the resolved username
        session = auth_mgr.save_session(
            token=token,
            url=profile.url,
            username=actual_user,
        )

        if output == "json":
            print_json(
                {
                    "status": "authenticated",
                    "username": actual_user,
                    "url": profile.url,
                    "expires_at": session.expires_at.isoformat() if session.expires_at else None,
                }
            )
        else:
            print_success(f"Logged in as {actual_user}")
            if actual_user != user:
                print_warning(
                    f"Credentials authenticated as {actual_user} (requested username was {user})"
                )
            click.echo(f"Session cached until {session.expires_at}")

    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        raise SystemExit(1) from e
    finally:
        client.close()


@auth.command("logout")
@click.option("--profile", "-p", "profile_name", help="Profile to logout")
def auth_logout(profile_name: str | None) -> None:
    """Clear cached session.

    Example:
        xnatctl auth logout
        xnatctl auth logout --profile myserver
    """
    config = Config.load()
    auth_mgr = AuthManager()

    try:
        profile = config.get_profile(profile_name)
    except ProfileNotFoundError as e:
        print_error(str(e))
        raise SystemExit(1) from e

    # Check if there's a session for this URL
    session = auth_mgr.load_session(profile.url)
    if session:
        # Invalidate on server
        client = XNATClient(
            base_url=profile.url,
            session_token=session.token,
            verify_ssl=profile.verify_ssl,
        )
        client.invalidate_session()
        client.close()

    # Clear local cache
    if auth_mgr.clear_session():
        print_success("Logged out")
    else:
        print_warning("No cached session found")


@auth.command("status")
@click.option("--profile", "-p", "profile_name", help="Profile to check")
@click.option("--output", "-o", type=click.Choice(["table", "json"]), default="table")
def auth_status(profile_name: str | None, output: str) -> None:
    """Check authentication status.

    Example:
        xnatctl auth status
        xnatctl auth status --profile myserver
    """
    config = Config.load()
    auth_mgr = AuthManager()

    try:
        profile = config.get_profile(profile_name)
    except ProfileNotFoundError as e:
        print_error(str(e))
        raise SystemExit(1) from e

    # Check for session
    session_info = auth_mgr.get_session_info(profile.url)
    env_user, env_pass = auth_mgr.get_credentials()
    env_token = auth_mgr.get_token_from_env()

    status = {
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

    if output == "json":
        print_json(status)
    else:
        print_key_value(status, title=f"Auth Status: {profile_name or config.default_profile}")


@auth.command("test")
@click.option("--profile", "-p", "profile_name", help="Profile to test")
@click.option("--output", "-o", type=click.Choice(["table", "json"]), default="table")
def auth_test(profile_name: str | None, output: str) -> None:
    """Test authentication by connecting to server.

    Example:
        xnatctl auth test
        xnatctl auth test --profile myserver
    """
    config = Config.load()
    auth_mgr = AuthManager()

    try:
        profile = config.get_profile(profile_name)
    except ProfileNotFoundError as e:
        print_error(str(e))
        raise SystemExit(1) from e

    # Try session token first, then credentials (env vars > profile config)
    session_token = auth_mgr.get_session_token(profile.url)
    env_user, env_pass = auth_mgr.get_credentials()
    user = env_user or profile.username
    pwd = env_pass or profile.password

    if session_token:
        click.echo("Testing with cached session...")
        client = XNATClient(
            base_url=profile.url,
            session_token=session_token,
            verify_ssl=profile.verify_ssl,
            timeout=profile.timeout,
        )
    elif user and pwd:
        click.echo("Testing with credentials...")
        client = XNATClient(
            base_url=profile.url,
            username=user,
            password=pwd,
            verify_ssl=profile.verify_ssl,
            timeout=profile.timeout,
        )
        try:
            client.authenticate()
        except AuthenticationError as e:
            print_error(f"Authentication failed: {e}")
            raise SystemExit(1) from e
    else:
        print_error(
            "No credentials found. Set XNAT_USER/XNAT_PASS, configure in profile, or use 'xnatctl auth login'"
        )
        raise SystemExit(1)

    try:
        user_info = client.whoami()

        if output == "json":
            print_json(
                {
                    "status": "authenticated",
                    "url": profile.url,
                    **user_info,
                }
            )
        else:
            print_success("Authentication successful")
            click.echo(f"User: {user_info.get('username', 'unknown')}")
            name = f"{user_info.get('firstname', '')} {user_info.get('lastname', '')}".strip()
            if name:
                click.echo(f"Name: {name}")
            if user_info.get("email"):
                click.echo(f"Email: {user_info['email']}")

    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        raise SystemExit(1) from e
    except Exception as e:
        print_error(f"Connection failed: {e}")
        raise SystemExit(1) from e
    finally:
        client.close()
