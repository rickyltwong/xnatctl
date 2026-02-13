Models API
==========

The models package provides Pydantic data models for XNAT resources with
validation, serialization, and table formatting capabilities.

Base Models
-----------

Foundation classes that all resource models inherit from.

**Model Hierarchy:**

.. code-block:: text

   PydanticBaseModel
   └── BaseModel
       └── XNATResource
           ├── Project
           ├── Subject
           ├── Session (Experiment)
           ├── Scan
           └── Resource

**Common Features:**

All models provide:

- Automatic validation of field types and constraints
- JSON serialization/deserialization
- Table row formatting for Rich output
- Alias support for XNAT API field names (e.g., ``ID`` → ``id``)
- Nullable field handling with ``None`` defaults

**BaseModel**

Common configuration and utility methods for all models.

.. autoclass:: xnatctl.models.base.BaseModel
   :members:
   :undoc-members:
   :show-inheritance:

**XNATResource**

Base class for all XNAT resource types with common fields.

.. autoclass:: xnatctl.models.base.XNATResource
   :members:
   :undoc-members:
   :show-inheritance:

Project
-------

Represents an XNAT project containing subjects and sessions.

**Model Structure:**

A project is the top-level organizational unit in XNAT. It contains subjects
(participants) and sessions (imaging visits).

**Usage Example:**

.. code-block:: python

   from xnatctl.core.client import XNATClient
   from xnatctl.services.projects import ProjectService

   client = XNATClient(base_url="https://xnat.example.org")
   client.authenticate()

   service = ProjectService(client)
   projects = service.list()

   for project in projects:
       print(f"{project.id}: {project.name}")
       print(f"  PI: {project.pi}")
       print(f"  Subjects: {project.subject_count}")
       print(f"  Sessions: {project.session_count}")

.. autoclass:: xnatctl.models.project.Project
   :members:
   :undoc-members:
   :show-inheritance:

Subject
-------

Represents a subject (participant/patient) within a project.

**Model Structure:**

Subjects belong to a single project and can have multiple imaging sessions.

.. autoclass:: xnatctl.models.subject.Subject
   :members:
   :undoc-members:
   :show-inheritance:

Session (Experiment)
--------------------

Represents an imaging session or experiment. A session contains scans and
resources.

**Model Structure:**

Sessions are the primary data collection events in XNAT. They belong to a
subject and contain one or more scans (individual imaging series).

**Modality Types:**

Common modality values:

- ``MR`` - Magnetic Resonance Imaging
- ``PET`` - Positron Emission Tomography
- ``CT`` - Computed Tomography
- ``US`` - Ultrasound
- ``XA`` - X-Ray Angiography

**Usage Example:**

.. code-block:: python

   from xnatctl.services.sessions import SessionService

   service = SessionService(client)
   sessions = service.list(project="MYPROJECT", modality="MR")

   for session in sessions:
       print(f"{session.label} - {session.session_date}")
       print(f"  Subject: {session.subject_label}")
       print(f"  Scans: {session.scan_count}")
       print(f"  Modality: {session.modality}")

.. autoclass:: xnatctl.models.session.Session
   :members:
   :undoc-members:
   :show-inheritance:

Scan
----

Represents an individual imaging scan (series) within a session.

**Model Structure:**

Scans are the lowest-level imaging data unit in XNAT. Each scan represents
a single imaging series (e.g., T1-weighted MRI, PET acquisition).

.. autoclass:: xnatctl.models.scan.Scan
   :members:
   :undoc-members:
   :show-inheritance:

Resource
--------

Represents a file resource attached to a project, subject, session, or scan.

**Model Structure:**

Resources are file containers (like folders) that can be attached to any
XNAT resource. Common resource types include DICOM, NIFTI, SNAPSHOTS.

**Usage Example:**

.. code-block:: python

   from xnatctl.services.resources import ResourceService

   service = ResourceService(client)
   resources = service.list(
       project="MYPROJECT",
       session="SESSION01"
   )

   for resource in resources:
       print(f"{resource.label} - {resource.file_count} files")
       print(f"  Size: {resource.size_bytes} bytes")
       print(f"  Format: {resource.format}")

.. autoclass:: xnatctl.models.resource.Resource
   :members:
   :undoc-members:
   :show-inheritance:

Progress Models
---------------

Data models for tracking upload and download progress.

.. automodule:: xnatctl.models.progress
   :members:
   :undoc-members:
