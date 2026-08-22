Services API
============

The services package provides high-level Python interfaces for XNAT REST API
operations. Each service class encapsulates operations for a specific resource
type (projects, subjects, sessions, etc.).

Covered by semver: the service classes listed in ``xnatctl.__all__``
(``xnatctl.ProjectService``, ``xnatctl.SessionService``, ...). A service class
that exists but is not in that list, or one reached by importing its module
directly (``xnatctl.services.projects``) rather than the top-level name, is
Provisional and may move between minor releases. See :doc:`../stability` for
the exact boundary.

Service Layer Architecture
---------------------------

**Design Pattern:**

All services follow the service layer pattern:

1. **Instantiate with authenticated client**
2. **Call service methods** (list, get, create, update, delete)
3. **Receive a typed model for the core resource services** (projects,
   subjects, sessions, scans, resources) **or a plain dict/list for
   prearchive, pipelines, and admin**, which do not wrap results in
   Pydantic models

**Benefits:**

- Type-safe operations with Pydantic models, for the services that return them
- Automatic retry and error handling
- Consistent pagination and filtering
- Clean separation of concerns

**Common Usage Pattern:**

Build a client from a config profile and reach a service through the bound,
cached accessor on the client. ``from_profile`` runs the same credential
resolution the CLI does (environment variables over profile config, cached or
``XNAT_TOKEN`` session token, ``auto_reauth`` on), and the context manager logs
in when a password is available and no token is cached yet.

.. code-block:: python

   from pathlib import Path

   import xnatctl

   with xnatctl.XNATClient.from_profile("prod") as client:
       projects = client.projects.list()
       project = client.projects.get("MYPROJECT")
       client.downloads.download_resource("XNAT_E00001", "DICOM", Path("./out"))

Each resource type has an accessor: ``client.projects``, ``client.subjects``,
``client.sessions``, ``client.scans``, ``client.resources``,
``client.prearchive``, ``client.pipelines``, ``client.admin``,
``client.hierarchy``, ``client.downloads``, ``client.uploads``, and
``client.exam_uploads``. To point at a server without a saved profile,
construct :class:`~xnatctl.core.client.XNATClient` directly with ``base_url``
and credentials.

Most services return typed Pydantic models -- projects, subjects, sessions,
scans, and resources. A few return plain ``dict``/``list`` results instead
(raw or lightly normalized XNAT JSON, never Pydantic models): prearchive,
pipelines, and admin. Each section below notes which
applies.

Error Contract
--------------

Download and upload service methods split into two kinds, and they report
failure differently:

**Single-target operations raise.** A method that downloads or uploads one thing
(:meth:`~xnatctl.services.downloads.DownloadService.download_resource`,
:meth:`~xnatctl.services.upload.UploadService.upload_resource`, and
:meth:`~xnatctl.services.downloads.DownloadService.download_scan` when a
resource label is given -- with ``resource=None`` it delegates to the batch
``download_scans`` and returns that summary) returns its summary only on
success. On failure it raises: a typed
:class:`~xnatctl.core.exceptions.XNATCtlError` from the client layer
(:class:`~xnatctl.core.exceptions.SessionExpiredError`,
:class:`~xnatctl.core.exceptions.PermissionDeniedError`,
:class:`~xnatctl.core.exceptions.ResourceNotFoundError`, ...) passes through
untouched, and any other exception (``OSError``, a corrupt archive, an
unexpected error) is wrapped as
:class:`~xnatctl.core.exceptions.DownloadError` or
:class:`~xnatctl.core.exceptions.UploadError` with the original exception as
``__cause__``. (``upload_resource`` raises a plain ``FileNotFoundError`` for a
missing source path -- a local input error, not an upload failure.) A caller
therefore distinguishes an expired session from a full disk by exception type
rather than by parsing a string.

.. code-block:: python

   from xnatctl.core.exceptions import DownloadError, SessionExpiredError

   try:
       summary = client.downloads.download_resource("XNAT_E00001", "DICOM", out)
   except SessionExpiredError:
       ...  # re-authenticate and retry
   except DownloadError as exc:
       ...  # exc.__cause__ carries the underlying failure

**Batch operations return a summary.** A method that fans out over many items
(``download_scans``, the ``upload_dicom_*`` family)
keeps returning a :class:`~xnatctl.models.progress.DownloadSummary` or
:class:`~xnatctl.models.progress.UploadSummary` so a partial result stays
inspectable. (``download_session_fast`` is also a batch operation but reports
through its own ``DownloadOutcome``, which has no ``raise_for_status``.) Both summaries expose ``raise_for_status()``, mirroring
``httpx.Response.raise_for_status()``: a no-op on success, and a
:class:`~xnatctl.core.exceptions.BatchOperationError` (carrying the succeeded
and failed counts and the per-item error list) when the batch did not fully
succeed.

.. code-block:: python

   summary = client.downloads.download_scans("XNAT_E00001", ["1", "2"], out)
   summary.raise_for_status()  # BatchOperationError if any scan failed

Base Service
------------

Foundation class providing common HTTP method wrappers and pagination utilities.

.. autoclass:: xnatctl.services.base.BaseService
   :members:
   :undoc-members:
   :show-inheritance:

Hierarchy Service
------------------

Builds and resolves project/subject/experiment/scan/resource paths, and
resolves a subject or experiment reference (accession ID or label) to its
canonical IDs. The resource-scoped services above use this internally; it is
also useful directly when a caller already has a
:mod:`~xnatctl.models.hierarchy` ref and wants the same path-building or
resolution logic without going through a resource-specific service.

.. autoclass:: xnatctl.services.hierarchy.HierarchyService
   :members:
   :undoc-members:
   :show-inheritance:

Projects Service
----------------

Manage XNAT projects: list, inspect, create, and configure.

**Hierarchy:**

.. code-block:: text

   Project (top-level)
   └── Subject
       └── Session
           └── Scan
               └── Resource

**Common Operations:**

.. code-block:: python

   import xnatctl

   with xnatctl.XNATClient.from_profile("prod") as client:
       service = client.projects

       # List all accessible projects
       projects = service.list()

       # Get specific project details
       project = service.get("MYPROJECT")
       print(f"Name: {project.name}")
       print(f"PI: {project.pi}")
       print(f"Subjects: {project.subject_count}")

       # Create new project
       new_project = service.create(
           project_id="NEWPROJECT",
           name="New Project",
           pi_firstname="Jane",
           pi_lastname="Smith",
           accessibility="private"
       )

.. autoclass:: xnatctl.services.projects.ProjectService
   :members:
   :undoc-members:
   :show-inheritance:

Subjects Service
----------------

Manage subjects (participants) within projects.

**Subject Lifecycle:**

1. Create subject in a project
2. Add imaging sessions to subject
3. Optionally rename or delete subject

**Common Operations:**

.. code-block:: python

   from xnatctl.services.subjects import SubjectService

   service = SubjectService(client)

   # List subjects in a project
   subjects = service.list(project="MYPROJECT")

   # Get specific subject
   subject = service.get(
       subject_id="SUB001",
       project="MYPROJECT"
   )

   # Rename subject
   service.rename(
       subject_id="SUB001",
       new_label="PARTICIPANT001",
       project="MYPROJECT"
   )

   # Delete subject (WARNING: destructive)
   service.delete(
       subject_id="SUB001",
       project="MYPROJECT"
   )

.. autoclass:: xnatctl.services.subjects.SubjectService
   :members:
   :undoc-members:
   :show-inheritance:

Sessions Service
----------------

Manage imaging sessions (experiments) containing scan data.

**Session Types:**

Sessions represent data collection events. Common types include:

- Baseline scans
- Follow-up visits
- Longitudinal timepoints
- Multi-modal acquisitions

**Filtering by Modality:**

.. code-block:: python

   from xnatctl.services.sessions import SessionService

   service = SessionService(client)

   # List all MR sessions in a project
   mr_sessions = service.list(
       project="MYPROJECT",
       modality="MR"
   )

   # List sessions for a specific subject
   subject_sessions = service.list(
       project="MYPROJECT",
       subject="SUB001"
   )

   # Get session details
   session = service.get(
       session_id="XNAT_E00001",
       project="MYPROJECT"
   )

   print(f"Date: {session.session_date}")
   print(f"Scans: {session.scan_count}")
   print(f"Scanner: {session.scanner}")

.. autoclass:: xnatctl.services.sessions.SessionService
   :members:
   :undoc-members:
   :show-inheritance:

Scans Service
-------------

Manage individual imaging scans (series) within sessions.

**Scan Operations:**

.. code-block:: python

   from xnatctl.services.scans import ScanService

   service = ScanService(client)

   # List scans in a session
   scans = service.list(
       session_id="SESSION01",
       project="MYPROJECT"
   )

   for scan in scans:
       print(f"{scan.id}: {scan.type}")
       print(f"  Quality: {scan.quality}")
       print(f"  Files: {scan.file_count}")

   # Delete a scan
   service.delete(
       session_id="SESSION01",
       scan_id="1",
       project="MYPROJECT"
   )

.. autoclass:: xnatctl.services.scans.ScanService
   :members:
   :undoc-members:
   :show-inheritance:

Resources Service
-----------------

Manage file resources attached to XNAT objects.

**Resource Types:**

Common resource categories:

- ``DICOM`` - Raw DICOM files
- ``NIFTI`` - Converted NIfTI volumes
- ``SNAPSHOTS`` - Preview images
- ``QC`` - Quality control reports
- ``PROCESSED`` - Analysis outputs

**Upload and Download:**

``ResourceService`` handles listing and single-file upload; downloading a
resource's files goes through
:class:`~xnatctl.services.downloads.DownloadService` instead (see
`Downloads Service`_ below).

.. code-block:: python

   from pathlib import Path

   from xnatctl.services.resources import ResourceService

   service = ResourceService(client)

   # List resources
   resources = service.list(
       session_id="SESSION01",
       project="MYPROJECT"
   )

   # Upload a file to a resource
   service.upload_file(
       session_id="SESSION01",
       resource_label="PROCESSED",
       file_path=Path("analysis.nii.gz"),
       project="MYPROJECT"
   )

   # Download a resource's files
   client.downloads.download_resource(
       session_id="SESSION01",
       resource_label="DICOM",
       output_dir=Path("./downloads/"),
       project="MYPROJECT"
   )

.. autoclass:: xnatctl.services.resources.ResourceService
   :members:
   :undoc-members:
   :show-inheritance:

Downloads Service
-----------------

Parallel, atomic download operations.

**Features:**

- Multi-threaded parallel per-scan downloads
- Atomic writes: data streams to a temporary ``.part`` file that is renamed
  into place only after the byte count matches the server's Content-Length
- Automatic retry on transient failures (through the client's retry ladder)
- Progress via caller-supplied callbacks; rendering stays with the caller

**Parallel Download Example:**

.. code-block:: python

   from pathlib import Path

   from xnatctl.services.downloads import DownloadService

   service = DownloadService(client)

   # Download every scan of a session with 8 parallel workers
   outcome = service.download_session_fast(
       session_project="MYPROJECT",
       subject="SUBJ01",
       resolved_session_id="XNAT_E00001",
       session_dir=Path("./data/SESSION01"),
       workers=8,
   )
   if outcome.failed:
       raise SystemExit(f"{len(outcome.failed)} scan downloads failed")

.. automodule:: xnatctl.services.downloads
   :members:
   :undoc-members:

Uploads Service
---------------

High-performance parallel upload operations for DICOM and file resources.

**Upload Strategies:**

``session upload`` supports three archive modes, selected with ``--mode``:

1. **TAR batching** (default): files are packed into parallel TAR archives
   and sent through the REST import service
2. **ZIP batching**: same batched-archive path, using ZIP instead of TAR
3. **Gradual**: individual files streamed one at a time through the REST API,
   for servers where batched-archive import is unreliable

``session upload-dicom`` is a separate command that sends files over the
DICOM C-STORE network protocol instead of REST.

**Features:**

- Multi-threaded parallel uploads
- Automatic retry with exponential backoff
- Progress tracking
- Sequential fallback for failed files
- Thread-local HTTP clients for stability

**Parallel Upload Example:**

.. code-block:: python

   from pathlib import Path

   from xnatctl.services.upload import UploadService

   service = UploadService(client)

   # Upload a directory of DICOM files with parallel REST batching (TAR)
   service.upload_dicom_parallel(
       source_dir=Path("/path/to/dicom"),
       project="MYPROJECT",
       subject="SUB001",
       session="SESSION01",
       upload_workers=8,
   )

**Error Handling:**

Failed uploads are automatically retried at lower concurrency, with a final
sequential retry pass to maximize completion rate on flaky networks.

.. automodule:: xnatctl.services.upload
   :members:
   :undoc-members:

Exam Upload Service
-------------------

Orchestrates ``session upload-exam``: it uploads the exam root's DICOM, waits
for the session to archive, then attaches the top-level resource directories and
misc files. The Click command keeps only option resolution and output rendering;
the service returns an :class:`~xnatctl.services.exam_upload.ExamUploadResult`
whose ``to_json_dict`` is the ``-o json`` compatibility contract.

.. automodule:: xnatctl.services.exam_upload
   :members:
   :undoc-members:

Import Service Requests
-----------------------

Single construction point for ``POST /data/services/import`` querystrings,
shared by the archive, gradual-DICOM, and cross-server transfer paths.

.. automodule:: xnatctl.services.import_service
   :members:
   :undoc-members:

Prearchive Service
------------------

Manage the XNAT prearchive staging area for reviewing uploads before archiving.

**Prearchive Workflow:**

1. Upload DICOM files to prearchive
2. Review session metadata and quality
3. Archive to final location or delete

**Operations:**

``PrearchiveService`` returns plain dicts, not typed models -- index with
``session["name"]``, not attribute access.

.. code-block:: python

   from xnatctl.services.prearchive import PrearchiveService

   service = PrearchiveService(client)

   # List prearchive sessions
   sessions = service.list(project="MYPROJECT")

   for session in sessions:
       print(f"{session['name']} - {session['status']}")
       print(f"  Subject: {session.get('subject')}")
       print(f"  Scan date: {session.get('scan_date')}")

   # Archive session from prearchive
   service.archive(
       project="MYPROJECT",
       timestamp="20240101_120000",
       session_name="SESSION01"
   )

   # Delete prearchive session
   service.delete(
       project="MYPROJECT",
       timestamp="20240101_120000",
       session_name="SESSION01"
   )

.. autoclass:: xnatctl.services.prearchive.PrearchiveService
   :members:
   :undoc-members:
   :show-inheritance:

Pipelines Service
-----------------

Execute and monitor XNAT processing pipelines.

**Pipeline Operations:**

.. code-block:: python

   from xnatctl.services.pipelines import PipelineService

   service = PipelineService(client)

   # List available pipelines
   pipelines = service.list(project="MYPROJECT")

   # Run pipeline on a session (returns a dict, e.g. {"job_id": ..., ...})
   run_result = service.run(
       pipeline_name="DicomToNifti",
       experiment_id="XNAT_E00001",
       project="MYPROJECT"
   )

   # Check pipeline job status
   status = service.status(job_id=run_result["job_id"])
   print(f"Status: {status}")

.. autoclass:: xnatctl.services.pipelines.PipelineService
   :members:
   :undoc-members:
   :show-inheritance:

Admin Service
-------------

Administrative operations including catalog refresh, user management, and audit logs.

**Admin Operations:**

.. code-block:: python

   from xnatctl.services.admin import AdminService

   service = AdminService(client)

   # Refresh project catalogs
   service.refresh_catalogs(project="MYPROJECT")

   # List users
   users = service.list_users()

   # View audit log
   logs = service.audit_log(
       project="MYPROJECT",
       limit=100
   )

.. autoclass:: xnatctl.services.admin.AdminService
   :members:
   :undoc-members:
   :show-inheritance:
