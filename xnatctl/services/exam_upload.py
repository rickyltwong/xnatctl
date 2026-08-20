"""Exam-upload orchestration service.

``session upload-exam`` uploads an exam root -- DICOM anywhere under the root,
plus top-level resource directories and misc files -- then, once the session has
archived, attaches those resources. This module owns that sequencing so it can
be tested without ``CliRunner``; the Click command keeps only option resolution
and output rendering.
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from xnatctl.core.exam import ExamRootClassification, classify_exam_root
from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.core.validation import validate_resource_label
from xnatctl.models.hierarchy import ExperimentRef

from .base import BaseService
from .hierarchy import HierarchyService


class ExamOutcome(str, Enum):
    """What an exam upload achieved.

    ``NO_DICOM`` and ``DICOM_FAILED`` are error outcomes carrying an
    ``error_message``: the CLI surfaces them as a ``ClickException`` (exit 1)
    rather than a rendered result. The other three are success outcomes the CLI
    serialises.
    """

    NO_DICOM = "no_dicom"
    DICOM_FAILED = "dicom_failed"
    NO_RESOURCES = "no_resources"
    NOT_ARCHIVED = "not_archived"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ExamPlan:
    """The validated, classified exam root -- shared by dry-run and upload."""

    exam_root: Path
    classification: ExamRootClassification
    resource_labels: tuple[str, ...]
    misc_label: str


@dataclass(frozen=True)
class ExamUploadResult:
    """The outcome of an exam upload, with the numbers the CLI reports.

    ``to_json_dict`` reproduces the exact ``-o json`` structure the command has
    always emitted; it is a compatibility contract for scripting consumers and
    is pinned by tests.
    """

    outcome: ExamOutcome
    project: str
    subject: str
    session: str
    exam_root: str
    attach_only: bool
    dicom_total: int
    dicom_uploaded: int
    skip_resources: bool
    misc_label: str
    attached_resource_dirs: int = 0
    attached_misc_files: int = 0
    pending: int = 0
    rerun: str = ""
    wait_for_archive: bool = False
    wait_timeout: int = 0
    error_message: str | None = None

    def _dicom_dict(self) -> dict[str, Any]:
        return {
            "skipped": self.attach_only,
            "total": self.dicom_total,
            "uploaded": self.dicom_uploaded,
        }

    def to_json_dict(self) -> dict[str, Any]:
        """The exact ``-o json`` payload for this result's outcome."""
        base: dict[str, Any] = {
            "project": self.project,
            "subject": self.subject,
            "session": self.session,
            "exam_root": self.exam_root,
            "dicom": self._dicom_dict(),
        }
        if self.outcome is ExamOutcome.NOT_ARCHIVED:
            base["resources"] = {
                "skipped": False,
                "attached": False,
                "pending": self.pending,
                "resource_dirs": 0,
                "misc_files": 0,
                "misc_label": self.misc_label,
                "reason": "session not archived before wait timeout",
                "rerun": self.rerun,
            }
        elif self.outcome is ExamOutcome.NO_RESOURCES:
            base["resources"] = {
                "skipped": bool(self.skip_resources),
                "resource_dirs": 0,
                "misc_files": 0,
                "misc_label": self.misc_label,
            }
        else:  # COMPLETE
            base["resources"] = {
                "skipped": False,
                "resource_dirs": self.attached_resource_dirs,
                "misc_files": self.attached_misc_files,
                "misc_label": self.misc_label,
            }
        return base


class ExamUploadService(BaseService):
    """Orchestrates ``session upload-exam``: DICOM upload, wait, resource attach."""

    def plan(self, exam_root: Path, misc_label: str) -> ExamPlan:
        """Validate the misc label, classify the exam root, validate its labels.

        Shared by the dry-run preview and the upload path so both see the same
        classification. Raises the validators' typed errors on a bad label.
        """
        misc_label = validate_resource_label(misc_label)
        classification = classify_exam_root(exam_root)
        resource_labels = tuple(
            validate_resource_label(resource_dir.name)
            for resource_dir in classification.resource_dirs
        )
        return ExamPlan(
            exam_root=exam_root,
            classification=classification,
            resource_labels=resource_labels,
            misc_label=misc_label,
        )

    def upload_exam(
        self,
        plan: ExamPlan,
        *,
        project: str,
        subject: str,
        session: str,
        workers: int,
        direct_archive: bool,
        skip_resources: bool,
        attach_only: bool,
        wait: int,
        wait_interval: int,
    ) -> ExamUploadResult:
        """Upload the exam's DICOM, wait for archiving, then attach resources.

        Returns an :class:`ExamUploadResult`; the two error outcomes
        (``NO_DICOM``, ``DICOM_FAILED``) carry ``error_message`` for the CLI to
        raise as a ``ClickException``. On a wait timeout the DICOM upload is kept
        and the unattached resources are reported for an ``--attach-only`` rerun.
        """
        # Imported here so tests that monkeypatch ``xnatctl.services.uploads``
        # /``xnatctl.services.resources`` still intercept the lookup.
        from xnatctl.services.uploads import UploadService

        classification = plan.classification
        exam_root_path = plan.exam_root
        misc_label = plan.misc_label

        # wait > 0 means "poll until archived"; wait is the timeout in seconds.
        wait_for_archive = wait > 0
        wait_timeout = wait

        dicom_total = len(classification.dicom_files)
        dicom_uploaded = 0

        def _result(outcome: ExamOutcome, **extra: Any) -> ExamUploadResult:
            return ExamUploadResult(
                outcome=outcome,
                project=project,
                subject=subject,
                session=session,
                exam_root=str(exam_root_path),
                attach_only=attach_only,
                dicom_total=dicom_total,
                dicom_uploaded=dicom_uploaded,
                skip_resources=skip_resources,
                misc_label=misc_label,
                wait_for_archive=wait_for_archive,
                wait_timeout=wait_timeout,
                **extra,
            )

        if not attach_only:
            if not classification.dicom_files:
                return _result(
                    ExamOutcome.NO_DICOM,
                    error_message=f"No DICOM files found under: {exam_root_path}",
                )

            summary = UploadService(self.client).upload_dicom_gradual_files(
                files=classification.dicom_files,
                project=project,
                subject=subject,
                session=session,
                workers=workers,
                direct_archive=direct_archive,
            )
            if not summary.success:
                errors = "; ".join(summary.errors[:3])
                return _result(
                    ExamOutcome.DICOM_FAILED,
                    error_message=(
                        f"DICOM upload failed ({summary.succeeded}/{summary.total} "
                        f"succeeded): {errors}"
                    ),
                )
            dicom_uploaded = summary.succeeded

        has_attachable_resources = bool(classification.resource_dirs) or bool(
            classification.misc_files
        )
        if skip_resources or not has_attachable_resources:
            return _result(ExamOutcome.NO_RESOURCES)

        resolved_experiment_id = self._resolve_experiment_id(project, session)
        if not resolved_experiment_id and wait_for_archive:
            deadline = time.monotonic() + wait_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(wait_interval, remaining))
                resolved_experiment_id = self._resolve_experiment_id(project, session)
                if resolved_experiment_id:
                    break

        if not resolved_experiment_id:
            # Archiving outlived the wait window (or --wait 0 on a not-yet-archived
            # session). Don't discard the successful DICOM upload or silently drop
            # resources: report an actionable partial result so the caller can
            # attach resources with --attach-only once archiving completes.
            pending = len(classification.resource_dirs) + len(classification.misc_files)
            rerun = (
                f"xnatctl session upload-exam '{exam_root_path}' "
                f"-P {project} -S {subject} -E {session} --attach-only"
            )
            return _result(ExamOutcome.NOT_ARCHIVED, pending=pending, rerun=rerun)

        self._attach_resources(
            classification,
            resolved_experiment_id=resolved_experiment_id,
            project=project,
            misc_label=misc_label,
        )

        return _result(
            ExamOutcome.COMPLETE,
            attached_resource_dirs=len(classification.resource_dirs),
            attached_misc_files=len(classification.misc_files),
        )

    def _resolve_experiment_id(self, project: str, session: str) -> str | None:
        """Resolve the session label to an archived experiment ID, or None."""
        try:
            resolved = HierarchyService(self.client).resolve_experiment(
                ExperimentRef(
                    experiment=session,
                    project_id=project,
                    experiment_is_label=True,
                )
            )
        except ResourceNotFoundError:
            return None
        return resolved.experiment_id or session

    def _attach_resources(
        self,
        classification: ExamRootClassification,
        *,
        resolved_experiment_id: str,
        project: str,
        misc_label: str,
    ) -> None:
        """Attach resource directories and zip up misc files onto the session."""
        from xnatctl.services.resources import ResourceService

        resource_service = ResourceService(self.client)

        for resource_dir in classification.resource_dirs:
            label = validate_resource_label(resource_dir.name)
            resource_service.create(
                session_id=resolved_experiment_id,
                resource_label=label,
                project=project,
            )
            resource_service.upload_directory(
                session_id=resolved_experiment_id,
                resource_label=label,
                directory_path=resource_dir,
                project=project,
            )

        if classification.misc_files:
            resource_service.create(
                session_id=resolved_experiment_id,
                resource_label=misc_label,
                project=project,
            )
            with tempfile.TemporaryDirectory() as tmp_dir:
                zip_path = Path(tmp_dir) / f"{misc_label}.zip"
                with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zf:
                    for misc_file in classification.misc_files:
                        zf.write(misc_file, arcname=misc_file.name)
                resource_service.upload_file(
                    session_id=resolved_experiment_id,
                    resource_label=misc_label,
                    file_path=zip_path,
                    project=project,
                    extract=True,
                )
