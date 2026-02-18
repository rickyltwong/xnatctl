Downloading Data
================

This guide covers downloading imaging sessions, scans, and resources from XNAT
using ``xnatctl``.

Overview
--------

``xnatctl`` supports three primary download entry points:

- **Session download**: download all scans (and optionally session-level resources)
- **Scan download**: download specific scans from a session in one batch ZIP
- **Resource download**: download a specific session/scan resource as a ZIP

Tip: for exact flags and positional arguments, run ``xnatctl <command> --help``.

Download an entire session
-------------------------

Download by XNAT internal ID (recommended)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use the XNAT internal experiment ID (accession #, e.g. ``XNAT_E00001``):

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data

If you want a custom output directory name:

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --name SESSION01

Download by session label (needs project)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you only have a session label, pass ``-P/--project`` so XNAT can resolve it.
If your profile has ``default_project`` set, ``-P`` is optional:

.. code-block:: console

   $ xnatctl session download -E SESSION_LABEL -P MYPROJECT --out ./data

Parallel session downloads
~~~~~~~~~~~~~~~~~~~~~~~~~

``session download`` supports two modes controlled by ``--workers``:

- ``--workers 1``: single ZIP request for all scans (sequential, simplest)
- ``--workers >1``: parallel per-scan ZIP downloads (faster on high-latency links)

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --workers 8

Including session-level resources
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Session-level resources are optional:

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --include-resources

Extracting ZIPs
~~~~~~~~~~~~~~~

By default, downloads are saved as ZIP(s) under the session output directory.
Use ``--unzip`` to extract them, and ``--cleanup`` to remove ZIPs after extraction:

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data --unzip --cleanup

Download specific scans
-----------------------

Use ``scan download`` to fetch one or more scan IDs from a session as a single ZIP.

Download one scan by internal experiment ID
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl scan download -E XNAT_E00001 -s 1 --out ./data

Download multiple scans (or all scans)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl scan download -E XNAT_E00001 -s 1,2,3 --out ./data
   $ xnatctl scan download -E XNAT_E00001 -s '*' --out ./data

Download by session label (needs project)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl scan download -P MYPROJECT -E SESSION_LABEL -s 1 --out ./data

Download a specific resource type from scans
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl scan download -E XNAT_E00001 -s 1,2 --resource DICOM --out ./data

Extracting scan ZIPs
~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl scan download -E XNAT_E00001 -s 1 --out ./data --unzip --cleanup

Download a resource
-------------------

Use ``resource download`` to download a session- or scan-level resource as a ZIP.

Session resource
~~~~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl resource download XNAT_E00001 DICOM --file ./dicoms.zip

Scan resource
~~~~~~~~~~~~~

.. code-block:: console

   $ xnatctl resource download XNAT_E00001 DICOM --scan 1 --file ./scan1_dicoms.zip

Local extraction (offline)
--------------------------

If you downloaded ZIPs without ``--unzip``, you can extract them later:

.. code-block:: console

   $ xnatctl local extract ./data/XNAT_E00001
   $ xnatctl local extract ./data --recursive

Troubleshooting
---------------

401 Unauthorized
~~~~~~~~~~~~~~~~

Re-authenticate:

.. code-block:: console

   $ xnatctl auth login
   $ xnatctl whoami

Session label cannot be resolved
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you pass a session label to ``-E``, you must provide ``-P/--project`` or set
``default_project`` in your active profile. Without a project, ``-E`` only
accepts experiment IDs (accession numbers like ``XNAT_E00001``).

