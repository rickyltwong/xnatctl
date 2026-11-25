"""Single-archive upload through the XNAT import service.

The archive path shared by ``session upload`` and the parallel batch
transport in :mod:`~xnatctl.services.upload.rest_batch`:
:func:`upload_single_archive` returns a result the batch path can tally,
and :func:`upload_archive_or_raise` maps a failure to the typed exception
taxonomy for the single-archive CLI path.
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import httpx

from xnatctl.core.cancellation import NULL_TOKEN, CancellationToken
from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import (
    NetworkError,
    PermissionDeniedError,
    ResourceNotFoundError,
    RetryExhaustedError,
    SessionExpiredError,
    UploadError,
)
from xnatctl.core.exceptions import RequestTimeoutError as XNATTimeoutError
from xnatctl.core.logging import FILE_ONLY_ATTR
from xnatctl.core.retry import RETRYABLE_STATUS_CODES, UPLOAD_MAX_RETRIES, upload_with_retry
from xnatctl.core.server_version import MIN_VERSION_DIRECT_ARCHIVE, require_server_version
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS, build_httpx_timeout
from xnatctl.services.import_service import IMPORT_ENDPOINT, build_import_params

from .archives import _maybe_zip_to_tar
from .shared import SessionRefresher, session_cookie_header

logger = logging.getLogger(__name__)


class ArchiveUploadResult(NamedTuple):
    """Outcome of one archive upload, with enough context to classify it.

    ``status_code`` and ``exception`` exist so the CLI can map a failure to the
    documented exit-code taxonomy (auth 3, network 4, permission 6) instead of
    flattening everything to a string and exiting 1.
    """

    success: bool
    error: str
    status_code: int | None = None
    exception: BaseException | None = None


@dataclass
class _AuthAttempt:
    failure: ArchiveUploadResult | None = None
    session_token: str | None = None
    cookies: dict[str, str] | None = None
    created_session: bool = False


def _resolve_batch_credentials(
    client: httpx.Client,
    username: str | None,
    password: str | None,
    session_token: str | None,
) -> _AuthAttempt:
    if session_token:
        return _AuthAttempt(session_token=session_token, cookies={"JSESSIONID": session_token})

    if not username or not password:
        return _AuthAttempt(
            failure=ArchiveUploadResult(False, "Authentication failed: missing credentials")
        )

    auth_resp = client.post(
        "/data/JSESSION",
        auth=(str(username), str(password)),
    )
    if auth_resp.status_code != 200:
        return _AuthAttempt(
            failure=ArchiveUploadResult(
                False,
                f"Authentication failed: HTTP {auth_resp.status_code}",
                status_code=auth_resp.status_code,
            )
        )

    if "<html" in auth_resp.text.lower():
        return _AuthAttempt(
            failure=ArchiveUploadResult(False, "Authentication failed: invalid credentials")
        )

    token = auth_resp.text.strip()
    return _AuthAttempt(session_token=token, cookies={"JSESSIONID": token}, created_session=True)


def _upload_with_reauth(
    attempt_with: Callable[[dict[str, str]], httpx.Response],
    *,
    archive_name: str,
    session_token: str | None,
    cookies: dict[str, str],
    session_refresher: SessionRefresher | None,
    cancel_token: CancellationToken,
) -> httpx.Response:
    resp = upload_with_retry(
        lambda: attempt_with(cookies),
        label=f"batch {archive_name}",
        cancel_token=cancel_token,
    )

    if resp.status_code == 401 and session_refresher is not None:
        fresh = session_refresher.refresh(session_token)
        if fresh and fresh != session_token:
            logger.info(
                "Session expired mid-upload; retrying batch %s with a refreshed token",
                archive_name,
            )
            cookies["JSESSIONID"] = fresh
            resp = upload_with_retry(
                lambda: attempt_with(cookies),
                label=f"batch {archive_name} (after reauth)",
                cancel_token=cancel_token,
            )
    return resp


def _classify_import_response(resp: httpx.Response) -> ArchiveUploadResult:
    if resp.status_code == 200:
        return ArchiveUploadResult(True, "")
    if resp.status_code in (401, 403):
        return ArchiveUploadResult(
            False,
            "Authentication failed: invalid or expired session",
            status_code=resp.status_code,
        )
    return ArchiveUploadResult(
        False,
        f"HTTP {resp.status_code}: {resp.text[:500]}",
        status_code=resp.status_code,
    )


def upload_single_archive(
    *,
    xnat_client: XNATClient,
    username: str | None,
    password: str | None,
    session_token: str | None,
    verify_ssl: bool | ssl.SSLContext,
    timeout: int,
    archive_path: Path,
    project: str,
    subject: str,
    session: str,
    import_handler: str,
    ignore_unparsable: bool,
    overwrite: str,
    direct_archive: bool,
    cancel_token: CancellationToken = NULL_TOKEN,
    session_refresher: SessionRefresher | None = None,
) -> ArchiveUploadResult:
    """Upload a single archive file to XNAT.

    Creates a fresh httpx client for thread-safety in parallel execution.
    ``xnat_client`` is used only for the version-gate check below (its
    cached ``server_version``, safe under concurrent access) -- the actual
    upload always goes over a fresh per-call ``httpx.Client``, never
    ``xnat_client``'s own.

    That upload client's base URL is taken from ``xnat_client``, not passed
    in separately. A separate ``base_url`` argument would let the gate and
    the upload address different servers: a caller could hand in a 1.9
    client and a 1.7 target URL, satisfy the version check, and still send
    ``Direct-Archive=true`` to a server that does not support it. Deriving
    the URL from the same object the gate inspects makes that divergence
    unrepresentable rather than merely unlikely.

    This is one of the two canonical public entry points that can set
    ``Direct-Archive=true`` on the wire (the other is
    :class:`~xnatctl.services.upload.gradual.GradualUploadRun`); both gate
    here rather than relying solely on a higher wrapper, because both are
    re-exported for direct library use and a caller going straight to either
    must not be able to send a direct-archive import to an unsupported
    server ungated. ``upload_archive_or_raise`` and the parallel batch path
    also gate before this point, purely to fail fast before doing any local
    archive work -- by the time either reaches here, the version is already
    cached, so the check below costs no extra network round trip.

    A 401 mid-upload must not end the batch when credentials are available:
    XNAT evicts sessions when an account exceeds its concurrent-session
    limit -- routine when several workers share a service account -- so a
    long parallel upload would otherwise fail batch by batch against a
    server that was working perfectly. Refreshing through the shared
    *session_refresher* rather than logging in per batch matters: it
    serialises the reauth, so N workers hitting the same eviction do not
    answer it with N more logins.

    Returns:
        ArchiveUploadResult; on failure it carries the final HTTP status or the
        transport exception so callers can classify the error.

    Raises:
        UnsupportedServerVersionError: If ``direct_archive`` is set and the
            server is known to be older than
            :data:`~xnatctl.core.server_version.MIN_VERSION_DIRECT_ARCHIVE`.
    """
    if direct_archive:
        require_server_version(xnat_client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")

    base_url = xnat_client.base_url

    name = archive_path.name.lower()
    content_type = "application/zip" if name.endswith(".zip") else "application/x-tar"

    params = build_import_params(
        import_handler=import_handler,
        project=project,
        subject=subject,
        session=session,
        overwrite=overwrite,
        overwrite_files=True,
        quarantine=False,
        trigger_pipelines=True,
        rename=False,
        inbody=True,
        ignore_unparsable=ignore_unparsable,
        direct_archive=direct_archive,
    )

    with httpx.Client(
        base_url=base_url,
        timeout=build_httpx_timeout(timeout),
        verify=verify_ssl,
    ) as client:
        try:
            auth = _resolve_batch_credentials(client, username, password, session_token)
            if auth.failure is not None:
                return auth.failure
            session_token = auth.session_token
            cookies = auth.cookies or {}
            created_session = auth.created_session

            def _attempt_with(jar: dict[str, str]) -> httpx.Response:
                with archive_path.open("rb") as data:
                    return client.post(
                        IMPORT_ENDPOINT,
                        params=params,
                        headers={
                            "Content-Type": content_type,
                            **session_cookie_header(jar.get("JSESSIONID")),
                        },
                        content=data,
                    )

            try:
                resp = _upload_with_reauth(
                    _attempt_with,
                    archive_name=archive_path.name,
                    session_token=session_token,
                    cookies=cookies,
                    session_refresher=session_refresher,
                    cancel_token=cancel_token,
                )
            finally:
                if created_session:
                    try:
                        client.delete(
                            "/data/JSESSION",
                            headers=session_cookie_header(cookies.get("JSESSIONID")),
                        )
                    except httpx.HTTPError:
                        pass

            return _classify_import_response(resp)

        except httpx.ConnectTimeout as e:
            return ArchiveUploadResult(False, "Connection timed out; not retried", exception=e)
        except httpx.TimeoutException as e:
            return ArchiveUploadResult(False, "Upload timed out (after retries)", exception=e)
        except httpx.ConnectError as e:
            return ArchiveUploadResult(
                False, f"Connection failed (after retries): {e}", exception=e
            )
        except Exception as e:
            logger.exception(
                "Archive upload failed for %s", archive_path.name, extra={FILE_ONLY_ATTR: True}
            )
            return ArchiveUploadResult(False, str(e), exception=e)


def upload_archive_or_raise(
    client: XNATClient,
    archive_path: Path,
    project: str,
    subject: str,
    session: str,
    overwrite: str,
    direct_archive: bool,
    ignore_unparsable: bool,
    zip_to_tar: bool,
) -> None:
    """Upload one archive through :func:`upload_single_archive`, raising on failure.

    A thin classifier over :func:`upload_single_archive`: that function returns an
    :class:`ArchiveUploadResult` so the parallel batch path can tally failures,
    whereas the single-archive CLI path wants the failure mapped to a typed
    exception so ``@handle_errors`` keeps the documented exit-code taxonomy
    (auth 3, network 4, permission 6). The two are kept separate deliberately --
    one returns a result to be counted, one raises to stop a command.

    Deliberately not ``client.post`` wrapped in ``upload_with_retry``: that
    stacked two retry ladders, and because ``_request`` raises typed errors on
    4xx, ``upload_with_retry`` never saw a raw 400 response -- so the
    transient-vs-permanent 400 discrimination the import service needs was dead
    on this path.

    Raises:
        UnsupportedServerVersionError: If ``direct_archive`` is set and the
            server is known to be older than
            :data:`~xnatctl.core.server_version.MIN_VERSION_DIRECT_ARCHIVE`.
    """
    if direct_archive:
        require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")

    refresher = SessionRefresher(
        base_url=client.base_url,
        verify_ssl=client.httpx_verify(),
        token=client.session_token,
        username=client.username,
        password=client.password,
        owner=client,
    )

    with _maybe_zip_to_tar(archive_path, zip_to_tar) as upload_path:
        result = upload_single_archive(
            xnat_client=client,
            username=client.username,
            password=client.password,
            session_token=client.session_token,
            verify_ssl=client.httpx_verify(),
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            archive_path=upload_path,
            project=project,
            subject=subject,
            session=session,
            import_handler="DICOM-zip",
            ignore_unparsable=ignore_unparsable,
            overwrite=overwrite,
            direct_archive=direct_archive,
            session_refresher=refresher,
        )

    _raise_for_archive_result(
        result,
        base_url=client.base_url,
        archive_path=archive_path,
        project=project,
        subject=subject,
        session=session,
    )


def _raise_for_archive_result(
    result: ArchiveUploadResult,
    *,
    base_url: str,
    archive_path: Path,
    project: str,
    subject: str,
    session: str,
) -> None:
    if result.success:
        return
    if result.status_code == 401:
        raise SessionExpiredError(base_url)
    if result.status_code == 403:
        raise PermissionDeniedError(f"project {project}", "upload to", url=base_url)
    if result.status_code == 404:
        raise ResourceNotFoundError("Import destination", f"{project}/{subject}/{session}")
    if result.status_code in RETRYABLE_STATUS_CODES:
        raise RetryExhaustedError(
            f"upload {archive_path.name}",
            UPLOAD_MAX_RETRIES + 1,
            UploadError(result.error),
        )
    if isinstance(result.exception, httpx.TimeoutException):
        raise XNATTimeoutError(
            base_url,
            DEFAULT_HTTP_TIMEOUT_SECONDS,
            message=f"Upload of {archive_path.name} failed: {result.error}",
        )
    if isinstance(result.exception, httpx.TransportError):
        raise NetworkError(base_url, cause=result.error)
    raise UploadError(
        f"Upload of {archive_path.name} failed: {result.error}",
        file_path=str(archive_path),
    )
