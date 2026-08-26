"""Configuration management for xnatctl.

Supports YAML profiles and environment variable overrides.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from xnatctl.core.exceptions import (
    ConfigurationError,
    NoConfigurationError,
    ProfileNotFoundError,
)
from xnatctl.core.fsutil import (
    POSIX_PERMISSIONS,
    atomic_private_write,
    ensure_private_dir,
    restrict_permissions,
)
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS

# =============================================================================
# Constants
# =============================================================================

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "xnatctl"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
SESSION_CACHE_FILE = CONFIG_DIR / ".session"

# Config file schema version. Bump whenever the on-disk shape of config.yaml
# changes in a way that needs a migration, and register the migration below.
CURRENT_CONFIG_VERSION = 1

# Migrations are keyed by the version they migrate *from*, applied in order
# in memory only (see Config.load). Empty until the first breaking change to
# the file shape actually ships -- there is nothing to migrate yet.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}

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

# Keychain integration.
KEYRING_SERVICE = "xnatctl"
PASSWORD_SOURCE_KEYRING = "keyring"


def keyring_key(profile_name: str, url: str) -> str:
    """Return the keychain entry name for a profile.

    Keyed on the profile name rather than the username, which may be supplied
    by the environment and therefore absent from the profile entirely.
    """
    return f"{profile_name}@{url}"


def load_keyring() -> Any:
    """Import the ``keyring`` backend.

    Imported lazily inside the function: importing keyring eagerly would make
    every xnatctl invocation pay for a backend probe it usually does not need.
    """
    import keyring

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
                the profile has no name to key on, or no entry exists.
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
# On-disk schema (validation only)
# =============================================================================
#
# These pydantic models exist to validate a loaded config.yaml and detect
# unrecognized keys -- they are not the runtime representation. Config/Profile
# above remain that; parsed models are converted into them via
# Profile.from_dict so every other call site is unaffected. `extra="allow"`
# is what lets a field unknown to this version of xnatctl surface in
# `model_extra` for the unknown-key warning, instead of being rejected.


class ProfileModel(BaseModel):
    """Validated shape of one entry under ``profiles:`` in config.yaml."""

    model_config = ConfigDict(extra="allow")

    url: str = ""
    verify_ssl: bool = True
    ca_bundle: str | None = None
    timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS
    default_project: str | None = None
    username: str | None = None
    password: str | None = None
    password_source: str | None = None
    workers: int | None = None
    overwrite: str | None = None
    direct_archive: bool | None = None
    archive_mode: str | None = None
    extract: bool | None = None


class ConfigFileModel(BaseModel):
    """Validated shape of the whole config.yaml document."""

    model_config = ConfigDict(extra="allow")

    version: int = 1
    default_profile: str = "default"
    output_format: str = "table"
    log_file: str | None = None
    update_check: bool = True
    profiles: dict[str, ProfileModel] = Field(default_factory=dict)


def _coerce_version(value: Any) -> int | None:
    """Best-effort int coercion for a version field.

    A quoted integer (``version: "2"``) must mean the same thing as an
    unquoted one -- YAML's own type inference should not change the
    document's meaning. Anything else (a float, a bool, a list, non-numeric
    text) is not a version this code understands.
    """
    if isinstance(value, bool):  # bool is an int subclass; never a real version
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def _normalize_version(data: dict[str, Any], path: Path) -> int:
    """Resolve ``data["version"]`` to a single int, in place.

    Every later step (migration dispatch, the newer-than-known-version
    warning, and Pydantic validation) reads this same normalized value off
    ``data``, so a version that is missing, quoted, or outright invalid can
    only ever be interpreted one way -- a mismatch here would let an
    invalid value (e.g. ``version: banana``) sail past a local sanity check
    only to reach Pydantic's strict ``int`` field and crash the whole load.
    A missing key predates the field and is version 1; anything that will
    not coerce is corrupt, treated as 1 (never a reason to brick every
    command) with a warning naming the bad value.
    """
    raw_version = data.get("version", 1)
    version = _coerce_version(raw_version)
    if version is None:
        logger.warning(
            "%s has an invalid config version %r; treating it as version 1.",
            path,
            raw_version,
        )
        version = 1
    data["version"] = version
    return version


def _sanitize_keys(data: dict[Any, Any]) -> dict[str, Any]:
    """Stringify every key at the top level and inside each profile mapping.

    PyYAML's default (YAML 1.1) loader turns certain unquoted scalars into
    non-string keys -- ``yes:``/``no:``/``on:``/``off:`` become ``bool``, a
    bare ``123:`` becomes ``int``. Pydantic's ``extra="allow"`` requires
    string keys and raises on anything else; the old hand-rolled loader
    tolerated this silently (it only ever did ``dict.get("known_name")``), so
    a config carrying one of these keys must keep loading, not hard-error,
    now that Pydantic sits in the path.
    """
    sanitized: dict[str, Any] = {str(key): value for key, value in data.items()}
    profiles = sanitized.get("profiles")
    if isinstance(profiles, dict):
        sanitized["profiles"] = {
            str(pname): (
                {str(k): v for k, v in pdata.items()} if isinstance(pdata, dict) else pdata
            )
            for pname, pdata in profiles.items()
        }
    return sanitized


def _migrate(data: dict[str, Any], version: int) -> tuple[dict[str, Any], int]:
    """Apply registered migrations in order, purely in memory.

    Never called from a save path -- the caller decides whether the migrated
    form gets written back.

    Raises:
        ConfigurationError: If a migration step between ``version`` and
            ``CURRENT_CONFIG_VERSION`` is missing. Stopping silently partway
            would validate a half-migrated document against the current
            schema's defaults and then, on the next save, stamp it as fully
            current -- data loss dressed up as success.
    """
    while version < CURRENT_CONFIG_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise ConfigurationError(
                f"No migration registered to take config version {version} to "
                f"{version + 1} (target: {CURRENT_CONFIG_VERSION}). Refusing to "
                "load this file with a gap in its migration path."
            )
        data = migration(data)
        version += 1
    data["version"] = version
    return data, version


def _warn_on_unknown_keys(model: ConfigFileModel, path: Path) -> None:
    """Warn once, naming every unrecognized top-level and profile key.

    Never raises: an unknown key is forward/backward-compat noise, not a
    reason to fail a load.
    """
    sections: dict[str, list[str]] = {}
    if top_extra := sorted(model.model_extra or {}):
        sections["top-level"] = top_extra
    for name, profile in model.profiles.items():
        if profile_extra := sorted(profile.model_extra or {}):
            sections[f"profile '{name}'"] = profile_extra

    if not sections:
        return

    detail = "; ".join(f"{where}: {', '.join(keys)}" for where, keys in sections.items())
    logger.warning("%s has unrecognized config keys, ignoring: %s", path, detail)


# =============================================================================
# Config
# =============================================================================


@dataclass
class Config:
    """Application configuration."""

    default_profile: str = "default"
    output_format: str = "table"
    #: Explicit-path-only diagnostics log (see ``--log-file``/``XNATCTL_LOG_FILE``
    #: in ``cli/common.py``). ``None`` means the feature is off; there is no
    #: default path -- setting this key requires a real value, same as the
    #: flag/env forms. Additive under config version 1: an older file simply
    #: lacks the key and gets this default, and writes it back unchanged.
    log_file: str | None = None
    #: Opt-out for the update-availability notice and its background cache
    #: refresh (see ``core/update_check.py``). Additive under config version
    #: 1, same as ``log_file``: an older file simply lacks the key and gets
    #: this (enabled) default.
    update_check: bool = True
    profiles: dict[str, Profile] = field(default_factory=dict)
    #: The version this config was loaded from (or, for a config built in
    #: memory, the current version). Drives the fail-closed check in
    #: :meth:`save` -- see there for why a newer-than-known version is not
    #: just downgraded silently.
    version: int = CURRENT_CONFIG_VERSION

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
                    raw = yaml.safe_load(f) or {}
                if not isinstance(raw, dict):
                    raise ConfigurationError("config.yaml must contain a YAML mapping")

                raw = _sanitize_keys(raw)
                # Normalizes data["version"] to a clean int in place (missing
                # predates the field -> 1; invalid -> 1 with a warning), so
                # every step below -- migration dispatch, the newer-version
                # warning, and Pydantic's `version: int` field -- agrees on
                # the exact same value.
                file_version = _normalize_version(raw, path)

                data, applied_version = _migrate(raw, file_version)
                if applied_version > CURRENT_CONFIG_VERSION:
                    logger.warning(
                        "%s declares config version %d, newer than this xnatctl "
                        "understands (%d). Loading best-effort.",
                        path,
                        applied_version,
                        CURRENT_CONFIG_VERSION,
                    )

                model = ConfigFileModel.model_validate(data)
                _warn_on_unknown_keys(model, path)

                config.default_profile = model.default_profile
                config.output_format = model.output_format
                config.log_file = model.log_file
                config.update_check = model.update_check
                config.version = applied_version

                for name, pmodel in model.profiles.items():
                    config.profiles[name] = Profile.from_dict(pmodel.model_dump(), name=name)
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

    def ensure_saveable(self) -> None:
        """Raise if this config must not be written back to disk.

        Called by :meth:`save` itself, and by any command that has an
        external side effect (writing to the OS keychain, for one) BEFORE
        that side effect runs -- calling it only inside ``save()`` would let
        such a side effect complete and then fail on the write that was
        supposed to record it, leaving the two out of sync.

        Raises:
            ConfigurationError: If this config was loaded from a file
                declaring a version newer than ``CURRENT_CONFIG_VERSION``.
                Fields this build does not know about were never captured
                (only the fields this schema declares survive the
                dataclass round-trip), so saving would silently drop them
                and mislabel the file as the older, current version --
                refusing is the honest failure mode, not a downgrade.
        """
        if self.version > CURRENT_CONFIG_VERSION:
            raise ConfigurationError(
                f"Refusing to save: this config was loaded from a file "
                f"declaring version {self.version}, newer than this xnatctl "
                f"understands ({CURRENT_CONFIG_VERSION}). Saving now would "
                f"silently drop unrecognized fields and relabel the file as "
                f"version {CURRENT_CONFIG_VERSION}. Upgrade xnatctl before "
                "making changes, or edit config.yaml directly."
            )

    def save(self, config_path: Path | None = None) -> None:
        """Save config to file with 0600 permissions.

        Does NOT exclude secrets: a profile's inline ``password`` is written
        out verbatim. Prefer the OS keychain (``xnatctl config set-password``)
        or the XNAT_PASS environment variable. Profiles whose
        ``password_source`` is ``keyring`` never write a password here.

        Args:
            config_path: Optional path to config file.

        Raises:
            ConfigurationError: See :meth:`ensure_saveable`.
        """
        self.ensure_saveable()
        path = config_path or CONFIG_FILE
        ensure_private_dir(path.parent)

        data: dict[str, Any] = {
            "version": CURRENT_CONFIG_VERSION,
            "default_profile": self.default_profile,
            "output_format": self.output_format,
            "profiles": {name: p.to_dict() for name, p in self.profiles.items()},
        }
        if self.log_file:
            data["log_file"] = self.log_file
        if not self.update_check:
            data["update_check"] = False

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

    # Windows reports 0o666 for every writable file, so the check below would
    # fire on every command and tell the user to run a chmod that neither
    # exists there nor would help.
    if not POSIX_PERMISSIONS:
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
