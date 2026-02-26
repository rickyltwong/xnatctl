DICOM Utilities
===============

xnatctl includes a set of local DICOM utilities for validating, inspecting,
and anonymizing DICOM files before uploading them to XNAT. These commands
operate entirely offline -- they read and write files on your local filesystem
and do not require an XNAT server connection or authentication.

The DICOM utilities are provided as an **optional extra** because they depend on
`pydicom <https://pydicom.github.io/>`_, which is not needed for the core CLI.
This keeps the base install lightweight for users who only interact with
XNAT's REST API.


Installation
------------

Install the ``dicom`` extra alongside the base package:

.. code-block:: console

   $ pip install "xnatctl[dicom]"

Or with uv:

.. code-block:: console

   $ uv pip install "xnatctl[dicom]"

This installs the base ``xnatctl`` package plus two additional dependencies:

- **pydicom** (>=2.4.0) -- DICOM file parsing and manipulation
- **pynetdicom** (>=2.0.0) -- DICOM network protocol (used by
  ``session upload-dicom``)

.. note::

   ``pip install "xnatctl[dicom]"`` installs the full ``xnatctl`` CLI plus the
   DICOM extras. You cannot install the extras without the base package -- the
   ``[dicom]`` syntax is a Python packaging convention for optional dependency
   groups.

If you installed xnatctl as a standalone binary, the DICOM utilities are
**not available**. The binary bundles a fixed set of dependencies determined
at build time and does not support extras. Use the Python package install
method to access these commands.

You can verify the extra is installed by running:

.. code-block:: console

   $ xnatctl dicom --help

If pydicom is not installed, xnatctl prints a warning with install
instructions.


Commands
--------

All DICOM commands live under the ``xnatctl dicom`` group. They share common
options for output format (``--output json|table``) and operate on local file
paths.


.. _dicom-validate:

dicom validate
^^^^^^^^^^^^^^

Validate DICOM files by checking for required tags and structural integrity.
This is useful for catching malformed files before uploading to XNAT, where
import failures can be harder to diagnose.

**Usage:**

.. code-block:: console

   $ xnatctl dicom validate PATH [OPTIONS]

**Arguments:**

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Argument
     - Description
   * - ``PATH``
     - A single DICOM file or a directory of files to validate.

**Options:**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Option
     - Description
   * - ``-r`` / ``--recursive``
     - Search directories recursively for files.
   * - ``-o`` / ``--output``
     - Output format: ``json`` or ``table`` (default: ``table``).
   * - ``-q`` / ``--quiet``
     - Only print invalid files and their errors.

**Required tags checked:**

The validator checks for these tags that are essential for XNAT import:

- ``PatientID`` (0010,0020)
- ``PatientName`` (0010,0010)
- ``StudyInstanceUID`` (0020,000D)
- ``SeriesInstanceUID`` (0020,000E)
- ``SOPInstanceUID`` (0008,0018)
- ``Modality`` (0008,0060)

Files missing any of these tags, or where any of these values are blank/empty, are reported as
invalid. The validator also warns when a file contains more than 100 private tags, which can cause
performance issues during import.

**Examples:**

Validate a single file:

.. code-block:: console

   $ xnatctl dicom validate /path/to/scan.dcm

Validate an entire directory tree and show only failures:

.. code-block:: console

   $ xnatctl dicom validate /path/to/dicoms -r -q

Get machine-readable output for scripting:

.. code-block:: console

   $ xnatctl dicom validate /path/to/dicoms -r -o json


.. _dicom-inspect:

dicom inspect
^^^^^^^^^^^^^

Inspect DICOM headers for a single file. By default, the command displays a
curated set of common tags (patient, study, series, and equipment metadata).
You can also request specific tags by name or include private tags.

**Usage:**

.. code-block:: console

   $ xnatctl dicom inspect FILE [OPTIONS]

**Arguments:**

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Argument
     - Description
   * - ``FILE``
     - Path to the DICOM file to inspect.

**Options:**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Option
     - Description
   * - ``-t`` / ``--tag TEXT``
     - Show only specific tags by keyword name (repeatable). Example:
       ``--tag PatientID --tag Modality``.
   * - ``--private``
     - Include private (vendor-specific) tags in the output.
   * - ``-o`` / ``--output``
     - Output format: ``json`` or ``table`` (default: ``table``).

**Default tags displayed** (when no ``--tag`` is specified):

PatientID, PatientName, PatientBirthDate, PatientSex, StudyInstanceUID,
StudyDate, StudyTime, StudyDescription, SeriesInstanceUID, SeriesNumber,
SeriesDescription, SOPInstanceUID, SOPClassUID, Modality, Manufacturer,
ManufacturerModelName, InstitutionName, StationName, AccessionNumber.

**Examples:**

Inspect common headers:

.. code-block:: console

   $ xnatctl dicom inspect /path/to/scan.dcm

Check specific tags:

.. code-block:: console

   $ xnatctl dicom inspect /path/to/scan.dcm --tag PatientID --tag StudyDate

Include private/vendor tags:

.. code-block:: console

   $ xnatctl dicom inspect /path/to/scan.dcm --private

Output as JSON for piping:

.. code-block:: console

   $ xnatctl dicom inspect /path/to/scan.dcm -o json | jq '.PatientID'


.. _dicom-list-tags:

dicom list-tags
^^^^^^^^^^^^^^^

List every tag present in a DICOM file, showing the tag number, value
representation (VR), keyword name, and value (truncated to 50 characters).
This is more exhaustive than ``inspect`` and is useful for understanding the
full metadata content of a file.

**Usage:**

.. code-block:: console

   $ xnatctl dicom list-tags FILE [OPTIONS]

**Arguments:**

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Argument
     - Description
   * - ``FILE``
     - Path to the DICOM file.

**Options:**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Option
     - Description
   * - ``--private``
     - Include private tags in the listing.
   * - ``-o`` / ``--output``
     - Output format: ``json`` or ``table`` (default: ``table``).

**Output columns** (table mode):

.. list-table::
   :header-rows: 1
   :widths: 15 15 25 45

   * - tag
     - vr
     - name
     - value
   * - ``(0010,0020)``
     - ``LO``
     - ``PatientID``
     - ``SUBJ001``

**Examples:**

List all standard tags:

.. code-block:: console

   $ xnatctl dicom list-tags /path/to/scan.dcm

Include private tags:

.. code-block:: console

   $ xnatctl dicom list-tags /path/to/scan.dcm --private

Export as JSON:

.. code-block:: console

   $ xnatctl dicom list-tags /path/to/scan.dcm -o json


.. _dicom-anonymize:

dicom anonymize
^^^^^^^^^^^^^^^

Remove or replace identifying information in DICOM files. The command strips
a fixed set of identifying tags and optionally replaces patient identifiers
with values you provide. Use this to de-identify data before uploading to a
shared XNAT instance or distributing to collaborators.

**Usage:**

.. code-block:: console

   $ xnatctl dicom anonymize INPUT_PATH OUTPUT_PATH [OPTIONS]

**Arguments:**

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Argument
     - Description
   * - ``INPUT_PATH``
     - Source DICOM file or directory.
   * - ``OUTPUT_PATH``
     - Destination file or directory for anonymized output.

**Options:**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Option
     - Description
   * - ``--patient-id TEXT``
     - Replace PatientID with this value.
   * - ``--patient-name TEXT``
     - Replace PatientName with this value.
   * - ``--remove-private``
     - Remove all private (vendor-specific) tags.
   * - ``-r`` / ``--recursive``
     - Process directories recursively, preserving the subdirectory structure
       in the output path.
   * - ``--dry-run``
     - Preview what would be anonymized without writing files.

**Tags removed by default:**

The following identifying tags are deleted from every processed file:

- PatientBirthDate
- PatientAddress
- InstitutionName
- InstitutionAddress
- ReferringPhysicianName
- PerformingPhysicianName
- OperatorsName

.. note::

   This is a basic anonymization suitable for many research workflows but does
   not implement a full DICOM de-identification profile (e.g., DICOM PS3.15
   Annex E). For clinical or regulatory use cases, consider dedicated
   de-identification tools and review your institution's policies.

**Examples:**

Anonymize a single file with a new patient ID:

.. code-block:: console

   $ xnatctl dicom anonymize input.dcm output.dcm --patient-id ANON001

Anonymize a directory tree, removing private tags:

.. code-block:: console

   $ xnatctl dicom anonymize /scanner/export /cleaned -r --remove-private

Preview what would happen without writing:

.. code-block:: console

   $ xnatctl dicom anonymize /input /output -r --dry-run


Typical Workflows
-----------------

Pre-upload validation
^^^^^^^^^^^^^^^^^^^^^

Validate DICOM files before uploading to catch issues locally where they are
easier to fix:

.. code-block:: console

   $ xnatctl dicom validate /scanner/export -r -q
   $ xnatctl session upload /scanner/export -P MYPROJ -S SUB001 -E SESS001

Anonymize-then-upload
^^^^^^^^^^^^^^^^^^^^^

Strip identifying information, validate the result, then upload:

.. code-block:: console

   $ xnatctl dicom anonymize /raw /cleaned -r \
       --patient-id ANON001 --remove-private
   $ xnatctl dicom validate /cleaned -r
   $ xnatctl session upload /cleaned -P MYPROJ -S ANON001 -E SESS001

Inspect before archiving
^^^^^^^^^^^^^^^^^^^^^^^^

Check headers on a file that failed import to understand what went wrong:

.. code-block:: console

   $ xnatctl dicom inspect /failed/scan.dcm
   $ xnatctl dicom inspect /failed/scan.dcm --tag PatientID --tag Modality


Relationship to Server-Side Commands
-------------------------------------

The ``dicom`` commands are purely local and do not communicate with XNAT.
For uploading DICOM data to a server, use:

- ``xnatctl session upload`` -- Upload via XNAT's REST Import API (HTTP)
- ``xnatctl session upload-dicom`` -- Upload via DICOM C-STORE network
  protocol (requires the ``[dicom]`` extra for pynetdicom)

See :doc:`uploading` for full details on upload methods, tuning, and
post-upload verification.
