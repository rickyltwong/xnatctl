XNAT Compatibility
==================

``xnatctl`` targets modern XNAT deployments and communicates exclusively through
documented REST endpoints and standard session authentication. In this context,
"compatibility" means that a given ``xnatctl`` command can issue requests that the
server understands and responds to correctly. It does *not* imply that every
feature works identically across every XNAT installation.

XNAT is a configurable platform. The features available on any particular server
depend on the XNAT version, the plugins installed, and the choices the
administrator has made. A site may disable the prearchive, restrict
direct-to-archive imports, or run an older release that lacks certain API
endpoints. Because of this variation, compatibility is best understood per feature
area rather than as a single minimum version number.

Compatibility model
-------------------

``xnatctl`` is designed and tested against XNAT 1.8.x. It relies on the stable
REST API surface that XNAT exposes under ``/data/`` and ``/xapi/``, using
standard session-cookie authentication. No XNAT-specific client libraries are
used -- all communication is plain HTTP through ``httpx``.

Where a feature requires a specific minimum XNAT version or server-side
configuration, the relevant section below calls it out explicitly. If you are
unsure which XNAT version your server runs, the :ref:`validation-tips` section
shows you how to check.

Feature notes
-------------

The subsections below break down compatibility by feature area. If a command
family is not listed here, it uses the same stable REST endpoints as the core
operations and is expected to work on any supported XNAT 1.8.x server.

Core REST operations
^^^^^^^^^^^^^^^^^^^^

These command families use stable, widely available XNAT REST APIs and are
expected to work on any typical XNAT 1.8.x server without special configuration:

- ``project``
- ``subject``
- ``session``
- ``scan``
- ``resource``
- ``api``

If any of these commands return unexpected errors, the most common causes are
permission restrictions on your XNAT account or network-level issues such as
proxies or firewalls.

Prearchive workflows
^^^^^^^^^^^^^^^^^^^^

The :doc:`concepts` page explains the prearchive staging area in detail.
Prearchive workflows (``prearchive`` commands and ``session upload --prearchive``)
require prearchive functionality to be enabled and accessible on the target
server. Most XNAT installations leave the prearchive enabled by default, but an
administrator can disable it or restrict access at the project level.

Direct-to-archive uploads
^^^^^^^^^^^^^^^^^^^^^^^^^

Direct-to-archive mode bypasses the :doc:`concepts` prearchive staging area and
writes uploaded data straight into the permanent archive. This mode relies on
XNAT Import API support for ``Direct-Archive=true``.

.. note::

   Direct-to-archive uploads require **XNAT 1.8.3 or later**. If your server
   runs an older version, or if direct-archive has been disabled by an
   administrator, uploads using this mode will fail. Use prearchive-first
   workflows instead (``session upload --prearchive``).

DICOM C-STORE uploads
^^^^^^^^^^^^^^^^^^^^^

DICOM network uploads (``session upload --transport cstore``) depend on the
XNAT DICOM receiver (SCP) being configured and network-reachable from your
workstation. These are operational requirements independent of REST API
compatibility -- a server can have a fully functional REST API while its DICOM
receiver is disabled or firewalled.

You will need the DICOM Application Entity (AE) title and port from your XNAT
administrator before attempting C-STORE uploads.

.. _validation-tips:

Validation tips
---------------

When you connect to a new XNAT server for the first time, it is worth running
through a short validation sequence to confirm that your credentials, network
path, and intended upload mode all work before you start moving real data. The
steps below walk you through this process.

.. tip::

   Run this full sequence on every new server or after a major XNAT upgrade.
   It takes under a minute and can save you significant troubleshooting time
   later.

**Step 1 -- Confirm server version and connectivity.**
Use ``admin info`` to verify that ``xnatctl`` can reach the server and to check
the reported XNAT version:

.. code-block:: console

   $ xnatctl admin info

You should see the server URL and XNAT version in the output. If the command
times out or returns a connection error, check your network, VPN, and the
``url`` value in your profile.

**Step 2 -- Validate authentication.**
Log in and confirm that the server recognizes your account:

.. code-block:: console

   $ xnatctl auth login
   $ xnatctl whoami

The ``whoami`` output should display your username and project memberships. If
authentication fails, verify your credentials or ask your XNAT administrator
whether your account is active.

**Step 3 -- Test listing operations.**
Run a couple of read-only list commands to confirm that your account has the
expected project access:

.. code-block:: console

   $ xnatctl project list
   $ xnatctl session list -P YOUR_PROJECT

If the project list is empty or a specific project is missing, you may need
additional permissions from the project owner.

**Step 4 -- Run a small test upload.**
Upload a small dataset in your intended mode (prearchive or direct archive) to
confirm that the upload path works end-to-end:

.. code-block:: console

   $ xnatctl session upload /path/to/small-dataset -P YOUR_PROJECT --prearchive

If you plan to use direct-to-archive mode, substitute ``--direct-archive`` for
``--prearchive`` and confirm your server meets the XNAT 1.8.3+ requirement.

Reporting issues
----------------

If you encounter a problem that you believe is a bug in ``xnatctl`` rather than
a server configuration issue, please open a GitHub issue. Including thorough
details up front helps maintainers reproduce and diagnose the problem quickly.

.. note::

   When opening an issue, please include the following information:

   - **xnatctl version** -- run ``xnatctl --version``.
   - **XNAT server version** -- from ``xnatctl admin info`` output.
   - **Exact command** -- the full command line you ran, with any sensitive
     values (passwords, internal hostnames) redacted.
   - **Sanitized error output** -- the complete traceback or error message,
     with credentials and PHI removed.
   - **Server configuration details** -- whether prearchive, direct-archive,
     or C-STORE is enabled, and any relevant project-level settings.

Providing this information avoids back-and-forth and lets maintainers start
investigating immediately.
