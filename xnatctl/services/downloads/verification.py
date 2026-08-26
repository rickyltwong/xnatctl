"""Post-download verification manifest building and comparison.

Fetches the server-side checksum manifest for a download's scope, then
compares it against what actually landed on disk.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from xnatctl.models.hierarchy import ExperimentRef, ScanRef
from xnatctl.models.progress import VerificationReport

from .. import verify
from ..resources import ResourceService
from ..sessions import SessionService
from .shared import _HierarchyResolveMixin, _reject_empty_resource_filter_values


class _VerificationMixin(_HierarchyResolveMixin):
    """Mixin providing verification-manifest building and comparison."""

    def build_verification_manifest(
        self,
        *,
        session_id: str,
        project: str | None,
        subject: str | None = None,
        scan_ids: list[str] | None = None,
        include_resources: tuple[str, ...] = (),
        exclude_resources: tuple[str, ...] = (),
        resource_filter: str | None = None,
        include_session_resources: bool = False,
    ) -> verify.VerificationManifest:
        """Fetch server-side checksums for a downloaded scan scope, keyed by path.

        Mirrors the scope :meth:`~xnatctl.services.downloads.session.
        _SessionDownloadMixin.download_session_fast`/
        :meth:`~xnatctl.services.downloads.resource_scan.
        _ResourceScanDownloadMixin.download_scans` used to fetch the files in
        the first place: the same experiment resolution, and -- with
        *scan_ids* omitted -- the same flat, unscoped scan enumeration
        ``download_session_fast`` uses.

        Args:
            session_id: Session ID or label.
            project: Project ID (enables label resolution).
            subject: Subject ID/label, when known.
            scan_ids: Scans to cover; None covers every scan in the session.
            include_resources: Resource labels to include (empty = all).
            exclude_resources: Resource labels to exclude.
            resource_filter: A single resource label to scope every scan to,
                skipping the per-scan resource listing call. Mutually
                exclusive in practice with include/exclude, which only make
                sense when the resource set is discovered per scan.
            include_session_resources: Also cover session-level (outside-scans)
                resources, i.e. the ``--session-resources`` download scope.

        Returns:
            The digest map (see :func:`xnatctl.services.verify.key_from_uri`;
            a None digest means the server listed the file with no checksum)
            plus any key two different server-reported files both mapped to.

        Raises:
            InputValidationError: If ``include_resources`` or
                ``exclude_resources`` contains an empty/whitespace-only value.
        """
        _reject_empty_resource_filter_values(include_resources, exclude_resources)

        resolved = self._resolve_zip_experiment_ref(session_id, project=project, subject=subject)
        resolved_session_id = resolved.experiment
        experiment_ref = ExperimentRef(
            experiment=resolved_session_id, project_id=project, subject=subject
        )

        resource_svc = ResourceService(self.client)
        collector = verify.ManifestCollector()

        if scan_ids is None:
            rows = SessionService(self.client).scan_rows(resolved_session_id)
            scan_ids = [r["ID"] for r in rows if r.get("ID")]

        include_set = frozenset(include_resources)
        exclude_set = frozenset(exclude_resources)

        for scan_id in scan_ids:
            scan_ref = ScanRef(experiment=experiment_ref, scan_id=scan_id)
            # `is not None`, not truthy: `resource_filter=""` is a caller
            # mistake, not "no single-resource scope" -- it must not silently
            # widen verification to every resource on the scan.
            # list_file_rows below rejects the empty string via ResourceRef.
            if resource_filter is not None:
                labels = [resource_filter]
            else:
                labels = [
                    str(r["label"]) for r in resource_svc.list_rows(scan_ref) if r.get("label")
                ]
                if include_set:
                    labels = [label for label in labels if label in include_set]
                elif exclude_set:
                    labels = [label for label in labels if label not in exclude_set]

            for label in labels:
                rows = resource_svc.list_file_rows(scan_ref, label)
                collector.ingest(rows, label=label, scan_id=scan_id)

        if include_session_resources:
            session_resource_rows = SessionService(self.client).experiment_resource_rows(
                resolved_session_id, project=project, subject=subject
            )
            for res in session_resource_rows:
                label = str(res.get("label") or "")
                if not label:
                    continue
                rows = resource_svc.list_file_rows(experiment_ref, label)
                collector.ingest(rows, label=label, scan_id=None)

        return verify.VerificationManifest(
            digests=collector.manifest, collisions=sorted(collector.collisions)
        )

    def verify_scan_downloads(
        self,
        *,
        session_id: str,
        project: str | None,
        subject: str | None = None,
        scan_ids: list[str] | None = None,
        include_resources: tuple[str, ...] = (),
        exclude_resources: tuple[str, ...] = (),
        resource_filter: str | None = None,
        include_session_resources: bool = False,
        local_root: Path | None = None,
        local_root_wrapped: bool = False,
        zip_paths: Sequence[verify.ZipSource] = (),
    ) -> VerificationReport:
        """Verify a completed download against server-reported MD5 checksums.

        Fetches the server-side file manifest for the same scope the download
        used (see :meth:`build_verification_manifest`), then compares it
        against the files on disk: an extracted tree (*local_root*) and/or one
        or more unextracted archives (*zip_paths*, not mutually exclusive with
        *local_root* -- session-level resources can remain as separate,
        un-extracted ZIPs alongside an extracted scan tree), streamed rather
        than loaded whole into memory either way.

        Args:
            session_id: Session ID or label.
            project: Project ID (enables label resolution).
            subject: Subject ID/label, when known.
            scan_ids: Scans to verify; None covers every scan in the session.
            include_resources: Resource labels to include (empty = all).
            exclude_resources: Resource labels to exclude.
            resource_filter: A single resource label the download was scoped to.
            include_session_resources: Also cover session-level resources.
            local_root: Root of an extracted download tree.
            local_root_wrapped: Whether *local_root*'s tree carries a
                session/experiment-label wrapper -- see
                :func:`xnatctl.services.verify.scan_source_key`. The caller
                already knows this from how it produced *local_root*.
            zip_paths: Unextracted archive(s) to verify against too. Each
                entry is a bare path or a ``(path, label)`` pair overriding
                *resource_filter* for that one archive.

        Returns:
            The comparison report, its ``collisions`` including both
            server-side ambiguities from the manifest and local-side ones
            found while indexing *local_root*/*zip_paths*.
        """
        manifest = self.build_verification_manifest(
            session_id=session_id,
            project=project,
            subject=subject,
            scan_ids=scan_ids,
            include_resources=include_resources,
            exclude_resources=exclude_resources,
            resource_filter=resource_filter,
            include_session_resources=include_session_resources,
        )
        report = verify.verify_manifest(
            manifest.digests,
            local_root=local_root,
            local_root_wrapped=local_root_wrapped,
            zip_paths=zip_paths,
            resource_label=resource_filter,
        )
        if manifest.collisions:
            report.collisions = sorted(set(report.collisions) | set(manifest.collisions))
        return report
