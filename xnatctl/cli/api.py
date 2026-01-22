"""Raw API access commands for xnatctl.

Provides direct access to XNAT REST endpoints as an escape hatch.
"""

from __future__ import annotations

import click

from xnatctl.cli.common import (
    Context,
    global_options,
    handle_errors,
    require_auth,
)
from xnatctl.core.output import OutputFormat, print_json, print_output


@click.group()
def api() -> None:
    """Raw API access (escape hatch).

    Execute requests directly against XNAT REST endpoints.

    Examples:

        xnatctl api get /data/projects

        xnatctl api get /data/projects/MYPROJ/subjects --params columns=ID,label

        xnatctl api post /data/services/import --file payload.json
    """
    pass


@api.command("get")
@click.argument("path")
@click.option(
    "--params",
    "-P",
    multiple=True,
    help="Query parameters as key=value (can repeat)",
)
@global_options
@require_auth
@handle_errors
def api_get(
    ctx: Context,
    path: str,
    params: tuple,
) -> None:
    """GET request to any XNAT endpoint.

    Examples:

        xnatctl api get /data/projects

        xnatctl api get /data/projects/MYPROJ/subjects --params columns=ID,label

        xnatctl api get /xapi/users -o json
    """
    client = ctx.get_client()

    # Parse params
    query_params = {}
    for param in params:
        if "=" in param:
            key, value = param.split("=", 1)
            query_params[key] = value

    resp = client.get(path, params=query_params if query_params else None)

    try:
        data = resp.json()
        if ctx.output_format == OutputFormat.JSON:
            print_json(data)
        else:
            # Try to extract ResultSet.Result for table display
            if isinstance(data, dict) and "ResultSet" in data:
                results = data.get("ResultSet", {}).get("Result", [])
                if results and isinstance(results, list):
                    columns = list(results[0].keys()) if results else []
                    print_output(results, format=ctx.output_format, columns=columns)
                else:
                    print_json(data)
            elif isinstance(data, list):
                if data and isinstance(data[0], dict):
                    columns = list(data[0].keys())
                    print_output(data, format=ctx.output_format, columns=columns)
                else:
                    print_json(data)
            else:
                print_json(data)
    except Exception:
        # Not JSON, print raw text
        click.echo(resp.text)


@api.command("post")
@click.argument("path")
@click.option(
    "--params",
    "-P",
    multiple=True,
    help="Query parameters as key=value (can repeat)",
)
@click.option(
    "--data",
    "-d",
    help="Request body (JSON string)",
)
@click.option(
    "--file",
    "-f",
    "file_path",
    type=click.Path(exists=True),
    help="Read body from file",
)
@global_options
@require_auth
@handle_errors
def api_post(
    ctx: Context,
    path: str,
    params: tuple,
    data: str | None,
    file_path: str | None,
) -> None:
    """POST request to any XNAT endpoint.

    Examples:

        xnatctl api post /data/projects --data '{"ID": "NEWPROJ"}'

        xnatctl api post /data/services/import --file payload.json
    """
    import json as json_module

    client = ctx.get_client()

    # Parse params
    query_params = {}
    for param in params:
        if "=" in param:
            key, value = param.split("=", 1)
            query_params[key] = value

    # Get body
    body = None
    json_body = None

    if file_path:
        with open(file_path) as f:
            content = f.read()
        try:
            json_body = json_module.loads(content)
        except json_module.JSONDecodeError:
            body = content
    elif data:
        try:
            json_body = json_module.loads(data)
        except json_module.JSONDecodeError:
            body = data

    resp = client.post(
        path,
        params=query_params if query_params else None,
        json=json_body,
        data=body,
    )

    try:
        result = resp.json()
        print_json(result)
    except Exception:
        click.echo(resp.text)


@api.command("put")
@click.argument("path")
@click.option(
    "--params",
    "-P",
    multiple=True,
    help="Query parameters as key=value (can repeat)",
)
@click.option(
    "--data",
    "-d",
    help="Request body (JSON string)",
)
@click.option(
    "--file",
    "-f",
    "file_path",
    type=click.Path(exists=True),
    help="Read body from file",
)
@global_options
@require_auth
@handle_errors
def api_put(
    ctx: Context,
    path: str,
    params: tuple,
    data: str | None,
    file_path: str | None,
) -> None:
    """PUT request to any XNAT endpoint.

    Examples:

        xnatctl api put /data/projects/MYPROJ --data '{"description": "Updated"}'
    """
    import json as json_module

    client = ctx.get_client()

    # Parse params
    query_params = {}
    for param in params:
        if "=" in param:
            key, value = param.split("=", 1)
            query_params[key] = value

    # Get body
    body = None
    json_body = None

    if file_path:
        with open(file_path) as f:
            content = f.read()
        try:
            json_body = json_module.loads(content)
        except json_module.JSONDecodeError:
            body = content
    elif data:
        try:
            json_body = json_module.loads(data)
        except json_module.JSONDecodeError:
            body = data

    resp = client.put(
        path,
        params=query_params if query_params else None,
        json=json_body,
        data=body,
    )

    try:
        result = resp.json()
        print_json(result)
    except Exception:
        click.echo(resp.text)


@api.command("delete")
@click.argument("path")
@click.option(
    "--params",
    "-P",
    multiple=True,
    help="Query parameters as key=value (can repeat)",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation",
)
@global_options
@require_auth
@handle_errors
def api_delete(
    ctx: Context,
    path: str,
    params: tuple,
    yes: bool,
) -> None:
    """DELETE request to any XNAT endpoint.

    Examples:

        xnatctl api delete /data/projects/MYPROJ/subjects/SUB001 --yes
    """
    if not yes:
        click.confirm(f"Delete {path}?", abort=True)

    client = ctx.get_client()

    # Parse params
    query_params = {}
    for param in params:
        if "=" in param:
            key, value = param.split("=", 1)
            query_params[key] = value

    resp = client.delete(path, params=query_params if query_params else None)

    if resp.status_code in (200, 204):
        click.echo(f"Deleted: {path}")
    else:
        try:
            result = resp.json()
            print_json(result)
        except Exception:
            click.echo(resp.text)
