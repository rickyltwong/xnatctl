"""Tests for XNAT server-version detection and feature-floor gating.

Covers ``xnatctl.core.server_version`` (parsing, the gate function) and
``XNATClient.server_version`` (probing/caching, including a failed probe).
The direct-archive call sites are exercised for the actionable-error shape
through the gate function directly plus one call-site test per transport, so
a regression in wiring shows up here rather than only in slower upload tests.
"""

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
    """Never actually sleep: a 500 probe response triggers real retry backoff."""
    monkeypatch.setattr("xnatctl.core.transport.time.sleep", lambda _s: None)


def make_client(handler: Handler, **kwargs: object) -> tuple[XNATClient, list[httpx.Request]]:
    """Build an XNATClient wired to ``handler`` via the public transport seam."""
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = XNATClient(
        base_url="https://xnat.example.org",
        transport=httpx.MockTransport(recording),
        **kwargs,  # type: ignore[arg-type]
    )
    return client, calls


# =============================================================================
# parse_server_version
# =============================================================================


class TestParseServerVersion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.8.3", (1, 8, 3)),
            ("1.9.2.1", (1, 9, 2)),  # trailing build number dropped
            ("1.9", (1, 9, 0)),  # missing patch defaults to 0
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
            # A misbehaving proxy/WAF in front of buildInfo returning a raw
            # HTTP status line: an unanchored regex would find "1.1"
            # inside this and parse it as XNAT "1.1.0", wrongly blocking a
            # legitimate upload against a server whose version was never
            # actually determined.
            "HTTP/1.1 404 Not Found",
            "HTTP/1.0 500 Internal Server Error",
        ],
    )
    def test_garbage_returns_none(self, raw: str) -> None:
        assert parse_server_version(raw) is None

    def test_none_returns_none(self) -> None:
        assert parse_server_version(None) is None

    def test_oversized_numeric_component_returns_none_not_raise(self) -> None:
        """A garbage/adversarial digit run must never reach `int()` and raise.

        Python's int-from-str conversion has its own limit and raises
        ValueError past it; parsing must be total regardless of what a
        broken or hostile buildInfo endpoint returns.
        """
        huge = "9" * 5000
        assert parse_server_version(f"{huge}.8.3") is None
        assert parse_server_version(f"1.{huge}.3") is None
        assert parse_server_version(f"1.8.{huge}") is None


# =============================================================================
# XNATClient.server_version -- probing and caching
# =============================================================================


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
        # 500 is a retryable status, but the probe opts out of the retry
        # ladder, so a failed probe is exactly one round trip -- and a second
        # property access adds none.
        client, calls = make_client(lambda _r: httpx.Response(500, text="boom"))

        assert client.server_version is None
        assert len(calls) == 1

        for _ in range(5):
            assert client.server_version is None

        assert len(calls) == 1

    def test_probe_does_not_retry_or_sleep_on_a_throttled_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A throttled buildInfo endpoint must not stall the caller.

        The retry ladder honours Retry-After up to 300s per attempt. A short
        per-request timeout does not bound that, so without an explicit
        single-shot budget a 503-ing version probe could hold up an upload
        for many minutes before failing open -- for a check whose entire
        contract is to be cheap and skippable.
        """
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
        # Single-shot: the probe overrides the client's retry budget, so even
        # a connect failure is one attempt, and repeat access re-probes never.
        assert len(calls) == 1

    def test_oversized_numeric_response_caches_none_and_does_not_reprobe(self) -> None:
        """A response body parse_server_version cannot raise on must still cache.

        Before the fix, `parse_server_version`'s ValueError on an oversized
        digit run happened outside `probe_server_version`'s try block, and
        propagated past `_server_version_probed = True` too -- so every
        subsequent access re-probed *and* re-raised.
        """
        huge = "9" * 5000
        client, calls = make_client(lambda _r: httpx.Response(200, text=f"{huge}.8.3"))

        assert client.server_version is None
        assert len(calls) == 1
        assert client.server_version is None
        assert len(calls) == 1

    def test_probe_uses_its_own_short_timeout_not_the_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stalled buildInfo endpoint must fail in seconds, not inherit the
        client's own (often multi-hour) transfer timeout.
        """
        client, _ = make_client(lambda _r: httpx.Response(200, text="1.9.2"), timeout=21600)
        assert client.timeout == 21600

        captured: dict[str, object] = {}
        real_get = client.get

        def spy_get(path: str, **kwargs: object) -> httpx.Response:
            captured.update(kwargs)
            return real_get(path, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(client, "get", spy_get)

        assert probe_server_version(client) == (1, 9, 2)
        assert captured.get("timeout") == PROBE_TIMEOUT_SECONDS
        assert client.timeout > PROBE_TIMEOUT_SECONDS

    def test_concurrent_access_probes_at_most_once(self) -> None:
        """Two threads racing the first access must not both issue a GET.

        The second thread blocks on the property's lock until the first
        probe (held mid-flight here) finishes and caches its result, then
        reads the cache instead of probing again.
        """
        import threading

        call_started = threading.Event()
        release = threading.Event()

        def handler(_r: httpx.Request) -> httpx.Response:
            call_started.set()
            assert release.wait(timeout=5), "second thread never let the probe finish"
            return httpx.Response(200, text="1.9.2")

        client, calls = make_client(handler)
        results: list[tuple[int, int, int] | None] = []
        errors: list[BaseException] = []

        def access() -> None:
            try:
                results.append(client.server_version)
            except BaseException as e:  # noqa: BLE001 -- surfaced via `errors` below
                errors.append(e)

        first = threading.Thread(target=access)
        second = threading.Thread(target=access)

        first.start()
        assert call_started.wait(timeout=5), "first thread never reached the probe"
        second.start()
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)

        assert not errors, errors
        assert results == [(1, 9, 2), (1, 9, 2)]
        assert len(calls) == 1


# =============================================================================
# require_server_version
# =============================================================================


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

        require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")  # no raise

    def test_passes_above_floor(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(200, text="1.9.2.1"))

        require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")  # no raise

    def test_passes_when_version_unknown(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(500, text="boom"))

        require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")  # no raise

    def test_passes_when_buildinfo_returns_an_unrelated_http_status_line(self) -> None:
        """A misbehaving proxy's own status line must not be read as a version."""
        client, _ = make_client(lambda _r: httpx.Response(200, text="HTTP/1.1 404 Not Found"))

        require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")  # no raise


# =============================================================================
# Direct-archive call-site wiring
# =============================================================================


class TestDirectArchiveGating:
    """The upload transports invoke the gate before doing any network work.

    Each assertion checks the exception is the actionable
    ``UnsupportedServerVersionError`` (not a raw 4xx/5xx from the import
    service) and that gating happens before any archive/import POST --
    only the version probe itself should have hit the transport.
    """

    def test_upload_archive_or_raise_gates_direct_archive(self, tmp_path: Path) -> None:
        from xnatctl.services.upload.rest_archive import upload_archive_or_raise

        archive = tmp_path / "batch.zip"
        archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # minimal empty-zip EOCD

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
                True,  # direct_archive
                True,
                False,
            )

        assert len(calls) == 1  # only the version probe, no import POST

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
                False,  # direct_archive
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


# =============================================================================
# Canonical public-boundary gating: `upload_single_archive` and
# `GradualUploadRun` are exported for direct library use, bypassing every
# wrapper above -- both must gate themselves rather than relying solely on a
# higher wrapper to do it for them.
# =============================================================================


class TestPublicBoundaryGating:
    def test_upload_single_archive_direct_call_gates_direct_archive(self, tmp_path: Path) -> None:
        from xnatctl.services.upload.rest_archive import upload_single_archive

        archive = tmp_path / "batch.zip"
        archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # minimal empty-zip EOCD

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

        assert len(calls) == 1  # only the version probe, no import POST

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

        assert len(calls) == 1  # only the version probe, no per-file upload


# =============================================================================
# Local validation must precede the gate: an empty file list, a missing
# source, or an empty directory is a caller mistake unrelated to the
# server's version, and must produce its ordinary local-validation result
# rather than an UnsupportedServerVersionError -- and must not even probe
# the version to get there.
# =============================================================================


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
