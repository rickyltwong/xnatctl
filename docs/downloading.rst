Downloading Data
================

Downloading is one of the most common operations you will perform with xnatctl.
Whether you are pulling an entire imaging session for local analysis, grabbing a
handful of scans for quality review, or fetching a specific resource like a NIFTI
conversion, xnatctl provides a dedicated command for each scenario.

xnatctl offers three download commands, each targeting a different level of the
XNAT data hierarchy. All three support streaming transfers, progress reporting,
and optional ZIP extraction. The sections below explain when to reach for each
command and walk through the most common workflows.

For the full set of flags on any command, run ``xnatctl <resource> download --help``.


Which Download Command Should You Use?
--------------------------------------

If you are unsure which command to start with, use the table below as a quick
decision guide.

.. list-table::
   :header-rows: 1
   :widths: 45 30 25

   * - Goal
     - Command
     - Key flags
   * - Download everything from a session (all scans, all resources)
     - ``session download``
     - ``-E``, ``--workers``, ``--include-resources``
   * - Download one or more specific scans from a session
     - ``scan download``
     - ``-E``, ``-s``, ``--resource``
   * - Download a single named resource (e.g., DICOM, NIFTI, BIDS)
     - ``resource download``
     - positional ``SESSION_ID`` and ``RESOURCE_LABEL``, ``--scan``

Start with ``session download`` when you need a full local copy. Switch to
``scan download`` when you only need a subset of scans. Use ``resource download``
when you need a specific resource type -- for example, pulling just the NIFTI
files without the raw DICOMs.


Download an Entire Session
--------------------------

The ``session download`` command retrieves all scans (and optionally
session-level resources) from a single imaging session. This is the right
starting point when you want a complete local copy of everything the scanner
produced for one visit.

Download by accession ID
~~~~~~~~~~~~~~~~~~~~~~~~

The simplest way to identify a session is by its XNAT accession ID (e.g.,
``XNAT_E00001``). Accession IDs are globally unique, so you do not need to
specify a project.

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data

This downloads all scans as a single ZIP into ``./data/XNAT_E00001/``. To use
a more descriptive directory name, pass ``--name``.

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --name SESSION01

Download by session label
~~~~~~~~~~~~~~~~~~~~~~~~~

Session labels like ``SUB001_MR_20240115`` are easier to remember than accession
IDs, but they are only unique within a project. To use a label with ``-E``, you
must also provide ``-P`` so XNAT knows which project to search.

.. code-block:: console

   $ xnatctl session download -E SUB001_MR_20240115 -P MYPROJECT --out ./data

.. tip::

   If you primarily work within one project, set ``default_project`` in your
   profile so you can skip ``-P`` on every command. See :doc:`configuration`
   for profile setup details.

For a deeper explanation of how accession IDs and labels differ, see
:ref:`ids-vs-labels`.

Parallel downloads
~~~~~~~~~~~~~~~~~~

By default, ``session download`` fetches all scans in a single ZIP request
(``--workers 1``). For larger sessions or high-latency connections, you can
speed up the transfer by downloading each scan in parallel.

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --workers 8

Each scan is downloaded and extracted independently, so you see incremental
progress as individual scans complete.

.. tip::

   Start with ``--workers 4`` for typical institutional networks. Increase to 8
   or 16 if your XNAT server and network can handle the concurrency. Going
   beyond 16 rarely helps and may trigger server-side rate limiting.

Including session-level resources
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Session-level resources are files attached directly to the session rather than
to a specific scan -- quality control reports, processing logs, or protocol
documents. By default, ``session download`` skips these to keep downloads
focused on imaging data.

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --include-resources

Extracting ZIPs automatically
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

By default, downloads are saved as ZIP archives. Add ``--unzip`` to extract
files into a directory structure immediately, and ``--cleanup`` (the default)
to remove the ZIP files after successful extraction.

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --unzip --cleanup

.. tip::

   To keep the ZIPs as backup copies alongside the extracted files, use
   ``--unzip --no-cleanup``.

Preview with dry run
~~~~~~~~~~~~~~~~~~~~

Before committing to a large download, preview what would happen without
transferring any data.

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --dry-run


Download Specific Scans
-----------------------

The ``scan download`` command fetches one or more scans from a session as a
single batched ZIP. Use this when you only need a subset of scans -- for
example, pulling just the T1-weighted anatomical for a segmentation pipeline.

Download a single scan
~~~~~~~~~~~~~~~~~~~~~~

Specify the scan number with ``-s`` and the parent session with ``-E``.

.. code-block:: console

   $ xnatctl scan download -E XNAT_E00001 -s 1 --out ./data

Download multiple scans
~~~~~~~~~~~~~~~~~~~~~~~

Pass a comma-separated list to ``-s``, or use the wildcard ``'*'`` for all scans.

.. code-block:: console

   $ xnatctl scan download -E XNAT_E00001 -s 1,2,3 --out ./data
   $ xnatctl scan download -E XNAT_E00001 -s '*' --out ./data

Download by session label
~~~~~~~~~~~~~~~~~~~~~~~~~

Just like ``session download``, you can use a session label instead of an
accession ID by providing ``-P``.

.. code-block:: console

   $ xnatctl scan download -P MYPROJECT -E SUB001_MR_20240115 -s 1 --out ./data

Download a specific resource type
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

By default, ``scan download`` fetches all resources for the requested scans. If
you only need one resource type, use ``--resource`` to filter.

.. code-block:: console

   $ xnatctl scan download -E XNAT_E00001 -s 1,2 --resource DICOM --out ./data

This is useful when scans have both raw DICOMs and converted NIfTI files and
you only need one format.

Extracting scan ZIPs
~~~~~~~~~~~~~~~~~~~~~

As with session downloads, you can extract and clean up in one step.

.. code-block:: console

   $ xnatctl scan download -E XNAT_E00001 -s 1 --out ./data --unzip --cleanup


Download a Resource
-------------------

The ``resource download`` command gives you fine-grained control when you need
a single named resource from a session or scan. This is the most targeted
download option.

Session-level resource
~~~~~~~~~~~~~~~~~~~~~~

Pass the session accession ID and the resource label as positional arguments.

.. code-block:: console

   $ xnatctl resource download XNAT_E00001 DICOM --file ./dicoms.zip

Scan-level resource
~~~~~~~~~~~~~~~~~~~

To download a resource from a specific scan, add ``--scan``.

.. code-block:: console

   $ xnatctl resource download XNAT_E00001 DICOM --scan 1 --file ./scan1_dicoms.zip

The output is always a single ZIP file written to the path you specify with
``--file``.


Understanding the Output Directory Structure
--------------------------------------------

After downloading and extracting a session with ``--unzip``, your local
directory mirrors the XNAT data hierarchy. Understanding this layout helps you
write scripts that process downloaded data reliably.

A parallel session download (``--workers > 1``) produces the following structure.

.. code-block:: text

   ./data/
   +-- XNAT_E00001/
       +-- scans/
           +-- 1/
           |   +-- resources/
           |       +-- DICOM/
           |           +-- files/
           |               +-- 0001.dcm
           |               +-- 0002.dcm
           +-- 2/
           |   +-- resources/
           |       +-- DICOM/
           |           +-- files/
           |               +-- 0001.dcm
           +-- 3/
               +-- resources/
                   +-- DICOM/
                       +-- files/
                           +-- 0001.dcm

The general path pattern is always:

.. code-block:: text

   {output_dir}/{session}/scans/{scan_id}/resources/{resource_label}/files/

If you also passed ``--include-resources``, session-level resources appear
alongside the ``scans/`` directory:

.. code-block:: text

   ./data/
   +-- XNAT_E00001/
       +-- scans/
       |   +-- 1/
       |   +-- 2/
       +-- resources_QC/
       +-- resources_PROTOCOLS/

.. tip::

   You can rely on the ``scans/{scan_id}/resources/DICOM/files/`` path
   convention when writing processing scripts. This layout is consistent across
   parallel downloads and matches the structure expected by XNAT's compressed
   uploader for round-trip workflows.


Troubleshooting
---------------

401 Unauthorized
~~~~~~~~~~~~~~~~

A 401 error means your session token has expired or was never created. XNAT
session tokens expire after 15 minutes of inactivity. Re-authenticate and
verify your identity before retrying.

.. code-block:: console

   $ xnatctl auth login
   $ xnatctl whoami

If you are running downloads in a script or CI pipeline, make sure ``XNAT_USER``
and ``XNAT_PASS`` environment variables are set so xnatctl can re-authenticate
automatically. See :doc:`configuration` for details.

Session label cannot be resolved
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you pass a session label to ``-E`` without ``-P`` (and without
``default_project`` in your profile), XNAT cannot resolve the label because
labels are only unique within a project. You will see "Session not found."

.. code-block:: console

   # Option 1: pass -P explicitly
   $ xnatctl session download -E SUB001_MR_20240115 -P MYPROJECT --out ./data

   # Option 2: set default_project in your profile
   $ xnatctl config add-profile default --project MYPROJECT

.. note::

   When ``-P`` is omitted and no ``default_project`` is configured, the ``-E``
   value is sent directly to ``/data/experiments/{value}``, which only accepts
   globally unique accession IDs (e.g., ``XNAT_E00001``). See
   :ref:`ids-vs-labels` for the full explanation.

Slow downloads
~~~~~~~~~~~~~~

Large imaging sessions can take a long time to download over institutional
networks. Try increasing parallelism to saturate available bandwidth.

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --workers 8

.. warning::

   Setting ``--workers`` too high (e.g., 32+) may overwhelm the XNAT server or
   trigger rate limiting. If you see intermittent failures at high worker counts,
   reduce the number.

Incomplete downloads
~~~~~~~~~~~~~~~~~~~~

If a download is interrupted by a network timeout or dropped connection, you can
safely re-run the same command. xnatctl overwrites partial files from the
previous attempt, making downloads effectively idempotent.

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --workers 8

.. tip::

   For very large sessions on unreliable networks, consider downloading scans
   individually with ``scan download -s <id>`` so you can retry only the scans
   that failed rather than re-downloading the entire session.
