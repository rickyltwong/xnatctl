Uploading Data
==============

This guide covers uploading DICOM sessions and non-DICOM resources to XNAT using
``xnatctl``.

Overview
--------

XNAT supports multiple ingestion pathways for DICOM data. There is no single
universally “recommended” method; each is a different transport with different
tradeoffs.

At a high level:

1. **REST Import API** (``POST /data/services/import``): Upload data over HTTP
   using an XNAT *import-handler*. The two most common handlers are:

   - **``gradual-DICOM``**: file-by-file upload of individual DICOM instances.
   - **``DICOM-zip``**: upload a *compressed collection* of DICOM files (zip/tar)
     in a single request.

   See: `Image Session Import Service API <https://wiki.xnat.org/xnat-api/image-session-import-service-api>`_.

2. **DICOM network transfer (C-STORE)**: Upload over the DICOM protocol to an
   XNAT DICOM Receiver/SCP. This is **not** REST/HTTP-based.

In ``xnatctl``:

- **REST Import API** uploads use ``xnatctl session upload`` (default: ``DICOM-zip``).
- **DICOM C-STORE** uploads use ``xnatctl session upload-dicom``.

For background on XNAT’s ingestion paths, see the official XNAT documentation:

- `Image Session Import Service API <https://wiki.xnat.org/xnat-api/image-session-import-service-api>`_
- `Using Direct-to-Archive Uploading <https://wiki.xnat.org/documentation/using-direct-to-archive-uploading>`_

REST Import API: gradual-DICOM vs DICOM-zip
-------------------------------------------

The XNAT Import API supports multiple handlers selected via the
``import-handler`` query parameter. If unspecified, the default is ``DICOM-zip``.
When attaching a file directly in the request body, XNAT requires ``inbody=true``.

Key idea:

- **``gradual-DICOM``**: file-by-file transfer (many requests; one DICOM per request).
- **``DICOM-zip``**: compressed collection upload (fewer requests; many DICOMs per archive).

See: `Image Session Import Service API <https://wiki.xnat.org/xnat-api/image-session-import-service-api>`_.

Upload DICOM via REST Import API (DICOM-zip: compressed archive upload)
----------------------------------------------------------------------

This mode bundles many DICOM files into one archive per batch, then uploads each
archive as a single HTTP request to XNAT’s import service.

Basic directory upload (DICOM-zip)
~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION

Notes
^^^^^

- ``xnatctl`` scans directories **recursively** and includes extensionless files
  (common for scanner DICOM exports).
- For directory uploads, ``xnatctl`` splits the files into **N batches where
  N = --workers**. Each batch is archived and uploaded independently.

Tuning workers and archive format
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   # More upload parallelism
   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --workers 8

.. code-block:: console

   # Select archive format (tar or zip)
   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --workers 8 \
       --archive-format tar

Direct-archive vs prearchive
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

XNAT’s Import API supports a **Direct-to-Archive** (DA) mode (XNAT 1.8.3+) that
can bypass the prearchive by setting the ``Direct-Archive=true`` query parameter.
DA is handled by the ``gradual-DICOM`` and ``DICOM-zip`` import handlers.

See: `Using Direct-to-Archive Uploading <https://wiki.xnat.org/documentation/using-direct-to-archive-uploading>`_.

``xnatctl`` exposes this toggle:

.. code-block:: console

   # Default: direct archive
   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --direct-archive

.. code-block:: console

   # Stage into prearchive instead
   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --prearchive

Authentication note (directory uploads)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Directory REST uploads are parallelized across worker threads. If you do not
have a cached session token, ``xnatctl`` will prompt for credentials (or you can
provide them explicitly).

Credential sources follow the normal priority described in the Import API tools:
CLI args > env vars > profile config > prompt.

Example:

.. code-block:: console

   $ xnatctl session upload /path/to/DICOM_ROOT -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --username myuser \
       --password mypassword

REST Import API: gradual-DICOM (file-by-file HTTP) with ``--gradual``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use ``--gradual`` to upload one DICOM instance per HTTP request using the
``gradual-DICOM`` handler (still REST/HTTP, but file-by-file).

.. code-block:: console

   $ xnatctl session upload /path/to/DICOM_ROOT \
       -P MYPROJECT -S MYSUBJECT -E MYSESSION \
       --gradual \
       --workers 16

Upload DICOM via DICOM C-STORE (file-by-file transfer)
------------------------------------------------------

This mode sends DICOM objects over the DICOM protocol (C-STORE). It is **not**
REST/HTTP-based. It is inherently
**file-by-file transfer** (each DICOM instance is sent separately), even if
multiple associations are used in parallel.

Requirements
~~~~~~~~~~~~

- XNAT must have a DICOM Receiver/SCP enabled and reachable (host/port/AET).
- Install optional dependencies:

.. code-block:: console

   $ pip install "xnatctl[dicom]"

Example
~~~~~~~

.. code-block:: console

   $ xnatctl session upload-dicom /path/to/DICOM_ROOT \
       --host xnat.example.org \
       --port 8104 \
       --called-aet XNAT \
       --calling-aet XNATCTL \
       --workers 4

Where data lands (prearchive vs archive) depends on the XNAT server’s DICOM
receiver configuration and routing rules.

Managing the XNAT prearchive
----------------------------

If your upload lands in prearchive (by choice or server configuration), you can
inspect and archive it with ``xnatctl prearchive``.

List prearchive sessions
~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl prearchive list --project MYPROJECT

Archive a prearchive session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl prearchive archive MYPROJECT 20240115_120000 Session1 \
       --subject MYSUBJECT \
       --label MYSESSION

Delete a prearchive session
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl prearchive delete MYPROJECT 20240115_120000 Session1 --yes

Uploading non-DICOM resources
-----------------------------

Use ``xnatctl resource upload`` to attach files/directories to a session or scan
resource. Directories are zipped locally and extracted server-side.

Upload a directory to a session resource
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl resource upload XNAT_E00001 BIDS ./bids_dataset

Upload a file to a session resource
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl resource upload XNAT_E00001 NIFTI ./t1w.nii.gz

Upload to a scan resource
~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl resource upload XNAT_E00001 SNAPSHOTS ./qc_pngs --scan 1

Troubleshooting
---------------

401 Unauthorized / not authenticated
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Login and retry:

.. code-block:: console

   $ xnatctl auth login
   $ xnatctl whoami

Directory REST upload complains about credentials
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you are not logged in with a cached session token, provide credentials via
``--username``/``--password`` or environment variables (``XNAT_USER``/``XNAT_PASS``),
then retry.

DICOM C-STORE connection issues
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Verify host/port/AET values with your XNAT admin. C-STORE failures are usually
one of:

- Wrong port (e.g., 104 vs 8104).
- Wrong called AE title.
- Network/firewall issues.

