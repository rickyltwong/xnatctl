"""Tests for xnatctl.services.uploads utility functions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.services.uploads import (
    collect_dicom_files,
    is_retryable_status,
    split_into_batches,
    split_into_n_batches,
    upload_with_retry,
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

    def test_skips_broken_symlinks(self, temp_dir: Path):
        """Broken symlinks should be skipped."""
        # Create a valid file
        valid_file = temp_dir / "valid.dcm"
        valid_file.write_text("valid")

        # Create a broken symlink
        broken_link = temp_dir / "broken.dcm"
        broken_link.symlink_to(temp_dir / "nonexistent.dcm")

        files = collect_dicom_files(temp_dir)

        assert len(files) == 1
        assert files[0].name == "valid.dcm"

    def test_follows_valid_symlinks(self, temp_dir: Path):
        """Valid symlinks pointing to real files should be included."""
        # Create a real file
        real_file = temp_dir / "real.dcm"
        real_file.write_text("content")

        # Create a symlink to it
        link_file = temp_dir / "link.dcm"
        link_file.symlink_to(real_file)

        files = collect_dicom_files(temp_dir)

        # Both the real file and symlink should be found
        assert len(files) == 2


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


# =============================================================================
# is_retryable_status Tests
# =============================================================================


class TestIsRetryableStatus:
    def test_server_errors_are_retryable(self):
        for code in (500, 502, 503, 504):
            assert is_retryable_status(code) is True

    def test_rate_limit_is_retryable(self):
        assert is_retryable_status(429) is True

    def test_success_not_retryable(self):
        assert is_retryable_status(200) is False
        assert is_retryable_status(201) is False

    def test_400_is_retryable(self):
        assert is_retryable_status(400) is True

    def test_client_errors_not_retryable(self):
        assert is_retryable_status(401) is False
        assert is_retryable_status(403) is False
        assert is_retryable_status(404) is False


# =============================================================================
# upload_with_retry Tests
# =============================================================================


class TestUploadWithRetry:
    def _make_response(self, status_code: int) -> MagicMock:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        resp.text = f"HTTP {status_code}"
        return resp

    def test_returns_immediately_on_success(self):
        resp_200 = self._make_response(200)
        fn = MagicMock(return_value=resp_200)

        result = upload_with_retry(fn, max_retries=3, backoff_base=0)

        assert result.status_code == 200
        assert fn.call_count == 1

    def test_returns_immediately_on_non_retryable_client_error(self):
        """Non-retryable 4xx errors (e.g. 404) should NOT be retried."""
        resp_404 = self._make_response(404)
        fn = MagicMock(return_value=resp_404)

        result = upload_with_retry(fn, max_retries=3, backoff_base=0)

        assert result.status_code == 404
        assert fn.call_count == 1

    def test_retries_on_400(self):
        """400 should be retried."""
        resp_400 = self._make_response(400)
        resp_200 = self._make_response(200)
        fn = MagicMock(side_effect=[resp_400, resp_200])

        result = upload_with_retry(fn, max_retries=2, backoff_base=0)

        assert result.status_code == 200
        assert fn.call_count == 2

    def test_retries_on_server_error(self):
        """5xx should be retried, then return last response if exhausted."""
        resp_502 = self._make_response(502)
        fn = MagicMock(return_value=resp_502)

        result = upload_with_retry(fn, max_retries=2, backoff_base=0)

        assert result.status_code == 502
        assert fn.call_count == 3  # initial + 2 retries

    def test_retries_on_429(self):
        resp_429 = self._make_response(429)
        fn = MagicMock(return_value=resp_429)

        result = upload_with_retry(fn, max_retries=1, backoff_base=0)

        assert result.status_code == 429
        assert fn.call_count == 2

    def test_succeeds_after_transient_failure(self):
        """Retryable error followed by success should return success."""
        resp_503 = self._make_response(503)
        resp_200 = self._make_response(200)
        fn = MagicMock(side_effect=[resp_503, resp_200])

        result = upload_with_retry(fn, max_retries=2, backoff_base=0)

        assert result.status_code == 200
        assert fn.call_count == 2

    def test_retries_on_timeout_then_succeeds(self):
        resp_200 = self._make_response(200)
        fn = MagicMock(side_effect=[httpx.TimeoutException("timed out"), resp_200])

        result = upload_with_retry(fn, max_retries=2, backoff_base=0)

        assert result.status_code == 200
        assert fn.call_count == 2

    def test_retries_on_connect_error_then_succeeds(self):
        resp_200 = self._make_response(200)
        fn = MagicMock(side_effect=[httpx.ConnectError("refused"), resp_200])

        result = upload_with_retry(fn, max_retries=2, backoff_base=0)

        assert result.status_code == 200
        assert fn.call_count == 2

    def test_raises_timeout_after_all_retries_exhausted(self):
        fn = MagicMock(side_effect=httpx.TimeoutException("timed out"))

        with pytest.raises(httpx.TimeoutException):
            upload_with_retry(fn, max_retries=1, backoff_base=0)

        assert fn.call_count == 2

    def test_raises_connect_error_after_all_retries_exhausted(self):
        fn = MagicMock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(httpx.ConnectError):
            upload_with_retry(fn, max_retries=1, backoff_base=0)

        assert fn.call_count == 2

    def test_does_not_retry_non_transient_exceptions(self):
        """Exceptions other than Timeout/ConnectError should propagate immediately."""
        fn = MagicMock(side_effect=ValueError("bad data"))

        with pytest.raises(ValueError, match="bad data"):
            upload_with_retry(fn, max_retries=3, backoff_base=0)

        assert fn.call_count == 1
