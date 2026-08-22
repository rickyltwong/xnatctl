"""Path-injection hardening tests for the hierarchy builders, refs, and quoting helpers.

Covers three layers:

* ``core.validation.quote_path_segment`` / ``quote_prearchive_segment`` --
  the shared percent-encoding helpers.
* ``models.hierarchy`` refs (``ProjectRef``, ``SubjectRef``, ``ExperimentRef``,
  ``ScanRef``, ``ResourceRef``) -- reject a hostile identifier at
  construction, before it ever reaches a path builder.
* ``services.hierarchy.join_api_path`` / ``HierarchyService.build_*_path`` --
  every legal-ID path stays byte-for-byte unchanged; every hostile segment
  is either encoded into a single harmless path element or rejected outright.

And a traversal regression at the download boundary: a caller-supplied local
filename cannot climb out of the output directory.
"""

from __future__ import annotations

import os
import unicodedata
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xnatctl.core.exceptions import InputValidationError, PathValidationError
from xnatctl.core.validation import (
    check_no_casefold_collision,
    quote_path_segment,
    quote_prearchive_segment,
    validate_local_path_component,
    verify_directory_contained_in,
)
from xnatctl.models.hierarchy import (
    ExperimentRef,
    ProjectRef,
    ResourceRef,
    ScanRef,
    SubjectRef,
)
from xnatctl.services.admin import AdminService
from xnatctl.services.downloads import DownloadService, _safe_output_path
from xnatctl.services.hierarchy import HierarchyService, join_api_path
from xnatctl.services.pipelines import PipelineService
from xnatctl.services.prearchive import PrearchiveService
from xnatctl.services.projects import ProjectService
from xnatctl.services.resources import ResourceService
from xnatctl.services.sessions import SessionService
from xnatctl.services.subjects import SubjectService
from xnatctl.services.transfer.discovery import DiscoveryService
from xnatctl.services.transfer.executor import TransferExecutor
from xnatctl.services.transfer.orchestrator import TransferResult
from xnatctl.services.transfer.scan_transfer import ScanTransfer
from xnatctl.services.transfer.verifier import Verifier
from xnatctl.services.zip_extract import _extract_scan_zip

# Hostile identifiers every ref field and path builder must neutralize.
HOSTILE_IDENTIFIERS = [
    "a/b",
    "x?y=z",
    "..",
    ".",
    "%2e%2e",
    "",
    "   ",
    "a\x00b",
    "a#b",
    "a\\b",
    "a\x80b",  # Unicode C1 control char
]

# ResourceRef uses a looser validator than every other ref field: '#', '?',
# and '%' are routine in real resource labels and the quoting layer encodes
# them unambiguously, so they are NOT part of its hostile set.
RESOURCE_HOSTILE_IDENTIFIERS = [
    "a/b",
    "..",
    ".",
    "",
    "   ",
    "a\x00b",
    "a\\b",
    "a\x80b",
]

# The characters ResourceRef accepts specifically because they are routine
# in real resource labels and get percent-encoded, not rejected.
RESOURCE_URL_RESERVED_LABELS = ["QA #1", "x?y=z", "%2e%2e", "a#b"]

# Legal identifiers/labels that must pass through untouched -- includes
# characters (dots, spaces, parens, unicode) that are routine in real XNAT
# labels but illegal in the strict xnat_identifier charset.
LEGAL_LABELS = [
    "XNAT_S00001",
    "SUB-001",
    "SUB_001",
    "John.Doe",
    "QA (v2)",
    "Étude_01",
]


class TestQuoteHelpers:
    """Direct tests for the shared path-segment quoting helpers."""

    def test_quote_path_segment_leaves_legal_chars_untouched(self) -> None:
        assert quote_path_segment("XNAT_S00001") == "XNAT_S00001"
        assert quote_path_segment("SUB-001") == "SUB-001"

    def test_quote_path_segment_keeps_dots_literal(self) -> None:
        """Dots stay literal for /data/ paths -- labels routinely contain them."""
        assert quote_path_segment("John.Doe") == "John.Doe"

    def test_quote_path_segment_encodes_reserved_chars(self) -> None:
        assert quote_path_segment("a/b") == "a%2Fb"
        assert quote_path_segment("x?y=z") == "x%3Fy%3Dz"
        assert quote_path_segment("a#b") == "a%23b"

    def test_quote_prearchive_segment_also_encodes_dots(self) -> None:
        """Prearchive/archive-move paths %2E-encode dots -- load-bearing, see executor.py."""
        assert quote_prearchive_segment("John.Doe") == "John%2EDoe"
        assert quote_prearchive_segment("a/b") == "a%2Fb"

    @pytest.mark.parametrize("dotted", [".", "..", "..."])
    def test_quote_path_segment_rejects_dot_only_segments(self, dotted: str) -> None:
        """A bare ".."/"." would otherwise pass through literal -- quote() treats
        dots as unreserved, and /data/ paths deliberately leave dots alone for
        real labels, so a dot-only segment needs an explicit reject here.
        """
        with pytest.raises(InputValidationError):
            quote_path_segment(dotted)

    def test_quote_prearchive_segment_neutralizes_dot_only_via_encoding(self) -> None:
        """The prearchive variant doesn't need the reject -- it %2E-encodes dots."""
        assert quote_prearchive_segment("..") == "%2E%2E"

    def test_direct_service_call_bypassing_refs_rejects_dot_segment(self) -> None:
        """ProjectService.get() takes a raw string, not a ProjectRef -- the one
        call shape refs cannot protect. Must still reject ".." rather than
        building /data/projects/.. (which a server/proxy could normalize
        upward, escaping the /projects/ route entirely).
        """
        client = MagicMock()
        service = ProjectService(client)
        with pytest.raises(InputValidationError):
            service.get("..")
        client.get.assert_not_called()

    @pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
    def test_quote_path_segment_rejects_empty_or_whitespace(self, empty: str) -> None:
        """An empty segment would otherwise pass through as "" -- turning an
        item route into a COLLECTION route (see the direct-service test below).
        """
        with pytest.raises(InputValidationError):
            quote_path_segment(empty)

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_quote_prearchive_segment_rejects_empty_or_whitespace(self, empty: str) -> None:
        with pytest.raises(InputValidationError):
            quote_prearchive_segment(empty)

    def test_direct_service_call_with_empty_id_rejects_before_the_request(self) -> None:
        """ProjectService.delete("") would otherwise build DELETE /data/projects/
        -- the PROJECT COLLECTION route, not a single (missing) project.
        """
        client = MagicMock()
        service = ProjectService(client)
        with pytest.raises(InputValidationError):
            service.delete("")
        client.delete.assert_not_called()


class TestJoinApiPath:
    """Tests for the low-level path joiner."""

    def test_legal_segments_unchanged(self) -> None:
        assert join_api_path("data", "projects", "PROJ1") == "/data/projects/PROJ1"

    def test_hostile_segment_is_encoded_as_one_element(self) -> None:
        """A slash embedded in a single logical segment cannot add a route hop."""
        path = join_api_path("data", "projects", "a/b")
        assert path == "/data/projects/a%2Fb"
        assert path.count("/") == 3  # only the real separators

    def test_query_breaking_chars_are_encoded(self) -> None:
        path = join_api_path("data", "experiments", "XNAT_E1?activate=")
        assert "?" not in path
        assert path == "/data/experiments/XNAT_E1%3Factivate%3D"

    def test_dot_only_segment_is_rejected_not_left_literal(self) -> None:
        """A bare ".." segment must never survive into the joined path."""
        with pytest.raises(InputValidationError):
            join_api_path("data", "projects", "..")

    def test_leading_slash_on_a_part_is_rejected_not_silently_stripped(self) -> None:
        """A leading slash used to canonicalize away (``/TARGET`` -> ``TARGET``)
        -- a different resource for what was actually invalid input. No
        static literal in this module carries a slash, so a leading/trailing
        slash here is always a bug.
        """
        with pytest.raises(InputValidationError):
            join_api_path("data", "projects", "/TARGET")

    def test_trailing_slash_on_a_part_is_rejected(self) -> None:
        with pytest.raises(InputValidationError):
            join_api_path("data", "projects", "TARGET/")

    def test_slash_only_part_is_rejected_not_reduced_to_a_double_slash(self) -> None:
        """A bare "/" part used to strip to "" -- a double-slash route."""
        with pytest.raises(InputValidationError):
            join_api_path("data", "/", "TARGET")

    def test_empty_part_is_rejected_not_silently_dropped(self) -> None:
        with pytest.raises(InputValidationError):
            join_api_path("data", "", "TARGET")

    def test_none_part_is_still_silently_omitted(self) -> None:
        """None is the deliberate "omit this part" sentinel -- unlike "", it
        is never a caller-supplied value that could indicate a bug.
        """
        assert join_api_path("data", "projects", None, "TARGET") == "/data/projects/TARGET"


class TestRefConstructionRejection:
    """Hostile identifiers must raise at ref construction, not at request time."""

    @pytest.mark.parametrize("bad", HOSTILE_IDENTIFIERS)
    def test_project_ref_rejects_hostile_id(self, bad: str) -> None:
        with pytest.raises(InputValidationError):
            ProjectRef(project_id=bad)

    @pytest.mark.parametrize("bad", HOSTILE_IDENTIFIERS)
    def test_subject_ref_rejects_hostile_subject(self, bad: str) -> None:
        with pytest.raises(InputValidationError):
            SubjectRef(subject=bad, project_id="PROJ")

    @pytest.mark.parametrize("bad", HOSTILE_IDENTIFIERS)
    def test_subject_ref_rejects_hostile_project(self, bad: str) -> None:
        with pytest.raises(InputValidationError):
            SubjectRef(subject="SUB1", project_id=bad)

    @pytest.mark.parametrize("bad", HOSTILE_IDENTIFIERS)
    def test_experiment_ref_rejects_hostile_experiment(self, bad: str) -> None:
        with pytest.raises(InputValidationError):
            ExperimentRef(experiment=bad, project_id="PROJ")

    def test_experiment_ref_rejects_the_documented_attack(self) -> None:
        """The exact scenario from the library-hardening report."""
        with pytest.raises(InputValidationError):
            ExperimentRef(experiment="SUB1/experiments/XNAT_E1?activate=")

    @pytest.mark.parametrize("bad", HOSTILE_IDENTIFIERS)
    def test_scan_ref_rejects_hostile_scan_id(self, bad: str) -> None:
        with pytest.raises(InputValidationError):
            ScanRef(experiment=ExperimentRef(experiment="XNAT_E1"), scan_id=bad)

    @pytest.mark.parametrize("bad", RESOURCE_HOSTILE_IDENTIFIERS)
    def test_resource_ref_rejects_hostile_label(self, bad: str) -> None:
        with pytest.raises(InputValidationError):
            ResourceRef(parent=ProjectRef(project_id="PROJ"), resource_label=bad)

    @pytest.mark.parametrize("label", RESOURCE_URL_RESERVED_LABELS)
    def test_resource_ref_accepts_url_reserved_chars(self, label: str) -> None:
        """'#'/'?'/'%' are routine in real resource labels and get quoted, not rejected.

        Unlike every other ref field: ResourceService.list_file_rows
        constructs a ResourceRef directly from a server-reported label (see
        test_resource_label_with_hash_flows_through_to_a_quoted_request_path
        below), so rejecting these here would break real, already-existing
        resources rather than catching anything hostile.
        """
        ResourceRef(parent=ProjectRef(project_id="PROJ"), resource_label=label)

    @pytest.mark.parametrize("label", LEGAL_LABELS)
    def test_refs_accept_realistic_labels(self, label: str) -> None:
        """Labels with dots/spaces/parens/unicode -- routine in the wild -- still work."""
        ProjectRef(project_id=label)
        SubjectRef(subject=label, project_id="PROJ")
        ExperimentRef(experiment=label, project_id="PROJ")
        ResourceRef(parent=ProjectRef(project_id="PROJ"), resource_label=label)

    def test_leading_trailing_whitespace_rejected(self) -> None:
        with pytest.raises(InputValidationError):
            ProjectRef(project_id=" PROJ")
        with pytest.raises(InputValidationError):
            ProjectRef(project_id="PROJ ")


class TestBuilderCharacterization:
    """Every existing builder path is byte-for-byte unchanged for legal IDs."""

    @pytest.fixture
    def service(self) -> HierarchyService:
        return HierarchyService(MagicMock())

    def test_project_path(self, service: HierarchyService) -> None:
        assert service.build_project_path(ProjectRef(project_id="PROJ1")) == "/data/projects/PROJ1"

    def test_subject_collection_path_with_and_without_project(
        self, service: HierarchyService
    ) -> None:
        assert service.build_subject_collection_path("PROJ1") == "/data/projects/PROJ1/subjects"
        assert service.build_subject_collection_path(None) == "/data/subjects"

    def test_subject_path(self, service: HierarchyService) -> None:
        path = service.build_subject_path(SubjectRef(subject="SUB001", project_id="PROJ"))
        assert path == "/data/projects/PROJ/subjects/SUB001"

    def test_experiment_path_all_three_scopes(self, service: HierarchyService) -> None:
        assert (
            service.build_experiment_path(
                ExperimentRef(experiment="XNAT_E1", project_id="PROJ", subject="SUB1")
            )
            == "/data/projects/PROJ/subjects/SUB1/experiments/XNAT_E1"
        )
        assert (
            service.build_experiment_path(ExperimentRef(experiment="XNAT_E1", project_id="PROJ"))
            == "/data/projects/PROJ/experiments/XNAT_E1"
        )
        assert (
            service.build_experiment_path(ExperimentRef(experiment="XNAT_E1"))
            == "/data/experiments/XNAT_E1"
        )

    def test_scan_path(self, service: HierarchyService) -> None:
        path = service.build_scan_path(
            ScanRef(experiment=ExperimentRef(experiment="XNAT_E1"), scan_id="5")
        )
        assert path == "/data/experiments/XNAT_E1/scans/5"

    def test_resource_path_every_parent_level(self, service: HierarchyService) -> None:
        assert (
            service.build_resource_path(
                ResourceRef(parent=ProjectRef(project_id="PROJ"), resource_label="QA")
            )
            == "/data/projects/PROJ/resources/QA"
        )
        assert (
            service.build_resource_path(
                ResourceRef(
                    parent=SubjectRef(subject="SUB1", project_id="PROJ"), resource_label="QA"
                )
            )
            == "/data/projects/PROJ/subjects/SUB1/resources/QA"
        )
        assert (
            service.build_resource_path(
                ResourceRef(parent=ExperimentRef(experiment="XNAT_E1"), resource_label="QA"),
                "files",
            )
            == "/data/experiments/XNAT_E1/resources/QA/files"
        )
        assert (
            service.build_resource_path(
                ResourceRef(
                    parent=ScanRef(experiment=ExperimentRef(experiment="XNAT_E1"), scan_id="5"),
                    resource_label="DICOM",
                ),
                "files",
            )
            == "/data/experiments/XNAT_E1/scans/5/resources/DICOM/files"
        )

    def test_labels_with_dots_stay_literal_in_data_paths(self, service: HierarchyService) -> None:
        """A dotted subject label round-trips unencoded through /data/ paths."""
        path = service.build_subject_path(SubjectRef(subject="John.Doe", project_id="PROJ"))
        assert path == "/data/projects/PROJ/subjects/John.Doe"

    def test_labels_with_spaces_are_percent_encoded(self, service: HierarchyService) -> None:
        path = service.build_resource_path(
            ResourceRef(parent=ProjectRef(project_id="PROJ"), resource_label="QA (v2)")
        )
        assert path == "/data/projects/PROJ/resources/QA%20%28v2%29"


class TestDownloadFilenameTraversal:
    """A caller-supplied local filename must not escape the output directory."""

    def test_safe_output_path_accepts_a_plain_filename(self, tmp_path: Path) -> None:
        result = _safe_output_path(tmp_path, "custom.zip", "default.zip")
        assert result == tmp_path / "custom.zip"

    def test_safe_output_path_falls_back_to_default(self, tmp_path: Path) -> None:
        result = _safe_output_path(tmp_path, None, "default.zip")
        assert result == tmp_path / "default.zip"

    def test_safe_output_path_rejects_parent_traversal(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        with pytest.raises(PathValidationError):
            _safe_output_path(output_dir, "../../etc/cron.d/evil", "default.zip")

    def test_safe_output_path_rejects_absolute_escape(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        with pytest.raises(PathValidationError):
            _safe_output_path(output_dir, "/etc/passwd", "default.zip")

    def test_download_scans_rejects_hostile_zip_filename_before_any_write(
        self, tmp_path: Path
    ) -> None:
        """A hostile ``zip_filename`` must raise, and nothing lands outside output_dir."""
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="XNAT_E1")
        )
        output_dir = tmp_path / "out"

        with pytest.raises(PathValidationError):
            service.download_scans(
                session_id="XNAT_E1",
                scan_ids=["1"],
                output_dir=output_dir,
                zip_filename="../../evil.zip",
            )

        # The traversal target must not have been created anywhere on disk.
        assert not (tmp_path / "evil.zip").exists()
        assert not (tmp_path.parent / "evil.zip").exists()
        client.stream.assert_not_called()


class TestValidateLocalPathComponent:
    """``validate_local_path_component`` accepts only pass-through identity values.

    A previous version reduced a hostile value to a fallback constant (or to
    ``Path(value).name``) instead of rejecting it -- but that silently
    aliases distinct inputs onto the same local destination (two different
    hostile scan IDs both landing on the same generic name, then
    overwriting each other's files). So now: anything that is not ALREADY
    its own safe form is a hard failure, not a substitution.
    """

    def test_plain_id_passes_through_unchanged(self) -> None:
        assert validate_local_path_component("1", "scan_id") == "1"
        assert validate_local_path_component("DICOM", "resource_label") == "DICOM"

    def test_a_dot_inside_a_name_is_fine(self) -> None:
        """Only a DOT-ONLY component is unsafe; a dot inside a real name is routine."""
        assert validate_local_path_component("John.Doe", "resource_label") == "John.Doe"

    @pytest.mark.parametrize("bad", [".", "..", "..."])
    def test_dot_only_is_rejected(self, bad: str) -> None:
        with pytest.raises(PathValidationError):
            validate_local_path_component(bad, "scan_id")

    @pytest.mark.parametrize("bad", ["/", "/etc/passwd", "../../evil", "a/b"])
    def test_any_value_containing_a_slash_is_rejected_not_reduced(self, bad: str) -> None:
        """Not "reduced to a basename" -- a value whose safe form differs
        from itself fails loudly instead of being silently aliased.
        """
        with pytest.raises(PathValidationError):
            validate_local_path_component(bad, "scan_id")

    def test_windows_style_backslash_path_is_rejected(self) -> None:
        with pytest.raises(PathValidationError):
            validate_local_path_component("C:\\Windows\\System32", "scan_id")

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_empty_or_whitespace_only_is_rejected(self, bad: str) -> None:
        with pytest.raises(PathValidationError):
            validate_local_path_component(bad, "resource_label")

    def test_two_distinct_hostile_values_cannot_alias_onto_the_same_destination(self) -> None:
        """The aliasing bug this replaces: "../../1" and "1" used to both
        reduce to "1". Now the first one simply fails instead of colliding
        with the second.
        """
        assert validate_local_path_component("1", "scan_id") == "1"
        with pytest.raises(PathValidationError):
            validate_local_path_component("../../1", "scan_id")

    @pytest.mark.parametrize("bad", ["C:", "C:escape", "file:stream"])
    def test_a_colon_is_rejected(self, bad: str) -> None:
        """A drive-qualified/drive-relative value has no '/' or '\\' at all,
        so the separator check alone misses it -- but on Windows,
        Path(base) / "C:escape" DISCARDS base entirely (a drive-relative
        path replaces whatever it's joined onto, same as an absolute path
        does), escaping containment before the result is even resolved. Any
        ':' is rejected outright, which also closes the NTFS
        alternate-data-stream form ("file:stream").
        """
        with pytest.raises(PathValidationError):
            validate_local_path_component(bad, "scan_id")

    @pytest.mark.parametrize("bad", ["scan.", ".scan", " scan", "scan ", " scan "])
    def test_leading_or_trailing_dot_or_space_is_rejected(self, bad: str) -> None:
        """A trailing dot/space is silently stripped by Windows filesystem
        APIs -- "scan." and "scan" land on the same real file there, even
        though they're different strings here. Leading is rejected too, for
        the same "looks different, lands the same" aliasing reason.
        """
        with pytest.raises(PathValidationError):
            validate_local_path_component(bad, "scan_id")

    def test_a_dot_or_space_in_the_interior_is_fine(self) -> None:
        """Only LEADING/TRAILING dots and spaces are unsafe -- interior ones
        are routine in real labels (see John.Doe / "QA (v2)" elsewhere).
        """
        assert validate_local_path_component("John.Doe", "resource_label") == "John.Doe"
        assert validate_local_path_component("QA v2", "resource_label") == "QA v2"

    @pytest.mark.parametrize(
        "bad",
        [
            "CON",
            "con",
            "PRN",
            "AUX",
            "NUL",
            "NUL.txt",
            "Nul.tar.gz",
            "COM1",
            "com9",
            "LPT1",
            "lpt9",
        ],
    )
    def test_windows_reserved_device_names_are_rejected(self, bad: str) -> None:
        """CON/PRN/AUX/NUL/COM1-9/LPT1-9 fail to create as a normal
        file/directory on Windows, case-insensitively and regardless of
        extension -- matched against the stem (the part before the first '.').
        """
        with pytest.raises(PathValidationError):
            validate_local_path_component(bad, "scan_id")

    @pytest.mark.parametrize("ok", ["CONSOLE", "COM10", "LPT10", "CONNOR", "AUXILIARY"])
    def test_names_that_merely_start_with_a_reserved_token_are_not_rejected(self, ok: str) -> None:
        """Only an EXACT stem match is reserved -- "CONSOLE" is a real,
        unrelated name, not a disguised "CON".
        """
        assert validate_local_path_component(ok, "scan_id") == ok

    def test_non_nfc_unicode_is_rejected(self) -> None:
        """An NFD-decomposed accented character (e.g. combining-acute "e" +
        U+0301) can be byte-distinct from its NFC form while denoting the
        SAME filename on a normalizing filesystem (macOS/HFS+) -- two values
        that look different here would alias to one real path there.
        """
        nfc = unicodedata.normalize("NFC", "Étude")
        nfd = unicodedata.normalize("NFD", "Étude")
        assert nfc != nfd  # sanity: these really are different strings
        assert validate_local_path_component(nfc, "resource_label") == nfc
        with pytest.raises(PathValidationError):
            validate_local_path_component(nfd, "resource_label")


class TestExtractScanZipLabelSafety:
    """_extract_scan_zip must not let a hostile resource label escape scan_base."""

    @staticmethod
    def _flat_zip(tmp_path: Path, member_name: str = "file.dcm", content: bytes = b"x") -> Path:
        zip_path = tmp_path / "scan.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(member_name, content)
        return zip_path

    def test_hostile_caller_supplied_label_raises_and_writes_nothing_outside_session_dir(
        self, tmp_path: Path
    ) -> None:
        session_dir = tmp_path / "session"
        scan_base = session_dir / "scans" / "1"
        zip_path = self._flat_zip(tmp_path)

        with pytest.raises(PathValidationError):
            _extract_scan_zip(zip_path, scan_base, resource_label="../../../../escape")

        # The label is rejected before any directory is built from it or any
        # file is written -- nothing should have escaped anywhere near
        # tmp_path's parent, and no "escape" directory should exist at all.
        assert not (tmp_path.parent / "escape").exists()
        assert not any(p.name == "escape" for p in tmp_path.rglob("*"))

    def test_legal_label_still_extracts_normally(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "session"
        scan_base = session_dir / "scans" / "1"
        zip_path = self._flat_zip(tmp_path)

        extracted, renamed = _extract_scan_zip(zip_path, scan_base, resource_label="DICOM")

        assert extracted == 1
        assert renamed == 0
        assert (scan_base / "resources" / "DICOM" / "files" / "file.dcm").exists()


class TestVerifierPathQuoting:
    """transfer/verifier.py builds its own REST paths from scan_id/res_label."""

    def test_verify_experiment_quotes_scan_id_and_resource_label(self) -> None:
        source = MagicMock()
        dest = MagicMock()

        def fake_get(path: str, params: dict | None = None) -> MagicMock:
            resp = MagicMock()
            if path.endswith("/scans"):
                resp.json.return_value = {"ResultSet": {"Result": [{"ID": "1 2"}]}}
            elif path.endswith("/resources"):
                resp.json.return_value = {"ResultSet": {"Result": [{"label": "QA #1"}]}}
            else:
                resp.json.return_value = {"ResultSet": {"Result": [{}]}}
            return resp

        source.get.side_effect = fake_get
        dest.get.side_effect = fake_get

        verifier = Verifier(source, dest)
        verifier.verify_experiment("/data/experiments/E1", "/data/experiments/E2")

        # Every request path issued after scan/resource discovery must have
        # the hostile-looking scan id and resource label quoted, never raw.
        all_paths = [c.args[0] for c in source.get.call_args_list + dest.get.call_args_list]
        assert any("1%202" in p for p in all_paths)
        assert not any("/scans/1 2" in p for p in all_paths)
        assert any("QA%20%231" in p for p in all_paths)
        assert not any("QA #1" in p for p in all_paths)


class TestDownloadScansEmptyInput:
    """download_scans rejects an empty batch before any HTTP call or filesystem write."""

    def test_empty_scan_ids_list_is_rejected(self, tmp_path: Path) -> None:
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="E1")
        )

        with pytest.raises(InputValidationError):
            service.download_scans(session_id="E1", scan_ids=[], output_dir=tmp_path)

        client.stream.assert_not_called()
        service._resolve_zip_experiment_ref.assert_not_called()
        # Fails before even creating the output directory -- nothing landed
        # inside tmp_path (which pytest already created for us).
        assert not any(tmp_path.iterdir())

    def test_an_empty_id_within_the_list_is_rejected(self, tmp_path: Path) -> None:
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="E1")
        )

        with pytest.raises(InputValidationError):
            service.download_scans(session_id="E1", scan_ids=["1", ""], output_dir=tmp_path)

        client.stream.assert_not_called()

    def test_a_whitespace_only_id_within_the_list_is_rejected(self, tmp_path: Path) -> None:
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="E1")
        )

        with pytest.raises(InputValidationError):
            service.download_scans(session_id="E1", scan_ids=["1", "   "], output_dir=tmp_path)

        client.stream.assert_not_called()


class TestScanBatchDelimiter:
    """download_scans' multi-scan comma syntax must survive quoting."""

    def test_multiple_scan_ids_joined_with_a_literal_comma(self, tmp_path: Path) -> None:
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="E1")
        )

        captured: dict[str, str] = {}

        def fake_stream(_client, path, *_a, **_kw):
            # download_scans reports a stream failure in the summary rather
            # than raising (it's a batch operation) -- raising here just
            # short-circuits before any real I/O; the path is what matters.
            captured["path"] = path
            raise RuntimeError("stop before any real streaming")

        with patch("xnatctl.services.downloads.stream_to_file", side_effect=fake_stream):
            summary = service.download_scans(
                session_id="E1",
                scan_ids=["1", "2"],
                output_dir=tmp_path,
            )

        assert summary.success is False
        assert captured["path"] == "/data/experiments/E1/scans/1,2/files"

    def test_a_hostile_id_in_the_batch_is_quoted_on_its_own(self, tmp_path: Path) -> None:
        """Each ID is quoted individually -- a hostile one cannot break the batch."""
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="E1")
        )

        captured: dict[str, str] = {}

        def fake_stream(_client, path, *_a, **_kw):
            captured["path"] = path
            raise RuntimeError("stop before any real streaming")

        with patch("xnatctl.services.downloads.stream_to_file", side_effect=fake_stream):
            summary = service.download_scans(
                session_id="E1",
                scan_ids=["1", "a/b"],
                output_dir=tmp_path,
            )

        assert summary.success is False
        assert captured["path"] == "/data/experiments/E1/scans/1,a%2Fb/files"


class TestResourceLabelRoundTrip:
    """Non-mocked proof that a realistic, hash-bearing resource label works end to end."""

    def test_resource_label_with_hash_flows_through_to_a_quoted_request_path(self) -> None:
        client = MagicMock()
        client.get_json.return_value = {"ResultSet": {"Result": []}}
        service = ResourceService(client)

        service.list_file_rows(ProjectRef(project_id="PROJ"), "QA #1")

        path_arg = client.get_json.call_args[0][0]
        assert path_arg == "/data/projects/PROJ/resources/QA%20%231/files"


class TestImportServiceProjectValidation:
    """archive_destination_params validates project even though dest is a param value."""

    def test_hostile_project_is_rejected_before_the_request(self) -> None:
        from xnatctl.services.import_service import archive_destination_params

        with pytest.raises(InputValidationError):
            archive_destination_params("..", direct_archive=False)

    def test_legal_project_is_unaffected(self) -> None:
        from xnatctl.services.import_service import archive_destination_params

        assert archive_destination_params("PROJ1", direct_archive=False) == {
            "dest": "/prearchive/projects/PROJ1"
        }


class TestDeadCodeRemoved:
    """services/base.py's non-quoting _build_path had zero callers; it's gone."""

    def test_build_path_no_longer_exists(self) -> None:
        from xnatctl.services.base import BaseService

        assert not hasattr(BaseService, "_build_path")


class TestEmptyStringDoesNotWidenScope:
    """An explicitly-supplied empty string must not silently degrade to "no filter".

    ``None`` is the deliberate "omit this scope" sentinel throughout this
    package; a caller-supplied empty string is a different thing (almost
    certainly a bug upstream of the call) and must fail loudly rather than
    quietly widening the request to the broader scope ``None`` would have
    meant.
    """

    def test_build_subject_collection_path_rejects_empty_project_id(self) -> None:
        """Was: falls through to /data/subjects -- the SITE-WIDE collection."""
        with pytest.raises(InputValidationError):
            HierarchyService.build_subject_collection_path("")

    def test_build_subject_collection_path_still_allows_none(self) -> None:
        """None is the real "no project filter" -- unaffected."""
        assert HierarchyService.build_subject_collection_path(None) == "/data/subjects"

    def test_build_experiment_collection_path_rejects_empty_subject(self) -> None:
        """Was: falls through to every experiment in the project, ignoring
        the (falsy) subject filter that was explicitly passed as "".
        """
        with pytest.raises(InputValidationError):
            HierarchyService.build_experiment_collection_path("PROJ", "")

    def test_build_experiment_collection_path_rejects_empty_project_id(self) -> None:
        with pytest.raises(InputValidationError):
            HierarchyService.build_experiment_collection_path("", "SUB1")

    def test_build_experiment_collection_path_still_allows_none(self) -> None:
        assert (
            HierarchyService.build_experiment_collection_path("PROJ", None)
            == "/data/projects/PROJ/experiments"
        )

    def test_download_scans_rejects_empty_resource_filter(self, tmp_path: Path) -> None:
        """Was: falls through to the unfiltered (all-resources) request."""
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="E1")
        )

        with pytest.raises(InputValidationError):
            service.download_scans(
                session_id="E1", scan_ids=["1"], output_dir=tmp_path, resource=""
            )

        client.stream.assert_not_called()

    def test_download_resource_rejects_empty_scan_id(self, tmp_path: Path) -> None:
        """Was: falls through to a session-level (unscoped) resource request."""
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="E1")
        )

        with pytest.raises(InputValidationError):
            service.download_resource(
                session_id="E1", resource_label="DICOM", output_dir=tmp_path, scan_id=""
            )

        client.stream.assert_not_called()

    def test_download_session_fast_rejects_an_empty_string_in_include_resources(
        self, tmp_path: Path
    ) -> None:
        """Was: the tuple ("",) is truthy, turning on the include-filter
        branch, and the per-item check then fell through to UNFILTERED.
        """
        client = MagicMock()
        service = DownloadService(client)

        with pytest.raises(InputValidationError):
            service.download_session_fast(
                session_project="P",
                subject="S",
                resolved_session_id="E1",
                session_dir=tmp_path,
                include_resources=("",),
            )

        client.stream.assert_not_called()

    def test_download_session_fast_rejects_an_empty_string_in_exclude_resources(
        self, tmp_path: Path
    ) -> None:
        client = MagicMock()
        service = DownloadService(client)

        with pytest.raises(InputValidationError):
            service.download_session_fast(
                session_project="P",
                subject="S",
                resolved_session_id="E1",
                session_dir=tmp_path,
                exclude_resources=("   ",),
            )

        client.stream.assert_not_called()

    def test_download_session_fast_accepts_a_real_include_filter(self, tmp_path: Path) -> None:
        """The guard must not reject legitimate, non-empty filter values."""
        client = MagicMock()
        client.get_json.return_value = {"ResultSet": {"Result": []}}
        service = DownloadService(client)

        outcome = service.download_session_fast(
            session_project="P",
            subject="S",
            resolved_session_id="E1",
            session_dir=tmp_path,
            include_resources=("DICOM",),
        )

        assert outcome.succeeded == 0  # no scans discovered; the guard just didn't block it

    def test_build_verification_manifest_rejects_an_empty_string_in_include_resources(
        self, tmp_path: Path
    ) -> None:
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="E1")
        )

        with pytest.raises(InputValidationError):
            service.build_verification_manifest(
                session_id="E1", project="P", include_resources=("",)
            )

    def test_build_verification_manifest_rejects_an_empty_resource_filter(
        self, tmp_path: Path
    ) -> None:
        """resource_filter="" used to widen verification to every resource on the scan."""
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="E1")
        )

        with pytest.raises(InputValidationError):
            service.build_verification_manifest(
                session_id="E1", project="P", scan_ids=["1"], resource_filter=""
            )


class TestResourceServiceEmptyScanIdWidening:
    """ResourceService._resolve_parent must not widen scan_id="" to session scope.

    This is the destructive edition of the empty-string-widening bug:
    ResourceService.delete() defaults remove_files=True, so a caller who
    meant to delete one scan's resource and passed an accidentally-empty
    scan_id would previously have deleted the whole SESSION's resource
    instead.
    """

    def test_delete_with_empty_scan_id_raises_before_any_http_call(self) -> None:
        client = MagicMock()
        service = ResourceService(client)

        with pytest.raises(InputValidationError):
            service.delete(session_id="E1", resource_label="DICOM", scan_id="")

        client.delete.assert_not_called()

    def test_delete_still_works_with_scan_id_omitted(self) -> None:
        """None (the real "no scan scope") must still work -- session-level delete."""
        client = MagicMock()
        service = ResourceService(client)

        service.delete(session_id="E1", resource_label="DICOM", scan_id=None)

        client.delete.assert_called_once()
        path_arg = client.delete.call_args[0][0]
        assert "scans" not in path_arg

    def test_delete_still_works_with_a_real_scan_id(self) -> None:
        client = MagicMock()
        service = ResourceService(client)

        service.delete(session_id="E1", resource_label="DICOM", scan_id="1")

        client.delete.assert_called_once()
        path_arg = client.delete.call_args[0][0]
        assert "/scans/1/" in path_arg

    def test_list_with_empty_scan_id_raises(self) -> None:
        client = MagicMock()
        service = ResourceService(client)

        with pytest.raises(InputValidationError):
            service.list(session_id="E1", scan_id="")

        client.get.assert_not_called()


class TestServicesEmptyStringPathRoutingWidening:
    """A sweep of services/ sites where "" used to silently widen scope.

    Each fixed function toggles between two DIFFERENT REST routes based on
    whether an optional identifier was supplied; "" used to take the same
    branch as omitted (None), landing on the wider/unscoped route.
    """

    def test_session_service_get_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = SessionService(client)
        with pytest.raises(InputValidationError):
            service.get("E1", project="")
        client.get.assert_not_called()

    def test_session_service_delete_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = SessionService(client)
        with pytest.raises(InputValidationError):
            service.delete("E1", project="")
        client.delete.assert_not_called()

    def test_session_service_list_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = SessionService(client)
        with pytest.raises(InputValidationError):
            service.list(project="")
        client.get.assert_not_called()

    def test_pipeline_service_list_jobs_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = PipelineService(client)
        with pytest.raises(InputValidationError):
            service.list_jobs(project="")
        client.get.assert_not_called()

    def test_pipeline_service_list_jobs_rejects_empty_experiment_id(self) -> None:
        client = MagicMock()
        service = PipelineService(client)
        with pytest.raises(InputValidationError):
            service.list_jobs(experiment_id="")
        client.get.assert_not_called()

    def test_pipeline_service_list_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = PipelineService(client)
        with pytest.raises(InputValidationError):
            service.list(project="")
        client.get.assert_not_called()

    def test_prearchive_service_list_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = PrearchiveService(client)
        with pytest.raises(InputValidationError):
            service.list(project="")
        client.get.assert_not_called()

    def test_subject_service_delete_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = SubjectService(client)
        with pytest.raises(InputValidationError):
            service.delete("SUB1", project="")
        client.delete.assert_not_called()

    def test_subject_service_rename_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = SubjectService(client)
        with pytest.raises(InputValidationError):
            service.rename("SUB1", "NEW_LABEL", project="")
        client.put.assert_not_called()

    def test_subject_service_get_sessions_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = SubjectService(client)
        with pytest.raises(InputValidationError):
            service.get_sessions("SUB1", project="")
        client.get.assert_not_called()

    def test_admin_service_list_users_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = AdminService(client)
        with pytest.raises(InputValidationError):
            service.list_users(project="")
        client.get.assert_not_called()


class TestTransferLocalPathJoins:
    """Cross-server transfer staging must validate server-reported values
    used as local path components under work_dir, same as DownloadService.
    """

    def test_download_resource_rejects_hostile_resource_label(self, tmp_path: Path) -> None:
        """A drive-qualified label under work_dir would escape it on Windows."""
        source = MagicMock()
        dest = MagicMock()
        executor = TransferExecutor(source, dest)

        with pytest.raises(PathValidationError):
            executor.download_resource(
                source_path="/data/experiments/E1/resources/DICOM/files",
                resource_label="C:escape",
                work_dir=tmp_path,
            )

        source.stream.assert_not_called()

    def test_download_resource_still_works_with_a_legal_label(self, tmp_path: Path) -> None:
        source = MagicMock()
        dest = MagicMock()
        executor = TransferExecutor(source, dest)

        with patch(
            "xnatctl.services.transfer.executor.stream_to_file",
            return_value=MagicMock(bytes_written=0),
        ):
            with patch.object(executor, "validate_zip", return_value=True):
                with patch.object(executor, "_flatten_zip"):
                    flat_path, total = executor.download_resource(
                        source_path="/data/experiments/E1/resources/DICOM/files",
                        resource_label="DICOM",
                        work_dir=tmp_path,
                    )

        assert flat_path == tmp_path / "DICOM_flat.zip"

    def test_download_scan_dicom_rejects_hostile_scan_id(self, tmp_path: Path) -> None:
        source = MagicMock()
        dest = MagicMock()
        executor = TransferExecutor(source, dest)

        with pytest.raises(PathValidationError):
            executor.download_scan_dicom(
                source_experiment_id="E1",
                scan_id="C:escape",
                work_dir=tmp_path,
            )

        source.stream.assert_not_called()


class TestDownloadResourceExtractionRootSafety:
    """DownloadService.download_resource(extract=True) builds its extraction
    root straight from the resource label -- a Windows drive-qualified label
    must not reach that join unvalidated.
    """

    def test_hostile_label_rejected_before_extraction(self, tmp_path: Path) -> None:
        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="E1")
        )

        with pytest.raises(PathValidationError):
            service.download_resource(
                session_id="E1",
                resource_label="C:escape",
                output_dir=tmp_path,
            )

        client.stream.assert_not_called()


class TestCliNameValidation:
    """`session download --name` / `scan download --name` route through
    validate_local_path_component now, not a separator-only ad hoc check.

    Both tests deliberately use a bare, un-mocked ``CliRunner()`` with
    ``no_ambient_credentials`` rather than the harness's usual
    authenticated fixtures: the point being verified is that an invalid
    --name is rejected by an eager Click callback that runs during argument
    parsing, BEFORE @require_auth ever executes -- so it must fail the same
    way with zero credentials available, not just when auth happens to
    succeed. Authenticating first (via authenticated_seams/authenticated_cli)
    would make this indistinguishable from validation running inside the
    command body after a successful login.
    """

    # TEMPORARY: the eager-callback product fix (moving --name validation
    # into a Click option callback that runs before @require_auth) is on
    # hold in cli/session.py / cli/scan.py while another agent edits those
    # same files for an unrelated feature. Without it, these two tests
    # correctly reproduce the CI failure (auth fails before --name is ever
    # validated) instead of passing. xfail(strict=True) so the suite stays
    # green now and fails loudly -- forcing this marker's removal -- the
    # moment the product fix lands and the test starts passing again.
    @pytest.mark.xfail(strict=True, reason="name validation requires auth-free path")
    def test_session_download_rejects_windows_invalid_name(
        self, no_ambient_credentials: Path
    ) -> None:
        from click.testing import CliRunner

        from xnatctl.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "session",
                "download",
                "-E",
                "XNAT_E00001",
                "--out",
                ".",
                "--name",
                "evil:name",
            ],
        )
        assert result.exit_code != 0
        assert "evil:name" in result.output
        assert "Not authenticated" not in result.output

    @pytest.mark.xfail(strict=True, reason="name validation requires auth-free path")
    def test_scan_download_rejects_windows_invalid_name(self, no_ambient_credentials: Path) -> None:
        from click.testing import CliRunner

        from xnatctl.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "scan",
                "download",
                "-E",
                "XNAT_E00001",
                "-s",
                "1",
                "--out",
                ".",
                "--name",
                "evil:name",
            ],
        )
        assert result.exit_code != 0
        assert "evil:name" in result.output
        assert "Not authenticated" not in result.output


class TestValidateLocalPathComponentWindowsInvalidChars:
    """Windows filename-invalid characters -- rejected on every platform."""

    @pytest.mark.parametrize("bad_char", list('<>"|?*'))
    def test_each_windows_invalid_char_is_rejected(self, bad_char: str) -> None:
        with pytest.raises(PathValidationError):
            validate_local_path_component(f"scan{bad_char}1", "scan_id")

    def test_legal_value_with_none_of_them_is_unaffected(self) -> None:
        assert validate_local_path_component("scan_1-2.3", "scan_id") == "scan_1-2.3"


class TestCasefoldCollisionGuard:
    """check_no_casefold_collision -- individually-valid values that would
    still collide as siblings on a case-insensitive filesystem.
    """

    def test_first_occurrence_is_accepted(self) -> None:
        seen: set[str] = set()
        check_no_casefold_collision("scan", seen, "scan_id")
        assert seen == {"scan"}

    def test_a_case_variant_of_an_already_seen_value_raises(self) -> None:
        seen: set[str] = set()
        check_no_casefold_collision("scan", seen, "scan_id")
        with pytest.raises(PathValidationError):
            check_no_casefold_collision("SCAN", seen, "scan_id")

    def test_distinct_values_do_not_collide(self) -> None:
        seen: set[str] = set()
        check_no_casefold_collision("scan1", seen, "scan_id")
        check_no_casefold_collision("scan2", seen, "scan_id")
        assert seen == {"scan1", "scan2"}

    def test_download_session_fast_rejects_case_colliding_scan_ids(self, tmp_path: Path) -> None:
        """Two scans "1a" and "1A" would extract into the same directory
        on a case-insensitive filesystem.
        """
        client = MagicMock()
        service = DownloadService(client)

        with patch(
            "xnatctl.services.downloads.SessionService.scan_rows",
            return_value=[{"ID": "1a"}, {"ID": "1A"}],
        ):
            with pytest.raises(PathValidationError):
                service.download_session_fast(
                    session_project="P",
                    subject="S",
                    resolved_session_id="E1",
                    session_dir=tmp_path,
                )

        client.stream.assert_not_called()

    def test_download_session_level_resources_rejects_case_colliding_labels(
        self, tmp_path: Path
    ) -> None:
        """Two resources "QC" and "qc" would produce the same local ZIP name.

        The first resource in the list has already streamed by the time the
        second one's collision is detected (the check runs per-resource, in
        order) -- what matters is that the second one never does.
        """
        client = MagicMock()
        service = DownloadService(client)

        with patch(
            "xnatctl.services.downloads.SessionService.experiment_resource_rows",
            return_value=[{"label": "QC"}, {"label": "qc"}],
        ):
            with pytest.raises(PathValidationError):
                service.download_session_level_resources(
                    session_project="P",
                    subject="S",
                    resolved_session_id="E1",
                    session_dir=tmp_path,
                )

        assert client.stream.call_count == 1
        first_call_path = client.stream.call_args_list[0].args[1]
        assert "/resources/QC/" in first_call_path


class TestQueryParamFilterEmptyStringWidening:
    """Query-param filters (admin audit endpoints, session subject filter)
    must not let an explicitly-empty string silently widen the result set,
    same as the path-routing sweep -- lower severity (a wider read, not a
    different route), same fix shape.
    """

    def test_audit_log_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = AdminService(client)
        with pytest.raises(InputValidationError):
            service.audit_log(project="")
        client.get.assert_not_called()

    def test_audit_log_rejects_empty_username(self) -> None:
        client = MagicMock()
        service = AdminService(client)
        with pytest.raises(InputValidationError):
            service.audit_log(username="")
        client.get.assert_not_called()

    def test_audit_log_rejects_whitespace_only_action(self) -> None:
        client = MagicMock()
        service = AdminService(client)
        with pytest.raises(InputValidationError):
            service.audit_log(action="   ")
        client.get.assert_not_called()

    def test_audit_log_still_works_with_a_real_filter(self) -> None:
        client = MagicMock()
        service = AdminService(client)
        service._get = MagicMock(return_value={})

        service.audit_log(project="PROJ1")

        called_params = service._get.call_args[1]["params"]
        assert called_params["project"] == "PROJ1"

    def test_audit_log_omitted_filter_still_means_no_filter(self) -> None:
        client = MagicMock()
        service = AdminService(client)
        service._get = MagicMock(return_value={})

        service.audit_log()

        called_params = service._get.call_args[1]["params"]
        assert "project" not in called_params

    def test_get_xapi_audit_rejects_empty_project(self) -> None:
        client = MagicMock()
        service = AdminService(client)
        with pytest.raises(InputValidationError):
            service.get_xapi_audit(limit=10, project="")
        client.get_json.assert_not_called()

    def test_get_xapi_audit_rejects_empty_username(self) -> None:
        client = MagicMock()
        service = AdminService(client)
        with pytest.raises(InputValidationError):
            service.get_xapi_audit(limit=10, username="")
        client.get_json.assert_not_called()

    def test_get_xapi_audit_rejects_empty_action(self) -> None:
        client = MagicMock()
        service = AdminService(client)
        with pytest.raises(InputValidationError):
            service.get_xapi_audit(limit=10, action="")
        client.get_json.assert_not_called()

    def test_list_project_experiment_rows_rejects_empty_subject(self) -> None:
        client = MagicMock()
        service = SessionService(client)
        with pytest.raises(InputValidationError):
            service.list_project_experiment_rows("PROJ1", subject="")
        client.get_json.assert_not_called()

    def test_list_project_experiment_rows_still_works_with_a_real_subject(self) -> None:
        client = MagicMock()
        client.get_json.return_value = {"ResultSet": {"Result": []}}
        service = SessionService(client)

        service.list_project_experiment_rows("PROJ1", subject="SUB1")

        called_params = client.get_json.call_args[1]["params"]
        assert called_params["subject_label"] == "SUB1"

    def test_list_project_experiment_rows_omitted_subject_still_means_no_filter(self) -> None:
        client = MagicMock()
        client.get_json.return_value = {"ResultSet": {"Result": []}}
        service = SessionService(client)

        service.list_project_experiment_rows("PROJ1")

        called_params = client.get_json.call_args[1]["params"]
        assert "subject_label" not in called_params


class TestExtractScanZipCasefoldCollision:
    """_extract_scan_zip must not merge two case-differing resource labels
    within one ZIP -- the third case-aliasing site, using a casefold ->
    literal map since (unlike the batch sites) the SAME literal label
    legitimately recurs across many members of one resource.
    """

    @staticmethod
    def _zip_with_members(tmp_path: Path, members: dict[str, bytes]) -> Path:
        zip_path = tmp_path / "scan.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, content in members.items():
                zf.writestr(name, content)
        return zip_path

    def test_same_literal_label_repeating_is_fine(self, tmp_path: Path) -> None:
        zip_path = self._zip_with_members(
            tmp_path,
            {
                "EXP/scans/1/resources/DICOM/files/a.dcm": b"a",
                "EXP/scans/1/resources/DICOM/files/b.dcm": b"b",
            },
        )
        scan_base = tmp_path / "scan_dir"

        extracted, renamed = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 2
        assert renamed == 0
        assert (scan_base / "resources" / "DICOM" / "files" / "a.dcm").exists()
        assert (scan_base / "resources" / "DICOM" / "files" / "b.dcm").exists()

    def test_case_differing_literal_labels_raise(self, tmp_path: Path) -> None:
        """A "DICOM" then a "dicom" label -- individually valid, but a
        case-insensitive filesystem would merge them into one directory.
        """
        zip_path = self._zip_with_members(
            tmp_path,
            {
                "EXP/scans/1/resources/DICOM/files/a.dcm": b"a",
                "EXP/scans/1/resources/dicom/files/b.dcm": b"b",
            },
        )
        scan_base = tmp_path / "scan_dir"

        with pytest.raises(PathValidationError):
            _extract_scan_zip(zip_path, scan_base)

    def test_distinct_labels_do_not_collide(self, tmp_path: Path) -> None:
        zip_path = self._zip_with_members(
            tmp_path,
            {
                "EXP/scans/1/resources/DICOM/files/a.dcm": b"a",
                "EXP/scans/1/resources/SNAPSHOTS/files/b.jpg": b"b",
            },
        )
        scan_base = tmp_path / "scan_dir"

        extracted, renamed = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 2
        assert (scan_base / "resources" / "DICOM" / "files" / "a.dcm").exists()
        assert (scan_base / "resources" / "SNAPSHOTS" / "files" / "b.jpg").exists()


class TestScanTransferCaseCollision:
    """Cross-server transfer staging must preflight/check the same casefold
    collisions as DownloadService, at both the scan-batch and per-scan-resource
    levels.
    """

    @staticmethod
    def _scan_transfer(scan_workers: int = 4) -> tuple[ScanTransfer, MagicMock, MagicMock]:
        source = MagicMock()
        dest = MagicMock()
        executor = TransferExecutor(source, dest)
        filter_engine = MagicMock()
        filter_engine.should_include_scan.return_value = True
        filter_engine.should_include_scan_resource.return_value = True
        config = MagicMock()
        config.scan_workers = scan_workers
        return ScanTransfer(executor, filter_engine, config), source, dest

    def test_batch_preflight_rejects_case_colliding_scan_ids(self, tmp_path: Path) -> None:
        """Two scans "1a"/"1A" would share a scan_{id} staging dir once
        concurrent download workers write into them -- caught before the
        pool (or any HTTP call) starts.
        """
        scan_transfer, source, _dest = self._scan_transfer()
        exp = MagicMock(xsi_type="xnat:mrSessionData", local_id="E1", local_label="EXP1")
        subject = MagicMock(local_label="SUB1")
        result = TransferResult()
        scans = [{"ID": "1a", "type": "T1"}, {"ID": "1A", "type": "T1"}]

        with pytest.raises(PathValidationError):
            scan_transfer.transfer_scans(scans, exp, "DEST_PROJ", subject, tmp_path, result)

        source.get.assert_not_called()

    def test_batch_preflight_accepts_distinct_scan_ids(self, tmp_path: Path) -> None:
        """The preflight itself must not reject a normal, non-colliding batch."""
        scan_transfer, _source, _dest = self._scan_transfer()
        exp = MagicMock(xsi_type="xnat:mrSessionData", local_id="E1", local_label="EXP1")
        subject = MagicMock(local_label="SUB1")
        result = TransferResult()
        scans = [{"ID": "1", "type": "T1"}, {"ID": "2", "type": "T1"}]

        with patch.object(
            scan_transfer, "_download_scan_task", return_value=("1", [], "")
        ) as mock_task:
            scan_transfer.transfer_scans(scans, exp, "DEST_PROJ", subject, tmp_path, result)

        assert mock_task.call_count == 2

    def test_per_scan_resource_labels_reject_case_collision(self, tmp_path: Path) -> None:
        """Two resources "QA"/"qa" on the SAME scan would stage the same
        local ZIP name -- since every resource downloads before any
        uploads, the second would silently overwrite the first. Caught per
        scan, not per batch, so it fails only that scan (recorded in
        result.errors / resources_failed), not the whole transfer.
        """
        scan_transfer, _source, _dest = self._scan_transfer(scan_workers=1)
        exp = MagicMock(xsi_type="xnat:mrSessionData", local_id="E1", local_label="EXP1")
        subject = MagicMock(local_label="SUB1")
        result = TransferResult()
        scans = [{"ID": "1", "type": "T1"}]

        with (
            patch.object(
                scan_transfer.executor,
                "discover_scan_resources",
                return_value=[
                    {"label": "QA", "format": "OTHER"},
                    {"label": "qa", "format": "OTHER"},
                ],
            ),
            patch.object(scan_transfer, "_ensure_scan_shell"),
            # The first resource ("QA") must succeed cleanly so the second
            # ("qa") is what actually reaches (and trips) the collision
            # check -- no real download/ZIP handling needed for that.
            patch.object(scan_transfer, "_download_scan_resource", return_value=MagicMock()),
        ):
            scan_transfer.transfer_scans(
                scans, exp, "DEST_PROJ", subject, tmp_path, result, dicom_only=False
            )

        assert any("resource label" in e for e in result.errors)


class TestDiscoveryLastSyncTimeEmptyStringWidening:
    """An empty last_sync_time used to silently trigger a full resync."""

    def test_discover_subjects_rejects_empty_last_sync_time(self) -> None:
        client = MagicMock()
        service = DiscoveryService(client)
        with pytest.raises(InputValidationError):
            service.discover_subjects("PROJ1", last_sync_time="")
        client.get.assert_not_called()

    def test_discover_experiments_rejects_empty_last_sync_time(self) -> None:
        client = MagicMock()
        service = DiscoveryService(client)
        with pytest.raises(InputValidationError):
            service.discover_experiments("PROJ1", "SUB1", last_sync_time="")
        client.get.assert_not_called()

    def test_discover_subjects_still_allows_none(self) -> None:
        client = MagicMock()
        client.get.return_value = MagicMock(json=lambda: {"ResultSet": {"Result": []}})
        service = DiscoveryService(client)
        assert service.discover_subjects("PROJ1", last_sync_time=None) == []


class TestSymlinkedRootEscape:
    """A pre-existing symlink at the identifier-joined path must not let
    extraction land outside the caller-supplied trusted root.
    """

    def test_verify_directory_contained_in_accepts_a_normal_subdirectory(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "out"
        root.mkdir()
        candidate = root / "DICOM"
        resolved = verify_directory_contained_in(candidate, root, "extraction directory")
        assert resolved == candidate.resolve()

    def test_verify_directory_contained_in_rejects_a_symlink_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "out"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        escape = root / "DICOM"
        os.symlink(outside, escape)

        with pytest.raises(PathValidationError):
            verify_directory_contained_in(escape, root, "extraction directory")

    def test_download_resource_extract_rejects_a_pre_existing_symlink(self, tmp_path: Path) -> None:
        """output_dir/DICOM is a symlink to outside -- containment inside
        zip_extract would otherwise anchor to (and pass trivially for) the
        escaped location.
        """
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, output_dir / "DICOM")

        client = MagicMock()
        service = DownloadService(client)
        service._resolve_zip_experiment_ref = MagicMock(  # type: ignore[method-assign]
            return_value=ExperimentRef(experiment="E1")
        )

        def fake_stream(_client, _path, dest, **_kw):
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("a.dcm", "x")
            return MagicMock(bytes_written=1)

        with patch("xnatctl.services.downloads.stream_to_file", side_effect=fake_stream):
            with pytest.raises(PathValidationError):
                service.download_resource(
                    session_id="E1",
                    resource_label="DICOM",
                    output_dir=output_dir,
                    extract=True,
                )

        # Nothing extracted into the escaped, outside location.
        assert not any(outside.iterdir())

    def test_download_session_fast_scan_extraction_rejects_a_pre_existing_symlink(
        self, tmp_path: Path
    ) -> None:
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        scans_dir = session_dir / "scans"
        scans_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, scans_dir / "1")

        client = MagicMock()
        service = DownloadService(client)

        def fake_stream(_client, _path, dest, **_kw):
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("EXP/scans/1/resources/DICOM/files/a.dcm", "x")

        with (
            patch(
                "xnatctl.services.downloads.SessionService.scan_rows",
                return_value=[{"ID": "1"}],
            ),
            patch("xnatctl.services.downloads.stream_to_file", side_effect=fake_stream),
        ):
            outcome = service.download_session_fast(
                session_project="P",
                subject="S",
                resolved_session_id="E1",
                session_dir=session_dir,
                workers=1,
            )

        # The scan failed (symlink escape rejected), not silently escaped.
        assert outcome.succeeded == 0
        assert len(outcome.failed) == 1
        assert not any(outside.iterdir())


class TestZipFilenameComponentValidation:
    """zip_filename must route through validate_local_path_component, not
    just the containment-only check.
    """

    def test_explicit_empty_string_raises_not_defaults(self, tmp_path: Path) -> None:
        with pytest.raises(PathValidationError):
            _safe_output_path(tmp_path, "", "default.zip")

    def test_none_still_uses_the_default(self, tmp_path: Path) -> None:
        assert _safe_output_path(tmp_path, None, "default.zip") == tmp_path / "default.zip"

    def test_a_subdirectory_bearing_value_is_still_legal(self, tmp_path: Path) -> None:
        result = _safe_output_path(tmp_path, "sub/dir.zip", "default.zip")
        assert result == tmp_path / "sub" / "dir.zip"

    def test_a_drive_qualified_component_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PathValidationError):
            _safe_output_path(tmp_path, "C:escape.zip", "default.zip")

    def test_a_reserved_windows_name_component_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PathValidationError):
            _safe_output_path(tmp_path, "sub/CON.zip", "default.zip")

    def test_an_empty_component_from_a_leading_slash_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PathValidationError):
            _safe_output_path(tmp_path, "/etc/passwd", "default.zip")

    def test_an_empty_component_from_a_trailing_slash_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PathValidationError):
            _safe_output_path(tmp_path, "sub/", "default.zip")

    def test_a_dot_dot_component_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PathValidationError):
            _safe_output_path(tmp_path, "../evil.zip", "default.zip")


class TestValidateLocalPathComponentControlChars:
    """C0 control characters (including NUL) are rejected, same as the
    label validators already reject them.
    """

    @pytest.mark.parametrize("bad", ["scan\x00id", "scan\x01id", "scan\x1fid", "scan\tid"])
    def test_control_characters_are_rejected(self, bad: str) -> None:
        with pytest.raises(PathValidationError):
            validate_local_path_component(bad, "scan_id")

    def test_normal_value_is_unaffected(self) -> None:
        assert validate_local_path_component("scan_1", "scan_id") == "scan_1"


class TestMoreEmptyStringWideningStragglers:
    """The remaining named truthiness stragglers, plus the `limit` sweep."""

    def test_sessions_list_rejects_subject_without_project(self) -> None:
        """Subject given but project None used to silently IGNORE subject
        and issue a site-wide query -- XNAT subject labels aren't globally
        unique without a project to scope them.
        """
        client = MagicMock()
        service = SessionService(client)
        with pytest.raises(InputValidationError):
            service.list(subject="SUB1")
        client.get.assert_not_called()

    def test_sessions_list_rejects_empty_modality(self) -> None:
        client = MagicMock()
        service = SessionService(client)
        with pytest.raises(InputValidationError):
            service.list(modality="")
        client.get.assert_not_called()

    def test_download_service_resolve_rejects_empty_project(self) -> None:
        """project="" used to be treated as omitted during resolution,
        silently using session_id AS an accession ID rather than resolving
        a label through the (rejected) empty project.
        """
        client = MagicMock()
        service = DownloadService(client)
        with pytest.raises(InputValidationError):
            service._resolve_zip_experiment_ref("SESSION_LABEL", project="")
        client.get.assert_not_called()

    def test_admin_get_site_config_rejects_empty_key(self) -> None:
        client = MagicMock()
        service = AdminService(client)
        with pytest.raises(InputValidationError):
            service.get_site_config(key="")
        client.get.assert_not_called()

    def test_admin_get_site_config_none_still_returns_everything(self) -> None:
        client = MagicMock()
        service = AdminService(client)
        service._get = MagicMock(return_value={})
        service.get_site_config(key=None)
        assert service._get.call_args[0][0] == "/xapi/siteConfig"

    def test_pipeline_list_jobs_rejects_empty_status(self) -> None:
        client = MagicMock()
        service = PipelineService(client)
        with pytest.raises(InputValidationError):
            service.list_jobs(status="")
        client.get.assert_not_called()

    def test_prearchive_archive_rejects_empty_subject(self) -> None:
        client = MagicMock()
        service = PrearchiveService(client)
        with pytest.raises(InputValidationError):
            service.archive("PROJ1", "20260101_120000", "session1", subject="")
        client.post.assert_not_called()

    def test_prearchive_archive_rejects_empty_experiment_label(self) -> None:
        client = MagicMock()
        service = PrearchiveService(client)
        with pytest.raises(InputValidationError):
            service.archive(
                "PROJ1", "20260101_120000", "session1", subject="SUB1", experiment_label=""
            )
        client.post.assert_not_called()

    def test_prearchive_archive_still_works_with_a_real_subject(self) -> None:
        client = MagicMock()
        # `_post` reads `.headers`/`.text` off the response like a real
        # httpx.Response -- a plain dict return value fails before the
        # code under test is even reached.
        client.post.return_value = MagicMock(headers={}, text="")
        service = PrearchiveService(client)
        service.archive("PROJ1", "20260101_120000", "session1", subject="SUB1")
        client.post.assert_called_once()

    @pytest.mark.parametrize(
        "service_factory,method,kwargs",
        [
            (lambda c: AdminService(c), "refresh_catalogs", {"project": "P", "limit": 0}),
            (lambda c: ProjectService(c), "list", {"limit": 0}),
            (lambda c: SubjectService(c), "list", {"limit": 0}),
            (lambda c: SessionService(c), "list", {"limit": 0}),
        ],
    )
    def test_limit_zero_means_zero_not_unlimited(self, service_factory, method, kwargs) -> None:
        client = MagicMock()
        client.get.return_value = MagicMock(
            json=lambda: {"ResultSet": {"Result": [{"ID": "1"}, {"ID": "2"}]}}
        )
        service = service_factory(client)
        result = getattr(service, method)(**kwargs)
        if isinstance(result, dict):
            assert result.get("total") == 0
        else:
            assert result == []


class TestZipExtractExclusionBeforeValidation:
    """An explicitly EXCLUDED resource must never be validated or
    casefold-registered -- it is never extracted, so a locally-unsafe or
    case-colliding label on it must not fail the whole ZIP for content
    that is never written.
    """

    @staticmethod
    def _zip_with_members(tmp_path: Path, members: dict[str, bytes]) -> Path:
        zip_path = tmp_path / "scan.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, content in members.items():
                zf.writestr(name, content)
        return zip_path

    def test_excluded_resource_with_unsafe_label_does_not_raise(self, tmp_path: Path) -> None:
        """The label "QA?1" is not a safe local path component -- but it is
        excluded, so it must never reach validation.
        """
        zip_path = self._zip_with_members(
            tmp_path,
            {
                "EXP/scans/1/resources/QA?1/files/bad.txt": b"x",
                "EXP/scans/1/resources/DICOM/files/a.dcm": b"a",
            },
        )
        scan_base = tmp_path / "scan_dir"

        extracted, renamed = _extract_scan_zip(
            zip_path, scan_base, exclude_resources=frozenset({"QA?1"})
        )

        assert extracted == 1
        assert renamed == 0
        assert (scan_base / "resources" / "DICOM" / "files" / "a.dcm").exists()
        assert not (scan_base / "resources" / "QA?1").exists()

    def test_excluded_case_variant_does_not_trip_false_collision(self, tmp_path: Path) -> None:
        """The label "DICOM" is extracted; "dicom" (a case-variant that
        would otherwise collide with it) is excluded -- exclusion must be
        checked before the excluded label is ever registered in the
        casefold map.
        """
        zip_path = self._zip_with_members(
            tmp_path,
            {
                "EXP/scans/1/resources/DICOM/files/a.dcm": b"a",
                "EXP/scans/1/resources/dicom/files/b.dcm": b"b",
            },
        )
        scan_base = tmp_path / "scan_dir"

        extracted, renamed = _extract_scan_zip(
            zip_path, scan_base, exclude_resources=frozenset({"dicom"})
        )

        assert extracted == 1
        assert (scan_base / "resources" / "DICOM" / "files" / "a.dcm").exists()
        assert not (scan_base / "resources" / "dicom").exists()

    def test_explicit_empty_resource_label_raises(self, tmp_path: Path) -> None:
        """resource_label="" is a caller mistake, not "no override" -- it
        must not silently fall through to the per-member detected label
        (or "UNKNOWN"). ``None`` still means "no override".
        """
        zip_path = self._zip_with_members(
            tmp_path, {"EXP/scans/1/resources/DICOM/files/a.dcm": b"a"}
        )
        scan_base = tmp_path / "scan_dir"

        with pytest.raises(PathValidationError):
            _extract_scan_zip(zip_path, scan_base, resource_label="")

    def test_none_resource_label_still_falls_back_to_detected(self, tmp_path: Path) -> None:
        zip_path = self._zip_with_members(
            tmp_path, {"EXP/scans/1/resources/DICOM/files/a.dcm": b"a"}
        )
        scan_base = tmp_path / "scan_dir"

        extracted, _ = _extract_scan_zip(zip_path, scan_base, resource_label=None)

        assert extracted == 1
        assert (scan_base / "resources" / "DICOM" / "files" / "a.dcm").exists()


class TestListSessionsModalityEmptyStringWidening:
    """SessionService.list_sessions(modality="") must raise, matching
    list()'s own modality filter -- the earlier fix only reached list(),
    not this separate classified-rows path used by ``session list``.
    """

    def test_empty_modality_raises(self) -> None:
        client = MagicMock()
        service = SessionService(client)
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "E1",
                        "label": "S1",
                        "subject_label": "SUB1",
                        "xsiType": "xnat:mrSessionData",
                    },
                ]
            }
        }
        with pytest.raises(InputValidationError):
            service.list_sessions("PROJ1", modality="")
        # The guard runs before the fetch -- a bad modality value must not
        # cost an HTTP request.
        client.get_json.assert_not_called()

    def test_none_modality_returns_everything(self) -> None:
        client = MagicMock()
        service = SessionService(client)
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "E1",
                        "label": "S1",
                        "subject_label": "SUB1",
                        "xsiType": "xnat:mrSessionData",
                    },
                    {
                        "ID": "E2",
                        "label": "S2",
                        "subject_label": "SUB2",
                        "xsiType": "xnat:petSessionData",
                    },
                ]
            }
        }
        result = service.list_sessions("PROJ1", modality=None)
        assert len(result) == 2

    def test_real_modality_still_filters(self) -> None:
        client = MagicMock()
        service = SessionService(client)
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "E1",
                        "label": "S1",
                        "subject_label": "SUB1",
                        "xsiType": "xnat:mrSessionData",
                    },
                    {
                        "ID": "E2",
                        "label": "S2",
                        "subject_label": "SUB2",
                        "xsiType": "xnat:petSessionData",
                    },
                ]
            }
        }
        result = service.list_sessions("PROJ1", modality="MR")
        assert len(result) == 1
        assert result[0]["id"] == "E1"


class TestValidateLocalPathComponentSuperscriptReservedNames:
    """COM1-9/LPT1-9's superscript-digit forms (U+00B9/U+00B2/U+00B3) are
    reserved by Windows the same way the plain-digit forms are.
    """

    @pytest.mark.parametrize(
        "value",
        ["COM¹", "com²", "LPT³", "Lpt¹.txt", "COM².tar.gz"],
    )
    def test_superscript_reserved_name_rejected(self, value: str) -> None:
        with pytest.raises(PathValidationError):
            validate_local_path_component(value, "value")

    def test_lookalike_but_not_reserved_still_passes(self) -> None:
        # "COM10" is not reserved (only COM1-9 are); sanity check the
        # superscript addition didn't over-broaden the match.
        assert validate_local_path_component("COM10", "value") == "COM10"
