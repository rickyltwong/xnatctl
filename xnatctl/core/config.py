"""Configuration management for xnatctl.

Supports YAML profiles and environment variable overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from xnatctl.core.exceptions import ConfigurationError, ProfileNotFoundError

# =============================================================================
# Constants
# =============================================================================

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


# =============================================================================
# Profile
# =============================================================================


@dataclass
class Profile:
    """Configuration profile for an XNAT server."""

    url: str
    verify_ssl: bool = True
    timeout: int = 30
    default_project: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "url": self.url,
            "verify_ssl": self.verify_ssl,
            "timeout": self.timeout,
            "default_project": self.default_project,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        """Create from dictionary."""
        return cls(
            url=data.get("url", ""),
            verify_ssl=data.get("verify_ssl", True),
            timeout=data.get("timeout", 30),
            default_project=data.get("default_project"),
        )


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
    def load(cls, config_path: Optional[Path] = None) -> "Config":
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
                    config.profiles[name] = Profile.from_dict(pdata)
            except Exception as e:
                raise ConfigurationError(f"Failed to load config: {e}")

        # Environment variable overrides
        if url := os.getenv(ENV_URL):
            verify_ssl = os.getenv(ENV_VERIFY_SSL, "true").lower() in ("true", "1", "yes")
            timeout = int(os.getenv(ENV_TIMEOUT, "30"))

            config.profiles["default"] = Profile(
                url=url,
                verify_ssl=verify_ssl,
                timeout=timeout,
            )

        if profile := os.getenv(ENV_PROFILE):
            config.default_profile = profile

        return config

    def save(self, config_path: Optional[Path] = None) -> None:
        """Save config to file (excludes secrets).

        Args:
            config_path: Optional path to config file.
        """
        path = config_path or CONFIG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "default_profile": self.default_profile,
            "output_format": self.output_format,
            "profiles": {name: p.to_dict() for name, p in self.profiles.items()},
        }

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def get_profile(self, name: Optional[str] = None) -> Profile:
        """Get profile by name or default.

        Args:
            name: Profile name. If None, uses default_profile.

        Returns:
            Profile configuration.

        Raises:
            ProfileNotFoundError: If profile doesn't exist.
        """
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
        timeout: int = 30,
        default_project: Optional[str] = None,
    ) -> Profile:
        """Add or update a profile.

        Args:
            name: Profile name.
            url: XNAT server URL.
            verify_ssl: Whether to verify SSL certificates.
            timeout: Request timeout in seconds.
            default_project: Default project ID.

        Returns:
            Created profile.
        """
        profile = Profile(
            url=url,
            verify_ssl=verify_ssl,
            timeout=timeout,
            default_project=default_project,
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


def get_credentials(profile: Optional[Profile] = None) -> tuple[Optional[str], Optional[str]]:
    """Get credentials from environment variables.

    Args:
        profile: Optional profile (not used for credentials, kept for API consistency).

    Returns:
        Tuple of (username, password) from environment.
    """
    username = os.getenv(ENV_USER)
    password = os.getenv(ENV_PASS)
    return username, password


def get_token() -> Optional[str]:
    """Get session token from environment variable.

    Returns:
        Token if set, None otherwise.
    """
    return os.getenv(ENV_TOKEN)
