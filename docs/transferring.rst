Transferring Data Between XNAT Servers
======================================

xnatctl provides a built-in ``project transfer`` command that copies data
directly between two XNAT instances over the network. Unlike the manual
download-then-upload approach described in :doc:`workflows`, ``project transfer``
handles subject creation, experiment creation, per-scan DICOM import, non-DICOM
resource upload, state tracking, and post-transfer verification automatically.

This is the recommended approach for server-to-server migrations. For
single-session or ad hoc copies, see :doc:`downloading` and :doc:`uploading`.


Quick Start
-----------

Set up profiles for both XNAT servers:

.. code-block:: console

   $ xnatctl config add-profile prod --url https://xnat-prod.example.org
   $ xnatctl config add-profile staging --url https://xnat-staging.example.org
   $ xnatctl auth login -p prod
   $ xnatctl auth login -p staging

Run a pre-flight check:

.. code-block:: console

   $ xnatctl project transfer-check -p prod -P NEURO --dest-profile staging --dest-project NEURO_DEV

Preview the transfer with ``--dry-run``:

.. code-block:: console

   $ xnatctl project transfer -p prod -P NEURO --dest-profile staging --dest-project NEURO_DEV --dry-run

Execute the transfer:

.. code-block:: console

   $ xnatctl project transfer -p prod -P NEURO --dest-profile staging --dest-project NEURO_DEV --yes


How Transfer Works
------------------

The transfer pipeline processes data in a per-scan decomposition, which gives
fine-grained retry and verification at the scan level rather than downloading
entire sessions as monolithic ZIPs.

.. code-block:: text

   For each subject in the source project:
     1. Create subject on destination (if not exists)
     2. For each experiment:
        a. Create experiment on destination (if not exists)
        b. For each scan (in parallel):
           - Download DICOM ZIP from source
           - Validate ZIP integrity (size + structure)
           - Import via DICOM-zip handler on destination
           - Retry on failure (exponential backoff)
        c. For each non-DICOM scan resource (NII, BIDS, SNAPSHOTS, ...):
           - Download as ZIP, upload via REST PUT with extract
        d. For each session-level resource:
           - Download as ZIP, upload via REST PUT with extract
     3. Verify: compare scan sets and file counts between source and destination

**State tracking.** Transfer state is persisted in a local SQLite database
(``~/.config/xnatctl/transfer.db``). Subsequent runs skip already-transferred
subjects, making the command safe to re-run after interruptions.


Transfer Commands
-----------------

All transfer commands are sub-commands of ``project``.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Command
     - Description
   * - ``project transfer``
     - Transfer project data to another XNAT instance
   * - ``project transfer-check``
     - Pre-flight connectivity and permissions check
   * - ``project transfer-status``
     - Show status of the last transfer run
   * - ``project transfer-history``
     - Show full transfer history for a project
   * - ``project transfer-init``
     - Generate a starter transfer configuration YAML

Destination options
~~~~~~~~~~~~~~~~~~~

Transfer commands that connect to a destination XNAT accept these options:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Option
     - Description
   * - ``--dest-profile TEXT``
     - Use a named profile for the destination connection
   * - ``--dest-project TEXT``
     - Destination project ID (required)
   * - ``--dest-url TEXT``
     - Destination XNAT URL (inline, instead of a profile)
   * - ``--dest-user TEXT``
     - Destination username (inline)
   * - ``--dest-pass TEXT``
     - Destination password (inline)

You can either reference a pre-configured profile with ``--dest-profile`` or
provide connection details inline with ``--dest-url``, ``--dest-user``, and
``--dest-pass``.


project transfer
~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl project transfer -P SOURCE_PROJECT --dest-profile DEST --dest-project DEST_PROJECT [options]

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Option
     - Description
   * - ``-P`` / ``--project``
     - Source project ID (required)
   * - ``--config PATH``
     - Transfer configuration YAML file (see :ref:`transfer-config`)
   * - ``--dry-run``
     - Preview what would be transferred without writing data
   * - ``--yes`` / ``-y``
     - Skip confirmation prompt
   * - ``--workers N`` / ``-w N``
     - Number of parallel workers (default: 4, use 1 for sequential)

The command outputs a summary with counts of synced, failed, and skipped
subjects and experiments. It exits with status 1 if any transfers failed.


project transfer-check
~~~~~~~~~~~~~~~~~~~~~~

Run this before your first transfer to verify connectivity and permissions on
both servers.

.. code-block:: console

   $ xnatctl project transfer-check -P SRC --dest-profile staging --dest-project DST

The check verifies:

- Source server connectivity and version
- Source server authentication
- Destination server connectivity and version
- Destination server authentication


project transfer-status
~~~~~~~~~~~~~~~~~~~~~~~

Show the status of the most recent transfer for a project.

.. code-block:: console

   $ xnatctl project transfer-status -P MYPROJECT

Output includes sync ID, status, start/end timestamps, subject counts, and
destination details.


project transfer-history
~~~~~~~~~~~~~~~~~~~~~~~~

Show all past transfer runs for a project.

.. code-block:: console

   $ xnatctl project transfer-history -P MYPROJECT
   $ xnatctl project transfer-history -P MYPROJECT -o json


project transfer-init
~~~~~~~~~~~~~~~~~~~~~

Generate a starter YAML configuration file with default settings that you can
customize.

.. code-block:: console

   $ xnatctl project transfer-init -P SRC --dest-project DST
   $ xnatctl project transfer-init -P SRC --dest-project DST -f transfer.yaml


.. _transfer-config:

Transfer Configuration
----------------------

For fine-grained control over what gets transferred, create a YAML configuration
file. Generate a starter config with ``project transfer-init``, then customize it.

.. code-block:: yaml

   source_project: NEURO
   dest_project: NEURO_STAGING
   sync_new_only: true
   max_failures: 5
   scan_retry_count: 3
   scan_retry_delay: 5.0
   verify_after_transfer: true
   scan_workers: 4
   filtering:
     project_resources:
       sync_type: all
     subject_resources:
       sync_type: all
     subject_assessors:
       sync_type: all
     imaging_sessions:
       sync_type: all
       xsi_types: []

Configuration fields
~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Field
     - Default
     - Description
   * - ``sync_new_only``
     - ``true``
     - Only sync subjects not already present in the state database
   * - ``max_failures``
     - ``5``
     - Abort transfer after this many consecutive subject failures (circuit breaker)
   * - ``scan_retry_count``
     - ``3``
     - Number of retry attempts per scan DICOM import
   * - ``scan_retry_delay``
     - ``5.0``
     - Base delay in seconds between retries (exponential backoff)
   * - ``verify_after_transfer``
     - ``true``
     - Run two-tier verification after each experiment transfer
   * - ``scan_workers``
     - ``4``
     - Number of parallel workers for per-scan transfers within an experiment

Filtering
~~~~~~~~~

The ``filtering`` section controls which data types are included in the
transfer. Each level supports a ``sync_type`` of ``all``, ``none``,
``include``, or ``exclude`` with an ``items`` list.

Transfer only MR sessions:

.. code-block:: yaml

   filtering:
     imaging_sessions:
       sync_type: include
       xsi_types:
         - xsi_type: "xnat:mrSessionData"

Transfer MR sessions but exclude SNAPSHOTS resources:

.. code-block:: yaml

   filtering:
     imaging_sessions:
       sync_type: include
       xsi_types:
         - xsi_type: "xnat:mrSessionData"
           scan_resources:
             sync_type: exclude
             items:
               - SNAPSHOTS

Pass the config file to the transfer command:

.. code-block:: console

   $ xnatctl project transfer -p prod -P NEURO \
       --dest-profile staging --dest-project NEURO_DEV \
       --config transfer.yaml --yes


Verification
------------

When ``verify_after_transfer`` is enabled (the default), xnatctl performs
two-tier verification after each experiment transfer:

- **Tier 1 -- Scan set comparison.** Verifies that every scan ID present on the
  source also exists on the destination.
- **Tier 2 -- File count comparison.** For each scan and resource type, compares
  the file count between source and destination.

Verification results are included in the transfer summary output. If
verification fails, the transfer still completes but reports the mismatches.


Troubleshooting
---------------

Transfer fails with 401 on destination
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Ensure the destination profile credentials are correct and the user has write
permissions on the destination project.

.. code-block:: console

   $ xnatctl auth test -p staging
   $ xnatctl project transfer-check -P SRC --dest-profile staging --dest-project DST

DICOM import fails repeatedly
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If a scan's DICOM import fails after all retries, the ZIP file is retained in
a temporary directory for debugging. Check the logs for the file path. Common
causes include:

- Destination project missing required DICOM routing rules
- DICOM headers referencing a different project or subject ID
- Insufficient disk space on the destination server

Increase ``scan_retry_count`` and ``scan_retry_delay`` in the config if the
destination server is under heavy load.

Transfer interrupted mid-run
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Re-run the same command. The state database tracks which subjects have been
successfully transferred, so already-completed subjects are skipped
automatically. Only partially-transferred subjects are retried.

Circuit breaker triggers
~~~~~~~~~~~~~~~~~~~~~~~~~

If ``max_failures`` consecutive subjects fail, the transfer aborts early to
avoid wasting time when there is a systemic issue (e.g., destination server
down, permissions revoked). Fix the underlying issue and re-run.
