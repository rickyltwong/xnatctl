"""Keep the benchmark from bitrotting.

``scripts/bench_transfer.py`` drives internals -- ``DownloadService.download_session_fast``
in the download service, ``upload_dicom_parallel`` and ``upload_dicom_gradual_files``
in the upload service. Those move, and a benchmark nobody runs between releases
would break silently and only be noticed when someone needed a number.

These are not performance assertions. A perf gate that fails on a loaded CI
runner teaches people to ignore failures; see the note at the end of
``docs/performance.rst``. What is asserted is that each scenario still runs
end to end and moves the bytes it claims to.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

bench = pytest.importorskip("bench_transfer")

# Small enough to stay a smoke test: the point is that the code path runs.
FILES = 8
FILE_KIB = 4


class TestScenarios:
    def test_download_runs_and_extracts_every_file(self) -> None:
        result = bench.run_download(
            workers=2, scans=2, files_per_scan=4, file_kib=FILE_KIB, latency_ms=0
        )

        assert result.note == "", result.note  # set when the file count is wrong
        assert result.files == 8
        assert result.seconds > 0
        assert result.peak_rss_mib > 0

    def test_batched_upload_runs(self) -> None:
        result = bench.run_upload(
            workers=2, files=FILES, file_kib=FILE_KIB, archive_format="tar", latency_ms=0
        )

        assert result.note == "", result.note  # set when the upload reports failure
        assert result.scenario == "upload-tar"
        assert result.files_per_second > 0

    def test_zip_archives_are_still_supported(self) -> None:
        result = bench.run_upload(
            workers=2, files=FILES, file_kib=FILE_KIB, archive_format="zip", latency_ms=0
        )

        assert result.note == "", result.note

    def test_gradual_upload_runs(self) -> None:
        result = bench.run_gradual(workers=2, files=FILES, file_kib=FILE_KIB, latency_ms=0)

        assert result.note == "", result.note
        assert result.scenario == "upload-gradual"


class TestHarness:
    def test_the_synthetic_files_look_like_dicom_to_the_collector(self, tmp_path: Path) -> None:
        """The collector accepts them, or every upload scenario measures zero files."""
        from xnatctl.services.upload import collect_dicom_files

        bench._write_synthetic_dicom(tmp_path, 5, 2048)

        assert len(collect_dicom_files(tmp_path)) == 5

    def test_the_scan_zip_matches_the_layout_the_extractor_expects(self) -> None:
        """A ZIP shaped wrongly would extract to UNKNOWN and still look successful."""
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(bench._build_scan_zip(3, 512))) as zf:
            names = zf.namelist()

        assert len(names) == 3
        assert all("/resources/DICOM/files/" in n for n in names)

    def test_latency_is_actually_applied(self) -> None:
        """Without this the --latency-ms flag could silently do nothing."""
        fast = bench.run_gradual(workers=1, files=4, file_kib=1, latency_ms=0)
        slow = bench.run_gradual(workers=1, files=4, file_kib=1, latency_ms=50)

        # 4 files x 50 ms on one worker is at least 0.2s of added wall time.
        assert slow.seconds > fast.seconds + 0.15
