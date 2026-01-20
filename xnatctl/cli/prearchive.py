"""Prearchive commands for xnatctl."""

from __future__ import annotations

from typing import Optional

import click

from xnatctl.core.config import Config
from xnatctl.core.client import XNATClient
from xnatctl.core.auth import AuthManager
from xnatctl.core.exceptions import (
    AuthenticationError,
    ProfileNotFoundError,
    ResourceNotFoundError,
)
from xnatctl.core.output import (
    print_error,
    print_json,
    print_success,
    print_table,
    print_warning,
)
from xnatctl.services.prearchive import PrearchiveService


def get_client(profile_name: Optional[str] = None) -> XNATClient:
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
def prearchive() -> None:
    """Manage XNAT prearchive sessions."""
    pass


@prearchive.command("list")
@click.option("--profile", "-p", "profile_name", help="Config profile to use")
@click.option("--project", help="Filter by project ID")
@click.option("--output", "-o", type=click.Choice(["json", "table"]), default="table")
@click.option("--quiet", "-q", is_flag=True, help="Only output session paths")
def prearchive_list(
    profile_name: Optional[str],
    project: Optional[str],
    output: str,
    quiet: bool,
) -> None:
    """List prearchive sessions.

    Example:
        xnatctl prearchive list
        xnatctl prearchive list --project MYPROJ
    """
    try:
        client = get_client(profile_name)
        service = PrearchiveService(client)

        sessions = service.list(project=project)

        if quiet:
            for s in sessions:
                path = f"{s.get('project', '')}/{s.get('timestamp', '')}/{s.get('name', '')}"
                click.echo(path)
        elif output == "json":
            print_json(sessions)
        else:
            columns = ["project", "timestamp", "name", "status", "scan_date", "subject"]
            print_table(sessions, columns, title="Prearchive Sessions")

    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        raise SystemExit(2) from e
    except Exception as e:
        print_error(str(e))
        raise SystemExit(1) from e
    finally:
        if "client" in locals():
            client.close()


@prearchive.command("archive")
@click.argument("project")
@click.argument("timestamp")
@click.argument("session_name")
@click.option("--profile", "-p", "profile_name", help="Config profile to use")
@click.option("--subject", help="Target subject ID")
@click.option("--label", help="Target session label")
@click.option("--overwrite", is_flag=True, help="Overwrite existing data")
@click.option("--output", "-o", type=click.Choice(["json", "table"]), default="table")
def prearchive_archive(
    project: str,
    timestamp: str,
    session_name: str,
    profile_name: Optional[str],
    subject: Optional[str],
    label: Optional[str],
    overwrite: bool,
    output: str,
) -> None:
    """Archive a session from prearchive.

    Example:
        xnatctl prearchive archive MYPROJ 20240115_120000 Session1
        xnatctl prearchive archive MYPROJ 20240115_120000 Session1 --subject SUB001
    """
    try:
        client = get_client(profile_name)
        service = PrearchiveService(client)

        result = service.archive(
            project=project,
            timestamp=timestamp,
            session_name=session_name,
            subject=subject,
            experiment_label=label,
            overwrite=overwrite,
        )

        if output == "json":
            print_json(result)
        else:
            print_success(f"Archived {session_name} from prearchive")

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


@prearchive.command("delete")
@click.argument("project")
@click.argument("timestamp")
@click.argument("session_name")
@click.option("--profile", "-p", "profile_name", help="Config profile to use")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def prearchive_delete(
    project: str,
    timestamp: str,
    session_name: str,
    profile_name: Optional[str],
    yes: bool,
) -> None:
    """Delete a session from prearchive.

    Example:
        xnatctl prearchive delete MYPROJ 20240115_120000 Session1 --yes
    """
    if not yes:
        click.confirm(
            f"Delete {session_name} from prearchive? This cannot be undone.",
            abort=True,
        )

    try:
        client = get_client(profile_name)
        service = PrearchiveService(client)

        service.delete(
            project=project,
            timestamp=timestamp,
            session_name=session_name,
        )

        print_success(f"Deleted {session_name} from prearchive")

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


@prearchive.command("rebuild")
@click.argument("project")
@click.argument("timestamp")
@click.argument("session_name")
@click.option("--profile", "-p", "profile_name", help="Config profile to use")
def prearchive_rebuild(
    project: str,
    timestamp: str,
    session_name: str,
    profile_name: Optional[str],
) -> None:
    """Rebuild/refresh a prearchive session.

    Example:
        xnatctl prearchive rebuild MYPROJ 20240115_120000 Session1
    """
    try:
        client = get_client(profile_name)
        service = PrearchiveService(client)

        service.rebuild(
            project=project,
            timestamp=timestamp,
            session_name=session_name,
        )

        print_success(f"Rebuilt {session_name}")

    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        raise SystemExit(2) from e
    except Exception as e:
        print_error(str(e))
        raise SystemExit(1) from e
    finally:
        if "client" in locals():
            client.close()


@prearchive.command("move")
@click.argument("project")
@click.argument("timestamp")
@click.argument("session_name")
@click.argument("target_project")
@click.option("--profile", "-p", "profile_name", help="Config profile to use")
def prearchive_move(
    project: str,
    timestamp: str,
    session_name: str,
    target_project: str,
    profile_name: Optional[str],
) -> None:
    """Move a prearchive session to another project.

    Example:
        xnatctl prearchive move MYPROJ 20240115_120000 Session1 OTHERPROJ
    """
    try:
        client = get_client(profile_name)
        service = PrearchiveService(client)

        service.move(
            project=project,
            timestamp=timestamp,
            session_name=session_name,
            target_project=target_project,
        )

        print_success(f"Moved {session_name} to {target_project}")

    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        raise SystemExit(2) from e
    except Exception as e:
        print_error(str(e))
        raise SystemExit(1) from e
    finally:
        if "client" in locals():
            client.close()
