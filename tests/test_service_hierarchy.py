from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError, XNATCtlError
from xnatctl.models.hierarchy import ExperimentRef, ProjectRef, ResourceRef, ScanRef, SubjectRef
from xnatctl.services.hierarchy import HierarchyService


@pytest.fixture
def mock_client() -> MagicMock:
    return MagicMock()


@pytest.fixture
def service(mock_client: MagicMock) -> HierarchyService:
    return HierarchyService(mock_client)


class TestPathBuilders:
    def test_build_subject_path_with_project(self, service: HierarchyService) -> None:
        path = service.build_subject_path(SubjectRef(subject="SUB001", project_id="PROJ"))
        assert path == "/data/projects/PROJ/subjects/SUB001"

    def test_build_experiment_path_with_subject_scope(self, service: HierarchyService) -> None:
        path = service.build_experiment_path(
            ExperimentRef(experiment="SESS001", project_id="PROJ", subject="SUB001")
        )
        assert path == "/data/projects/PROJ/subjects/SUB001/experiments/SESS001"

    def test_build_experiment_path_rejects_subject_without_project(
        self, service: HierarchyService
    ) -> None:
        with pytest.raises(ValueError):
            service.build_experiment_path(ExperimentRef(experiment="SESS001", subject="SUB001"))

    def test_build_scan_resource_path(self, service: HierarchyService) -> None:
        path = service.build_resource_path(
            ResourceRef(
                parent=ScanRef(
                    experiment=ExperimentRef(experiment="XNAT_E00001"),
                    scan_id="5",
                ),
                resource_label="DICOM",
            ),
            "files",
        )
        assert path == "/data/experiments/XNAT_E00001/scans/5/resources/DICOM/files"

    def test_build_project_resource_path(self, service: HierarchyService) -> None:
        path = service.build_resource_path(
            ResourceRef(parent=ProjectRef(project_id="PROJ"), resource_label="QA")
        )
        assert path == "/data/projects/PROJ/resources/QA"


class TestResolution:
    def test_extract_rows_from_top_level_list(self, service: HierarchyService) -> None:
        rows = service.extract_rows(
            [
                {"ID": "PROJ1", "name": "Project 1"},
                {"ID": "PROJ2", "name": "Project 2"},
            ]
        )

        assert [row["ID"] for row in rows] == ["PROJ1", "PROJ2"]

    def test_parse_resolved_experiment_from_resultset(self, service: HierarchyService) -> None:
        resolved = service.parse_resolved_experiment(
            ExperimentRef(experiment="SESS001", project_id="PROJ", experiment_is_label=True),
            {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "XNAT_E00001",
                            "label": "SESS001",
                            "project": "PROJ",
                            "subject_ID": "XNAT_S00001",
                            "subject_label": "SUB001",
                            "date": "2025-01-15",
                            "xsiType": "xnat:eegSessionData",
                        }
                    ]
                }
            },
        )

        assert resolved.experiment_id == "XNAT_E00001"
        assert resolved.project_id == "PROJ"
        assert resolved.subject_id == "XNAT_S00001"
        assert resolved.subject_label == "SUB001"
        assert resolved.session_date == "2025-01-15"
        assert resolved.xsi_type == "xnat:eegSessionData"

    def test_parse_resolved_experiment_from_items(self, service: HierarchyService) -> None:
        resolved = service.parse_resolved_experiment(
            ExperimentRef(experiment="SESS001", project_id="PROJ", experiment_is_label=True),
            {
                "items": [
                    {
                        "data_fields": {
                            "ID": "XNAT_E00001",
                            "label": "SESS001",
                            "project": "PROJ",
                            "subject_ID": "XNAT_S00001",
                            "subject_label": "SUB001",
                            "date": "2025-01-15",
                        },
                        "meta": {"xsi:type": "xnat:mrSessionData"},
                    }
                ]
            },
        )

        assert resolved.experiment_id == "XNAT_E00001"
        assert resolved.xsi_type == "xnat:mrSessionData"

    def test_resolve_experiment_uses_get_json(
        self, service: HierarchyService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "label": "SESS001",
                        "project": "PROJ",
                    }
                ]
            }
        }

        resolved = service.resolve_experiment(
            ExperimentRef(experiment="SESS001", project_id="PROJ", experiment_is_label=True)
        )

        assert resolved.experiment_id == "XNAT_E00001"
        mock_client.get_json.assert_called_once_with("/data/projects/PROJ/experiments/SESS001")

    def test_parse_resolved_experiment_not_found(self, service: HierarchyService) -> None:
        with pytest.raises(ResourceNotFoundError):
            service.parse_resolved_experiment(
                ExperimentRef(experiment="MISSING"), {"ResultSet": {"Result": []}}
            )

    def test_resolve_scan_xsi_type(self, service: HierarchyService) -> None:
        assert service.resolve_scan_xsi_type("xnat:eegSessionData") == "xnat:eegScanData"

    def test_hierarchy_resolve_experiment_label_fallback(
        self, service: HierarchyService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.side_effect = [
            {"ResultSet": {"Result": []}},
            {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "XNAT_E00001",
                            "label": "SESS_LABEL",
                            "subject_ID": "XNAT_S00001",
                            "xsiType": "xnat:mrSessionData",
                        },
                        {
                            "ID": "XNAT_E00002",
                            "label": "OTHER_LABEL",
                            "subject_ID": "XNAT_S00002",
                            "xsiType": "xnat:mrSessionData",
                        },
                    ]
                }
            },
            {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "XNAT_E00001",
                            "label": "SESS_LABEL",
                            "project": "PROJ",
                        }
                    ]
                }
            },
        ]

        resolved = service.resolve_experiment(
            ExperimentRef(
                experiment="SESS_LABEL",
                project_id="PROJ",
                experiment_is_label=True,
            )
        )

        assert resolved.experiment_id == "XNAT_E00001"
        assert mock_client.get_json.call_count == 3
        listing_call = mock_client.get_json.call_args_list[1]
        assert listing_call[0][0] == "/data/projects/PROJ/experiments"
        assert listing_call[1]["params"] == {"columns": "ID,label,subject_ID,xsiType"}
        retry_call = mock_client.get_json.call_args_list[2]
        assert retry_call[0][0] == "/data/projects/PROJ/experiments/XNAT_E00001"

    def test_hierarchy_resolve_experiment_accession_id_cross_project_fallback(
        self, service: HierarchyService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.side_effect = [
            ResourceNotFoundError("session", "XNAT_E00001"),
            {"ResultSet": {"Result": []}},
            {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "XNAT_E00001",
                            "label": "OTHER_LABEL",
                            "project": "OTHER_PROJ",
                        }
                    ]
                }
            },
        ]

        resolved = service.resolve_experiment(
            ExperimentRef(
                experiment="XNAT_E00001",
                project_id="DEFAULT_PROJ",
                experiment_is_label=True,
            )
        )

        assert resolved.experiment_id == "XNAT_E00001"
        assert resolved.project_id == "OTHER_PROJ"
        cross_project_call = mock_client.get_json.call_args_list[-1]
        assert cross_project_call[0][0] == "/data/experiments/XNAT_E00001"

    def test_hierarchy_resolve_experiment_label_fallback_misses_raises(
        self, service: HierarchyService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.side_effect = [
            ResourceNotFoundError("session", "MISSING_LABEL"),
            {"ResultSet": {"Result": []}},
        ]

        with pytest.raises(ResourceNotFoundError):
            service.resolve_experiment(
                ExperimentRef(
                    experiment="MISSING_LABEL",
                    project_id="PROJ",
                    experiment_is_label=True,
                )
            )


class TestListScanRows:
    def _ref(self) -> ExperimentRef:
        return ExperimentRef(experiment="XNAT_E00001", experiment_is_label=False)

    def test_unfiltered_listing_returns_all_scans(
        self, service: HierarchyService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "1", "type": "Angiography", "xsiType": "xnat:otherDicomScanData"},
                    {"ID": "2", "type": "Macular Cube", "xsiType": "xnat:otherDicomScanData"},
                ]
            }
        }

        rows = service.list_scan_rows(self._ref(), "xnat:optSessionData")

        assert [r["ID"] for r in rows] == ["1", "2"]
        assert mock_client.get_json.call_count == 1
        assert mock_client.get_json.call_args_list[0][0][0] == "/data/experiments/XNAT_E00001/scans"
        assert "params" not in mock_client.get_json.call_args_list[0][1]

    def test_empty_unfiltered_listing_falls_back_to_scan_xsi_type(
        self, service: HierarchyService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.side_effect = [
            {"ResultSet": {"Result": []}},
            {"ResultSet": {"Result": [{"ID": "1", "type": "EEG"}]}},
        ]

        rows = service.list_scan_rows(self._ref(), "xnat:eegSessionData")

        assert [r["ID"] for r in rows] == ["1"]
        assert mock_client.get_json.call_count == 2
        assert mock_client.get_json.call_args_list[1][1]["params"] == {
            "xsiType": "xnat:eegScanData"
        }

    def test_empty_unfiltered_listing_without_resolvable_xsi_type_returns_empty(
        self, service: HierarchyService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        rows = service.list_scan_rows(self._ref(), None)

        assert rows == []
        assert mock_client.get_json.call_count == 1


class TestRoutableScanParent:
    def test_project_scoped_id_drops_the_project(self) -> None:
        ref = ExperimentRef(experiment="XNAT_E00001", project_id="PROJ")
        assert HierarchyService.build_scan_collection_path(ref) == (
            "/data/experiments/XNAT_E00001/scans"
        )

    def test_accession_id_flagged_as_label_still_routes(self) -> None:
        ref = ExperimentRef(experiment="XNAT_E00001", project_id="PROJ", experiment_is_label=True)
        assert HierarchyService.routable_scan_parent(ref) == ExperimentRef(experiment="XNAT_E00001")

    def test_a_subject_is_left_alone(self) -> None:
        ref = ExperimentRef(experiment="XNAT_E00001", project_id="PROJ", subject="XNAT_S00001")
        assert HierarchyService.build_scan_collection_path(ref) == (
            "/data/projects/PROJ/subjects/XNAT_S00001/experiments/XNAT_E00001/scans"
        )

    def test_a_genuine_label_cannot_be_rewritten(self) -> None:
        ref = ExperimentRef(experiment="SESS_LABEL", project_id="PROJ", experiment_is_label=True)
        assert HierarchyService.routable_scan_parent(ref) == ref

    def test_no_project_is_untouched(self) -> None:
        ref = ExperimentRef(experiment="XNAT_E00001")
        assert HierarchyService.routable_scan_parent(ref) == ref

    def test_scan_item_and_nested_suffixes_route_too(self) -> None:
        scan = ScanRef(
            experiment=ExperimentRef(experiment="XNAT_E00001", project_id="PROJ"), scan_id="2"
        )
        assert HierarchyService.build_scan_path(scan) == "/data/experiments/XNAT_E00001/scans/2"
        assert HierarchyService.build_scan_path(scan, "files") == (
            "/data/experiments/XNAT_E00001/scans/2/files"
        )
        assert HierarchyService.build_resource_collection_path(scan) == (
            "/data/experiments/XNAT_E00001/scans/2/resources"
        )

    def test_experiment_level_resources_keep_the_project(self) -> None:
        ref = ExperimentRef(experiment="XNAT_E00001", project_id="PROJ")
        assert HierarchyService.build_resource_collection_path(ref) == (
            "/data/projects/PROJ/experiments/XNAT_E00001/resources"
        )


class TestGetExperimentJson:
    def test_no_params_issues_plain_get(
        self, service: HierarchyService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {"items": []}

        service.get_experiment_json(ExperimentRef(experiment="EXP01"))

        mock_client.get_json.assert_called_once_with("/data/experiments/EXP01")

    def test_parts_and_params_forwarded(
        self, service: HierarchyService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        service.get_experiment_json(
            ExperimentRef(experiment="EXP01"), "scans", params={"xsiType": "x"}
        )

        mock_client.get_json.assert_called_once_with(
            "/data/experiments/EXP01/scans", params={"xsiType": "x"}
        )

    def test_returns_parsed_json(self, service: HierarchyService, mock_client: MagicMock) -> None:
        payload = {"ResultSet": {"Result": [{"ID": "1"}]}}
        mock_client.get_json.return_value = payload

        assert service.get_experiment_json(ExperimentRef(experiment="EXP01")) is payload


class TestExtractItemChildren:
    def _document(self, children: list[dict]) -> dict:
        return {"items": [{"data_fields": {}, "meta": {}, "children": children}]}

    def test_no_matching_child_returns_empty(self) -> None:
        data = self._document([{"field": "other/field", "items": []}])

        assert HierarchyService.extract_item_children(data, "fields/field") == []

    def test_no_first_item_returns_empty(self) -> None:
        assert HierarchyService.extract_item_children({"items": []}, "fields/field") == []

    def test_missing_items_key_returns_empty(self) -> None:
        data = self._document([{"field": "fields/field"}])

        assert HierarchyService.extract_item_children(data, "fields/field") == []

    def test_null_items_returns_empty(self) -> None:
        data = self._document([{"field": "fields/field", "items": None}])

        assert HierarchyService.extract_item_children(data, "fields/field") == []

    def test_normal_items_extracted(self) -> None:
        data = self._document(
            [
                {
                    "field": "fields/field",
                    "items": [{"data_fields": {"name": "a", "field": "1"}}],
                }
            ]
        )

        assert HierarchyService.extract_item_children(data, "fields/field") == [
            {"name": "a", "field": "1"}
        ]

    def test_non_list_items_raises(self) -> None:
        data = self._document([{"field": "fields/field", "items": "not-a-list"}])

        with pytest.raises(XNATCtlError):
            HierarchyService.extract_item_children(data, "fields/field")

    def test_non_dict_entry_raises(self) -> None:
        data = self._document([{"field": "fields/field", "items": ["not-a-dict"]}])

        with pytest.raises(XNATCtlError):
            HierarchyService.extract_item_children(data, "fields/field")

    def test_entry_missing_data_fields_raises(self) -> None:
        data = self._document([{"field": "fields/field", "items": [{"no": "data_fields"}]}])

        with pytest.raises(XNATCtlError):
            HierarchyService.extract_item_children(data, "fields/field")

    def test_entry_data_fields_not_dict_raises(self) -> None:
        data = self._document([{"field": "fields/field", "items": [{"data_fields": "not-a-dict"}]}])

        with pytest.raises(XNATCtlError):
            HierarchyService.extract_item_children(data, "fields/field")

    def test_missing_top_level_items_key_raises(self) -> None:
        with pytest.raises(XNATCtlError):
            HierarchyService.extract_item_children({"message": "plugin disabled"}, "fields/field")

    def test_empty_top_level_items_list_stays_genuinely_empty(self) -> None:
        assert HierarchyService.extract_item_children({"items": []}, "fields/field") == []

    def test_malformed_top_level_entry_raises(self) -> None:
        with pytest.raises(XNATCtlError):
            HierarchyService.extract_item_children({"items": [{}]}, "fields/field")

    def test_non_dict_top_level_entry_raises(self) -> None:
        with pytest.raises(XNATCtlError):
            HierarchyService.extract_item_children({"items": ["not-a-dict"]}, "fields/field")

    def test_non_list_top_level_items_raises(self) -> None:
        data = {"items": "not-a-list"}

        with pytest.raises(XNATCtlError):
            HierarchyService.extract_item_children(data, "fields/field")


class TestStringifyField:
    def test_none_becomes_empty_string_not_the_word_none(self) -> None:
        assert HierarchyService.stringify_field(None) == ""

    def test_non_none_value_stringified(self) -> None:
        assert HierarchyService.stringify_field(42) == "42"
        assert HierarchyService.stringify_field("value") == "value"
