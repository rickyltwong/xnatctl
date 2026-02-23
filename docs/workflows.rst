Common Workflows
================

This page presents real-world patterns that combine multiple xnatctl commands to
accomplish common research data management tasks. Each workflow includes a scenario
description, a step-by-step walkthrough, and a ready-to-use script.

Whether you are onboarding a new dataset, exporting sessions for analysis, or
automating nightly transfers in CI/CD, these recipes give you a concrete starting
point. Adapt the project IDs, paths, and worker counts to match your environment.


Upload DICOMs to Prearchive, Then Archive
-----------------------------------------

**Scenario.** You have received DICOM data from a scanner export and need to import
it into your XNAT project. The safest approach is to land the data in the prearchive
staging area first, verify it looks correct, and then archive it into the permanent
project structure. This two-phase workflow protects you from accidentally committing
malformed or mislabeled sessions.

For a detailed walkthrough of each upload method, see :doc:`uploading`.

Step 1 -- Upload to prearchive
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use ``session upload`` with ``--prearchive`` to send an import to the prearchive
staging area.

.. code-block:: console

   $ xnatctl session upload /incoming/SUBJECT001_MR_20240115 \
       -P NEUROIMAGING \          # target project
       -S SUBJECT001 \            # subject label
       -E MR001 \                 # experiment label
       --prearchive \             # land in prearchive (not directly in archive)
       --workers 8                # parallel upload threads

Under the hood this uses the XNAT Import Service (``POST /data/services/import``)
with the default handler ``DICOM-zip``. See:
`Image Session Import Service API <https://wiki.xnat.org/xnat-api/image-session-import-service-api>`_.

Step 2 -- List prearchive sessions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Once the upload finishes, confirm the session appeared in the prearchive.

.. code-block:: console

   $ xnatctl prearchive list --project NEUROIMAGING

Step 3 -- Archive the prearchive session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

From the ``prearchive list`` output, identify the **timestamp** and **session name**
and archive it into the permanent project structure.

.. code-block:: console

   $ xnatctl prearchive archive NEUROIMAGING 20240115_120530 MR001 \
       --subject SUBJECT001 \     # ensure correct subject assignment
       --label MR001              # set the experiment label in the archive

If you need to delete a bad upload instead of archiving it:

.. code-block:: console

   $ xnatctl prearchive delete NEUROIMAGING 20240115_120530 MR001 --yes

**What to expect.** After archiving, the session appears under
``/data/projects/NEUROIMAGING/subjects/SUBJECT001/experiments/MR001`` in the XNAT
REST hierarchy. You can verify with ``xnatctl session show -P NEUROIMAGING -E MR001``.
If the DICOM headers contain subject or session metadata that conflicts with the
labels you provided, XNAT may rename the session -- check the archive output for
any warnings.


Export All Sessions for a Subject
---------------------------------

**Scenario.** You need to download all imaging sessions for a specific subject, for
instance to run a local processing pipeline or to create an offline backup before
a server migration. This script iterates over every session belonging to a subject
and downloads each one to a local directory.

For detailed download options and flags, see :doc:`downloading`.

.. code-block:: bash

   #!/usr/bin/env bash
   set -euo pipefail

   # -- Configuration --
   PROJECT="NEUROIMAGING"
   SUBJECT="SUB001"
   OUTPUT="/data/exports/${SUBJECT}"
   WORKERS=8

   mkdir -p "$OUTPUT"

   # Fetch all session IDs for this subject as JSON, extract the id field
   sessions=$(xnatctl session list -P "$PROJECT" --subject "$SUBJECT" \
       --output json | jq -r '.[].id')

   # Download each session into the output directory
   for session_id in $sessions; do
     echo "Downloading $session_id..."
     xnatctl session download -E "$session_id" \
         --out "$OUTPUT" \
         --workers "$WORKERS" \
         --quiet
   done

**What to expect.** Each session is downloaded as a ZIP archive (or extracted
directory, depending on your flags) into ``/data/exports/SUB001/``. The script
processes sessions sequentially, but each individual download uses parallel workers
for the file transfer itself. For very large subjects with many sessions, you may
want to add a short ``sleep`` between iterations to avoid overwhelming the server.


Download Only NIFTI Resources If Present
----------------------------------------

**Scenario.** Your analysis pipeline requires NIFTI files, but not every session has
had NIFTI conversion run. You want to iterate over all sessions in a project and
download only the NIFTI resource where it exists, skipping sessions that have not
been converted yet.

.. code-block:: bash

   #!/usr/bin/env bash
   set -euo pipefail

   # -- Configuration --
   PROJECT="NEUROIMAGING"
   OUTPUT="/data/nifti_exports"

   mkdir -p "$OUTPUT"

   # Get all session IDs in the project
   sessions=$(xnatctl session list -P "$PROJECT" --output json | jq -r '.[].id')

   for session_id in $sessions; do
     # Check whether this session has a resource labeled "NIFTI"
     has_nifti=$(xnatctl resource list "$session_id" --output json \
         | jq -r '.[] | select(.label == "NIFTI") | .label' \
         | head -n 1)

     if [ -n "$has_nifti" ]; then
       echo "Downloading NIFTI for $session_id..."
       xnatctl resource download "$session_id" NIFTI \
           --file "$OUTPUT/${session_id}_NIFTI.zip"
     fi
   done

**What to expect.** Only sessions that have a resource with the label ``NIFTI``
produce a download. Sessions without that resource are silently skipped. The
resulting ZIP files are named by session ID so you can trace each file back to its
source. If your project uses a different label for converted images (e.g.,
``NIFTI_RAW`` or ``BIDS``), adjust the ``select(.label == ...)`` filter accordingly.

For more details on resource-level downloads, see :doc:`downloading`.


Refresh Catalogs After Manual File Operations
----------------------------------------------

**Scenario.** You or a system administrator have moved, renamed, or deleted files
directly on the XNAT filesystem (for example, cleaning up duplicates or correcting
directory structures outside of XNAT). XNAT's internal catalog files are now out of
sync with the actual files on disk. Running a catalog refresh tells XNAT to re-scan
the file system and update its metadata to match reality.

.. code-block:: console

   $ xnatctl admin refresh-catalogs NEUROIMAGING

.. tip::

   Catalog refreshes can be slow on large projects. If you only modified files in a
   single session, consider running the refresh at the session level through the XNAT
   web UI to avoid a project-wide scan.

**What to expect.** XNAT walks every resource directory under the specified project,
updates checksums, and reconciles its catalog XML files. You will not see immediate
output -- the operation runs as a server-side background task. Check the XNAT admin
logs or run ``xnatctl admin audit`` to confirm completion.


Run a Pipeline on a Session
----------------------------

**Scenario.** You want to trigger an automated processing pipeline (for example,
DICOM-to-NIFTI conversion or FreeSurfer segmentation) on a specific session. xnatctl
lets you list available pipelines, launch a run, and monitor its progress without
leaving the terminal.

.. code-block:: bash

   #!/usr/bin/env bash
   set -euo pipefail

   # Step 1: List available pipelines for the project
   xnatctl pipeline list --project NEUROIMAGING

   # Step 2: Launch the pipeline on a specific experiment
   xnatctl pipeline run DicomToNifti --experiment XNAT_E00001

   # Step 3: Monitor the job status (use the job ID from step 2)
   xnatctl pipeline status JOB_12345

.. note::

   Pipeline availability depends on your XNAT server configuration. Not all servers
   have the same pipelines installed. Use ``pipeline list`` to discover what is
   available before attempting to run anything.

**What to expect.** The ``pipeline run`` command returns a job ID immediately. The
pipeline itself executes asynchronously on the XNAT server. Poll with
``pipeline status`` until the job reaches a terminal state (``COMPLETE`` or
``FAILED``). If the pipeline fails, check the XNAT pipeline logs for details.


Auditing a Project
------------------

**Scenario.** You want to verify that all expected sessions exist in a project and
that each session has the expected number of scans. This is useful after a bulk
upload, a server migration, or as a periodic data integrity check. The script below
compares the sessions on the server against a local manifest file and reports any
discrepancies.

First, create a manifest file (``manifest.json``) that describes the expected
structure. Each entry maps a session label to the expected scan count:

.. code-block:: json

   {
     "MR001": 12,
     "MR002": 12,
     "PET001": 8
   }

Then run the audit script:

.. code-block:: bash

   #!/usr/bin/env bash
   set -euo pipefail

   # -- Configuration --
   PROJECT="NEUROIMAGING"
   MANIFEST="manifest.json"
   ERRORS=0

   echo "Auditing project: $PROJECT"

   # Iterate over each expected session in the manifest
   for session_label in $(jq -r 'keys[]' "$MANIFEST"); do
     expected_scans=$(jq -r --arg s "$session_label" '.[$s]' "$MANIFEST")

     # Fetch actual scan count from the server
     actual_scans=$(xnatctl scan list -P "$PROJECT" -E "$session_label" \
         --output json 2>/dev/null | jq 'length')

     if [ "$actual_scans" = "null" ] || [ -z "$actual_scans" ]; then
       echo "MISSING: session $session_label not found on server"
       ERRORS=$((ERRORS + 1))
     elif [ "$actual_scans" -ne "$expected_scans" ]; then
       echo "MISMATCH: $session_label has $actual_scans scans (expected $expected_scans)"
       ERRORS=$((ERRORS + 1))
     else
       echo "OK: $session_label ($actual_scans scans)"
     fi
   done

   # Summary
   if [ "$ERRORS" -gt 0 ]; then
     echo ""
     echo "Audit complete: $ERRORS issue(s) found."
     exit 1
   else
     echo ""
     echo "Audit complete: all sessions match the manifest."
   fi

.. tip::

   You can generate the manifest automatically from a known-good state by running
   ``xnatctl session list`` and ``xnatctl scan list`` on the source server, then
   formatting the output with ``jq``.

**What to expect.** The script prints one line per session: ``OK`` if the scan count
matches, ``MISMATCH`` if the count differs, or ``MISSING`` if the session does not
exist on the server. It exits with a non-zero status if any issues are found, making
it suitable for use in CI/CD pipelines or cron jobs that alert on failure.


Migrating Data Between XNAT Servers
------------------------------------

**Scenario.** You need to copy sessions from a production XNAT server to a
development server for testing. xnatctl profiles make this straightforward: download
from one profile, upload to another.

Before you begin, make sure you have two profiles configured in your
``~/.config/xnatctl/config.yaml``:

.. code-block:: yaml

   profiles:
     prod:
       url: https://xnat-prod.example.org
       username: admin
     dev:
       url: https://xnat-dev.example.org
       username: admin

Then run the migration script:

.. code-block:: bash

   #!/usr/bin/env bash
   set -euo pipefail

   # -- Configuration --
   PROJECT="NEUROIMAGING"
   SUBJECT="SUB001"
   STAGING="/tmp/xnat_migration"
   WORKERS=8

   mkdir -p "$STAGING"

   echo "Fetching session list from prod..."
   sessions=$(xnatctl session list \
       --profile prod \
       -P "$PROJECT" \
       --subject "$SUBJECT" \
       --output json | jq -r '.[].id')

   # Download each session from the production server
   for session_id in $sessions; do
     echo "Downloading $session_id from prod..."
     xnatctl session download \
         --profile prod \
         -E "$session_id" \
         --out "$STAGING/$session_id" \
         --workers "$WORKERS"
   done

   # Upload each downloaded session to the dev server
   for session_dir in "$STAGING"/*/; do
     session_name=$(basename "$session_dir")
     echo "Uploading $session_name to dev..."
     xnatctl session upload "$session_dir" \
         --profile dev \
         -P "$PROJECT" \
         -S "$SUBJECT" \
         -E "$session_name" \
         --workers "$WORKERS"
   done

   echo "Migration complete."

.. warning::

   The target server must already have the matching project and subject created
   before you upload. If the project or subject does not exist on the destination,
   create them first with ``xnatctl project create`` and the XNAT web UI. Also
   verify that the target server's DICOM routing rules will not reassign your
   sessions to a different project.

**What to expect.** Sessions are downloaded to a local staging directory, then
uploaded to the development server. This is a full copy -- the data passes through
your local machine, so ensure you have sufficient disk space in the staging
directory. For large migrations, consider processing sessions in batches rather than
all at once.


Using xnatctl in CI/CD
-----------------------

**Scenario.** You want to automate nightly data exports using GitHub Actions. By
storing your XNAT credentials as repository secrets and invoking xnatctl in a
workflow, you can schedule recurring exports, audits, or pipeline triggers without
manual intervention.

Below is an example GitHub Actions workflow that exports all sessions for a project
every night at midnight UTC:

.. code-block:: yaml

   name: Nightly XNAT Export

   on:
     schedule:
       - cron: "0 0 * * *"       # run at midnight UTC daily
     workflow_dispatch:            # allow manual trigger

   env:
     XNAT_URL: ${{ secrets.XNAT_URL }}
     XNAT_USER: ${{ secrets.XNAT_USER }}
     XNAT_PASS: ${{ secrets.XNAT_PASS }}

   jobs:
     export:
       runs-on: ubuntu-latest

       steps:
         - name: Install xnatctl
           run: pip install xnatctl

         - name: Export sessions
           run: |
             PROJECT="NEUROIMAGING"
             OUTPUT="/tmp/export"
             mkdir -p "$OUTPUT"

             # List all session IDs in the project
             sessions=$(xnatctl session list -P "$PROJECT" --output json \
                 | jq -r '.[].id')

             # Download each session
             for session_id in $sessions; do
               echo "Downloading $session_id..."
               xnatctl session download -E "$session_id" \
                   --out "$OUTPUT" --quiet
             done

         - name: Upload export artifact
           uses: actions/upload-artifact@v4
           with:
             name: xnat-export
             path: /tmp/export

.. warning::

   Never hardcode credentials in your workflow file. Always use GitHub repository
   secrets (or your CI platform's equivalent) to store ``XNAT_URL``, ``XNAT_USER``,
   and ``XNAT_PASS``. If your XNAT server requires VPN access, you will also need
   to configure network connectivity in the CI runner.

.. tip::

   You can combine this workflow with the auditing script from the previous section
   to run nightly integrity checks. Add a step that runs the audit and fails the
   workflow if discrepancies are found.

**What to expect.** The workflow installs xnatctl, authenticates using the
environment variables populated from secrets, and downloads all sessions in the
project. The exported data is saved as a GitHub Actions artifact that you can
download from the workflow run page. If any step fails, the workflow reports an error
and you receive a notification through your configured GitHub alerting channels.
