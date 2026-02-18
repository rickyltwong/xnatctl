XNAT Compatibility
==================

This page describes the practical XNAT compatibility expectations for
``xnatctl``.

Compatibility model
-------------------

``xnatctl`` interacts with XNAT through documented REST endpoints and standard
session authentication. It is designed to work with modern XNAT deployments,
including current XNAT 1.8.x installations.

Because XNAT features and endpoint behavior can vary by server version and
administrator configuration, compatibility is best understood by feature area
rather than by one fixed minimum version.

Feature notes
-------------

Core REST operations
^^^^^^^^^^^^^^^^^^^^

The following command families use stable, widely available XNAT REST APIs and
are expected to work on typical XNAT 1.8.x servers:

- ``project``
- ``subject``
- ``session``
- ``scan``
- ``resource``
- ``api``

Prearchive workflows
^^^^^^^^^^^^^^^^^^^^

Prearchive workflows (``prearchive`` commands and ``session upload --prearchive``)
require prearchive functionality to be enabled and accessible on the target
server.

Direct-to-archive uploads
^^^^^^^^^^^^^^^^^^^^^^^^^

Direct-to-archive mode relies on XNAT Import API support for
``Direct-Archive=true``, which is available in XNAT 1.8.3+.

If this mode is unsupported or disallowed by server configuration,
``xnatctl`` uploads should use prearchive-first workflows.

DICOM C-STORE uploads
^^^^^^^^^^^^^^^^^^^^^

DICOM network uploads (``session upload --transport cstore``) depend on XNAT's
DICOM receiver/SCP configuration and network reachability. These are operational
requirements independent of REST API compatibility.

Validation tips
---------------

For a new server, validate compatibility with this quick checklist:

1. Confirm server version and connectivity: ``xnatctl admin info``.
2. Validate authentication: ``xnatctl auth login`` and ``xnatctl whoami``.
3. Test listing operations: ``xnatctl project list`` and ``xnatctl session list``.
4. Run a small upload in your intended mode (prearchive or direct archive).

When opening issues, include:

- XNAT version (from ``admin info``)
- Exact command used
- Sanitized error output
- Whether prearchive/direct-archive/C-STORE is enabled
