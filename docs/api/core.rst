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

.. code-block:: python

   from xnatctl.core.client import XNATClient

   # Create and authenticate client
   client = XNATClient(
       base_url="https://xnat.example.org",
       username="admin",
       password="secret",
       timeout=60,
       verify_ssl=True
   )

   client.authenticate()

   # Make API calls
   response = client.get("/data/projects")

   # Use as context manager
   with XNATClient(base_url="https://xnat.example.org") as client:
       client.authenticate()
       data = client.get("/data/projects")

**Class Reference:**

.. autoclass:: xnatctl.core.client.XNATClient
   :members:
   :undoc-members:
   :special-members: __init__, __enter__, __exit__

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

**Exception Hierarchy:**

.. code-block:: text

   XNATCtlError (base)
   ├── ConfigurationError
   │   └── ProfileNotFoundError
   ├── AuthenticationError
   ├── ValidationError
   ├── NetworkError
   │   ├── ConnectionError
   │   ├── ServerUnreachableError
   │   ├── RetryExhaustedError
   │   └── TimeoutError
   └── ResourceNotFoundError

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
