"""Raw API access commands for xnatctl.

Provides direct access to XNAT REST endpoints as an escape hatch.
"""

from __future__ import annotations

from urllib.parse import quote

import click

from xnatctl.cli.common import (
    Context,
    global_options,
    handle_errors,
    require_auth,
)
from xnatctl.core.output import OutputFormat, print_json, print_output


def _split_param(param: str) -> tuple[str, str] | None:
    """Split a ``key=value`` param at the first ``=`` outside brackets.

    XNAT field paths may contain ``=`` inside bracket expressions
    (e.g. ``field[name=session_type]/field=Research``).  A naive
    ``split("=", 1)`` would split on the wrong ``=``.

    Args:
        param: A ``key=value`` string.

    Returns:
        ``(key, value)`` tuple, or ``None`` if no valid split found.
    """
    depth = 0
    for i, ch in enumerate(param):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "=" and depth == 0:
            return param[:i], param[i + 1 :]
    return None


def _is_text_content_type(content_type: str) -> bool:
    """Check if a Content-Type header indicates text content.

    Args:
        content_type: The Content-Type header value.

    Returns:
        True if the content type is text-based.
    """
    ct = content_type.lower().split(";")[0].strip()
    if ct.startswith("text/"):
        return True
    return ct in {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/javascript",
    }


def _build_query_string(params: tuple) -> str:
    """Build a raw query string preserving special chars in keys.

    httpx URL-encodes query parameter keys (e.g. ``xnat:mrSessionData``
    becomes ``xnat%3AmrSessionData``), which XNAT rejects.  This helper
    builds a pre-encoded query string where colons, slashes, and other
    characters in keys are preserved verbatim.

    Args:
        params: Tuple of ``key=value`` strings from Click ``-P`` options.

    Returns:
        A ``key=value&...`` query string (empty string if no params).
    """
    parts: list[str] = []
    for param in params:
        result = _split_param(param)
        if result is not None:
            key, value = result
            # Preserve key verbatim (XNAT XSI paths contain :, /, [], =)
            # Only percent-encode the value for safety (spaces, etc.)
            encoded_value = quote(value, safe=":/[]@!$&'()*+,;=-._~")
            parts.append(f"{key}={encoded_value}")
    return "&".join(parts)


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

    qs = _build_query_string(params)
    url = f"{path}?{qs}" if qs else path

    resp = client.get(url)

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
    except Exception as exc:
        if ctx.output_format == OutputFormat.JSON:
            raise click.ClickException(
                "Response is not JSON; cannot format as JSON. "
                "Omit -o json to get raw response content."
            ) from exc
        content_type = resp.headers.get("content-type", "")
        if _is_text_content_type(content_type):
            click.echo(resp.text)
        else:
            click.echo(resp.content, nl=False)


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

    qs = _build_query_string(params)
    url = f"{path}?{qs}" if qs else path

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
        url,
        json=json_body,
        data=body,
    )

    click.echo(f"[{resp.status_code}] POST {path}", err=True)
    try:
        result = resp.json()
        print_json(result)
    except Exception:
        if resp.text:
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

    qs = _build_query_string(params)
    url = f"{path}?{qs}" if qs else path

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
        url,
        json=json_body,
        data=body,
    )

    click.echo(f"[{resp.status_code}] PUT {path}", err=True)
    try:
        result = resp.json()
        print_json(result)
    except Exception:
        if resp.text:
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

    qs = _build_query_string(params)
    url = f"{path}?{qs}" if qs else path

    resp = client.delete(url)

    if resp.status_code in (200, 204):
        click.echo(f"Deleted: {path}")
    else:
        try:
            result = resp.json()
            print_json(result)
        except Exception:
            click.echo(resp.text)
