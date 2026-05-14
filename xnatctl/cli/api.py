"""Raw API access commands for xnatctl.

Provides direct access to XNAT REST endpoints as an escape hatch.
"""

from __future__ import annotations

import json as json_module
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import click

from xnatctl.cli.common import (
    Context,
    global_options,
    handle_errors,
    require_auth,
)
from xnatctl.core.output import OutputFormat, print_json, print_output, print_warning

_PARAMS_HELP = (
    "Query parameters as key=value (can repeat). "
    "Values appear in the URL query string and may be logged by the XNAT "
    "server's access log. Do not use for secrets; pass credentials via "
    "-d / -f JSON body instead."
)

_STDIN_SENTINEL = "-"

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_env(raw: str) -> str:
    """Expand ``$VAR`` and ``${VAR}`` references in ``raw``.

    Any referenced variable name that is not set in ``os.environ`` is
    surfaced via :func:`print_warning` so silent empty-string substitution
    is observable. Uses :func:`os.path.expandvars`, which leaves unset
    references as their literal ``$VAR`` text on POSIX. The warning is
    sufficient to flag the leak; we do not raise.

    Args:
        raw: The unexpanded string from ``-d``.

    Returns:
        The expanded string (same text if no references were present).
    """
    referenced: set[str] = set()
    for braced, bare in _ENV_VAR_RE.findall(raw):
        name = braced or bare
        if name:
            referenced.add(name)
    if referenced:
        missing = sorted(name for name in referenced if name not in os.environ)
        if missing:
            print_warning(f"Environment variable(s) not set: {', '.join(missing)}")
    return os.path.expandvars(raw)


def _read_body(
    data: str | None,
    file_path: str | None,
) -> tuple[bytes | str | None, object, bytes | None, str | None]:
    """Resolve the request body from ``-d``, ``-f``, or stdin.

    Handles three input shapes:

    * ``-d <string>``: env-vars are expanded, then the JSON-or-raw decision
      tree is applied. JSON-decodable input becomes ``json_body``; anything
      else becomes the raw string ``body``.
    * ``-f <path>``: file contents are read as bytes. UTF-8 decoded input
      passes through the same JSON-or-raw branch. Non-UTF-8 bytes are sent
      as raw bytes (binary-safe). Env-var expansion is NOT applied to file
      contents.
    * ``-d -`` or ``-f -``: stdin is read once as bytes and follows the
      same UTF-8 / JSON-or-raw branch as ``-f``. Env-var expansion is NOT
      applied to stdin bytes. Specifying both ``-d -`` and ``-f -`` is a
      usage error.

    Args:
        data: Value of the ``-d`` / ``--data`` option (may be the literal
            ``-`` sentinel for stdin).
        file_path: Value of the ``-f`` / ``--file`` option (may be the
            literal ``-`` sentinel for stdin).

    Returns:
        ``(body, json_body, raw_bytes, decoded_text)``. ``body`` /
        ``json_body`` are the values passed to the HTTP client; at most
        one is non-None. ``raw_bytes`` and ``decoded_text`` carry the
        underlying bytes and UTF-8 decode (when available) for callers
        that need to drive content-type detection or send the body
        verbatim under an explicit ``Content-Type``. ``(None, None,
        None, None)`` indicates no body was given.

    Raises:
        click.UsageError: If both ``-d -`` and ``-f -`` are passed.
    """
    data_is_stdin = data == _STDIN_SENTINEL
    file_is_stdin = file_path == _STDIN_SENTINEL

    if data_is_stdin and file_is_stdin:
        raise click.UsageError("cannot combine -d - and -f -")

    body: bytes | str | None = None
    json_body: object = None
    raw_bytes: bytes | None = None
    decoded_text: str | None = None

    if data_is_stdin or file_is_stdin:
        raw_bytes = sys.stdin.buffer.read()
        try:
            decoded_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            body = raw_bytes
        else:
            try:
                json_body = json_module.loads(decoded_text)
            except json_module.JSONDecodeError:
                body = decoded_text
        return body, json_body, raw_bytes, decoded_text

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
        return body, json_body, raw_bytes, decoded_text

    if data:
        expanded = _expand_env(data)
        decoded_text = expanded
        try:
            json_body = json_module.loads(expanded)
        except json_module.JSONDecodeError:
            body = expanded
        return body, json_body, raw_bytes, decoded_text

    return None, None, None, None


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
    help=_PARAMS_HELP,
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
    help=_PARAMS_HELP,
)
@click.option(
    "--data",
    "-d",
    help=(
        "Request body (JSON string). Use '-' to read the body from stdin. "
        "Supports ${VAR} / $VAR expansion from the environment."
    ),
)
@click.option(
    "--file",
    "-f",
    "file_path",
    help="Read body from file. Use '-' to read from stdin.",
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
    without text decoding. Pass ``-d -`` or ``-f -`` to read the body from
    stdin (keeps secrets out of argv and shell history).

    Use ``--content-type/-t`` to send a non-JSON body.  This is required for
    XNAT XSync endpoints (e.g. ``/xapi/xsync/credentials/...``) which
    respond with ``415 Unsupported Media Type`` unless the request is sent
    as ``text/plain``.

    Examples:

        xnatctl api post /data/projects --data '{"ID": "NEWPROJ"}'

        xnatctl api post /data/services/import --file payload.json

        echo '{"k":"v"}' | xnatctl api post /data/endpoint -d -

        xnatctl api post /data/.../files/foo.dcm -f ./foo.dcm

        xnatctl api post /xapi/xsync/credentials/check/projects/PROJ \\
            -d 'user:pass' -t text/plain
    """
    client = ctx.get_client()

    qs = _build_query_string(params)
    url = f"{path}?{qs}" if qs else path

    body, json_body, raw_bytes, decoded_text = _read_body(data, file_path)

    # Stdin sentinel (-f -) has no extension to detect from; treat as no file.
    detect_file = None if file_path == _STDIN_SENTINEL else file_path
    effective_type = content_type or _detect_content_type(detect_file, raw_bytes, decoded_text)
    headers: dict[str, str] | None = None

    # Route body via data= when an explicit/non-JSON content type is in
    # effect; httpx would otherwise force application/json on json=.
    if content_type is not None:
        if json_body is not None and body is None:
            # User passed -t with text input that JSON-parsed; send the
            # original text verbatim instead of re-serializing.
            body = decoded_text
            json_body = None
        headers = {"Content-Type": content_type}
    elif effective_type is not None and effective_type != "application/json":
        # Auto-detected non-JSON file (.txt, .xml, octet-stream).  Drop
        # any speculative json_body and send the original bytes/text.
        if json_body is not None and body is None:
            body = decoded_text
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
    help=_PARAMS_HELP,
)
@click.option(
    "--data",
    "-d",
    help=(
        "Request body (JSON string). Use '-' to read the body from stdin. "
        "Supports ${VAR} / $VAR expansion from the environment."
    ),
)
@click.option(
    "--file",
    "-f",
    "file_path",
    help="Read body from file. Use '-' to read from stdin.",
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
    without text decoding. Pass ``-d -`` or ``-f -`` to read the body from
    stdin (keeps secrets out of argv and shell history).

    Use ``--content-type/-t`` to send a non-JSON body.  This is required
    for XNAT XSync endpoints (e.g.
    ``/xapi/xsync/credentials/save/projects/PROJ``) which respond with
    ``415 Unsupported Media Type`` unless the request is sent as
    ``text/plain``.

    Examples:

        xnatctl api put /data/projects/MYPROJ --data '{"description": "Updated"}'

        echo '{"description": "x"}' | xnatctl api put /data/projects/MYPROJ -d -

        xnatctl api put /data/.../files/foo.dcm -f ./foo.dcm

        xnatctl api put /data/projects/PROJ -f project.xml

        xnatctl api put /xapi/xsync/credentials/save/projects/PROJ \\
            -f ./creds.txt -t text/plain
    """
    client = ctx.get_client()

    qs = _build_query_string(params)
    url = f"{path}?{qs}" if qs else path

    body, json_body, raw_bytes, decoded_text = _read_body(data, file_path)

    detect_file = None if file_path == _STDIN_SENTINEL else file_path
    effective_type = content_type or _detect_content_type(detect_file, raw_bytes, decoded_text)
    headers: dict[str, str] | None = None

    # Route body via data= when an explicit/non-JSON content type is in
    # effect; httpx would otherwise force application/json on json=.
    if content_type is not None:
        if json_body is not None and body is None:
            body = decoded_text
            json_body = None
        headers = {"Content-Type": content_type}
    elif effective_type is not None and effective_type != "application/json":
        if json_body is not None and body is None:
            body = decoded_text
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
    help=_PARAMS_HELP,
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
