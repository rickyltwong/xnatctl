====================
xnatctl Cheat Sheet
====================

.. note::

   This page is intended as a quick reference for xnatctl commands. When in doubt, defer to ``xnatctl --help`` (CLI is the source
   of truth).

Command hierarchy
=================

.. code-block:: text

   xnatctl
     config    init | show | use-context | current-context | add-profile | remove-profile
     auth      login | logout | status | test
     project   list | show | create
     subject   list | show | rename | delete
     session   list | show | download | upload | upload-exam
     scan      list | show | delete | download
     resource  list | show | upload | download
     prearchive list | archive | delete | rebuild | move
     pipeline  list | run | status | jobs | cancel
     admin     refresh-catalogs | user add | audit
     dicom     validate | inspect | list-tags | anonymize   (requires xnatctl[dicom])
     api       get | post | put | delete                    (raw REST escape hatch)
     whoami
     health ping
     completion [bash|zsh|fish]


Global options (all commands)
=============================

.. list-table::
   :header-rows: 1
   :widths: 20 10 70

   * - Flag
     - Short
     - Description
   * - ``--profile``
     - ``-p``
     - Named profile from config
   * - ``--output``
     - ``-o``
     - ``json`` or ``table`` (default: table)
   * - ``--quiet``
     - ``-q``
     - IDs only output
   * - ``--verbose``
     - ``-v``
     - Debug logging


Parent-resource options (session & scan commands)
=================================================

.. list-table::
   :header-rows: 1
   :widths: 25 10 35 30

   * - Flag
     - Short
     - Purpose
     - Key rule
   * - ``--project``
     - ``-P``
     - Project ID
     - Enables label lookup for ``-E``
   * - ``--subject``
     - ``-S``
     - Subject ID/label
     - Used in ``session list`` (filter) and ``session upload``
   * - ``--experiment``
     - ``-E``
     - Experiment ID or label
     - **Labels require -P** (explicit or via profile ``default_project``)

**Critical**: ``-E LABEL`` without ``-P`` fails. ``-E XNAT_E00001`` (accession ID) works without
``-P``.


Quick reference: common commands
================================

Setup & auth
------------

.. code-block:: console

   # Initialize config with profile
   xnatctl config init --url https://xnat.example.org --profile myserver

   # Add another profile
   xnatctl config add-profile prod --url https://prod.xnat.org --project DEFAULT_PROJ

   # Switch profiles
   xnatctl config use-context prod

   # Login (prompts for credentials if not in config/env)
   xnatctl auth login -p myserver

   # Test connection
   xnatctl auth test


Projects & subjects
-------------------

.. code-block:: console

   # List projects
   xnatctl project list

   # Show project details
   xnatctl project show MYPROJ

   # List subjects with filter (NOTE: colon syntax, not equals)
   xnatctl subject list -P MYPROJ --filter "label:CTRL_*"

   # Delete subject (with safety)
   xnatctl subject delete SUB001 -P MYPROJ --dry-run
   xnatctl subject delete SUB001 -P MYPROJ --yes


Sessions
--------

.. code-block:: console

   # List sessions in project
   xnatctl session list -P MYPROJ

   # Show session (by label - needs -P)
   xnatctl session show -P MYPROJ -E MR_Session_01

   # Show session (by accession ID - no -P needed)
   xnatctl session show -E XNAT_E00001

   # Download session - single ZIP (default)
   xnatctl session download -P MYPROJ -E MR_Session_01 --out ./data

   # Download session - parallel per-scan (workers > 1)
   xnatctl session download -P MYPROJ -E MR_Session_01 --out ./data -w 8


Uploads
-------

.. code-block:: console

   # Upload DICOM directory (parallel batches)
   xnatctl session upload ./dicoms -P NEURO -S SUB001 -E SESS001 --workers 4

   # Upload DICOM archive file
   xnatctl session upload ./archive.tar -P NEURO -S SUB001 -E SESS001

   # Gradual per-file upload (parallel)
   xnatctl session upload ./dicoms -P NEURO -S SUB001 -E SESS001 --gradual --workers 16

   # Upload exam root (DICOM + resources)
   # Directory structure: top-level dirs become resources, DICOMs found recursively
   xnatctl session upload-exam ./exam_root -P NEURO -S SUB001 -E SESS001 -w 4

   # Attach resources only (skip DICOM upload)
   xnatctl session upload-exam ./exam_root -P NEURO -S SUB001 -E SESS001 --attach-only

**upload vs upload-exam**: ``upload`` handles DICOM files only. ``upload-exam`` handles a mixed
directory (DICOMs + non-DICOM resource files like PDFs, spreadsheets). In upload-exam, top-level
directories become session-level resources by name.


Scans
-----

.. code-block:: console

   # List scans
   xnatctl scan list -E XNAT_E00001

   # Delete specific scans (comma-separated with -s flag)
   xnatctl scan delete -E XNAT_E00042 -P BRAIN -s 1,3,5 --dry-run
   xnatctl scan delete -E XNAT_E00042 -P BRAIN -s 1,3,5 --yes

   # Delete ALL scans
   xnatctl scan delete -E XNAT_E00042 -s "*" --yes

   # Download scans as ZIP
   xnatctl scan download -E XNAT_E00001 -s 1,2,3 --out ./scans


Resources
---------

.. code-block:: console

   # List resources on session
   xnatctl resource list XNAT_E00001

   # List resources on scan
   xnatctl resource list XNAT_E00001 --scan 1

   # Upload file/directory as resource
   xnatctl resource upload XNAT_E00001 MY_RESOURCE ./data/

   # Download resource
   xnatctl resource download XNAT_E00001 MY_RESOURCE --file ./output.zip


Prearchive
----------

**Note**: Prearchive commands use POSITIONAL args: ``PROJECT TIMESTAMP SESSION_NAME``

.. code-block:: console

   # List prearchive sessions
   xnatctl prearchive list
   xnatctl prearchive list --project MYPROJ

   # Archive (move to main archive)
   xnatctl prearchive archive MYPROJ 20240115_143022 SessionFolder

   # Delete from prearchive
   xnatctl prearchive delete MYPROJ 20240115_143022 SessionFolder --yes

   # Rebuild (refresh metadata)
   xnatctl prearchive rebuild MYPROJ 20240115_143022 SessionFolder

   # Move to different project
   xnatctl prearchive move MYPROJ 20240115_143022 SessionFolder TARGET_PROJ


Pipelines
---------

.. code-block:: console

   # List pipelines
   xnatctl pipeline list --project MYPROJ

   # Run pipeline and wait for completion
   xnatctl pipeline run dcm2niix -e XNAT_E00001 -P key1=val1 -P key2=val2 --wait

   # Check job status (with watch mode)
   xnatctl pipeline status JOB_ID --watch

   # Cancel job
   xnatctl pipeline cancel JOB_ID --yes


Admin
-----

.. code-block:: console

   # Refresh catalogs with parallel workers
   xnatctl admin refresh-catalogs MYPROJ -O checksum -O populateStats --parallel --workers 8

   # Add user to project groups
   xnatctl admin user add jsmith Owners --projects MYPROJ

   # View audit log
   xnatctl admin audit -P MYPROJ --since 7d --limit 50


Raw API (escape hatch)
----------------------

.. code-block:: console

   # GET with query parameters (use -P key=value, NOT query strings in path)
   xnatctl api get /data/projects/MYPROJ/subjects -P format=json

   # POST with data
   xnatctl api post /data/projects -d '{"ID":"NEW_PROJ","name":"New Project"}'

   # DELETE with confirmation skip
   xnatctl api delete /data/experiments/XNAT_E00001 --yes


DICOM tools (requires ``xnatctl[dicom]``)
-----------------------------------------

.. code-block:: console

   # Validate DICOM files
   xnatctl dicom validate ./dicoms -r

   # Inspect headers
   xnatctl dicom inspect ./scan.dcm -t PatientID -t Modality

   # Anonymize
   xnatctl dicom anonymize ./input ./output --patient-id ANON001 --remove-private -r


Configuration
=============

**Config file**: ``~/.config/xnatctl/config.yaml``

.. code-block:: yaml

   default_profile: default
   output_format: table
   profiles:
     default:
       url: https://xnat.example.org
       verify_ssl: true
       timeout: 21600       # 6 hours (default for large transfers)
       default_project: MYPROJ
       username: admin      # optional
       password: secret     # optional

**Environment variables** (priority: CLI args > env vars > profile > prompt):

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Purpose
   * - ``XNAT_TOKEN``
     - Session token (**highest auth priority**, skips credential prompt)
   * - ``XNAT_URL``
     - Server URL (auto-creates ``default`` profile)
   * - ``XNAT_USER``
     - Username
   * - ``XNAT_PASS``
     - Password
   * - ``XNAT_PROFILE``
     - Active profile
   * - ``XNAT_VERIFY_SSL``
     - ``true``/``false``
   * - ``XNAT_TIMEOUT``
     - Timeout seconds


Gotchas
=======

#. **``-o`` is output FORMAT** (``json``/``table``), NOT output directory. Use ``--out`` for
   download destination.
#. **Filter uses colon**: ``--filter "label:CTRL_*"`` not ``label=CTRL_*``.
#. **Scan IDs use ``-s`` flag**: ``-s 1,3,5`` (comma-separated) or ``-s "*"`` (all). NOT positional
   args.
#. **Prearchive uses positional args**: ``PROJECT TIMESTAMP SESSION_NAME``. NOT ``-P``/``-E`` flags.
#. **API params use ``-P key=value``**: NOT query strings appended to path.
#. **Workers flag varies**: session download uses ``-w``, upload uses ``--workers``. Both control
   parallelism.
#. **``-P`` flag is overloaded**: In session/scan commands, ``-P`` means ``--project``. In ``api``
   and ``pipeline`` commands, ``-P`` means parameter (``key=value``). Context matters.
#. **Default timeout is 6 hours** (21600s) for large DICOM transfers.
#. **upload-exam waits for archive**: By default waits for XNAT to finish archiving before
   attaching resources. Control with ``--wait-for-archive``/``--no-wait-for-archive``.
#. **``default_project`` fallback**: If profile has ``default_project``, ``-P`` can be omitted and
   session/scan commands auto-resolve.


Safety decorators
=================

Destructive commands include ``--yes/-y`` (skip confirmation) and ``--dry-run`` (preview only).
**Always use ``--dry-run`` first** for delete/rename operations.

Parallel commands include ``--parallel/--no-parallel`` and ``--workers N``.

