"""Tests for xnatctl.core.validation module."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from xnatctl.core.exceptions import (
    ConfigurationError,
    InvalidIdentifierError,
    InvalidPortError,
    InvalidURLError,
    PathValidationError,
)
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS
from xnatctl.core.validation import (
    validate_ae_title,
    validate_path_exists,
    validate_path_writable,
    validate_port,
    validate_project_id,
    validate_project_list,
    validate_regex_pattern,
    validate_resource_label,
    validate_scan_id,
    validate_scan_ids_input,
    validate_server_url,
    validate_session_id,
    validate_subject_id,
    validate_timeout,
    validate_url_or_none,
    validate_workers,
    validate_xnat_identifier,
)

# =============================================================================
# URL Validation Tests
# =============================================================================


class TestValidateServerUrl:
    """Tests for validate_server_url."""

    def test_valid_https_url(self):
        assert validate_server_url("https://xnat.example.org") == "https://xnat.example.org"

    def test_valid_http_url(self):
        assert validate_server_url("http://localhost:8080") == "http://localhost:8080"

    def test_strips_trailing_slash(self):
        assert validate_server_url("https://xnat.example.org/") == "https://xnat.example.org"
        assert validate_server_url("https://xnat.example.org///") == "https://xnat.example.org"

    def test_strips_whitespace(self):
        assert validate_server_url("  https://xnat.example.org  ") == "https://xnat.example.org"

    def test_empty_url_raises(self):
        with pytest.raises(InvalidURLError):
            validate_server_url("")

    def test_none_raises(self):
        with pytest.raises(InvalidURLError):
            validate_server_url(None)  # type: ignore

    def test_missing_scheme_raises(self):
        with pytest.raises(InvalidURLError, match="must include scheme"):
            validate_server_url("xnat.example.org")

    def test_unsupported_scheme_raises(self):
        with pytest.raises(InvalidURLError, match="Unsupported scheme"):
            validate_server_url("ftp://xnat.example.org")

    def test_missing_hostname_raises(self):
        with pytest.raises(InvalidURLError, match="must include hostname"):
            validate_server_url("https://")


class TestValidateUrlOrNone:
    """Tests for validate_url_or_none."""

    def test_valid_url(self):
        assert validate_url_or_none("https://xnat.example.org") == "https://xnat.example.org"

    def test_none_returns_none(self):
        assert validate_url_or_none(None) is None

    def test_empty_string_returns_none(self):
        assert validate_url_or_none("") is None
        assert validate_url_or_none("   ") is None


# =============================================================================
# Port Validation Tests
# =============================================================================


class TestValidatePort:
    """Tests for validate_port."""

    def test_valid_port(self):
        assert validate_port(8080) == 8080
        assert validate_port(1) == 1
        assert validate_port(65535) == 65535

    def test_port_as_string(self):
        assert validate_port("8080") == 8080

    def test_none_with_allow_none(self):
        assert validate_port(None, allow_none=True) is None

    def test_none_without_allow_none_raises(self):
        with pytest.raises(InvalidPortError):
            validate_port(None)

    def test_port_zero_raises(self):
        with pytest.raises(InvalidPortError):
            validate_port(0)

    def test_port_too_high_raises(self):
        with pytest.raises(InvalidPortError):
            validate_port(65536)

    def test_negative_port_raises(self):
        with pytest.raises(InvalidPortError):
            validate_port(-1)

    def test_invalid_string_raises(self):
        with pytest.raises(InvalidPortError):
            validate_port("not_a_port")


# =============================================================================
# XNAT Identifier Validation Tests
# =============================================================================


class TestValidateXnatIdentifier:
    """Tests for validate_xnat_identifier."""

    def test_valid_identifier(self):
        assert validate_xnat_identifier("PROJECT01") == "PROJECT01"
        assert validate_xnat_identifier("my_project") == "my_project"
        assert validate_xnat_identifier("test-123") == "test-123"

    def test_strips_whitespace(self):
        assert validate_xnat_identifier("  PROJECT01  ") == "PROJECT01"

    def test_empty_raises(self):
        with pytest.raises(InvalidIdentifierError, match="cannot be empty"):
            validate_xnat_identifier("")

    def test_empty_allowed(self):
        assert validate_xnat_identifier("", allow_empty=True) == ""

    def test_too_long_raises(self):
        long_id = "a" * 65
        with pytest.raises(InvalidIdentifierError, match="exceeds maximum length"):
            validate_xnat_identifier(long_id)

    def test_custom_max_length(self):
        assert validate_xnat_identifier("short", max_length=10) == "short"
        with pytest.raises(InvalidIdentifierError):
            validate_xnat_identifier("toolongvalue", max_length=5)

    def test_invalid_characters_raise(self):
        with pytest.raises(InvalidIdentifierError, match="alphanumeric"):
            validate_xnat_identifier("project@123")
        with pytest.raises(InvalidIdentifierError):
            validate_xnat_identifier("project 123")
        with pytest.raises(InvalidIdentifierError):
            validate_xnat_identifier("project/123")


class TestValidateProjectId:
    """Tests for validate_project_id."""

    def test_valid_project(self):
        assert validate_project_id("ABC01_CMH") == "ABC01_CMH"

    def test_invalid_raises(self):
        with pytest.raises(InvalidIdentifierError):
            validate_project_id("project with spaces")


class TestValidateSubjectId:
    """Tests for validate_subject_id."""

    def test_valid_subject(self):
        assert validate_subject_id("SUB001") == "SUB001"


class TestValidateSessionId:
    """Tests for validate_session_id."""

    def test_valid_session(self):
        assert validate_session_id("XNAT_E00001") == "XNAT_E00001"


class TestValidateScanId:
    """Tests for validate_scan_id."""

    def test_numeric_scan_id(self):
        assert validate_scan_id("1") == "1"
        assert validate_scan_id("123") == "123"

    def test_alphanumeric_scan_id(self):
        assert validate_scan_id("T1w") == "T1w"


class TestValidateResourceLabel:
    """Tests for validate_resource_label."""

    def test_valid_label(self):
        assert validate_resource_label("DICOM") == "DICOM"
        assert validate_resource_label("NIFTI") == "NIFTI"

    def test_with_special_chars(self):
        assert validate_resource_label("my-resource_01") == "my-resource_01"

    def test_path_separator_raises(self):
        with pytest.raises(InvalidIdentifierError, match="path separators"):
            validate_resource_label("path/label")
        with pytest.raises(InvalidIdentifierError):
            validate_resource_label("path\\label")

    def test_too_long_raises(self):
        long_label = "a" * 65
        with pytest.raises(InvalidIdentifierError, match="exceeds maximum length"):
            validate_resource_label(long_label)


# =============================================================================
# DICOM Validation Tests
# =============================================================================


class TestValidateAeTitle:
    """Tests for validate_ae_title."""

    def test_valid_ae_title(self):
        assert validate_ae_title("XNAT") == "XNAT"
        assert validate_ae_title("DICOM_STORE") == "DICOM_STORE"

    def test_max_length_16(self):
        assert validate_ae_title("1234567890123456") == "1234567890123456"
        with pytest.raises(InvalidIdentifierError, match="exceeds maximum length"):
            validate_ae_title("12345678901234567")  # 17 chars

    def test_backslash_raises(self):
        with pytest.raises(InvalidIdentifierError, match="printable ASCII"):
            validate_ae_title("AE\\TITLE")


# =============================================================================
# Path Validation Tests
# =============================================================================


class TestValidatePathExists:
    """Tests for validate_path_exists."""

    def test_existing_file(self, temp_dir: Path):
        test_file = temp_dir / "test.txt"
        test_file.write_text("test content")
        result = validate_path_exists(test_file)
        assert result.exists()

    def test_existing_directory(self, temp_dir: Path):
        result = validate_path_exists(temp_dir)
        assert result.is_dir()

    def test_nonexistent_raises(self, temp_dir: Path):
        with pytest.raises(PathValidationError, match="does not exist"):
            validate_path_exists(temp_dir / "nonexistent")

    def test_must_be_file(self, temp_dir: Path):
        with pytest.raises(PathValidationError, match="must be a file"):
            validate_path_exists(temp_dir, must_be_file=True)

    def test_must_be_dir(self, temp_dir: Path):
        test_file = temp_dir / "test.txt"
        test_file.write_text("test")
        with pytest.raises(PathValidationError, match="must be a directory"):
            validate_path_exists(test_file, must_be_dir=True)


class TestValidatePathWritable:
    """Tests for validate_path_writable."""

    def test_writable_path(self, temp_dir: Path):
        result = validate_path_writable(temp_dir / "new_file.txt")
        assert result.parent.exists()

    def test_nonexistent_parent_raises(self, temp_dir: Path):
        with pytest.raises(PathValidationError, match="parent directory does not exist"):
            validate_path_writable(temp_dir / "nonexistent" / "file.txt")


# =============================================================================
# Configuration Validation Tests
# =============================================================================


class TestValidateTimeout:
    """Tests for validate_timeout."""

    def test_valid_timeout(self):
        assert validate_timeout(30) == 30
        assert validate_timeout("60") == 60

    def test_none_returns_default(self):
        assert validate_timeout(None) == DEFAULT_HTTP_TIMEOUT_SECONDS
        assert validate_timeout(None, default=120) == 120

    def test_too_small_raises(self):
        with pytest.raises(ConfigurationError, match="at least"):
            validate_timeout(0)

    def test_invalid_value_raises(self):
        with pytest.raises(ConfigurationError, match="valid integer"):
            validate_timeout("not_a_number")


class TestValidateWorkers:
    """Tests for validate_workers."""

    def test_valid_workers(self):
        assert validate_workers(4) == 4
        assert validate_workers("8") == 8

    def test_none_returns_default(self):
        assert validate_workers(None) == 4
        assert validate_workers(None, default=8) == 8

    def test_too_small_raises(self):
        with pytest.raises(ConfigurationError, match="at least"):
            validate_workers(0)

    def test_too_large_raises(self):
        with pytest.raises(ConfigurationError, match="cannot exceed"):
            validate_workers(101)


class TestValidateRegexPattern:
    """Tests for validate_regex_pattern."""

    def test_valid_pattern(self):
        result = validate_regex_pattern(r"^SUB\d{3}$")
        assert isinstance(result, re.Pattern)
        assert result.match("SUB001")

    def test_empty_raises(self):
        with pytest.raises(ConfigurationError, match="cannot be empty"):
            validate_regex_pattern("")

    def test_invalid_regex_raises(self):
        with pytest.raises(ConfigurationError, match="Invalid regex"):
            validate_regex_pattern("[unclosed")


# =============================================================================
# Batch Input Validation Tests
# =============================================================================


class TestValidateScanIdsInput:
    """Tests for validate_scan_ids_input."""

    def test_asterisk_returns_none(self):
        assert validate_scan_ids_input("*") is None

    def test_single_id(self):
        assert validate_scan_ids_input("1") == ["1"]

    def test_comma_separated(self):
        assert validate_scan_ids_input("1,2,3") == ["1", "2", "3"]

    def test_strips_whitespace(self):
        assert validate_scan_ids_input(" 1 , 2 , 3 ") == ["1", "2", "3"]

    def test_empty_raises(self):
        with pytest.raises(InvalidIdentifierError, match="no valid scan IDs"):
            validate_scan_ids_input("")


class TestValidateProjectList:
    """Tests for validate_project_list."""

    def test_single_project(self):
        assert validate_project_list("PROJECT01") == ["PROJECT01"]

    def test_comma_separated(self):
        assert validate_project_list("PROJ1,PROJ2,PROJ3") == ["PROJ1", "PROJ2", "PROJ3"]

    def test_strips_whitespace(self):
        assert validate_project_list(" PROJ1 , PROJ2 ") == ["PROJ1", "PROJ2"]

    def test_empty_raises(self):
        with pytest.raises(InvalidIdentifierError, match="no valid project IDs"):
            validate_project_list("")
