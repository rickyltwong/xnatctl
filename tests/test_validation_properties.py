"""Property-based tests for xnatctl.core.validation.

These complement the hand-enumerated cases in ``test_validation.py`` with
generated inputs, checking two properties across the validators:

* Idempotence -- for an input a validator accepts, re-validating its own
  output returns the same value.
* Rejection -- any input containing a construct a validator's own docstring
  says it rejects (a path separator, a specific control-character range, a
  dot-only segment, a Windows-reserved device name, ...) always raises the
  SPECIFIC exception subclass that validator documents, never a bare
  ``Exception`` or a different subclass.

Each rejection test is scoped to what its target function actually documents
-- e.g. ``validate_local_path_component`` only promises to reject the C0
control range (``\\x00``-``\\x1f``), not DEL or the C1 range, so its
control-character property only injects C0. ``validate_xnat_label`` and
``validate_xnat_resource_label`` document the wider C0 + DEL + C1 set, so
their properties cover that instead. Conflating the two would either produce
a test that fails against documented (not buggy) behaviour, or a test that
passes without checking anything real.

The ``ci`` Hypothesis profile (registered in ``conftest.py``: no deadline,
50 examples) keeps this file deterministic and fast.
"""

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

# =============================================================================
# Shared strategies and helpers
# =============================================================================


def _inject_at_random_position(data: st.DataObject, base: str, insert: str) -> str:
    """Splice ``insert`` into ``base`` at a hypothesis-drawn interior position.

    The position varies per example rather than always splicing at a fixed
    offset, so a test built on this helper cannot pass against a validator
    that regressed to checking only ``str.startswith``/``str.endswith`` for
    the forbidden construct -- an append-only (or prepend-only) test would
    not catch that regression, since the construct would coincidentally
    always be at the very edge it happens to check.

    ``base`` is padded to length >= 2 first if needed, and the drawn position
    is always strictly between the first and last character, so the result
    never starts or ends with ``insert``. This also means a validator's own
    leading/trailing-whitespace handling (``.strip()`` in
    ``validate_xnat_identifier``, the leading/trailing dot-or-space check in
    ``validate_local_path_component``) can never accidentally strip the very
    character being injected -- the string's own edges stay ``base[0]`` and
    ``base[-1]``, both drawn from a safe alphabet.
    """
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

# Hardcoded independently of xnatctl.core.validation.WINDOWS_RESERVED_BASENAMES
# (per its own docstring: CON/PRN/AUX/NUL, COM1-9, LPT1-9, and the
# superscript-digit COM/LPT forms). Sampling from the production constant
# itself would make the constant its own oracle -- deleting an entry (say
# "COM5") from it would green both the generator and the assertion at once,
# since neither side would ever see the deleted name.
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
# validate_ae_title strips the value before validating, so an all-whitespace
# string (legal in the alphabet above) collapses to "" and is rejected --
# excluded here since it is not actually an accepted input.
ae_title_text = st.text(alphabet=_AE_TITLE_CHARS, min_size=1, max_size=AE_TITLE_MAX_LENGTH).filter(
    lambda s: s.strip() != ""
)


# =============================================================================
# Idempotence
# =============================================================================


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


# =============================================================================
# Rejection -- validate_xnat_identifier
# =============================================================================


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


# =============================================================================
# Rejection -- validate_xnat_label (documents C0 + DEL + C1, '/', '\\', '?', '#', '%')
# =============================================================================


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


# =============================================================================
# Rejection -- validate_xnat_resource_label (documents C0 + DEL + C1, '/', '\\';
# '#', '?', '%' are explicitly ALLOWED here, unlike validate_xnat_label)
# =============================================================================


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


# =============================================================================
# Rejection -- validate_resource_label (older, stricter: only '/', '\\', length)
# =============================================================================


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


# =============================================================================
# Rejection -- validate_local_path_component
# =============================================================================


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
    assert value != unicodedata.normalize("NFC", value)  # sanity check on the fixture itself
    with pytest.raises(PathValidationError):
        validate_local_path_component(value)


# =============================================================================
# Rejection -- check_no_casefold_collision
# =============================================================================


@given(st.text(alphabet=string.ascii_letters, min_size=1, max_size=20))
def test_casefold_collision_detects_case_variant(value: str) -> None:
    seen: set[str] = set()
    check_no_casefold_collision(value, seen)
    with pytest.raises(PathValidationError):
        check_no_casefold_collision(value.swapcase(), seen)


# =============================================================================
# Rejection -- quote_path_segment (empty / dot-only)
# =============================================================================


@given(dot_only_segments)
def test_quote_path_segment_rejects_dot_only(value: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        quote_path_segment(value)


# =============================================================================
# Rejection -- validate_port
# =============================================================================


@given(st.integers().filter(lambda p: p < 1 or p > 65535))
def test_port_rejects_out_of_range_int(value: int) -> None:
    with pytest.raises(InvalidPortError):
        validate_port(value)


@given(st.text(alphabet=string.ascii_letters, min_size=1, max_size=10))
def test_port_rejects_non_numeric_string(value: str) -> None:
    with pytest.raises(InvalidPortError):
        validate_port(value)


# =============================================================================
# validate_ae_title -- arbitrary Unicode fuzzing
# =============================================================================


def _ae_title_oracle(value: str) -> tuple[bool, str]:
    """Independent grammar check for validate_ae_title, evaluated AFTER edges are stripped.

    Mirrors the DICOM AE-title grammar from validate_ae_title's own
    docstring (1-16 printable ASCII characters, 0x20-0x7E, no backslash)
    without importing anything from xnatctl.core.validation. Evaluated on
    ``value.strip()`` because the validator legitimately strips leading/
    trailing whitespace before checking the grammar --
    ``validate_ae_title("\\tXNAT") == "XNAT"``, not a raise -- so a
    whitespace character only at the very edges of the input is not itself
    a violation; one still present after stripping (i.e. anywhere in the
    interior) is.
    """
    stripped = value.strip()
    ok = 1 <= len(stripped) <= 16 and all(0x20 <= ord(c) <= 0x7E and c != "\\" for c in stripped)
    return ok, stripped


@given(st.text(max_size=40))
def test_ae_title_rejects_anything_outside_its_grammar(value: str) -> None:
    """Every input the independent oracle marks invalid actually raises.

    Without this, a validator that silently strips or coerces a bad
    character (a NUL byte, a non-ASCII code point) into something that
    happens to look like a clean result would pass the companion test below
    undetected -- that one only checks ACCEPTED results are clean, and says
    nothing about which inputs are supposed to be rejected in the first
    place.
    """
    ok, _stripped = _ae_title_oracle(value)
    if not ok:
        with pytest.raises(InvalidIdentifierError):
            validate_ae_title(value)


@given(st.text(max_size=40))
def test_ae_title_accepted_result_matches_dicom_charset_or_raises(value: str) -> None:
    """Any accepted result is <=16 printable-ASCII chars with no backslash.

    Secondary, weaker check alongside the rejection property above: this one
    says nothing about which inputs SHOULD raise, only that whatever comes
    back from an accepted call is clean.
    """
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


# =============================================================================
# Explicit branch coverage -- deterministic edge cases a random search does
# not reliably land on: a wrong-type argument (an all-string alphabet never
# rolls an int), the exact "empty" or "one-past-the-limit" degenerate case,
# or a branch that only differs by argument TYPE (str vs. Path) rather than
# content, which no string-generating strategy can exercise at all.
# =============================================================================


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
        validator(123)  # type: ignore[arg-type]


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
    # Distinct from the plain-empty-string branch: this string is non-empty
    # before .strip() but empty after, hitting the second "URL cannot be
    # empty" check rather than the first.
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
    # validate_path_exists takes str | Path; a bare str exercises the
    # isinstance(path, str) conversion branch, which no test passing a
    # pathlib.Path fixture value (e.g. tmp_path / "x") ever touches.
    with pytest.raises(PathValidationError, match="does not exist"):
        validate_path_exists(f"{tmp_path}/nonexistent-str-path")


def test_path_writable_accepts_str_argument(tmp_path: Path) -> None:
    result = validate_path_writable(f"{tmp_path}/new_file.txt")
    assert result.parent.exists()
