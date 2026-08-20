"""Tests for the single /data/services/import request builder.

Each expected dict below is the literal querystring its call site sent
*before* construction was centralized -- the builder must reproduce the exact
key set and values, because XNAT's importer is sensitive to both the key
convention and which keys are present at all. Key *order* is not pinned: the
sites historically disagreed on it (gradual-DICOM led with ``inbody``, the
archive path ended with it) and querystring parameter order carries no meaning
to XNAT's servlet parsing.
"""

from __future__ import annotations

from xnatctl.services.import_service import (
    IMPORT_ENDPOINT,
    archive_destination_params,
    build_import_params,
)


def test_the_endpoint_is_the_import_service() -> None:
    assert IMPORT_ENDPOINT == "/data/services/import"


class TestArchiveDestinationParams:
    def test_direct_archive(self) -> None:
        assert archive_destination_params("PROJ", True) == {"Direct-Archive": "true"}

    def test_prearchive(self) -> None:
        assert archive_destination_params("PROJ", False) == {"dest": "/prearchive/projects/PROJ"}


class TestBuildImportParams:
    def test_archive_upload_site(self) -> None:
        """The batch/single-archive upload path (session key convention)."""
        params = build_import_params(
            import_handler="DICOM-zip",
            project="PROJ",
            subject="SUBJ",
            session="SESS",
            overwrite="delete",
            overwrite_files=True,
            quarantine=False,
            trigger_pipelines=True,
            rename=False,
            inbody=True,
            ignore_unparsable=False,
            direct_archive=True,
        )
        assert params == {
            "import-handler": "DICOM-zip",
            "Ignore-Unparsable": "false",
            "project": "PROJ",
            "subject": "SUBJ",
            "session": "SESS",
            "overwrite": "delete",
            "overwrite_files": "true",
            "quarantine": "false",
            "triggerPipelines": "true",
            "rename": "false",
            "inbody": "true",
            "Direct-Archive": "true",
        }

    def test_gradual_dicom_site(self) -> None:
        """The per-file gradual-DICOM path (experiment key convention)."""
        params = build_import_params(
            import_handler="gradual-DICOM",
            project="PROJ",
            subject="SUBJ",
            session="SESS",
            entity_keys="experiment",
            inbody=True,
            direct_archive=False,
        )
        assert params == {
            "import-handler": "gradual-DICOM",
            "PROJECT_ID": "PROJ",
            "SUBJECT_ID": "SUBJ",
            "EXPT_LABEL": "SESS",
            "inbody": "true",
            "dest": "/prearchive/projects/PROJ",
        }

    def test_transfer_executor_site(self) -> None:
        """The cross-server transfer path: append + literal /archive destination."""
        params = build_import_params(
            import_handler="DICOM-zip",
            project="DST",
            subject="SUB001",
            session="EXP001",
            entity_keys="experiment",
            overwrite="append",
            destination="/archive",
        )
        assert params == {
            "import-handler": "DICOM-zip",
            "PROJECT_ID": "DST",
            "SUBJECT_ID": "SUB001",
            "EXPT_LABEL": "EXP001",
            "overwrite": "append",
            "destination": "/archive",
        }

    def test_none_omits_keys_entirely(self) -> None:
        """An absent key and an explicit default differ to XNAT; None sends nothing."""
        params = build_import_params(
            import_handler="DICOM-zip",
            project="P",
            subject="S",
            session="E",
        )
        assert params == {
            "import-handler": "DICOM-zip",
            "project": "P",
            "subject": "S",
            "session": "E",
        }
