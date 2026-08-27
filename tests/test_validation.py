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


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://xnat.example.org", "https://xnat.example.org"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("https://xnat.example.org/", "https://xnat.example.org"),
        ("https://xnat.example.org///", "https://xnat.example.org"),
        ("  https://xnat.example.org  ", "https://xnat.example.org"),
    ],
)
def test_validate_server_url_valid(url: str, expected: str) -> None:
    assert validate_server_url(url) == expected


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("", None),
        (None, None),
        ("xnat.example.org", "must include scheme"),
        ("ftp://xnat.example.org", "Unsupported scheme"),
        ("https://", "must include hostname"),
    ],
    ids=["empty", "none", "missing-scheme", "unsupported-scheme", "missing-hostname"],
)
def test_validate_server_url_invalid(url: str | None, match: str | None) -> None:
    with pytest.raises(InvalidURLError, match=match):
        validate_server_url(url)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://xnat.example.org", "https://xnat.example.org"),
        (None, None),
        ("", None),
        ("   ", None),
    ],
    ids=["valid", "none", "empty", "whitespace-only"],
)
def test_validate_url_or_none(url: str | None, expected: str | None) -> None:
    result = validate_url_or_none(url)
    if expected is None:
        assert result is None
    else:
        assert result == expected


# =============================================================================
# Port Validation Tests
# =============================================================================


@pytest.mark.parametrize(
    ("port", "expected"), [(8080, 8080), (1, 1), (65535, 65535), ("8080", 8080)]
)
def test_validate_port_valid(port: int | str, expected: int) -> None:
    assert validate_port(port) == expected


def test_validate_port_none_with_allow_none() -> None:
    assert validate_port(None, allow_none=True) is None


@pytest.mark.parametrize("port", [None, 0, 65536, -1, "not_a_port"])
def test_validate_port_invalid(port: int | str | None) -> None:
    with pytest.raises(InvalidPortError):
        validate_port(port)


# =============================================================================
# XNAT Identifier Validation Tests
# =============================================================================


@pytest.mark.parametrize("value", ["PROJECT01", "my_project", "test-123"])
def test_validate_xnat_identifier_valid(value: str) -> None:
    assert validate_xnat_identifier(value) == value


def test_validate_xnat_identifier_strips_whitespace() -> None:
    assert validate_xnat_identifier("  PROJECT01  ") == "PROJECT01"


def test_validate_xnat_identifier_empty_allowed() -> None:
    assert validate_xnat_identifier("", allow_empty=True) == ""


def test_validate_xnat_identifier_custom_max_length() -> None:
    assert validate_xnat_identifier("short", max_length=10) == "short"
    with pytest.raises(InvalidIdentifierError):
        validate_xnat_identifier("toolongvalue", max_length=5)


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("", "cannot be empty"),
        ("a" * 65, "exceeds maximum length"),
        ("project@123", "alphanumeric"),
        ("project 123", None),
        ("project/123", None),
    ],
    ids=["empty", "too-long", "at-symbol", "space", "slash"],
)
def test_validate_xnat_identifier_invalid(value: str, match: str | None) -> None:
    with pytest.raises(InvalidIdentifierError, match=match):
        validate_xnat_identifier(value)


@pytest.mark.parametrize(
    ("validator", "value", "expected"),
    [
        (validate_project_id, "ABC01_CMH", "ABC01_CMH"),
        (validate_subject_id, "SUB001", "SUB001"),
        (validate_session_id, "XNAT_E00001", "XNAT_E00001"),
        (validate_scan_id, "1", "1"),
        (validate_scan_id, "123", "123"),
        (validate_scan_id, "T1w", "T1w"),
    ],
)
def test_identifier_wrappers_valid(validator, value: str, expected: str) -> None:
    assert validator(value) == expected


def test_validate_project_id_invalid() -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_project_id("project with spaces")


# =============================================================================
# Resource Label Validation Tests
# =============================================================================


@pytest.mark.parametrize("value", ["DICOM", "NIFTI", "my-resource_01"])
def test_validate_resource_label_valid(value: str) -> None:
    assert validate_resource_label(value) == value


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("path/label", "path separators"),
        ("path\\label", None),
        ("a" * 65, "exceeds maximum length"),
    ],
    ids=["forward-slash", "backslash", "too-long"],
)
def test_validate_resource_label_invalid(value: str, match: str | None) -> None:
    with pytest.raises(InvalidIdentifierError, match=match):
        validate_resource_label(value)


# =============================================================================
# DICOM Validation Tests
# =============================================================================


@pytest.mark.parametrize("value", ["XNAT", "DICOM_STORE", "1234567890123456"])
def test_validate_ae_title_valid(value: str) -> None:
    assert validate_ae_title(value) == value


@pytest.mark.parametrize(
    ("value", "match"),
    [("12345678901234567", "exceeds maximum length"), ("AE\\TITLE", "printable ASCII")],
    ids=["17-chars", "backslash"],
)
def test_validate_ae_title_invalid(value: str, match: str) -> None:
    with pytest.raises(InvalidIdentifierError, match=match):
        validate_ae_title(value)


# =============================================================================
# Path Validation Tests
# =============================================================================


def test_validate_path_exists_file(temp_dir: Path) -> None:
    test_file = temp_dir / "test.txt"
    test_file.write_text("test content")
    assert validate_path_exists(test_file).exists()


def test_validate_path_exists_directory(temp_dir: Path) -> None:
    assert validate_path_exists(temp_dir).is_dir()


def test_validate_path_exists_nonexistent_raises(temp_dir: Path) -> None:
    with pytest.raises(PathValidationError, match="does not exist"):
        validate_path_exists(temp_dir / "nonexistent")


def test_validate_path_exists_must_be_file_raises(temp_dir: Path) -> None:
    with pytest.raises(PathValidationError, match="must be a file"):
        validate_path_exists(temp_dir, must_be_file=True)


def test_validate_path_exists_must_be_dir_raises(temp_dir: Path) -> None:
    test_file = temp_dir / "test.txt"
    test_file.write_text("test")
    with pytest.raises(PathValidationError, match="must be a directory"):
        validate_path_exists(test_file, must_be_dir=True)


def test_validate_path_writable_valid(temp_dir: Path) -> None:
    assert validate_path_writable(temp_dir / "new_file.txt").parent.exists()


def test_validate_path_writable_nonexistent_parent_raises(temp_dir: Path) -> None:
    with pytest.raises(PathValidationError, match="parent directory does not exist"):
        validate_path_writable(temp_dir / "nonexistent" / "file.txt")


# =============================================================================
# Configuration Validation Tests (timeout + workers share a shape: both take
# an int/str/None value and a ConfigurationError min/max, so their cases are
# tabulated together)
# =============================================================================


@pytest.mark.parametrize(
    ("validator", "value", "kwargs", "expected"),
    [
        (validate_timeout, 30, {}, 30),
        (validate_timeout, "60", {}, 60),
        (validate_timeout, None, {}, DEFAULT_HTTP_TIMEOUT_SECONDS),
        (validate_timeout, None, {"default": 120}, 120),
        (validate_workers, 4, {}, 4),
        (validate_workers, "8", {}, 8),
        (validate_workers, None, {}, 4),
        (validate_workers, None, {"default": 8}, 8),
    ],
)
def test_timeout_and_workers_valid(validator, value, kwargs: dict, expected: int) -> None:
    assert validator(value, **kwargs) == expected


@pytest.mark.parametrize(
    ("validator", "value", "match"),
    [
        (validate_timeout, 0, "at least"),
        (validate_timeout, "not_a_number", "valid integer"),
        (validate_workers, 0, "at least"),
        (validate_workers, 101, "cannot exceed"),
    ],
)
def test_timeout_and_workers_invalid(validator, value, match: str) -> None:
    with pytest.raises(ConfigurationError, match=match):
        validator(value)


def test_validate_regex_pattern_valid() -> None:
    result = validate_regex_pattern(r"^SUB\d{3}$")
    assert isinstance(result, re.Pattern)
    assert result.match("SUB001")


@pytest.mark.parametrize(
    ("pattern", "match"),
    [("", "cannot be empty"), ("[unclosed", "Invalid regex")],
    ids=["empty", "unclosed-bracket"],
)
def test_validate_regex_pattern_invalid(pattern: str, match: str) -> None:
    with pytest.raises(ConfigurationError, match=match):
        validate_regex_pattern(pattern)


# =============================================================================
# Batch Input Validation Tests (scan-IDs and project-list parsers share a
# shape: comma-split + per-item validate, "" -> InvalidIdentifierError)
# =============================================================================


@pytest.mark.parametrize(
    ("validator", "value", "expected"),
    [
        (validate_scan_ids_input, "*", None),
        (validate_scan_ids_input, "1", ["1"]),
        (validate_scan_ids_input, "1,2,3", ["1", "2", "3"]),
        (validate_scan_ids_input, " 1 , 2 , 3 ", ["1", "2", "3"]),
        (validate_project_list, "PROJECT01", ["PROJECT01"]),
        (validate_project_list, "PROJ1,PROJ2,PROJ3", ["PROJ1", "PROJ2", "PROJ3"]),
        (validate_project_list, " PROJ1 , PROJ2 ", ["PROJ1", "PROJ2"]),
    ],
)
def test_scan_ids_and_project_list_valid(validator, value: str, expected: list[str] | None) -> None:
    result = validator(value)
    if expected is None:
        assert result is None
    else:
        assert result == expected


@pytest.mark.parametrize(
    ("validator", "match"),
    [
        (validate_scan_ids_input, "no valid scan IDs"),
        (validate_project_list, "no valid project IDs"),
    ],
)
def test_scan_ids_and_project_list_empty_raises(validator, match: str) -> None:
    with pytest.raises(InvalidIdentifierError, match=match):
        validator("")
