from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "xnatctl"
DICOM_CMD = PACKAGE_DIR / "cli" / "dicom_cmd.py"

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

_PHI_TAG_GROUP_ELEM: frozenset[tuple[int, int]] = frozenset(
    {
        (0x0010, 0x0020),
        (0x0010, 0x0010),
        (0x0010, 0x0030),
        (0x0010, 0x0040),
        (0x0010, 0x1040),
        (0x0010, 0x2154),
        (0x0010, 0x1030),
        (0x0010, 0x1000),
        (0x0010, 0x2160),
        (0x0008, 0x0080),
        (0x0008, 0x0081),
        (0x0008, 0x0090),
        (0x0008, 0x1050),
        (0x0008, 0x1070),
        (0x0008, 0x0050),
    }
)

#: The same tags as the single 32-bit int pydicom also accepts
#: (``ds[0x00100010]`` / ``ds.get(0x00100010)``), i.e. ``(group << 16) | element``.
PHI_TAG_INTS: frozenset[int] = frozenset(
    (group << 16) | element for group, element in _PHI_TAG_GROUP_ELEM
)

ALLOWLIST = {"cli/dicom_cmd.py"}

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
    bare: set[ast.expr] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            bare.add(node.value)
    return bare


def _is_phi_tuple(node: ast.AST) -> bool:
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
    return _phi_tag_nodes(tree, _docstring_and_bare_string_nodes(tree))


def _log_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in LOG_METHOD_NAMES
    ]


def _log_calls_carrying_phi(tree: ast.Module) -> list[ast.Call]:
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
    offenders = _log_calls_carrying_phi(ast.parse(DICOM_CMD.read_text()))
    lines = [f"line {node.lineno}" for node in offenders]
    assert not offenders, (
        f"cli/dicom_cmd.py passes a PHI-bearing DICOM tag to a logging call "
        f"({', '.join(lines)}). Reading the tag for the command's own stdout is "
        "fine; logging it is not."
    )


def test_allowlist_references_a_real_file() -> None:
    names = {p.relative_to(PACKAGE_DIR).as_posix() for p in PACKAGE_DIR.rglob("*.py")}
    assert names >= ALLOWLIST


def test_init_files_are_not_exempt() -> None:
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
        "group = 0x0010\n",
        "pair = (0x0010, 0x9999)\n",
        "value = ds[0x00080060]\n",
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
        "click.echo(ds.PatientName)\n",
        "results.append(ds.PatientName)\n",
    ],
    ids=["unrelated-warning", "unrelated-info", "stdout-not-log", "non-log-call"],
)
def test_log_call_guard_ignores_non_phi_logging(source: str) -> None:
    assert not _log_calls_carrying_phi(ast.parse(source))
