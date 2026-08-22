"""One-call client construction from a config profile.

The credential-resolution stack the CLI runs before every command used to live
only inside ``Context.get_client``. :func:`resolve_client_params` is that same
resolution, extracted so a library user gets an authenticated-ready client from
a profile name alone -- :func:`build_client_from_profile` (and
``XNATClient.from_profile``) wrap it, and the CLI context reuses it to build the
client every command shares. The rendering side of ``get_client`` (the
disabled-TLS warning) stays in the CLI: this module resolves parameters and must
not import the Rich output helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xnatctl.core.auth import AuthManager
from xnatctl.core.config import Config, get_credentials

if TYPE_CHECKING:
    from pathlib import Path

    from xnatctl.core.client import XNATClient


def resolve_client_params(
    profile_name: str | None = None,
    *,
    config: Config | None = None,
    config_path: Path | None = None,
    auth_manager: AuthManager | None = None,
) -> dict[str, Any]:
    """Resolve the keyword arguments for an XNATClient from a config profile.

    Reproduces the CLI's credential resolution exactly: environment variables
    win over profile config for username/password, an ``XNAT_TOKEN`` env var or
    a cached session token authenticates without a password prompt, and the
    cached session's username backfills the current-user hint when the profile
    supplies none.

    ``auto_reauth`` is always ``True`` in the result. That is part of the
    resolution semantics rather than a convenience: a session that expires
    mid-operation self-heals with one silent re-login when a password is
    available, while a token-only client (no password) still surfaces
    ``SessionExpiredError`` as before.

    Args:
        profile_name: Profile to load. ``None`` uses the config default.
        config: Pre-loaded config. When omitted, ``Config.load(config_path)``
            is called.
        config_path: Config file path, used only when ``config`` is omitted.
        auth_manager: Session-cache manager. Defaults to a fresh
            :class:`AuthManager` over the standard cache location.

    Returns:
        A kwargs dict ready to splat into ``XNATClient(**params)``.

    Raises:
        ProfileNotFoundError: If the named profile does not exist. Propagated
            as-is; it carries its own next-step hint.
    """
    if config is None:
        config = Config.load(config_path)
    if auth_manager is None:
        auth_manager = AuthManager()

    # ProfileNotFoundError carries its own next-step hint, so it propagates
    # rather than being restated as a ConfigurationError.
    profile = config.get_profile(profile_name)

    # Credentials: env vars > profile config. A cached session keeps its
    # username as a hint for current-user lookups on servers where /data/user
    # returns a user listing.
    username, password = get_credentials(profile)
    session = auth_manager.load_session(profile.url)
    token = auth_manager.get_token_from_env() or (session.token if session else None)
    username_hint = username or (session.username if session else None)

    return {
        "base_url": profile.url,
        "username": username_hint,
        "password": password,
        "session_token": token,
        "timeout": profile.timeout,
        "verify_ssl": profile.verify_ssl,
        "ca_bundle": profile.ca_bundle,
        "auto_reauth": True,
    }


def build_client_from_profile(
    profile_name: str | None = None,
    *,
    config: Config | None = None,
    config_path: Path | None = None,
    auth_manager: AuthManager | None = None,
) -> XNATClient:
    """Build an XNATClient from a config profile, resolving credentials.

    The one-call library entry point. See :func:`resolve_client_params` for the
    resolution rules and the ``auto_reauth`` semantics. The returned client is
    not eagerly authenticated -- call ``authenticate()`` (or use it as a context
    manager) when a password-based login is wanted.

    Args:
        profile_name: Profile to load. ``None`` uses the config default.
        config: Pre-loaded config. When omitted, ``Config.load(config_path)``
            is called.
        config_path: Config file path, used only when ``config`` is omitted.
        auth_manager: Session-cache manager. Defaults to a fresh
            :class:`AuthManager`.

    Returns:
        A ready-to-use XNATClient.

    Raises:
        ProfileNotFoundError: If the named profile does not exist.
    """
    # Imported here rather than at module scope: core.client imports this module
    # via its from_profile classmethod, and a top-level import back into
    # core.client would close that cycle.
    from xnatctl.core.client import XNATClient

    return XNATClient(
        **resolve_client_params(
            profile_name,
            config=config,
            config_path=config_path,
            auth_manager=auth_manager,
        )
    )
