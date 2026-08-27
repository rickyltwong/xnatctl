"""Tests for the ``session normalize-labels`` CLI command."""

from __future__ import annotations

from conftest import AuthenticatedCLI


def _row(exp_id: str, label: str, *, date: str = "2024-01-01") -> dict:
    return {
        "ID": exp_id,
        "label": label,
        "subject_label": "SUB01",
        "xsiType": "xnat:mrsessiondata",
        "date": date,
        "time": "",
        "insert_date": "",
        "insert_time": "",
    }


class TestSessionNormalizeLabels:
    def test_happy_path_renames_and_prints_summary(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = [_row("E1", "OLD1")]

        result = authenticated_cli.invoke(["session", "normalize-labels", "-P", "PROJ", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/data/experiments/E1", params={"label": "SUB01_01_SE01_MR"}
        )
        assert "OLD1 -> SUB01_01_SE01_MR" in result.output
        assert "Renamed 1 experiment label" in result.output

    def test_declined_confirmation_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [_row("E1", "OLD1")]

        result = authenticated_cli.invoke(
            ["session", "normalize-labels", "-P", "PROJ"], input="n\n"
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_dry_run_previews_without_put(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [_row("E1", "OLD1")]

        result = authenticated_cli.invoke(
            ["session", "normalize-labels", "-P", "PROJ", "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_not_called()
        assert "OLD1 -> SUB01_01_SE01_MR" in result.output

    def test_no_op_when_all_labels_already_correct(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = [_row("E1", "SUB01_01_SE01_MR")]

        result = authenticated_cli.invoke(["session", "normalize-labels", "-P", "PROJ", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_not_called()
        assert "Renamed 0 experiment label" in result.output

    def test_failed_rename_exits_nonzero(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [_row("E1", "OLD1")]
        authenticated_cli.client.put.side_effect = RuntimeError("boom")

        result = authenticated_cli.invoke(["session", "normalize-labels", "-P", "PROJ", "--yes"])

        assert result.exit_code != 0
        assert "boom" in result.output

    def test_malformed_body_fails_instead_of_renamed_zero(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        # A 200 with no 'ResultSet' key must surface as an error, not read
        # as "nothing needed doing" -- before the fix this printed "Renamed
        # 0 experiment label(s), skipped 0".
        authenticated_cli.client.get_json.return_value = {"message": "plugin disabled"}

        result = authenticated_cli.invoke(["session", "normalize-labels", "-P", "PROJ", "--yes"])

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()
