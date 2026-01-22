"""Pipeline commands for xnatctl."""

from __future__ import annotations

import click

from xnatctl.core.auth import AuthManager
from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config
from xnatctl.core.exceptions import (
    AuthenticationError,
    OperationError,
    ResourceNotFoundError,
)
from xnatctl.core.output import (
    print_error,
    print_json,
    print_success,
    print_table,
)
from xnatctl.services.pipelines import PipelineService


def get_client(profile_name: str | None = None) -> XNATClient:
    """Get authenticated client."""
    config = Config.load()
    auth_mgr = AuthManager()

    profile = config.get_profile(profile_name)
    session_token = auth_mgr.get_session_token(profile.url)
    env_user, env_pass = auth_mgr.get_credentials()

    if session_token:
        return XNATClient(
            base_url=profile.url,
            session_token=session_token,
            verify_ssl=profile.verify_ssl,
            timeout=profile.timeout,
        )
    elif env_user and env_pass:
        client = XNATClient(
            base_url=profile.url,
            username=env_user,
            password=env_pass,
            verify_ssl=profile.verify_ssl,
            timeout=profile.timeout,
        )
        client.authenticate()
        return client
    else:
        raise AuthenticationError(reason="No credentials found")


@click.group()
def pipeline() -> None:
    """Manage XNAT pipelines."""
    pass


@pipeline.command("list")
@click.option("--profile", "-p", "profile_name", help="Config profile to use")
@click.option("--project", help="Filter by project ID")
@click.option("--output", "-o", type=click.Choice(["json", "table"]), default="table")
@click.option("--quiet", "-q", is_flag=True, help="Only output pipeline names")
def pipeline_list(
    profile_name: str | None,
    project: str | None,
    output: str,
    quiet: bool,
) -> None:
    """List available pipelines.

    Example:
        xnatctl pipeline list
        xnatctl pipeline list --project MYPROJ
    """
    try:
        client = get_client(profile_name)
        service = PipelineService(client)

        pipelines = service.list(project=project)

        if quiet:
            for p in pipelines:
                click.echo(p.get("name", p.get("Name", "")))
        elif output == "json":
            print_json(pipelines)
        else:
            columns = ["name", "description", "version", "path"]
            print_table(pipelines, columns, title="Available Pipelines")

    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        raise SystemExit(2) from e
    except Exception as e:
        print_error(str(e))
        raise SystemExit(1) from e
    finally:
        if "client" in locals():
            client.close()


@pipeline.command("run")
@click.argument("pipeline_name")
@click.option("--profile", "-p", "profile_name", help="Config profile to use")
@click.option("--experiment", "-e", required=True, help="Experiment/session ID")
@click.option("--param", "-P", multiple=True, help="Pipeline parameter (key=value)")
@click.option("--wait", "-w", is_flag=True, help="Wait for completion")
@click.option("--timeout", type=int, default=3600, help="Wait timeout in seconds")
@click.option("--output", "-o", type=click.Choice(["json", "table"]), default="table")
def pipeline_run(
    pipeline_name: str,
    profile_name: str | None,
    experiment: str,
    param: tuple[str, ...],
    wait: bool,
    timeout: int,
    output: str,
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

    try:
        client = get_client(profile_name)
        service = PipelineService(client)

        click.echo(f"Starting pipeline {pipeline_name} on {experiment}...")

        result = service.run(
            pipeline_name=pipeline_name,
            experiment_id=experiment,
            params=params if params else None,
        )

        job_id = result.get("job_id")

        if wait and job_id:
            click.echo(f"Waiting for job {job_id} to complete...")

            def progress_callback(status: dict) -> None:
                """Print pipeline job status updates during wait."""
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
                print_success(f"Pipeline completed: {final_status.get('status', 'Complete')}")
            except OperationError as e:
                print_error(str(e))
                raise SystemExit(1) from e

        if output == "json":
            print_json(result)
        elif not wait:
            print_success(f"Pipeline started. Job ID: {job_id}")

    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        raise SystemExit(2) from e
    except ResourceNotFoundError as e:
        print_error(str(e))
        raise SystemExit(1) from e
    except Exception as e:
        print_error(str(e))
        raise SystemExit(1) from e
    finally:
        if "client" in locals():
            client.close()


@pipeline.command("status")
@click.argument("job_id")
@click.option("--profile", "-p", "profile_name", help="Config profile to use")
@click.option("--watch", "-w", is_flag=True, help="Watch status until completion")
@click.option("--interval", type=int, default=30, help="Poll interval in seconds")
@click.option("--output", "-o", type=click.Choice(["json", "table"]), default="table")
def pipeline_status(
    job_id: str,
    profile_name: str | None,
    watch: bool,
    interval: int,
    output: str,
) -> None:
    """Check pipeline job status.

    Example:
        xnatctl pipeline status JOB123
        xnatctl pipeline status JOB123 --watch
    """
    try:
        client = get_client(profile_name)
        service = PipelineService(client)

        if watch:
            click.echo(f"Watching job {job_id}...")

            def progress_callback(status: dict) -> None:
                """Print pipeline job status updates during watch."""
                job_status = status.get("status", "unknown")
                click.echo(f"  Status: {job_status}")

            try:
                final_status = service.wait(
                    job_id=job_id,
                    timeout=86400,  # 24 hours
                    poll_interval=interval,
                    progress_callback=progress_callback,
                )
                if output == "json":
                    print_json(final_status)
                else:
                    print_success(f"Job completed: {final_status.get('status', 'Complete')}")
            except OperationError as e:
                print_error(str(e))
                raise SystemExit(1) from e
        else:
            status = service.status(job_id)

            if output == "json":
                print_json(status)
            else:
                click.echo(f"Job ID: {job_id}")
                click.echo(f"Status: {status.get('status', 'unknown')}")
                if status.get("message"):
                    click.echo(f"Message: {status['message']}")
                if status.get("start_time"):
                    click.echo(f"Started: {status['start_time']}")
                if status.get("end_time"):
                    click.echo(f"Ended: {status['end_time']}")

    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        raise SystemExit(2) from e
    except Exception as e:
        print_error(str(e))
        raise SystemExit(1) from e
    finally:
        if "client" in locals():
            client.close()


@pipeline.command("cancel")
@click.argument("job_id")
@click.option("--profile", "-p", "profile_name", help="Config profile to use")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def pipeline_cancel(
    job_id: str,
    profile_name: str | None,
    yes: bool,
) -> None:
    """Cancel a running pipeline job.

    Example:
        xnatctl pipeline cancel JOB123 --yes
    """
    if not yes:
        click.confirm(f"Cancel job {job_id}?", abort=True)

    try:
        client = get_client(profile_name)
        service = PipelineService(client)

        service.cancel(job_id)
        print_success(f"Cancelled job {job_id}")

    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        raise SystemExit(2) from e
    except Exception as e:
        print_error(str(e))
        raise SystemExit(1) from e
    finally:
        if "client" in locals():
            client.close()


@pipeline.command("jobs")
@click.option("--profile", "-p", "profile_name", help="Config profile to use")
@click.option("--experiment", "-e", help="Filter by experiment ID")
@click.option("--project", help="Filter by project ID")
@click.option("--status", "-s", help="Filter by status")
@click.option("--limit", type=int, default=100, help="Maximum results")
@click.option("--output", "-o", type=click.Choice(["json", "table"]), default="table")
def pipeline_jobs(
    profile_name: str | None,
    experiment: str | None,
    project: str | None,
    status: str | None,
    limit: int,
    output: str,
) -> None:
    """List pipeline jobs.

    Example:
        xnatctl pipeline jobs
        xnatctl pipeline jobs --experiment XNAT_E00001
        xnatctl pipeline jobs --status Running
    """
    try:
        client = get_client(profile_name)
        service = PipelineService(client)

        jobs = service.list_jobs(
            experiment_id=experiment,
            project=project,
            status=status,
            limit=limit,
        )

        if output == "json":
            print_json(jobs)
        else:
            columns = ["id", "pipeline", "experiment", "status", "start_time", "end_time"]
            print_table(jobs, columns, title="Pipeline Jobs")

    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        raise SystemExit(2) from e
    except Exception as e:
        print_error(str(e))
        raise SystemExit(1) from e
    finally:
        if "client" in locals():
            client.close()
