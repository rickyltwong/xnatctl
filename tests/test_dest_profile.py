"""Tests for dest-profile CLI helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xnatctl.cli.common import Context, create_dest_client
from xnatctl.core.exceptions import ConfigurationError


class TestCreateDestClient:
    def test_from_dest_profile(self) -> None:
        ctx = Context()
        with patch("xnatctl.cli.common.Config") as MockConfig:
            mock_config = MagicMock()
            mock_profile = MagicMock()
            mock_profile.url = "https://dst.example.org"
            mock_profile.username = "user"
            mock_profile.password = "pass"
            mock_profile.timeout = 30
            mock_profile.verify_ssl = True
            mock_config.get_profile.return_value = mock_profile
            MockConfig.load.return_value = mock_config
            ctx.config = mock_config

            client = create_dest_client(
                ctx,
                dest_profile="staging",
            )

            assert client.base_url.rstrip("/") == "https://dst.example.org"

    def test_from_inline_creds(self) -> None:
        ctx = Context()
        client = create_dest_client(
            ctx,
            dest_url="https://dst.example.org",
            dest_user="admin",
            dest_pass="secret",
        )
        assert client.base_url.rstrip("/") == "https://dst.example.org"
        assert client.username == "admin"

    def test_raises_without_dest(self) -> None:
        ctx = Context()
        with pytest.raises(ConfigurationError):
            create_dest_client(ctx)
