"""Tests for transfer filter engine."""
from __future__ import annotations

import pytest

from xnatctl.models.transfer import (
    FilterConfig,
    FilterSyncType,
    ImagingSessionFilter,
    ResourceFilter,
    ScanTypeFilter,
    XsiTypeFilter,
)
from xnatctl.services.transfer.discovery import ChangeType, DiscoveredEntity
from xnatctl.services.transfer.filter import FilterEngine


@pytest.fixture
def engine() -> FilterEngine:
    config = FilterConfig(
        project_resources=ResourceFilter(sync_type=FilterSyncType.NONE),
        imaging_sessions=ImagingSessionFilter(
            sync_type=FilterSyncType.INCLUDE,
            xsi_types=[
                XsiTypeFilter(
                    xsi_type="xnat:mrSessionData",
                    scan_types=ScanTypeFilter(
                        sync_type=FilterSyncType.INCLUDE,
                        items=["T1w", "T2w"],
                    ),
                    scan_resources=ResourceFilter(
                        sync_type=FilterSyncType.INCLUDE,
                        items=["DICOM"],
                    ),
                ),
            ],
        ),
    )
    return FilterEngine(config)


class TestFilterExperiments:
    def test_includes_matching_xsi_type(self, engine: FilterEngine) -> None:
        exp = DiscoveredEntity(
            local_id="E1",
            local_label="E1",
            change_type=ChangeType.NEW,
            xsi_type="xnat:mrSessionData",
        )
        assert engine.should_include_experiment(exp) is True

    def test_excludes_non_matching_xsi_type(self, engine: FilterEngine) -> None:
        exp = DiscoveredEntity(
            local_id="E1",
            local_label="E1",
            change_type=ChangeType.NEW,
            xsi_type="xnat:petSessionData",
        )
        assert engine.should_include_experiment(exp) is False


class TestFilterScans:
    def test_includes_matching_scan_type(self, engine: FilterEngine) -> None:
        assert engine.should_include_scan("xnat:mrSessionData", "T1w") is True

    def test_excludes_non_matching_scan_type(self, engine: FilterEngine) -> None:
        assert engine.should_include_scan("xnat:mrSessionData", "localizer") is False

    def test_unknown_xsi_type_defaults_to_include(self, engine: FilterEngine) -> None:
        assert engine.should_include_scan("xnat:petSessionData", "T1w") is False


class TestFilterResources:
    def test_scan_resource_filter(self, engine: FilterEngine) -> None:
        assert engine.should_include_scan_resource("xnat:mrSessionData", "DICOM") is True
        assert engine.should_include_scan_resource("xnat:mrSessionData", "SNAP") is False

    def test_project_resources_none(self, engine: FilterEngine) -> None:
        assert engine.should_include_project_resource("anything") is False


class TestFilterAllConfig:
    def test_default_config_includes_everything(self) -> None:
        engine = FilterEngine(FilterConfig())
        exp = DiscoveredEntity(
            local_id="E1",
            local_label="E1",
            change_type=ChangeType.NEW,
            xsi_type="xnat:mrSessionData",
        )
        assert engine.should_include_experiment(exp) is True
        assert engine.should_include_scan("xnat:mrSessionData", "T1w") is True
        assert engine.should_include_project_resource("any") is True
