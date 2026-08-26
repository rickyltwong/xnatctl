Site Administration
===================

This page covers the administrative commands available in xnatctl. These
commands are aimed at **project owners** and **site administrators** who need
to maintain data integrity, manage user access, or review audit trails on an
XNAT server.

Most admin operations require elevated privileges. If a command returns a
permission error, check with your XNAT site administrator that your account
has the appropriate role.


Prerequisites
-------------

XNAT uses a role-based permission model. The roles relevant to admin
commands are:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Role
     - Capabilities
   * - **Member**
     - Read and download data within assigned projects
   * - **Collaborator**
     - Member permissions plus upload to assigned projects
   * - **Owner**
     - Full control over a project: manage users, delete data, refresh
       catalogs, run pipelines
   * - **Site Administrator**
     - Server-wide access: all projects, user management, site configuration,
       audit logs

To check your current role and authentication context:

.. code-block:: console

   $ xnatctl whoami
   $ xnatctl auth status

If you are not authenticated, run ``xnatctl auth login`` first. See
:doc:`configuration` for profile setup.


Catalog Maintenance
-------------------

XNAT tracks every file in a project through XML catalog files. These catalogs
record file paths, checksums, and resource statistics. When files are moved,
renamed, or deleted directly on the filesystem -- outside of XNAT's web
interface or REST API -- the catalogs become stale. A stale catalog causes
XNAT to report incorrect file counts, missing resources, or checksum
mismatches.

The ``admin refresh-catalogs`` command tells XNAT to re-scan the filesystem
and reconcile its catalogs with the actual files on disk.

**Basic usage:**

.. code-block:: console

   $ xnatctl admin refresh-catalogs MYPROJECT

This refreshes catalogs for every experiment in the project.

**Refresh options**

You can control what the refresh does with the ``--option`` flag. Specify it
multiple times to combine options:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Option
     - What it does
   * - ``checksum``
     - Generate checksums for files that are missing them
   * - ``delete``
     - Remove catalog entries for files that no longer exist on disk
   * - ``append``
     - Add catalog entries for new files found on disk
   * - ``populateStats``
     - Recalculate resource statistics (file counts, total size)

.. code-block:: console

   $ xnatctl admin refresh-catalogs MYPROJECT --option checksum --option delete

**Targeted refresh**

If you only changed files in specific experiments, use ``--experiment`` to
avoid a project-wide scan:

.. code-block:: console

   $ xnatctl admin refresh-catalogs MYPROJECT --experiment XNAT_E00001
   $ xnatctl admin refresh-catalogs MYPROJECT --experiment XNAT_E00001 --experiment XNAT_E00002

You can also limit the number of experiments processed with ``--limit``:

.. code-block:: console

   $ xnatctl admin refresh-catalogs MYPROJECT --limit 50

**Parallel execution**

Catalog refresh runs in parallel by default (4 workers). Use ``--workers N`` to
control concurrency, or ``--workers 1`` for sequential execution:

.. code-block:: console

   $ xnatctl admin refresh-catalogs MYPROJECT --workers 8
   $ xnatctl admin refresh-catalogs MYPROJECT --workers 1

.. note::

   Catalog refreshes can be slow on large projects with thousands of
   experiments. Consider running targeted refreshes or using ``--limit``
   to process experiments in batches.

For a scripted workflow that uses catalog refresh after manual filesystem
operations, see the "Refresh Catalogs After Manual File Operations" section
in :doc:`workflows`.


User Management
---------------

The ``admin user`` subgroup provides commands for managing XNAT user accounts:
adding a user to project groups, listing and inspecting accounts, enabling or
disabling an account, granting or revoking site-wide roles, removing project
membership, and killing a user's active sessions.

**Adding a user to groups**

Use ``admin user add`` to grant a user access to one or more projects:

.. code-block:: console

   $ xnatctl admin user add jsmith PROJ1_member PROJ2_owner

Group names follow the XNAT convention of ``{PROJECT}_{ROLE}``. You can also
generate group names automatically from project IDs:

.. code-block:: console

   $ xnatctl admin user add jsmith --projects PROJ1,PROJ2 --role member

This is equivalent to specifying ``PROJ1_member PROJ2_member`` directly.

Available roles:

- **owner** -- Full project control (manage users, delete data, run pipelines)
- **member** -- Read and download project data
- **collaborator** -- Read, download, and upload project data

**Bulk onboarding example**

To add a new team member to multiple projects at once:

.. code-block:: console

   $ xnatctl admin user add newresearcher \
       --projects STUDY_A,STUDY_B,STUDY_C \
       --role collaborator

**Listing and inspecting users**

.. code-block:: console

   $ xnatctl admin user list
   $ xnatctl admin user list --active   # only users with a live session
   $ xnatctl admin user show jsmith

**Enabling, disabling, and site-wide roles**

.. code-block:: console

   $ xnatctl admin user disable jsmith --yes
   $ xnatctl admin user enable jsmith --yes
   $ xnatctl admin user roles jsmith                          # list roles
   $ xnatctl admin user roles jsmith --grant Administrator --yes
   $ xnatctl admin user roles jsmith --revoke Administrator --yes

**Removing a user from a project, killing sessions, and listing groups**

.. code-block:: console

   $ xnatctl admin user remove jsmith --project MYPROJECT --yes
   $ xnatctl admin user kill-sessions jsmith --yes
   $ xnatctl admin user groups jsmith

.. tip::

   ``kill-sessions`` is the fix for a shared/service account that has
   exhausted its concurrent-session limit -- every new login starts failing
   with 401s even though the password is correct, because XNAT enforces a
   per-user cap on concurrent sessions and the shared credential has
   accumulated sessions from crashed or timed-out clients that never logged
   out. Killing them frees the slots back up.

Project-level membership (which role a user holds on a specific project, as
opposed to the server-wide groups above) is managed with ``project users``,
``project grant``, and ``project revoke`` -- see :doc:`cli-reference`.


Audit Log
---------

The ``admin audit`` command queries the XNAT audit log to review actions
performed on the server. This is useful for compliance, debugging, and
tracking changes made by users or automated processes.

.. code-block:: console

   $ xnatctl admin audit

**Filtering**

Narrow the results with filter options:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Option
     - Description
   * - ``--project TEXT`` / ``-P``
     - Filter entries by project ID
   * - ``--user TEXT`` / ``-u``
     - Filter entries by username
   * - ``--action TEXT``
     - Filter by action type (e.g., ``create``, ``delete``, ``modify``)
   * - ``--since TEXT``
     - Time range: relative (``7d``, ``30d``) or absolute (``2025-01-01``)
   * - ``--limit INT``
     - Maximum number of entries to return (default: 50)

.. code-block:: console

   $ xnatctl admin audit --project MYPROJECT --limit 20
   $ xnatctl admin audit --user admin --since 7d
   $ xnatctl admin audit --action delete --since 2025-01-01

Use ``--output json`` to get structured output for further processing:

.. code-block:: console

   $ xnatctl admin audit --project MYPROJECT --output json | jq '.[] | .timestamp'

.. warning::

   Audit log availability depends on your XNAT server configuration. Some
   XNAT deployments do not enable audit logging by default. If the command
   returns an error, contact your site administrator to enable it.

.. note::

   Independently of the server-side log, xnatctl records every destructive
   command it runs (deletes, transfers, prearchive operations) in a local
   audit trail at ``~/.config/xnatctl/audit.log``, which needs no server
   support at all. See the "Audit trail" section in :doc:`configuration`.

For a scripted approach to data integrity auditing (comparing session counts
against a manifest), see the "Auditing a Project" section in :doc:`workflows`.


.. _admin-api-escape-hatch:

Using the API for Additional Admin Tasks
-----------------------------------------

Server version, site configuration, installed plugins, and per-project user
listings now all have dedicated commands (``admin version``,
``admin site-config get``/``set``, ``admin plugins``, ``project users`` --
see :doc:`cli-reference`). The ``api`` escape hatch remains useful for
anything else XNAT's REST API exposes that xnatctl does not wrap yet.

**Server information**

.. code-block:: console

   $ xnatctl admin version
   $ xnatctl admin version -q   # bare version string

**Site configuration**

.. code-block:: console

   $ xnatctl admin site-config get              # entire site config
   $ xnatctl admin site-config get siteId        # one key
   $ xnatctl admin site-config set siteId MyXNAT --yes

**Installed plugins**

.. code-block:: console

   $ xnatctl admin plugins
   $ xnatctl admin plugins show containers  # plugin id, not "container-service"

.. tip::

   The ``api get`` command automatically formats XNAT's ``ResultSet``
   responses as tables, so you get readable output without extra processing.
   Use ``--output json`` when you need machine-readable data for an endpoint
   that has no dedicated command yet.
