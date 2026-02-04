"""Tests for xnatctl.core.config module."""

from __future__ import annotations

from pathlib import Path

import pytest

from xnatctl.core.config import Config, Profile
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS

# =============================================================================
# Profile Tests
# =============================================================================


class TestProfile:
    """Tests for Profile dataclass."""

    def test_default_values(self):
        profile = Profile(url="https://xnat.example.org")
        assert profile.url == "https://xnat.example.org"
        assert profile.verify_ssl is True
        assert profile.timeout == DEFAULT_HTTP_TIMEOUT_SECONDS
        assert profile.default_project is None
        assert profile.username is None
        assert profile.password is None

    def test_custom_values(self):
        profile = Profile(
            url="https://xnat.example.org",
            verify_ssl=False,
            timeout=60,
            default_project="MYPROJ",
        )
        assert profile.verify_ssl is False
        assert profile.timeout == 60
        assert profile.default_project == "MYPROJ"

    def test_credentials_in_profile(self):
        """Test that username/password can be stored in profile."""
        profile = Profile(
            url="https://xnat.example.org",
            username="testuser",
            password="testpass",
        )
        assert profile.username == "testuser"
        assert profile.password == "testpass"

    def test_to_dict_excludes_none_credentials(self):
        """Test that to_dict excludes None username/password."""
        profile = Profile(url="https://xnat.example.org")
        data = profile.to_dict()
        assert "username" not in data
        assert "password" not in data

    def test_to_dict_includes_credentials_when_set(self):
        """Test that to_dict includes username/password when set."""
        profile = Profile(
            url="https://xnat.example.org",
            username="testuser",
            password="testpass",
        )
        data = profile.to_dict()
        assert data["username"] == "testuser"
        assert data["password"] == "testpass"

    def test_from_dict_with_credentials(self):
        """Test that from_dict loads username/password."""
        data = {
            "url": "https://xnat.example.org",
            "username": "testuser",
            "password": "testpass",
            "verify_ssl": True,
            "timeout": 30,
        }
        profile = Profile.from_dict(data)
        assert profile.username == "testuser"
        assert profile.password == "testpass"

    def test_from_dict_without_credentials(self):
        """Test that from_dict handles missing credentials."""
        data = {
            "url": "https://xnat.example.org",
            "verify_ssl": True,
            "timeout": 30,
        }
        profile = Profile.from_dict(data)
        assert profile.username is None
        assert profile.password is None


# =============================================================================
# Config Tests
# =============================================================================


class TestConfig:
    """Tests for Config class."""

    def test_default_config(self):
        config = Config()
        assert config.default_profile == "default"
        assert config.output_format == "table"
        assert config.profiles == {}

    def test_load_from_yaml(self, temp_dir: Path, sample_config_yaml: str):
        config_path = temp_dir / "config.yaml"
        config_path.write_text(sample_config_yaml)

        config = Config.load(config_path)

        assert config.default_profile == "test"
        assert config.output_format == "table"
        assert "test" in config.profiles
        assert "production" in config.profiles

        test_profile = config.profiles["test"]
        assert test_profile.url == "https://xnat-test.example.org"
        assert test_profile.verify_ssl is False
        assert test_profile.timeout == 30
        assert test_profile.default_project == "TESTPROJ"

    def test_load_nonexistent_returns_default(self, temp_dir: Path):
        config = Config.load(temp_dir / "nonexistent.yaml")
        assert config.default_profile == "default"
        assert config.profiles == {}

    def test_load_with_credentials(self, temp_dir: Path, sample_config_with_credentials_yaml: str):
        """Test that credentials are loaded from config file."""
        config_path = temp_dir / "config.yaml"
        config_path.write_text(sample_config_with_credentials_yaml)

        config = Config.load(config_path)

        test_profile = config.profiles["test"]
        assert test_profile.username == "testuser"
        assert test_profile.password == "testpass"

        prod_profile = config.profiles["production"]
        assert prod_profile.username == "produser"
        assert prod_profile.password == "prodpass"

    def test_load_empty_file_returns_default(self, temp_dir: Path):
        config_path = temp_dir / "config.yaml"
        config_path.write_text("")

        config = Config.load(config_path)
        assert config.default_profile == "default"

    def test_save_config(self, temp_dir: Path):
        config = Config(
            default_profile="myprofile",
            output_format="json",
            profiles={
                "myprofile": Profile(
                    url="https://xnat.example.org",
                    verify_ssl=True,
                    timeout=45,
                )
            },
        )

        config_path = temp_dir / "saved_config.yaml"
        config.save(config_path)

        assert config_path.exists()

        # Reload and verify
        loaded = Config.load(config_path)
        assert loaded.default_profile == "myprofile"
        assert loaded.output_format == "json"
        assert "myprofile" in loaded.profiles
        assert loaded.profiles["myprofile"].timeout == 45

    def test_get_profile(self):
        config = Config(
            default_profile="test",
            profiles={
                "test": Profile(url="https://test.example.org"),
                "prod": Profile(url="https://prod.example.org"),
            },
        )

        # Get by name
        profile = config.get_profile("prod")
        assert profile.url == "https://prod.example.org"

        # Get default
        profile = config.get_profile()
        assert profile.url == "https://test.example.org"

    def test_get_profile_not_found(self):
        from xnatctl.core.exceptions import ProfileNotFoundError

        config = Config(profiles={})

        with pytest.raises(ProfileNotFoundError):
            config.get_profile("nonexistent")

    def test_env_var_override(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
        # Set environment variable
        monkeypatch.setenv("XNAT_URL", "https://env-override.example.org")
        monkeypatch.setenv("XNAT_VERIFY_SSL", "false")

        config = Config.load(temp_dir / "nonexistent.yaml")

        assert "default" in config.profiles
        default_profile = config.profiles["default"]
        assert default_profile.url == "https://env-override.example.org"
        assert default_profile.verify_ssl is False

    def test_save_creates_parent_dirs(self, temp_dir: Path):
        config = Config(profiles={"test": Profile(url="https://test.example.org")})

        nested_path = temp_dir / "subdir" / "nested" / "config.yaml"
        config.save(nested_path)

        assert nested_path.exists()
        assert nested_path.parent.exists()


# =============================================================================
# Integration Tests
# =============================================================================


class TestConfigIntegration:
    """Integration tests for config loading scenarios."""

    def test_roundtrip_save_load(self, temp_dir: Path):
        """Test that saving and loading preserves all data."""
        original = Config(
            default_profile="production",
            output_format="json",
            profiles={
                "development": Profile(
                    url="https://dev.example.org",
                    verify_ssl=False,
                    timeout=60,
                    default_project="DEV_PROJECT",
                ),
                "production": Profile(
                    url="https://prod.example.org",
                    verify_ssl=True,
                    timeout=30,
                    default_project=None,
                ),
            },
        )

        config_path = temp_dir / "config.yaml"
        original.save(config_path)
        loaded = Config.load(config_path)

        assert loaded.default_profile == original.default_profile
        assert loaded.output_format == original.output_format
        assert set(loaded.profiles.keys()) == set(original.profiles.keys())

        for name in original.profiles:
            orig_profile = original.profiles[name]
            loaded_profile = loaded.profiles[name]
            assert loaded_profile.url == orig_profile.url
            assert loaded_profile.verify_ssl == orig_profile.verify_ssl
            assert loaded_profile.timeout == orig_profile.timeout
            assert loaded_profile.default_project == orig_profile.default_project

    def test_roundtrip_save_load_with_credentials(self, temp_dir: Path):
        """Test that saving and loading preserves credentials."""
        original = Config(
            default_profile="production",
            output_format="json",
            profiles={
                "development": Profile(
                    url="https://dev.example.org",
                    username="devuser",
                    password="devpass",
                    verify_ssl=False,
                    timeout=60,
                ),
                "production": Profile(
                    url="https://prod.example.org",
                    username="produser",
                    password="prodpass",
                    verify_ssl=True,
                    timeout=30,
                ),
            },
        )

        config_path = temp_dir / "config.yaml"
        original.save(config_path)
        loaded = Config.load(config_path)

        for name in original.profiles:
            orig_profile = original.profiles[name]
            loaded_profile = loaded.profiles[name]
            assert loaded_profile.username == orig_profile.username
            assert loaded_profile.password == orig_profile.password
