CLI Reference
=============

This is the complete command reference for xnatctl. Every command family, sub-command,
and option is documented here. If you are new to xnatctl, start with the
:doc:`quickstart` guide for a hands-on introduction.

Common Command Options
----------------------

Most resource-oriented commands (for example ``project``, ``subject``,
``session``, ``scan``, ``resource``, ``prearchive``, ``pipeline``, ``admin``,
and ``api``) accept these common options:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Option
     - Description
   * - ``--profile TEXT`` / ``-p``
     - Select a configuration profile for this command. Overrides the default profile
       and the ``XNAT_PROFILE`` environment variable.
   * - ``--output TEXT`` / ``-o``
     - Output format: ``json``, ``table``, or ``tsv`` (default: ``table``). JSON is
       useful for scripting and piping into tools like ``jq``; TSV is plain
       tab-separated lines (a header line of column keys, then one record per line,
       never colored) for ``awk``/``cut`` pipelines. **Not to be confused with** the
       ``--out`` flag on download commands, which specifies a filesystem destination
       path.
   * - ``--no-headers``
     - Omit the header line/row from ``table`` and ``tsv`` output. Ignored by
       ``json`` and ``--quiet``, which have no header.
   * - ``--quiet`` / ``-q``
     - Suppress non-essential output; print only IDs.
   * - ``--verbose`` / ``-v``
     - Enable verbose/debug logging (HTTP requests, retries, timing).
   * - ``--help``
     - Show the help message for any command or sub-command.

These options are command-level options and should be passed after the
sub-command path. For example:

.. code-block:: console

   $ xnatctl project list --output json

.. note::

   Top-level utility groups such as ``config``, ``auth``, ``completion``,
   ``local``, ``health``, and ``dicom`` expose their own options and do not
   universally support ``--quiet`` or ``--verbose``.

Exit Codes
----------

Failures exit with a differentiated code, so scripts can branch on *why* a
command failed rather than parsing stderr. Codes only ever became more
specific than the old blanket ``1``, so existing ``!= 0`` checks keep working.

.. list-table::
   :header-rows: 1
   :widths: 10 25 65

   * - Code
     - Meaning
     - Typical cause
   * - 0
     - Success
     -
   * - 1
     - General error
     - Server-side errors, upload/download failures, anything unclassified
   * - 2
     - Usage error
     - Reserved by Click: wrong flags or arguments (including a refused argv
       password)
   * - 3
     - Authentication error
     - Bad credentials, expired session that could not be refreshed
   * - 4
     - Network error
     - Server unreachable, timeout, retries exhausted
   * - 5
     - Not found
     - Project/subject/session/resource does not exist
   * - 6
     - Permission denied
     - Authenticated, but the account lacks the required role
   * - 7
     - User cancelled
     - A confirmation prompt was declined or interrupted

Failed uploads and downloads exit nonzero under ``-o json`` too -- machine
output does not swallow the failure signal.

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
   * - ``command``
     - Inspect Container Service command registrations
   * - ``wrapper``
     - Inspect Container Service command wrappers and their configuration
   * - ``container``
     - List, inspect, launch, kill, and read logs for Container Service container runs
   * - ``anon``
     - Manage site-wide and per-project DicomEdit anonymization scripts
   * - ``scp``
     - Manage DICOM SCP receivers (AE title/port)
   * - ``search``
     - List, show, run, and delete XNAT saved (stored) searches
   * - ``admin``
     - Administrative operations (catalogs, users, audit, docker daemon)
   * - ``api``
     - Raw REST API access (escape hatch)
   * - ``local``
     - Offline operations on downloaded data (extract ZIPs)
   * - ``dicom``
     - DICOM validation and inspection utilities
   * - ``xsync``
     - Manage XSync (cross-site federation) projects and credentials
   * - ``whoami``
     - Show current user and authentication context
   * - ``health``
     - Server connectivity and version checks
   * - ``completion``
     - Generate shell completion scripts (bash/zsh/fish)
   * - ``upgrade``
     - Detect the install method and update xnatctl in place

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
- ``config set-password`` -- Store a profile's password in the OS keychain
  (prompts for it)

Set up xnatctl for the first time with your server URL -- after writing the
profile it offers to log in right away (``--login``/``--no-login`` decide up
front in scripts):

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

Log in interactively (credentials are prompted if not provided), or pipe the
password in for scripts:

.. code-block:: console

   $ xnatctl auth login
   $ echo "$PASS" | xnatctl auth login -u admin --password-stdin
   $ xnatctl auth status

.. note::

   Credentials resolve in priority order: CLI arguments > environment variables
   (``XNAT_USER``, ``XNAT_PASS``) > profile configuration (inline or OS
   keychain) > interactive prompt. Set ``XNAT_TOKEN`` to skip credential-based
   authentication entirely. A password *value* on argv
   (``--password <secret>``) is refused -- it would be visible in ``ps`` and
   shell history; use ``--password-stdin`` instead.

xnatctl also provides three top-level utility commands:

- ``whoami`` -- Show the authenticated user, server, profile, and auth mode
- ``health ping`` -- Check server connectivity, version, and latency
- ``upgrade`` -- Detect the install method (standalone binary, pipx, pip/uv,
  Docker) and update xnatctl in place; dry by default, ``--yes`` to run it,
  ``--check`` to look up the latest version without upgrading

.. code-block:: console

   $ xnatctl whoami
   $ xnatctl health ping
   $ xnatctl upgrade
   $ xnatctl upgrade --yes
   $ xnatctl upgrade --check

project
-------

The ``project`` commands let you list accessible projects, inspect details (including
subject and session counts), and create new projects. These are typically the first
commands you run after authenticating.

- ``project list`` -- List all projects you have access to
- ``project show`` -- Display detailed information about a specific project
- ``project create`` -- Create a new project on the server
- ``project users`` -- List a project's users and roles
- ``project grant`` -- Grant a user a role (owner/member/collaborator) on a project
- ``project revoke`` -- Revoke a user's access to a project
- ``project access`` -- Get, or ``--set``, a project's accessibility level
- ``project requests`` -- List a project's access requests, pending and resolved
- ``project transfer`` -- Transfer project data to another XNAT instance
- ``project transfer-check`` -- Pre-flight connectivity and permissions check
- ``project transfer-status`` -- Show status of the last transfer run
- ``project transfer-history`` -- Show full transfer history for a project
- ``project transfer-init`` -- Generate a starter transfer configuration YAML

List projects as a table or JSON, inspect one, or create a new project:

.. code-block:: console

   $ xnatctl project list
   $ xnatctl project list --output json
   $ xnatctl project show MYPROJECT
   $ xnatctl project create NEWPROJ --name "New Project" --pi Smith

Transfer a project to another XNAT server:

.. code-block:: console

   $ xnatctl project transfer-check -P SRC --dest-profile staging --dest-project DST
   $ xnatctl project transfer -P SRC --dest-profile staging --dest-project DST --dry-run
   $ xnatctl project transfer -P SRC --dest-profile staging --dest-project DST --yes

Manage project membership and accessibility:

.. code-block:: console

   $ xnatctl project users MYPROJ
   $ xnatctl project grant MYPROJ jsmith --role member --yes
   $ xnatctl project revoke MYPROJ jsmith --yes
   $ xnatctl project access MYPROJ
   $ xnatctl project access MYPROJ --set protected --yes

List a project's access requests -- both pending and already-resolved, with
the ``approved`` column showing state:

.. code-block:: console

   $ xnatctl project requests MYPROJ
   $ xnatctl project requests MYPROJ -o json

.. note::

   There is no ``--approve``/``--deny`` here. A project access request is an
   invitation, and XNAT resolves it against whichever account is signed in
   when the resolution call is made -- not the account the request was
   actually addressed to. An admin cannot safely accept or decline a
   request on another user's behalf; the invited user has to log in and
   respond to it themselves.

.. tip::

   Use ``-q`` with ``project list`` to get a plain list of project IDs for scripting:
   ``xnatctl project list -q | head -5``.

For detailed transfer documentation, configuration, and filtering options, see
:doc:`transferring`.

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
- ``subject share`` -- Share a subject into another project without moving it
- ``subject unshare`` -- Remove a subject's share from another project
- ``subject vars`` -- List a subject's custom variables
- ``subject vars set`` -- Set one or more custom variables on a subject

List and inspect subjects:

.. code-block:: console

   $ xnatctl subject list -P MYPROJ
   $ xnatctl subject show SUB001 -P MYPROJ

Delete with a dry-run preview, then confirm:

.. code-block:: console

   $ xnatctl subject delete SUB001 -P MYPROJ --dry-run
   $ xnatctl subject delete SUB001 -P MYPROJ --yes

Delete a batch of subjects piped in from ``subject list -q`` (``--batch -``
requires ``--yes`` or ``--dry-run``, since stdin is consumed by the batch list):

.. code-block:: console

   $ xnatctl subject list -P MYPROJ -q | xnatctl subject delete -P MYPROJ --batch - --yes

Share a subject into a second project without moving it. The subject stays
owned by its primary project; the target project gains access to it, and may
see it under a different label:

.. code-block:: console

   $ xnatctl subject share SUB001 -P MYPROJ --into OTHERPROJ --yes
   $ xnatctl subject share SUB001 -P MYPROJ --into OTHERPROJ --label ALIAS01 --yes
   $ xnatctl subject unshare SUB001 -P MYPROJ --from OTHERPROJ --yes

``subject show`` lists the projects a subject is shared into, so there is no
separate "list shares" verb.

Read and write a subject's custom variables (the project-defined fields XNAT
calls custom variables; ``xnat-varput`` covers the same ground):

.. code-block:: console

   $ xnatctl subject vars SUB001 -P MYPROJ
   $ xnatctl subject vars set SUB001 -P MYPROJ group=control timepoint=baseline --yes

Multiple ``KEY=VALUE`` pairs in one ``vars set`` are written in a single
request, so they land together or not at all.

Rename subjects using a patterns file (first matching rule wins):

.. code-block:: console

   $ xnatctl subject rename -P MYPROJ --patterns-file patterns.json --dry-run

``scripts/example_patterns.json`` in the repository is a working example of
the format: a top-level ``patterns`` list, each rule carrying ``project``,
a ``match`` regex, and a ``to`` template where ``{project}`` and ``{1}``,
``{2}``, ... expand to the project ID and the regex's capture groups.

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
- ``session share`` -- Share a session into another project without moving it
- ``session unshare`` -- Remove a session's share from another project
- ``session vars`` -- List a session's custom variables
- ``session vars set`` -- Set one or more custom variables on a session
- ``session normalize-labels`` -- Rename a project's experiment labels to
  the standardized ``{SUBJECT}_{VISIT:02d}_SE{SESSION:02d}_{MODALITY}`` form

Sharing and custom variables work the same way as their ``subject``
equivalents (documented above), addressing the session with ``-E``:

.. code-block:: console

   $ xnatctl session share -E XNAT_E00001 -P MYPROJ --into OTHERPROJ --yes
   $ xnatctl session unshare -E XNAT_E00001 -P MYPROJ --from OTHERPROJ --yes
   $ xnatctl session vars -E XNAT_E00001 -P MYPROJ
   $ xnatctl session vars set -E XNAT_E00001 -P MYPROJ qc=pass --yes

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
   $ xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001 --mode gradual -w 40

Upload a scanner exam-root directory (DICOM + top-level resources):

.. code-block:: console

   $ xnatctl session upload-exam ./exam_root -P MYPROJ -S SUB001 -E SESS001

``session upload-exam`` attaches top-level resources after the DICOM import. By
default, it waits for the session to appear in the **permanent archive** before
attaching resources.

.. list-table:: Archive wait options (``session upload-exam``)
   :header-rows: 1
   :widths: 35 65

   * - Option
     - Description
   * - ``--wait SECONDS``
     - Seconds to wait for the session to appear in the archive (default:
       ``900``). Set to ``0`` to skip waiting.

Upload via DICOM C-STORE:

.. code-block:: console

   $ xnatctl session upload-dicom ./dicoms --host xnat.example.org --called-aet XNAT

.. warning::

   Plain C-STORE is **unencrypted**. Pixel data and the patient identifiers
   attached to it cross the network in cleartext, so anything that can see the
   traffic can read the PHI. Pass ``--tls`` when the SCP supports it:

   .. code-block:: console

      $ xnatctl session upload-dicom ./dicoms --host xnat.example.org \
          --called-aet XNAT --tls

   ``--tls-ca-bundle`` supplies a PEM file of CAs to trust instead of the
   system store, and ``--tls-cert`` / ``--tls-key`` provide a client
   certificate for SCPs that require mutual TLS. Each has an environment
   variable equivalent (``XNAT_DICOM_TLS``, ``XNAT_DICOM_TLS_CA_BUNDLE``,
   ``XNAT_DICOM_TLS_CERT``, ``XNAT_DICOM_TLS_KEY``).

   There is deliberately no option to skip certificate verification. Such a
   mode looks encrypted while accepting any certificate presented, which
   protects nothing and hides that fact. If a deployment cannot verify
   certificates, send plaintext knowingly -- the command says which transport
   it used on every run.

.. note::

   Use ``--dry-run`` on download and upload commands to preview what would happen
   without transferring data.

Normalize a project's experiment labels, previewing first:

.. code-block:: console

   $ xnatctl session normalize-labels -P MYPROJ --dry-run
   $ xnatctl session normalize-labels -P MYPROJ --yes

.. note::

   Experiments are grouped by subject and modality, ordered by session date
   (assigning ``VISIT``) and then by time of day within a date (assigning
   ``SESSION``); an experiment already at its target label is left alone,
   and two experiments that would compute the same target label are both
   refused rather than either one being renamed. For subject renames, use
   ``subject rename``.

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

Delete a batch of scans piped in from ``scan list -q`` (``--scans`` and
``--batch`` are mutually exclusive):

.. code-block:: console

   $ xnatctl scan list -E XNAT_E00001 -q | xnatctl scan delete -E XNAT_E00001 --batch - --yes

Download specific scans or all scans at once:

.. code-block:: console

   $ xnatctl scan download -E XNAT_E00001 --scans 1,2 --out ./data --extract
   $ xnatctl scan download -E XNAT_E00001 --scans '*' --out ./data
   $ xnatctl scan download -E XNAT_E00001 --scans 1 --scans 2 --out ./data

.. tip::

   Use ``-r`` / ``--resource`` with ``scan download`` to download a specific resource
   type (e.g., ``-r DICOM`` or ``-r NIFTI``).

For more download patterns, see :doc:`downloading`.

resource
--------

The ``resource`` commands manage file collections attached to projects,
subjects, sessions, or scans. Resources are labeled containers (e.g.,
``DICOM``, ``NIFTI``, ``BIDS``) that hold files.

- ``resource list`` -- List resources at project, subject, session, or scan level
- ``resource show`` -- Display resource details and file listing
- ``resource upload`` -- Upload a file or directory to a resource
- ``resource download`` -- Download a resource as a ZIP archive
- ``resource refresh`` -- Refresh a single resource catalog by archive URI

The scope is chosen by what you pass: ``-P`` alone targets the project,
``-P`` with ``-S`` the subject, a session ID the session, and ``--scan``
narrows to one scan:

.. code-block:: console

   $ xnatctl resource list -P MYPROJ
   $ xnatctl resource list -P MYPROJ -S SUB001
   $ xnatctl resource list XNAT_E00001
   $ xnatctl resource list XNAT_E00001 --scan 1

Upload and download work at every level too -- project-scope resources are
how shared templates or QC outputs are stored:

.. code-block:: console

   $ xnatctl resource upload XNAT_E00001 NIFTI ./file.nii.gz
   $ xnatctl resource upload XNAT_E00001 DICOM ./dicoms --scan 1
   $ xnatctl resource download XNAT_E00001 DICOM --output-file ./dicom.zip
   $ xnatctl resource download -P MYPROJ TEMPLATEFLOW --output-file ./tf.zip
   $ xnatctl resource download -P MYPROJ -S SUB001 QC --output-file ./qc.zip

.. note::

   When uploading a directory, xnatctl archives it and extracts it server-side. The
   resource label is created automatically if it does not already exist.

Refresh a resource's catalog XML directly by its archive URI (e.g. after files
were dropped onto disk outside of xnatctl):

.. code-block:: console

   $ xnatctl resource refresh /archive/projects/MYPROJ/subjects/SUB001/experiments/XNAT_E00001/scans/1/resources/DICOM --options checksum --options append

prearchive
----------

The prearchive is a staging area where XNAT holds uploaded data before formal
archiving. Data lands here via DICOM C-STORE or REST import with the prearchive flag.
You review sessions and either archive them into the project hierarchy or delete them.
For more on this concept, see :doc:`concepts`.

- ``prearchive list`` -- List sessions in the prearchive
- ``prearchive settings`` -- Get or set a project's prearchive routing mode
- ``prearchive archive`` -- Move a session into the permanent archive
- ``prearchive delete`` -- Permanently delete a prearchive session
- ``prearchive rebuild`` -- Rebuild/refresh a session (re-parses headers)
- ``prearchive move`` -- Move a session to a different project

List and archive prearchive sessions:

.. code-block:: console

   $ xnatctl prearchive list --project MYPROJ
   $ xnatctl prearchive archive MYPROJ 20240115_120000 Session1
   $ xnatctl prearchive archive MYPROJ 20240115_120000 Session1 --subject SUB001

Get or set a project's prearchive routing mode:

.. code-block:: console

   $ xnatctl prearchive settings -P MYPROJ
   $ xnatctl prearchive settings -P MYPROJ --set auto-archive --yes
   $ xnatctl prearchive settings -P MYPROJ --set manual --dry-run

Move a session to another project:

.. code-block:: console

   $ xnatctl prearchive move MYPROJ 20240115_120000 Session1 OTHERPROJ

.. tip::

   The three positional arguments (``PROJECT``, ``TIMESTAMP``, ``SESSION_NAME``)
   uniquely identify a prearchive entry. Find them with ``prearchive list``.

.. note::

   ``prearchive settings --set`` takes MODE as a readable name --
   ``manual``, ``auto-archive``, or ``auto-archive-overwrite`` -- never a
   raw integer. Verified live against XNAT 1.9.2.1: the server's own
   ``PUT`` route accepts and silently stores ANY integer with no
   validation at all, so a typo'd raw code would otherwise leave a project
   in an undefined routing state with a cheerful 200; ``click.Choice``
   rejects anything outside the three valid modes before a request is ever
   sent. A 403 setting a non-manual mode means the site property
   ``project.allow-auto-archive`` is disabled -- site policy, not a
   permissions problem -- and the error says so.

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
   $ xnatctl pipeline run myproc -E XNAT_E00001 --param param1=value1

Check status or watch a job until completion:

.. code-block:: console

   $ xnatctl pipeline status JOB123
   $ xnatctl pipeline status JOB123 --watch
   $ xnatctl pipeline jobs --status Running
   $ xnatctl pipeline cancel JOB123 --yes

command
-------

The ``command`` commands list, inspect, register, update, and delete
Container Service command registrations -- the ``command.json`` definitions
that tell XNAT which docker image to run and what a wrapper's inputs/outputs
are.

- ``command list`` -- List registered commands
- ``command show`` -- Show one command's full definition
- ``command create`` -- Register a new command from a FILE (or ``-`` for
  stdin)
- ``command update`` -- Replace a command's full definition from a FILE (or
  ``-``)
- ``command delete`` -- Delete a command

.. code-block:: console

   $ xnatctl command list
   $ xnatctl command show 12 -o json
   $ xnatctl command create command.json --yes
   $ xnatctl command update 12 command.json --dry-run
   $ xnatctl command delete 12 --yes

.. note::

   ``command update`` is a FULL REPLACE, not a merge: ``POST
   /xapi/commands/{id}`` is the only route Container Service 3.7.2 offers
   for this (there is no ``PUT``), and omitting ``xnat`` from FILE wipes
   every wrapper registered on the command -- verified live. To keep
   existing wrappers, include the command's current ``xnat`` array (from
   ``command show COMMAND_ID -o json``) in FILE. ``--dry-run`` on ``command
   create``/``command update`` prints a unified diff against the server's
   current state rather than a generic preview line.

wrapper
-------

The ``wrapper`` commands list a command's XNAT wrappers (the
context-specific launch points a command exposes -- typically one per
scan/session/project context it can run against, embedded under the
command's ``xnat`` key), read or write a wrapper's site- or project-scoped
configuration, and enable/disable a wrapper. There is no server-side
wrapper-listing endpoint, so ``wrapper list`` derives its output from
``command list``'s data client-side.

- ``wrapper list`` -- List wrappers, optionally scoped with ``--command``
- ``wrapper config get`` -- Read a wrapper's configuration by numeric ID or
  name (site-wide, or project-scoped with ``-P``)
- ``wrapper config set`` -- Replace a wrapper's configuration from a FILE
  (or ``-``), given its command and wrapper ID (site-wide, or
  project-scoped with ``-P``)
- ``wrapper enable`` / ``wrapper disable`` -- Enable or disable a wrapper,
  given its command and wrapper ID (site-wide, or project-scoped with
  ``-P``)

.. code-block:: console

   $ xnatctl wrapper list --command 12
   $ xnatctl wrapper config get 34
   $ xnatctl wrapper config get dcm2niix-scan
   $ xnatctl wrapper config get 34 -P MYPROJ
   $ xnatctl wrapper config set 12 34 config.json --yes
   $ xnatctl wrapper enable 12 34 --yes
   $ xnatctl wrapper disable 12 34 -P MYPROJ --yes

A wrapper name that matches wrappers on more than one command is rejected as
ambiguous (names are unique only within a command) -- pass the numeric ID
instead. This applies to ``wrapper config get``; ``wrapper config
set``/``enable``/``disable`` take the command ID and a numeric wrapper ID
directly, since the underlying routes need both.

.. note::

   ``PUT .../enabled`` and ``PUT .../disabled`` are two SEPARATE routes in
   Container Service 3.7.2, not one route with a boolean query parameter --
   verified live. ``wrapper config set`` does not validate the wrapper ID
   against the server (the route accepts a POST to a nonexistent wrapper ID
   without error); the existence check that turns an unknown command/wrapper
   pair into a clean error is entirely client-side.

container
---------

The ``container`` commands list, inspect, launch, and kill Container
Service container runs (one per wrapper launch), and read their captured
log output. A container ID may be the numeric database ID or the Docker
container ID string -- both are accepted everywhere one is expected.

- ``container list`` -- List containers, scoped to ``-P`` (or the profile's
  ``default_project``) when known, filterable with ``--status``; falls back
  to a site-wide listing otherwise
- ``container show`` -- Show one container's full record
- ``container logs`` -- Stream a container's captured stdout (or
  ``--stderr``) to stdout, byte-for-byte; ``--follow`` keeps polling for
  new output
- ``container launch`` -- Launch a container from a command wrapper
  (``WRAPPER`` is a numeric wrapper ID or a wrapper name, resolved the
  same way as ``wrapper`` commands); no ``--yes`` confirmation, matching
  ``pipeline run``'s precedent -- this is the group's normal-use verb
- ``container kill`` -- Kill a running container (destructive: ``--yes``/
  ``--dry-run``)

.. code-block:: console

   $ xnatctl container list -P MYPROJ --status Running
   $ xnatctl container show 501
   $ xnatctl container logs 501 --stderr
   $ xnatctl container logs 501 --follow
   $ xnatctl container launch dcm2niix-scan -P MYPROJ -E XNAT_E00001
   $ xnatctl container launch 34 -P MYPROJ --param greeting=hello --wait
   $ xnatctl container kill 501 --yes

.. note::

   ``container list``'s "Started" column is derived, not read from a single
   field: the Container Service DTO has no ``start-time``, so the value is
   the earliest ``history`` entry's ``time-recorded``, falling back to the
   container's ``status-time`` (its *current* status timestamp, which is not
   the same thing) and then to a ``-`` placeholder. What has not been
   exercised end to end is a container that actually ran -- that needs a
   Docker daemon, which this project's test stack deliberately does not
   provide.

.. note::

   The launch route does not validate the wrapper, project scope, or
   required inputs before queueing the launch -- verified live, it answers
   "success" for a wrapper ID that does not exist. ``container launch``
   resolves and validates ``WRAPPER`` client-side first (via the same
   ``CommandService.resolve_wrapper`` that backs ``wrapper`` commands) so a
   typo'd wrapper fails loudly instead of silently queueing nothing.
   ``-E``/``--experiment`` only works when the wrapper has exactly one
   external input of type ``Session``; use ``--param <name>=<value>``
   directly for any other input, or when that's ambiguous.

.. note::

   ``container launch --wait`` and ``container logs --follow`` could not be
   exercised against a real, daemon-backed launch: this project's
   integration stack has no reachable Docker daemon, so no container here
   has ever run to a terminal status. Both are documented as best-effort,
   code-grounded mechanisms rather than verified ones -- see
   ``ContainerService.launch`` and ``cli/container.py``'s
   ``_wait_for_launch``/``_follow_logs`` docstrings for exactly what was and
   was not confirmed live.

.. note::

   The Container Service is a separate XNAT plugin; these commands only work
   against a server with it installed and configured (check with
   ``admin plugins show containers`` -- verified live against Container
   Service 3.7.2: the plugin is keyed and identified as ``containers``, not
   ``container-service``). ``admin docker images``,
   ``admin docker hubs``, and ``admin docker server`` (under ``admin``,
   documented below) inspect the underlying docker daemon connection
   rather than command/wrapper/container state.

anon
----

The ``anon`` commands manage DicomEdit anonymization scripts -- the script
that strips or rewrites DICOM header fields as data is archived. XNAT keeps
one site-wide script plus an optional per-project override; a project
override only takes effect while it is enabled, otherwise the project
inherits the site-wide script.

- ``anon show`` -- Show the site-wide script, or one project's override
  with ``-P``
- ``anon set`` -- Replace a script from a FILE (or ``-`` for stdin)
- ``anon enable`` / ``anon disable`` -- Enable or disable a script

.. code-block:: console

   $ xnatctl anon show
   $ xnatctl anon show -P MYPROJ
   $ xnatctl anon set script.dicomedit --yes
   $ xnatctl anon set script.dicomedit -P MYPROJ --yes
   $ xnatctl anon set script.dicomedit --dry-run
   $ xnatctl anon disable -P MYPROJ --yes
   $ xnatctl anon enable -P MYPROJ --yes

.. note::

   ``anon set --dry-run`` prints a unified diff against the script currently
   on the server rather than a generic preview line, the same pattern
   ``command update --dry-run`` uses.

.. note::

   A DISABLED project override and a project that never had one set both
   read back identically from ``GET /xapi/anonymize/projects/{project}``
   (empty, as if unset) -- verified live. ``anon show -P`` tells the two
   apart and warns accordingly; re-running ``anon enable -P`` on a project
   whose override is merely disabled reactivates the existing script rather
   than requiring ``anon set`` again.

scp
---

The ``scp`` commands manage XNAT's DICOM SCP receivers -- the AE
title/port listeners that accept incoming DICOM C-STORE traffic.

- ``scp list`` -- List registered receivers
- ``scp show`` -- Show one receiver's full definition
- ``scp create`` -- Register a new receiver
- ``scp delete`` -- Delete a receiver
- ``scp enable`` / ``scp disable`` -- Enable or disable a receiver

.. code-block:: console

   $ xnatctl scp list
   $ xnatctl scp show 1
   $ xnatctl scp create --ae-title MYSCP --port 8105 --yes
   $ xnatctl scp create --ae-title MYSCP --port 8105 --identifier dicomObjectIdentifier --yes
   $ xnatctl scp disable 2 --yes
   $ xnatctl scp delete 2 --yes

.. note::

   The server does not validate ``--port`` at all -- verified live, both
   ``0`` and a port already bound by another receiver are accepted
   silently. ``scp create`` validates the port range client-side
   (``xnatctl.core.validation.validate_port``) before sending anything.
   ``--identifier`` defaults to the site's only registered DICOM object
   identifier when there is exactly one; with more than one registered, it
   must be given explicitly.

search
------

The ``search`` commands manage XNAT saved (stored) searches -- searches
built and persisted server-side (typically from the web UI's search
builder) that can be listed, inspected, re-run for fresh results, and
deleted from the CLI.

- ``search list`` -- List saved searches
- ``search show`` -- Show a saved search's XML definition
- ``search run`` -- Execute a saved search and print its result rows
- ``search delete`` -- Delete a saved search

.. code-block:: console

   $ xnatctl search list
   $ xnatctl search show my_search
   $ xnatctl search run my_search
   $ xnatctl search run my_search --columns ID,label
   $ xnatctl search delete my_search --yes

.. note::

   ``search show`` has NO JSON representation on the server -- verified
   live against XNAT 1.9.2.1, ``GET /data/search/saved/{id}?format=json``
   answers a clean 404 for the SAME search that the default (and explicit
   ``?format=xml``) request answers 200 for. ``search show -o json`` wraps
   that XML text in a small JSON envelope; it does not mean the server
   itself answered JSON.

.. note::

   A saved search's result columns (``search run``) are entirely dynamic
   -- whatever fields the search was built with -- so there is no fixed
   default column list. Run once without ``--columns`` to see what a given
   search returns.

.. note::

   ``search delete`` is idempotent-succeeds -- verified live, ``DELETE``
   answers 200 even for an unknown ``SEARCH_ID`` -- so ``--dry-run`` has no
   existence preflight to run; there is nothing that would distinguish
   "already gone" from "about to be deleted".

event
-----

The ``event`` commands manage XNAT Event Service subscriptions -- rules
that trigger an action (e.g. logging, sending an email) when a matching
event occurs (a session created, a scan added, ...).

- ``event list`` -- List event subscriptions
- ``event show`` -- Show one subscription's full definition
- ``event create`` -- Register a new subscription from a definition file
- ``event delete`` -- Delete a subscription
- ``event enable`` / ``event disable`` -- Activate or deactivate a subscription
- ``event actions`` -- List available actions (valid ``action-key`` values)
- ``event types`` -- List available trigger types (valid
  ``event-filter.event-type`` values)

.. code-block:: console

   $ xnatctl event list
   $ xnatctl event actions
   $ xnatctl event types
   $ xnatctl event create subscription.json --yes
   $ xnatctl event show 5
   $ xnatctl event disable 5 --yes
   $ xnatctl event delete 5 --yes

A subscription definition file is a JSON object with kebab-case keys, e.g.:

.. code-block:: json

   {
     "name": "log-new-projects",
     "active": true,
     "event-filter": {
       "event-type": "org.nrg.xnat.eventservice.events.ProjectEvent",
       "status": "CREATED",
       "project-ids": []
     },
     "act-as-event-user": true,
     "action-key": "org.nrg.xnat.eventservice.actions.EventServiceLoggingAction:org.nrg.xnat.eventservice.actions.EventServiceLoggingAction",
     "attributes": {}
   }

.. note::

   The site can have the Event Service switched off entirely (a fresh
   install does) -- ``event list``/``show``/``actions``/``types`` all still
   work either way, but ``event create`` fails with "Event Service
   disabled." until an administrator turns it on. That toggle is not
   exposed by this command group.

.. note::

   The server does not enforce an action's declared required attributes
   (verified live: an Email Action subscription created with no
   ``to``/``subject``/``body`` in ``attributes`` succeeds anyway) -- check
   ``event actions`` for what each action expects before creating a
   subscription that depends on it.

admin
-----

The ``admin`` commands provide server administration operations. These typically
require elevated privileges on the XNAT server.

- ``admin refresh-catalogs`` -- Refresh catalog XMLs for experiments in a project
- ``admin user add`` -- Add a user to one or more XNAT groups
- ``admin user list`` -- List XNAT user accounts (``--active`` for signed-in only)
- ``admin user show`` -- Show details for one user
- ``admin user enable`` / ``admin user disable`` -- Enable or disable a user account
- ``admin user roles`` -- List, ``--grant``, or ``--revoke`` a user's site-wide roles
- ``admin user remove`` -- Remove a user from a project's groups
- ``admin user kill-sessions`` -- Terminate a user's active sessions
- ``admin user groups`` -- List the XNAT groups a user belongs to
- ``admin audit`` -- View the audit log (depends on server configuration)
- ``admin site-config get`` / ``admin site-config set`` -- Read or write site configuration
- ``admin plugins`` -- List installed plugins
- ``admin plugins show`` -- Show details for one installed plugin, by ID
- ``admin version`` -- Show XNAT server build/version information
- ``admin docker images`` -- List docker images known to the configured daemon
- ``admin docker hubs`` -- List configured docker hubs
- ``admin docker server`` -- Show the configured docker daemon connection, or
  set its host with ``--set-host`` (a plain read without ``--set-host``,
  never prompts)
- ``admin docker pull`` -- Pull a docker image from the default hub,
  optionally registering any commands embedded in it
  (``--save-commands``/``--no-save-commands``, default: save)

Refresh catalogs with checksum generation and stale entry cleanup:

.. code-block:: console

   $ xnatctl admin refresh-catalogs MYPROJ --option checksum --option delete
   $ xnatctl admin refresh-catalogs MYPROJ --experiment XNAT_E00001

Add a user to project groups and view audit entries:

.. code-block:: console

   $ xnatctl admin user add jsmith --projects PROJ1,PROJ2 --role member
   $ xnatctl admin audit --project MYPROJ --limit 20

Pull a docker image and point Container Service at a different daemon:

.. code-block:: console

   $ xnatctl admin docker pull xnat/dcm2niix:v1.2 --yes
   $ xnatctl admin docker server --set-host unix:///var/run/docker.sock --yes

.. note::

   ``admin docker images``, ``admin docker server``, and ``admin docker
   pull`` all require a Docker daemon reachable from the XNAT server. With
   none configured, XNAT answers with a raw Java exception in a 500 body;
   these commands recognize that specific signature and render it as an
   actionable message instead.

Manage a user's account and site-wide roles:

.. code-block:: console

   $ xnatctl admin user list --active
   $ xnatctl admin user disable jsmith --yes
   $ xnatctl admin user roles jsmith --grant Administrator --yes

.. tip::

   ``admin user kill-sessions`` is the fix for a shared/service account that
   has exhausted its concurrent-session limit and started failing every new
   login with 401s -- it clears the stale sessions and frees the slots back up.

Read and write site configuration, and check installed plugins and server version:

.. code-block:: console

   $ xnatctl admin site-config get siteId
   $ xnatctl admin site-config set siteId MyXNAT --yes
   $ xnatctl admin plugins
   $ xnatctl admin plugins show my-plugin-id
   $ xnatctl admin version -q

.. note::

   Catalog refresh runs in parallel by default (4 workers). Use ``--workers 1``
   for sequential execution or ``--workers N`` to control concurrency.

For detailed admin workflows, prerequisites, and workarounds for tasks not yet
exposed as CLI commands, see :doc:`administration`.

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
connection.

- ``dicom validate`` -- Validate files for required tags and structural integrity
- ``dicom inspect`` -- Inspect DICOM headers for a single file
- ``dicom list-tags`` -- List all tags present in a file
- ``dicom anonymize`` -- Remove or replace identifying tags
- ``dicom modify`` -- Modify DICOM tags in-place (``KEYWORD=VALUE`` pairs)

Examples:

.. code-block:: console

   $ xnatctl dicom validate /path/to/dicoms -r
   $ xnatctl dicom inspect /path/to/file.dcm
   $ xnatctl dicom inspect /path/to/file.dcm --tag PatientID --tag Modality
   $ xnatctl dicom anonymize /input/dir /output/dir -r --patient-id ANON001
   $ xnatctl dicom modify /path/to/file.dcm -t PatientID=ANON001
   $ xnatctl dicom modify ./dicoms -r -t PatientID=ANON --backup --dry-run

.. note::

   These commands are independent of the XNAT server. Use them to pre-validate or
   anonymize files before uploading with ``session upload`` or ``session upload-dicom``.

For detailed documentation on each DICOM command and workflow examples, see
:doc:`dicom`.

local
-----

The ``local`` commands perform offline operations on previously downloaded data.
They do not require an XNAT connection.

- ``local extract`` -- Extract ZIP files from downloaded sessions into organized
  directory structures

Extract downloaded session ZIPs:

.. code-block:: console

   $ xnatctl local extract ./data/XNAT_E00001
   $ xnatctl local extract ./data --recursive
   $ xnatctl local extract ./data --recursive --no-cleanup
   $ xnatctl local extract ./data --recursive --dry-run

.. tip::

   Use ``local extract`` when you downloaded sessions without ``--extract`` and
   want to extract them later. The ``--cleanup`` flag (on by default) removes ZIP
   files after successful extraction.

xsync
-----

The ``xsync`` commands manage XSync, XNAT's cross-site federation feature:
they read and drive the setup, status, history, and credentials records
XNAT itself maintains for a project bound to a remote XNAT, rather than
replacing them with a separate transfer mechanism. Contrast with
``project transfer`` (see :doc:`transferring`), which is xnatctl's own
independent, unidirectional data-migration pipeline.

.. note::

   XSync is an XNAT plugin, not part of the stock server. These commands
   only work against a server with the XSync plugin installed and configured.

- ``xsync list`` -- List XSync-bound projects on the local XNAT
- ``xsync setup`` -- Show the XSync setup record for a project
- ``xsync status`` -- Show the XSync status record for a project
- ``xsync history`` -- Show the XSync run history for a project
- ``xsync progress`` -- Stream the current XSync progress log (plain text)
- ``xsync sync`` -- Trigger an XSync run for a project
- ``xsync sync-subject`` -- Trigger an XSync run for a single subject/experiment
- ``xsync refresh-credentials`` -- Rotate the XSync remote credentials for a
  project (or every bound project sharing a remote URL, with ``--all``)

Inspect a bound project's setup, status, and history:

.. code-block:: console

   $ xnatctl xsync list
   $ xnatctl xsync setup -P MYPROJ
   $ xnatctl xsync status -P MYPROJ
   $ xnatctl xsync history -P MYPROJ
   $ xnatctl xsync progress -P MYPROJ

Trigger a sync for a whole project or a single subject/experiment:

.. code-block:: console

   $ xnatctl xsync sync -P MYPROJ
   $ xnatctl xsync sync-subject -E XNAT_E00001

Rotate the remote credentials XSync uses for a project, reading the remote
password from stdin. ``--yes`` is required here: without it, the
confirmation prompt reads the piped line first and the password is never
seen.

.. code-block:: console

   $ echo -n "$REMOTE_PASS" | xnatctl xsync refresh-credentials -P MYPROJ \
       --remote-url https://remote.example.org --remote-user alice \
       --remote-pass-stdin --yes

.. tip::

   Pass ``--all`` instead of ``-P`` to rotate every XSync-bound project that
   shares the same ``--remote-url`` in one call.

completion
----------

The ``completion`` commands generate shell auto-completion scripts for bash,
zsh, and fish. Once installed, pressing Tab completes command names, options,
and sub-commands.

- ``completion bash`` -- Generate bash completion script
- ``completion zsh`` -- Generate zsh completion script
- ``completion fish`` -- Generate fish completion script

Install completions for your shell:

.. code-block:: console

   # Bash
   $ xnatctl completion bash > ~/.local/share/bash-completion/completions/xnatctl

   # Zsh (add ~/.zfunc to fpath in .zshrc first)
   $ mkdir -p ~/.zfunc
   $ xnatctl completion zsh > ~/.zfunc/_xnatctl

   # Fish
   $ xnatctl completion fish > ~/.config/fish/completions/xnatctl.fish

After installing, restart your shell or source your shell config file for
completions to take effect.

Getting Help
------------

You can get help for any command by appending ``--help``:

.. code-block:: console

   $ xnatctl --help
   $ xnatctl session --help
   $ xnatctl session download --help
