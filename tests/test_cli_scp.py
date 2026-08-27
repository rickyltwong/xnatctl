"""Tests for xnatctl CLI `scp` commands."""

from __future__ import annotations

from conftest import AuthenticatedCLI, make_response

SAMPLE_SCP = {
    "identifier": "dicomObjectIdentifier",
    "label": "XNAT:8104",
    "port": 8104,
    "aeTitle": "XNAT",
    "enabled": True,
    "id": 1,
    "disabled": 0,
}


class TestScpList:
    """Tests for `scp list`."""

    def test_list_requests_expected_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_SCP]

        result = authenticated_cli.invoke(["scp", "list"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/dicomscp")
        assert "XNAT" in result.output

    def test_list_quiet(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_SCP]

        result = authenticated_cli.invoke(["scp", "list", "-q"])

        assert result.exit_code == 0
        assert result.output.strip() == "1"


class TestScpShow:
    """Tests for `scp show`."""

    def test_show(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_SCP

        result = authenticated_cli.invoke(["scp", "show", "1"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/dicomscp/1")
        assert "XNAT" in result.output


class TestScpCreate:
    """Tests for `scp create` -- happy path, declined confirmation, dry-run, port validation."""

    def test_create(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = [
            {"dicomObjectIdentifier": "Default..."},  # identifier lookup
            [],  # existing receivers -- no port collision
        ]
        created = {**SAMPLE_SCP, "id": 2, "aeTitle": "TESTSCP", "port": 18104}
        authenticated_cli.client.post.return_value = make_response(created)

        result = authenticated_cli.invoke(
            ["scp", "create", "--ae-title", "TESTSCP", "--port", "18104", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/dicomscp",
            json={"aeTitle": "TESTSCP", "port": 18104, "identifier": "dicomObjectIdentifier"},
        )

    def test_create_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = [
            {"dicomObjectIdentifier": "Default..."},
            [],
        ]

        result = authenticated_cli.invoke(
            ["scp", "create", "--ae-title", "TESTSCP", "--port", "18104", "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_create_duplicate_port_rejected_no_post(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """The server accepts a duplicate port silently (verified live) --
        this must be rejected client-side before any request is sent, since
        two receivers cannot independently bind one listening socket.
        """
        authenticated_cli.client.get_json.side_effect = [
            {"dicomObjectIdentifier": "Default..."},
            [SAMPLE_SCP],  # existing receiver already bound to port 8104
        ]

        result = authenticated_cli.invoke(
            ["scp", "create", "--ae-title", "TESTSCP", "--port", "8104", "--yes"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_create_free_port_still_succeeds(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = [
            {"dicomObjectIdentifier": "Default..."},
            [SAMPLE_SCP],  # existing receiver on a different port (8104)
        ]
        created = {**SAMPLE_SCP, "id": 2, "aeTitle": "TESTSCP", "port": 18104}
        authenticated_cli.client.post.return_value = make_response(created)

        result = authenticated_cli.invoke(
            ["scp", "create", "--ae-title", "TESTSCP", "--port", "18104", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once()

    def test_create_dry_run_duplicate_port_also_rejected(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Dry run runs every validation, skipping only the mutation -- the
        port-collision check is not an exception to that.
        """
        authenticated_cli.client.get_json.side_effect = [
            {"dicomObjectIdentifier": "Default..."},
            [SAMPLE_SCP],
        ]

        result = authenticated_cli.invoke(
            ["scp", "create", "--ae-title", "TESTSCP", "--port", "8104", "--dry-run"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_create_invalid_port_rejected_client_side(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """The server accepts port 0 silently (verified live) -- this must be
        rejected before any request is sent.
        """
        result = authenticated_cli.invoke(
            ["scp", "create", "--ae-title", "TESTSCP", "--port", "0", "--yes"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.get_json.assert_not_called()
        authenticated_cli.client.post.assert_not_called()

    def test_create_invalid_ae_title_rejected(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["scp", "create", "--ae-title", "", "--port", "18104", "--yes"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_create_unknown_identifier_rejected(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = {"dicomObjectIdentifier": "Default..."}

        result = authenticated_cli.invoke(
            [
                "scp",
                "create",
                "--ae-title",
                "TESTSCP",
                "--port",
                "18104",
                "--identifier",
                "bogus",
                "--yes",
            ]
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_create_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = {"dicomObjectIdentifier": "Default..."}

        result = authenticated_cli.invoke(
            ["scp", "create", "--ae-title", "TESTSCP", "--port", "18104"], input="n\n"
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()


class TestScpDelete:
    """Tests for `scp delete` -- happy path, declined confirmation, dry-run."""

    def test_delete(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.delete.return_value = make_response(
            text="", content_type="text/plain"
        )

        result = authenticated_cli.invoke(["scp", "delete", "2", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.delete.assert_called_once_with("/xapi/dicomscp/2")

    def test_delete_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(["scp", "delete", "2"], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.delete.assert_not_called()

    def test_delete_dry_run_checks_existence_no_delete(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_SCP

        result = authenticated_cli.invoke(["scp", "delete", "1", "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/dicomscp/1")
        authenticated_cli.client.delete.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_delete_dry_run_unknown_id_refused(self, authenticated_cli: AuthenticatedCLI) -> None:
        from xnatctl.core.exceptions import ResourceNotFoundError

        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError(
            "resource", "/xapi/dicomscp/999"
        )

        result = authenticated_cli.invoke(["scp", "delete", "999", "--dry-run"])

        assert result.exit_code != 0
        authenticated_cli.client.delete.assert_not_called()


class TestScpEnableDisable:
    """Tests for `scp enable`/`scp disable`."""

    def test_enable(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_SCP
        authenticated_cli.client.put.return_value = make_response({**SAMPLE_SCP, "enabled": True})

        result = authenticated_cli.invoke(["scp", "enable", "1", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/xapi/dicomscp/1", json={"enabled": True}
        )

    def test_disable(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_SCP
        authenticated_cli.client.put.return_value = make_response({**SAMPLE_SCP, "enabled": False})

        result = authenticated_cli.invoke(["scp", "disable", "1", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/xapi/dicomscp/1", json={"enabled": False}
        )

    def test_enable_dry_run_checks_existence_no_put(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_SCP

        result = authenticated_cli.invoke(["scp", "enable", "1", "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_disable_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(["scp", "disable", "1"], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()
