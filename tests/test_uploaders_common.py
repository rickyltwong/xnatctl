"""Tests for xnatctl.uploaders.common module."""

from __future__ import annotations

from pathlib import Path

import pytest

from xnatctl.uploaders.common import (
    collect_dicom_files,
    split_into_batches,
    split_into_n_batches,
)

# =============================================================================
# collect_dicom_files Tests
# =============================================================================


class TestCollectDicomFiles:
    """Tests for collect_dicom_files function."""

    def test_collects_dcm_files(self, temp_dir: Path):
        (temp_dir / "file1.dcm").write_text("dcm1")
        (temp_dir / "file2.dcm").write_text("dcm2")
        (temp_dir / "readme.txt").write_text("txt")

        files = collect_dicom_files(temp_dir)

        assert len(files) == 2
        assert all(f.suffix == ".dcm" for f in files)

    def test_collects_ima_files(self, temp_dir: Path):
        (temp_dir / "scan.ima").write_text("ima")

        files = collect_dicom_files(temp_dir)

        assert len(files) == 1
        assert files[0].suffix == ".ima"

    def test_collects_extensionless_files(self, temp_dir: Path):
        (temp_dir / "IM00001").write_text("dicom")
        (temp_dir / "IM00002").write_text("dicom")

        files = collect_dicom_files(temp_dir, include_extensionless=True)

        assert len(files) == 2

    def test_excludes_extensionless_when_disabled(self, temp_dir: Path):
        (temp_dir / "IM00001").write_text("dicom")
        (temp_dir / "scan.dcm").write_text("dcm")

        files = collect_dicom_files(temp_dir, include_extensionless=False)

        assert len(files) == 1
        assert files[0].suffix == ".dcm"

    def test_excludes_hidden_files(self, temp_dir: Path):
        (temp_dir / ".hidden.dcm").write_text("hidden")
        (temp_dir / "visible.dcm").write_text("visible")

        files = collect_dicom_files(temp_dir)

        assert len(files) == 1
        assert files[0].name == "visible.dcm"

    def test_recursive_search(self, temp_dir: Path):
        subdir = temp_dir / "series1"
        subdir.mkdir()
        (subdir / "file1.dcm").write_text("dcm1")
        (temp_dir / "file2.dcm").write_text("dcm2")

        files = collect_dicom_files(temp_dir)

        assert len(files) == 2

    def test_returns_sorted_paths(self, temp_dir: Path):
        (temp_dir / "c.dcm").write_text("c")
        (temp_dir / "a.dcm").write_text("a")
        (temp_dir / "b.dcm").write_text("b")

        files = collect_dicom_files(temp_dir)

        names = [f.name for f in files]
        assert names == ["a.dcm", "b.dcm", "c.dcm"]

    def test_raises_for_nonexistent_dir(self, temp_dir: Path):
        with pytest.raises(ValueError, match="Not a directory"):
            collect_dicom_files(temp_dir / "nonexistent")

    def test_raises_for_file_path(self, temp_dir: Path):
        file_path = temp_dir / "test.dcm"
        file_path.write_text("test")

        with pytest.raises(ValueError, match="Not a directory"):
            collect_dicom_files(file_path)

    def test_empty_directory(self, temp_dir: Path):
        files = collect_dicom_files(temp_dir)
        assert files == []


# =============================================================================
# split_into_batches Tests
# =============================================================================


class TestSplitIntoBatches:
    """Tests for split_into_batches function."""

    def test_splits_evenly(self, temp_dir: Path):
        files = [Path(f"file{i}.dcm") for i in range(10)]

        batches = split_into_batches(files, batch_size=5)

        assert len(batches) == 2
        assert len(batches[0]) == 5
        assert len(batches[1]) == 5

    def test_handles_remainder(self, temp_dir: Path):
        files = [Path(f"file{i}.dcm") for i in range(7)]

        batches = split_into_batches(files, batch_size=3)

        assert len(batches) == 3
        assert len(batches[0]) == 3
        assert len(batches[1]) == 3
        assert len(batches[2]) == 1

    def test_single_batch_when_fewer_files(self, temp_dir: Path):
        files = [Path(f"file{i}.dcm") for i in range(3)]

        batches = split_into_batches(files, batch_size=10)

        assert len(batches) == 1
        assert len(batches[0]) == 3

    def test_empty_input(self):
        batches = split_into_batches([], batch_size=5)
        assert batches == []

    def test_zero_batch_size_returns_single_batch(self):
        files = [Path(f"file{i}.dcm") for i in range(5)]

        batches = split_into_batches(files, batch_size=0)

        assert len(batches) == 1
        assert len(batches[0]) == 5

    def test_negative_batch_size_returns_single_batch(self):
        files = [Path(f"file{i}.dcm") for i in range(5)]

        batches = split_into_batches(files, batch_size=-1)

        assert len(batches) == 1


# =============================================================================
# split_into_n_batches Tests
# =============================================================================


class TestSplitIntoNBatches:
    """Tests for split_into_n_batches function."""

    def test_round_robin_distribution(self):
        files = [Path(f"file{i}.dcm") for i in range(10)]

        batches = split_into_n_batches(files, num_batches=3)

        assert len(batches) == 3
        # Round-robin: 0,1,2,0,1,2,0,1,2,0 -> [4,3,3]
        assert len(batches[0]) == 4
        assert len(batches[1]) == 3
        assert len(batches[2]) == 3

    def test_fewer_files_than_batches(self):
        files = [Path(f"file{i}.dcm") for i in range(2)]

        batches = split_into_n_batches(files, num_batches=5)

        # Only 2 batches created (one per file)
        assert len(batches) == 2
        assert len(batches[0]) == 1
        assert len(batches[1]) == 1

    def test_empty_input(self):
        batches = split_into_n_batches([], num_batches=5)
        assert batches == []

    def test_zero_batches_returns_single_batch(self):
        files = [Path(f"file{i}.dcm") for i in range(5)]

        batches = split_into_n_batches(files, num_batches=0)

        assert len(batches) == 1
        assert len(batches[0]) == 5

    def test_single_batch_request(self):
        files = [Path(f"file{i}.dcm") for i in range(10)]

        batches = split_into_n_batches(files, num_batches=1)

        assert len(batches) == 1
        assert len(batches[0]) == 10
