Quick Start
===========

This guide walks you through your first session with xnatctl. By the end, you
will have connected to your XNAT server, browsed your projects, and downloaded
imaging data.


Before You Begin
----------------

Before you start, make sure you have the following:

- **xnatctl installed** -- Follow the :doc:`installation` guide if you have not
  set it up yet. Verify with ``xnatctl --version``.
- **Access to an XNAT server** -- You need the server URL (e.g.,
  ``https://xnat.example.org``) and valid credentials (username and password).

.. note::

   If your institution provides a test or sandbox XNAT server, use that while
   you are learning. Experimenting on a non-production server lets you explore
   freely without worrying about affecting real study data.


Step 1: Configure Your Server Connection
-----------------------------------------

xnatctl stores connection details in a YAML configuration file so you do not
have to type your server URL every time you run a command. The ``config init``
command creates this file interactively.

Run the following command, replacing the URL with your own XNAT server address:

.. code-block:: console

   $ xnatctl config init --url https://xnat.example.org

The command prompts you for your username and password, then writes a
configuration file at ``~/.config/xnatctl/config.yaml``. The resulting file
looks like this:

.. code-block:: yaml

   active_profile: default
   profiles:
     default:
       url: https://xnat.example.org
       username: youruser
       verify_ssl: true

Your password is not stored in the configuration file. xnatctl will prompt for
it when needed, or you can supply it through environment variables.

.. tip::

   If you prefer not to use a configuration file (for example, in a CI
   pipeline), you can set environment variables instead:

   .. code-block:: console

      $ export XNAT_URL=https://xnat.example.org
      $ export XNAT_USER=youruser
      $ export XNAT_PASS=yourpassword

   Environment variables take precedence over values in the configuration file.
   See :doc:`configuration` for the full priority order.


Step 2: Log In
--------------

xnatctl uses session tokens to authenticate with your XNAT server. When you log
in, xnatctl exchanges your credentials for a temporary JSESSIONID token, caches
it locally, and reuses it for subsequent commands. If the token expires, xnatctl
re-authenticates automatically so you can run long sequences of commands without
interruption.

Log in and verify your identity:

.. code-block:: console

   $ xnatctl auth login

You should see a confirmation that authentication succeeded. Then confirm which
user is associated with the current session:

.. code-block:: console

   $ xnatctl whoami

Expected output:

.. code-block:: text

   User:    youruser
   Server:  https://xnat.example.org

.. note::

   For non-interactive environments like CI pipelines or cron jobs, set the
   ``XNAT_USER`` and ``XNAT_PASS`` environment variables (or ``XNAT_TOKEN`` if
   you already have a session token). xnatctl will authenticate automatically
   without prompting for input.


Step 3: Browse Your Data
------------------------

XNAT organizes data in a hierarchy: projects contain subjects, subjects contain
sessions (imaging visits), and sessions contain scans. You can explore each
level from the command line. For a deeper explanation of this hierarchy, see
:doc:`concepts`.

Start by listing the projects you have access to:

.. code-block:: console

   $ xnatctl project list

Example output:

.. code-block:: text

   ID            Name                  PI            Subjects
   BRAINAGING    Brain Aging Study     Dr. Smith     42
   CONNECTIVITY  Connectivity Atlas    Dr. Jones     128
   PHANTOMQA     Phantom QA            Dr. Lee       3

Next, list the subjects within a specific project using the ``-P`` flag:

.. code-block:: console

   $ xnatctl subject list -P BRAINAGING

Example output:

.. code-block:: text

   ID              Label       Sessions
   XNAT_S00001     SUB001      2
   XNAT_S00002     SUB002      1
   XNAT_S00003     SUB003      3

Finally, list the imaging sessions in that project:

.. code-block:: console

   $ xnatctl session list -P BRAINAGING

Example output:

.. code-block:: text

   ID              Label                  Subject   Modality   Date
   XNAT_E00001     SUB001_MR_20240115     SUB001    MR         2024-01-15
   XNAT_E00002     SUB001_MR_20240715     SUB001    MR         2024-07-15
   XNAT_E00003     SUB002_MR_20240220     SUB002    MR         2024-02-20

Each command supports ``--output json`` for machine-readable output or
``--quiet`` for ID-only output suitable for shell pipelines.


Step 4: Download a Session
---------------------------

Now that you can browse your data, try downloading a session to your local
machine. The ``session download`` command pulls all scans and resources for a
given experiment.

You identify the session with the ``-E`` flag, which accepts either an
accession ID (like ``XNAT_E00001``) or a label (like ``SUB001_MR_20240115``).
When you use a label, you must also provide the ``-P`` flag so xnatctl knows
which project to search. Accession IDs are globally unique and do not require a
project. For a full explanation of this distinction, see :ref:`ids-vs-labels`.

Download a session by its accession ID:

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data

xnatctl shows a progress bar as it downloads each scan:

.. code-block:: text

   Downloading XNAT_E00001 (SUB001_MR_20240115)...
   [1/3] Scan 1 - T1w MPRAGE          [=============================] 100%  156 MB
   [2/3] Scan 2 - BOLD resting state   [=============================] 100%  892 MB
   [3/3] Scan 3 - DWI                  [=============================] 100%  445 MB
   Download complete: 3 scans, 1.49 GB total

After the download finishes, you will find the data organized on disk by scan
number and resource type:

.. code-block:: text

   data/
   +-- XNAT_E00001/
       +-- scans/
           +-- 1-T1w_MPRAGE/
           |   +-- DICOM/
           |       +-- 00001.dcm
           |       +-- 00002.dcm
           |       +-- ...
           +-- 2-BOLD_resting_state/
           |   +-- DICOM/
           |       +-- ...
           +-- 3-DWI/
               +-- DICOM/
                   +-- ...

.. tip::

   By default, scans are downloaded as ZIP archives that are then extracted. If
   you want to skip extraction and keep the raw ZIP files, omit the ``--unzip``
   flag. Conversely, ``--unzip`` (the default) automatically extracts archives
   after download so you can work with the files immediately.


Step 5: Upload Data (Preview)
-----------------------------

Uploading imaging data to XNAT is a common workflow, but it involves choices
about destination project, subject assignment, and whether data should land in
the prearchive for review or go directly into the archive. xnatctl supports
both REST-based file uploads and DICOM C-STORE for scanner integration.

Here is a basic example that uploads a directory of DICOM files:

.. code-block:: console

   $ xnatctl session upload ./dicoms -P BRAINAGING -S SUB001 -E SESSION01

The upload guide covers all of the options in detail, including parallel
workers, direct archive mode, and handling upload errors. See
:doc:`uploading` for the full walkthrough.


Using a Different Profile
-------------------------

If you work with multiple XNAT servers (for example, development and
production), you can store a separate profile for each one in your configuration
file. Use the ``--profile`` flag to select a profile for a single command:

.. code-block:: console

   $ xnatctl --profile staging project list

To switch your active profile persistently so that all subsequent commands use
it by default, run:

.. code-block:: console

   $ xnatctl config use-context staging

See :doc:`configuration` for instructions on adding and managing profiles.


Next Steps
----------

You have configured xnatctl, authenticated with your XNAT server, browsed the
data hierarchy, and downloaded a session. From here, you can explore the rest of
the documentation:

- :doc:`configuration` -- Set up multiple profiles, default projects, and
  environment variable overrides.
- :doc:`concepts` -- Understand the XNAT data model, the prearchive, and how
  IDs and labels work.
- :doc:`cli-reference` -- Full reference for every command and flag.
- :doc:`downloading` -- Advanced download options including scan filtering,
  checksum verification, and parallel workers.
- :doc:`uploading` -- Upload DICOM files, manage the prearchive, and configure
  direct archive mode.
- :doc:`workflows` -- End-to-end recipes for common tasks like bulk downloads,
  data migration, and automated QA.
