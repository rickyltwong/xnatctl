"""Stream discipline: stdout is for data, stderr for everything else.

Success lines, progress bars, prompts, and dry-run previews used to go to
stdout, with two visible consequences: piping `-o json` output interleaved
status text with the JSON, and redirecting stdout (`... > log`) killed the
live progress bar entirely, because Rich disables live display when its
console is not a tty -- even though stderr still was one.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from conftest import make_authenticated_cli, make_response

from xnatctl.core.output import (
    create_progress,
    create_spinner,
    print_info,
    print_success,
    print_table,
)


class TestOutputHelpers:
    def test_success_goes_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_success("Deleted subject SUB001")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Deleted subject SUB001" in captured.err

    def test_info_goes_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_info("3 sessions matched")

        captured = capsys.readouterr()
        assert captured.out == ""
        # Rich colourises the message; compare on the uncoloured text.
        assert "3 sessions matched" in captured.err.replace("\x1b[1;36m3\x1b[0m", "3")

    def test_progress_renders_on_stderr(self) -> None:
        """Redirecting stdout must not kill the live bar."""
        assert create_progress().console.stderr is True
        assert create_spinner().console.stderr is True

    def test_empty_table_notice_goes_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An empty `-o table` pipe stays byte-clean; scripts should use
        `-o json` for emptiness checks."""
        print_table([], columns=["id"])

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "No results" in captured.err

    def test_table_data_stays_on_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_table([{"id": "P1"}], columns=["id"])

        assert "P1" in capsys.readouterr().out


class TestCommandStreams:
    def test_json_stdout_is_exactly_one_document(self) -> None:
        harness = make_authenticated_cli(default_project="PROJ")
        harness.client.get_json.return_value = {
            "ResultSet": {
                "Result": [{"ID": "P1", "name": "One", "pi_lastname": "", "description": ""}]
            }
        }

        result = harness.invoke(["project", "list", "-o", "json"])

        assert result.exit_code == 0
        # The whole point: stdout parses as a single JSON document.
        json.loads(result.stdout)

    def test_dry_run_delete_keeps_stdout_empty(self) -> None:
        harness = make_authenticated_cli(default_project="PROJ")

        result = harness.invoke(["subject", "delete", "SUB001", "-P", "PROJ", "--dry-run"])

        assert result.exit_code == 0
        assert result.stdout == ""
        assert "Would delete subject" in result.stderr

    def test_confirmation_prompt_goes_to_stderr(self) -> None:
        """A prompt on stdout would corrupt piped output before the user even
        answers it."""
        harness = make_authenticated_cli(default_project="PROJ")
        harness.client.delete.return_value = make_response(text="OK")

        result = harness.invoke(["subject", "delete", "SUB001", "-P", "PROJ"], input="n\n")

        assert "Delete this subject" in result.stderr
        assert "Delete this subject" not in result.stdout

    def test_success_line_never_lands_in_json_stdout(self) -> None:
        harness = make_authenticated_cli(default_project="PROJ")
        harness.client.delete.return_value = make_response(text="OK")

        result = harness.invoke(["subject", "delete", "SUB001", "-P", "PROJ", "--yes"])

        assert result.exit_code == 0
        assert result.stdout == ""
        assert "Deleted subject" in result.stderr

    def test_quiet_ids_stay_on_stdout(self) -> None:
        """Quiet mode IS data: `subject list -q | xargs ...` must keep working."""
        harness = make_authenticated_cli(default_project="PROJ")
        harness.client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "XNAT_S01", "label": "SUB001", "project": "PROJ"}]}
        }

        result = harness.invoke(["subject", "list", "-P", "PROJ", "-q"])

        assert result.stdout == "SUB001\n"


def test_runner_importable() -> None:
    assert isinstance(CliRunner(), CliRunner)
