from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

bench = pytest.importorskip("bench_transfer")

FILES = 8
FILE_KIB = 4


class TestScenarios:
    def test_download_runs_and_extracts_every_file(self) -> None:
        result = bench.run_download(
            workers=2, scans=2, files_per_scan=4, file_kib=FILE_KIB, latency_ms=0
        )

        assert result.note == "", result.note
        assert result.files == 8
        assert result.seconds > 0
        assert result.peak_rss_mib > 0

    def test_batched_upload_runs(self) -> None:
        result = bench.run_upload(
            workers=2, files=FILES, file_kib=FILE_KIB, archive_format="tar", latency_ms=0
        )

        assert result.note == "", result.note
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
        from xnatctl.services.upload import collect_dicom_files

        bench._write_synthetic_dicom(tmp_path, 5, 2048)

        assert len(collect_dicom_files(tmp_path)) == 5

    def test_the_scan_zip_matches_the_layout_the_extractor_expects(self) -> None:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(bench._build_scan_zip(3, 512))) as zf:
            names = zf.namelist()

        assert len(names) == 3
        assert all("/resources/DICOM/files/" in n for n in names)

    def test_latency_is_actually_applied(self) -> None:
        fast = bench.run_gradual(workers=1, files=4, file_kib=1, latency_ms=0)
        slow = bench.run_gradual(workers=1, files=4, file_kib=1, latency_ms=50)

        assert slow.seconds > fast.seconds + 0.15
