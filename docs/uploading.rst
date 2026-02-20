Uploading Data
==============

Uploading imaging data to XNAT is one of the most common operations you will
perform with xnatctl. Whether you are ingesting a single session from a scanner
export or batch-loading thousands of DICOM files from an archive migration,
xnatctl provides several upload pathways that balance speed, reliability, and
operational requirements.

At the transport level, XNAT accepts imaging data through two independent
mechanisms: the **REST Import API** (HTTP-based) and the **DICOM network
protocol** (C-STORE). The REST Import API is the most common path and is what
``xnatctl session upload`` uses by default. The DICOM network protocol is a
separate pathway that speaks native DICOM to an XNAT DICOM Receiver -- this is
what ``xnatctl session upload-dicom`` uses. Both ultimately land data in the same
XNAT archive, but they differ in how data is packaged, transmitted, and routed.

This guide walks you through each method, explains when to choose one over
another, and covers the full lifecycle from upload through verification. For
background on XNAT's data hierarchy, prearchive staging area, and the difference
between IDs and labels, see :doc:`concepts`.


.. _upload-method-comparison:

Which Upload Method Should You Use?
------------------------------------

xnatctl supports three distinct upload methods. The right choice depends on the
size of your dataset, whether you need resumability, and whether your XNAT
administrator requires the native DICOM protocol. The table below summarizes the
key differences to help you decide.

.. list-table:: Upload Method Comparison
   :header-rows: 1
   :widths: 18 28 28 26

   * - Criteria
     - REST DICOM-zip (default)
     - REST gradual-DICOM (``--gradual``)
     - DICOM C-STORE (``upload-dicom``)
   * - **Command**
     - ``session upload``
     - ``session upload --gradual``
     - ``session upload-dicom``
   * - **Transport**
     - HTTP (REST API)
     - HTTP (REST API)
     - DICOM network protocol
   * - **How files are sent**
     - Bundled into archive batches (tar/zip), one request per batch
     - One HTTP request per DICOM file
     - One C-STORE association per file (parallelizable)
   * - **Speed**
     - Fast (fewer HTTP requests, compressed payloads)
     - Slower (many small HTTP requests)
     - Moderate (native protocol, depends on network)
   * - **Resumability**
     - No -- a failed batch must be re-sent entirely
     - Yes -- individual files succeed or fail independently
     - Yes -- individual files succeed or fail independently
   * - **Requirements**
     - XNAT 1.7+ with Import Service enabled
     - XNAT 1.7+ with Import Service enabled
     - XNAT DICOM Receiver/SCP enabled; ``xnatctl[dicom]`` extra installed
   * - **Best for**
     - Most users and most datasets
     - Very large datasets where partial progress matters, or debugging imports
     - Sites where the XNAT admin requires native DICOM ingestion

For most workflows, the default **DICOM-zip** mode is the right choice. It
minimizes the number of HTTP requests by bundling files into compressed archives,
which reduces overhead and is typically the fastest path. If you are uploading a
very large dataset (tens of thousands of files) and want the ability to resume
after a failure without re-sending files that already succeeded, use
**gradual-DICOM**. Use **DICOM C-STORE** only when your XNAT deployment requires
native DICOM protocol ingestion.


REST Import API: DICOM-zip (Compressed Archive Upload)
------------------------------------------------------

The default upload mode in xnatctl bundles your DICOM files into compressed
archives and sends them to XNAT's
`Image Session Import Service API <https://wiki.xnat.org/xnat-api/image-session-import-service-api>`_
using the ``DICOM-zip`` import handler. This approach is efficient because it
reduces the total number of HTTP requests: instead of one request per file, you
send one request per batch, where each batch contains many DICOM files packed
into a single archive.

Under the hood, xnatctl splits the files in your source directory into N batches
(where N equals the ``--workers`` count), compresses each batch into a tar or zip
archive, and uploads the batches in parallel. This means increasing ``--workers``
both increases upload parallelism and creates more (smaller) batches.


Basic Directory Upload
~~~~~~~~~~~~~~~~~~~~~~

To upload a directory of DICOM files, provide the path along with the project,
subject, and session identifiers.

.. code-block:: console

   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION

.. note::

   xnatctl scans directories **recursively** and includes extensionless files,
   which is common for scanner DICOM exports. You do not need to flatten your
   directory structure before uploading.

You can also upload a pre-built archive file (zip or tar) directly.

.. code-block:: console

   $ xnatctl session upload /path/to/session_data.zip \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION


Tuning Workers and Archive Format
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

By default, xnatctl uses 4 parallel workers. For large datasets or fast network
connections, you can increase this to speed up the upload. Each worker handles
one batch, so more workers means more concurrent uploads and smaller individual
batches.

.. code-block:: console

   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --workers 8

You can also select the archive format used for batching. The default is ``tar``,
but ``zip`` is available if your XNAT instance handles it better.

.. code-block:: console

   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --workers 8 \
       --archive-format zip

.. tip::

   If you have a pre-existing ZIP archive but your XNAT instance processes TAR
   archives more reliably, use ``--zip-to-tar`` to convert on the fly before
   upload:

   .. code-block:: console

      $ xnatctl session upload /path/to/session.zip \
          -P MYPROJECT -S MYSUBJECT -E MYSESSION \
          --zip-to-tar


Direct Archive vs Prearchive
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When data arrives at XNAT via the Import API, it can go to one of two places:
the **permanent archive** or the **prearchive** (a temporary staging area). The
choice depends on your workflow and how much review you need before data becomes
part of the official dataset.

**Direct archive** (``--direct-archive``, the default) sends data straight into
the permanent archive, bypassing the prearchive entirely. This is the right
choice for automated pipelines, re-uploads of validated data, or any situation
where you are confident the data is correct. Direct archive is supported on XNAT
1.8.3 and later.

**Prearchive** (``--prearchive``) stages data in XNAT's temporary holding area
first. A human operator (or automated script) must then review and explicitly
archive the session before it appears in the main data hierarchy. This is useful
when you want to inspect DICOM headers, verify subject assignments, or check scan
completeness before committing data. See :doc:`concepts` for a full explanation
of the prearchive workflow.

.. code-block:: console

   # Direct archive (default) -- data goes straight into the permanent archive
   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --direct-archive

.. code-block:: console

   # Prearchive -- data stages for review before archiving
   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --prearchive

See the XNAT documentation for more on direct archive behavior:
`Using Direct-to-Archive Uploading <https://wiki.xnat.org/documentation/using-direct-to-archive-uploading>`_.

.. warning::

   Direct archive mode requires XNAT 1.8.3 or later. If your server runs an
   older version, uploads with ``--direct-archive`` may fail or silently route
   to the prearchive. Check with your XNAT administrator if you are unsure.


Authentication for Parallel Uploads
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Directory uploads are parallelized across worker threads. Each worker needs valid
credentials to authenticate its HTTP requests. If you have a cached session token
(from ``xnatctl auth login``), xnatctl reuses it across all workers
automatically.

If you do not have a cached session token, xnatctl will prompt for credentials
or you can provide them explicitly. Credential sources follow the standard
priority: CLI args > environment variables > profile config > interactive prompt.

.. code-block:: console

   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --username myuser \
       --password mypassword

.. tip::

   For scripted or automated uploads, set ``XNAT_USER`` and ``XNAT_PASS``
   environment variables to avoid interactive prompts. Alternatively, run
   ``xnatctl auth login`` beforehand to cache a session token.


REST Import API: gradual-DICOM (File-by-File)
----------------------------------------------

The ``--gradual`` flag switches from batch-archive upload to file-by-file upload
using the ``gradual-DICOM`` import handler. Instead of bundling files into
archives, xnatctl sends one HTTP request per DICOM file to XNAT's Import
Service.

This mode is slower than DICOM-zip because of the per-file HTTP overhead, but it
has a significant advantage: **each file succeeds or fails independently**. If
an upload is interrupted partway through, the files that already succeeded are
safely in XNAT, and you only need to re-send the remainder. This makes
gradual-DICOM a better choice for very large datasets (tens of thousands of
files) where a single batch failure would be costly to retry.

Because each file is an independent request, you can increase ``--workers`` to a
higher value than you would use for DICOM-zip uploads. The overhead per request
is small, so more parallelism helps compensate for the per-file latency.

.. code-block:: console

   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --gradual \
       --workers 16

.. note::

   With ``--gradual``, progress output reports every 100 files to avoid flooding
   your terminal. For full per-file details, use ``--output json``.


DICOM C-STORE
--------------

The ``upload-dicom`` command sends DICOM files over the native DICOM network
protocol (C-STORE) to an XNAT DICOM Receiver (SCP). Unlike the REST-based
methods above, this pathway does not use HTTP at all -- it speaks the same
protocol that scanners and PACS systems use to transfer images.

Use C-STORE when your XNAT administrator has configured a DICOM Receiver and
requires data to arrive through that pathway, or when you need to replicate the
same ingestion path that your scanner uses. Where data lands (prearchive vs
archive) depends on the XNAT server's DICOM receiver configuration and routing
rules, not on any flag you pass to xnatctl.


Requirements
~~~~~~~~~~~~

DICOM C-STORE has two prerequisites that the REST methods do not:

1. **XNAT must have a DICOM Receiver/SCP enabled and reachable** at a specific
   host, port, and Application Entity Title (AET). Your XNAT administrator can
   provide these values.

2. **You must install the optional DICOM dependencies** (pydicom and
   pynetdicom):

.. code-block:: console

   $ pip install "xnatctl[dicom]"


Example
~~~~~~~

To send a directory of DICOM files via C-STORE, provide the DICOM Receiver's
connection details.

.. code-block:: console

   $ xnatctl session upload-dicom /path/to/DICOM_ROOT \
       --host xnat.example.org \
       --port 8104 \
       --called-aet XNAT \
       --calling-aet XNATCTL \
       --workers 4

You can also set connection parameters via environment variables to avoid
repeating them in every command:

- ``XNAT_DICOM_HOST`` -- DICOM Receiver hostname
- ``XNAT_DICOM_PORT`` -- DICOM Receiver port (default: 104)
- ``XNAT_DICOM_CALLED_AET`` -- Called AE Title
- ``XNAT_DICOM_CALLING_AET`` -- Calling AE Title (default: XNATCTL)

.. tip::

   Use ``--dry-run`` to verify your connection parameters before sending any
   data:

   .. code-block:: console

      $ xnatctl session upload-dicom /path/to/DICOM_ROOT \
          --host xnat.example.org \
          --port 8104 \
          --called-aet XNAT \
          --dry-run


Managing the Prearchive After Upload
--------------------------------------

If your upload lands in the prearchive -- either because you used
``--prearchive``, because your XNAT server's project settings route imports
there, or because DICOM C-STORE routing rules directed data to staging -- you
need to explicitly archive the session before it appears in the main data
hierarchy.

The prearchive workflow is: **upload** -> **review in prearchive** -> **archive
to permanent storage** (or delete if the data is incorrect). This extra step
gives you a chance to verify subject assignments, check scan completeness, and
catch any header issues before data becomes part of the official dataset. See
:doc:`concepts` for a full explanation of the prearchive.


List Prearchive Sessions
~~~~~~~~~~~~~~~~~~~~~~~~~

After uploading, check whether your data arrived in the prearchive.

.. code-block:: console

   $ xnatctl prearchive list --project MYPROJECT


Archive a Prearchive Session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Once you are satisfied the data is correct, move it from the prearchive into the
permanent archive.

.. code-block:: console

   $ xnatctl prearchive archive MYPROJECT 20240115_120000 Session1 \
       --subject MYSUBJECT \
       --label MYSESSION


Delete a Prearchive Session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If the data is incorrect or was uploaded by mistake, you can remove it from the
prearchive without it ever entering the archive.

.. code-block:: console

   $ xnatctl prearchive delete MYPROJECT 20240115_120000 Session1 --yes


Uploading Non-DICOM Resources
-------------------------------

Not all data in XNAT is DICOM. You may need to attach NIfTI files, BIDS
datasets, quality-control snapshots, or other derived outputs to sessions or
scans. Use ``xnatctl resource upload`` for these cases. When you upload a
directory, xnatctl zips it locally and extracts it server-side; single files are
uploaded directly.


Upload a Directory to a Session Resource
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This uploads the entire directory as a named resource on the session.

.. code-block:: console

   $ xnatctl resource upload XNAT_E00001 BIDS ./bids_dataset


Upload a File to a Session Resource
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

You can also upload individual files.

.. code-block:: console

   $ xnatctl resource upload XNAT_E00001 NIFTI ./t1w.nii.gz


Upload to a Scan Resource
~~~~~~~~~~~~~~~~~~~~~~~~~~

To attach files to a specific scan within a session, use the ``--scan`` flag.

.. code-block:: console

   $ xnatctl resource upload XNAT_E00001 SNAPSHOTS ./qc_pngs --scan 1


.. _verifying-upload:

Verifying Your Upload
----------------------

After uploading, you should confirm that your data arrived in XNAT and that the
scan count matches your expectations. Where you look depends on whether the data
went to the permanent archive or the prearchive.


Check the Archive
~~~~~~~~~~~~~~~~~~

If you used ``--direct-archive`` (the default for REST uploads), your session
should appear in the session listing immediately after the upload completes.

.. code-block:: console

   $ xnatctl session list -P MYPROJECT

To verify individual scan counts, inspect the session detail view.

.. code-block:: console

   $ xnatctl scan list -E MYSESSION -P MYPROJECT

Compare the number of scans returned against what you expect from your source
data (e.g., the number of series directories in your DICOM export).


Check the Prearchive
~~~~~~~~~~~~~~~~~~~~~

If your data does not appear in ``session list``, it may be in the prearchive.

.. code-block:: console

   $ xnatctl prearchive list --project MYPROJECT

.. note::

   Data in the prearchive is not yet part of the permanent archive. It will not
   appear in ``session list`` or ``scan list`` until you explicitly archive it
   with ``xnatctl prearchive archive``.


Common Reasons Data Might Not Appear
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If your upload command completed successfully but you cannot find the data in
either the archive or the prearchive, consider these possibilities:

- **Data is still in the prearchive.** This is the most common reason. Check
  with ``xnatctl prearchive list --project MYPROJECT``.
- **Data went to a different project.** If the DICOM headers contain a different
  project identifier than what you specified, XNAT may have routed the session
  elsewhere. Ask your XNAT admin to check the server logs.
- **Import errors on the server.** XNAT may have rejected some or all files due
  to unparsable DICOM headers or validation failures. Check the XNAT server's
  import logs (accessible through the web UI under Administration > Event
  Service or the prearchive error log).
- **The session merged with an existing session.** If a session with the same
  subject and session label already exists, XNAT may have merged the upload
  into the existing session rather than creating a new one. Check the existing
  session's scan list to see if new scans appeared.


Troubleshooting
----------------

Upload Succeeds but Data Not in Archive
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The most likely explanation is that data landed in the prearchive. Check with:

.. code-block:: console

   $ xnatctl prearchive list --project MYPROJECT

If you see your session there, archive it with ``xnatctl prearchive archive``.
If you intended direct archive, verify that you passed ``--direct-archive``
(which is the default) and that your XNAT server supports it (XNAT 1.8.3+).
Some XNAT project-level settings can override the upload request and force data
into the prearchive regardless of the ``Direct-Archive`` flag.


Timeout on Large Uploads
~~~~~~~~~~~~~~~~~~~~~~~~~~

If uploads of large datasets fail with timeout errors, you can increase the HTTP
timeout in your profile configuration or via environment variable. The default
timeout for upload operations is 6 hours (21600 seconds), which should be
sufficient for most datasets. If you need more, set a higher value.

.. code-block:: yaml

   # In ~/.config/xnatctl/config.yaml
   profiles:
     production:
       url: https://xnat.example.org
       timeout: 43200  # 12 hours

Alternatively, use the ``XNAT_TIMEOUT`` environment variable:

.. code-block:: console

   $ XNAT_TIMEOUT=43200 xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION


"Already Exists" Errors
~~~~~~~~~~~~~~~~~~~~~~~~~

If XNAT reports that a session or scan already exists, the behavior depends on
the ``--overwrite`` flag. By default, xnatctl uses ``--overwrite delete``, which
replaces existing data. If you want to append new scans to an existing session
without removing old data, use ``--overwrite append``. To prevent any changes to
existing sessions, use ``--overwrite none``.

.. code-block:: console

   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --overwrite append


401 Unauthorized
~~~~~~~~~~~~~~~~~

A 401 error means your session token has expired or you are not authenticated.
Log in again and retry.

.. code-block:: console

   $ xnatctl auth login
   $ xnatctl whoami

If you are running uploads in a script, ensure that ``XNAT_USER`` and
``XNAT_PASS`` environment variables are set, or that you have run
``xnatctl auth login`` recently enough that the cached session token has not
expired. Session tokens expire after 15 minutes of inactivity by default.


DICOM C-STORE Connection Issues
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

C-STORE failures are almost always caused by incorrect connection parameters or
network configuration. Verify the following with your XNAT administrator:

- **Wrong port.** The default DICOM port is 104, but many XNAT installations use
  a non-standard port such as 8104. Confirm the correct port.
- **Wrong Called AE Title.** The Called AET must match what is configured on the
  XNAT DICOM Receiver. A mismatch causes the receiver to reject the
  association.
- **Firewall or network issues.** Ensure that the DICOM Receiver port is open
  and reachable from your machine. Try ``telnet xnat.example.org 8104`` (or the
  appropriate port) to test basic connectivity.
- **Missing DICOM dependencies.** C-STORE requires the ``xnatctl[dicom]`` extra.
  If you see an import error mentioning pydicom or pynetdicom, install it with
  ``pip install "xnatctl[dicom]"``.

.. tip::

   Use ``--dry-run`` with ``upload-dicom`` to print the connection parameters
   that xnatctl would use, without actually sending any data. This is a quick
   way to verify your configuration.
