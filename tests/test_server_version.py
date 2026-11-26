from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import UnsupportedServerVersionError
from xnatctl.core.server_version import (
    MIN_VERSION_DIRECT_ARCHIVE,
    PROBE_TIMEOUT_SECONDS,
    parse_server_version,
    probe_server_version,
    require_server_version,
)

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xnatctl.core.transport.time.sleep", lambda _s: None)


def make_client(handler: Handler, **kwargs: object) -> tuple[XNATClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = XNATClient(
        base_url="https://xnat.example.org",
        transport=httpx.MockTransport(recording),
        **kwargs,
    )
    return client, calls


class TestParseServerVersion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.8.3", (1, 8, 3)),
            ("1.9.2.1", (1, 9, 2)),
            ("1.9", (1, 9, 0)),
            ("1.7.6", (1, 7, 6)),
        ],
    )
    def test_parses_release_strings(self, raw: str, expected: tuple[int, int, int]) -> None:
        assert parse_server_version(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.9.3-SNAPSHOT", (1, 9, 3)),
            ("1.9.3.dev0", (1, 9, 3)),
            ("Enterprise 1.8.7", (1, 8, 7)),
            ("XNAT 1.8.3-rc1", (1, 8, 3)),
        ],
    )
    def test_parses_version_embedded_in_text(
        self, raw: str, expected: tuple[int, int, int]
    ) -> None:
        assert parse_server_version(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "unknown",
            "",
            "not a version",
            "v",
            "  ",
            "HTTP/1.1 404 Not Found",
            "HTTP/1.0 500 Internal Server Error",
        ],
    )
    def test_garbage_returns_none(self, raw: str) -> None:
        assert parse_server_version(raw) is None

    def test_none_returns_none(self) -> None:
        assert parse_server_version(None) is None

    def test_oversized_numeric_component_returns_none_not_raise(self) -> None:
        huge = "9" * 5000
        assert parse_server_version(f"{huge}.8.3") is None
        assert parse_server_version(f"1.{huge}.3") is None
        assert parse_server_version(f"1.8.{huge}") is None


class TestServerVersionProperty:
    def test_probes_buildinfo_and_parses(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, text="1.9.2.1"))

        assert client.server_version == (1, 9, 2)
        assert calls[0].url.path == "/xapi/siteConfig/buildInfo/version"

    def test_caches_across_repeated_access(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, text="1.8.9"))

        for _ in range(5):
            assert client.server_version == (1, 8, 9)

        assert len(calls) == 1

    def test_failed_probe_caches_none_and_does_not_reprobe(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(500, text="boom"))

        assert client.server_version is None
        assert len(calls) == 1

        for _ in range(5):
            assert client.server_version is None

        assert len(calls) == 1

    def test_probe_does_not_retry_or_sleep_on_a_throttled_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr("xnatctl.core.transport.time.sleep", slept.append)

        client, calls = make_client(
            lambda _r: httpx.Response(503, headers={"Retry-After": "300"}, text="busy"),
            max_retries=5,
        )

        assert client.server_version is None
        assert len(calls) == 1
        assert slept == []

    def test_unparsable_body_caches_none(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, text="not-a-version"))

        assert client.server_version is None
        assert client.server_version is None
        assert len(calls) == 1

    def test_connect_failure_caches_none(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

        client, calls = make_client(handler, max_retries=1)

        assert client.server_version is None
        assert client.server_version is None
        assert len(calls) == 1

    def test_oversized_numeric_response_caches_none_and_does_not_reprobe(self) -> None:
        huge = "9" * 5000
        client, calls = make_client(lambda _r: httpx.Response(200, text=f"{huge}.8.3"))

        assert client.server_version is None
        assert len(calls) == 1
        assert client.server_version is None
        assert len(calls) == 1

    def test_probe_uses_its_own_short_timeout_not_the_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = make_client(lambda _r: httpx.Response(200, text="1.9.2"), timeout=21600)
        assert client.timeout == 21600

        captured: dict[str, object] = {}
        real_get = client.get

        def spy_get(path: str, **kwargs: object) -> httpx.Response:
            captured.update(kwargs)
            return real_get(path, **kwargs)

        monkeypatch.setattr(client, "get", spy_get)

        assert probe_server_version(client) == (1, 9, 2)
        assert captured.get("timeout") == PROBE_TIMEOUT_SECONDS
        assert client.timeout > PROBE_TIMEOUT_SECONDS

    def test_concurrent_access_probes_at_most_once(self) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor

        call_started = threading.Event()
        release = threading.Event()

        def handler(_r: httpx.Request) -> httpx.Response:
            call_started.set()
            assert release.wait(timeout=5), "second thread never let the probe finish"
            return httpx.Response(200, text="1.9.2")

        client, calls = make_client(handler)

        def access() -> tuple[int, int, int] | None:
            return client.server_version

        pool = ThreadPoolExecutor(max_workers=2)
        try:
            first = pool.submit(access)
            assert call_started.wait(timeout=5), "first thread never reached the probe"
            second = pool.submit(access)
            release.set()
            results = [first.result(timeout=5), second.result(timeout=5)]
        finally:
            pool.shutdown(wait=False)

        assert results == [(1, 9, 2), (1, 9, 2)]
        assert len(calls) == 1


class TestRequireServerVersion:
    def test_raises_actionable_error_below_floor(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(200, text="1.7.6"))

        with pytest.raises(UnsupportedServerVersionError) as exc_info:
            require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")

        message = str(exc_info.value)
        assert "direct-archive requires XNAT >= 1.8.3" in message
        assert "server reports 1.7.6" in message

    def test_passes_at_floor(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(200, text="1.8.3"))

        require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")

    def test_passes_above_floor(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(200, text="1.9.2.1"))

        require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")

    def test_passes_when_version_unknown(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(500, text="boom"))

        require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")

    def test_passes_when_buildinfo_returns_an_unrelated_http_status_line(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(200, text="HTTP/1.1 404 Not Found"))

        require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")


class TestDirectArchiveGating:
    def test_upload_archive_or_raise_gates_direct_archive(self, tmp_path: Path) -> None:
        from xnatctl.services.upload.rest_archive import upload_archive_or_raise

        archive = tmp_path / "batch.zip"
        archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/xapi/siteConfig/buildInfo/version":
                return httpx.Response(200, text="1.7.6")
            raise AssertionError(f"unexpected request to {request.url.path}")

        client, calls = make_client(handler)

        with pytest.raises(UnsupportedServerVersionError):
            upload_archive_or_raise(
                client,
                archive,
                "PROJ",
                "SUBJ",
                "SESS",
                "none",
                True,
                True,
                False,
            )

        assert len(calls) == 1

    def test_upload_archive_or_raise_skips_gate_for_prearchive(self, tmp_path: Path) -> None:
        from xnatctl.core.exceptions import UploadError
        from xnatctl.services.upload.rest_archive import upload_archive_or_raise

        archive = tmp_path / "batch.zip"
        archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/xapi/siteConfig/buildInfo/version":
                raise AssertionError("prearchive path must not probe the version")
            return httpx.Response(502, text="server hiccup")

        client, _ = make_client(handler)

        with pytest.raises(UploadError):
            upload_archive_or_raise(
                client,
                archive,
                "PROJ",
                "SUBJ",
                "SESS",
                "none",
                False,
                True,
                False,
            )

    def test_upload_dicom_gradual_files_gates_direct_archive(self, tmp_path: Path) -> None:
        from xnatctl.services.upload.gradual import upload_dicom_gradual_files

        dicom_file = tmp_path / "IM0001.dcm"
        dicom_file.write_bytes(b"\x00" * 8)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/xapi/siteConfig/buildInfo/version":
                return httpx.Response(200, text="1.7.6")
            raise AssertionError(f"unexpected request to {request.url.path}")

        client, calls = make_client(handler)

        with pytest.raises(UnsupportedServerVersionError):
            upload_dicom_gradual_files(
                client,
                files=[dicom_file],
                project="PROJ",
                subject="SUBJ",
                session="SESS",
                direct_archive=True,
            )

        assert len(calls) == 1

    def test_upload_dicom_gradual_gates_direct_archive(self, tmp_path: Path) -> None:
        from xnatctl.services.upload.gradual import upload_dicom_gradual

        dicom_dir = tmp_path / "dicoms"
        dicom_dir.mkdir()
        (dicom_dir / "IM0001.dcm").write_bytes(b"\x00" * 8)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/xapi/siteConfig/buildInfo/version":
                return httpx.Response(200, text="1.7.6")
            raise AssertionError(f"unexpected request to {request.url.path}")

        client, calls = make_client(handler)

        with pytest.raises(UnsupportedServerVersionError):
            upload_dicom_gradual(
                client,
                dicom_dir,
                "PROJ",
                "SUBJ",
                "SESS",
                direct_archive=True,
            )

        assert len(calls) == 1

    def test_upload_dicom_parallel_gates_direct_archive(self, tmp_path: Path) -> None:
        from xnatctl.services.upload.rest_batch import upload_dicom_parallel

        dicom_dir = tmp_path / "dicoms"
        dicom_dir.mkdir()
        (dicom_dir / "IM0001.dcm").write_bytes(b"\x00" * 8)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/xapi/siteConfig/buildInfo/version":
                return httpx.Response(200, text="1.7.6")
            raise AssertionError(f"unexpected request to {request.url.path}")

        client, calls = make_client(handler)

        with pytest.raises(UnsupportedServerVersionError):
            upload_dicom_parallel(
                client,
                dicom_dir,
                "PROJ",
                "SUBJ",
                "SESS",
                direct_archive=True,
            )

        assert len(calls) == 1


class TestPublicBoundaryGating:
    def test_upload_single_archive_direct_call_gates_direct_archive(self, tmp_path: Path) -> None:
        from xnatctl.services.upload.rest_archive import upload_single_archive

        archive = tmp_path / "batch.zip"
        archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/xapi/siteConfig/buildInfo/version":
                return httpx.Response(200, text="1.7.6")
            raise AssertionError(f"unexpected request to {request.url.path}")

        client, calls = make_client(handler)

        with pytest.raises(UnsupportedServerVersionError):
            upload_single_archive(
                xnat_client=client,
                username=None,
                password=None,
                session_token="TOKEN",
                verify_ssl=True,
                timeout=30,
                archive_path=archive,
                project="PROJ",
                subject="SUBJ",
                session="SESS",
                import_handler="DICOM-zip",
                ignore_unparsable=True,
                overwrite="none",
                direct_archive=True,
            )

        assert len(calls) == 1

    def test_gradual_upload_run_direct_construction_gates_direct_archive(
        self, tmp_path: Path
    ) -> None:
        from xnatctl.services.upload.gradual import GradualUploadRun
        from xnatctl.services.upload.gradual_client import GradualClientPool

        dicom_file = tmp_path / "IM0001.dcm"
        dicom_file.write_bytes(b"\x00" * 8)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/xapi/siteConfig/buildInfo/version":
                return httpx.Response(200, text="1.7.6")
            raise AssertionError(f"unexpected request to {request.url.path}")

        client, calls = make_client(handler)

        run = GradualUploadRun(
            client=client,
            pool=GradualClientPool(),
            project="PROJ",
            subject="SUBJ",
            session="SESS",
            direct_archive=True,
            display_root=tmp_path,
            progress_callback=None,
            start_time=0.0,
        )

        with pytest.raises(UnsupportedServerVersionError):
            run.run([dicom_file], workers=1)

        assert len(calls) == 1


class TestLocalValidationPrecedesGate:
    def test_upload_dicom_gradual_files_empty_list_skips_the_gate(self) -> None:
        from xnatctl.services.upload.gradual import upload_dicom_gradual_files

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                f"unexpected request to {request.url.path} -- "
                "local validation must precede any version probe"
            )

        client, calls = make_client(handler)

        summary = upload_dicom_gradual_files(
            client,
            files=[],
            project="PROJ",
            subject="SUBJ",
            session="SESS",
            direct_archive=True,
        )

        assert summary.success is False
        assert summary.errors == ["No files provided"]
        assert calls == []

    def test_upload_dicom_gradual_missing_source_skips_the_gate(self, tmp_path: Path) -> None:
        from xnatctl.services.upload.gradual import upload_dicom_gradual

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                f"unexpected request to {request.url.path} -- "
                "local validation must precede any version probe"
            )

        client, calls = make_client(handler)

        with pytest.raises(FileNotFoundError):
            upload_dicom_gradual(
                client,
                tmp_path / "does-not-exist",
                "PROJ",
                "SUBJ",
                "SESS",
                direct_archive=True,
            )

        assert calls == []

    def test_upload_dicom_parallel_empty_directory_skips_the_gate(self, tmp_path: Path) -> None:
        from xnatctl.services.upload.rest_batch import upload_dicom_parallel

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                f"unexpected request to {request.url.path} -- "
                "local validation must precede any version probe"
            )

        client, calls = make_client(handler)

        summary = upload_dicom_parallel(
            client,
            empty_dir,
            "PROJ",
            "SUBJ",
            "SESS",
            direct_archive=True,
        )

        assert summary.success is False
        assert "No DICOM files found" in summary.errors
        assert calls == []
