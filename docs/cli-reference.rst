CLI Reference
=============

Global Options
--------------

All commands accept the following global options:

.. code-block:: text

   --profile TEXT    Configuration profile to use
   --output TEXT     Output format: json, table (default: table)
   --quiet           Suppress non-essential output
   --dry-run         Show what would be done without executing
   --verbose         Enable verbose logging
   --help            Show help message

Command Summary
---------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Command
     - Description
   * - ``xnatctl config``
     - Manage configuration profiles
   * - ``xnatctl auth``
     - Authentication (login/logout/status)
   * - ``xnatctl project``
     - Project operations (list/show/create)
   * - ``xnatctl subject``
     - Subject operations (list/show/rename/delete)
   * - ``xnatctl session``
     - Session operations (list/show/download/upload)
   * - ``xnatctl scan``
     - Scan operations (list/show/delete)
   * - ``xnatctl resource``
     - Resource operations (list/upload/download)
   * - ``xnatctl prearchive``
     - Prearchive management
   * - ``xnatctl pipeline``
     - Pipeline execution
   * - ``xnatctl admin``
     - Administrative operations
   * - ``xnatctl api``
     - Raw API access (escape hatch)
   * - ``xnatctl dicom``
     - DICOM utilities (requires pydicom)

config
------

Manage configuration profiles and switch between XNAT server environments.

.. code-block:: console

   $ xnatctl config init --url https://xnat.example.org
   $ xnatctl config add-profile dev --url https://xnat-dev.example.org --no-verify-ssl
   $ xnatctl config use-context dev
   $ xnatctl config show

auth
----

Authenticate against an XNAT server and manage session tokens.

.. code-block:: console

   $ xnatctl auth login
   $ xnatctl auth logout
   $ xnatctl whoami

project
-------

List, inspect, and create XNAT projects.

.. code-block:: console

   $ xnatctl project list [--output json|table]
   $ xnatctl project show PROJECT_ID
   $ xnatctl project create PROJECT_ID --name "Display Name"

subject
-------

Manage subjects within a project.

.. code-block:: console

   $ xnatctl subject list -P PROJECT_ID
   $ xnatctl subject show SUBJECT_ID -P PROJECT_ID
   $ xnatctl subject delete SUBJECT_ID -P PROJECT_ID
   $ xnatctl subject rename --help

Subject rename patterns file
~~~~~~~~~~~~~~~~~~~~~~~~~~~

You can rename subjects using a per-project patterns JSON file (first match wins).
An anonymized example is included in the repo:

.. code-block:: console

   $ xnatctl subject rename -P TESTPROJ \
       --patterns-file docs/examples/subject-rename-patterns.example.json \
       --dry-run

session
-------

List, inspect, download, and upload imaging sessions.

.. code-block:: console

   $ xnatctl session list -P PROJECT_ID
   $ xnatctl session show SESSION_ID
   $ xnatctl session download XNAT_E00001 --out ./data
   $ xnatctl session upload ./dicoms -P PROJECT_ID -S SUBJECT_ID -E SESSION_LABEL
   $ xnatctl session upload ./dicoms -P PROJECT_ID -S SUBJECT_ID -E SESSION_LABEL --gradual

Downloads and uploads run in parallel by default with progress tracking.

scan
----

Manage individual scans within a session.

.. code-block:: console

   $ xnatctl scan list XNAT_E00001
   $ xnatctl scan show XNAT_E00001 1
   $ xnatctl scan delete XNAT_E00001 --scans 1,2,3
   $ xnatctl scan download -E XNAT_E00001 -s 1 --out ./data --unzip

resource
--------

Manage file resources attached to sessions or scans.

.. code-block:: console

   $ xnatctl resource list XNAT_E00001
   $ xnatctl resource list XNAT_E00001 --scan 1
   $ xnatctl resource upload XNAT_E00001 NIFTI ./file.nii.gz
   $ xnatctl resource download XNAT_E00001 DICOM --file ./dicoms.zip
   $ xnatctl resource download XNAT_E00001 DICOM --scan 1 --file ./scan1_dicoms.zip

prearchive
----------

Manage the prearchive staging area.

.. code-block:: console

   $ xnatctl prearchive list [--project PROJECT_ID]
   $ xnatctl prearchive archive PROJECT_ID TIMESTAMP SESSION_NAME
   $ xnatctl prearchive delete PROJECT_ID TIMESTAMP SESSION_NAME --yes

pipeline
--------

Run and monitor processing pipelines.

.. code-block:: console

   $ xnatctl pipeline list [--project PROJECT_ID]
   $ xnatctl pipeline run PIPELINE_NAME --experiment XNAT_E00001
   $ xnatctl pipeline status JOB_ID

admin
-----

Administrative operations for XNAT server management.

.. code-block:: console

   $ xnatctl admin refresh-catalogs --project PROJECT_ID
   $ xnatctl admin user list
   $ xnatctl admin audit --project PROJECT_ID

api
---

Raw REST API access for operations not covered by dedicated commands.

.. code-block:: console

   $ xnatctl api get /data/projects
   $ xnatctl api post /data/projects --data '{"ID": "test"}'
   $ xnatctl api put /data/projects/test --data '{"name": "Test"}'
   $ xnatctl api delete /data/projects/test

dicom
-----

DICOM validation and inspection utilities. Requires the optional ``dicom`` extra:

.. code-block:: console

   $ pip install "xnatctl[dicom]"
   $ xnatctl dicom validate ./scan.dcm
   $ xnatctl dicom inspect ./scan.dcm
