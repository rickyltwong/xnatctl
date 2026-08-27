"""Tests for the ``-o tsv`` output format and the ``--no-headers`` flag."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from conftest import AuthenticatedCLI

from xnatctl.core import output
from xnatctl.core.output import OutputFormat, print_output, print_table

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_no_headers() -> Iterator[None]:
    """Restore the module-level header toggle after every test."""
    yield
    output.set_no_headers(False)


ROWS: list[dict[str, Any]] = [
    {"id": "A1", "name": "First", "size": 10},
    {"id": "B2", "name": "Second", "size": 20},
]


# =============================================================================
# OutputFormat
# =============================================================================


def test_from_string_picks_up_tsv() -> None:
    assert OutputFormat.from_string("tsv") is OutputFormat.TSV
    assert OutputFormat.from_string("TSV") is OutputFormat.TSV


# =============================================================================
# print_output TSV -- unit, via capsys
# =============================================================================


def test_tsv_list_header_plus_rows(capsys: pytest.CaptureFixture[str]) -> None:
    """Header line of raw column keys, then one tab-joined line per row."""
    print_output(ROWS, format=OutputFormat.TSV, columns=["id", "name", "size"])
    out = capsys.readouterr().out
    assert "\x1b" not in out
    lines = out.splitlines()
    assert lines == ["id\tname\tsize", "A1\tFirst\t10", "B2\tSecond\t20"]


def test_tsv_header_uses_raw_keys_not_display_labels(capsys: pytest.CaptureFixture[str]) -> None:
    """column_labels is table presentation; TSV headers stay --columns/JSON names."""
    print_output(
        ROWS,
        format=OutputFormat.TSV,
        columns=["id", "name"],
        column_labels={"id": "ID", "name": "Fancy Name"},
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "id\tname"


def test_tsv_sanitizes_embedded_tabs_and_newlines(capsys: pytest.CaptureFixture[str]) -> None:
    """A value with tabs/newlines stays one record on one line."""
    rows = [{"id": "A1", "desc": "has\ttab and\nnewline and\r\ncrlf"}]
    print_output(rows, format=OutputFormat.TSV, columns=["id", "desc"])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert lines[1] == "A1\thas tab and newline and crlf"
    # Exactly one tab: the field boundary.
    assert lines[1].count("\t") == 1


def test_tsv_strips_ansi_and_control_bytes(capsys: pytest.CaptureFixture[str]) -> None:
    """A value carrying ANSI/control escapes never reaches the TSV stream raw."""
    rows = [{"id": "A1", "note": "\x1b[31mred\x1b[0m alert\x07\x01"}]
    print_output(rows, format=OutputFormat.TSV, columns=["id", "note"])
    lines = capsys.readouterr().out.splitlines()
    assert "\x1b" not in lines[1]
    assert "\x07" not in lines[1]
    assert "\x01" not in lines[1]
    # ESC/BEL/SOH bytes are stripped outright; the surviving printable
    # characters of the escape sequence ("[31m", "[0m") are left alone --
    # only the actual control bytes are control-byte-shaped.
    assert lines[1] == "A1\t[31mred[0m alert"


def test_tsv_list_without_columns_derives_union_first_seen(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No columns: union of row keys in first-seen order (table would fall back to JSON)."""
    rows = [{"id": "A1", "name": "First"}, {"id": "B2", "extra": "x"}]
    print_output(rows, format=OutputFormat.TSV)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "id\tname\textra"
    assert lines[1] == "A1\tFirst\t"
    assert lines[2] == "B2\t\tx"


def test_tsv_single_dict_is_header_plus_one_row(capsys: pytest.CaptureFixture[str]) -> None:
    """A single record keeps the same shape as a list: header line, then the record."""
    print_output({"id": "A1", "name": "First"}, format=OutputFormat.TSV)
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["id\tname", "A1\tFirst"]


def test_tsv_scalar_value_conversions(capsys: pytest.CaptureFixture[str]) -> None:
    """None empty, bools JSON-style true/false, nested values compact JSON."""
    rows = [{"a": None, "b": True, "c": False, "d": {"k": 1}, "e": [1, 2]}]
    print_output(rows, format=OutputFormat.TSV, columns=["a", "b", "c", "d", "e"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == '\ttrue\tfalse\t{"k": 1}\t[1, 2]'


def test_tsv_empty_list_prints_header_only(capsys: pytest.CaptureFixture[str]) -> None:
    """Zero rows: header alone on stdout, nothing on stderr."""
    print_output([], format=OutputFormat.TSV, columns=["id", "name"])
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["id\tname"]
    assert captured.err == ""


def test_tsv_empty_list_without_columns_prints_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_output([], format=OutputFormat.TSV)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_tsv_list_of_scalars_one_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    print_output(["x", "y"], format=OutputFormat.TSV)
    assert capsys.readouterr().out.splitlines() == ["x", "y"]


def test_tsv_bare_scalar(capsys: pytest.CaptureFixture[str]) -> None:
    print_output("plain\tvalue", format=OutputFormat.TSV)
    assert capsys.readouterr().out == "plain value\n"


def test_quiet_beats_tsv(capsys: pytest.CaptureFixture[str]) -> None:
    """quiet=True wins over format=TSV, exactly as it does over json/table."""
    print_output(ROWS, format=OutputFormat.TSV, columns=["id", "name"], quiet=True)
    assert capsys.readouterr().out.splitlines() == ["A1", "B2"]


def test_tsv_no_ansi_even_with_forced_terminal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """TSV never routes through Rich, so a color-forcing terminal changes nothing."""
    monkeypatch.setattr(output.console, "no_color", False)
    monkeypatch.setattr(output.console, "_force_terminal", True)
    print_output(ROWS, format=OutputFormat.TSV, columns=["id", "name"])
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert out.splitlines()[0] == "id\tname"


# =============================================================================
# --no-headers -- unit
# =============================================================================


def test_no_headers_drops_tsv_header(capsys: pytest.CaptureFixture[str]) -> None:
    output.set_no_headers(True)
    print_output(ROWS, format=OutputFormat.TSV, columns=["id", "name"])
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["A1\tFirst", "B2\tSecond"]


def test_no_headers_empty_list_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    output.set_no_headers(True)
    print_output([], format=OutputFormat.TSV, columns=["id", "name"])
    assert capsys.readouterr().out == ""


def test_no_headers_drops_table_header(capsys: pytest.CaptureFixture[str]) -> None:
    print_table(ROWS, ["id", "name"])
    with_header = capsys.readouterr().out
    assert "Name" in with_header

    output.set_no_headers(True)
    print_table(ROWS, ["id", "name"])
    without_header = capsys.readouterr().out
    assert "Name" not in without_header
    assert "First" in without_header


# =============================================================================
# CLI integration -- project list through CliRunner
# =============================================================================

_PROJECT_RESULTSET = {
    "ResultSet": {
        "Result": [
            {"ID": "PROJ1", "name": "Project One", "pi_lastname": "Smith", "description": "d1"},
            {"ID": "PROJ2", "name": "Project Two", "pi_lastname": "Jones", "description": "d2"},
        ]
    }
}


def _wire_project_list(harness: AuthenticatedCLI) -> None:
    harness.client.get_json.return_value = _PROJECT_RESULTSET


def test_project_list_tsv_is_cut_compatible(authenticated_cli: AuthenticatedCLI) -> None:
    _wire_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["project", "list", "-o", "tsv"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "id\tname\tpi\tdescription"
    assert lines[1].split("\t") == ["PROJ1", "Project One", "Smith", "d1"]
    assert lines[2].split("\t")[0] == "PROJ2"
    assert "\x1b" not in result.stdout


def test_project_list_tsv_respects_columns(authenticated_cli: AuthenticatedCLI) -> None:
    _wire_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["project", "list", "-o", "tsv", "--columns", "id,pi"])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["id\tpi", "PROJ1\tSmith", "PROJ2\tJones"]


def test_project_list_tsv_no_headers_subcommand(authenticated_cli: AuthenticatedCLI) -> None:
    _wire_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["project", "list", "-o", "tsv", "--no-headers"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].split("\t")[0] == "PROJ1"
    assert len(lines) == 2


def test_quiet_beats_tsv_cli(authenticated_cli: AuthenticatedCLI) -> None:
    _wire_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["project", "list", "-o", "tsv", "-q"])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["PROJ1", "PROJ2"]


def test_root_no_headers_inherited_by_subcommand(authenticated_cli: AuthenticatedCLI) -> None:
    """Root --no-headers reaches a subcommand that did not set it."""
    _wire_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["--no-headers", "project", "list", "-o", "tsv"])
    assert result.exit_code == 0
    assert result.stdout.splitlines()[0].split("\t")[0] == "PROJ1"


def test_root_tsv_inherited_by_subcommand(authenticated_cli: AuthenticatedCLI) -> None:
    """Root -o tsv reaches a subcommand that did not set an output format."""
    _wire_project_list(authenticated_cli)
    result = authenticated_cli.invoke(["-o", "tsv", "project", "list"])
    assert result.exit_code == 0
    assert result.stdout.splitlines()[0] == "id\tname\tpi\tdescription"


def test_no_headers_does_not_leak_across_invocations(
    authenticated_cli: AuthenticatedCLI,
) -> None:
    """A later invocation without the flag gets its header back (state resets)."""
    _wire_project_list(authenticated_cli)
    first = authenticated_cli.invoke(["--no-headers", "project", "list", "-o", "tsv"])
    assert first.exit_code == 0
    assert first.stdout.splitlines()[0].split("\t")[0] == "PROJ1"

    second = authenticated_cli.invoke(["project", "list", "-o", "tsv"])
    assert second.exit_code == 0
    assert second.stdout.splitlines()[0] == "id\tname\tpi\tdescription"


def test_no_headers_table_via_cli(authenticated_cli: AuthenticatedCLI) -> None:
    """--no-headers also drops the table's header row."""
    _wire_project_list(authenticated_cli)
    with_header = authenticated_cli.invoke(["project", "list"])
    assert with_header.exit_code == 0
    assert "Name" in with_header.stdout

    without_header = authenticated_cli.invoke(["project", "list", "--no-headers"])
    assert without_header.exit_code == 0
    assert "Name" not in without_header.stdout
    assert "PROJ1" in without_header.stdout


def test_no_headers_silently_ignored_under_json(authenticated_cli: AuthenticatedCLI) -> None:
    """JSON output is unchanged by --no-headers (no header exists to drop)."""
    import json as jsonlib

    _wire_project_list(authenticated_cli)
    plain = authenticated_cli.invoke(["project", "list", "-o", "json"])
    flagged = authenticated_cli.invoke(["project", "list", "-o", "json", "--no-headers"])
    assert plain.exit_code == 0
    assert flagged.exit_code == 0
    assert jsonlib.loads(plain.stdout) == jsonlib.loads(flagged.stdout)
