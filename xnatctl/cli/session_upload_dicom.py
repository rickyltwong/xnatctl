"""``session upload-dicom`` -- DICOM C-STORE network transfer.

Sends a directory of DICOM files to a remote SCP over the DICOM C-STORE
protocol (plaintext by default, optionally TLS). No project/subject/session
routing here: where each file lands is decided by the receiver's own DICOM
routing rules, not by anything xnatctl passes.
"""

from __future__ import annotations

import time
from pathlib import Path

import click

from xnatctl.cli.common import (
    Context,
    global_options,
    handle_errors,
    require_auth,
    resolve_workers_from_context,
)
from xnatctl.cli.session import session
from xnatctl.core.output import (
    OutputFormat,
    print_error,
    print_output,
    print_success,
)
from xnatctl.models.progress import TransferItemResult, TransferSummary, transfer_status


def _upload_dicom_store(
    ctx: Context,
    source_path: Path,
    dicom_host: str | None,
    dicom_port: int,
    called_aet: str | None,
    calling_aet: str,
    dicom_workers: int,
    tls: bool = False,
    tls_ca_bundle: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
) -> None:
    """Upload via DICOM C-STORE protocol."""

    def _emit_preflight_json(message: str) -> None:
        # These guards run before the upload_dicom_store try/except below,
        # so without this they'd exit with no JSON at all in JSON mode --
        # the same silent-failure shape the try/except fixes for everything
        # downstream of it. Only the JSON side: each guard below prints its
        # own human-mode text (a plain `print_error` for the ones that raise
        # SystemExit directly, or Click's own usage-error rendering for the
        # ones that raise UsageError -- printing both here would double it).
        if ctx.output_format == OutputFormat.JSON:
            TransferSummary(
                operation="upload",
                source=str(source_path),
                files=None,
                duration_seconds=0.0,
                status="failed",
                items=[TransferItemResult(id=str(source_path), status="failed", error=message)],
            ).emit()

    if not dicom_host:
        message = "DICOM C-STORE requires --dicom-host or XNAT_DICOM_HOST environment variable"
        _emit_preflight_json(message)
        print_error(message)
        raise SystemExit(1)

    if not called_aet:
        message = (
            "DICOM C-STORE requires --called-aet or XNAT_DICOM_CALLED_AET environment variable"
        )
        _emit_preflight_json(message)
        print_error(message)
        raise SystemExit(1)

    if not source_path.is_dir():
        message = "DICOM C-STORE requires a directory of DICOM files, not an archive"
        _emit_preflight_json(message)
        print_error(message)
        raise SystemExit(1)

    # Deferred: tests monkeypatch xnatctl.services.upload.UploadService to
    # intercept the lookup; a module-scope import would bind the real class
    # before the patch runs (see tests/test_upload_exit_codes.py).
    from xnatctl.services.upload import UploadService

    if tls_key and not tls_cert:
        message = "--tls-key requires --tls-cert"
        _emit_preflight_json(message)
        raise click.UsageError(message)
    if (tls_ca_bundle or tls_cert) and not tls:
        message = "--tls-ca-bundle/--tls-cert/--tls-key have no effect without --tls"
        _emit_preflight_json(message)
        raise click.UsageError(message)

    if not ctx.quiet:
        scheme = "TLS" if tls else "plaintext"
        click.echo(
            f"Sending DICOM files to {dicom_host}:{dicom_port} ({called_aet}) over {scheme}...",
            err=True,
        )
        if not tls:
            # Said once, plainly, on stderr. Sites that mean to do this are not
            # helped by a warning; sites that did not know need to be told.
            click.echo(
                "  Note: DICOM C-STORE is unencrypted. Use --tls if the server supports it.",
                err=True,
            )

    client = ctx.get_client()
    service = UploadService(client)

    # No -P/-S/-E on this command: C-STORE is native DICOM network transfer,
    # and where each file lands is decided by the receiver's own routing
    # rules, not by anything xnatctl passes -- there is no session/project to
    # report (see the C-STORE section of docs/uploading.rst).
    upload_start = time.time()
    try:
        summary = service.upload_dicom_store(
            dicom_root=source_path,
            host=dicom_host,
            called_aet=called_aet,
            port=dicom_port,
            calling_aet=calling_aet,
            workers=dicom_workers,
            cleanup=True,
            tls=tls,
            tls_ca_bundle=tls_ca_bundle,
            tls_cert=tls_cert,
            tls_key=tls_key,
        )
    except Exception as exc:  # noqa: BLE001  # emits JSON failure summary before re-raising unchanged
        if ctx.output_format == OutputFormat.JSON:
            TransferSummary(
                operation="upload",
                source=str(source_path),
                files=None,
                duration_seconds=round(time.time() - upload_start, 3),
                status="failed",
                items=[
                    TransferItemResult(
                        id=f"{dicom_host}:{dicom_port}", status="failed", error=str(exc)
                    )
                ],
            ).emit()
        raise

    if ctx.output_format == OutputFormat.JSON:
        TransferSummary(
            operation="upload",
            source=str(source_path),
            # `sent` is the service's own successfully-transferred count,
            # distinct from `total_files` (everything scanned, sent or not).
            files=summary.sent,
            duration_seconds=round(time.time() - upload_start, 3),
            status=transfer_status(
                succeeded=summary.sent, failed=summary.failed, success=summary.success
            ),
            items=[
                TransferItemResult(
                    id=f"{dicom_host}:{dicom_port}",
                    status="success" if summary.success else "failed",
                    error=None
                    if summary.success
                    else f"{summary.failed}/{summary.total_files} files failed",
                )
            ],
        ).emit()
    else:
        if summary.success:
            print_success(f"Sent {summary.sent}/{summary.total_files} DICOM files")
        else:
            print_error(
                f"DICOM C-STORE completed with errors: "
                f"{summary.failed}/{summary.total_files} files failed"
            )
            click.echo(f"Check logs in: {summary.log_dir}", err=True)

    # Format-independent failure exit: -o json must also return nonzero.
    if not summary.success:
        raise SystemExit(1)


@session.command("upload-dicom")
@click.argument("input_path", type=click.Path(exists=True))
@click.option(
    "--host",
    envvar="XNAT_DICOM_HOST",
    required=True,
    help="DICOM SCP host (env: XNAT_DICOM_HOST)",
)
@click.option(
    "--called-aet",
    envvar="XNAT_DICOM_CALLED_AET",
    required=True,
    help="Called AE Title (env: XNAT_DICOM_CALLED_AET)",
)
@click.option(
    "--port",
    type=int,
    default=104,
    show_default=True,
    envvar="XNAT_DICOM_PORT",
    help="DICOM SCP port (env: XNAT_DICOM_PORT)",
)
@click.option(
    "--calling-aet",
    default="XNATCTL",
    show_default=True,
    hidden=True,
    envvar="XNAT_DICOM_CALLING_AET",
    help="Calling AE Title (env: XNAT_DICOM_CALLING_AET)",
)
@click.option(
    "--workers",
    "-w",
    type=int,
    default=None,
    show_default="4 (or profile)",
    help="Parallel DICOM C-STORE associations",
)
@click.option(
    "--tls",
    is_flag=True,
    envvar="XNAT_DICOM_TLS",
    help="Encrypt the DICOM association (env: XNAT_DICOM_TLS)",
)
@click.option(
    "--tls-ca-bundle",
    type=click.Path(exists=True, dir_okay=False),
    envvar="XNAT_DICOM_TLS_CA_BUNDLE",
    help="PEM file of CAs to trust for --tls (default: system store)",
)
@click.option(
    "--tls-cert",
    type=click.Path(exists=True, dir_okay=False),
    envvar="XNAT_DICOM_TLS_CERT",
    help="Client certificate, for SCPs requiring mutual TLS",
)
@click.option(
    "--tls-key",
    type=click.Path(exists=True, dir_okay=False),
    envvar="XNAT_DICOM_TLS_KEY",
    help="Private key for --tls-cert",
)
@click.option("--dry-run", is_flag=True, help="Preview without sending")
@global_options
@handle_errors
@require_auth
def session_upload_dicom(
    ctx: Context,
    input_path: str,
    host: str,
    called_aet: str,
    port: int,
    calling_aet: str,
    workers: int | None,
    tls: bool,
    tls_ca_bundle: str | None,
    tls_cert: str | None,
    tls_key: str | None,
    dry_run: bool,
) -> None:
    """Upload DICOM files via C-STORE network protocol.

    \b
    Example:
        xnatctl session upload-dicom ./dicoms --host xnat.example.org --called-aet XNAT
        xnatctl session upload-dicom ./dicoms --host xnat.example.org --called-aet XNAT --port 8104
        xnatctl session upload-dicom ./dicoms --host xnat.example.org --called-aet XNAT -w 8
    """
    source_path = Path(input_path)

    workers = resolve_workers_from_context(ctx, workers)

    if dry_run:
        if ctx.output_format == OutputFormat.JSON:
            print_output(
                {
                    "operation": "upload",
                    "dry_run": True,
                    "source": str(source_path),
                    "host": host,
                    "port": port,
                    "called_aet": called_aet,
                    "calling_aet": calling_aet,
                    "workers": workers,
                    "transport": "tls" if tls else "plaintext",
                },
                format=OutputFormat.JSON,
            )
            return
        click.echo("[DRY-RUN] Would send DICOM files via C-STORE:", err=True)
        click.echo(f"  Source: {source_path}", err=True)
        click.echo(f"  Host: {host}:{port}", err=True)
        click.echo(f"  Called AET: {called_aet}", err=True)
        click.echo(f"  Calling AET: {calling_aet}", err=True)
        click.echo(f"  Workers: {workers}", err=True)
        click.echo(f"  Transport: {'TLS' if tls else 'plaintext'}", err=True)
        return

    _upload_dicom_store(
        ctx=ctx,
        source_path=source_path,
        dicom_host=host,
        dicom_port=port,
        called_aet=called_aet,
        calling_aet=calling_aet,
        dicom_workers=workers,
        tls=tls,
        tls_ca_bundle=tls_ca_bundle,
        tls_cert=tls_cert,
        tls_key=tls_key,
    )
