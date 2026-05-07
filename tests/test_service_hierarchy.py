"""Unit tests for hierarchy path building and resolution helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.models.hierarchy import ExperimentRef, ProjectRef, ResourceRef, ScanRef, SubjectRef
from xnatctl.services.hierarchy import HierarchyService


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock XNAT client."""

    return MagicMock()


@pytest.fixture
def service(mock_client: MagicMock) -> HierarchyService:
    """Create a hierarchy service."""

    return HierarchyService(mock_client)


class TestPathBuilders:
    """Tests for hierarchy-aware path construction."""

    def test_build_subject_path_with_project(self, service: HierarchyService) -> None:
        """Subjects become project-scoped when a project is supplied."""

        path = service.build_subject_path(SubjectRef(subject="SUB001", project_id="PROJ"))
        assert path == "/data/projects/PROJ/subjects/SUB001"

    def test_build_experiment_path_with_subject_scope(self, service: HierarchyService) -> None:
        """Experiments support project+subject scoping."""

        path = service.build_experiment_path(
            ExperimentRef(experiment="SESS001", project_id="PROJ", subject="SUB001")
        )
        assert path == "/data/projects/PROJ/subjects/SUB001/experiments/SESS001"

    def test_build_experiment_path_rejects_subject_without_project(
        self, service: HierarchyService
    ) -> None:
        """Subject scope without project is invalid in XNAT."""

        with pytest.raises(ValueError):
            service.build_experiment_path(ExperimentRef(experiment="SESS001", subject="SUB001"))

    def test_build_scan_resource_path(self, service: HierarchyService) -> None:
        """Scan resource paths descend through experiment and scan levels."""

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
        """Project resources are rooted directly under the project."""

        path = service.build_resource_path(
            ResourceRef(parent=ProjectRef(project_id="PROJ"), resource_label="QA")
        )
        assert path == "/data/projects/PROJ/resources/QA"


class TestResolution:
    """Tests for response parsing and live resolution helpers."""

    def test_extract_rows_from_top_level_list(self, service: HierarchyService) -> None:
        """Top-level JSON arrays are treated as collection rows."""

        rows = service.extract_rows(
            [
                {"ID": "PROJ1", "name": "Project 1"},
                {"ID": "PROJ2", "name": "Project 2"},
            ]
        )

        assert [row["ID"] for row in rows] == ["PROJ1", "PROJ2"]

    def test_parse_resolved_experiment_from_resultset(self, service: HierarchyService) -> None:
        """ResultSet detail rows are normalized into a resolved experiment ref."""

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
        """`items[]` detail responses are normalized into a resolved experiment ref."""

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
        """Live resolution goes through the client's JSON helper."""

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
        """Empty responses raise a resource-not-found error."""

        with pytest.raises(ResourceNotFoundError):
            service.parse_resolved_experiment(
                ExperimentRef(experiment="MISSING"), {"ResultSet": {"Result": []}}
            )

    def test_resolve_scan_xsi_type(self, service: HierarchyService) -> None:
        """Session xsiTypes map cleanly onto scan xsiTypes."""

        assert service.resolve_scan_xsi_type("xnat:eegSessionData") == "xnat:eegScanData"

    def test_hierarchy_resolve_experiment_label_fallback(
        self, service: HierarchyService, mock_client: MagicMock
    ) -> None:
        """When the direct GET returns empty ResultSet, fall back to project listing."""

        # 1) direct path returns empty ResultSet (label probe miss);
        # 2) project listing returns the canonical row;
        # 3) retry with canonical accession ID resolves.
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
        """Accession-ID-shaped tokens fall back to /data/experiments/{ID} cross-project."""

        # 1) direct project-scoped GET 404s (ID not owned by default_project);
        # 2) project listing returns no match;
        # 3) cross-project /data/experiments/{ID} resolves.
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
        """When no fallback resolves, raise ResourceNotFoundError with the original token."""

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
