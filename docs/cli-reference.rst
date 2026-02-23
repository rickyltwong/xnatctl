CLI Reference
=============

This is the complete command reference for xnatctl. Every command family, sub-command,
and option is documented here. If you are new to xnatctl, start with the
:doc:`quickstart` guide for a hands-on introduction.

Global Options
--------------

Every xnatctl command accepts these global options:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Option
     - Description
   * - ``--profile TEXT`` / ``-p``
     - Select a configuration profile for this command. Overrides the default profile
       and the ``XNAT_PROFILE`` environment variable.
   * - ``--output TEXT`` / ``-o``
     - Output format: ``json`` or ``table`` (default: ``table``). JSON is useful for
       scripting and piping into tools like ``jq``.
   * - ``--quiet`` / ``-q``
     - Suppress non-essential output; print only IDs.
   * - ``--verbose`` / ``-v``
     - Enable verbose/debug logging (HTTP requests, retries, timing).
   * - ``--help``
     - Show the help message for any command or sub-command.

These options work before or after the sub-command name. For example, both
``xnatctl --output json project list`` and ``xnatctl project list --output json``
are valid.

Command Summary
---------------

The table below lists every command family with a one-line description.

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Command
     - Description
   * - ``config``
     - Manage configuration profiles and switch environments
   * - ``auth``
     - Authenticate and manage session tokens
   * - ``project``
     - List, inspect, and create projects
   * - ``subject``
     - Manage subjects within a project
   * - ``session``
     - List, inspect, download, and upload imaging sessions
   * - ``scan``
     - Manage individual scans within a session
   * - ``resource``
     - Manage file resources attached to sessions or scans
   * - ``prearchive``
     - Manage the prearchive staging area
   * - ``pipeline``
     - Run and monitor processing pipelines
   * - ``admin``
     - Administrative operations (catalogs, users, audit)
   * - ``api``
     - Raw REST API access (escape hatch)
   * - ``dicom``
     - DICOM validation and inspection utilities

config
------

Use ``config`` to create and manage connection profiles. Each profile stores a server
URL, default project, SSL settings, and timeout. Switch between profiles to target
different XNAT environments without re-entering connection details.

- ``config init`` -- Create a configuration file with an initial profile
- ``config add-profile`` -- Add a named profile to an existing configuration
- ``config use-context`` -- Switch the active (default) profile
- ``config show`` -- Display the current configuration and all profiles
- ``config current-context`` -- Print the name of the active profile
- ``config remove-profile`` -- Remove a named profile

Set up xnatctl for the first time with your server URL:

.. code-block:: console

   $ xnatctl config init --url https://xnat.example.org

Add a development profile with SSL verification disabled:

.. code-block:: console

   $ xnatctl config add-profile dev --url https://xnat-dev.example.org --no-verify-ssl

Switch the active profile and review settings:

.. code-block:: console

   $ xnatctl config use-context dev
   $ xnatctl config show

.. tip::

   Set ``default_project`` when creating a profile (``--project MYPROJ``) to avoid
   passing ``-P`` on every command. See :doc:`configuration` for full details.

auth
----

The ``auth`` commands handle authentication against an XNAT server. xnatctl caches
a session token locally so you do not re-authenticate on every command. Tokens expire
after 15 minutes of inactivity; xnatctl re-authenticates automatically.

- ``auth login`` -- Authenticate and cache a session token
- ``auth logout`` -- Invalidate the session on the server and clear the local cache
- ``auth status`` -- Show the current authentication state (cached session, env vars)
- ``auth test`` -- Test connectivity by making a live request to the server

Log in interactively (credentials are prompted if not provided):

.. code-block:: console

   $ xnatctl auth login
   $ xnatctl auth status

.. note::

   Credentials resolve in priority order: CLI arguments > environment variables
   (``XNAT_USER``, ``XNAT_PASS``) > profile configuration > interactive prompt.
   Set ``XNAT_TOKEN`` to skip credential-based authentication entirely.

xnatctl also provides two top-level utility commands:

- ``whoami`` -- Show the authenticated user, server, profile, and auth mode
- ``health ping`` -- Check server connectivity, version, and latency

.. code-block:: console

   $ xnatctl whoami
   $ xnatctl health ping

project
-------

The ``project`` commands let you list accessible projects, inspect details (including
subject and session counts), and create new projects. These are typically the first
commands you run after authenticating.

- ``project list`` -- List all projects you have access to
- ``project show`` -- Display detailed information about a specific project
- ``project create`` -- Create a new project on the server

List projects as a table or JSON, inspect one, or create a new project:

.. code-block:: console

   $ xnatctl project list
   $ xnatctl project list --output json
   $ xnatctl project show MYPROJECT
   $ xnatctl project create NEWPROJ --name "New Project" --pi Smith

.. tip::

   Use ``-q`` with ``project list`` to get a plain list of project IDs for scripting:
   ``xnatctl project list -q | head -5``.

subject
-------

The ``subject`` commands manage subjects (participants) within a project. You can
list, inspect, delete, and rename subjects. The rename sub-command supports mapping
files, regex-based pattern matching, and per-project patterns files for bulk label
normalization.

- ``subject list`` -- List subjects in a project (includes session counts)
- ``subject show`` -- Display subject details and associated sessions
- ``subject delete`` -- Delete a subject and all its sessions (requires confirmation)
- ``subject rename`` -- Rename subjects using a mapping file, regex, or patterns file

List and inspect subjects:

.. code-block:: console

   $ xnatctl subject list -P MYPROJ
   $ xnatctl subject show SUB001 -P MYPROJ

Delete with a dry-run preview, then confirm:

.. code-block:: console

   $ xnatctl subject delete SUB001 -P MYPROJ --dry-run
   $ xnatctl subject delete SUB001 -P MYPROJ --yes

Rename subjects using a patterns file (first matching rule wins):

.. code-block:: console

   $ xnatctl subject rename -P MYPROJ --patterns-file patterns.json --dry-run

.. note::

   The ``rename`` command supports merging: if the target label already exists,
   xnatctl moves experiments from the source into the target rather than failing.

session
-------

The ``session`` commands let you list, inspect, download, and upload imaging sessions
(experiments). Sessions are the primary data containers in XNAT, holding scans and
resources. This is the command family you will use most for day-to-day data management.

- ``session list`` -- List sessions, optionally filtered by subject or modality
- ``session show`` -- Display session details including scans and resources
- ``session download`` -- Download session data (scans and optional resources)
- ``session upload`` -- Upload DICOM data via REST import
- ``session upload-exam`` -- Upload a scanner exam-root directory (DICOM + top-level resources)
- ``session upload-dicom`` -- Upload DICOM files via C-STORE network protocol

**Parent-resource options.** The ``-E`` and ``-P`` flags identify experiments:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Option
     - Description
   * - ``-E`` / ``--experiment``
     - Experiment ID (accession number) or label. Required for ``show``, ``download``.
       Labels require ``-P`` so xnatctl can resolve them within a project.
   * - ``-P`` / ``--project``
     - Project ID. Enables label lookup with ``-E``. Falls back to profile
       ``default_project``.

.. tip::

   If your profile has ``default_project`` set, you can pass ``-E`` with a session
   label without explicitly providing ``-P``. See :doc:`configuration` for details.

List MR sessions for a subject:

.. code-block:: console

   $ xnatctl session list -P MYPROJ --subject SUB001 --modality MR

Show session details by accession number or by label with project context:

.. code-block:: console

   $ xnatctl session show -E XNAT_E00001
   $ xnatctl session show -E SESSION_LABEL -P MYPROJ

Download a session with parallel workers:

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --workers 8

Upload DICOM files via REST (batch or gradual per-file):

.. code-block:: console

   $ xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001
   $ xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001 --gradual --workers 40

Upload a scanner exam-root directory (DICOM + top-level resources):

.. code-block:: console

   $ xnatctl session upload-exam ./exam_root -P MYPROJ -S SUB001 -E SESS001

Upload via DICOM C-STORE (requires the ``[dicom]`` extra):

.. code-block:: console

   $ xnatctl session upload-dicom ./dicoms --host xnat.example.org --called-aet XNAT

.. note::

   Use ``--dry-run`` on download and upload commands to preview what would happen
   without transferring data.

For detailed transfer workflows, see :doc:`downloading` and :doc:`uploading`.

scan
----

The ``scan`` commands manage individual scans within a session. These commands follow
the same ``-E`` / ``-P`` convention as session commands for identifying the parent
experiment.

- ``scan list`` -- List scans with type, series description, and quality
- ``scan show`` -- Display scan details and attached resources
- ``scan delete`` -- Delete one or more scans (supports parallel deletion)
- ``scan download`` -- Download specific scans by ID

List scans and show details:

.. code-block:: console

   $ xnatctl scan list -E XNAT_E00001
   $ xnatctl scan show -E SESSION_LABEL 1 -P MYPROJ

Delete scans with dry-run preview:

.. code-block:: console

   $ xnatctl scan delete -E XNAT_E00001 --scans 1,2,3 --dry-run

Download specific scans or all scans at once:

.. code-block:: console

   $ xnatctl scan download -E XNAT_E00001 -s 1,2 --out ./data --unzip
   $ xnatctl scan download -E XNAT_E00001 -s '*' --out ./data

.. tip::

   Use ``--resource`` with ``scan download`` to download only a specific resource
   type (e.g., ``DICOM`` or ``NIFTI``).

For more download patterns, see :doc:`downloading`.

resource
--------

The ``resource`` commands manage file collections attached to sessions or scans.
Resources are labeled containers (e.g., ``DICOM``, ``NIFTI``, ``BIDS``) that hold
files.

- ``resource list`` -- List resources at session or scan level
- ``resource show`` -- Display resource details and file listing
- ``resource upload`` -- Upload a file or directory to a resource
- ``resource download`` -- Download a resource as a ZIP archive

List resources at session and scan level:

.. code-block:: console

   $ xnatctl resource list XNAT_E00001
   $ xnatctl resource list XNAT_E00001 --scan 1

Upload files and download resources:

.. code-block:: console

   $ xnatctl resource upload XNAT_E00001 NIFTI ./file.nii.gz
   $ xnatctl resource upload XNAT_E00001 DICOM ./dicoms --scan 1
   $ xnatctl resource download XNAT_E00001 DICOM --file ./dicom.zip
   $ xnatctl resource download XNAT_E00001 DICOM --file ./scan1.zip --scan 1

.. note::

   When uploading a directory, xnatctl archives it and extracts it server-side. The
   resource label is created automatically if it does not already exist.

prearchive
----------

The prearchive is a staging area where XNAT holds uploaded data before formal
archiving. Data lands here via DICOM C-STORE or REST import with the prearchive flag.
You review sessions and either archive them into the project hierarchy or delete them.
For more on this concept, see :doc:`concepts`.

- ``prearchive list`` -- List sessions in the prearchive
- ``prearchive archive`` -- Move a session into the permanent archive
- ``prearchive delete`` -- Permanently delete a prearchive session
- ``prearchive rebuild`` -- Rebuild/refresh a session (re-parses headers)
- ``prearchive move`` -- Move a session to a different project

List and archive prearchive sessions:

.. code-block:: console

   $ xnatctl prearchive list --project MYPROJ
   $ xnatctl prearchive archive MYPROJ 20240115_120000 Session1
   $ xnatctl prearchive archive MYPROJ 20240115_120000 Session1 --subject SUB001

Move a session to another project:

.. code-block:: console

   $ xnatctl prearchive move MYPROJ 20240115_120000 Session1 OTHERPROJ

.. tip::

   The three positional arguments (``PROJECT``, ``TIMESTAMP``, ``SESSION_NAME``)
   uniquely identify a prearchive entry. Find them with ``prearchive list``.

pipeline
--------

The ``pipeline`` commands discover, launch, monitor, and cancel processing pipelines
on the XNAT server. Pipelines are server-side workflows (e.g., dcm2niix, FreeSurfer)
that operate on experiments.

- ``pipeline list`` -- List available pipelines
- ``pipeline run`` -- Launch a pipeline on an experiment
- ``pipeline status`` -- Check a job's status
- ``pipeline jobs`` -- List pipeline jobs with optional filters
- ``pipeline cancel`` -- Cancel a running job

List pipelines and run one with parameters:

.. code-block:: console

   $ xnatctl pipeline list --project MYPROJ
   $ xnatctl pipeline run dcm2niix --experiment XNAT_E00001 --wait
   $ xnatctl pipeline run myproc -e XNAT_E00001 -P param1=value1

Check status or watch a job until completion:

.. code-block:: console

   $ xnatctl pipeline status JOB123
   $ xnatctl pipeline status JOB123 --watch
   $ xnatctl pipeline jobs --status Running
   $ xnatctl pipeline cancel JOB123 --yes

admin
-----

The ``admin`` commands provide server administration operations. These typically
require elevated privileges on the XNAT server.

- ``admin refresh-catalogs`` -- Refresh catalog XMLs for experiments in a project
- ``admin user add-to-groups`` -- Add a user to one or more XNAT groups
- ``admin audit`` -- View the audit log (depends on server configuration)

Refresh catalogs with checksum generation and stale entry cleanup:

.. code-block:: console

   $ xnatctl admin refresh-catalogs MYPROJ --option checksum --option delete
   $ xnatctl admin refresh-catalogs MYPROJ --experiment XNAT_E00001

Add a user to project groups and view audit entries:

.. code-block:: console

   $ xnatctl admin user add-to-groups jsmith --projects PROJ1,PROJ2 --role member
   $ xnatctl admin audit --project MYPROJ --limit 20

.. note::

   Catalog refresh runs in parallel by default. Use ``--no-parallel`` for sequential
   execution or ``--workers N`` to control concurrency.

api
---

The ``api`` commands provide a raw REST escape hatch for XNAT endpoints not covered
by dedicated commands. This is useful for one-off queries, automation, or accessing
newer API endpoints that xnatctl does not yet wrap.

- ``api get`` -- GET request to any endpoint
- ``api post`` -- POST with optional JSON body or file payload
- ``api put`` -- PUT with optional JSON body or file payload
- ``api delete`` -- DELETE (requires confirmation or ``--yes``)

Query, create, update, and delete resources directly:

.. code-block:: console

   $ xnatctl api get /data/projects
   $ xnatctl api get /data/projects/MYPROJ/subjects --params columns=ID,label -o json
   $ xnatctl api post /data/projects --data '{"ID": "NEWPROJ"}'
   $ xnatctl api put /data/projects/MYPROJ --data '{"description": "Updated"}'
   $ xnatctl api delete /data/projects/MYPROJ/subjects/SUB001 --yes

.. tip::

   ``api get`` automatically formats XNAT ``ResultSet`` responses as tables in table
   output mode, so you get readable output without extra processing.

dicom
-----

The ``dicom`` commands provide local DICOM file utilities that do not require an XNAT
connection. They require the optional ``[dicom]`` extra (``pip install xnatctl[dicom]``).

- ``dicom validate`` -- Validate files for required tags and structural integrity
- ``dicom inspect`` -- Inspect DICOM headers for a single file
- ``dicom list-tags`` -- List all tags present in a file
- ``dicom anonymize`` -- Remove or replace identifying tags

Install and use DICOM utilities:

.. code-block:: console

   $ pip install "xnatctl[dicom]"
   $ xnatctl dicom validate /path/to/dicoms -r
   $ xnatctl dicom inspect /path/to/file.dcm
   $ xnatctl dicom inspect /path/to/file.dcm --tag PatientID --tag Modality
   $ xnatctl dicom anonymize /input/dir /output/dir -r --patient-id ANON001

.. note::

   These commands are independent of the XNAT server. Use them to pre-validate or
   anonymize files before uploading with ``session upload`` or ``session upload-dicom``.

Getting Help
------------

You can get help for any command by appending ``--help``:

.. code-block:: console

   $ xnatctl --help
   $ xnatctl session --help
   $ xnatctl session download --help
