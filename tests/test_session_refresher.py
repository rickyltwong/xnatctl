"""Tests for _SessionRefresher and 401-retry in gradual-DICOM uploads."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from xnatctl.services.uploads import _SessionRefresher


class TestSessionRefresher:
    """Tests for _SessionRefresher token management."""

    def test_returns_initial_token(self) -> None:
        refresher = _SessionRefresher(
            base_url="https://xnat.example.org",
            verify_ssl=True,
            token="initial-token",
            username="user",
            password="pass",
        )
        assert refresher.token == "initial-token"

    def test_refresh_acquires_new_token(self) -> None:
        refresher = _SessionRefresher(
            base_url="https://xnat.example.org",
            verify_ssl=True,
            token="stale-token",
            username="user",
            password="pass",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "  fresh-token  "

        with patch("xnatctl.services.uploads.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = refresher.refresh("stale-token")

        assert result == "fresh-token"
        assert refresher.token == "fresh-token"

    def test_refresh_skips_when_already_refreshed(self) -> None:
        """If another thread already refreshed, return the new token without hitting the server."""
        refresher = _SessionRefresher(
            base_url="https://xnat.example.org",
            verify_ssl=True,
            token="already-fresh",
            username="user",
            password="pass",
        )

        with patch("xnatctl.services.uploads.httpx.Client") as mock_client_cls:
            result = refresher.refresh("stale-token")
            mock_client_cls.assert_not_called()

        assert result == "already-fresh"

    def test_refresh_without_credentials_returns_stale_token(self) -> None:
        refresher = _SessionRefresher(
            base_url="https://xnat.example.org",
            verify_ssl=True,
            token="stale-token",
            username=None,
            password=None,
        )
        result = refresher.refresh("stale-token")
        assert result == "stale-token"

    def test_refresh_on_auth_failure_keeps_old_token(self) -> None:
        refresher = _SessionRefresher(
            base_url="https://xnat.example.org",
            verify_ssl=True,
            token="stale-token",
            username="user",
            password="pass",
        )

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch("xnatctl.services.uploads.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = refresher.refresh("stale-token")

        assert result == "stale-token"

    def test_concurrent_refresh_only_authenticates_once(self) -> None:
        """Multiple threads hitting 401 simultaneously should result in a single reauth."""
        refresher = _SessionRefresher(
            base_url="https://xnat.example.org",
            verify_ssl=True,
            token="stale-token",
            username="user",
            password="pass",
        )

        auth_call_count = 0
        barrier = threading.Barrier(4)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "fresh-token"

        def counting_post(*args: object, **kwargs: object) -> MagicMock:
            nonlocal auth_call_count
            auth_call_count += 1
            return mock_response

        def thread_fn() -> str | None:
            barrier.wait()
            return refresher.refresh("stale-token")

        with patch("xnatctl.services.uploads.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = counting_post
            mock_client_cls.return_value = mock_client

            threads = [threading.Thread(target=thread_fn) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        assert auth_call_count == 1
        assert refresher.token == "fresh-token"


class TestGradualUpload401Retry:
    """Test that _upload_single_file_gradual retries on 401."""

    def test_retries_on_401_with_refreshed_token(self, tmp_path: Path) -> None:
        from xnatctl.services.uploads import _upload_single_file_gradual

        dcm = tmp_path / "test.dcm"
        dcm.write_bytes(b"\x00" * 128)

        refresher = _SessionRefresher(
            base_url="https://xnat.example.org",
            verify_ssl=True,
            token="stale-token",
            username="user",
            password="pass",
        )

        resp_401 = MagicMock(spec=httpx.Response)
        resp_401.status_code = 401
        resp_401.text = "Unauthorized"

        resp_200 = MagicMock(spec=httpx.Response)
        resp_200.status_code = 200
        resp_200.text = "OK"

        call_tokens: list[str | None] = []

        def fake_retry(fn: object, **kwargs: object) -> MagicMock:
            """Intercept upload_with_retry to track which token was used."""
            # The fn closure captures cookies; call it to get the response
            resp = fn()  # type: ignore[operator]
            return resp

        mock_client = MagicMock()
        call_count = 0

        def mock_post(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            cookies = kwargs.get("cookies", {})
            call_tokens.append(cookies.get("JSESSIONID"))
            call_count += 1
            if call_count == 1:
                return resp_401
            return resp_200

        mock_client.post.side_effect = mock_post

        auth_response = MagicMock()
        auth_response.status_code = 200
        auth_response.text = "fresh-token"

        with (
            patch(
                "xnatctl.services.uploads._get_gradual_http_client",
                return_value=mock_client,
            ),
            patch("xnatctl.services.uploads.upload_with_retry", side_effect=fake_retry),
            patch("xnatctl.services.uploads.httpx.Client") as mock_auth_client_cls,
        ):
            mock_auth_client = MagicMock()
            mock_auth_client.__enter__ = MagicMock(return_value=mock_auth_client)
            mock_auth_client.__exit__ = MagicMock(return_value=False)
            mock_auth_client.post.return_value = auth_response
            mock_auth_client_cls.return_value = mock_auth_client

            name, ok, err = _upload_single_file_gradual(
                base_url="https://xnat.example.org",
                session_refresher=refresher,
                verify_ssl=True,
                file_path=dcm,
                project="PROJ",
                subject="SUBJ",
                session="SESS",
            )

        assert ok is True
        assert call_count == 2
        assert call_tokens[0] == "stale-token"
        assert call_tokens[1] == "fresh-token"
