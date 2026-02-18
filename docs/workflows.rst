Common Workflows
================

This page shows end-to-end workflows using **commands that exist in this repo**
and examples that match the current CLI.

Upload DICOMs to prearchive, then archive
-----------------------------------------

Step 1: Upload to prearchive (REST Import API)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use ``session upload`` with ``--prearchive`` to send an import to the prearchive
staging area.

.. code-block:: console

   $ xnatctl session upload /incoming/SUBJECT001_MR_20240115 \
       -P NEUROIMAGING \
       -S SUBJECT001 \
       -E MR001 \
       --prearchive \
       --workers 8

Under the hood this uses the XNAT Import Service (``POST /data/services/import``)
with the default handler ``DICOM-zip``. See:
`Image Session Import Service API <https://wiki.xnat.org/xnat-api/image-session-import-service-api>`_.

Step 2: List prearchive sessions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl prearchive list --project NEUROIMAGING

Step 3: Archive the prearchive session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

From the ``prearchive list`` output, identify the **timestamp** and **session name**
and archive it:

.. code-block:: console

   $ xnatctl prearchive archive NEUROIMAGING 20240115_120530 MR001 \
       --subject SUBJECT001 \
       --label MR001

If you need to delete a bad upload:

.. code-block:: console

   $ xnatctl prearchive delete NEUROIMAGING 20240115_120530 MR001 --yes

Export all sessions for a subject
---------------------------------

This workflow lists sessions (by internal XNAT IDs) and downloads them.

.. code-block:: bash

   #!/usr/bin/env bash
   set -euo pipefail

   PROJECT="NEUROIMAGING"
   SUBJECT="SUB001"
   OUTPUT="/data/exports/${SUBJECT}"
   WORKERS=8

   mkdir -p "$OUTPUT"

   sessions=$(xnatctl session list -P "$PROJECT" --subject "$SUBJECT" --output json | jq -r '.[].id')

   for session_id in $sessions; do
     echo "Downloading $session_id..."
     xnatctl session download -E "$session_id" --out "$OUTPUT" --workers "$WORKERS" --quiet
   done

Download only NIFTI resources if present
----------------------------------------

``resource list`` and ``resource download`` operate on a **session ID** (e.g. ``XNAT_E...``).

.. code-block:: bash

   #!/usr/bin/env bash
   set -euo pipefail

   PROJECT="NEUROIMAGING"
   OUTPUT="/data/nifti_exports"

   mkdir -p "$OUTPUT"

   sessions=$(xnatctl session list -P "$PROJECT" --output json | jq -r '.[].id')

   for session_id in $sessions; do
     has_nifti=$(xnatctl resource list "$session_id" --output json | jq -r '.[] | select(.label == "NIFTI") | .label' | head -n 1)
     if [ -n "$has_nifti" ]; then
       echo "Downloading NIFTI for $session_id..."
       xnatctl resource download "$session_id" NIFTI --file "$OUTPUT/${session_id}_NIFTI.zip"
     fi
   done

Refresh catalogs after manual file operations
---------------------------------------------

.. code-block:: console

   $ xnatctl admin refresh-catalogs --project NEUROIMAGING

Run a pipeline on a session
---------------------------

.. code-block:: console

   $ xnatctl pipeline list --project NEUROIMAGING
   $ xnatctl pipeline run DicomToNifti --experiment XNAT_E00001
   $ xnatctl pipeline status JOB_12345

