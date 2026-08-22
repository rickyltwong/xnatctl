"""Single construction point for ``POST /data/services/import`` requests.

Every import call in the package -- archive batch uploads, gradual-DICOM
per-file uploads, and cross-server transfer imports -- builds its querystring
here, so the wire conventions live in one place instead of drifting across
call sites.

Two entity-key conventions exist because XNAT's Importer accepts both and
each upload path shipped (and was verified against real servers) with one of
them:

* ``session``: ``project``/``subject``/``session`` -- the archive upload path.
* ``experiment``: ``PROJECT_ID``/``SUBJECT_ID``/``EXPT_LABEL`` -- the
  gradual-DICOM and transfer paths.

Converging on one convention is possible but is a wire-behavior change; do it
deliberately with server testing, not as a refactor side effect.
"""

from __future__ import annotations

from typing import Literal

from xnatctl.core.validation import validate_xnat_label

IMPORT_ENDPOINT = "/data/services/import"


def archive_destination_params(project: str, direct_archive: bool) -> dict[str, str]:
    """Return the querystring keys that route a POST /data/services/import.

    * Direct-archive path: ``Direct-Archive=true`` — handled by the
      ``DICOM-zip`` and ``gradual-DICOM`` import handlers; bypasses the
      prearchive and writes straight to the project archive.
    * Prearchive path: ``dest=/prearchive/projects/{project}`` — the
      documented destination form. ``Direct-Archive=false`` alone is
      equivalent to "use standard upload mechanism"; we prefer the
      explicit ``dest`` because it is self-describing and matches the
      ``PrearchiveService`` pattern used elsewhere in this repo.

    ``dest`` is a query-parameter *value* (httpx percent-encodes the whole
    querystring), so an unvalidated ``project`` cannot redirect the request
    the way an unquoted URL *path* segment could -- but ``project`` is still
    validated here for airtightness, and to fail with a clear error at
    construction rather than a confusing 400 from the import service.

    Caveat: neither form can prevent a *project-configured* auto-archive.
    XNAT's ``prearchive_code`` on the project (0=manual, 4/5=auto) is the
    authoritative switch. When a project has auto-archive enabled, a
    session uploaded via either of these paths will land in prearchive
    momentarily then be auto-archived by the server. To force
    prearchive-only behaviour, the project's prearchive setting must be
    changed to "Leave in prearchive" (prearchive_code=0). There is no
    per-upload import-service override for this on XNAT 1.8+.
    """
    if direct_archive:
        return {"Direct-Archive": "true"}
    project = validate_xnat_label(project, "project")
    return {"dest": f"/prearchive/projects/{project}"}


def build_import_params(
    *,
    import_handler: str,
    project: str,
    subject: str,
    session: str,
    entity_keys: Literal["session", "experiment"] = "session",
    overwrite: str | None = None,
    overwrite_files: bool | None = None,
    quarantine: bool | None = None,
    trigger_pipelines: bool | None = None,
    rename: bool | None = None,
    inbody: bool = False,
    ignore_unparsable: bool | None = None,
    direct_archive: bool | None = None,
    destination: str | None = None,
) -> dict[str, str]:
    """Build the querystring for a ``POST /data/services/import``.

    ``None`` means "omit the key entirely" -- XNAT treats an absent key and an
    explicit default differently in places, so callers state exactly what they
    send.

    Args:
        import_handler: XNAT import handler (``DICOM-zip``, ``gradual-DICOM``).
        project: Target project ID.
        subject: Target subject label.
        session: Target session/experiment label.
        entity_keys: Which key convention routes the entity IDs (see module
            docstring).
        overwrite: XNAT overwrite mode (``none``/``append``/``delete``).
        overwrite_files: Whether existing files may be replaced.
        quarantine: Whether imported data lands in quarantine.
        trigger_pipelines: Whether the import fires autorun pipelines.
        rename: Whether XNAT may rename the session on collision.
        inbody: True when the archive is the raw request body rather than a
            multipart upload.
        ignore_unparsable: Whether non-DICOM files in the archive are skipped
            instead of failing the import.
        direct_archive: Routes via :func:`archive_destination_params`
            (direct-archive vs prearchive). ``None`` emits no routing keys.
        destination: Literal ``destination`` key (the transfer path sends
            ``/archive``). Distinct from ``direct_archive``: this is the older
            destination form that predates the ``Direct-Archive`` flag.

    Returns:
        Querystring parameters for the import POST.
    """

    def flag(value: bool) -> str:
        return "true" if value else "false"

    params: dict[str, str] = {"import-handler": import_handler}
    if ignore_unparsable is not None:
        params["Ignore-Unparsable"] = flag(ignore_unparsable)
    if entity_keys == "session":
        params.update({"project": project, "subject": subject, "session": session})
    else:
        params.update({"PROJECT_ID": project, "SUBJECT_ID": subject, "EXPT_LABEL": session})
    if overwrite is not None:
        params["overwrite"] = overwrite
    if overwrite_files is not None:
        params["overwrite_files"] = flag(overwrite_files)
    if quarantine is not None:
        params["quarantine"] = flag(quarantine)
    if trigger_pipelines is not None:
        params["triggerPipelines"] = flag(trigger_pipelines)
    if rename is not None:
        params["rename"] = flag(rename)
    if inbody:
        params["inbody"] = "true"
    if destination is not None:
        params["destination"] = destination
    if direct_archive is not None:
        params.update(archive_destination_params(project, direct_archive))
    return params
