"""Reauth has to reach the shared client, and the cache must not retire live tokens.

Two independent defects, both invisible until a transfer ran long:

* Worker threads refreshed an expired session into their own copy of the token
  and nothing wrote it back, so the phases that run after an upload went out
  with the token the command started with.
* The cached session was declared dead 15 minutes after *creation*, though XNAT
  retires a JSESSIONID after 15 minutes of *inactivity*. A session in constant
  use was thrown away while still valid.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.core.auth import SESSION_EXPIRY_MINUTES, AuthManager, CachedSession


class TestSlidingExpiry:
    def test_a_session_in_constant_use_is_not_retired(self) -> None:
        """The bug: 15 minutes after login, a busy session was declared dead."""
        created = datetime.now() - timedelta(hours=3)
        session = CachedSession(
            token="TOK",
            url="https://x",
            username="u",
            created_at=created,
            expires_at=created + timedelta(minutes=SESSION_EXPIRY_MINUTES),
            last_used_at=datetime.now() - timedelta(minutes=1),
        )

        assert session.is_expired() is False

    def test_an_idle_session_still_expires(self) -> None:
        """Sliding must not mean never expiring."""
        idle_since = datetime.now() - timedelta(minutes=SESSION_EXPIRY_MINUTES + 5)
        session = CachedSession(
            token="TOK",
            url="https://x",
            username="u",
            created_at=idle_since,
            expires_at=idle_since + timedelta(minutes=SESSION_EXPIRY_MINUTES),
            last_used_at=idle_since,
        )

        assert session.is_expired() is True

    def test_touch_restarts_the_idle_clock(self) -> None:
        idle_since = datetime.now() - timedelta(minutes=SESSION_EXPIRY_MINUTES + 5)
        session = CachedSession(
            token="TOK",
            url="https://x",
            username="u",
            created_at=idle_since,
            expires_at=idle_since,
            last_used_at=idle_since,
        )
        assert session.is_expired() is True

        session.touch()

        assert session.is_expired() is False

    def test_a_session_with_no_deadline_never_expires(self) -> None:
        session = CachedSession(
            token="TOK", url="https://x", username="u", created_at=datetime.now()
        )
        assert session.is_expired() is False

    def test_a_cache_written_before_the_field_existed_still_loads(self) -> None:
        """Old files have no last_used_at; they fall back to created_at.

        That reproduces the previous behaviour for exactly one read, after
        which the touch on load puts them on the sliding clock.
        """
        created = datetime.now() - timedelta(minutes=1)
        session = CachedSession.from_dict(
            {
                "token": "TOK",
                "url": "https://x",
                "username": "u",
                "created_at": created.isoformat(),
                "expires_at": (created + timedelta(minutes=15)).isoformat(),
            }
        )

        assert session.last_used_at == created
        assert session.is_expired() is False

    def test_loading_a_session_slides_its_deadline_on_disk(self, tmp_path: Path) -> None:
        """The window has to move in the file, or it never slides at all."""
        manager = AuthManager(cache_file=tmp_path / ".session")
        manager.save_session("TOK", "https://x", "u")

        stale = datetime.now() - timedelta(minutes=SESSION_EXPIRY_MINUTES - 1)
        raw = json.loads((tmp_path / ".session").read_text())
        raw["last_used_at"] = stale.isoformat()
        raw["expires_at"] = (stale + timedelta(minutes=SESSION_EXPIRY_MINUTES)).isoformat()
        (tmp_path / ".session").write_text(json.dumps(raw))

        loaded = manager.load_session("https://x")
        assert loaded is not None

        after = json.loads((tmp_path / ".session").read_text())
        assert datetime.fromisoformat(after["last_used_at"]) > stale

    def test_an_expired_cache_is_still_cleared_on_load(self, tmp_path: Path) -> None:
        manager = AuthManager(cache_file=tmp_path / ".session")
        manager.save_session("TOK", "https://x", "u")

        dead = datetime.now() - timedelta(minutes=SESSION_EXPIRY_MINUTES * 2)
        raw = json.loads((tmp_path / ".session").read_text())
        raw["last_used_at"] = dead.isoformat()
        raw["expires_at"] = dead.isoformat()
        (tmp_path / ".session").write_text(json.dumps(raw))

        assert manager.load_session("https://x") is None

    def test_a_read_only_cache_does_not_break_the_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token in hand is good; a failed cache write is not worth an error."""
        manager = AuthManager(cache_file=tmp_path / ".session")
        manager.save_session("TOK", "https://x", "u")

        def boom(*_a: object, **_k: object) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr("xnatctl.core.auth.atomic_private_write", boom)

        loaded = manager.load_session("https://x")

        assert loaded is not None
        assert loaded.token == "TOK"


class TestRefreshPropagation:
    """A worker's reauth must update the client the later phases use."""

    @staticmethod
    def _refresher(owner: object | None, token: str = "OLD"):
        from xnatctl.services.upload.shared import SessionRefresher

        return SessionRefresher(
            base_url="https://x",
            verify_ssl=True,
            token=token,
            username="u",
            password="p",
            owner=owner,  # type: ignore[arg-type]
        )

    @staticmethod
    def _patch_transport(monkeypatch, handler) -> None:
        """Point the refresher's own httpx.Client at a MockTransport.

        The real class is captured first: ``uploads.httpx`` *is* the httpx
        module, so patching ``Client`` on it is a global patch, and a
        replacement that called ``httpx.Client`` would call itself.
        """
        import xnatctl.services.upload.shared as uploads

        real_client = httpx.Client

        def fake_client(**_kwargs: object) -> httpx.Client:
            return real_client(base_url="https://x", transport=httpx.MockTransport(handler))

        monkeypatch.setattr(uploads.httpx, "Client", fake_client)

    def test_a_refresh_updates_the_owning_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The defect: the client kept its original, long-dead token."""
        client = MagicMock()
        client.session_token = "OLD"
        self._patch_transport(monkeypatch, lambda _r: httpx.Response(200, text="FRESH"))

        refresher = self._refresher(client)
        new = refresher.refresh("OLD")

        assert new == "FRESH"
        assert client.session_token == "FRESH", "the owning client kept a dead token"

    def test_without_an_owner_nothing_blows_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Owner is optional; the refresher must still work standalone."""
        self._patch_transport(monkeypatch, lambda _r: httpx.Response(200, text="FRESH"))

        assert self._refresher(None).refresh("OLD") == "FRESH"

    def test_a_failed_refresh_leaves_the_client_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a *successful* reauth may overwrite the client's token."""
        client = MagicMock()
        client.session_token = "OLD"
        self._patch_transport(monkeypatch, lambda _r: httpx.Response(500, text="nope"))

        self._refresher(client).refresh("OLD")

        assert client.session_token == "OLD"

    def test_a_cache_write_failure_does_not_fail_the_upload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cache updates are a courtesy to other processes, not a dependency."""
        client = MagicMock()
        client.session_token = "OLD"
        self._patch_transport(monkeypatch, lambda _r: httpx.Response(200, text="FRESH"))

        def exploding_manager(*_a: object, **_k: object):
            raise OSError("no home directory")

        monkeypatch.setattr("xnatctl.core.auth.AuthManager", exploding_manager)

        new = self._refresher(client).refresh("OLD")

        assert new == "FRESH"
        assert client.session_token == "FRESH"

    def test_a_second_thread_reuses_the_first_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the first caller re-authenticates; this must stay true."""
        calls: list[int] = []

        def handler(_r: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, text="FRESH")

        self._patch_transport(monkeypatch, handler)
        client = MagicMock()
        refresher = self._refresher(client)

        assert refresher.refresh("OLD") == "FRESH"
        # A second worker arrives holding the same stale token it started with.
        assert refresher.refresh("OLD") == "FRESH"

        assert len(calls) == 1, "re-authenticated twice for one expiry"


class TestTheBatchPathAlsoReauthenticates:
    """A 401 mid-upload used to kill the batch.

    Only the gradual path re-authenticated; ``upload_single_archive`` returned
    "Authentication failed: invalid or expired session" even with credentials
    in hand. XNAT evicts sessions when an account exceeds its concurrent-session
    limit -- routine when several workers share a service account -- so a long
    parallel upload failed batch by batch against a healthy server.
    """

    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, refresher: object | None):
        """Upload one archive whose first attempt 401s, and report the calls."""
        import httpx

        import xnatctl.services.upload.rest_batch as uploads

        archive = tmp_path / "batch_1.tar"
        archive.write_bytes(b"tar-ish")
        posts: list[dict[str, str]] = []

        real_client = httpx.Client

        class FakeClient:
            def __init__(self, *a: object, **k: object) -> None:
                pass

            def __enter__(self) -> FakeClient:
                return self

            def __exit__(self, *a: object) -> None:
                return None

            def post(self, url: str, **kw: object) -> httpx.Response:
                jar = dict(kw.get("cookies") or {})
                posts.append(jar)
                token = jar.get("JSESSIONID")
                status = 200 if token == "FRESH" else 401
                return httpx.Response(status, request=httpx.Request("POST", "https://x" + url))

            def delete(self, *a: object, **k: object) -> None:
                return None

        assert real_client
        monkeypatch.setattr(uploads.httpx, "Client", FakeClient)

        result = uploads.upload_single_archive(
            base_url="https://xnat.example.org",
            username="u",
            password="p",
            session_token="STALE",
            verify_ssl=True,
            timeout=30,
            archive_path=archive,
            project="P",
            subject="S",
            session="E",
            import_handler="DICOM-zip",
            ignore_unparsable=True,
            overwrite="delete",
            direct_archive=True,
            session_refresher=refresher,
        )
        return result.success, result.error, posts

    def test_a_stale_token_is_refreshed_and_the_batch_retried(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock

        refresher = MagicMock()
        refresher.refresh.return_value = "FRESH"

        ok, err, posts = self._run(monkeypatch, tmp_path, refresher)

        assert ok, f"the batch failed instead of reauthenticating: {err}"
        refresher.refresh.assert_called_once_with("STALE")
        assert [p.get("JSESSIONID") for p in posts] == ["STALE", "FRESH"]

    def test_without_a_refresher_the_old_behaviour_stands(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Credential-only workers log in per batch; there is nothing to refresh."""
        ok, err, posts = self._run(monkeypatch, tmp_path, None)

        assert not ok
        assert "expired" in err.lower()
        assert len(posts) == 1

    def test_a_refresh_that_returns_the_same_token_does_not_loop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock

        refresher = MagicMock()
        refresher.refresh.return_value = "STALE"  # reauth got us nowhere

        ok, _err, posts = self._run(monkeypatch, tmp_path, refresher)

        assert not ok
        assert len(posts) == 1, "retried with a token already known to be dead"
