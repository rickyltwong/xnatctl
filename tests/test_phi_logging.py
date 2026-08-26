"""Guard: DICOM tag *values* (PHI) are read in exactly one place.

``xnatctl dicom validate/inspect/anonymize/list-tags/modify`` (``cli/dicom_cmd.py``)
deliberately reads identifying DICOM tags -- PatientName, PatientID, and
friends -- and writes them to the command's own stdout. That is the command's
declared purpose, not a leak.

Nowhere else in the package may reference those tag names or numbers. In
particular, nothing in ``core/`` or ``services/`` -- where log lines, retry
warnings, and exception ``details`` dicts get built -- may pull a
patient-identifying value out of a dataset and hand it to a diagnostic side
channel. An audit of the codebase (2026-08-24) found no such site; this test
pins that finding so a future change has to touch this allowlist deliberately
instead of drifting into it by accident.

Even inside the one allowlisted file, a PHI tag must never reach a *logging*
call: reading it for the command's own stdout is fine, logging it is not.
``test_dicom_cmd_never_logs_a_phi_tag`` enforces that narrower rule.

Two detection shapes are covered: a keyword-style reference (``ds.PatientName``,
``getattr(ds, "PatientID", ...)``, ``hasattr(ds, "PatientBirthDate")``, a tag
name collected into a list/dict) and a numeric-tag reference (a bare DICOM tag
int like ``0x00100020`` as pydicom's ``Dataset.get``/subscript accept it, or a
``(group, element)`` tuple like ``(0x0010, 0x0010)``). Excludes bare
string-literal statements (docstrings, and any other standalone string
expression) so prose mentioning a tag name -- ``core/validation.py`` explains
a quoting rule using "DICOM PatientName" as an example -- does not trip the
guard.

**Documented limitation:** this is a static, AST-level guard. It cannot catch
a tag built at runtime by string concatenation (``"Patient" + "Name"``),
``chr()``/``ord()`` arithmetic on a tag number, or anything else that only
exists as a value once the program runs. The logging-call rule has the same
static boundary: it flags a PHI reference appearing IN a logging call's
arguments, not indirect flow through a variable
(``value = ds.PatientName; logger.info("%s", value)``) -- taint tracking is
out of scope for an AST net. It is a net against a name or number appearing
in the source, not a proof that no code path can ever compute or route a PHI
tag reference into a log -- that's a real gap, not an oversight.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "xnatctl"
DICOM_CMD = PACKAGE_DIR / "cli" / "dicom_cmd.py"

# Every tag ``cli/dicom_cmd.py`` itself treats as identifying -- the
# required/removed/anonymized tag lists in that module are the source of
# truth for what counts as PHI here -- plus AccessionNumber, which identifies
# a specific real-world imaging order even though it is not a "patient" tag.
PHI_TAG_NAMES: frozenset[str] = frozenset(
    {
        "PatientID",
        "PatientName",
        "PatientBirthDate",
        "PatientSex",
        "PatientAddress",
        "PatientTelephoneNumbers",
        "PatientWeight",
        "OtherPatientIDs",
        "EthnicGroup",
        "InstitutionName",
        "InstitutionAddress",
        "ReferringPhysicianName",
        "PerformingPhysicianName",
        "OperatorsName",
        "AccessionNumber",
    }
)

#: (group, element) for each name above, verified against
#: ``pydicom.datadict.tag_for_keyword`` -- not retyped from memory.
_PHI_TAG_GROUP_ELEM: frozenset[tuple[int, int]] = frozenset(
    {
        (0x0010, 0x0020),  # PatientID
        (0x0010, 0x0010),  # PatientName
        (0x0010, 0x0030),  # PatientBirthDate
        (0x0010, 0x0040),  # PatientSex
        (0x0010, 0x1040),  # PatientAddress
        (0x0010, 0x2154),  # PatientTelephoneNumbers
        (0x0010, 0x1030),  # PatientWeight
        (0x0010, 0x1000),  # OtherPatientIDs
        (0x0010, 0x2160),  # EthnicGroup
        (0x0008, 0x0080),  # InstitutionName
        (0x0008, 0x0081),  # InstitutionAddress
        (0x0008, 0x0090),  # ReferringPhysicianName
        (0x0008, 0x1050),  # PerformingPhysicianName
        (0x0008, 0x1070),  # OperatorsName
        (0x0008, 0x0050),  # AccessionNumber
    }
)

#: The same tags as the single 32-bit int pydicom also accepts
#: (``ds[0x00100010]`` / ``ds.get(0x00100010)``), i.e. ``(group << 16) | element``.
PHI_TAG_INTS: frozenset[int] = frozenset(
    (group << 16) | element for group, element in _PHI_TAG_GROUP_ELEM
)

# The one file allowed to read these tags: the command's own stdout is the
# declared output, not a diagnostic side channel. It is NOT exempt from the
# logging-specific check below.
ALLOWLIST = {"cli/dicom_cmd.py"}

#: Attribute names that make a call "a logging call" for the purposes of the
#: dicom_cmd-specific rule. Not restricted to a receiver literally named
#: ``logger``: a future local log helper (e.g. a module-level ``_log_error``
#: wrapping ``logger.error``) must still be caught, the same way
#: ``test_architecture.py``'s client-call guard doesn't require the receiver
#: to be spelled ``client``.
LOG_METHOD_NAMES: frozenset[str] = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical", "log"}
)


def _package_files() -> list[Path]:
    return sorted(
        p
        for p in PACKAGE_DIR.rglob("*.py")
        if p.relative_to(PACKAGE_DIR).as_posix() not in ALLOWLIST
    )


def _docstring_and_bare_string_nodes(tree: ast.AST) -> set[ast.expr]:
    """Constant string nodes that are prose, not code: bare string statements.

    Covers module/class/function docstrings and any other standalone string
    expression (``"some prose"`` as its own statement), which is how
    ``validation.py`` mentions ``PatientName`` in an explanatory docstring.
    """
    bare: set[ast.expr] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            bare.add(node.value)
    return bare


def _is_phi_tuple(node: ast.AST) -> bool:
    """True for a 2-element ``(group, element)`` int literal pair, e.g. ``(0x0010, 0x0010)``.

    The two halves are not flagged individually -- a bare ``0x0010`` is just
    "group 0x0010" and appears constantly in innocent code -- only the exact
    pair identifies a PHI tag.
    """
    if not (isinstance(node, ast.Tuple) and len(node.elts) == 2):
        return False
    group, element = node.elts
    if not (
        isinstance(group, ast.Constant)
        and isinstance(group.value, int)
        and not isinstance(group.value, bool)
        and isinstance(element, ast.Constant)
        and isinstance(element.value, int)
        and not isinstance(element.value, bool)
    ):
        return False
    return (group.value, element.value) in _PHI_TAG_GROUP_ELEM


def _phi_tag_nodes(
    root: ast.AST, bare_strings: set[ast.expr] | frozenset[ast.expr] = frozenset()
) -> list[ast.AST]:
    """Every AST node under ``root`` that reads or names a PHI-bearing tag as code.

    Four shapes matter: ``x.PatientName``-style attribute access; a string
    constant that exactly equals a tag name (``getattr(ds, "PatientID", "")``,
    ``hasattr(ds, "PatientBirthDate")``, a tag name collected into a list for
    iteration); a bare int constant equal to a tag's combined 32-bit value
    (``ds[0x00100010]``, ``ds.get(0x00100010)``); and a ``(group, element)``
    int-literal pair (``ds[0x0010, 0x0010]``).
    """
    offenders: list[ast.AST] = []
    for node in ast.walk(root):
        is_attr = isinstance(node, ast.Attribute) and node.attr in PHI_TAG_NAMES
        is_name_string = (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in PHI_TAG_NAMES
            and node not in bare_strings
        )
        is_tag_int = (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and not isinstance(node.value, bool)
            and node.value in PHI_TAG_INTS
        )
        if is_attr or is_name_string or is_tag_int or _is_phi_tuple(node):
            offenders.append(node)
    return offenders


def _phi_tag_references(tree: ast.Module) -> list[ast.AST]:
    """Whole-module PHI tag scan, with docstrings/bare-string prose excluded."""
    return _phi_tag_nodes(tree, _docstring_and_bare_string_nodes(tree))


def _log_calls(tree: ast.Module) -> list[ast.Call]:
    """Every call that looks like a logging call: ``<anything>.<log-method>(...)``."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in LOG_METHOD_NAMES
    ]


def _log_calls_carrying_phi(tree: ast.Module) -> list[ast.Call]:
    """Logging calls whose arguments (positional or keyword) name a PHI tag."""
    offenders = []
    for call in _log_calls(tree):
        args_and_kwargs: list[ast.expr] = list(call.args) + [kw.value for kw in call.keywords]
        if any(_phi_tag_nodes(arg) for arg in args_and_kwargs):
            offenders.append(call)
    return offenders


@pytest.mark.parametrize(
    "path", _package_files(), ids=lambda p: p.relative_to(PACKAGE_DIR).as_posix()
)
def test_no_phi_tag_names_outside_dicom_cmd(path: Path) -> None:
    """No module besides ``cli/dicom_cmd.py`` names a PHI-bearing DICOM tag."""
    offenders = _phi_tag_references(ast.parse(path.read_text()))
    rel = path.relative_to(PACKAGE_DIR).as_posix()
    lines = [f"line {getattr(node, 'lineno', '?')}" for node in offenders]
    assert not offenders, (
        f"{rel} references a PHI-bearing DICOM tag name or number ({', '.join(lines)}). "
        "If this is genuinely reading a patient-identifying tag value, it must "
        "stay out of logs/warnings/exception details -- route it to the "
        "command's own stdout (like cli/dicom_cmd.py does) and add it to "
        "ALLOWLIST here only if that's what this is."
    )


def test_dicom_cmd_never_logs_a_phi_tag() -> None:
    """``cli/dicom_cmd.py`` may read PHI tags for its own stdout, but never log one."""
    offenders = _log_calls_carrying_phi(ast.parse(DICOM_CMD.read_text()))
    lines = [f"line {node.lineno}" for node in offenders]
    assert not offenders, (
        f"cli/dicom_cmd.py passes a PHI-bearing DICOM tag to a logging call "
        f"({', '.join(lines)}). Reading the tag for the command's own stdout is "
        "fine; logging it is not."
    )


# -----------------------------------------------------------------------------
# Self-tests: the guard must catch what it claims to catch
# -----------------------------------------------------------------------------


def test_allowlist_references_a_real_file() -> None:
    names = {p.relative_to(PACKAGE_DIR).as_posix() for p in PACKAGE_DIR.rglob("*.py")}
    assert names >= ALLOWLIST


def test_init_files_are_not_exempt() -> None:
    """The guard must not silently skip ``__init__.py`` modules."""
    names = {p.name for p in _package_files()}
    assert "__init__.py" in names


@pytest.mark.parametrize(
    "source",
    [
        "ds.PatientName\n",
        "value = getattr(ds, 'PatientID', '')\n",
        "if hasattr(ds, 'PatientBirthDate'):\n    pass\n",
        "tags = ['InstitutionName', 'OperatorsName']\n",
        "logger.warning('leak: %s', ds.PatientSex)\n",
        "value = ds[0x00100020]\n",
        "value = ds.get(0x00100010)\n",
        "value = ds[0x0010, 0x0010]\n",
        "tag = (0x0008, 0x0050)\n",
    ],
    ids=[
        "attr-access",
        "getattr",
        "hasattr",
        "list-literal",
        "logged-attr",
        "combined-int-subscript",
        "combined-int-get",
        "group-elem-tuple-subscript",
        "group-elem-tuple-literal",
    ],
)
def test_guard_catches_phi_tag_reference_shapes(source: str) -> None:
    assert _phi_tag_references(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        '"""Docstring mentioning PatientName as an example."""\n',
        "'''Also a bare string mentioning PatientID.'''\n",
        "x = 'a value that is not a tag name'\n",
        "value = getattr(ds, 'Modality', '')\n",
        "subject_label = ds.StudyDescription\n",
        "group = 0x0010\n",  # a bare group number alone must not false-positive
        "pair = (0x0010, 0x9999)\n",  # right group, wrong element
        "value = ds[0x00080060]\n",  # Modality's combined int, not a PHI tag
    ],
    ids=[
        "module-docstring",
        "bare-string",
        "unrelated-string",
        "unrelated-getattr",
        "unrelated-attr",
        "bare-group-number",
        "mismatched-pair",
        "unrelated-combined-int",
    ],
)
def test_guard_ignores_prose_and_unrelated_code(source: str) -> None:
    assert not _phi_tag_references(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "logger.warning('leak: %s', ds.PatientName)\n",
        "logger.info('id=%s', getattr(ds, 'PatientID', ''))\n",
        "log.error('tag %s', ds[0x00100020])\n",
        "self._log_helper.debug('sex=%s', patient_sex)\n".replace("patient_sex", "ds.PatientSex"),
    ],
    ids=["warning-attr", "info-getattr", "error-numeric", "custom-log-helper"],
)
def test_log_call_guard_catches_phi_in_arguments(source: str) -> None:
    assert _log_calls_carrying_phi(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "logger.warning('file %s failed', file_path)\n",
        "logger.info('%s files processed', count)\n",
        "click.echo(ds.PatientName)\n",  # stdout, not a logging call
        "results.append(ds.PatientName)\n",  # not a logging call at all
    ],
    ids=["unrelated-warning", "unrelated-info", "stdout-not-log", "non-log-call"],
)
def test_log_call_guard_ignores_non_phi_logging(source: str) -> None:
    assert not _log_calls_carrying_phi(ast.parse(source))
