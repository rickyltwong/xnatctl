Core API
========

The core package provides foundational classes for XNAT server communication,
configuration management, authentication, and error handling.

Client
------

The ``XNATClient`` class is the primary HTTP client for all XNAT REST API operations.
It provides automatic retry logic, session management, pagination support, and
connection pooling.

**Key Features:**

- Automatic retry with exponential backoff for transient errors (502, 503, 504)
- Session-based authentication with token caching
- Pagination support for large result sets
- SSL verification control
- Context manager support for automatic cleanup
- Connection pooling via httpx

**Basic Usage:**

The one-call entry point is
:meth:`XNATClient.from_profile <xnatctl.core.client.XNATClient.from_profile>`,
which resolves credentials from a saved config profile exactly as the CLI does.
Entering the context manager logs in when a password is available and no session
token is cached yet:

.. code-block:: python

   import xnatctl

   with xnatctl.XNATClient.from_profile("prod") as client:
       projects = client.projects.list()

To target a server without a saved profile, construct the client directly:

.. code-block:: python

   from xnatctl.core.client import XNATClient

   client = XNATClient(
       base_url="https://xnat.example.org",
       username="admin",
       password="secret",
       timeout=60,
       verify_ssl=True,
   )
   client.authenticate()
   response = client.get("/data/projects")

**Class Reference:**

.. autoclass:: xnatctl.core.client.XNATClient
   :members:
   :undoc-members:
   :special-members: __init__, __enter__, __exit__

Retry Policy
------------

The single home for retry policy: the status-code sets and backoff helpers the
client ladder consumes, the response-based upload ladder with its
transient-vs-permanent HTTP 400 discrimination, and the generic
``retry_call`` primitive.

.. automodule:: xnatctl.core.retry
   :members:
   :undoc-members:

Config
------

Configuration management with YAML file support and environment variable overrides.
Supports multiple server profiles for different XNAT environments.

**Configuration File Location:**

- ``~/.config/xnatctl/config.yaml`` (default)

**Profile Structure:**

Each profile defines connection parameters for one XNAT server:

.. code-block:: yaml

   default_profile: production
   output_format: table

   profiles:
     production:
       url: https://xnat.example.org
       username: admin
       verify_ssl: true
       timeout: 30
       default_project: MYPROJECT

**Credential Resolution:**

The ``Config`` class resolves credentials in this priority order:

1. Environment variables (``XNAT_URL``, ``XNAT_USER``, ``XNAT_PASS``)
2. Config file profile settings
3. Default values

**Class Reference:**

.. autoclass:: xnatctl.core.config.Config
   :members:
   :undoc-members:

.. autoclass:: xnatctl.core.config.Profile
   :members:
   :undoc-members:

Connect
-------

One-call client construction from a config profile. This is the credential
resolution the CLI runs before every command, extracted so a library caller
(and :meth:`XNATClient.from_profile <xnatctl.core.client.XNATClient.from_profile>`)
gets the same client.

.. automodule:: xnatctl.core.connect
   :members:
   :undoc-members:

Authentication
--------------

Session-based authentication with token caching. Handles login, logout, and
session validation.

**Session Token Storage:**

- Cached at ``~/.config/xnatctl/.session`` per profile
- Automatically reused until expiration
- Can be overridden with ``XNAT_TOKEN`` environment variable

.. automodule:: xnatctl.core.auth
   :members:
   :undoc-members:

Exceptions
----------

Comprehensive exception hierarchy for error handling.

**Exception Hierarchy** (excerpt -- the most commonly caught branches; the
autodoc below lists every class, including the HTTP-response, operation,
DICOM, transfer, and cancellation branches):

.. code-block:: text

   XNATCtlError (base)
   ├── ConfigurationError
   │   └── ProfileNotFoundError
   ├── InputValidationError
   ├── AuthenticationError
   │   ├── SessionExpiredError
   │   └── PermissionDeniedError
   ├── XNATConnectionError
   │   ├── NetworkError
   │   ├── ServerUnreachableError
   │   ├── RequestTimeoutError
   │   └── RetryExhaustedError
   └── ResourceError
       ├── ResourceNotFoundError
       └── ResourceExistsError

The stdlib-shadowing names ``ConnectionError``, ``TimeoutError``, and
``ValidationError`` remain as deprecated subclass aliases of
``XNATConnectionError``, ``RequestTimeoutError``, and ``InputValidationError``.
They are never raised internally and emit a ``DeprecationWarning`` on
instantiation, so ``except xnatctl.ConnectionError`` no longer matches the
connection errors the library raises. They are removed in a later minor release.

**Usage Example:**

.. code-block:: python

   from xnatctl.core.client import XNATClient
   from xnatctl.core.exceptions import (
       AuthenticationError,
       ResourceNotFoundError,
       NetworkError
   )

   try:
       client = XNATClient(base_url="https://xnat.example.org")
       client.authenticate()
   except AuthenticationError:
       print("Invalid credentials")
   except NetworkError:
       print("Cannot reach server")
   except ResourceNotFoundError as e:
       print(f"Resource not found: {e.resource_type} {e.resource_id}")

.. automodule:: xnatctl.core.exceptions
   :members:
   :undoc-members:
   :show-inheritance:

Validation
----------

Input validation utilities for XNAT resource identifiers, URLs, and parameters.

**Validators:**

- ``validate_server_url(url: str) -> str`` - Normalize and validate XNAT server URLs
- ``validate_project_id(project_id: str) -> None`` - Validate project ID format
- ``validate_subject_label(label: str) -> None`` - Validate subject label format
- ``validate_session_label(label: str) -> None`` - Validate session label format

.. automodule:: xnatctl.core.validation
   :members:
   :undoc-members:

Output
------

Output formatting utilities for JSON, table, and quiet modes. Uses Rich for
terminal rendering.

**Supported Formats:**

- ``json`` - Machine-readable JSON output
- ``table`` - Human-readable table with borders and alignment
- ``quiet`` - Minimal output (IDs only)

**Usage Example:**

.. code-block:: python

   from xnatctl.core.output import OutputFormatter

   formatter = OutputFormatter(format="table")
   formatter.print_table(
       data=[{"id": "proj1", "name": "Project 1"}],
       columns=["id", "name"]
   )

.. automodule:: xnatctl.core.output
   :members:
   :undoc-members:

Logging
-------

Structured logging utilities with configurable verbosity levels.

.. automodule:: xnatctl.core.logging
   :members:
   :undoc-members:
