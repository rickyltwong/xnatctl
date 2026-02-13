Services API
============

The services package provides high-level Python interfaces for XNAT REST API
operations. Each service class encapsulates operations for a specific resource
type (projects, subjects, sessions, etc.).

Service Layer Architecture
---------------------------

**Design Pattern:**

All services follow the service layer pattern:

1. **Instantiate with authenticated client**
2. **Call service methods** (list, get, create, update, delete)
3. **Receive typed model objects** (not raw JSON)

**Benefits:**

- Type-safe operations with Pydantic models
- Automatic retry and error handling
- Consistent pagination and filtering
- Clean separation of concerns

**Common Usage Pattern:**

.. code-block:: python

   from xnatctl.core.client import XNATClient
   from xnatctl.services.projects import ProjectService

   # Create and authenticate client
   client = XNATClient(
       base_url="https://xnat.example.org",
       username="admin",
       password="secret"
   )
   client.authenticate()

   # Instantiate service
   service = ProjectService(client)

   # Call service methods
   projects = service.list()
   project = service.get("MYPROJECT")

Base Service
------------

Foundation class providing common HTTP method wrappers and pagination utilities.

.. autoclass:: xnatctl.services.base.BaseService
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

   from xnatctl.services.projects import ProjectService

   service = ProjectService(client)

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
       project="MYPROJECT",
       subject="SUB001"
   )

   # Rename subject
   service.rename(
       project="MYPROJECT",
       subject="SUB001",
       new_label="PARTICIPANT001"
   )

   # Delete subject (WARNING: destructive)
   service.delete(
       project="MYPROJECT",
       subject="SUB001"
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
       project="MYPROJECT",
       session="SESSION01"
   )

   for scan in scans:
       print(f"{scan.scan_id}: {scan.type}")
       print(f"  Quality: {scan.quality}")
       print(f"  Files: {scan.file_count}")

   # Delete a scan
   service.delete(
       project="MYPROJECT",
       session="SESSION01",
       scan="1"
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

.. code-block:: python

   from xnatctl.services.resources import ResourceService

   service = ResourceService(client)

   # List resources
   resources = service.list(
       project="MYPROJECT",
       session="SESSION01"
   )

   # Upload files to a resource
   service.upload(
       project="MYPROJECT",
       session="SESSION01",
       resource="PROCESSED",
       files=["analysis.nii.gz", "report.pdf"]
   )

   # Download resource
   service.download(
       project="MYPROJECT",
       session="SESSION01",
       resource="DICOM",
       dest="./downloads/"
   )

.. autoclass:: xnatctl.services.resources.ResourceService
   :members:
   :undoc-members:
   :show-inheritance:

Downloads Service
-----------------

High-performance parallel download operations with resume support.

**Features:**

- Multi-threaded parallel downloads
- Resume support for interrupted transfers
- Progress tracking with Rich progress bars
- Automatic retry on transient failures
- Checksum verification

**Parallel Download Example:**

.. code-block:: python

   from xnatctl.services.downloads import DownloadService

   service = DownloadService(client)

   # Download with 8 parallel workers
   service.download_session(
       project="MYPROJECT",
       session="SESSION01",
       dest="./data/",
       workers=8,
       show_progress=True
   )

.. automodule:: xnatctl.services.downloads
   :members:
   :undoc-members:

Uploads Service
---------------

High-performance parallel upload operations for DICOM and file resources.

**Upload Strategies:**

xnatctl supports two DICOM upload strategies:

1. **Gradual DICOM** (default): REST API upload with parallel workers
2. **Prearchive**: Upload to staging area for review before archiving

**Features:**

- Multi-threaded parallel uploads
- Automatic retry with exponential backoff
- Progress tracking
- Sequential fallback for failed files
- Thread-local HTTP clients for stability

**Parallel Upload Example:**

.. code-block:: python

   from xnatctl.services.uploads import UploadService

   service = UploadService(client)

   # Upload DICOM files with 8 workers
   service.upload_dicom(
       project="MYPROJECT",
       subject="SUB001",
       session="SESSION01",
       files=["scan001.dcm", "scan002.dcm", ...],
       workers=8,
       show_progress=True
   )

**Error Handling:**

Failed uploads are automatically retried at lower concurrency, with a final
sequential retry pass to maximize completion rate on flaky networks.

.. automodule:: xnatctl.services.uploads
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

.. code-block:: python

   from xnatctl.services.prearchive import PrearchiveService

   service = PrearchiveService(client)

   # List prearchive sessions
   sessions = service.list(project="MYPROJECT")

   for session in sessions:
       print(f"{session.name} - {session.status}")
       print(f"  Uploaded: {session.upload_date}")
       print(f"  Scans: {session.scan_count}")

   # Archive session from prearchive
   service.archive(
       project="MYPROJECT",
       timestamp="20240101_120000",
       session="SESSION01"
   )

   # Delete prearchive session
   service.delete(
       project="MYPROJECT",
       timestamp="20240101_120000",
       session="SESSION01"
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

   # Run pipeline on a session
   run_id = service.run(
       project="MYPROJECT",
       session="SESSION01",
       pipeline="DicomToNifti"
   )

   # Check pipeline status
   status = service.status(
       project="MYPROJECT",
       run_id=run_id
   )
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

   # View audit logs
   logs = service.audit_logs(
       project="MYPROJECT",
       limit=100
   )

.. autoclass:: xnatctl.services.admin.AdminService
   :members:
   :undoc-members:
   :show-inheritance:
