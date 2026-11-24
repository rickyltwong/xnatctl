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
        manager = AuthManager(cache_file=tmp_path / ".session")
        manager.save_session("TOK", "https://x", "u")

        def boom(*_a: object, **_k: object) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr("xnatctl.core.auth.atomic_private_write", boom)

        loaded = manager.load_session("https://x")

        assert loaded is not None
        assert loaded.token == "TOK"


class TestRefreshPropagation:
    @staticmethod
    def _refresher(owner: object | None, token: str = "OLD"):
        from xnatctl.services.upload.shared import SessionRefresher

        return SessionRefresher(
            base_url="https://x",
            verify_ssl=True,
            token=token,
            username="u",
            password="p",
            owner=owner,
        )

    @staticmethod
    def _patch_transport(monkeypatch, handler) -> None:
        import xnatctl.services.upload.shared as uploads

        real_client = httpx.Client

        def fake_client(**_kwargs: object) -> httpx.Client:
            return real_client(base_url="https://x", transport=httpx.MockTransport(handler))

        monkeypatch.setattr(uploads.httpx, "Client", fake_client)

    def test_a_refresh_updates_the_owning_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MagicMock()
        client.session_token = "OLD"
        self._patch_transport(monkeypatch, lambda _r: httpx.Response(200, text="FRESH"))

        refresher = self._refresher(client)
        new = refresher.refresh("OLD")

        assert new == "FRESH"
        assert client.session_token == "FRESH", "the owning client kept a dead token"

    def test_without_an_owner_nothing_blows_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_transport(monkeypatch, lambda _r: httpx.Response(200, text="FRESH"))

        assert self._refresher(None).refresh("OLD") == "FRESH"

    def test_a_failed_refresh_leaves_the_client_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.session_token = "OLD"
        self._patch_transport(monkeypatch, lambda _r: httpx.Response(500, text="nope"))

        self._refresher(client).refresh("OLD")

        assert client.session_token == "OLD"

    def test_a_cache_write_failure_does_not_fail_the_upload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        calls: list[int] = []

        def handler(_r: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, text="FRESH")

        self._patch_transport(monkeypatch, handler)
        client = MagicMock()
        refresher = self._refresher(client)

        assert refresher.refresh("OLD") == "FRESH"
        assert refresher.refresh("OLD") == "FRESH"

        assert len(calls) == 1, "re-authenticated twice for one expiry"


class TestTheBatchPathAlsoReauthenticates:
    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, refresher: object | None):
        import httpx

        import xnatctl.services.upload.rest_archive as uploads

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
                cookie = dict(kw.get("headers") or {}).get("Cookie", "")
                jar = dict([cookie.split("=", 1)]) if cookie else {}
                posts.append(jar)
                token = jar.get("JSESSIONID")
                status = 200 if token == "FRESH" else 401
                return httpx.Response(status, request=httpx.Request("POST", "https://x" + url))

            def delete(self, *a: object, **k: object) -> None:
                return None

        assert real_client
        monkeypatch.setattr(uploads.httpx, "Client", FakeClient)

        result = uploads.upload_single_archive(
            xnat_client=MagicMock(server_version=None, base_url="https://xnat.example.org"),
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
        ok, err, posts = self._run(monkeypatch, tmp_path, None)

        assert not ok
        assert "expired" in err.lower()
        assert len(posts) == 1

    def test_a_refresh_that_returns_the_same_token_does_not_loop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock

        refresher = MagicMock()
        refresher.refresh.return_value = "STALE"

        ok, _err, posts = self._run(monkeypatch, tmp_path, refresher)

        assert not ok
        assert len(posts) == 1, "retried with a token already known to be dead"
