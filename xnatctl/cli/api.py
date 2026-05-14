"""Raw API access commands for xnatctl.

Provides direct access to XNAT REST endpoints as an escape hatch.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import click

from xnatctl.cli.common import (
    Context,
    global_options,
    handle_errors,
    require_auth,
)
from xnatctl.core.output import OutputFormat, print_json, print_output

# Mapping of lowercased file extensions to MIME types used by
# ``_detect_content_type``.  Kept module-level so the table is allocated once.
_EXTENSION_CONTENT_TYPES: dict[str, str] = {
    ".json": "application/json",
    ".txt": "text/plain",
    ".xml": "application/xml",
}


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


def _detect_content_type(
    file_path: Path | str | None,
    raw_bytes: bytes | None,
    decoded_text: str | None,
) -> str | None:
    """Auto-detect a request ``Content-Type`` from a file body.

    Args:
        file_path: Path supplied via ``-f/--file``, or ``None`` when no
            file is in use.
        raw_bytes: The file's bytes as read from disk, or ``None`` when
            no file is in use.  Reserved for future content-sniffing
            extensions; not inspected by the current rule set.
        decoded_text: The file's UTF-8 decoded text, or ``None`` when
            decoding failed.  ``None`` always maps to
            ``application/octet-stream`` regardless of extension.

    Returns:
        A MIME type string when detection succeeds, or ``None`` when the
        caller should fall back to the existing behavior.
    """
    del raw_bytes  # accepted for parity with the contract; not used yet
    if file_path is None:
        return None
    if decoded_text is None:
        return "application/octet-stream"
    ext = os.path.splitext(os.fspath(file_path))[1].lower()
    if ext in _EXTENSION_CONTENT_TYPES:
        return _EXTENSION_CONTENT_TYPES[ext]
    return "application/octet-stream"


def _build_query_string(params: tuple) -> str:
    """Build a raw query string preserving special chars in keys.

    httpx URL-encodes query parameter keys (e.g. ``xnat:mrSessionData``
    becomes ``xnat%3AmrSessionData``), which XNAT rejects.  This helper
    builds a pre-encoded query string where colons, slashes, and other
    characters in keys are preserved verbatim.

    Args:
        params: Tuple of ``key=value`` strings from Click ``--params`` options.

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
@click.option(
    "--content-type",
    "-t",
    "content_type",
    default=None,
    help=(
        "Override request Content-Type (e.g. text/plain for XNAT XSync "
        "endpoints that return 415 on application/json). Auto-detected "
        "from file extension when -f is used (.json, .txt, .xml; other "
        "extensions or non-UTF-8 content fall back to "
        "application/octet-stream)."
    ),
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
    content_type: str | None,
) -> None:
    """POST request to any XNAT endpoint.

    Binary files (DICOM, ZIP archives, vendor blobs, etc.) are supported via
    ``--file/-f``: payloads that are not valid UTF-8 are sent as raw bytes
    without text decoding.

    Use ``--content-type/-t`` to send a non-JSON body.  This is required for
    XNAT XSync endpoints (e.g. ``/xapi/xsync/credentials/...``) which
    respond with ``415 Unsupported Media Type`` unless the request is sent
    as ``text/plain``.

    Examples:

        xnatctl api post /data/projects --data '{"ID": "NEWPROJ"}'

        xnatctl api post /data/services/import --file payload.json

        xnatctl api post /data/.../files/foo.dcm -f ./foo.dcm

        xnatctl api post /xapi/xsync/credentials/check/projects/PROJ \\
            -d 'user:pass' -t text/plain
    """
    import json as json_module

    client = ctx.get_client()

    qs = _build_query_string(params)
    url = f"{path}?{qs}" if qs else path

    # Read body and try the existing UTF-8 -> JSON -> raw text/bytes ladder.
    # Preserve the original ``raw`` bytes and ``text`` decode so the
    # explicit-content-type branch can send the body verbatim via data=
    # without re-serializing through json.dumps.
    body: bytes | str | None = None
    json_body = None
    raw_bytes: bytes | None = None
    decoded_text: str | None = None

    if file_path:
        with open(file_path, "rb") as f:
            raw_bytes = f.read()
        try:
            decoded_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            body = raw_bytes
        else:
            try:
                json_body = json_module.loads(decoded_text)
            except json_module.JSONDecodeError:
                body = decoded_text
    elif data is not None:
        try:
            json_body = json_module.loads(data)
        except json_module.JSONDecodeError:
            body = data

    effective_type = content_type or _detect_content_type(file_path, raw_bytes, decoded_text)
    headers: dict[str, str] | None = None

    # Route body via data= when an explicit/non-JSON content type is in
    # effect; httpx would otherwise force application/json on json=.
    if content_type is not None:
        if json_body is not None and body is None:
            # User passed -t with -d '{...}' or a JSON file; send the
            # original text/bytes verbatim, do not re-serialize through
            # json= or json.dumps.
            body = decoded_text if file_path is not None else data
            json_body = None
        headers = {"Content-Type": content_type}
    elif effective_type is not None and effective_type != "application/json":
        # Auto-detected non-JSON file (.txt, .xml, octet-stream).  Drop
        # any speculative json_body and send the original bytes/text.
        if json_body is not None and body is None:
            body = decoded_text if file_path is not None else data
            json_body = None
        headers = {"Content-Type": effective_type}

    resp = client.post(
        url,
        json=json_body,
        data=body,
        headers=headers,
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
@click.option(
    "--content-type",
    "-t",
    "content_type",
    default=None,
    help=(
        "Override request Content-Type (e.g. text/plain for XNAT XSync "
        "endpoints that return 415 on application/json). Auto-detected "
        "from file extension when -f is used (.json, .txt, .xml; other "
        "extensions or non-UTF-8 content fall back to "
        "application/octet-stream)."
    ),
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
    content_type: str | None,
) -> None:
    """PUT request to any XNAT endpoint.

    Binary files (DICOM, ZIP archives, vendor blobs, etc.) are supported via
    ``--file/-f``: payloads that are not valid UTF-8 are sent as raw bytes
    without text decoding.

    Use ``--content-type/-t`` to send a non-JSON body.  This is required
    for XNAT XSync endpoints (e.g.
    ``/xapi/xsync/credentials/save/projects/PROJ``) which respond with
    ``415 Unsupported Media Type`` unless the request is sent as
    ``text/plain``.

    Examples:

        xnatctl api put /data/projects/MYPROJ --data '{"description": "Updated"}'

        xnatctl api put /data/.../files/foo.dcm -f ./foo.dcm

        xnatctl api put /data/projects/PROJ -f project.xml

        xnatctl api put /xapi/xsync/credentials/save/projects/PROJ \\
            -f ./creds.txt -t text/plain
    """
    import json as json_module

    client = ctx.get_client()

    qs = _build_query_string(params)
    url = f"{path}?{qs}" if qs else path

    # Read body and try the existing UTF-8 -> JSON -> raw text/bytes ladder.
    # Preserve the original ``raw`` bytes and ``text`` decode so the
    # explicit-content-type branch can send the body verbatim via data=
    # without re-serializing through json.dumps.
    body: bytes | str | None = None
    json_body = None
    raw_bytes: bytes | None = None
    decoded_text: str | None = None

    if file_path:
        with open(file_path, "rb") as f:
            raw_bytes = f.read()
        try:
            decoded_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            body = raw_bytes
        else:
            try:
                json_body = json_module.loads(decoded_text)
            except json_module.JSONDecodeError:
                body = decoded_text
    elif data is not None:
        try:
            json_body = json_module.loads(data)
        except json_module.JSONDecodeError:
            body = data

    effective_type = content_type or _detect_content_type(file_path, raw_bytes, decoded_text)
    headers: dict[str, str] | None = None

    # Route body via data= when an explicit/non-JSON content type is in
    # effect; httpx would otherwise force application/json on json=.
    if content_type is not None:
        if json_body is not None and body is None:
            body = decoded_text if file_path is not None else data
            json_body = None
        headers = {"Content-Type": content_type}
    elif effective_type is not None and effective_type != "application/json":
        if json_body is not None and body is None:
            body = decoded_text if file_path is not None else data
            json_body = None
        headers = {"Content-Type": effective_type}

    resp = client.put(
        url,
        json=json_body,
        data=body,
        headers=headers,
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
