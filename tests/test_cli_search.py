"""Tests for xnatctl CLI `search` commands."""

from __future__ import annotations

from conftest import AuthenticatedCLI, make_response

from xnatctl.core.exceptions import ResourceNotFoundError

SAMPLE_ROW = {
    "id": "test_search",
    "root_element_name": "xnat:projectData",
    "brief_description": "xnatctl test search",
    "secure": "1",
    "users": "{admin}",
}

SAMPLE_DEFINITION_XML = (
    '<xdat:bundle ID="test_search" xmlns:xdat="http://nrg.wustl.edu/security">'
    "<xdat:root_element_name>xnat:projectData</xdat:root_element_name>"
    "</xdat:bundle>"
)


class TestSearchList:
    """Tests for `search list`."""

    def test_list_requests_expected_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = {
            "ResultSet": {"Result": [SAMPLE_ROW], "title": "Stored Searches"}
        }

        result = authenticated_cli.invoke(["search", "list"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/data/search/saved")
        assert "test_search" in result.output

    def test_list_quiet(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = {
            "ResultSet": {"Result": [SAMPLE_ROW], "title": "Stored Searches"}
        }

        result = authenticated_cli.invoke(["search", "list", "-q"])

        assert result.exit_code == 0
        assert result.output.strip() == "test_search"

    def test_list_malformed_body_fails(self, authenticated_cli: AuthenticatedCLI) -> None:
        # A 200 with no 'ResultSet' key at all must surface as an error, not
        # as an empty listing.
        authenticated_cli.client.get_json.return_value = {"message": "plugin disabled"}

        result = authenticated_cli.invoke(["search", "list"])

        assert result.exit_code != 0


class TestSearchShow:
    """Tests for `search show`."""

    def test_show_requests_xml_format(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.return_value = make_response(
            text=SAMPLE_DEFINITION_XML, content_type="application/xml"
        )

        result = authenticated_cli.invoke(["search", "show", "test_search"])

        assert result.exit_code == 0
        authenticated_cli.client.get.assert_called_once_with(
            "/data/search/saved/test_search", params={"format": "xml"}
        )
        assert "root_element_name" in result.output


class TestSearchRun:
    """Tests for `search run`."""

    def test_run_prints_dynamic_columns(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "PROJ1", "name": "Project One"}]}
        }

        result = authenticated_cli.invoke(["search", "run", "test_search"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with(
            "/data/search/saved/test_search/results"
        )
        assert "PROJ1" in result.output

    def test_run_json(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "PROJ1"}]}
        }

        result = authenticated_cli.invoke(["search", "run", "test_search", "-o", "json"])

        assert result.exit_code == 0
        assert "PROJ1" in result.output

    def test_run_empty_result_set(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = {"ResultSet": {"Result": []}}

        result = authenticated_cli.invoke(["search", "run", "test_search"])

        assert result.exit_code == 0


class TestSearchDelete:
    """Tests for `search delete` -- happy path, declined confirmation, dry-run."""

    def test_delete(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.return_value = make_response(
            text=SAMPLE_DEFINITION_XML, content_type="application/xml"
        )
        authenticated_cli.client.delete.return_value = make_response(
            text="", content_type="text/plain"
        )

        result = authenticated_cli.invoke(["search", "delete", "test_search", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.delete.assert_called_once_with("/data/search/saved/test_search")

    def test_delete_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(["search", "delete", "test_search"], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.delete.assert_not_called()

    def test_delete_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.return_value = make_response(
            text=SAMPLE_DEFINITION_XML, content_type="application/xml"
        )

        result = authenticated_cli.invoke(["search", "delete", "test_search", "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.delete.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_delete_unknown_id_fails_no_delete(self, authenticated_cli: AuthenticatedCLI) -> None:
        # DELETE itself would answer 200 even for an unknown id (idempotent-
        # succeeds), so only the get_definition() preflight can catch a
        # typo. Before the fix, this printed "Deleted saved search bad_id".
        authenticated_cli.client.get.side_effect = ResourceNotFoundError("saved search", "bad_id")

        result = authenticated_cli.invoke(["search", "delete", "bad_id", "--yes"])

        assert result.exit_code != 0
        authenticated_cli.client.delete.assert_not_called()

    def test_delete_dry_run_unknown_id_fails(self, authenticated_cli: AuthenticatedCLI) -> None:
        # --dry-run runs every validation and skips only the mutation, so it
        # must run the same preflight and fail the same way.
        authenticated_cli.client.get.side_effect = ResourceNotFoundError("saved search", "bad_id")

        result = authenticated_cli.invoke(["search", "delete", "bad_id", "--dry-run"])

        assert result.exit_code != 0
        authenticated_cli.client.delete.assert_not_called()
