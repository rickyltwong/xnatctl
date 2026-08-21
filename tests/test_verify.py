"""Tests for xnatctl.services.verify: per-source path-keying and manifest comparison."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from xnatctl.models.progress import VerificationReport
from xnatctl.services.verify import (
    key_from_uri,
    scan_source_key,
    session_resource_zip_member_keys,
    verify_manifest,
)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class TestScanSourceKey:
    """scan_source_key: an extracted scan tree or a scan ZIP's members.

    `wrapped` is always passed explicitly, matching what a real call site
    always knows about its own source -- never probed by trying both and
    keeping whichever parses (that dual-probe is what let a session wrapper
    literally named "scans" parse under both anchors and silently pick the
    wrong one).
    """

    def test_unwrapped_anchored_at_position_zero(self) -> None:
        """A session download's own extracted tree: session_dir is the root."""
        parts = ("scans", "2", "resources", "DICOM", "files", "1.dcm")
        assert scan_source_key(parts, wrapped=False) == "scans/2/resources/DICOM/1.dcm"

    def test_wrapped_skips_one_leading_segment(self) -> None:
        """A raw combined-scan ZIP carries one session-label wrapper segment."""
        parts = ("XNAT_E00001", "scans", "2", "resources", "DICOM", "files", "1.dcm")
        assert scan_source_key(parts, wrapped=True) == "scans/2/resources/DICOM/1.dcm"

    def test_wrapped_with_a_wrapper_literally_named_scans(self) -> None:
        """The dual-probe this replaced could parse both anchors here and
        silently pick the wrong one; declaring wrapped=True removes the
        ambiguity entirely -- only the True-anchor shape is ever tried.
        """
        parts = ("scans", "scans", "2", "resources", "DICOM", "files", "1.dcm")
        assert scan_source_key(parts, wrapped=True) == "scans/2/resources/DICOM/1.dcm"

    def test_nested_filename_is_preserved(self) -> None:
        parts = ("scans", "2", "resources", "NII", "files", "sub", "brain.nii.gz")
        assert scan_source_key(parts, wrapped=False) == "scans/2/resources/NII/sub/brain.nii.gz"

    def test_same_basename_in_different_scans_yields_different_keys(self) -> None:
        """The regression the old basename-keyed verifier missed."""
        a = scan_source_key(("scans", "1", "resources", "DICOM", "files", "1.dcm"), wrapped=False)
        b = scan_source_key(("scans", "2", "resources", "DICOM", "files", "1.dcm"), wrapped=False)
        assert a != b

    def test_no_marker_and_no_label_returns_none(self) -> None:
        assert scan_source_key(("some", "random", "path", "data.dat"), wrapped=False) is None

    def test_a_resource_labeled_scans_does_not_confuse_the_marker(self) -> None:
        """A label literally 'scans' sits at the fixed label position (index
        3) -- never compared against the literal "scans" marker check, which
        only ever looks at position 0 (or 1, when wrapped).
        """
        parts = ("scans", "2", "resources", "scans", "files", "report.txt")
        assert scan_source_key(parts, wrapped=False) == "scans/2/resources/scans/report.txt"

    def test_prescoped_zip_uses_supplied_resource_label_fallback(self) -> None:
        """A single-resource download's ZIP entries can omit resources/{label}."""
        parts = ("EXP", "scans", "2", "files", "1.dcm")
        assert (
            scan_source_key(parts, wrapped=True, resource_label="DICOM")
            == "scans/2/resources/DICOM/1.dcm"
        )

    def test_prescoped_zip_without_label_context_returns_none(self) -> None:
        parts = ("EXP", "scans", "2", "files", "1.dcm")
        assert scan_source_key(parts, wrapped=True) is None

    def test_prescoped_shape_with_intervening_label_segment(self) -> None:
        """The live pre-scoped layout is unconfirmed -- accept both shapes."""
        parts = ("EXP", "scans", "2", "DICOM", "files", "1.dcm")
        assert (
            scan_source_key(parts, wrapped=True, resource_label="DICOM")
            == "scans/2/resources/DICOM/1.dcm"
        )

    def test_genuine_marker_is_not_shadowed_by_a_supplied_resource_label(self) -> None:
        parts = ("scans", "1", "resources", "DICOM", "files", "1.dcm")
        assert (
            scan_source_key(parts, wrapped=False, resource_label="DICOM")
            == "scans/1/resources/DICOM/1.dcm"
        )


class TestScanSourceKeyAdversarial:
    """A filtered single-resource ZIP's payload path can itself contain
    text that looks like a marker -- never revisited once the fixed
    positions have been read.
    """

    def test_nested_marker_in_payload_path(self) -> None:
        parts = ("EXP", "scans", "1", "files", "nested", "resources", "QC", "files", "report.txt")
        assert (
            scan_source_key(parts, wrapped=True, resource_label="DICOM")
            == "scans/1/resources/DICOM/nested/resources/QC/files/report.txt"
        )

    def test_marker_like_filename(self) -> None:
        parts = ("EXP", "scans", "1", "files", "resources")
        assert (
            scan_source_key(parts, wrapped=True, resource_label="DICOM")
            == "scans/1/resources/DICOM/resources"
        )

    def test_nested_marker_and_marker_like_filename_together(self) -> None:
        parts = ("EXP", "scans", "1", "files", "resources", "QC", "files", "resources")
        assert (
            scan_source_key(parts, wrapped=True, resource_label="DICOM")
            == "scans/1/resources/DICOM/resources/QC/files/resources"
        )

    def test_nested_marker_without_resource_label_context_is_unmatched(self) -> None:
        parts = ("EXP", "scans", "1", "files", "nested", "resources", "QC", "files", "report.txt")
        assert scan_source_key(parts, wrapped=True) is None

    def test_payload_containing_a_scans_segment_does_not_misfire(self) -> None:
        """A payload path containing its own "scans" segment, deep inside the
        opaque name, is preserved as-is -- never re-anchored on.
        """
        parts = ("scans", "1", "resources", "DICOM", "files", "scans", "2", "report.txt")
        assert scan_source_key(parts, wrapped=False) == "scans/1/resources/DICOM/scans/2/report.txt"


class TestKeyFromUri:
    """key_from_uri: positional parsing of the server's canonical XNAT URI form."""

    def test_scan_level_uri_flat(self) -> None:
        parts = ("data", "experiments", "E1", "scans", "2", "resources", "DICOM", "files", "1.dcm")
        assert key_from_uri(parts) == "scans/2/resources/DICOM/1.dcm"

    def test_scan_level_uri_project_scoped(self) -> None:
        parts = (
            "data",
            "projects",
            "P",
            "subjects",
            "S",
            "experiments",
            "E1",
            "scans",
            "2",
            "resources",
            "DICOM",
            "files",
            "sub",
            "1.dcm",
        )
        assert key_from_uri(parts) == "scans/2/resources/DICOM/sub/1.dcm"

    def test_scan_level_uri_project_no_subject(self) -> None:
        parts = (
            "data",
            "projects",
            "P",
            "experiments",
            "E1",
            "scans",
            "2",
            "resources",
            "DICOM",
            "files",
            "1.dcm",
        )
        assert key_from_uri(parts) == "scans/2/resources/DICOM/1.dcm"

    def test_session_level_uri(self) -> None:
        parts = ("data", "experiments", "E1", "resources", "QC", "files", "notes.txt")
        assert key_from_uri(parts) == "resources/QC/notes.txt"

    def test_a_resource_labeled_scans_is_not_mistaken_for_the_scan_marker(self) -> None:
        """The label sits at a fixed position read only after the marker
        itself was already matched positionally -- never mistaken for a
        second "scans" marker.
        """
        parts = ("data", "experiments", "E1", "resources", "scans", "files", "report.txt")
        assert key_from_uri(parts) == "resources/scans/report.txt"

    def test_no_data_segment_returns_none(self) -> None:
        assert key_from_uri(("experiments", "E1", "resources", "QC", "files", "x.txt")) is None

    def test_no_files_marker_returns_none(self) -> None:
        assert key_from_uri(("data", "experiments", "E1", "resources", "QC")) is None


class TestSessionResourceZipMemberKeys:
    """session_resource_zip_member_keys: candidate keys in priority order.
    The label is known in advance (from the ZIP's own filename), never
    guessed -- only located, or used to strip a wrapper. Real XNAT resource
    ZIPs are documented (see
    services/transfer/executor.py::_strip_xnat_prefix) to carry the full
    hierarchy; the exact shape a given server actually emits isn't
    guaranteed, so all three are accepted. Genuine ambiguity between shapes
    is resolved against the manifest -- see TestVerifyManifestSessionResourceZip.
    """

    def test_full_hierarchy_shape_is_the_top_candidate(self) -> None:
        """{session_label}/resources/{label}/files/{name} -- the documented,
        real XNAT resource-ZIP shape. Position-anchored: the marker is
        accepted only immediately after one leading segment.
        """
        candidates = session_resource_zip_member_keys(
            ("XNAT_E00001", "resources", "QC", "files", "notes.txt"), label="QC"
        )
        assert candidates[0] == "resources/QC/notes.txt"

    def test_full_hierarchy_shape_with_nested_name(self) -> None:
        candidates = session_resource_zip_member_keys(
            ("XNAT_E00001", "resources", "QC", "files", "sub", "notes.txt"), label="QC"
        )
        assert candidates[0] == "resources/QC/sub/notes.txt"

    def test_bare_label_prefixed_shape(self) -> None:
        candidates = session_resource_zip_member_keys(("QC", "notes.txt"), label="QC")
        assert candidates[0] == "resources/QC/notes.txt"

    def test_flat_shape_with_no_wrapper_at_all(self) -> None:
        """No other shape applies -- exactly one candidate."""
        candidates = session_resource_zip_member_keys(("notes.txt",), label="QC")
        assert candidates == ("resources/QC/notes.txt",)

    def test_a_label_literally_named_scans_full_hierarchy(self) -> None:
        candidates = session_resource_zip_member_keys(
            ("XNAT_E00001", "resources", "scans", "files", "notes.txt"), label="scans"
        )
        assert candidates[0] == "resources/scans/notes.txt"

    def test_a_label_literally_named_scans_bare(self) -> None:
        candidates = session_resource_zip_member_keys(("scans", "notes.txt"), label="scans")
        assert candidates[0] == "resources/scans/notes.txt"

    def test_payload_containing_scans_and_a_different_labels_marker(self) -> None:
        """A session-resource payload path containing its own "scans" or
        "resources/{other_label}/files" text (a DIFFERENT label than the
        known one) is never mistaken for the marker -- the label comparison
        in the position-1 check means a mismatched label can never match.
        """
        candidates = session_resource_zip_member_keys(
            ("QC", "scans", "resources", "X", "files", "report.txt"), label="QC"
        )
        assert candidates[0] == "resources/QC/scans/resources/X/files/report.txt"

    def test_marker_deeper_than_position_one_is_never_matched_as_structure(self) -> None:
        """A marker appearing at position 2+ (more than one leading segment)
        is payload content, never structure -- position-anchoring stops it
        from being matched as the full-hierarchy shape at all.
        """
        candidates = session_resource_zip_member_keys(
            ("EXP", "extra", "resources", "QC", "files", "report.txt"), label="QC"
        )
        # No full-hierarchy candidate: "resources" sits at position 2, not 1.
        assert "resources/QC/report.txt" not in candidates

    def test_codex_case_flat_member_with_embedded_own_label_marker(self) -> None:
        """A genuinely flat member (no wrapper) whose own name happens to
        start with "resources/{label}/files/" positionally coincides with
        the full-hierarchy shape -- both interpretations are offered as
        candidates; only the manifest can say which is correct (see
        TestVerifyManifestSessionResourceZip).
        """
        candidates = session_resource_zip_member_keys(
            ("nested", "resources", "QC", "files", "report.txt"), label="QC"
        )
        assert candidates == (
            "resources/QC/report.txt",
            "resources/QC/nested/resources/QC/files/report.txt",
        )

    def test_label_wrapped_member_with_embedded_marker_in_the_remainder(self) -> None:
        """The bare label-prefixed shape, whose remainder (after stripping)
        itself contains an embedded own-label marker -- the marker is never
        re-examined once the wrapper is stripped; "stripped" and "flat" are
        the only two candidates.
        """
        candidates = session_resource_zip_member_keys(
            ("QC", "nested", "resources", "QC", "files", "report.txt"), label="QC"
        )
        assert candidates == (
            "resources/QC/nested/resources/QC/files/report.txt",
            "resources/QC/QC/nested/resources/QC/files/report.txt",
        )


class TestVerifyManifestExtractedTree:
    """verify_manifest against an extracted local directory tree."""

    def _write(self, root: Path, scan: str, label: str, name: str, data: bytes) -> None:
        target = root / "scans" / scan / "resources" / label / "files" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def test_same_basename_across_scans_verified_independently(self, tmp_path: Path) -> None:
        self._write(tmp_path, "1", "DICOM", "1.dcm", b"scan-one-data")
        self._write(tmp_path, "2", "DICOM", "1.dcm", b"scan-two-data")

        manifest = {
            "scans/1/resources/DICOM/1.dcm": _md5(b"scan-one-data"),
            "scans/2/resources/DICOM/1.dcm": _md5(b"scan-two-data"),
        }

        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.matched == 2
        assert report.mismatched == []
        assert report.missing_local == []
        assert report.success is True

    def test_wrong_local_hash_is_a_mismatch_and_fails(self, tmp_path: Path) -> None:
        self._write(tmp_path, "1", "DICOM", "1.dcm", b"corrupted")
        manifest = {"scans/1/resources/DICOM/1.dcm": _md5(b"original")}

        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.mismatched == ["scans/1/resources/DICOM/1.dcm"]
        assert report.matched == 0
        assert report.success is False

    def test_server_file_missing_locally_fails(self, tmp_path: Path) -> None:
        manifest = {"scans/1/resources/DICOM/1.dcm": _md5(b"data")}

        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.missing_local == ["scans/1/resources/DICOM/1.dcm"]
        assert report.success is False

    def test_local_file_the_server_never_mentioned_is_missing_remote_only(
        self, tmp_path: Path
    ) -> None:
        self._write(tmp_path, "1", "DICOM", "1.dcm", b"data")
        self._write(tmp_path, "1", "DICOM", "extra.txt", b"local-only")
        manifest = {"scans/1/resources/DICOM/1.dcm": _md5(b"data")}

        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.missing_remote == ["scans/1/resources/DICOM/extra.txt"]
        assert report.success is True

    def test_file_without_server_digest_is_unverifiable_and_fails_alone(
        self, tmp_path: Path
    ) -> None:
        self._write(tmp_path, "1", "DICOM", "1.dcm", b"data")
        manifest = {"scans/1/resources/DICOM/1.dcm": None}

        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.unverifiable == ["scans/1/resources/DICOM/1.dcm"]
        assert report.matched == 0
        assert report.success is False


class TestVerifyManifestZip:
    """verify_manifest against an unextracted scan-level ZIP archive."""

    def test_verifies_members_without_extracting(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "scans.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("EXP/scans/1/resources/DICOM/files/1.dcm", b"scan-one-data")
            zf.writestr("EXP/scans/2/resources/DICOM/files/1.dcm", b"scan-two-data")

        manifest = {
            "scans/1/resources/DICOM/1.dcm": _md5(b"scan-one-data"),
            "scans/2/resources/DICOM/1.dcm": _md5(b"scan-two-data"),
        }

        report = verify_manifest(manifest, zip_paths=(zip_path,))

        assert report.matched == 2
        assert report.success is True

    def test_mismatch_inside_zip_member(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "scans.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("EXP/scans/1/resources/DICOM/files/1.dcm", b"corrupted")

        manifest = {"scans/1/resources/DICOM/1.dcm": _md5(b"original")}

        report = verify_manifest(manifest, zip_paths=(zip_path,))

        assert report.mismatched == ["scans/1/resources/DICOM/1.dcm"]
        assert report.success is False

    def test_prescoped_single_resource_zip_uses_label_fallback(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "scans.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("EXP/scans/1/files/1.dcm", b"data")

        manifest = {"scans/1/resources/DICOM/1.dcm": _md5(b"data")}

        report = verify_manifest(manifest, zip_paths=(zip_path,), resource_label="DICOM")

        assert report.matched == 1
        assert report.success is True


class TestVerifyManifestSessionResourceZip:
    """verify_manifest against a (path, label) session-resource ZIP."""

    def test_verifies_the_labeled_zip_bare_shape(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "resources_QC.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("QC/notes.txt", b"note-data")

        manifest = {"resources/QC/notes.txt": _md5(b"note-data")}

        report = verify_manifest(manifest, zip_paths=[(zip_path, "QC")])

        assert report.matched == 1
        assert report.success is True

    def test_verifies_the_labeled_zip_full_hierarchy_shape(self, tmp_path: Path) -> None:
        """The documented, real XNAT resource-ZIP shape: a session-label
        wrapper around the full resources/{label}/files/{name} path.
        """
        zip_path = tmp_path / "resources_QC.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("XNAT_E00001/resources/QC/files/notes.txt", b"note-data")

        manifest = {"resources/QC/notes.txt": _md5(b"note-data")}

        report = verify_manifest(manifest, zip_paths=[(zip_path, "QC")])

        assert report.matched == 1
        assert report.success is True

    def test_verifies_the_labeled_zip_flat_shape(self, tmp_path: Path) -> None:
        """No wrapper at all -- just the filename."""
        zip_path = tmp_path / "resources_QC.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("notes.txt", b"note-data")

        manifest = {"resources/QC/notes.txt": _md5(b"note-data")}

        report = verify_manifest(manifest, zip_paths=[(zip_path, "QC")])

        assert report.matched == 1
        assert report.success is True

    def test_a_label_literally_named_scans(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "resources_scans.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("scans/notes.txt", b"note-data")

        manifest = {"resources/scans/notes.txt": _md5(b"note-data")}

        report = verify_manifest(manifest, zip_paths=[(zip_path, "scans")])

        assert report.matched == 1
        assert report.success is True

    def test_missing_session_resource_zip_fails(self, tmp_path: Path) -> None:
        """No zip captured for a resource at all (e.g. its download failed) --
        the manifest still knows about it, so it must be reported missing.
        """
        manifest = {"resources/QC/notes.txt": _md5(b"note-data")}

        report = verify_manifest(manifest, zip_paths=[])

        assert report.missing_local == ["resources/QC/notes.txt"]
        assert report.success is False


class TestVerifyManifestSessionResourceZipAmbiguity:
    """Members whose shape genuinely can't be told apart positionally are
    resolved against the server manifest -- ground truth for which name the
    file actually has.
    """

    def test_codex_case_resolved_as_full_hierarchy_when_manifest_confirms_it(
        self, tmp_path: Path
    ) -> None:
        """nested/resources/QC/files/report.txt for resource QC: when the
        manifest has the short (full-hierarchy) key, that's the correct
        reading -- "nested" really was a wrapper.
        """
        zip_path = tmp_path / "resources_QC.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("nested/resources/QC/files/report.txt", b"report-data")

        manifest = {"resources/QC/report.txt": _md5(b"report-data")}
        report = verify_manifest(manifest, zip_paths=[(zip_path, "QC")])

        assert report.matched == 1
        assert report.collisions == []
        assert report.success is True

    def test_codex_case_resolved_as_flat_when_manifest_confirms_it(self, tmp_path: Path) -> None:
        """The same member, but the manifest's own key proves "nested" was
        genuine payload content, not a wrapper: the flat interpretation
        wins instead, even though it is the lower-priority candidate.
        """
        zip_path = tmp_path / "resources_QC.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("nested/resources/QC/files/report.txt", b"report-data")

        manifest = {"resources/QC/nested/resources/QC/files/report.txt": _md5(b"report-data")}
        report = verify_manifest(manifest, zip_paths=[(zip_path, "QC")])

        assert report.matched == 1
        assert report.collisions == []
        assert report.success is True

    def test_label_wrapped_member_with_embedded_marker_resolved_via_manifest(
        self, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "resources_QC.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("QC/nested/resources/QC/files/report.txt", b"report-data")

        manifest = {"resources/QC/nested/resources/QC/files/report.txt": _md5(b"report-data")}
        report = verify_manifest(manifest, zip_paths=[(zip_path, "QC")])

        assert report.matched == 1
        assert report.success is True

    def test_flat_payload_whose_first_dir_equals_the_label_resolved_via_manifest(
        self, tmp_path: Path
    ) -> None:
        """A genuinely flat member whose own first directory happens to be
        named exactly like the resource label -- the "stripped" reading
        would wrongly collapse it; the manifest's own (unstripped) key
        proves the flat reading is correct.
        """
        zip_path = tmp_path / "resources_QC.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("QC/subdir/report.txt", b"report-data")

        manifest = {"resources/QC/QC/subdir/report.txt": _md5(b"report-data")}
        report = verify_manifest(manifest, zip_paths=[(zip_path, "QC")])

        assert report.matched == 1
        assert report.success is True

    def test_both_candidates_matching_different_manifest_keys_is_ambiguous(
        self, tmp_path: Path
    ) -> None:
        """The manifest has BOTH candidate keys -- as two genuinely
        different real files. This member's true identity can't be
        determined from either the shape or the manifest, so it must not
        be silently assigned to one of them; both are reported as
        collisions and verification fails.
        """
        zip_path = tmp_path / "resources_QC.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("nested/resources/QC/files/report.txt", b"report-data")

        short_key = "resources/QC/report.txt"
        long_key = "resources/QC/nested/resources/QC/files/report.txt"
        manifest = {
            short_key: _md5(b"a different file's content"),
            long_key: _md5(b"yet another file's content"),
        }
        report = verify_manifest(manifest, zip_paths=[(zip_path, "QC")])

        assert sorted(report.collisions) == sorted([short_key, long_key])
        assert report.matched == 0
        assert report.success is False


class TestVerifyManifestCombinedSources:
    """local_root and zip_paths are not mutually exclusive: a session
    download can have its scans extracted while session-level resources
    remain as separate un-extracted ZIPs.
    """

    def test_scan_tree_and_session_resource_zip_verified_together(self, tmp_path: Path) -> None:
        scan_file = tmp_path / "scans" / "1" / "resources" / "DICOM" / "files" / "1.dcm"
        scan_file.parent.mkdir(parents=True)
        scan_file.write_bytes(b"scan-data")

        session_zip = tmp_path / "resources_QC.zip"
        with zipfile.ZipFile(session_zip, "w") as zf:
            zf.writestr("QC/notes.txt", b"note-data")

        manifest = {
            "scans/1/resources/DICOM/1.dcm": _md5(b"scan-data"),
            "resources/QC/notes.txt": _md5(b"note-data"),
        }

        report = verify_manifest(
            manifest,
            local_root=tmp_path,
            zip_paths=[(session_zip, "QC")],
        )

        assert report.matched == 2
        assert report.success is True


class TestVerifyManifestCollisions:
    """Two unrelated files mapping to the same key must not silently
    last-write-wins -- they are pulled out and reported.
    """

    def test_two_different_local_files_on_the_same_key_are_reported(self, tmp_path: Path) -> None:
        """A genuine resources/{label}/files/ tree and a pre-scoped
        scans/{id}/files/ tree (resource_label supplied) both landing on
        scans/1/resources/DICOM/a/b.dcm despite different content.
        """
        genuine = tmp_path / "scans" / "1" / "resources" / "DICOM" / "files" / "a" / "b.dcm"
        genuine.parent.mkdir(parents=True)
        genuine.write_bytes(b"first")
        prescoped = tmp_path / "scans" / "1" / "files" / "a" / "b.dcm"
        prescoped.parent.mkdir(parents=True)
        prescoped.write_bytes(b"second")

        manifest = {"scans/1/resources/DICOM/a/b.dcm": _md5(b"first")}
        report = verify_manifest(manifest, local_root=tmp_path, resource_label="DICOM")

        assert report.collisions == ["scans/1/resources/DICOM/a/b.dcm"]
        assert report.matched == 0
        assert report.missing_local == []
        assert report.success is False

    def test_zip_member_collision_within_one_zip(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "scans.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("EXP/scans/1/resources/DICOM/files/1.dcm", b"first")
            zf.writestr("EXP/scans/1/resources/DICOM/files/1.dcm", b"second")

        manifest = {"scans/1/resources/DICOM/1.dcm": _md5(b"first")}
        report = verify_manifest(manifest, zip_paths=(zip_path,))

        assert report.collisions == ["scans/1/resources/DICOM/1.dcm"]
        assert report.matched == 0
        assert report.success is False

    def test_collision_across_two_different_zips(self, tmp_path: Path) -> None:
        """Two different ZIPs both claiming the same key must not silently
        update()-overwrite one another.
        """
        zip_a = tmp_path / "a.zip"
        zip_b = tmp_path / "b.zip"
        with zipfile.ZipFile(zip_a, "w") as zf:
            zf.writestr("EXP/scans/1/resources/DICOM/files/1.dcm", b"first")
        with zipfile.ZipFile(zip_b, "w") as zf:
            zf.writestr("EXP/scans/1/resources/DICOM/files/1.dcm", b"second")

        manifest = {"scans/1/resources/DICOM/1.dcm": _md5(b"first")}
        report = verify_manifest(manifest, zip_paths=(zip_a, zip_b))

        assert report.collisions == ["scans/1/resources/DICOM/1.dcm"]
        assert report.success is False

    def test_cross_source_collision_local_and_zip(self, tmp_path: Path) -> None:
        local_file = tmp_path / "scans" / "1" / "resources" / "DICOM" / "files" / "1.dcm"
        local_file.parent.mkdir(parents=True)
        local_file.write_bytes(b"local")

        zip_path = tmp_path / "other.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("EXP/scans/1/resources/DICOM/files/1.dcm", b"zipped")

        manifest = {"scans/1/resources/DICOM/1.dcm": _md5(b"local")}
        report = verify_manifest(manifest, local_root=tmp_path, zip_paths=(zip_path,))

        assert report.collisions == ["scans/1/resources/DICOM/1.dcm"]
        assert report.success is False


class TestVerifyManifestDupRename:
    """`_extract_scan_zip`'s __dupN rename must not hide the fresh download
    behind a stale original -- but must also never clobber a genuine
    server-reported `X__dupN`-named file.
    """

    def _target_dir(self, root: Path) -> Path:
        target_dir = root / "scans" / "1" / "resources" / "DICOM" / "files"
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def test_stale_original_plus_fresh_dup_verifies_against_the_fresh_file(
        self, tmp_path: Path
    ) -> None:
        target_dir = self._target_dir(tmp_path)
        (target_dir / "1.dcm").write_bytes(b"stale-wrong-content")
        (target_dir / "1__dup1.dcm").write_bytes(b"fresh-correct-content")

        manifest = {"scans/1/resources/DICOM/1.dcm": _md5(b"fresh-correct-content")}
        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.matched == 1
        assert report.mismatched == []
        assert report.success is True

    def test_stale_original_correct_but_fresh_dup_corrupted_fails(self, tmp_path: Path) -> None:
        target_dir = self._target_dir(tmp_path)
        (target_dir / "1.dcm").write_bytes(b"stale-correct-content")
        (target_dir / "1__dup1.dcm").write_bytes(b"fresh-corrupted-content")

        manifest = {"scans/1/resources/DICOM/1.dcm": _md5(b"stale-correct-content")}
        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.mismatched == ["scans/1/resources/DICOM/1.dcm"]
        assert report.success is False

    def test_highest_numbered_dup_wins_over_lower_ones(self, tmp_path: Path) -> None:
        target_dir = self._target_dir(tmp_path)
        (target_dir / "1.dcm").write_bytes(b"stale")
        (target_dir / "1__dup1.dcm").write_bytes(b"older-retry")
        (target_dir / "1__dup2.dcm").write_bytes(b"latest-retry")

        manifest = {"scans/1/resources/DICOM/1.dcm": _md5(b"latest-retry")}
        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.matched == 1
        assert report.success is True

    def test_real_server_dup_named_file_alongside_real_base_file(self, tmp_path: Path) -> None:
        """The server genuinely has both series.dcm and series__dup1.dcm as
        two distinct files -- both must verify independently, with no
        folding at all (the manifest knows both literal keys).
        """
        target_dir = self._target_dir(tmp_path)
        (target_dir / "series.dcm").write_bytes(b"base-content")
        (target_dir / "series__dup1.dcm").write_bytes(b"dup-content")

        manifest = {
            "scans/1/resources/DICOM/series.dcm": _md5(b"base-content"),
            "scans/1/resources/DICOM/series__dup1.dcm": _md5(b"dup-content"),
        }
        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.matched == 2
        assert report.mismatched == []
        assert report.success is True

    def test_server_only_dup_named_file_with_no_base_file(self, tmp_path: Path) -> None:
        """The server has only series__dup1.dcm (no series.dcm at all) --
        still a real, literal server filename, not a local rename artifact.
        """
        target_dir = self._target_dir(tmp_path)
        (target_dir / "series__dup1.dcm").write_bytes(b"dup-content")

        manifest = {"scans/1/resources/DICOM/series__dup1.dcm": _md5(b"dup-content")}
        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.matched == 1
        assert report.success is True


class TestVerifyManifestAllUnverifiable:
    def test_all_unverifiable_fails_end_to_end(self, tmp_path: Path) -> None:
        target = tmp_path / "scans" / "1" / "resources" / "DICOM" / "files" / "1.dcm"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"data")

        manifest = {"scans/1/resources/DICOM/1.dcm": None}
        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.unverifiable == ["scans/1/resources/DICOM/1.dcm"]
        assert report.matched == 0
        assert report.success is False

    def test_mixed_matched_and_unverifiable_passes_end_to_end(self, tmp_path: Path) -> None:
        scan_dir = tmp_path / "scans" / "1" / "resources" / "DICOM" / "files"
        scan_dir.mkdir(parents=True)
        (scan_dir / "1.dcm").write_bytes(b"data")
        (scan_dir / "2.dcm").write_bytes(b"other")

        manifest = {
            "scans/1/resources/DICOM/1.dcm": _md5(b"data"),
            "scans/1/resources/DICOM/2.dcm": None,
        }
        report = verify_manifest(manifest, local_root=tmp_path)

        assert report.matched == 1
        assert report.unverifiable == ["scans/1/resources/DICOM/2.dcm"]
        assert report.success is True


class TestVerificationReport:
    def test_success_requires_no_mismatch_or_missing_local(self) -> None:
        assert VerificationReport(matched=5).success is True
        assert VerificationReport(matched=5, mismatched=["x"]).success is False
        assert VerificationReport(matched=5, missing_local=["y"]).success is False

    def test_missing_remote_and_unverifiable_do_not_affect_success(self) -> None:
        report = VerificationReport(matched=1, missing_remote=["a"], unverifiable=["b"])
        assert report.success is True

    def test_collisions_fail_regardless_of_other_fields(self) -> None:
        assert VerificationReport(matched=5, collisions=["x"]).success is False

    def test_all_unverifiable_fails(self) -> None:
        report = VerificationReport(matched=0, unverifiable=["a", "b", "c"])
        assert report.success is False

    def test_mixed_matched_and_some_unverifiable_passes(self) -> None:
        report = VerificationReport(matched=2, unverifiable=["a"])
        assert report.success is True

    def test_zero_files_in_scope_is_not_a_failure(self) -> None:
        assert VerificationReport().success is True
