from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from conftest import make_response

from xnatctl.core.exceptions import (
    ClientRequestError,
    InputValidationError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from xnatctl.models.resource import Resource
from xnatctl.models.scan import Scan
from xnatctl.models.session import Session
from xnatctl.services.sessions import SessionService


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> SessionService:
    return SessionService(mock_client)


SAMPLE_SESSION = {
    "ID": "XNAT_E00001",
    "label": "MR001",
    "project": "PROJ01",
    "subject_ID": "XNAT_S00001",
    "URI": "/data/experiments/XNAT_E00001",
}


class TestSessionList:
    def test_list_all(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        result = service.list()

        assert len(result) == 1
        assert isinstance(result[0], Session)
        assert result[0].id == "XNAT_E00001"
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/data/experiments"

    def test_list_by_project(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list(project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/experiments" in call_path

    def test_list_by_project_and_subject(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list(project="PROJ01", subject="SUB01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/subjects/SUB01/experiments" in call_path

    def test_list_with_modality(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list(modality="MR")

        params = mock_client.get.call_args[1]["params"]
        assert params["xsiType"] == "xnat:mrSessionData"

    def test_list_with_modality_oct_resolves_to_opt_xsitype(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list(modality="OCT")

        params = mock_client.get.call_args[1]["params"]
        assert params["xsiType"] == "xnat:optSessionData"

    def test_list_with_limit(self, service: SessionService, mock_client: MagicMock) -> None:
        rows = [{**SAMPLE_SESSION, "ID": f"E{i:05d}"} for i in range(10)]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.list(limit=3)

        assert len(result) == 3

    def test_list_with_columns(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list(columns=["ID", "label", "date"])

        params = mock_client.get.call_args[1]["params"]
        assert params["columns"] == "ID,label,date"


class TestSessionGet:
    def test_get_items_response(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response(
            {
                "items": [
                    {
                        "data_fields": {
                            "ID": "XNAT_E00001",
                            "label": "MR001",
                            "project": "PROJ01",
                            "subject_ID": "XNAT_S00001",
                        },
                        "meta": {"xsi:type": "xnat:mrSessionData"},
                    }
                ]
            }
        )

        result = service.get("XNAT_E00001")

        assert isinstance(result, Session)
        assert result.label == "MR001"
        assert result.xsi_type == "xnat:mrSessionData"

    def test_get_by_id(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        result = service.get("XNAT_E00001")

        assert isinstance(result, Session)
        assert result.label == "MR001"

    def test_get_with_project(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        service.get("MR001", project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/experiments/MR001" in call_path

    def test_get_not_found(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        with pytest.raises(ResourceNotFoundError):
            service.get("MISSING")

    def test_get_not_found_dispatches_on_type_not_message_text(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get.side_effect = ResourceNotFoundError("resource", "no such thing here")

        with pytest.raises(ResourceNotFoundError) as excinfo:
            service.get("MISSING")

        assert excinfo.value.details.get("resource_type") == "session"
        assert excinfo.value.details.get("resource_id") == "MISSING"


class TestSessionCreate:
    def test_create_default_xsi_type(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.put.return_value = make_response("", content_type="text/plain")
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        result = service.create("PROJ01", "SUB01", "MR001")

        assert isinstance(result, Session)
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["xsiType"] == "xnat:mrSessionData"

    def test_create_with_modality_override(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.put.return_value = make_response("", content_type="text/plain")
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        service.create("PROJ01", "SUB01", "PET001", modality="PET")

        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["xsiType"] == "xnat:petSessionData"

    def test_create_with_optional_params(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.put.return_value = make_response("", content_type="text/plain")
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        service.create("PROJ01", "SUB01", "MR001", date="2024-01-15", visit_id="V1")

        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["date"] == "2024-01-15"
        assert put_params["visit_id"] == "V1"


class TestSessionDelete:
    def test_delete_with_project(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.delete.return_value = make_response("")

        assert service.delete("MR001", project="PROJ01") is True
        call_path = mock_client.delete.call_args[0][0]
        assert "/data/projects/PROJ01/experiments/MR001" in call_path

    def test_delete_without_project(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.delete.return_value = make_response("")

        service.delete("XNAT_E00001")

        call_path = mock_client.delete.call_args[0][0]
        assert "/data/experiments/XNAT_E00001" in call_path


class TestSessionGetScans:
    def test_get_scans(self, service: SessionService, mock_client: MagicMock) -> None:
        rows = [{"ID": "1", "type": "T1w", "series_description": "t1_mprage"}]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.get_scans("XNAT_E00001")

        assert len(result) == 1
        assert isinstance(result[0], Scan)
        assert result[0].id == "1"
        assert result[0].type == "T1w"
        assert result[0].series_description == "t1_mprage"
        assert result[0].session_id == "XNAT_E00001"

    def test_get_scans_accepts_bare_array_response(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        rows = [{"ID": "1", "type": "T1w", "series_description": "t1_mprage"}]
        mock_client.get.return_value = make_response(rows)

        result = service.get_scans("XNAT_E00001")

        assert len(result) == 1
        assert isinstance(result[0], Scan)
        assert result[0].id == "1"


class TestSessionGetResources:
    def test_get_resources(self, service: SessionService, mock_client: MagicMock) -> None:
        rows = [
            {
                "xnat_abstractresource_id": "42",
                "label": "DICOM",
                "file_count": "200",
                "file_size": "1048576",
                "format": "DICOM",
            }
        ]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.get_resources("XNAT_E00001", project="PROJ01")

        assert len(result) == 1
        assert isinstance(result[0], Resource)
        assert result[0].label == "DICOM"
        assert result[0].file_count == 200
        assert result[0].file_size == 1048576
        assert result[0].session_id == "XNAT_E00001"
        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/experiments/XNAT_E00001/resources" in call_path

    def test_get_resources_accepts_bare_array_response(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        rows = [
            {
                "xnat_abstractresource_id": "42",
                "label": "DICOM",
                "file_count": "200",
                "file_size": "1048576",
                "format": "DICOM",
            }
        ]
        mock_client.get.return_value = make_response(rows)

        result = service.get_resources("XNAT_E00001", project="PROJ01")

        assert len(result) == 1
        assert isinstance(result[0], Resource)
        assert result[0].label == "DICOM"


class TestSessionSetField:
    def test_set_field(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.put.return_value = make_response("", content_type="text/plain")

        assert service.set_field("XNAT_E00001", "note", "test note") is True
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["note"] == "test note"


class TestSessionShare:
    def test_share(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.put.return_value = make_response("", content_type="text/plain")

        assert service.share("XNAT_E00001", "PROJ02", label="MR_SHARED") is True
        call_path = mock_client.put.call_args[0][0]
        assert "/data/experiments/XNAT_E00001/projects/PROJ02" in call_path
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["label"] == "MR_SHARED"

    def test_share_primary(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.put.return_value = make_response("", content_type="text/plain")

        service.share("XNAT_E00001", "PROJ02", primary=True)

        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["primary"] == "true"


class TestSessionRawAccessors:
    def test_list_project_experiment_rows_no_subject(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {"ResultSet": {"Result": [{"ID": "EXP01"}]}}

        rows = service.list_project_experiment_rows("PROJ01")

        assert rows == [{"ID": "EXP01"}]
        mock_client.get_json.assert_called_once_with(
            "/data/projects/PROJ01/experiments",
            params={"columns": "ID,label,subject_label,date,xsiType"},
        )

    def test_list_project_experiment_rows_with_subject(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        service.list_project_experiment_rows("PROJ01", subject="SUBJ01")

        params = mock_client.get_json.call_args[1]["params"]
        assert params["subject_label"] == "SUBJ01"

    def test_list_project_experiment_rows_missing_resultset(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {}

        assert service.list_project_experiment_rows("PROJ01") == []

    def test_scan_rows_uses_flat_experiment_url(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {"ResultSet": {"Result": [{"ID": "1"}]}}

        rows = service.scan_rows("EXP01")

        assert rows == [{"ID": "1"}]
        mock_client.get_json.assert_called_once_with("/data/experiments/EXP01/scans")

    def test_experiment_resource_rows_subject_scoped(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {"ResultSet": {"Result": [{"label": "MISC"}]}}

        rows = service.experiment_resource_rows("EXP01", project="PROJ01", subject="SUBJ01")

        assert rows == [{"label": "MISC"}]
        mock_client.get_json.assert_called_once_with(
            "/data/projects/PROJ01/subjects/SUBJ01/experiments/EXP01/resources"
        )


class TestListSessions:
    ROWS = {
        "ResultSet": {
            "Result": [
                {
                    "ID": "XNAT_E1",
                    "label": "MR001",
                    "subject_label": "SUB1",
                    "date": "2026-01-01",
                    "xsiType": "xnat:mrSessionData",
                },
                {
                    "ID": "XNAT_E2",
                    "label": "PET001",
                    "subject_label": "SUB2",
                    "date": "2026-01-02",
                    "xsiType": "xnat:petSessionData",
                },
                {
                    "ID": "XNAT_E3",
                    "label": "OTHER",
                    "subject_label": "SUB3",
                    "date": "",
                    "xsiType": "xnat:otherData",
                },
            ]
        }
    }

    def test_classifies_each_row_and_maps_fields(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = self.ROWS

        rows = service.list_sessions("PROJ01")

        assert rows == [
            {
                "id": "XNAT_E1",
                "label": "MR001",
                "subject": "SUB1",
                "date": "2026-01-01",
                "modality": "MR",
            },
            {
                "id": "XNAT_E2",
                "label": "PET001",
                "subject": "SUB2",
                "date": "2026-01-02",
                "modality": "PET",
            },
            {
                "id": "XNAT_E3",
                "label": "OTHER",
                "subject": "SUB3",
                "date": "",
                "modality": "?",
            },
        ]
        mock_client.get_json.assert_called_once_with(
            "/data/projects/PROJ01/experiments",
            params={"columns": "ID,label,subject_label,date,xsiType"},
        )

    def test_modality_filter_drops_non_matching_rows(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = self.ROWS

        rows = service.list_sessions("PROJ01", modality="MR")

        assert [r["id"] for r in rows] == ["XNAT_E1"]

    def test_modality_filter_supports_arbitrary_modalities(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    *self.ROWS["ResultSet"]["Result"],
                    {
                        "ID": "XNAT_E4",
                        "label": "US001",
                        "subject_label": "SUB4",
                        "date": "2026-01-04",
                        "xsiType": "xnat:usSessionData",
                    },
                ]
            }
        }

        rows = service.list_sessions("PROJ01", modality="US")

        assert [r["id"] for r in rows] == ["XNAT_E4"]

    def test_modality_filter_is_case_insensitive(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = self.ROWS

        rows = service.list_sessions("PROJ01", modality="mr")

        assert [r["id"] for r in rows] == ["XNAT_E1"]

    def test_modality_oct_matches_the_real_opt_sessiondata_xsitype(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    *self.ROWS["ResultSet"]["Result"],
                    {
                        "ID": "XNAT_E5",
                        "label": "OCT001",
                        "subject_label": "SUB5",
                        "date": "2026-01-05",
                        "xsiType": "xnat:optSessionData",
                    },
                ]
            }
        }

        rows = service.list_sessions("PROJ01", modality="OCT")

        assert [r["id"] for r in rows] == ["XNAT_E5"]

    def test_modality_opt_also_matches_the_same_sessiondata(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E5",
                        "label": "OCT001",
                        "subject_label": "SUB5",
                        "date": "2026-01-05",
                        "xsiType": "xnat:optSessionData",
                    },
                ]
            }
        }

        rows = service.list_sessions("PROJ01", modality="OPT")

        assert [r["id"] for r in rows] == ["XNAT_E5"]

    def test_modality_pet_excludes_petmr_sessions(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    *self.ROWS["ResultSet"]["Result"],
                    {
                        "ID": "XNAT_E6",
                        "label": "PETMR001",
                        "subject_label": "SUB6",
                        "date": "2026-01-06",
                        "xsiType": "xnat:petmrSessionData",
                    },
                ]
            }
        }

        rows = service.list_sessions("PROJ01", modality="PET")

        assert [r["id"] for r in rows] == ["XNAT_E2"]

    def test_modality_classification_rejects_trailing_newline(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E7",
                        "label": "BAD001",
                        "subject_label": "SUB7",
                        "date": "2026-01-07",
                        "xsiType": "xnat:mrSessionData\n",
                    },
                ]
            }
        }

        rows = service.list_sessions("PROJ01")

        assert rows[0]["modality"] == "?"

    def test_subject_filter_is_forwarded_as_subject_label(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        service.list_sessions("PROJ01", subject="SUB1")

        mock_client.get_json.assert_called_once_with(
            "/data/projects/PROJ01/experiments",
            params={"columns": "ID,label,subject_label,date,xsiType", "subject_label": "SUB1"},
        )


class TestSessionShareConflictAndUnshare:
    def test_share_conflict_raises_resource_exists(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.put.side_effect = ClientRequestError(
            409, "PUT", "/data/experiments/XNAT_E00001/projects/PROJ02", "Already assigned"
        )

        with pytest.raises(ResourceExistsError, match="XNAT_E00001 -> PROJ02"):
            service.share("XNAT_E00001", "PROJ02")

    def test_share_other_client_error_propagates(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.put.side_effect = ClientRequestError(
            400, "PUT", "/data/experiments/XNAT_E00001/projects/PROJ02", "bad request"
        )

        with pytest.raises(ClientRequestError):
            service.share("XNAT_E00001", "PROJ02")

    def test_unshare_deletes_experiment_projects_path(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.delete.return_value = make_response("", status_code=200)

        resp = service.unshare("XNAT_E00001", "PROJ02", primary_project="PROJ01")

        assert resp.status_code == 200
        mock_client.delete.assert_called_once_with("/data/experiments/XNAT_E00001/projects/PROJ02")

    def test_unshare_refuses_the_primary_project(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        with pytest.raises(InputValidationError):
            service.unshare("XNAT_E00001", "PROJ01", primary_project="PROJ01")

        mock_client.delete.assert_not_called()

    def test_unshare_primary_check_ignores_case(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        with pytest.raises(InputValidationError):
            service.unshare("XNAT_E00001", "proj01", primary_project="PROJ01")

        mock_client.delete.assert_not_called()

    def test_unshare_refuses_empty_primary_project(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        with pytest.raises(InputValidationError):
            service.unshare("XNAT_E00001", "PROJ01", primary_project="")

        mock_client.delete.assert_not_called()

    def test_unshare_refuses_whitespace_only_primary_project(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        with pytest.raises(InputValidationError):
            service.unshare("XNAT_E00001", "PROJ01", primary_project="   ")

        mock_client.delete.assert_not_called()

    def test_unshare_primary_check_ignores_target_padding(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        with pytest.raises(InputValidationError):
            service.unshare("XNAT_E00001", " PROJ01 ", primary_project="PROJ01")

        mock_client.delete.assert_not_called()

    def test_unshare_primary_check_ignores_primary_padding(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        with pytest.raises(InputValidationError):
            service.unshare("XNAT_E00001", "PROJ01", primary_project=" PROJ01 ")

        mock_client.delete.assert_not_called()

    def test_list_shares_returns_result_rows(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"label": "SESS01", "ID": "PROJ01"},
                    {"label": "XNAT_E00001", "ID": "PROJ02"},
                ]
            }
        }

        rows = service.list_shares("XNAT_E00001")

        assert rows == [
            {"label": "SESS01", "ID": "PROJ01"},
            {"label": "XNAT_E00001", "ID": "PROJ02"},
        ]
        mock_client.get_json.assert_called_once_with("/data/experiments/XNAT_E00001/projects")


class TestSessionVars:
    def _fields_document(self, fields: list[tuple[str, str]]) -> dict:
        return {
            "items": [
                {
                    "data_fields": {
                        "ID": "XNAT_E00001",
                        "label": "SESS01",
                        "subject_ID": "XNAT_S00001",
                    },
                    "meta": {"xsi:type": "xnat:mrSessionData"},
                    "children": [
                        {
                            "field": "fields/field",
                            "items": [
                                {"data_fields": {"name": name, "field": value}, "children": []}
                                for name, value in fields
                            ],
                        }
                    ],
                }
            ]
        }

    def test_list_vars_flat_path(self, service: SessionService, mock_client: MagicMock) -> None:
        mock_client.get_json.return_value = self._fields_document(
            [("studytag", "phase1"), ("cohort", "A")]
        )

        rows = service.list_vars("XNAT_E00001")

        assert rows == [
            {"name": "studytag", "value": "phase1"},
            {"name": "cohort", "value": "A"},
        ]
        mock_client.get_json.assert_called_once_with(
            "/data/experiments/XNAT_E00001", params={"format": "json"}
        )

    def test_list_vars_no_fields_child_returns_empty(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {
            "items": [{"data_fields": {"ID": "XNAT_E00001"}, "meta": {}, "children": []}]
        }

        assert service.list_vars("XNAT_E00001") == []

    def test_list_vars_null_field_value_stays_empty_not_the_word_none(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {
            "items": [
                {
                    "data_fields": {"ID": "XNAT_E00001"},
                    "meta": {},
                    "children": [
                        {
                            "field": "fields/field",
                            "items": [{"data_fields": {"name": "studytag", "field": None}}],
                        }
                    ],
                }
            ]
        }

        rows = service.list_vars("XNAT_E00001")

        assert rows == [{"name": "studytag", "value": ""}]
        assert "None" not in rows[0]["value"]

    def test_set_vars_requires_subject_scoped_path_and_xsi_prefix(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.put.return_value = make_response("", content_type="text/plain")

        service.set_vars(
            project="PROJ01",
            subject="XNAT_S00001",
            experiment_id="XNAT_E00001",
            xsi_type="xnat:mrSessionData",
            fields={"studytag": "phase1", "cohort": "A"},
        )

        mock_client.put.assert_called_once_with(
            "/data/projects/PROJ01/subjects/XNAT_S00001/experiments/XNAT_E00001",
            params={
                "xsiType": "xnat:mrSessionData",
                "xnat:mrSessionData/fields/field[name=studytag]/field": "phase1",
                "xnat:mrSessionData/fields/field[name=cohort]/field": "A",
            },
        )

    # "trailing\n" is the non-obvious one: Python's `$` matches just
    # BEFORE a final newline, so a `^...$` regex checked with `.match()`
    # accepts it and the newline lands inside the XNAT query key.
    @pytest.mark.parametrize(
        "bad_name",
        ["", "has space", "has]bracket", "has=equals", "a/b", "trailing\n", "crlf\r\n"],
    )
    def test_set_vars_rejects_unsafe_field_names(
        self, service: SessionService, mock_client: MagicMock, bad_name: str
    ) -> None:
        with pytest.raises(InputValidationError):
            service.set_vars(
                project="PROJ01",
                subject="XNAT_S00001",
                experiment_id="XNAT_E00001",
                xsi_type="xnat:mrSessionData",
                fields={bad_name: "value"},
            )
        mock_client.put.assert_not_called()
