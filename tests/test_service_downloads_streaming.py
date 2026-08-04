"""Streaming, checksum, and zip-slip coverage for DownloadService.

The existing `tests/test_downloads.py` covers ID/label resolution and endpoint
selection only. Everything that actually moves bytes was untested: the
streaming write, ZIP extraction, the path-traversal guard, checksum
verification, and progress reporting.

These drive a real `XNATClient` wired to `httpx.MockTransport`, so bytes are
streamed and written for real. `download_session` currently reaches through
`client._get_client()`; using the public transport seam instead means these
tests keep working when that private access is replaced.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

from xnatctl.core.client import XNATClient
from xnatctl.models.progress import DownloadProgress, OperationPhase
from xnatctl.services.downloads import DownloadService, _md5_file, _safe_extract_zip

Handler = Callable[[httpx.Request], httpx.Response]

SCAN_FILES = {
    "scans/1/DICOM/00001.dcm": b"first-file-contents",
    "scans/1/DICOM/00002.dcm": b"second-file-contents",
    "scans/2/DICOM/00003.dcm": b"third-file-contents",
}


def build_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def make_service(handler: Handler) -> DownloadService:
    client = XNATClient(
        base_url="https://xnat.example.org",
        session_token="TOKEN",
        transport=httpx.MockTransport(handler),
    )
    return DownloadService(client)


def zip_handler(payload: bytes) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"content-length": str(len(payload))},
        )

    return handler


# =============================================================================
# Streaming to disk
# =============================================================================


def test_downloaded_bytes_land_on_disk_intact(tmp_path: Path) -> None:
    """The whole point of the transport: what the server sent is what is
    written, byte for byte."""
    service = make_service(zip_handler(build_zip(SCAN_FILES)))

    summary = service.download_session("XNAT_E00001", tmp_path)

    assert summary.success is True
    extract_dir = tmp_path / "XNAT_E00001"
    for name, expected in SCAN_FILES.items():
        assert (extract_dir / name).read_bytes() == expected


def test_file_count_and_output_path_are_reported(tmp_path: Path) -> None:
    service = make_service(zip_handler(build_zip(SCAN_FILES)))

    summary = service.download_session("XNAT_E00001", tmp_path)

    assert summary.total_files == 3
    assert summary.output_path == str(tmp_path / "XNAT_E00001")
    assert summary.session_id == "XNAT_E00001"


def test_the_intermediate_zip_is_removed(tmp_path: Path) -> None:
    """Leaving it behind would double the download's disk footprint."""
    service = make_service(zip_handler(build_zip(SCAN_FILES)))

    service.download_session("XNAT_E00001", tmp_path)

    assert not (tmp_path / "XNAT_E00001.zip").exists()


def test_output_directory_is_created_when_missing(tmp_path: Path) -> None:
    service = make_service(zip_handler(build_zip(SCAN_FILES)))
    target = tmp_path / "deep" / "nested" / "out"

    summary = service.download_session("XNAT_E00001", target)

    assert summary.success is True
    assert target.exists()


def test_a_project_does_not_put_the_download_on_an_unrouted_url(tmp_path: Path) -> None:
    """Passing a project must not move the download off the routed endpoint.

    XNAT ignores the /scans/ALL/files suffix under
    /data/projects/{P}/experiments/{E} and answers with the experiment
    document, so the project-scoped variant fetched a ZIP of nothing.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, content=build_zip(SCAN_FILES))

    make_service(handler).download_session("XNAT_E00001", tmp_path, project="MYPROJ")

    assert seen[0] == "/data/experiments/XNAT_E00001/scans/ALL/files"


def test_without_a_project_the_global_endpoint_is_used(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, content=build_zip(SCAN_FILES))

    make_service(handler).download_session("XNAT_E00001", tmp_path)

    assert seen[0] == "/data/experiments/XNAT_E00001/scans/ALL/files"


def test_pattern_is_forwarded_as_a_file_format_filter(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=build_zip(SCAN_FILES))

    make_service(handler).download_session("XNAT_E00001", tmp_path, pattern="*.dcm")

    assert dict(seen[0].url.params)["file_format"] == "*.dcm"


# =============================================================================
# Failure handling
# =============================================================================


def test_server_error_is_reported_in_the_summary_not_raised(tmp_path: Path) -> None:
    """A download failure is a result, not an exception -- the CLI turns the
    summary into an exit code."""
    service = make_service(lambda r: httpx.Response(500, text="boom"))

    summary = service.download_session("XNAT_E00001", tmp_path)

    assert summary.success is False
    assert summary.failed == 1
    assert summary.errors


def test_a_failed_download_reports_the_error_phase(tmp_path: Path) -> None:
    service = make_service(lambda r: httpx.Response(404, text="no such session"))
    seen: list[DownloadProgress] = []

    service.download_session("XNAT_E00001", tmp_path, progress_callback=seen.append)

    assert seen[-1].phase is OperationPhase.ERROR
    assert seen[-1].success is False


def test_a_corrupt_archive_fails_cleanly(tmp_path: Path) -> None:
    service = make_service(zip_handler(b"this is not a zip file"))

    summary = service.download_session("XNAT_E00001", tmp_path)

    assert summary.success is False
    assert summary.errors


# =============================================================================
# Progress reporting
# =============================================================================


def test_progress_runs_preparing_downloading_processing_complete(tmp_path: Path) -> None:
    service = make_service(zip_handler(build_zip(SCAN_FILES)))
    seen: list[DownloadProgress] = []

    service.download_session("XNAT_E00001", tmp_path, progress_callback=seen.append)

    phases = [p.phase for p in seen]
    assert phases[0] is OperationPhase.PREPARING
    assert OperationPhase.DOWNLOADING in phases
    assert OperationPhase.PROCESSING in phases
    assert phases[-1] is OperationPhase.COMPLETE


def test_byte_counts_only_ever_increase_and_end_at_the_total(tmp_path: Path) -> None:
    payload = build_zip({f"scans/1/DICOM/{i:05d}.dcm": bytes(200) for i in range(80)})
    service = make_service(zip_handler(payload))
    seen: list[DownloadProgress] = []

    service.download_session("XNAT_E00001", tmp_path, progress_callback=seen.append)

    counts = [p.bytes_received for p in seen if p.phase is OperationPhase.DOWNLOADING]
    assert counts == sorted(counts), "byte counts must be monotonically nondecreasing"
    assert counts[-1] == len(payload)


def test_total_bytes_comes_from_the_content_length_header(tmp_path: Path) -> None:
    payload = build_zip(SCAN_FILES)
    service = make_service(zip_handler(payload))
    seen: list[DownloadProgress] = []

    service.download_session("XNAT_E00001", tmp_path, progress_callback=seen.append)

    downloading = [p for p in seen if p.phase is OperationPhase.DOWNLOADING]
    assert all(p.total_bytes == len(payload) for p in downloading)


# =============================================================================
# Zip-slip guard
# =============================================================================


def test_parent_traversal_member_cannot_escape_the_extraction_root(tmp_path: Path) -> None:
    """A malicious archive must not write outside the directory the user
    asked for. Today's policy is to skip such members silently."""
    archive = tmp_path / "evil.zip"
    archive.write_bytes(build_zip({"../../escaped.txt": b"pwned", "safe.txt": b"fine"}))
    extract_dir = tmp_path / "out" / "nested"

    _safe_extract_zip(archive, extract_dir)

    assert (extract_dir / "safe.txt").read_bytes() == b"fine"
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "out" / "escaped.txt").exists()


def test_absolute_path_member_cannot_escape_the_extraction_root(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        # zipfile strips a leading "/" on write, so the member name is forged
        # directly into the archive.
        info = zipfile.ZipInfo("/tmp/xnatctl_escaped.txt")
        zf.writestr(info, b"pwned")
        zf.writestr("safe.txt", b"fine")
    archive.write_bytes(payload.getvalue())
    extract_dir = tmp_path / "out"

    _safe_extract_zip(archive, extract_dir)

    assert (extract_dir / "safe.txt").exists()
    assert not Path("/tmp/xnatctl_escaped.txt").exists()


def test_a_hostile_member_does_not_abort_the_whole_extraction(tmp_path: Path) -> None:
    """One bad entry must not cost the user the rest of a large session."""
    archive = tmp_path / "mixed.zip"
    archive.write_bytes(build_zip({"../evil.txt": b"pwned", "a.txt": b"one", "sub/b.txt": b"two"}))
    extract_dir = tmp_path / "out"

    _safe_extract_zip(archive, extract_dir)

    assert (extract_dir / "a.txt").read_bytes() == b"one"
    assert (extract_dir / "sub" / "b.txt").read_bytes() == b"two"


def test_directory_entries_are_skipped_without_error(tmp_path: Path) -> None:
    archive = tmp_path / "dirs.zip"
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("scans/", b"")
        zf.writestr("scans/file.dcm", b"data")
    archive.write_bytes(payload.getvalue())
    extract_dir = tmp_path / "out"

    _safe_extract_zip(archive, extract_dir)

    assert (extract_dir / "scans" / "file.dcm").read_bytes() == b"data"


def test_nested_directories_are_created_as_needed(tmp_path: Path) -> None:
    archive = tmp_path / "deep.zip"
    archive.write_bytes(build_zip({"a/b/c/d/file.dcm": b"deep"}))
    extract_dir = tmp_path / "out"

    _safe_extract_zip(archive, extract_dir)

    assert (extract_dir / "a" / "b" / "c" / "d" / "file.dcm").read_bytes() == b"deep"


# =============================================================================
# Checksums
# =============================================================================


def test_md5_of_a_file_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    data = b"x" * (3 * 1024 * 1024 + 17)  # spans several read chunks
    path.write_bytes(data)

    assert _md5_file(path) == hashlib.md5(data).hexdigest()


def test_md5_reads_in_chunks_rather_than_slurping(tmp_path: Path) -> None:
    """Session archives run to gigabytes; a whole-file read would be fatal."""
    path = tmp_path / "payload.bin"
    data = b"y" * 5000
    path.write_bytes(data)

    assert _md5_file(path, chunk_size=64) == hashlib.md5(data).hexdigest()


def verifying_handler(zip_bytes: bytes, checksums: dict[str, str]) -> Handler:
    """Serve the archive, then the per-file checksum listing."""

    def handler(request: httpx.Request) -> httpx.Response:
        # The download and the scan-file listing share an endpoint; the
        # listing is requested with format=json.
        if dict(request.url.params).get("format") != "json":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(
            200,
            json={
                "ResultSet": {
                    "Result": [
                        {"Name": name, "digest": digest} for name, digest in checksums.items()
                    ]
                }
            },
        )

    return handler


def test_matching_checksums_verify(tmp_path: Path) -> None:
    checksums = {
        Path(name).name: hashlib.md5(data).hexdigest() for name, data in SCAN_FILES.items()
    }
    service = make_service(verifying_handler(build_zip(SCAN_FILES), checksums))

    summary = service.download_session("XNAT_E00001", tmp_path, verify=True)

    assert summary.success is True
    assert summary.verified is True


def test_a_mismatched_checksum_is_reported(tmp_path: Path) -> None:
    """Today verification is a boolean on the summary rather than an error;
    that is the contract asserted here."""
    checksums = {
        Path(name).name: hashlib.md5(data).hexdigest() for name, data in SCAN_FILES.items()
    }
    checksums["00002.dcm"] = hashlib.md5(b"different content entirely").hexdigest()
    service = make_service(verifying_handler(build_zip(SCAN_FILES), checksums))

    summary = service.download_session("XNAT_E00001", tmp_path, verify=True)

    assert summary.verified is False


def test_same_named_files_in_different_scans_both_verify(tmp_path: Path) -> None:
    """XNAT sites that number DICOM files per scan repeat `00001.dcm` in every
    scan. A basename->single-digest map reported those byte-perfect downloads
    as corrupt; the map holds a set of digests per name instead."""
    collide = {
        "scans/1/DICOM/00001.dcm": b"scan-one-data",
        "scans/2/DICOM/00001.dcm": b"scan-two-data",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if dict(request.url.params).get("format") != "json":
            return httpx.Response(200, content=build_zip(collide))
        return httpx.Response(
            200,
            json={
                "ResultSet": {
                    "Result": [
                        {"Name": "00001.dcm", "digest": hashlib.md5(data).hexdigest()}
                        for data in collide.values()
                    ]
                }
            },
        )

    summary = make_service(handler).download_session("XNAT_E00001", tmp_path, verify=True)

    assert summary.success is True
    assert summary.total_files == 2
    assert summary.verified is True, "both files are byte-perfect"


def test_verification_is_skipped_unless_asked_for(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, content=build_zip(SCAN_FILES))

    summary = make_service(handler).download_session("XNAT_E00001", tmp_path)

    assert summary.verified is False
    assert len(seen) == 1, "no checksum listing should be fetched"


def test_verifying_nothing_is_not_a_pass(tmp_path: Path) -> None:
    """The original defect: when the listing covered none of the downloaded
    files, every comparison was skipped and verification reported success
    without checking anything."""
    service = make_service(verifying_handler(build_zip(SCAN_FILES), {}))

    summary = service.download_session("XNAT_E00001", tmp_path, verify=True)

    assert summary.success is True, "the download itself is fine"
    assert summary.verified is False


def test_verify_download_consults_both_listings_project_scoped(tmp_path: Path) -> None:
    """Scan files and session-level resources live behind different endpoints;
    checking only one left the other unverified."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"ResultSet": {"Result": []}})

    make_service(handler)._verify_download("XNAT_E00001", tmp_path, project="MYPROJ")

    # Both listings, and both on the flat form: neither suffix routes under
    # /data/projects/{P}/experiments/{E}, so a project-scoped verify collected
    # zero checksums and reported a good download as unverifiable.
    assert seen == [
        "/data/experiments/XNAT_E00001/scans/ALL/files",
        "/data/experiments/XNAT_E00001/files",
    ]


def test_verify_download_falls_back_to_the_global_listing(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"ResultSet": {"Result": []}})

    make_service(handler)._verify_download("XNAT_E00001", tmp_path)

    assert seen == [
        "/data/experiments/XNAT_E00001/scans/ALL/files",
        "/data/experiments/XNAT_E00001/files",
    ]


def test_a_session_with_no_scans_still_verifies_its_resources(tmp_path: Path) -> None:
    """The scans listing 404s for a resource-only session; that must not
    abort verification of what the other listing does cover."""
    data = b"physio-recording"
    (tmp_path / "rest1.pmu").write_bytes(data)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scans/ALL/files"):
            return httpx.Response(404, text="no scans")
        return httpx.Response(
            200,
            json={
                "ResultSet": {
                    "Result": [{"Name": "rest1.pmu", "digest": hashlib.md5(data).hexdigest()}]
                }
            },
        )

    assert make_service(handler)._verify_download("XNAT_E00001", tmp_path) is True


def test_verify_download_detects_a_corrupted_local_file(tmp_path: Path) -> None:
    good = b"the original bytes"
    (tmp_path / "scan.dcm").write_bytes(b"CORRUPTED")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ResultSet": {
                    "Result": [{"Name": "scan.dcm", "digest": hashlib.md5(good).hexdigest()}]
                }
            },
        )

    assert make_service(handler)._verify_download("XNAT_E00001", tmp_path) is False


def test_entries_without_a_digest_are_not_treated_as_a_mismatch(tmp_path: Path) -> None:
    """XNAT omits `digest` for some file types. A blank must not be compared
    against a real hash -- but nor can it count as a verified file, so a
    listing of only such entries verifies nothing."""
    (tmp_path / "scan.dcm").write_bytes(b"whatever")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ResultSet": {"Result": [{"Name": "scan.dcm", "digest": ""}]}},
        )

    assert make_service(handler)._verify_download("XNAT_E00001", tmp_path) is False
