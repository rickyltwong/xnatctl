"""Pipeline commands for xnatctl."""

from __future__ import annotations

import click

from xnatctl.cli.common import Context, global_options, handle_errors, require_auth
from xnatctl.core.exceptions import OperationError
from xnatctl.core.output import OutputFormat, print_output, print_success
from xnatctl.services.pipelines import PipelineService


@click.group()
def pipeline() -> None:
    """Manage XNAT pipelines."""
    pass


@pipeline.command("list")
@click.option("--project", help="Filter by project ID")
@global_options
@require_auth
@handle_errors
def pipeline_list(
    ctx: Context,
    project: str | None,
) -> None:
    """List available pipelines.

    Example:
        xnatctl pipeline list
        xnatctl pipeline list --project MYPROJ
    """
    client = ctx.get_client()
    service = PipelineService(client)
    pipelines = service.list(project=project)

    if ctx.quiet:
        for p in pipelines:
            click.echo(p.get("name", p.get("Name", "")))
        return

    columns = ["name", "description", "version", "path"]
    print_output(pipelines, format=ctx.output_format, columns=columns, title="Available Pipelines")


@pipeline.command("run")
@click.argument("pipeline_name")
@click.option("--experiment", "-e", required=True, help="Experiment/session ID")
@click.option("--param", "-P", multiple=True, help="Pipeline parameter (key=value)")
@click.option("--wait", "-w", is_flag=True, help="Wait for completion")
@click.option("--timeout", type=int, default=3600, help="Wait timeout in seconds")
@global_options
@require_auth
@handle_errors
def pipeline_run(
    ctx: Context,
    pipeline_name: str,
    experiment: str,
    param: tuple[str, ...],
    wait: bool,
    timeout: int,
) -> None:
    """Run a pipeline on an experiment.

    Example:
        xnatctl pipeline run dcm2niix --experiment XNAT_E00001
        xnatctl pipeline run freesurfer -e XNAT_E00001 --wait
        xnatctl pipeline run myproc -e XNAT_E00001 -P param1=value1 -P param2=value2
    """
    # Parse parameters
    params = {}
    for p in param:
        if "=" in p:
            key, value = p.split("=", 1)
            params[key] = value

    client = ctx.get_client()
    service = PipelineService(client)

    if not ctx.quiet:
        click.echo(f"Starting pipeline {pipeline_name} on {experiment}...")

    result = service.run(
        pipeline_name=pipeline_name,
        experiment_id=experiment,
        params=params if params else None,
    )

    job_id = result.get("job_id")

    if wait and job_id:
        if not ctx.quiet:
            click.echo(f"Waiting for job {job_id} to complete...")

        def progress_callback(status: dict) -> None:
            """Print pipeline job status updates during wait."""
            if ctx.quiet:
                return
            job_status = status.get("status", "unknown")
            click.echo(f"  Status: {job_status}")

        try:
            final_status = service.wait(
                job_id=job_id,
                timeout=timeout,
                poll_interval=30,
                progress_callback=progress_callback,
            )
            result["final_status"] = final_status
            if not ctx.quiet:
                print_success(f"Pipeline completed: {final_status.get('status', 'Complete')}")
        except OperationError as e:
            raise click.ClickException(str(e)) from e

    if ctx.output_format == OutputFormat.JSON:
        print_output(result, format=OutputFormat.JSON)
    elif not wait and not ctx.quiet:
        print_success(f"Pipeline started. Job ID: {job_id}")


@pipeline.command("status")
@click.argument("job_id")
@click.option("--watch", "-w", is_flag=True, help="Watch status until completion")
@click.option("--interval", type=int, default=30, help="Poll interval in seconds")
@global_options
@require_auth
@handle_errors
def pipeline_status(
    ctx: Context,
    job_id: str,
    watch: bool,
    interval: int,
) -> None:
    """Check pipeline job status.

    Example:
        xnatctl pipeline status JOB123
        xnatctl pipeline status JOB123 --watch
    """
    client = ctx.get_client()
    service = PipelineService(client)

    if watch:
        if not ctx.quiet:
            click.echo(f"Watching job {job_id}...")

        def progress_callback(status: dict) -> None:
            """Print pipeline job status updates during watch."""
            if ctx.quiet:
                return
            job_status = status.get("status", "unknown")
            click.echo(f"  Status: {job_status}")

        try:
            final_status = service.wait(
                job_id=job_id,
                timeout=86400,  # 24 hours
                poll_interval=interval,
                progress_callback=progress_callback,
            )
        except OperationError as e:
            raise click.ClickException(str(e)) from e

        if ctx.output_format == OutputFormat.JSON:
            print_output(final_status, format=OutputFormat.JSON)
        elif not ctx.quiet:
            print_success(f"Job completed: {final_status.get('status', 'Complete')}")
        return

    status = service.status(job_id)
    if ctx.output_format == OutputFormat.JSON:
        print_output(status, format=OutputFormat.JSON)
    elif not ctx.quiet:
        click.echo(f"Job ID: {job_id}")
        click.echo(f"Status: {status.get('status', 'unknown')}")
        if status.get("message"):
            click.echo(f"Message: {status['message']}")
        if status.get("start_time"):
            click.echo(f"Started: {status['start_time']}")
        if status.get("end_time"):
            click.echo(f"Ended: {status['end_time']}")


@pipeline.command("cancel")
@click.argument("job_id")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@global_options
@require_auth
@handle_errors
def pipeline_cancel(
    ctx: Context,
    job_id: str,
    yes: bool,
) -> None:
    """Cancel a running pipeline job.

    Example:
        xnatctl pipeline cancel JOB123 --yes
    """
    if not yes:
        click.confirm(f"Cancel job {job_id}?", abort=True)

    client = ctx.get_client()
    service = PipelineService(client)
    service.cancel(job_id)
    print_success(f"Cancelled job {job_id}")


@pipeline.command("jobs")
@click.option("--experiment", "-e", help="Filter by experiment ID")
@click.option("--project", help="Filter by project ID")
@click.option("--status", "-s", help="Filter by status")
@click.option("--limit", type=int, default=100, help="Maximum results")
@global_options
@require_auth
@handle_errors
def pipeline_jobs(
    ctx: Context,
    experiment: str | None,
    project: str | None,
    status: str | None,
    limit: int,
) -> None:
    """List pipeline jobs.

    Example:
        xnatctl pipeline jobs
        xnatctl pipeline jobs --experiment XNAT_E00001
        xnatctl pipeline jobs --status Running
    """
    client = ctx.get_client()
    service = PipelineService(client)
    jobs = service.list_jobs(
        experiment_id=experiment,
        project=project,
        status=status,
        limit=limit,
    )

    columns = ["id", "pipeline", "experiment", "status", "start_time", "end_time"]
    print_output(jobs, format=ctx.output_format, columns=columns, title="Pipeline Jobs")
