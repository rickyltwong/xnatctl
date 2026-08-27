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
- Consistent filtering and result limits
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

Foundation class providing common HTTP method wrappers.

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
   :exclude-members: DownloadService

.. autoclass:: xnatctl.services.downloads.DownloadService
   :members:
   :undoc-members:
   :inherited-members: object

.. note::

   ``DownloadService`` composes its behaviour from private mixins, so its
   methods are inherited rather than declared on the class itself, and
   without ``:inherited-members:`` it renders with no methods at all. It
   gets its own ``autoclass`` rather than riding the ``automodule`` above
   because that option is module-wide: applied there it would also pull
   ``count``/``index`` onto every ``NamedTuple`` the module exports. The
   ``object`` argument stops the walk at ``object``, keeping the mixin and
   ``BaseService`` methods while dropping the ones every class has.

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
``session["name"]``, not attribute access. ``get_routing_code``/
``set_routing_mode`` manage a project's prearchive-routing setting (manual
review vs. auto-archive on upload); the three valid modes -- ``manual``
(code 0), ``auto-archive`` (code 4), ``auto-archive-overwrite`` (code 5) --
were read out of ``org.nrg.framework.constants.PrearchiveCode``'s static
initializer via ``javap`` against a running XNAT 1.9.2.1 server. The server
does not validate what gets written here -- verified live, any integer is
accepted and stored silently -- so ``set_routing_mode`` only accepts the
three mode names, not a raw code.

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

   # Get or set a project's prearchive routing mode
   code = service.get_routing_code("MYPROJECT")  # e.g. 4
   service.set_routing_mode("MYPROJECT", "auto-archive")

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

Command Service
----------------

Inspect and manage XNAT Container Service command and wrapper
registrations: list/show/create/update/delete commands, and
enable/disable/configure wrappers. Every method returns plain
``dict``/``list[dict]`` (never Pydantic models): Container Service JSON
shapes are plugin-version-dependent, since the Container Service ships as a
separate plugin JAR that versions independently of XNAT core. Verified live
against XNAT 1.9.2.1 + Container Service 3.7.2. A command's wrappers are
embedded under its ``xnat`` key -- there is no server-side wrapper-listing
endpoint in this plugin version, so ``list_wrappers()`` derives the flat
wrapper list client-side by walking every command's ``xnat`` array.
``update_command()`` is a full replace (``POST /xapi/commands/{id}`` is the
only route this plugin version offers -- there is no ``PUT``): omitting
``xnat`` from the payload wipes every wrapper on the command. Both
``update_command()`` and ``delete_command()`` check the command exists
first (via ``get_command()``) rather than trust the server's response for a
bad ID -- an unknown ID answers 500 (not 404) on update, and DELETE answers
204 even for an ID that never existed.

.. code-block:: python

   from xnatctl.services.commands import CommandService

   service = CommandService(client)

   # List registered commands
   commands = service.list_commands()

   # List one command's wrappers (derived from its "xnat" array)
   wrappers = service.list_wrappers(command_id=12)

   # Resolve a wrapper by numeric ID or name (used by ContainerService.launch())
   wrapper_id, wrapper = service.resolve_wrapper("dcm2niix-scan")

   # Register, enable a wrapper, then delete
   command_id = service.create_command({"name": "dcm2niix", "image": "xnat/dcm2niix:v1.2", ...})
   service.enable_wrapper(command_id, wrapper_id, project="MYPROJECT")
   service.delete_command(command_id)

.. autoclass:: xnatctl.services.commands.CommandService
   :members:
   :undoc-members:
   :show-inheritance:

Docker Admin Service
---------------------

Inspect and manage the XNAT Container Service's underlying docker daemon
connection -- known images, configured hubs, the daemon connection itself,
and pulling images. Returns plain ``dict``/``list[dict]``, for the same
reason as ``CommandService``. ``images()``, ``get_server()``,
``pull_image()``, and ``set_server()`` all raise a plain ``XNATCtlError``
with an actionable hint when no Docker daemon is reachable from the XNAT
server (a 500 with a raw Java exception body, verified live -- the single
most likely real-world state for any of these calls). ``set_server()``
reads the current configuration first and merges the new host into it,
since ``POST /xapi/docker/server`` is (by analogy with the sibling
``/xapi/commands/{id}`` route, which IS verified) presumed to be a full
replace -- this could not be confirmed directly, since this project's
integration stack has no Docker daemon to round-trip against.

.. code-block:: python

   from xnatctl.services.docker_admin import DockerAdminService

   service = DockerAdminService(client)

   images = service.images()
   server = service.get_server()
   service.pull_image("xnat/dcm2niix:v1.2")
   service.set_server("unix:///var/run/docker.sock")

.. autoclass:: xnatctl.services.docker_admin.DockerAdminService
   :members:
   :undoc-members:
   :show-inheritance:

Container Service
-------------------

List, inspect, launch, and kill Container Service container runs (one per
wrapper launch), and read their captured log output. Composes
``CommandService`` by constructor injection (defaulting to a fresh
instance) so ``launch()`` and ``resolve_wrapper()`` reuse its wrapper
resolution without a circular import. Returns plain ``dict``/``list[dict]``,
for the same reason as ``CommandService``. A container ID is a ``str``
throughout -- the routes accept either the numeric database ID or the
Docker container ID string. The container object's field names (``id``,
``status``, ``status-time``, ``command-id``, ``wrapper-id``, ``project``,
``user-id``, ``history``, ``workflow-id``, ...) and the launch response's
field names (``status``, ``params``, ``command-id``, ``wrapper-id``,
``workflow-id`` -- notably no ``container-id``) were read from the
Container Service plugin's own ``@JsonProperty`` declarations, so they are
the plugin's stated wire format rather than an inference. Note there is no
``start-time``: a start time comes from the earliest ``history`` entry's
``time-recorded``, falling back to the top-level ``status-time``. What is
still unexercised is a *populated* container object end to end, including
what ``launch()`` and a successful ``kill_container()`` look like against a
real running container -- that needs a reachable Docker daemon, which this
project's integration stack deliberately does not provide (mounting the
host Docker socket into a test container is a privilege-escalation risk).
``launch()`` itself, and the fact that the server does not validate the
wrapper/project/inputs before queueing, were verified live; only the
successful, daemon-backed launch path was not.

.. code-block:: python

   from xnatctl.services.containers import ContainerService

   service = ContainerService(client)

   containers = service.list(project="MYPROJECT", status="Running")
   container = service.get("501")

   # Logs are streamed, not buffered -- iterate and write bytes yourself.
   with service.stream_logs("501", stream="stdout") as response:
       for chunk in response.iter_bytes():
           sys.stdout.buffer.write(chunk)

   # Resolve a wrapper (numeric ID or name) and launch it in a project.
   wrapper_id, wrapper = service.resolve_wrapper("dcm2niix-scan")
   params = service.preflight_launch(
       "MYPROJECT", wrapper, {}, experiment="XNAT_E00001"
   )
   result = service.launch("MYPROJECT", wrapper_id, params)

   service.kill_container("501")

.. autoclass:: xnatctl.services.containers.ContainerService
   :members:
   :undoc-members:
   :show-inheritance:

Anonymize Service
-------------------

Read and write the site-wide and per-project DicomEdit anonymization
scripts, and their enabled state. Returns plain ``str``/``bool``/``None`` --
a script is DicomEdit text, not JSON, so there is no Pydantic model here.
Verified live against XNAT 1.9.2.1. Two shapes are surprising enough to call
out: the project-scoped routes answer a raw 500 (not 404) for a project that
does not exist, so every project-scoped method checks existence first via
``ProjectService.get``; and ``PUT .../enabled`` reads its actual value from
an ``enable`` query parameter rather than its JSON body (the body's content
is never read, but a JSON ``Content-Type`` is required or the route answers
415). ``GET /xapi/anonymize/projects/{project}`` answers empty both when a
project never had a script and when its script is merely disabled --
``project_has_script()`` uses a different, older config-history route to
tell those two states apart, which is what the enable/disable preflight
actually checks.

.. code-block:: python

   from xnatctl.services.anon import AnonymizeService

   service = AnonymizeService(client)

   site_script = service.get_site_script()
   service.set_site_script('version "6.1"\n(0010,0010) := subject')
   service.set_site_enabled(False)

   project_script = service.get_project_script("MYPROJECT")  # None if unset
   service.set_project_script("MYPROJECT", 'version "6.1"\n(0010,0010) := "ANON"')
   service.set_project_enabled("MYPROJECT", True)

.. autoclass:: xnatctl.services.anon.AnonymizeService
   :members:
   :undoc-members:
   :show-inheritance:

DICOM SCP Service
-------------------

List, create, delete, and enable/disable XNAT's DICOM SCP receivers (the AE
title/port listeners that accept incoming DICOM C-STORE traffic). Returns
plain ``dict``/``list[dict]``, for the same reason as ``CommandService`` --
``DicomSCPInstance`` JSON is a plugin-internal shape. Verified live against
XNAT 1.9.2.1. ``PUT /xapi/dicomscp/{id}`` is a PARTIAL MERGE (the opposite
of ``CommandService.update_command``'s full-replace ``POST
/xapi/commands/{id}``): a body of just ``{"enabled": ...}`` leaves every
other field untouched. The server does not validate ``port`` at all -- ``0``
and a port already bound by another receiver are both accepted silently --
so ``scp create`` validates it client-side first.

.. code-block:: python

   from xnatctl.services.scp import DicomScpService

   service = DicomScpService(client)

   receivers = service.list_scps()
   identifier = service.resolve_identifier(None)  # defaults if exactly one is registered
   created = service.create_scp("MYSCP", 8105, identifier)
   service.set_enabled(created["id"], False)
   service.delete_scp(created["id"])

.. autoclass:: xnatctl.services.scp.DicomScpService
   :members:
   :undoc-members:
   :show-inheritance:

Search Service
-------------------

List, show, run, and delete XNAT saved (stored) searches. Returns plain
``str``/``list[dict]`` -- a saved search's definition is XML with no fixed
schema, and its result rows have a dynamic, per-search column shape, so
there is no stable Pydantic model for either. Verified live against XNAT
1.9.2.1, including creating, running, and deleting one real saved search.
``GET /data/search/saved/{id}`` (the definition/"show" route) has NO JSON
representation at all -- ``?format=json`` 404s even for a real, existing
search, so ``get_definition()`` always requests XML. The distinct "run" route,
``GET /data/search/saved/{id}/results``, does support JSON and returns a
dynamic ``ResultSet.Columns``/``Result`` shape driven entirely by the
fields the search was built with. Delete is idempotent-succeeds: ``DELETE``
answers 200 even for an unknown search id.

.. code-block:: python

   from xnatctl.services.search import SearchService

   service = SearchService(client)

   searches = service.list_searches()
   definition_xml = service.get_definition("my_search")
   rows = service.run("my_search")
   service.delete("my_search")

.. autoclass:: xnatctl.services.search.SearchService
   :members:
   :undoc-members:
   :show-inheritance:

Event Service
-------------------

List, create, delete, and activate/deactivate XNAT Event Service
subscriptions, plus the ``actions``/``event-types`` catalogs used to build
one. Returns plain ``dict``/``list[dict]``/``int``, for the same reason as
``CommandService`` -- subscription JSON is a core-XNAT-version-dependent
shape. Verified live against XNAT 1.9.2.1. Single-subscription operations
(create/show/delete/activate/deactivate) live under the *singular*
``/xapi/events/subscription``; listing lives under the *plural*
``/xapi/events/subscriptions`` (``GET`` only) -- a different noun, not a
different verb on the same path. A successful create answers plain text
(``"{name}:{id}"``), not JSON. The site-wide Event Service can be switched
off entirely (``GET /xapi/events/prefs``), and *when* matters: toggled off
at runtime only ``create_subscription`` fails (405, "Event Service
disabled."), but on a server *booted* with it off -- every fresh install --
subscription listing and reads fail too (listing answers 200 with an empty
body, reads 405); only the ``actions``/``event-types`` catalogs work either
way. See the ``xnatctl.services.events`` module docstring.

.. code-block:: python

   from xnatctl.services.events import EventService

   service = EventService(client)

   subscriptions = service.list_subscriptions()
   actions = service.list_actions()          # valid action-key values
   event_types = service.list_event_types()  # valid event-filter.event-type values

   subscription_id = service.create_subscription({
       "name": "log-new-projects",
       "active": True,
       "event-filter": {
           "event-type": "org.nrg.xnat.eventservice.events.ProjectEvent",
           "status": "CREATED",
           "project-ids": [],
       },
       "act-as-event-user": True,
       "action-key": actions[0]["action-key"],
       "attributes": {},
   })
   service.deactivate_subscription(subscription_id)
   service.activate_subscription(subscription_id)
   service.delete_subscription(subscription_id)

.. autoclass:: xnatctl.services.events.EventService
   :members:
   :undoc-members:
   :show-inheritance:
