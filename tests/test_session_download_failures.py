"""A session download that loses scans must not report success.

``_download_session_fast`` used to collect per-scan failures, echo a warning,
and return nothing. ``session_download`` then printed "Downloaded session to:
..." and exited 0 regardless -- and because the warning was suppressed under
``--quiet``, the scripting mode said nothing at all. So
``xnatctl session download -q ... && process_data`` proceeded on an incomplete
dataset with no signal that anything was missing.

ROB-01 fixed exactly this for uploads and ``scan download``; this path has no
summary object, so it was missed and nothing covered it.

These tests drive a real local HTTP server rather than mocking the transport,
because ``_download_session_fast`` builds its own ``httpx.Client`` with no
injection seam -- so a mocked transport would prove nothing about the code that
actually runs.
"""

from __future__ import annotations

import io
import json
import threading
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

from xnatctl.cli.common import Context
from xnatctl.cli.main import cli
from xnatctl.core.client import XNATClient

SCANS = ("1", "2", "3")
EXPERIMENT = "XNAT_E00001"


def _scan_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"{EXPERIMENT}/scans/1/resources/DICOM/files/0001.dcm", b"x" * 64)
    return buffer.getvalue()


def _serve(failing_scan: str | None, status: int = 500) -> Iterator[str]:
    """Run a fake XNAT where one scan's file request fails."""
    zip_bytes = _scan_zip()
    listing = json.dumps({"ResultSet": {"Result": [{"ID": s} for s in SCANS]}}).encode()
    experiment = json.dumps(
        {
            "items": [
                {
                    "data_fields": {
                        "ID": EXPERIMENT,
                        "project": "P",
                        "subject_ID": "XNAT_S1",
                        "label": "E",
                        "xsiType": "xnat:mrSessionData",
                    }
                }
            ]
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        disable_nagle_algorithm = True

        def log_message(self, format: str, *args: Any) -> None:
            """Keep pytest output readable."""

        def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802  # BaseHTTPRequestHandler's spelling
            path = urlparse(self.path).path
            if failing_scan and f"/scans/{failing_scan}/" in path:
                self._send(b"upstream exploded", "text/plain", status)
            elif path.endswith("/scans"):
                self._send(listing, "application/json")
            elif "/scans/" in path and path.endswith("/files"):
                self._send(zip_bytes, "application/zip")
            elif "/experiments/" in path:
                self._send(experiment, "application/json")
            else:
                self._send(json.dumps({"ResultSet": {"Result": []}}).encode(), "application/json")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.socket.getsockname()[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _download(url: str, out: Path, *extra: str) -> Any:
    client = XNATClient(base_url=url, session_token="tok", max_retries=0, timeout=30)
    try:
        with patch.object(Context, "get_client", return_value=client):
            return CliRunner().invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    EXPERIMENT,
                    "-P",
                    "P",
                    "--out",
                    str(out),
                    "-w",
                    "3",
                    *extra,
                ],
            )
    finally:
        client.close()


class TestAFailedScanFailsTheCommand:
    def test_the_exit_code_is_nonzero(self, tmp_path: Path) -> None:
        for url in _serve(failing_scan="2"):
            result = _download(url, tmp_path)

        assert result.exit_code != 0, "a download that lost a scan reported success"

    def test_quiet_mode_still_reports_the_failure(self, tmp_path: Path) -> None:
        """--quiet is the scripting mode; silence there is the dangerous case."""
        for url in _serve(failing_scan="2"):
            result = _download(url, tmp_path, "-q")

        assert result.exit_code != 0
        assert "failed" in result.stderr.lower()

    def test_the_failing_scan_is_named(self, tmp_path: Path) -> None:
        """So the operator can retry exactly what is missing."""
        for url in _serve(failing_scan="2"):
            result = _download(url, tmp_path)

        assert "2" in result.stderr
        assert "incomplete" in result.stderr.lower()

    def test_nothing_claims_success(self, tmp_path: Path) -> None:
        for url in _serve(failing_scan="2"):
            result = _download(url, tmp_path)

        assert "Downloaded" not in result.stdout


class TestASuccessfulDownloadIsUnaffected:
    def test_it_exits_zero(self, tmp_path: Path) -> None:
        for url in _serve(failing_scan=None):
            result = _download(url, tmp_path)

        assert result.exit_code == 0

    def test_it_reports_what_arrived_not_just_where(self, tmp_path: Path) -> None:
        """Reporting only the path let an empty tree read as a complete download."""
        for url in _serve(failing_scan=None):
            result = _download(url, tmp_path)

        assert "3 scans" in result.stderr
        assert "3 files" in result.stderr


class TestAnEmptyScanIsNotAFailure:
    def test_a_404_does_not_fail_the_command(self, tmp_path: Path) -> None:
        """Under -r, a scan with no files of that type is normal."""
        for url in _serve(failing_scan="2", status=404):
            result = _download(url, tmp_path)

        assert result.exit_code == 0

    def test_but_it_is_counted_as_zero_files(self, tmp_path: Path) -> None:
        """An all-404 session must not read as a complete download.

        ADR-0010 records that a mis-routed XNAT URL fails silently as an
        empty 200 or a 404, so this count is what would expose a future URL
        regression instead of handing back an empty tree marked success.
        """
        for url in _serve(failing_scan="2", status=404):
            result = _download(url, tmp_path)

        assert "2 files" in result.stderr, result.stderr


class TestOutcomeShape:
    """The return value exists so the caller can decide the exit code."""

    def test_it_reports_counts_and_failures(self, tmp_path: Path) -> None:
        from xnatctl.cli.session import _download_session_fast

        for url in _serve(failing_scan="2"):
            client = XNATClient(base_url=url, session_token="tok", max_retries=0, timeout=30)
            try:
                outcome = _download_session_fast(
                    client=client,
                    session_project="P",
                    subject="XNAT_S1",
                    resolved_session_id=EXPERIMENT,
                    session_dir=tmp_path / "out",
                    workers=3,
                    quiet=True,
                )
            finally:
                client.close()

        assert outcome.succeeded == 2
        assert [scan for scan, _msg in outcome.failed] == ["2"]
        assert outcome.files == 2

    def test_no_scans_returns_an_empty_outcome(self, tmp_path: Path) -> None:
        from xnatctl.cli.session import _download_session_fast

        with patch.object(XNATClient, "get_json", return_value={"ResultSet": {"Result": []}}):
            client = XNATClient(base_url="https://x.example.org", session_token="t")
            outcome = _download_session_fast(
                client=client,
                session_project="P",
                subject="S",
                resolved_session_id=EXPERIMENT,
                session_dir=tmp_path / "empty",
                workers=2,
                quiet=True,
            )

        assert outcome == (0, [], 0)


@pytest.mark.parametrize("workers", [1, 4])
def test_the_failure_is_detected_at_any_worker_count(tmp_path: Path, workers: int) -> None:
    for url in _serve(failing_scan="3"):
        client = XNATClient(base_url=url, session_token="tok", max_retries=0, timeout=30)
        try:
            with patch.object(Context, "get_client", return_value=client):
                result = CliRunner().invoke(
                    cli,
                    [
                        "session",
                        "download",
                        "-E",
                        EXPERIMENT,
                        "-P",
                        "P",
                        "--out",
                        str(tmp_path / f"w{workers}"),
                        "-w",
                        str(workers),
                        "-r",
                        "DICOM",
                    ],
                )
        finally:
            client.close()

    assert result.exit_code != 0
