"""Tests for transfer configuration models."""

from __future__ import annotations

import pytest
import yaml

from xnatctl.models.transfer import (
    AssessorFilter,
    FilterSyncType,
    ImagingSessionFilter,
    ResourceFilter,
    ScanTypeFilter,
    TransferConfig,
    XsiTypeFilter,
)


class TestFilterSyncType:
    def test_all_is_default(self) -> None:
        assert FilterSyncType.ALL.value == "all"

    def test_valid_values(self) -> None:
        for v in ("all", "none", "include", "exclude"):
            assert FilterSyncType(v).value == v


class TestResourceFilter:
    def test_defaults_to_all(self) -> None:
        f = ResourceFilter()
        assert f.sync_type == FilterSyncType.ALL
        assert f.items == []

    def test_include_with_items(self) -> None:
        f = ResourceFilter(sync_type=FilterSyncType.INCLUDE, items=["DICOM", "NIFTI"])
        assert f.should_include("DICOM") is True
        assert f.should_include("SNAPSHOTS") is False

    def test_exclude_with_items(self) -> None:
        f = ResourceFilter(sync_type=FilterSyncType.EXCLUDE, items=["SNAPSHOTS"])
        assert f.should_include("DICOM") is True
        assert f.should_include("SNAPSHOTS") is False

    def test_none_excludes_all(self) -> None:
        f = ResourceFilter(sync_type=FilterSyncType.NONE)
        assert f.should_include("anything") is False

    def test_all_includes_all(self) -> None:
        f = ResourceFilter(sync_type=FilterSyncType.ALL)
        assert f.should_include("anything") is True


class TestScanTypeFilter:
    def test_defaults_to_all(self) -> None:
        f = ScanTypeFilter()
        assert f.sync_type == FilterSyncType.ALL
        assert f.items == []

    def test_include_with_items(self) -> None:
        f = ScanTypeFilter(sync_type=FilterSyncType.INCLUDE, items=["T1w", "T2w"])
        assert f.should_include("T1w") is True
        assert f.should_include("FLAIR") is False

    def test_exclude_with_items(self) -> None:
        f = ScanTypeFilter(sync_type=FilterSyncType.EXCLUDE, items=["localizer"])
        assert f.should_include("T1w") is True
        assert f.should_include("localizer") is False

    def test_none_excludes_all(self) -> None:
        f = ScanTypeFilter(sync_type=FilterSyncType.NONE)
        assert f.should_include("T1w") is False


class TestImagingSessionFilter:
    def test_defaults_to_all(self) -> None:
        f = ImagingSessionFilter()
        assert f.sync_type == FilterSyncType.ALL
        assert f.xsi_types == []

    def test_should_include_type_all(self) -> None:
        f = ImagingSessionFilter()
        assert f.should_include_type("xnat:mrSessionData") is True

    def test_should_include_type_none(self) -> None:
        f = ImagingSessionFilter(sync_type=FilterSyncType.NONE)
        assert f.should_include_type("xnat:mrSessionData") is False

    def test_should_include_type_include(self) -> None:
        f = ImagingSessionFilter(
            sync_type=FilterSyncType.INCLUDE,
            xsi_types=[XsiTypeFilter(xsi_type="xnat:mrSessionData")],
        )
        assert f.should_include_type("xnat:mrSessionData") is True
        assert f.should_include_type("xnat:ctSessionData") is False

    def test_should_include_type_exclude(self) -> None:
        f = ImagingSessionFilter(
            sync_type=FilterSyncType.EXCLUDE,
            xsi_types=[XsiTypeFilter(xsi_type="xnat:ctSessionData")],
        )
        assert f.should_include_type("xnat:mrSessionData") is True
        assert f.should_include_type("xnat:ctSessionData") is False

    def test_get_type_filter_found(self) -> None:
        tf = XsiTypeFilter(xsi_type="xnat:mrSessionData")
        f = ImagingSessionFilter(
            sync_type=FilterSyncType.INCLUDE,
            xsi_types=[tf],
        )
        assert f.get_type_filter("xnat:mrSessionData") is tf

    def test_get_type_filter_not_found(self) -> None:
        f = ImagingSessionFilter()
        assert f.get_type_filter("xnat:mrSessionData") is None


class TestAssessorFilter:
    def test_defaults_to_all(self) -> None:
        f = AssessorFilter()
        assert f.sync_type == FilterSyncType.ALL

    def test_should_include_type_include(self) -> None:
        f = AssessorFilter(
            sync_type=FilterSyncType.INCLUDE,
            xsi_types=[XsiTypeFilter(xsi_type="fs:fsData")],
        )
        assert f.should_include_type("fs:fsData") is True
        assert f.should_include_type("proc:genProcData") is False


class TestTransferConfig:
    def test_from_yaml(self, tmp_path: object) -> None:
        yaml_content = {
            "source_project": "SRC",
            "dest_project": "DST",
            "max_failures": 3,
            "filtering": {
                "project_resources": {"sync_type": "none"},
                "imaging_sessions": {
                    "sync_type": "include",
                    "xsi_types": [
                        {
                            "xsi_type": "xnat:mrSessionData",
                            "scan_types": {
                                "sync_type": "include",
                                "items": ["T1w", "T2w"],
                            },
                            "scan_resources": {
                                "sync_type": "include",
                                "items": ["DICOM"],
                            },
                        }
                    ],
                },
            },
        }
        path = tmp_path / "transfer.yaml"  # type: ignore[operator]
        path.write_text(yaml.dump(yaml_content))

        config = TransferConfig.from_yaml(path)

        assert config.source_project == "SRC"
        assert config.dest_project == "DST"
        assert config.max_failures == 3
        assert config.filtering.project_resources.sync_type == FilterSyncType.NONE

        sessions = config.filtering.imaging_sessions
        assert sessions.sync_type == FilterSyncType.INCLUDE
        assert len(sessions.xsi_types) == 1
        assert sessions.xsi_types[0].xsi_type == "xnat:mrSessionData"

    def test_default_config(self) -> None:
        config = TransferConfig(source_project="SRC", dest_project="DST")
        assert config.max_failures == 5
        assert config.filtering.project_resources.sync_type == FilterSyncType.ALL

    def test_scaffold_yaml(self) -> None:
        text = TransferConfig.scaffold("SRC", "DST")
        parsed = yaml.safe_load(text)
        assert parsed["source_project"] == "SRC"
        assert parsed["dest_project"] == "DST"

    def test_from_yaml_invalid_file(self, tmp_path: object) -> None:
        from xnatctl.core.exceptions import TransferConfigError

        path = tmp_path / "bad.yaml"  # type: ignore[operator]
        path.write_text("source_project: 123\n")  # missing dest_project

        with pytest.raises(TransferConfigError, match="Invalid transfer config"):
            TransferConfig.from_yaml(path)

    def test_from_yaml_missing_file(self, tmp_path: object) -> None:
        from xnatctl.core.exceptions import TransferConfigError

        path = tmp_path / "nonexistent.yaml"  # type: ignore[operator]

        with pytest.raises(TransferConfigError, match="Failed to load"):
            TransferConfig.from_yaml(path)

    def test_sync_new_only_default(self) -> None:
        config = TransferConfig(source_project="SRC", dest_project="DST")
        assert config.sync_new_only is True
