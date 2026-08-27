"""Tests for xnatctl CLI `anon` commands."""

from __future__ import annotations

from conftest import AuthenticatedCLI, make_response

PROJECT_EXISTS_RESPONSE = {"ResultSet": {"Result": [{"ID": "PROJ01"}]}}


class TestAnonShow:
    """Tests for `anon show`."""

    def test_show_site(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.return_value = make_response(
            text='version "6.1"', content_type="text/plain"
        )
        authenticated_cli.client.get_json.return_value = True

        result = authenticated_cli.invoke(["anon", "show"])

        assert result.exit_code == 0
        authenticated_cli.client.get.assert_called_once_with("/xapi/anonymize/site")
        assert "6.1" in result.output

    def test_show_project(self, authenticated_cli: AuthenticatedCLI) -> None:
        # get_project_script() checks project existence then reads the script;
        # project_enabled() independently re-checks project existence -- four
        # client.get() calls total for this command in the "has a script" path.
        authenticated_cli.client.get.side_effect = [
            make_response(PROJECT_EXISTS_RESPONSE),
            make_response(text='version "6.1"', content_type="text/plain"),
            make_response(PROJECT_EXISTS_RESPONSE),
        ]
        authenticated_cli.client.get_json.return_value = True

        result = authenticated_cli.invoke(["anon", "show", "-P", "PROJ01"])

        assert result.exit_code == 0
        assert "6.1" in result.output

    def test_show_project_no_override_warns(self, authenticated_cli: AuthenticatedCLI) -> None:
        from xnatctl.core.exceptions import ResourceNotFoundError

        authenticated_cli.client.get.side_effect = [
            make_response(PROJECT_EXISTS_RESPONSE),  # project exists (get_project_script)
            make_response(text="", content_type="text/plain", status_code=204),  # unset
            ResourceNotFoundError("resource", "path"),  # no config row -- never set
            make_response(PROJECT_EXISTS_RESPONSE),  # project exists (project_enabled)
        ]
        authenticated_cli.client.get_json.return_value = False

        result = authenticated_cli.invoke(["anon", "show", "-P", "PROJ01"])

        assert result.exit_code == 0
        assert "inherits the site-wide" in result.output


class TestAnonSet:
    """Tests for `anon set` -- happy path, declined confirmation, dry-run diff."""

    def test_set_site_from_file(self, authenticated_cli: AuthenticatedCLI, tmp_path) -> None:
        script_file = tmp_path / "script.dicomedit"
        script_file.write_text('version "6.1"')
        authenticated_cli.client.get.return_value = make_response(
            text="", content_type="text/plain"
        )
        authenticated_cli.client.put.return_value = make_response(
            text="", content_type="text/plain"
        )

        result = authenticated_cli.invoke(["anon", "set", str(script_file), "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/xapi/anonymize/site",
            content=b'version "6.1"',
            headers={"Content-Type": "text/plain"},
        )

    def test_set_from_stdin(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.return_value = make_response(
            text="", content_type="text/plain"
        )
        authenticated_cli.client.put.return_value = make_response(
            text="", content_type="text/plain"
        )

        result = authenticated_cli.invoke(["anon", "set", "-", "--yes"], input='version "6.1"')

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once()

    def test_set_stdin_without_yes_or_dry_run_rejected(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        result = authenticated_cli.invoke(["anon", "set", "-"], input='version "6.1"')

        assert result.exit_code != 0
        assert "--yes or --dry-run" in result.output
        authenticated_cli.client.put.assert_not_called()

    def test_set_prompt_abort_no_mutation(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        script_file = tmp_path / "script.dicomedit"
        script_file.write_text('version "6.1"')

        result = authenticated_cli.invoke(["anon", "set", str(script_file)], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_dry_run_prints_diff_not_generic_message(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        script_file = tmp_path / "script.dicomedit"
        script_file.write_text('version "6.1"\n(0010,0010) := "NEW"')
        authenticated_cli.client.get.return_value = make_response(
            text='version "6.1"\n(0010,0010) := subject', content_type="text/plain"
        )

        result = authenticated_cli.invoke(["anon", "set", str(script_file), "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_not_called()
        assert "---" in result.output
        assert "+++" in result.output
        assert '(0010,0010) := "NEW"' in result.output

    def test_set_project_dry_run_checks_project_existence(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        from xnatctl.core.exceptions import ResourceNotFoundError

        script_file = tmp_path / "script.dicomedit"
        script_file.write_text('version "6.1"')
        authenticated_cli.client.get.side_effect = ResourceNotFoundError("project", "NOPE")

        result = authenticated_cli.invoke(
            ["anon", "set", str(script_file), "-P", "NOPE", "--dry-run"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()


class TestAnonEnableDisable:
    """Tests for `anon enable`/`anon disable`."""

    def test_enable_site(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.put.return_value = make_response(
            text="", content_type="application/json"
        )

        result = authenticated_cli.invoke(["anon", "enable", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/xapi/anonymize/site/enabled",
            params={"enable": "true"},
            content=b"null",
            headers={"Content-Type": "application/json"},
        )

    def test_disable_project(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.side_effect = [
            make_response(PROJECT_EXISTS_RESPONSE),
            make_response({"ResultSet": {"Result": [{"status": "enabled"}]}}),
        ]
        authenticated_cli.client.put.return_value = make_response(
            text="", content_type="application/json"
        )

        result = authenticated_cli.invoke(["anon", "disable", "-P", "PROJ01", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/xapi/anonymize/projects/PROJ01/enabled",
            params={"enable": "false"},
            content=b"null",
            headers={"Content-Type": "application/json"},
        )

    def test_enable_project_without_script_rejected(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        from xnatctl.core.exceptions import ResourceNotFoundError

        authenticated_cli.client.get.side_effect = [
            make_response(PROJECT_EXISTS_RESPONSE),
            ResourceNotFoundError("resource", "path"),
        ]

        result = authenticated_cli.invoke(["anon", "enable", "-P", "PROJ01", "--yes"])

        assert result.exit_code != 0
        assert "no anonymization script set" in result.output
        authenticated_cli.client.put.assert_not_called()

    def test_enable_project_dry_run_checks_scope_no_put(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get.side_effect = [
            make_response(PROJECT_EXISTS_RESPONSE),
            make_response({"ResultSet": {"Result": [{"status": "disabled"}]}}),
        ]

        result = authenticated_cli.invoke(["anon", "enable", "-P", "PROJ01", "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_not_called()
        assert "Would enable" in result.output

    def test_disable_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(["anon", "disable"], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()
