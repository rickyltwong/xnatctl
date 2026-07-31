"""Configuration management for xnatctl.

Supports YAML profiles and environment variable overrides.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from xnatctl.core.exceptions import (
    ConfigurationError,
    NoConfigurationError,
    ProfileNotFoundError,
)
from xnatctl.core.fsutil import atomic_private_write, ensure_private_dir, restrict_permissions
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS

# =============================================================================
# Constants
# =============================================================================

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "xnatctl"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
SESSION_CACHE_FILE = CONFIG_DIR / ".session"

# Environment variable names
ENV_URL = "XNAT_URL"
ENV_USER = "XNAT_USER"
ENV_PASS = "XNAT_PASS"
ENV_TOKEN = "XNAT_TOKEN"
ENV_PROFILE = "XNAT_PROFILE"
ENV_VERIFY_SSL = "XNAT_VERIFY_SSL"
ENV_TIMEOUT = "XNAT_TIMEOUT"

_TRUE_VALUES = frozenset({"true", "1", "yes"})
_FALSE_VALUES = frozenset({"false", "0", "no"})


def _parse_bool_env(name: str, default: bool) -> bool:
    """Parse a boolean environment variable strictly.

    Accepts ``true/false/1/0/yes/no`` case-insensitively (after stripping
    whitespace). Anything else raises ``ConfigurationError`` rather than
    silently evaluating to ``False`` -- a typo like ``XNAT_VERIFY_SSL=on`` must
    never quietly disable TLS verification for a PHI-bearing connection.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigurationError(f"Invalid value for {name}: {raw!r}. Use true/false.")


# =============================================================================
# Profile
# =============================================================================


_OPERATIONAL_FIELDS = ("workers", "overwrite", "direct_archive", "archive_mode", "extract")

# Keychain integration. `keyring` is an optional extra: install with
# `pip install 'xnatctl[keyring]'`.
KEYRING_SERVICE = "xnatctl"
PASSWORD_SOURCE_KEYRING = "keyring"


def keyring_key(profile_name: str, url: str) -> str:
    """Return the keychain entry name for a profile.

    Keyed on the profile name rather than the username, which may be supplied
    by the environment and therefore absent from the profile entirely.
    """
    return f"{profile_name}@{url}"


def load_keyring() -> Any:
    """Import the optional ``keyring`` backend or raise an actionable error.

    Imported lazily inside the function, per this repo's convention: keyring is
    an optional dependency, and importing it eagerly would make every xnatctl
    invocation pay for a backend probe it usually does not need.
    """
    try:
        import keyring
    except ImportError as e:
        raise ConfigurationError(
            "Keychain support requires the 'keyring' package, which is not "
            "installed. Install it with: pip install 'xnatctl[keyring]'"
        ) from e
    return keyring


@dataclass
class Profile:
    """Configuration profile for an XNAT server."""

    url: str
    verify_ssl: bool = True
    ca_bundle: str | None = None
    timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS
    default_project: str | None = None
    username: str | None = None
    password: str | None = None
    # Where the password comes from: None means the inline `password` field,
    # "keyring" means the OS keychain. Optional with a default so existing
    # config files keep loading unchanged (a schema version field can come later).
    password_source: str | None = None
    # Operational defaults (None = not configured, use command default)
    workers: int | None = None
    overwrite: str | None = None
    direct_archive: bool | None = None
    archive_mode: str | None = None
    extract: bool | None = None
    # Set by Config when a profile is loaded or created. Needed to build the
    # keyring key; deliberately not serialized, because the name is already the
    # YAML mapping key.
    name: str | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result: dict[str, Any] = {
            "url": self.url,
            "verify_ssl": self.verify_ssl,
            "timeout": self.timeout,
            "default_project": self.default_project,
        }
        if self.ca_bundle:
            result["ca_bundle"] = self.ca_bundle
        if self.username:
            result["username"] = self.username
        if self.password_source:
            # A keychain-backed profile never writes the secret back out --
            # that is the whole point of moving it.
            result["password_source"] = self.password_source
        elif self.password:
            result["password"] = self.password
        for field_name in _OPERATIONAL_FIELDS:
            val = getattr(self, field_name)
            if val is not None:
                result[field_name] = val
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, name: str | None = None) -> Profile:
        """Create from dictionary.

        Args:
            data: Serialized profile mapping.
            name: The profile's name in the config, used to build its keyring
                key. Optional so existing callers keep working.
        """
        return cls(
            url=data.get("url", ""),
            verify_ssl=data.get("verify_ssl", True),
            ca_bundle=data.get("ca_bundle"),
            timeout=data.get("timeout", DEFAULT_HTTP_TIMEOUT_SECONDS),
            default_project=data.get("default_project"),
            username=data.get("username"),
            password=data.get("password"),
            password_source=data.get("password_source"),
            workers=data.get("workers"),
            overwrite=data.get("overwrite"),
            direct_archive=data.get("direct_archive"),
            archive_mode=data.get("archive_mode"),
            extract=data.get("extract"),
            name=name,
        )

    def resolve_password(self) -> str | None:
        """Return this profile's password, reading the keychain if configured.

        Raises:
            ConfigurationError: If the keychain is the configured source but
                the optional dependency is missing, the profile has no name to
                key on, or no entry exists.
        """
        if self.password_source != PASSWORD_SOURCE_KEYRING:
            return self.password

        if not self.name:
            raise ConfigurationError(
                "Cannot read a keychain password for a profile with no name. "
                "Load it through Config so the profile knows its own name."
            )

        keyring = load_keyring()
        password = keyring.get_password(KEYRING_SERVICE, keyring_key(self.name, self.url))
        if password is None:
            raise ConfigurationError(
                f"Profile '{self.name}' expects its password in the OS keychain, "
                f"but no entry was found. Store one with: "
                f"xnatctl config set-password {self.name}"
            )
        return str(password)


# =============================================================================
# Config
# =============================================================================


@dataclass
class Config:
    """Application configuration."""

    default_profile: str = "default"
    output_format: str = "table"
    profiles: dict[str, Profile] = field(default_factory=dict)

    @classmethod
    def load(cls, config_path: Path | None = None) -> Config:
        """Load config from file with environment variable overrides.

        Priority (highest to lowest):
        1. Environment variables
        2. Config file
        3. Defaults

        Args:
            config_path: Optional path to config file.

        Returns:
            Loaded configuration.
        """
        path = config_path or CONFIG_FILE
        config = cls()

        # Load from file if exists
        if path.exists():
            try:
                with open(path) as f:
                    data = yaml.safe_load(f) or {}

                config.default_profile = data.get("default_profile", "default")
                config.output_format = data.get("output_format", "table")

                for name, pdata in data.get("profiles", {}).items():
                    config.profiles[name] = Profile.from_dict(pdata, name=name)
            except Exception as e:
                raise ConfigurationError(f"Failed to load config: {e}") from e

            _warn_on_exposed_password(config, path)

        # Environment variable overrides
        if url := os.getenv(ENV_URL):
            verify_ssl = _parse_bool_env(ENV_VERIFY_SSL, True)
            timeout = int(os.getenv(ENV_TIMEOUT, str(DEFAULT_HTTP_TIMEOUT_SECONDS)))

            config.profiles["default"] = Profile(
                url=url,
                verify_ssl=verify_ssl,
                timeout=timeout,
                name="default",
            )

        if profile := os.getenv(ENV_PROFILE):
            config.default_profile = profile

        return config

    def save(self, config_path: Path | None = None) -> None:
        """Save config to file with 0600 permissions.

        Does NOT exclude secrets: a profile's inline ``password`` is written
        out verbatim. Prefer the OS keychain (``xnatctl config set-password``)
        or the XNAT_PASS environment variable. Profiles whose
        ``password_source`` is ``keyring`` never write a password here.

        Args:
            config_path: Optional path to config file.
        """
        path = config_path or CONFIG_FILE
        ensure_private_dir(path.parent)

        data = {
            "default_profile": self.default_profile,
            "output_format": self.output_format,
            "profiles": {name: p.to_dict() for name, p in self.profiles.items()},
        }

        # config.yaml can carry a plaintext profile password today, so it gets
        # the same 0600 atomic treatment as the session cache. Whether
        # it should hold a password at all, prefer the keychain.
        with atomic_private_write(path) as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        restrict_permissions(path)

    def get_profile(self, name: str | None = None) -> Profile:
        """Get profile by name or default.

        Args:
            name: Profile name. If None, uses default_profile.

        Returns:
            Profile configuration.

        Raises:
            ProfileNotFoundError: If profile doesn't exist.
        """
        if not self.profiles:
            # First run: nothing is configured, so "profile 'default' not
            # found" would send the user hunting for a typo.
            raise NoConfigurationError(
                f"No profiles configured. Expected a config file at {CONFIG_FILE}."
            )

        name = name or self.default_profile
        if name not in self.profiles:
            raise ProfileNotFoundError(name)
        return self.profiles[name]

    def has_profile(self, name: str) -> bool:
        """Check if profile exists."""
        return name in self.profiles

    def add_profile(
        self,
        name: str,
        url: str,
        verify_ssl: bool = True,
        timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
        default_project: str | None = None,
        ca_bundle: str | None = None,
    ) -> Profile:
        """Add or update a profile.

        Args:
            name: Profile name.
            url: XNAT server URL.
            verify_ssl: Whether to verify SSL certificates.
            timeout: Request timeout in seconds.
            default_project: Default project ID.
            ca_bundle: Path to a custom CA bundle for TLS verification.

        Returns:
            Created profile.
        """
        profile = Profile(
            url=url,
            verify_ssl=verify_ssl,
            ca_bundle=ca_bundle,
            timeout=timeout,
            default_project=default_project,
            name=name,
        )
        self.profiles[name] = profile
        return profile

    def remove_profile(self, name: str) -> bool:
        """Remove a profile.

        Args:
            name: Profile name.

        Returns:
            True if removed, False if didn't exist.
        """
        if name in self.profiles:
            del self.profiles[name]
            return True
        return False

    def set_default_profile(self, name: str) -> None:
        """Set the default profile.

        Args:
            name: Profile name to set as default.

        Raises:
            ProfileNotFoundError: If profile doesn't exist.
        """
        if name not in self.profiles:
            raise ProfileNotFoundError(name)
        self.default_profile = name


def _warn_on_exposed_password(config: Config, path: Path) -> None:
    """Warn when a plaintext password sits in a file others can read.

    New writes are 0600, but a config.yaml created before that, or
    copied in from elsewhere, keeps whatever mode it had. Warn rather than
    chmod: silently changing permissions on a file the user did not ask us to
    touch is its own surprise, and the actionable fix is to move the password
    into the keychain.
    """
    exposed = [name for name, profile in config.profiles.items() if profile.password]
    if not exposed:
        return

    try:
        mode = path.stat().st_mode
    except OSError:
        return

    if not mode & 0o077:
        return

    logger.warning(
        "%s is readable by other users (mode %o) and stores a plaintext "
        "password for profile(s): %s. Fix with: chmod 600 %s, and consider "
        "moving the password into the OS keychain: xnatctl config set-password %s",
        path,
        mode & 0o777,
        ", ".join(sorted(exposed)),
        path,
        sorted(exposed)[0],
    )


def get_credentials(profile: Profile | None = None) -> tuple[str | None, str | None]:
    """Get credentials with priority: env vars > profile config.

    Args:
        profile: Optional profile to read credentials from.

    Returns:
        Tuple of (username, password).
    """
    username = os.getenv(ENV_USER)
    password = os.getenv(ENV_PASS)

    if profile:
        if not username and profile.username:
            username = profile.username
        # Only consult the profile (and therefore, possibly, the OS keychain)
        # when the environment did not already supply a password.
        if not password:
            password = profile.resolve_password()

    return username, password


def get_token() -> str | None:
    """Get session token from environment variable.

    Returns:
        Token if set, None otherwise.
    """
    return os.getenv(ENV_TOKEN)
