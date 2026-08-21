#!/usr/bin/env python3
"""Throughput and peak-RSS baseline for the parallel transfer paths.

Nobody had ever measured this. The upload path streams from an open file
handle so it should not buffer an archive in memory, and the download path
writes 1 MiB chunks to a temp file so it should not buffer a ZIP -- but
"should" was the whole basis for both claims, and connection-pool behaviour
under ``--workers`` had never been looked at at all.

The server is a local ``ThreadingHTTPServer`` serving bytes prepared up front,
so what is being timed is the client: the thread pool, the HTTP stack, the
archive step, and the extraction step. Numbers from it are comparable run to
run on the same machine; they are not a prediction of throughput against a
real XNAT over a real network, where the server and the link dominate.

Each scenario runs in its own subprocess so ``ru_maxrss`` -- a whole-process
high-water mark that never falls -- measures that scenario rather than the
worst of everything before it.

Usage::

    uv run python scripts/bench_transfer.py                  # the standard set
    uv run python scripts/bench_transfer.py --json           # machine-readable
    uv run python scripts/bench_transfer.py --files 4000     # heavier
    uv run python scripts/bench_transfer.py --quick          # one case each

The recorded baseline lives in ``docs/performance.rst``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PROJECT = "BENCHPROJ"
SUBJECT = "BENCHSUBJ"
EXPERIMENT = "XNAT_E00001"

DEFAULT_FILES = 1024
DEFAULT_SCANS = 16
DEFAULT_FILE_KIB = 32
DOWNLOAD_WORKERS = (1, 4, 8, 16)
UPLOAD_WORKERS = (1, 4, 8)


# =============================================================================
# Fake XNAT
# =============================================================================


class _Payloads:
    """Response bodies built once, so the server is never what is measured."""

    def __init__(
        self, scans: int, files_per_scan: int, file_bytes: int, latency_ms: int = 0
    ) -> None:
        self.latency_s = latency_ms / 1000
        self.scan_ids = [str(i + 1) for i in range(scans)]
        self.scan_listing = json.dumps(
            {
                "ResultSet": {
                    "Result": [
                        {"ID": sid, "type": "T1", "xsiType": "xnat:mrScanData"}
                        for sid in self.scan_ids
                    ]
                }
            }
        ).encode()
        self.scan_zip = _build_scan_zip(files_per_scan, file_bytes)


def _build_scan_zip(files_per_scan: int, file_bytes: int) -> bytes:
    """Build a ZIP shaped like XNAT's scan export.

    Stored, not deflated: compressing incompressible DICOM is not what a real
    server does, and a compressible payload would make the extraction step
    look faster than it is.
    """
    blob = os.urandom(file_bytes)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as zf:
        for i in range(files_per_scan):
            name = f"{EXPERIMENT}/scans/1/resources/DICOM/files/{i + 1:06d}.dcm"
            zf.writestr(name, blob)
    return buffer.getvalue()


def _make_handler(payloads: _Payloads) -> type[BaseHTTPRequestHandler]:
    """Build a request handler closed over the prepared payloads."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive, so pooling is observable
        # Without this the per-file upload path measured 23 files/s instead of
        # 427: Nagle on the server socket meets the client's delayed ACK and
        # every small response stalls ~40 ms. That is an artifact of a loopback
        # fake, not anything a real deployment does, and leaving it in would
        # have made the recorded baseline wrong by 18x.
        disable_nagle_algorithm = True

        def log_message(self, format: str, *args: Any) -> None:
            """Stay quiet; the benchmark output is the only output."""

        def _send(self, body: bytes, content_type: str) -> None:
            # Stand-in for server think time and network RTT. Without it every
            # request completes in microseconds, extra workers only contend for
            # the GIL, and the run says nothing about the case parallelism
            # exists for.
            if payloads.latency_s:
                time.sleep(payloads.latency_s)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802  # BaseHTTPRequestHandler's spelling
            path = urlparse(self.path).path
            if path.endswith("/scans"):
                self._send(payloads.scan_listing, "application/json")
            elif "/scans/" in path and path.endswith("/files"):
                self._send(payloads.scan_zip, "application/zip")
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802  # BaseHTTPRequestHandler's spelling
            # The archive must actually be read off the socket, or the upload
            # would be timed against a server that never accepted the bytes.
            remaining = int(self.headers.get("Content-Length") or 0)
            while remaining > 0:
                remaining -= len(self.rfile.read(min(remaining, 1024 * 1024)))
            self._send(b"/archive/experiments/XNAT_E00001", "text/plain")

    return Handler


@dataclass
class _Server:
    url: str
    _httpd: ThreadingHTTPServer
    _thread: threading.Thread

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _start_server(payloads: _Payloads) -> _Server:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(payloads))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.socket.getsockname()[:2]
    return _Server(url=f"http://{host}:{port}", _httpd=httpd, _thread=thread)


# =============================================================================
# Measurement
# =============================================================================


def _peak_rss_mib() -> float:
    """Peak resident set size so far, in MiB.

    ``ru_maxrss`` is KiB on Linux and bytes on macOS. This never decreases,
    which is why each scenario gets its own process.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1024 if sys.platform != "darwin" else peak / (1024 * 1024)


@dataclass
class Result:
    """One measured run, and the numbers the baseline is written from."""

    scenario: str
    workers: int
    files: int
    payload_mib: float
    seconds: float
    files_per_second: float
    mib_per_second: float
    peak_rss_mib: float
    rss_growth_mib: float
    note: str = ""


def _make_client(base_url: str) -> Any:
    """An XNATClient pointed at the fake server, pre-tokened to skip login."""
    from xnatctl.core.client import XNATClient

    client = XNATClient(base_url=base_url, username="bench", password="bench", timeout=60)
    client.session_token = "BENCHSESSION"
    return client


# =============================================================================
# Scenarios
# =============================================================================


def run_download(
    workers: int, scans: int, files_per_scan: int, file_kib: int, latency_ms: int
) -> Result:
    """Time ``session download``'s parallel path end to end, including extraction."""
    from xnatctl.services.downloads import DownloadService

    payloads = _Payloads(scans, files_per_scan, file_kib * 1024, latency_ms)
    server = _start_server(payloads)
    out_dir = Path(tempfile.mkdtemp(prefix="bench_dl_"))
    baseline_rss = _peak_rss_mib()
    payload_mib = len(payloads.scan_zip) * scans / (1024 * 1024)

    try:
        client = _make_client(server.url)
        start = time.perf_counter()
        DownloadService(client).download_session_fast(
            session_project=PROJECT,
            subject=SUBJECT,
            resolved_session_id=EXPERIMENT,
            session_dir=out_dir,
            workers=workers,
        )
        elapsed = time.perf_counter() - start

        extracted = sum(1 for p in out_dir.rglob("*") if p.is_file())
        expected = scans * files_per_scan
        note = "" if extracted == expected else f"extracted {extracted}, expected {expected}"
    finally:
        server.close()
        shutil.rmtree(out_dir, ignore_errors=True)

    peak = _peak_rss_mib()
    return Result(
        scenario="download",
        workers=workers,
        files=scans * files_per_scan,
        payload_mib=round(payload_mib, 1),
        seconds=round(elapsed, 2),
        files_per_second=round(scans * files_per_scan / elapsed, 1),
        mib_per_second=round(payload_mib / elapsed, 1),
        peak_rss_mib=round(peak, 1),
        rss_growth_mib=round(peak - baseline_rss, 1),
        note=note,
    )


def run_gradual(workers: int, files: int, file_kib: int, latency_ms: int) -> Result:
    """Time the gradual-DICOM path: one POST per file, thread-local clients.

    The batched path issues one request per worker, so its cost is archiving,
    not HTTP. This one issues thousands, which is the case where per-request
    latency and connection reuse decide the wall time -- and the case the
    performance audit never examined.
    """
    from xnatctl.services.upload import UploadService

    payloads = _Payloads(scans=1, files_per_scan=1, file_bytes=1024, latency_ms=latency_ms)
    server = _start_server(payloads)
    src = Path(tempfile.mkdtemp(prefix="bench_gr_"))
    _write_synthetic_dicom(src, files, file_kib * 1024)
    file_list = sorted(src.rglob("*.dcm"))
    payload_mib = files * file_kib / 1024
    baseline_rss = _peak_rss_mib()

    try:
        service = UploadService(_make_client(server.url))
        start = time.perf_counter()
        summary = service.upload_dicom_gradual_files(
            files=file_list,
            project=PROJECT,
            subject=SUBJECT,
            session="BENCHSESS",
            workers=workers,
        )
        elapsed = time.perf_counter() - start
        note = "" if summary.success else f"FAILED: {summary.errors[:1]}"
    finally:
        server.close()
        shutil.rmtree(src, ignore_errors=True)

    peak = _peak_rss_mib()
    return Result(
        scenario="upload-gradual",
        workers=workers,
        files=files,
        payload_mib=round(payload_mib, 1),
        seconds=round(elapsed, 2),
        files_per_second=round(files / elapsed, 1),
        mib_per_second=round(payload_mib / elapsed, 1),
        peak_rss_mib=round(peak, 1),
        rss_growth_mib=round(peak - baseline_rss, 1),
        note=note,
    )


def run_upload(
    workers: int, files: int, file_kib: int, archive_format: str, latency_ms: int
) -> Result:
    """Time the parallel REST upload: batching, archiving, and POSTing."""
    from xnatctl.services.upload import UploadService

    payloads = _Payloads(scans=1, files_per_scan=1, file_bytes=1024, latency_ms=latency_ms)
    server = _start_server(payloads)
    src = Path(tempfile.mkdtemp(prefix="bench_ul_"))
    _write_synthetic_dicom(src, files, file_kib * 1024)
    payload_mib = files * file_kib / 1024
    baseline_rss = _peak_rss_mib()

    try:
        service = UploadService(_make_client(server.url))
        start = time.perf_counter()
        summary = service.upload_dicom_parallel(
            source_dir=src,
            project=PROJECT,
            subject=SUBJECT,
            session="BENCHSESS",
            upload_workers=workers,
            archive_workers=workers,
            archive_format=archive_format,
        )
        elapsed = time.perf_counter() - start
        note = "" if summary.success else f"FAILED: {summary.errors[:1]}"
    finally:
        server.close()
        shutil.rmtree(src, ignore_errors=True)

    peak = _peak_rss_mib()
    return Result(
        scenario=f"upload-{archive_format}",
        workers=workers,
        files=files,
        payload_mib=round(payload_mib, 1),
        seconds=round(elapsed, 2),
        files_per_second=round(files / elapsed, 1),
        mib_per_second=round(payload_mib / elapsed, 1),
        peak_rss_mib=round(peak, 1),
        rss_growth_mib=round(peak - baseline_rss, 1),
        note=note,
    )


def _write_synthetic_dicom(root: Path, count: int, file_bytes: int) -> None:
    """Write files the collector accepts: 128-byte preamble, then ``DICM``.

    Real pixel data is not needed: the parallel REST path tars or zips the
    files and POSTs them without parsing them.
    """
    body = os.urandom(max(0, file_bytes - 132))
    header = b"\0" * 128 + b"DICM"
    for i in range(count):
        # Spread across directories: one flat directory of 4000 entries is a
        # filesystem benchmark, not a transfer one.
        sub = root / f"series{i // 256:03d}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"{i:06d}.dcm").write_bytes(header + body)


# =============================================================================
# Driver
# =============================================================================


def _run_one(args: argparse.Namespace) -> Result:
    """Run the single scenario this (sub)process was started for."""
    if args.scenario == "download":
        files_per_scan = max(1, args.files // args.scans)
        return run_download(
            args.workers, args.scans, files_per_scan, args.file_kib, args.latency_ms
        )
    if args.scenario == "upload-gradual":
        return run_gradual(args.workers, args.files, args.file_kib, args.latency_ms)
    if args.scenario.startswith("upload-"):
        return run_upload(
            args.workers,
            args.files,
            args.file_kib,
            args.scenario.removeprefix("upload-"),
            args.latency_ms,
        )
    raise SystemExit(f"unknown scenario: {args.scenario}")


def _spawn(scenario: str, workers: int, args: argparse.Namespace) -> Result:
    """Run one scenario in a fresh process and read its result back."""
    completed = subprocess.run(
        [
            sys.executable,
            __file__,
            "--scenario",
            scenario,
            "--workers",
            str(workers),
            "--files",
            str(args.files),
            "--scans",
            str(args.scans),
            "--file-kib",
            str(args.file_kib),
            "--latency-ms",
            str(args.latency_ms),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"{scenario} w={workers} failed (exit {completed.returncode}):\n{completed.stderr}"
        )
    return Result(**json.loads(completed.stdout))


def _print_table(results: list[Result]) -> None:
    header = f"{'scenario':<14}{'workers':>8}{'files':>7}{'MiB':>8}{'sec':>8}"
    header += f"{'files/s':>10}{'MiB/s':>9}{'peak RSS':>10}{'growth':>9}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.scenario:<14}{r.workers:>8}{r.files:>7}{r.payload_mib:>8.1f}"
            f"{r.seconds:>8.2f}{r.files_per_second:>10.1f}{r.mib_per_second:>9.1f}"
            f"{r.peak_rss_mib:>9.1f}M{r.rss_growth_mib:>8.1f}M"
        )
        if r.note:
            print(f"  ! {r.note}")


def main() -> int:
    """Run the standard set, or the one scenario a subprocess was asked for."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=DEFAULT_FILES, help="files per run")
    parser.add_argument("--scans", type=int, default=DEFAULT_SCANS, help="scans to spread over")
    parser.add_argument("--file-kib", type=int, default=DEFAULT_FILE_KIB, help="size per file")
    parser.add_argument(
        "--latency-ms",
        type=int,
        default=0,
        help="server think time per response; 0 measures client cost, "
        "25 approximates a LAN XNAT and is what makes worker scaling visible",
    )
    parser.add_argument("--quick", action="store_true", help="one worker count per scenario")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--scenario", help=argparse.SUPPRESS)  # subprocess entry point
    parser.add_argument("--workers", type=int, default=4, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.scenario:
        print(json.dumps(asdict(_run_one(args))))
        return 0

    cases: list[tuple[str, int]] = []
    if args.quick:
        cases = [("download", 8), ("upload-tar", 4)]
    else:
        cases += [("download", w) for w in DOWNLOAD_WORKERS]
        cases += [("upload-tar", w) for w in UPLOAD_WORKERS]
        cases += [("upload-zip", 4)]
        cases += [("upload-gradual", w) for w in UPLOAD_WORKERS]

    if not args.json:
        print(
            f"{args.files} files x {args.file_kib} KiB "
            f"({args.files * args.file_kib / 1024:.0f} MiB) against a local fake XNAT, "
            f"{args.latency_ms} ms server latency\n"
        )

    results = [_spawn(scenario, workers, args) for scenario, workers in cases]

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        _print_table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
