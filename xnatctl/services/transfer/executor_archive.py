"""Destination prearchive listing/resolution and post-import archive polling.

Split out of :class:`~xnatctl.services.transfer.executor.TransferExecutor`.
Covers reading and manually archiving prearchive entries on the destination,
and :meth:`_ArchiveMixin.wait_for_archive`, which polls prearchive/archive
state after a DICOM import until the expected scan count shows up (or the
wait times out).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from xnatctl.core.validation import quote_prearchive_segment as _quote_path_segment
from xnatctl.services.transfer.executor_base import _ExecutorAttrs

logger = logging.getLogger(__name__)


class _ArchiveMixin(_ExecutorAttrs):
    """Prearchive listing/resolution and archive-wait polling."""

    def list_prearchive_entries(self, dest_project: str) -> list[dict[str, Any]]:
        """List all prearchive entries for a project on the destination.

        Returns the full prearchive listing for a project. Used by
        ArchivePoller to fetch a single snapshot per poll cycle instead
        of N individual find_prearchive_entry() calls.

        Args:
            dest_project: Destination project ID.

        Returns:
            List of prearchive entry dicts with name, folderName, status, timestamp, etc.
        """
        encoded_project = _quote_path_segment(dest_project)
        resp = self.dest.get(
            f"/data/prearchive/projects/{encoded_project}",
            params={"format": "json"},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return results

    def find_prearchive_entry(self, dest_project: str, session_label: str) -> dict[str, Any] | None:
        """Find a prearchive entry matching a session label on the destination.

        Args:
            dest_project: Destination project ID.
            session_label: Session label to search for.

        Returns:
            Prearchive entry dict with timestamp, status, name, etc., or None.
        """
        encoded_project = _quote_path_segment(dest_project)
        resp = self.dest.get(
            f"/data/prearchive/projects/{encoded_project}",
            params={"format": "json"},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        for entry in results:
            if entry.get("name") == session_label or entry.get("folderName") == session_label:
                return entry
        return None

    def archive_prearchive(
        self,
        dest_project: str,
        timestamp: str,
        session_name: str,
        subject_label: str,
        experiment_label: str,
        overwrite: str | None = None,
    ) -> None:
        """Manually archive a prearchive entry on the destination.

        Args:
            dest_project: Destination project ID.
            timestamp: Prearchive entry timestamp.
            session_name: Session folder name in prearchive.
            subject_label: Subject label for archiving.
            experiment_label: Experiment label for archiving.
            overwrite: Overwrite mode (``"append"`` or ``"delete"``).
                Used to resolve prearchive CONFLICT entries.
        """
        encoded_project = _quote_path_segment(dest_project)
        encoded_timestamp = _quote_path_segment(timestamp)
        encoded_session_name = _quote_path_segment(session_name)
        encoded_subject = _quote_path_segment(subject_label)
        encoded_experiment = _quote_path_segment(experiment_label)
        src = f"/prearchive/projects/{encoded_project}/{encoded_timestamp}/{encoded_session_name}"
        data: dict[str, str] = {
            "src": src,
            "dest": (
                f"/archive/projects/{encoded_project}/subjects/{encoded_subject}"
                f"/experiments/{encoded_experiment}"
            ),
        }
        if overwrite is not None:
            data["overwrite"] = overwrite
        self.dest.post(
            "/data/services/archive",
            data=data,
        )

    def _safe_count_dest_scans(
        self,
        dest_project: str,
        subject_label: str,
        experiment_label: str,
        context: str,
    ) -> int:
        """Count dest scans, returning 0 on failure.

        Args:
            dest_project: Destination project ID.
            subject_label: Subject label.
            experiment_label: Experiment label.
            context: Log context on failure.

        Returns:
            Scan count, or 0 if the query fails.
        """
        try:
            return self.count_dest_scans(dest_project, subject_label, experiment_label)
        except Exception as exc:  # noqa: BLE001  # best-effort poll helper: returns 0 on failure, retried next cycle (see docstring)
            logger.debug(
                "count_dest_scans failed for %s (%s): %s",
                experiment_label,
                context,
                exc,
            )
            return 0

    def wait_for_archive(
        self,
        dest_project: str,
        subject_label: str,
        experiment_label: str,
        expected_scans: int,
        timeout: float = 300.0,
        interval: float = 5.0,
    ) -> int:
        """Wait for experiment scans to appear in archive after DICOM import.

        Polls the prearchive and archive until the expected number of scans
        are available, manually archiving READY entries found in prearchive.

        Args:
            dest_project: Destination project ID.
            subject_label: Subject label.
            experiment_label: Experiment label.
            expected_scans: Number of scans expected in archive.
            timeout: Maximum seconds to wait.
            interval: Seconds between poll attempts.

        Returns:
            Actual scan count found in archive when done.
        """
        deadline = time.monotonic() + timeout
        prearchive_cleared = False

        while True:
            if not prearchive_cleared:
                prearchive_cleared = self._poll_and_resolve_prearchive(
                    dest_project, subject_label, experiment_label
                )

            if prearchive_cleared:
                actual = self._safe_count_dest_scans(
                    dest_project, subject_label, experiment_label, "polling"
                )
                if actual >= expected_scans:
                    logger.info(
                        "Archive has %d/%d scans for %s",
                        actual,
                        expected_scans,
                        experiment_label,
                    )
                    return actual

            if time.monotonic() >= deadline:
                actual = self._safe_count_dest_scans(
                    dest_project, subject_label, experiment_label, "timeout"
                )
                logger.warning(
                    "Archive wait timed out for %s: %d/%d scans after %.0fs",
                    experiment_label,
                    actual,
                    expected_scans,
                    timeout,
                )
                return actual

            time.sleep(interval)

    def _poll_and_resolve_prearchive(
        self,
        dest_project: str,
        subject_label: str,
        experiment_label: str,
    ) -> bool:
        """Fetch the prearchive entry once and resolve READY/CONFLICT status.

        Args:
            dest_project: Destination project ID.
            subject_label: Subject label.
            experiment_label: Experiment label.

        Returns:
            True if the experiment has left the prearchive (no entry found).

        Raises:
            httpx.HTTPStatusError: If archiving a READY/CONFLICT entry fails.
        """
        try:
            entry = self.find_prearchive_entry(dest_project, experiment_label)
        except Exception:  # noqa: BLE001  # per-poll-cycle isolation: prearchive lookup failure retried next cycle
            logger.debug(
                "Poll cycle error for %s, retrying next cycle",
                experiment_label,
                exc_info=True,
            )
            return False

        if entry is None:
            return True

        status = entry.get("status", "")
        if status == "RECEIVING":
            logger.debug("Prearchive entry for %s still RECEIVING, waiting...", experiment_label)
        elif status in ("READY", "CONFLICT"):
            self._archive_entry(status, entry, dest_project, subject_label, experiment_label)
        elif status == "_BUILDING":
            logger.debug("Prearchive entry for %s is building, waiting...", experiment_label)
        else:
            logger.debug(
                "Prearchive entry for %s has status=%s, waiting...",
                experiment_label,
                status,
            )
        return False

    def _archive_entry(
        self,
        status: str,
        entry: dict[str, Any],
        dest_project: str,
        subject_label: str,
        experiment_label: str,
    ) -> None:
        """Archive a READY prearchive entry, or resolve a CONFLICT one.

        A CONFLICT is resolved the same way as READY, just with
        ``overwrite="append"``. A missing timestamp skips the entry (with a
        warning for READY, silently for CONFLICT).

        Args:
            status: ``"READY"`` or ``"CONFLICT"``.
            entry: Prearchive entry dict.
            dest_project: Destination project ID.
            subject_label: Subject label.
            experiment_label: Experiment label.

        Raises:
            httpx.HTTPStatusError: If the archive POST fails.
        """
        timestamp = entry.get("timestamp", "")
        if not timestamp:
            if status == "READY":
                logger.warning(
                    "Prearchive entry for %s is READY but has no timestamp, skipping",
                    experiment_label,
                )
            return

        if status == "CONFLICT":
            logger.info("Resolving CONFLICT for %s by archiving with overwrite", experiment_label)
            error_prefix = "CONFLICT resolution failed"
            overwrite = "append"
        else:
            logger.info("Archiving prearchive entry for %s (status=READY)", experiment_label)
            error_prefix = "Archiving prearchive entry failed"
            overwrite = None

        try:
            self.archive_prearchive(
                dest_project=dest_project,
                timestamp=timestamp,
                session_name=entry.get("folderName") or entry.get("name", experiment_label),
                subject_label=subject_label,
                experiment_label=experiment_label,
                overwrite=overwrite,
            )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response else ""
            logger.error(
                "%s for %s: %s — response: %s",
                error_prefix,
                experiment_label,
                exc,
                body or "(no response body)",
            )
            raise
        except Exception:  # noqa: BLE001  # log-and-reraise: logs any archive-failure type with exc_info before propagating
            logger.error(
                "%s for %s",
                error_prefix,
                experiment_label,
                exc_info=True,
            )
            raise
