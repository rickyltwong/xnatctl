"""Pipeline service for XNAT pipeline operations."""

from __future__ import annotations

import builtins
import time
from collections.abc import Callable
from typing import Any

from xnatctl.core.exceptions import InputValidationError, OperationError, ResourceNotFoundError
from xnatctl.core.validation import quote_path_segment

from .base import BaseService


class PipelineService(BaseService):
    """Service for XNAT pipeline operations."""

    def list(
        self,
        project: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List available pipelines.

        Args:
            project: Filter by project ID

        Returns:
            List of pipeline dicts
        """
        # `is not None`, not truthy -- see list_jobs below for the rationale.
        if project is not None:
            path = f"/data/projects/{quote_path_segment(project)}/pipelines"
        else:
            path = "/data/pipelines"

        params = {"format": "json"}
        data = self._get(path, params=params)
        return self._extract_results(data)

    def get(
        self,
        pipeline_name: str,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Get pipeline details.

        Args:
            pipeline_name: Pipeline name
            project: Project ID

        Returns:
            Pipeline details dict

        Raises:
            ResourceNotFoundError: If pipeline not found
        """
        # `is not None`, not truthy -- see list_jobs below for the rationale.
        if project is not None:
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/pipelines/{quote_path_segment(pipeline_name)}"
            )
        else:
            path = f"/data/pipelines/{quote_path_segment(pipeline_name)}"

        params = {"format": "json"}

        try:
            data = self._get(path, params=params)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("pipeline", pipeline_name) from e

        if isinstance(data, dict):
            return data
        results = self._extract_results(data)
        if results:
            return results[0]
        raise ResourceNotFoundError("pipeline", pipeline_name)

    def run(
        self,
        pipeline_name: str,
        experiment_id: str,
        project: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a pipeline on an experiment.

        Args:
            pipeline_name: Pipeline name
            experiment_id: Experiment/session ID
            project: Project ID
            params: Additional pipeline parameters

        Returns:
            Job information dict with job ID
        """
        path = (
            f"/data/experiments/{quote_path_segment(experiment_id)}"
            f"/pipelines/{quote_path_segment(pipeline_name)}"
        )

        request_params: dict[str, Any] = {}
        if params:
            request_params.update(params)

        result = self._post(path, params=request_params)

        # Extract job ID from result
        job_id = None
        if isinstance(result, dict):
            job_id = result.get("jobId") or result.get("id")
        elif isinstance(result, str):
            # Sometimes returns just the job ID as text
            job_id = result.strip()

        return {
            "success": True,
            "pipeline": pipeline_name,
            "experiment": experiment_id,
            "job_id": job_id,
            "result": result,
        }

    def status(
        self,
        job_id: str,
    ) -> dict[str, Any]:
        """Get pipeline job status.

        Args:
            job_id: Job ID

        Returns:
            Job status dict
        """
        path = f"/data/pipelines/jobs/{quote_path_segment(job_id)}"
        params = {"format": "json"}

        data = self._get(path, params=params)
        if isinstance(data, dict):
            return data
        results = self._extract_results(data)
        if results:
            return results[0]
        return {"job_id": job_id, "status": "unknown"}

    def wait(
        self,
        job_id: str,
        timeout: int = 3600,
        poll_interval: int = 30,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Wait for a pipeline job to complete.

        Args:
            job_id: Job ID
            timeout: Maximum wait time in seconds
            poll_interval: Seconds between status checks
            progress_callback: Called with status on each poll

        Returns:
            Final job status dict

        Raises:
            OperationError: If job fails or times out
        """
        start_time = time.time()
        terminal_states = {"Complete", "Failed", "Error", "Killed"}

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise OperationError(
                    "pipeline_wait",
                    f"Job {job_id} timed out after {timeout}s",
                )

            status = self.status(job_id)

            if progress_callback:
                progress_callback(status)

            job_status = status.get("status", "").capitalize()
            if job_status in terminal_states:
                if job_status in ("Failed", "Error"):
                    raise OperationError(
                        "pipeline_run",
                        f"Job {job_id} failed: {status.get('message', 'Unknown error')}",
                    )
                return status

            time.sleep(poll_interval)

    def cancel(
        self,
        job_id: str,
    ) -> bool:
        """Cancel a running pipeline job.

        Args:
            job_id: Job ID

        Returns:
            True if cancelled successfully
        """
        path = f"/data/pipelines/jobs/{quote_path_segment(job_id)}"
        params = {"action": "kill"}
        self._post(path, params=params)
        return True

    def list_jobs(
        self,
        experiment_id: str | None = None,
        project: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        """List pipeline jobs.

        Args:
            experiment_id: Filter by experiment
            project: Filter by project
            status: Filter by status
            limit: Maximum results

        Returns:
            List of job dicts
        """
        # `is not None`, not truthy: an explicitly-supplied `experiment_id=""`
        # (or `project=""`) is a caller mistake, not "no filter" -- it must
        # not silently widen to the unfiltered, server-wide job listing.
        # quote_path_segment already rejects the empty string.
        if experiment_id is not None:
            path = f"/data/experiments/{quote_path_segment(experiment_id)}/pipelines/jobs"
        elif project is not None:
            path = f"/data/projects/{quote_path_segment(project)}/pipelines/jobs"
        else:
            path = "/data/pipelines/jobs"

        params: dict[str, Any] = {"format": "json", "limit": limit}
        # `is not None`, not truthy: `status=""` is a caller mistake, not
        # "no status filter" -- it must not silently widen to every status.
        if status is not None:
            if status.strip() == "":
                raise InputValidationError(
                    "status filter cannot be empty or whitespace-only",
                    field="status",
                    value=status,
                )
            params["status"] = status

        data = self._get(path, params=params)
        return self._extract_results(data)
