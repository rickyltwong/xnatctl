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

Catalog refresh runs in parallel by default. Use ``--no-parallel`` to run
sequentially, or ``--workers N`` to control the number of concurrent
requests:

.. code-block:: console

   $ xnatctl admin refresh-catalogs MYPROJECT --workers 8
   $ xnatctl admin refresh-catalogs MYPROJECT --no-parallel

.. note::

   Catalog refreshes can be slow on large projects with thousands of
   experiments. Consider running targeted refreshes or using ``--limit``
   to process experiments in batches.

For a scripted workflow that uses catalog refresh after manual filesystem
operations, see the "Refresh Catalogs After Manual File Operations" section
in :doc:`workflows`.


User Management
---------------

The ``admin user`` subgroup provides commands for managing XNAT user
permissions. Currently, xnatctl supports adding users to project groups.

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

**Listing and removing users**

User listing and removal are not yet available as dedicated commands. Use the
``api`` escape hatch as a workaround:

.. code-block:: console

   # List all users on the server
   $ xnatctl api get /data/users

   # List users in a specific project
   $ xnatctl api get /data/projects/MYPROJECT/users

   # Remove a user from a project (requires confirmation)
   $ xnatctl api delete /data/projects/MYPROJECT/users/member/jsmith --yes

See :ref:`admin-api-escape-hatch` below for more examples.


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

For a scripted approach to data integrity auditing (comparing session counts
against a manifest), see the "Auditing a Project" section in :doc:`workflows`.


.. _admin-api-escape-hatch:

Using the API for Additional Admin Tasks
-----------------------------------------

Several administrative operations are available through XNAT's REST API but
do not yet have dedicated xnatctl commands. You can access them using the
``api`` escape hatch.

**Server information**

.. code-block:: console

   $ xnatctl api get /xapi/siteConfig/buildInfo/version

Returns the XNAT version string (e.g. ``1.9.1.2``).

**Site configuration**

.. code-block:: console

   # View all site configuration
   $ xnatctl api get /xapi/siteConfig

   # View a specific setting
   $ xnatctl api get /xapi/siteConfig/siteId

**User details**

.. code-block:: console

   # List all users
   $ xnatctl api get /data/users

   # Get details for a specific user
   $ xnatctl api get /data/users/jsmith

   # List users in a project with their roles
   $ xnatctl api get /data/projects/MYPROJECT/users

.. tip::

   The ``api get`` command automatically formats XNAT's ``ResultSet``
   responses as tables, so you get readable output without extra processing.
   Use ``--output json`` when you need machine-readable data.


Planned Commands
----------------

The following admin commands are planned for future releases:

- ``admin user list`` -- List users (server-wide or per-project)
- ``admin user show`` -- Show detailed information for a user
- ``admin user remove`` -- Remove a user from project groups
- ``admin server-info`` -- Display XNAT server version and build information
- ``admin site-config`` -- View and modify site configuration

Until these commands are available, use ``xnatctl api get`` as described
above. See :doc:`cli-reference` for full ``api`` command documentation.
