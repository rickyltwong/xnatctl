"""DICOM C-STORE network transport (pynetdicom).

Sends files directly to a DICOM SCP over one or more C-STORE associations,
bypassing the XNAT REST import service entirely. Independent of any XNAT
client -- it only needs host/port/AE-title connection details.

Every ``pynetdicom``/``pydicom`` import in this module is function-local by
design, never module-scope: this module is imported unconditionally by
``services.upload`` (``from . import dicom_store, ...``), which every REST
upload path pulls in too, so a module-scope import would make pynetdicom's
(non-trivial) import cost part of every upload, not just ``session
upload-dicom``.
"""

from __future__ import annotations

import logging
import shutil
import ssl
import tempfile
from concurrent.futures import as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xnatctl.core.cancellation import cancellable_pool
from xnatctl.core.exceptions import UploadError

from .shared import collect_dicom_files, split_into_n_batches

logger = logging.getLogger(__name__)

DEFAULT_DICOM_STORE_WORKERS = 4
DEFAULT_DICOM_CALLING_AET = "XNATCTL"
DEFAULT_DICOM_PORT = 104


@dataclass
class DICOMStoreSummary:
    """Summary of a DICOM C-STORE operation."""

    total_files: int
    sent: int
    failed: int
    log_dir: Path
    workspace: Path
    success: bool


def _get_verification_sop_class() -> Any:
    """Get VerificationSOPClass with compatibility for pynetdicom versions."""
    from pynetdicom import sop_class as _sop_class

    verification_uid = "1.2.840.10008.1.1"
    return getattr(
        _sop_class,
        "VerificationSOPClass",
        getattr(_sop_class, "Verification", verification_uid),
    )


def _get_storage_contexts() -> list[Any]:
    """Get storage presentation contexts with version compatibility."""
    try:
        from pynetdicom import StoragePresentationContexts

        return list(StoragePresentationContexts)
    except ImportError:
        from pynetdicom import sop_class as _sc
        from pynetdicom.presentation import build_context

        uids = [getattr(_sc, name) for name in dir(_sc) if name.endswith("Storage")]
        return [build_context(uid) for uid in uids]


def _ensure_sop_uids(ds: Any) -> None:
    """Populate missing SOP UID attributes from file-meta.

    Args:
        ds: pydicom Dataset object.
    """
    if not getattr(ds, "SOPClassUID", None):
        uid = getattr(ds.file_meta, "MediaStorageSOPClassUID", None)
        if uid:
            ds.SOPClassUID = uid

    if not getattr(ds, "SOPInstanceUID", None):
        uid = getattr(ds.file_meta, "MediaStorageSOPInstanceUID", None)
        if uid:
            ds.SOPInstanceUID = uid


def _tls_kwargs(tls_context: ssl.SSLContext | None, host: str) -> dict[str, Any]:
    """Association kwargs for TLS, or nothing at all when plaintext.

    ``host`` is passed as the TLS ``server_hostname`` rather than the ``None``
    the pynetdicom examples use: with ``check_hostname`` on, omitting it means
    the certificate is never matched against the host being talked to, which
    removes most of the protection.
    """
    if tls_context is None:
        return {}
    return {"tls_args": (tls_context, host)}


def build_dicom_tls_context(
    ca_bundle: str | None = None,
    client_cert: str | None = None,
    client_key: str | None = None,
) -> ssl.SSLContext:
    """Build the TLS context for a DICOM association.

    Plain C-STORE puts pixel data and the patient identifiers attached to it on
    the wire in cleartext. DICOM's own answer is TLS, which pynetdicom supports
    through ``ae.associate(..., tls_args=...)``.

    There is deliberately no way to switch verification off. An "insecure TLS"
    mode is the worst of both worlds -- it looks encrypted in the command line
    and in the logs while accepting any certificate presented, so a
    man-in-the-middle reads the PHI anyway and nobody notices. A site that
    genuinely cannot verify certificates should send plaintext knowingly and
    see the notice that goes with it.

    Args:
        ca_bundle: PEM file of CAs to trust. Falls back to the system store.
        client_cert: Client certificate, for SCPs requiring mutual TLS.
        client_key: Its private key. May be omitted if the cert file holds both.

    Returns:
        A context with certificate and hostname verification enabled.

    Raises:
        UploadError: If the certificate material cannot be loaded.
    """
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca_bundle)
    # create_default_context already sets these; asserted rather than assigned
    # so a future edit that weakens them fails loudly here.
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True

    if client_cert:
        try:
            context.load_cert_chain(certfile=client_cert, keyfile=client_key)
        except (OSError, ssl.SSLError) as e:
            raise UploadError(
                f"Could not load the DICOM TLS client certificate: {e}",
                client_cert,
                {"client_key": client_key},
            ) from e

    return context


def _c_echo(
    host: str,
    port: int,
    calling_aet: str,
    called_aet: str,
    tls_context: ssl.SSLContext | None = None,
) -> bool:
    """Send a C-ECHO to verify connectivity and AE titles.

    Args:
        host: DICOM SCP host.
        port: DICOM SCP port.
        calling_aet: Our AE title.
        called_aet: Remote AE title.
        tls_context: When given, the association is encrypted.

    Returns:
        True if C-ECHO succeeded.
    """
    from pynetdicom import AE

    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(_get_verification_sop_class())

    assoc = ae.associate(host, port, ae_title=called_aet, **_tls_kwargs(tls_context, host))
    if not assoc.is_established:
        return False

    status = assoc.send_c_echo()
    assoc.release()

    return bool(status and status.Status == 0x0000)


def _send_dicom_batch(
    batch_id: str,
    files: list[Path],
    host: str,
    port: int,
    calling_aet: str,
    called_aet: str,
    log_dir: Path,
    tls_context: ssl.SSLContext | None = None,
) -> tuple[int, int]:
    """Send a batch of DICOM files over a single association.

    Args:
        batch_id: Identifier for this batch (for logging).
        files: List of DICOM file paths.
        host: DICOM SCP host.
        port: DICOM SCP port.
        calling_aet: Our AE title.
        called_aet: Remote AE title.
        log_dir: Directory for batch log files.
        tls_context: When given, the association is encrypted.

    Returns:
        Tuple of (sent_count, failed_count).
    """
    import pydicom
    from pydicom.errors import InvalidDicomError
    from pynetdicom import AE

    sent = failed = 0
    log_path = log_dir / f"{batch_id}.log"

    with log_path.open("w") as log:
        ae = AE(ae_title=calling_aet)
        ae.requested_contexts = _get_storage_contexts()
        ae.add_requested_context("1.3.12.2.1107.5.9.1")

        assoc = ae.associate(host, port, ae_title=called_aet, **_tls_kwargs(tls_context, host))
        if not assoc.is_established:
            log.write("Association rejected/aborted\n")
            return sent, len(files)

        for file_path in files:
            try:
                ds = pydicom.dcmread(file_path, force=True)
            except InvalidDicomError:
                failed += 1
                log.write(f"Skip non-DICOM {file_path}\n")
                continue

            _ensure_sop_uids(ds)

            try:
                status = assoc.send_c_store(ds)
            except Exception as e:  # noqa: BLE001  # per-file C-STORE isolation: pynetdicom/network failures are non-enumerable
                failed += 1
                log.write(f"Store error {file_path}: {type(e).__name__}: {e}\n")
                continue

            if status and status.Status == 0x0000:
                sent += 1
            else:
                failed += 1
                status_hex = hex(status.Status) if status else "0x0000"
                log.write(f"Failed {file_path} status {status_hex}\n")

        assoc.release()

    return sent, failed


def upload_dicom_store(
    dicom_root: Path,
    host: str,
    called_aet: str,
    *,
    port: int = DEFAULT_DICOM_PORT,
    calling_aet: str = DEFAULT_DICOM_CALLING_AET,
    workers: int = DEFAULT_DICOM_STORE_WORKERS,
    cleanup: bool = True,
    tls: bool = False,
    tls_ca_bundle: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
) -> DICOMStoreSummary:
    """Send DICOM files to an SCP using C-STORE.

    This function:
    1. Verifies connectivity with C-ECHO
    2. Collects DICOM files from the root directory
    3. Splits files into batches for parallel associations
    4. Sends files using multiple concurrent C-STORE associations

    Args:
        dicom_root: Directory containing DICOM files.
        host: DICOM SCP host.
        called_aet: Remote AE title.
        port: DICOM SCP port (default: 104).
        calling_aet: Our AE title (default: XNATCTL).
        workers: Number of parallel associations (default: 4).
        cleanup: Remove temporary workspace on completion (default: True).
        tls: Encrypt the associations. Off by default, which matches the
            DICOM standard's own default and the many deployments that run
            C-STORE inside a trusted VLAN -- but see the notice logged when
            it is off, because the alternative is PHI in cleartext.
        tls_ca_bundle: PEM file of CAs to trust (default: system store).
        tls_cert: Client certificate, for SCPs requiring mutual TLS.
        tls_key: Its private key.

    Returns:
        DICOMStoreSummary with results.

    Raises:
        ValueError: If dicom_root is not a directory.
        RuntimeError: If C-ECHO fails or no DICOM files found.
        UploadError: If TLS material is requested but cannot be loaded.
    """
    tls_context = _setup_tls_and_log(tls, tls_ca_bundle, tls_cert, tls_key, host, port)

    if not dicom_root.exists() or not dicom_root.is_dir():
        raise ValueError(f"dicom_root is not a directory: {dicom_root}")

    workspace = Path(tempfile.mkdtemp(prefix="xnatctl_dicom_store_"))
    log_dir = workspace / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Shared with _run_batches_in_parallel so the finally's cleanup condition
    # sees partial failure tallies even if a batch worker raises mid-loop.
    tally = _StoreTally()

    try:
        _preflight_c_echo(host, port, calling_aet, called_aet, tls_context)

        files = collect_dicom_files(dicom_root)
        if not files:
            raise RuntimeError(f"No DICOM files found in {dicom_root}")

        batches = split_into_n_batches(files, workers)
        logger.info(
            "Discovered %d files, using %d parallel associations",
            len(files),
            len(batches),
        )

        _run_batches_in_parallel(
            batches, host, port, calling_aet, called_aet, log_dir, tls_context, tally
        )

        return DICOMStoreSummary(
            total_files=len(files),
            sent=tally.sent,
            failed=tally.failed,
            log_dir=log_dir,
            workspace=workspace,
            success=tally.failed == 0,
        )

    finally:
        if cleanup and tally.failed == 0:
            shutil.rmtree(workspace, ignore_errors=True)


def _setup_tls_and_log(
    tls: bool,
    tls_ca_bundle: str | None,
    tls_cert: str | None,
    tls_key: str | None,
    host: str,
    port: int,
) -> ssl.SSLContext | None:
    """Build the TLS context if requested and log the encryption decision.

    Args:
        tls: Encrypt the associations.
        tls_ca_bundle: PEM file of CAs to trust (default: system store).
        tls_cert: Client certificate, for SCPs requiring mutual TLS.
        tls_key: Its private key.
        host: DICOM SCP host.
        port: DICOM SCP port.

    Returns:
        The TLS context, or None for a cleartext association.

    Raises:
        UploadError: If TLS material is requested but cannot be loaded.
    """
    tls_context = build_dicom_tls_context(tls_ca_bundle, tls_cert, tls_key) if tls else None
    if tls_context is None:
        # Informational, not alarming: plenty of sites run C-STORE on a
        # segregated network on purpose. But it should be a decision
        # someone made, not one they never knew they had.
        logger.info(
            "DICOM C-STORE to %s:%s is unencrypted; use --tls if the server supports it",
            host,
            port,
        )
    else:
        logger.info("DICOM C-STORE to %s:%s is TLS-encrypted", host, port)
    return tls_context


def _preflight_c_echo(
    host: str,
    port: int,
    calling_aet: str,
    called_aet: str,
    tls_context: ssl.SSLContext | None,
) -> None:
    """Verify SCP connectivity with C-ECHO before any C-STORE starts.

    Args:
        host: DICOM SCP host.
        port: DICOM SCP port.
        calling_aet: Our AE title.
        called_aet: Remote AE title.
        tls_context: When given, the association is encrypted.

    Raises:
        RuntimeError: If the C-ECHO fails.
    """
    logger.info(
        "Pre-flight C-ECHO %s -> %s @ %s:%s",
        calling_aet,
        called_aet,
        host,
        port,
    )
    if not _c_echo(host, port, calling_aet, called_aet, tls_context):
        raise RuntimeError(
            f"C-ECHO failed - check host/port/AET settings "
            f"(host={host}, port={port}, called_aet={called_aet})"
        )


@dataclass
class _StoreTally:
    """Mutable sent/failed counters shared across the batch loop.

    Mutated in place per completed batch so the caller's workspace-cleanup
    condition sees partial tallies even when a batch worker raises.
    """

    sent: int = 0
    failed: int = 0


def _run_batches_in_parallel(
    batches: list[list[Path]],
    host: str,
    port: int,
    calling_aet: str,
    called_aet: str,
    log_dir: Path,
    tls_context: ssl.SSLContext | None,
    tally: _StoreTally,
) -> None:
    """Send every batch over its own parallel C-STORE association.

    Args:
        batches: Per-association file batches.
        host: DICOM SCP host.
        port: DICOM SCP port.
        calling_aet: Our AE title.
        called_aet: Remote AE title.
        log_dir: Directory for batch log files.
        tls_context: When given, associations are encrypted.
        tally: Counters mutated in place per completed batch.
    """
    with cancellable_pool(len(batches)) as (pool, _cstore_token):
        futures = {
            pool.submit(
                _send_dicom_batch,
                f"{i:03d}",
                batch,
                host,
                port,
                calling_aet,
                called_aet,
                log_dir,
                tls_context,
            ): i
            for i, batch in enumerate(batches)
        }

        for future in as_completed(futures):
            batch_idx = futures[future]
            sent, failed = future.result()
            tally.sent += sent
            tally.failed += failed
            logger.info(
                "Batch %03d complete: %d sent, %d failed",
                batch_idx,
                sent,
                failed,
            )
