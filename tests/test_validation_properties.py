from __future__ import annotations

import string
import unicodedata
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from xnatctl.core.exceptions import (
    ConfigurationError,
    InvalidIdentifierError,
    InvalidPortError,
    InvalidURLError,
    PathValidationError,
)
from xnatctl.core.validation import (
    AE_TITLE_MAX_LENGTH,
    WINDOWS_RESERVED_BASENAMES,
    XNAT_ID_MAX_LENGTH,
    XNAT_LABEL_MAX_LENGTH,
    check_no_casefold_collision,
    quote_path_segment,
    quote_prearchive_segment,
    validate_ae_title,
    validate_local_path_component,
    validate_path_exists,
    validate_path_writable,
    validate_port,
    validate_resource_label,
    validate_server_url,
    validate_timeout,
    validate_workers,
    validate_xnat_identifier,
    validate_xnat_label,
    validate_xnat_resource_label,
)


def _inject_at_random_position(data: st.DataObject, base: str, insert: str) -> str:
    padded = base if len(base) >= 2 else base + "x"
    pos = data.draw(st.integers(min_value=1, max_value=len(padded) - 1))
    return padded[:pos] + insert + padded[pos:]


_XNAT_ID_CHARS = string.ascii_letters + string.digits + "_-"
xnat_id_text = st.text(alphabet=_XNAT_ID_CHARS, min_size=2, max_size=XNAT_ID_MAX_LENGTH)

_LABEL_SAFE_CHARS = string.ascii_letters + string.digits + "_-. ()"
xnat_label_text = st.text(alphabet=_LABEL_SAFE_CHARS, min_size=1, max_size=50).filter(
    lambda s: s == s.strip() and s.strip(".") != ""
)

_PATH_COMPONENT_CHARS = string.ascii_letters + string.digits + "_-"
local_path_component_text = st.text(alphabet=_PATH_COMPONENT_CHARS, min_size=1, max_size=50).filter(
    lambda s: s.upper() not in WINDOWS_RESERVED_BASENAMES
)

path_separators = st.sampled_from(["/", "\\"])
c0_control_chars = st.integers(min_value=0x00, max_value=0x1F).map(chr)
c1_control_chars = st.integers(min_value=0x80, max_value=0x9F).map(chr)
wide_control_chars = st.one_of(c0_control_chars, st.just("\x7f"), c1_control_chars)
dot_only_segments = st.sampled_from([".", "..", "...", "...."])

_INDEPENDENT_WINDOWS_RESERVED_NAMES = (
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
    "COM¹",
    "COM²",
    "COM³",
    "LPT¹",
    "LPT²",
    "LPT³",
)
windows_reserved_names = st.sampled_from(_INDEPENDENT_WINDOWS_RESERVED_NAMES)
windows_invalid_filename_chars = st.sampled_from(list('<>"|?*'))
nfd_decomposable = st.sampled_from(["é", "à", "ü", "ñ", "ç"])

_AE_TITLE_CHARS = "".join(chr(c) for c in range(0x20, 0x7F) if chr(c) != "\\")
ae_title_text = st.text(alphabet=_AE_TITLE_CHARS, min_size=1, max_size=AE_TITLE_MAX_LENGTH).filter(
    lambda s: s.strip() != ""
)


@given(xnat_id_text)
def test_xnat_identifier_idempotent(value: str) -> None:
    once = validate_xnat_identifier(value)
    assert validate_xnat_identifier(once) == once


@given(xnat_label_text)
def test_xnat_label_idempotent(value: str) -> None:
    once = validate_xnat_label(value)
    assert validate_xnat_label(once) == once


@given(xnat_label_text)
def test_xnat_resource_label_idempotent(value: str) -> None:
    once = validate_xnat_resource_label(value)
    assert validate_xnat_resource_label(once) == once


@given(st.text(alphabet=string.ascii_letters + string.digits + "_", min_size=1, max_size=64))
def test_resource_label_idempotent(value: str) -> None:
    once = validate_resource_label(value)
    assert validate_resource_label(once) == once


@given(local_path_component_text)
def test_local_path_component_idempotent(value: str) -> None:
    once = validate_local_path_component(value)
    assert validate_local_path_component(once) == once


@given(ae_title_text)
def test_ae_title_idempotent(value: str) -> None:
    once = validate_ae_title(value)
    assert validate_ae_title(once) == once


@given(st.integers(min_value=1, max_value=65535))
def test_port_idempotent(value: int) -> None:
    once = validate_port(value)
    assert validate_port(once) == once


@given(st.integers(min_value=1, max_value=86400 * 30))
def test_timeout_idempotent(value: int) -> None:
    once = validate_timeout(value)
    assert validate_timeout(once) == once


@given(st.integers(min_value=1, max_value=100))
def test_workers_idempotent(value: int) -> None:
    once = validate_workers(value)
    assert validate_workers(once) == once


@given(
    st.sampled_from(["http", "https"]),
    st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=15),
    st.one_of(st.none(), st.integers(min_value=1, max_value=65535)),
)
def test_server_url_idempotent(scheme: str, host: str, port: int | None) -> None:
    url = f"{scheme}://{host}" + (f":{port}" if port else "")
    once = validate_server_url(url)
    assert validate_server_url(once) == once


@given(
    xnat_id_text,
    st.characters(blacklist_characters=_XNAT_ID_CHARS, max_codepoint=0x10FFFF),
    st.data(),
)
def test_xnat_identifier_rejects_any_char_outside_allowlist(
    base: str, ch: str, data: st.DataObject
) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_xnat_identifier(_inject_at_random_position(data, base, ch))


@given(xnat_label_text, path_separators, st.data())
def test_xnat_label_rejects_path_separator(base: str, sep: str, data: st.DataObject) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_xnat_label(_inject_at_random_position(data, base, sep))


@given(xnat_label_text, wide_control_chars, st.data())
def test_xnat_label_rejects_control_char(base: str, ch: str, data: st.DataObject) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_xnat_label(_inject_at_random_position(data, base, ch))


@given(xnat_label_text, st.sampled_from(["?", "#", "%"]), st.data())
def test_xnat_label_rejects_url_reserved_char(base: str, ch: str, data: st.DataObject) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_xnat_label(_inject_at_random_position(data, base, ch))


@given(dot_only_segments)
def test_xnat_label_rejects_dot_only(value: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_xnat_label(value)


@given(xnat_label_text)
def test_xnat_label_rejects_leading_or_trailing_whitespace(base: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_xnat_label(" " + base)
    with pytest.raises(InvalidIdentifierError):
        validate_xnat_label(base + " ")


@given(xnat_label_text, path_separators, st.data())
def test_xnat_resource_label_rejects_path_separator(
    base: str, sep: str, data: st.DataObject
) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_xnat_resource_label(_inject_at_random_position(data, base, sep))


@given(xnat_label_text, wide_control_chars, st.data())
def test_xnat_resource_label_rejects_control_char(base: str, ch: str, data: st.DataObject) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_xnat_resource_label(_inject_at_random_position(data, base, ch))


@given(dot_only_segments)
def test_xnat_resource_label_rejects_dot_only(value: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_xnat_resource_label(value)


@given(st.sampled_from(["?", "#", "%"]), xnat_label_text, st.data())
def test_xnat_resource_label_allows_url_reserved_chars(
    ch: str, base: str, data: st.DataObject
) -> None:
    value = _inject_at_random_position(data, base, ch)
    assert validate_xnat_resource_label(value) == value


@given(
    st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=40),
    path_separators,
    st.data(),
)
def test_resource_label_rejects_path_separator(base: str, sep: str, data: st.DataObject) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_resource_label(_inject_at_random_position(data, base, sep))


@given(st.text(alphabet=string.ascii_letters + string.digits, min_size=65, max_size=100))
def test_resource_label_rejects_too_long(value: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_resource_label(value)


@given(local_path_component_text, path_separators, st.data())
def test_local_path_component_rejects_path_separator(
    base: str, sep: str, data: st.DataObject
) -> None:
    with pytest.raises(PathValidationError):
        validate_local_path_component(_inject_at_random_position(data, base, sep))


@given(local_path_component_text, c0_control_chars, st.data())
def test_local_path_component_rejects_c0_control_char(
    base: str, ch: str, data: st.DataObject
) -> None:
    with pytest.raises(PathValidationError):
        validate_local_path_component(_inject_at_random_position(data, base, ch))


@given(local_path_component_text, st.data())
def test_local_path_component_rejects_colon(base: str, data: st.DataObject) -> None:
    with pytest.raises(PathValidationError):
        validate_local_path_component(_inject_at_random_position(data, base, ":"))


@given(local_path_component_text, windows_invalid_filename_chars, st.data())
def test_local_path_component_rejects_windows_invalid_char(
    base: str, ch: str, data: st.DataObject
) -> None:
    with pytest.raises(PathValidationError):
        validate_local_path_component(_inject_at_random_position(data, base, ch))


@given(dot_only_segments)
def test_local_path_component_rejects_dot_only(value: str) -> None:
    with pytest.raises(PathValidationError):
        validate_local_path_component(value)


@given(st.sampled_from([".", " "]), st.text(alphabet=string.ascii_letters, min_size=1, max_size=10))
def test_local_path_component_rejects_leading_dot_or_space(edge: str, base: str) -> None:
    with pytest.raises(PathValidationError):
        validate_local_path_component(edge + base)


@given(st.sampled_from([".", " "]), st.text(alphabet=string.ascii_letters, min_size=1, max_size=10))
def test_local_path_component_rejects_trailing_dot_or_space(edge: str, base: str) -> None:
    with pytest.raises(PathValidationError):
        validate_local_path_component(base + edge)


@given(windows_reserved_names, st.sampled_from(["", ".txt", ".tar.gz"]), st.booleans())
def test_local_path_component_rejects_windows_reserved_basename(
    name: str, suffix: str, lower: bool
) -> None:
    value = (name.lower() if lower else name) + suffix
    with pytest.raises(PathValidationError):
        validate_local_path_component(value)


@given(nfd_decomposable)
def test_local_path_component_rejects_non_nfc_form(value: str) -> None:
    assert value != unicodedata.normalize("NFC", value)
    with pytest.raises(PathValidationError):
        validate_local_path_component(value)


@given(st.text(alphabet=string.ascii_letters, min_size=1, max_size=20))
def test_casefold_collision_detects_case_variant(value: str) -> None:
    seen: set[str] = set()
    check_no_casefold_collision(value, seen)
    with pytest.raises(PathValidationError):
        check_no_casefold_collision(value.swapcase(), seen)


@given(dot_only_segments)
def test_quote_path_segment_rejects_dot_only(value: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        quote_path_segment(value)


@given(st.integers().filter(lambda p: p < 1 or p > 65535))
def test_port_rejects_out_of_range_int(value: int) -> None:
    with pytest.raises(InvalidPortError):
        validate_port(value)


@given(st.text(alphabet=string.ascii_letters, min_size=1, max_size=10))
def test_port_rejects_non_numeric_string(value: str) -> None:
    with pytest.raises(InvalidPortError):
        validate_port(value)


def _ae_title_oracle(value: str) -> tuple[bool, str]:
    stripped = value.strip()
    ok = 1 <= len(stripped) <= 16 and all(0x20 <= ord(c) <= 0x7E and c != "\\" for c in stripped)
    return ok, stripped


@given(st.text(max_size=40))
def test_ae_title_rejects_anything_outside_its_grammar(value: str) -> None:
    ok, _stripped = _ae_title_oracle(value)
    if not ok:
        with pytest.raises(InvalidIdentifierError):
            validate_ae_title(value)


@given(st.text(max_size=40))
def test_ae_title_accepted_result_matches_dicom_charset_or_raises(value: str) -> None:
    try:
        result = validate_ae_title(value)
    except InvalidIdentifierError:
        return
    assert 1 <= len(result) <= AE_TITLE_MAX_LENGTH
    assert all(0x20 <= ord(c) <= 0x7E and c != "\\" for c in result)


@given(ae_title_text, st.data())
def test_ae_title_rejects_backslash(base: str, data: st.DataObject) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_ae_title(_inject_at_random_position(data, base, "\\"))


@given(
    st.text(
        alphabet=_AE_TITLE_CHARS,
        min_size=AE_TITLE_MAX_LENGTH + 1,
        max_size=AE_TITLE_MAX_LENGTH + 20,
    ).filter(lambda s: len(s.strip()) > AE_TITLE_MAX_LENGTH)
)
def test_ae_title_rejects_too_long(value: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        validate_ae_title(value)


@pytest.mark.parametrize(
    "validator",
    [
        validate_xnat_identifier,
        validate_xnat_label,
        validate_xnat_resource_label,
        validate_resource_label,
        validate_ae_title,
    ],
)
def test_identifier_validators_reject_non_string(validator) -> None:
    with pytest.raises(InvalidIdentifierError, match="must be a string"):
        validator(123)


@pytest.mark.parametrize(
    "validator", [validate_xnat_label, validate_xnat_resource_label, validate_resource_label]
)
def test_label_validators_reject_empty(validator) -> None:
    with pytest.raises(InvalidIdentifierError, match="cannot be empty"):
        validator("")


def test_xnat_label_rejects_too_long() -> None:
    with pytest.raises(InvalidIdentifierError, match="exceeds maximum length"):
        validate_xnat_label("a" * (XNAT_LABEL_MAX_LENGTH + 1))


def test_xnat_label_empty_allowed_returns_empty() -> None:
    assert validate_xnat_label("", allow_empty=True) == ""


def test_server_url_rejects_embedded_credentials() -> None:
    with pytest.raises(InvalidURLError, match="Do not embed credentials"):
        validate_server_url("https://admin:secret@xnat.example.org")


def test_server_url_rejects_whitespace_only() -> None:
    with pytest.raises(InvalidURLError, match="cannot be empty"):
        validate_server_url("   ")


def test_quote_path_segment_rejects_empty() -> None:
    with pytest.raises(InvalidIdentifierError):
        quote_path_segment("")


def test_quote_path_segment_returns_legal_segment_unchanged() -> None:
    assert quote_path_segment("XNAT_S00001") == "XNAT_S00001"


def test_quote_prearchive_segment_encodes_dots_and_rejects_empty() -> None:
    assert quote_prearchive_segment("John.Doe") == "John%2EDoe"
    with pytest.raises(InvalidIdentifierError):
        quote_prearchive_segment("")


def test_local_path_component_rejects_empty() -> None:
    with pytest.raises(PathValidationError):
        validate_local_path_component("")


def test_timeout_rejects_too_large() -> None:
    with pytest.raises(ConfigurationError, match="cannot exceed"):
        validate_timeout(86400 * 30 + 1)


def test_workers_rejects_non_numeric_string() -> None:
    with pytest.raises(ConfigurationError, match="valid integer"):
        validate_workers("not_a_number")


def test_path_exists_accepts_str_argument(tmp_path: Path) -> None:
    with pytest.raises(PathValidationError, match="does not exist"):
        validate_path_exists(f"{tmp_path}/nonexistent-str-path")


def test_path_writable_accepts_str_argument(tmp_path: Path) -> None:
    result = validate_path_writable(f"{tmp_path}/new_file.txt")
    assert result.parent.exists()
