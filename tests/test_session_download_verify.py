"""End-to-end tests for `session download --verify` against a fake XNAT server.

Reuses the real-local-HTTP-server pattern from
``test_session_download_failures.py`` rather than mocking the transport, so
these exercise the actual manifest-fetching requests (scan resource listing,
file listing with digest) and the local-tree hashing path together.
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from click.testing import CliRunner

from xnatctl.cli.common import Context
from xnatctl.cli.main import cli
from xnatctl.core.client import XNATClient

SCANS = ("1", "2")
EXPERIMENT = "XNAT_E00001"
FILE_CONTENT = b"x" * 64
FILE_DIGEST = hashlib.md5(FILE_CONTENT).hexdigest()
SESSION_RESOURCE_LABEL = "QC"
SESSION_RESOURCE_CONTENT = b"session note content"
SESSION_RESOURCE_DIGEST = hashlib.md5(SESSION_RESOURCE_CONTENT).hexdigest()
MISC_RESOURCE_LABEL = "MISC"
MISC_RESOURCE_CONTENT = b"misc resource content"
MISC_RESOURCE_DIGEST = hashlib.md5(MISC_RESOURCE_CONTENT).hexdigest()
_SESSION_RESOURCE_CONTENT_BY_LABEL = {
    SESSION_RESOURCE_LABEL: SESSION_RESOURCE_CONTENT,
    MISC_RESOURCE_LABEL: MISC_RESOURCE_CONTENT,
}
_SESSION_RESOURCE_DIGEST_BY_LABEL = {
    SESSION_RESOURCE_LABEL: SESSION_RESOURCE_DIGEST,
    MISC_RESOURCE_LABEL: MISC_RESOURCE_DIGEST,
}


def _scan_zip(scan_id: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"{EXPERIMENT}/scans/{scan_id}/resources/DICOM/files/0001.dcm", FILE_CONTENT)
    return buffer.getvalue()


def _all_scans_zip() -> bytes:
    """The single combined archive the sequential (workers=1) path fetches."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for scan_id in SCANS:
            zf.writestr(
                f"{EXPERIMENT}/scans/{scan_id}/resources/DICOM/files/0001.dcm", FILE_CONTENT
            )
    return buffer.getvalue()


def _session_resource_zip(label: str = SESSION_RESOURCE_LABEL) -> bytes:
    """The documented, real XNAT resource-ZIP shape: a session-label wrapper
    around the full ``resources/{label}/files/{name}`` path (see
    ``services/transfer/executor.py::_strip_xnat_prefix``) -- not the bare
    ``{label}/{name}`` shape this fixture previously (and incorrectly)
    synthesized.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            f"{EXPERIMENT}/resources/{label}/files/notes.txt",
            _SESSION_RESOURCE_CONTENT_BY_LABEL[label],
        )
    return buffer.getvalue()


def _serve(
    *,
    bad_digest_scan: str | None,
    session_resources: bool = False,
    session_resource_download_fails: bool = False,
    two_session_resources: bool = False,
    manifest_scans: tuple[str, ...] | None = None,
) -> Iterator[str]:
    """Fake XNAT serving a two-scan session plus its resource/file listings.

    *bad_digest_scan*, when set, reports a wrong digest for that scan's file
    so a verify pass over it must fail. *session_resources*, when set, also
    serves one session-level resource (label ``QC``); *session_resource_download_fails*
    makes fetching its ZIP 500, simulating the swallowed failure in
    ``session download --session-resources``. *two_session_resources* serves
    two (``QC`` and ``MISC``) instead, with the failure -- if requested --
    applying only to ``MISC``, so QC's download always succeeds: this
    reproduces a mid-loop failure in
    ``DownloadService.download_session_level_resources`` without losing QC's
    already-downloaded provenance. *manifest_scans*, when set, limits which
    scans' JSON file listings (the manifest source) return rows -- the
    download ZIPs are unaffected, so ``()`` yields an empty manifest over a
    fully-downloaded session and ``("1",)`` a manifest covering only scan 1.
    """
    resource_labels: list[str] = (
        [SESSION_RESOURCE_LABEL, MISC_RESOURCE_LABEL]
        if two_session_resources
        else [SESSION_RESOURCE_LABEL]
        if session_resources
        else []
    )
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
    scan_listing = json.dumps({"ResultSet": {"Result": [{"ID": s} for s in SCANS]}}).encode()
    resource_listing = json.dumps({"ResultSet": {"Result": [{"label": "DICOM"}]}}).encode()
    session_resource_listing = json.dumps(
        {"ResultSet": {"Result": [{"label": label} for label in resource_labels]}}
    ).encode()

    def session_resource_file_listing_for(label: str) -> bytes:
        return json.dumps(
            {
                "ResultSet": {
                    "Result": [
                        {
                            "Name": "notes.txt",
                            "URI": (
                                f"/data/experiments/{EXPERIMENT}/resources/{label}/files/notes.txt"
                            ),
                            "digest": _SESSION_RESOURCE_DIGEST_BY_LABEL[label],
                        }
                    ]
                }
            }
        ).encode()

    def file_listing_for(scan_id: str) -> bytes:
        if manifest_scans is not None and scan_id not in manifest_scans:
            return json.dumps({"ResultSet": {"Result": []}}).encode()
        digest = "0" * 32 if scan_id == bad_digest_scan else FILE_DIGEST
        return json.dumps(
            {
                "ResultSet": {
                    "Result": [
                        {
                            "Name": "0001.dcm",
                            "URI": (
                                f"/data/experiments/{EXPERIMENT}/scans/{scan_id}"
                                "/resources/DICOM/files/0001.dcm"
                            ),
                            "digest": digest,
                        }
                    ]
                }
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
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            fmt = query.get("format", [None])[0]

            session_resource_label = next(
                (
                    label
                    for label in resource_labels
                    if "/scans/" not in path and f"/resources/{label}/files" in path
                ),
                None,
            )
            if session_resource_label is not None:
                if fmt == "zip":
                    fails = (
                        session_resource_label == MISC_RESOURCE_LABEL
                        if two_session_resources
                        else session_resource_download_fails
                    )
                    if fails:
                        self._send(b"upstream exploded", "text/plain", 500)
                    else:
                        self._send(_session_resource_zip(session_resource_label), "application/zip")
                else:
                    self._send(
                        session_resource_file_listing_for(session_resource_label),
                        "application/json",
                    )
                return
            if resource_labels and path.endswith("/resources") and "/scans/" not in path:
                self._send(session_resource_listing, "application/json")
                return

            if "/resources/DICOM/files" in path and fmt != "zip":
                scan_id = path.split("/scans/")[1].split("/")[0]
                self._send(file_listing_for(scan_id), "application/json")
            elif path.endswith("/resources"):
                self._send(resource_listing, "application/json")
            elif path.endswith("/scans"):
                self._send(scan_listing, "application/json")
            elif "/scans/" in path and path.endswith("/files") and fmt == "zip":
                scan_id = path.split("/scans/")[1].split("/")[0]
                body = _all_scans_zip() if scan_id == "ALL" else _scan_zip(scan_id)
                self._send(body, "application/zip")
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
                    "2",
                    "--verify",
                    *extra,
                ],
            )
    finally:
        client.close()


class TestVerifyClean:
    def test_exits_zero(self, tmp_path: Path) -> None:
        for url in _serve(bad_digest_scan=None):
            result = _download(url, tmp_path)

        assert result.exit_code == 0, result.stderr

    def test_reports_verified_count(self, tmp_path: Path) -> None:
        for url in _serve(bad_digest_scan=None):
            result = _download(url, tmp_path)

        assert "Verified 2 files" in result.stderr


class TestVerifyMismatch:
    def test_exits_1(self, tmp_path: Path) -> None:
        for url in _serve(bad_digest_scan="1"):
            result = _download(url, tmp_path)

        assert result.exit_code == 1

    def test_the_mismatched_file_is_named_on_stderr(self, tmp_path: Path) -> None:
        for url in _serve(bad_digest_scan="1"):
            result = _download(url, tmp_path)

        assert "MISMATCH" in result.stderr
        assert "scans/1/resources/DICOM/0001.dcm" in result.stderr

    def test_the_other_scan_is_unaffected(self, tmp_path: Path) -> None:
        for url in _serve(bad_digest_scan="1"):
            result = _download(url, tmp_path)

        assert "scans/2/resources/DICOM/0001.dcm" not in result.stderr


class TestVerifyManifestGaps:
    def test_empty_manifest_fails(self, tmp_path: Path) -> None:
        """A server manifest listing nothing for a downloaded session must
        fail, not print 'Verified 0 files' and exit 0.
        """
        for url in _serve(bad_digest_scan=None, manifest_scans=()):
            result = _download(url, tmp_path)

        assert result.exit_code == 1
        assert "listed none" in result.stderr
        assert "nothing was verified" in result.stderr

    def test_partial_manifest_passes_but_reports_uncovered_files(self, tmp_path: Path) -> None:
        """Files the manifest never mentioned do not fail verification once
        something real was matched, but they must be reported, not silent.
        """
        for url in _serve(bad_digest_scan=None, manifest_scans=("1",)):
            result = _download(url, tmp_path)

        assert result.exit_code == 0, result.stderr
        assert "Verified 1 files" in result.stderr
        assert "absent from the server manifest" in result.stderr


def _download_sequential(url: str, out: Path, *extra: str) -> Any:
    """Like `_download`, but forces the sequential (workers=1) single-ZIP path."""
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
                    "--verify",
                    *extra,
                ],
            )
    finally:
        client.close()


class TestVerifySequentialUnextractedZip:
    """The sequential path (workers=1, no --extract) keeps scans.zip on disk;
    --verify must check the ZIP members directly, not an extracted tree.
    """

    def test_clean_exits_zero(self, tmp_path: Path) -> None:
        for url in _serve(bad_digest_scan=None):
            result = _download_sequential(url, tmp_path)

        assert result.exit_code == 0, result.stderr
        assert "Verified 2 files" in result.stderr
        # Proves it really checked the ZIP, not an (absent) extracted tree.
        assert (tmp_path / EXPERIMENT / "scans.zip").exists()

    def test_mismatch_fails(self, tmp_path: Path) -> None:
        for url in _serve(bad_digest_scan="1"):
            result = _download_sequential(url, tmp_path)

        assert result.exit_code == 1
        assert "MISMATCH" in result.stderr


class TestVerifySessionResources:
    """--session-resources --verify must cover session-level resources too."""

    def _download_with_session_resources(self, url: str, out: Path, *extra: str) -> Any:
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
                        "2",
                        "--session-resources",
                        "--verify",
                        *extra,
                    ],
                )
        finally:
            client.close()

    def test_successful_session_resource_download_verifies_clean(self, tmp_path: Path) -> None:
        for url in _serve(bad_digest_scan=None, session_resources=True):
            result = self._download_with_session_resources(url, tmp_path)

        assert result.exit_code == 0, result.stderr
        assert "Verified 3 files" in result.stderr

    def test_a_swallowed_session_resource_failure_is_still_caught(self, tmp_path: Path) -> None:
        """`download_session_level_resources` failing is swallowed by
        `session download` (it only echoes a warning and continues) --
        --verify's missing_local must be what actually fails the command.
        """
        for url in _serve(
            bad_digest_scan=None,
            session_resources=True,
            session_resource_download_fails=True,
        ):
            result = self._download_with_session_resources(url, tmp_path)

        assert result.exit_code == 1
        assert "MISSING" in result.stderr
        assert "resources/QC/notes.txt" in result.stderr

    def test_one_resource_succeeding_before_another_fails_still_verifies_the_first(
        self, tmp_path: Path
    ) -> None:
        """QC's download succeeds; MISC's then raises. QC's provenance must
        survive that exception (not be lost along with the whole method's
        return value), so verification reports QC matched and only MISC
        missing -- not both.
        """
        for url in _serve(bad_digest_scan=None, two_session_resources=True):
            result = self._download_with_session_resources(url, tmp_path)

        assert result.exit_code == 1, result.stderr
        assert "MISSING" in result.stderr
        assert "resources/MISC/notes.txt" in result.stderr
        assert "resources/QC/notes.txt" not in result.stderr

    def test_stale_leftover_zip_does_not_paper_over_this_runs_failure(self, tmp_path: Path) -> None:
        """A resources_QC.zip left over from an EARLIER run must never be
        globbed and mistaken for this run's (failed) download -- only the
        exact ZIP list this invocation produced is ever verified.
        """
        session_dir = tmp_path / EXPERIMENT
        session_dir.mkdir(parents=True)
        stale_zip = session_dir / "resources_QC.zip"
        with zipfile.ZipFile(stale_zip, "w") as zf:
            zf.writestr("QC/notes.txt", SESSION_RESOURCE_CONTENT)

        for url in _serve(
            bad_digest_scan=None,
            session_resources=True,
            session_resource_download_fails=True,
        ):
            result = self._download_with_session_resources(url, tmp_path)

        assert result.exit_code == 1, result.stderr
        assert "MISSING" in result.stderr
        assert "resources/QC/notes.txt" in result.stderr

    def test_extract_defers_session_resource_zip_deletion_until_after_verify(
        self, tmp_path: Path
    ) -> None:
        """BLOCKER regression: --extract used to flatten the session-resource
        ZIP's content to session_dir root (losing the QC label) and delete
        the ZIP before --verify ever got to read it, so a genuinely clean
        download always false-failed. The ZIP must be read intact first.
        """
        for url in _serve(bad_digest_scan=None, session_resources=True):
            result = self._download_with_session_resources(url, tmp_path, "--extract")

        assert result.exit_code == 0, result.stderr
        assert "Verified 3 files" in result.stderr
        # And the deferred extraction still ran afterward, with cleanup.
        assert not (tmp_path / EXPERIMENT / "resources_QC.zip").exists()

    def test_extract_keep_zips_still_verifies_clean_and_keeps_the_zip(self, tmp_path: Path) -> None:
        for url in _serve(bad_digest_scan=None, session_resources=True):
            result = self._download_with_session_resources(
                url, tmp_path, "--extract", "--keep-zips"
            )

        assert result.exit_code == 0, result.stderr
        assert "Verified 3 files" in result.stderr
        assert (tmp_path / EXPERIMENT / "resources_QC.zip").exists()


class TestVerifyJsonOutput:
    def test_clean_verify_reports_structured_json(self, tmp_path: Path) -> None:
        for url in _serve(bad_digest_scan=None):
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
                            str(tmp_path),
                            "-w",
                            "2",
                            "--verify",
                            "-o",
                            "json",
                        ],
                    )
            finally:
                client.close()

        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["verification"]["matched"] == 2
        assert payload["verification"]["mismatched"] == []
        assert payload["verification"]["collisions"] == []


def test_verify_is_off_by_default(tmp_path: Path) -> None:
    """Without --verify, a bad server digest must not affect the outcome."""
    for url in _serve(bad_digest_scan="1"):
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
                        str(tmp_path),
                        "-w",
                        "2",
                    ],
                )
        finally:
            client.close()

    assert result.exit_code == 0
