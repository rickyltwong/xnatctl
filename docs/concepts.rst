Key Concepts
============

XNAT is an open-source platform for managing, exploring, and sharing neuroimaging
data. If you are a researcher, data manager, or system administrator working with
brain imaging studies, XNAT is likely the system that stores your MRI, PET, or CT
data. Understanding how XNAT organizes data is essential to using xnatctl effectively.

This page is a reference you can return to whenever you need a refresher on XNAT
terminology or how xnatctl maps commands to the underlying data model. If you are
new to XNAT, read through this page once before diving into the
:doc:`quickstart`.


The XNAT Data Hierarchy
-----------------------

XNAT organizes neuroimaging data in a strict hierarchy:

.. code-block:: text

   Project
   +-- Subject
       +-- Session (Experiment)
           +-- Scan
               +-- Resource
                   +-- File

The sections below explain what each level represents and how you interact with it
through xnatctl.


Projects
^^^^^^^^

A **project** is the top-level organizational unit in XNAT. It typically corresponds
to a research study, clinical trial, or grant. Projects control access permissions --
team members are granted roles (Owner, Member, Collaborator) at the project level.
Every subject, session, and scan belongs to exactly one project.

Each project has an **ID** (e.g., ``MYPROJECT``) that you assigned when creating it.
This ID is used throughout xnatctl as the ``-P`` / ``--project`` flag:

.. code-block:: console

   $ xnatctl project list
   $ xnatctl project show MYPROJECT


Subjects
^^^^^^^^

A **subject** represents a single participant or patient in your study. Subjects have
a human-readable label (e.g., ``SUB001``) and a system-generated accession ID. A
subject can have multiple imaging sessions collected over time, which is common in
longitudinal studies.

.. code-block:: console

   $ xnatctl subject list --project MYPROJECT
   $ xnatctl subject show --project MYPROJECT SUB001


Sessions (Experiments)
^^^^^^^^^^^^^^^^^^^^^^

A **session** (also called an **experiment** in XNAT terminology) represents a single
visit to the scanner. For example, a subject's baseline MRI appointment would be one
session, and their 6-month follow-up would be another. Each session has a modality
type such as MR, PET, CT, or EEG.

You can list sessions in a project and filter by subject or modality:

.. code-block:: console

   $ xnatctl session list -P MYPROJECT
   $ xnatctl session list -P MYPROJECT --subject SUB001
   $ xnatctl session list -P MYPROJECT --modality MR

Sessions have both an **accession ID** (e.g., ``XNAT_E00001``) and a **label** (e.g.,
``SUB001_MR_20240115``). See :ref:`ids-vs-labels` below for how these differ.


Scans
^^^^^

A **scan** is a single acquisition within a session. An MRI session might contain a
T1-weighted anatomical scan, a functional BOLD scan, and a diffusion scan. Each scan
has a numeric ID (assigned by the scanner or XNAT) and a series description.

.. code-block:: console

   $ xnatctl scan list -E XNAT_E00001
   $ xnatctl scan download -E XNAT_E00001 -s 1,2,3 --out ./data


Resources
^^^^^^^^^

A **resource** is a labeled collection of files attached to a scan (or session). The
most common resource is ``DICOM``, which holds the raw imaging files from the scanner.
Other resources include ``NIFTI`` (converted images), ``SNAPSHOTS`` (preview
thumbnails), or custom labels for processed outputs.

.. code-block:: console

   $ xnatctl resource list -E XNAT_E00001 --scan 1


.. _ids-vs-labels:

IDs vs Labels
-------------

XNAT assigns two types of identifiers to sessions, subjects, and other objects.
Understanding the difference prevents confusion when constructing commands.

**Accession IDs** are system-generated, globally unique identifiers that XNAT creates
automatically when data is imported (e.g., ``XNAT_E00001`` for experiments,
``XNAT_S00001`` for subjects). Because they are globally unique, you can look up any
object by accession ID without specifying which project it belongs to.

**Labels** are human-assigned, descriptive names like ``SUB001_MR_20240115``. Labels
are unique within a project but not globally -- two different projects could each
have a session labeled ``BASELINE_MR``.

This distinction matters when you use the ``-E`` (experiment) flag in ``session`` and
``scan`` commands:

- **``-E`` alone** (no ``-P`` flag): xnatctl sends the value directly to
  ``/data/experiments/{value}``. XNAT expects a globally unique accession ID here, so
  you must pass something like ``XNAT_E00001``.

- **``-E`` with ``-P``**: xnatctl sends the value to
  ``/data/projects/{project}/experiments/{value}``. Because the project scopes the
  lookup, XNAT accepts either an accession ID or a label.

Here are both approaches in practice:

.. code-block:: console

   # Using an accession ID (no project needed)
   $ xnatctl session show -E XNAT_E00001

   # Using a label (project required for disambiguation)
   $ xnatctl session show -E SUB001_MR_20240115 -P MYPROJECT

.. tip::

   If you primarily work within a single project, set ``default_project`` in your
   profile configuration. This lets you use session labels with ``-E`` without
   typing ``-P`` every time:

   .. code-block:: yaml

      profiles:
        production:
          url: https://xnat.example.org
          default_project: MYPROJECT

   With this setting, ``xnatctl session show -E SUB001_MR_20240115`` automatically
   resolves the label within ``MYPROJECT``. See :doc:`configuration` for full
   profile setup details.


The Prearchive
--------------

The **prearchive** is a temporary staging area where uploaded imaging data lands
before it enters the permanent archive. Think of it as an inbox for your XNAT
server: data arrives, sits in the prearchive, and waits for someone to review and
approve it.

The prearchive exists because neuroimaging data often needs quality checks before
it becomes part of the official dataset. A research coordinator might verify that
the correct subject ID was assigned, that all expected scans are present, or that
no identifying information leaked into the DICOM headers. Archiving from the
prearchive is an explicit action, which prevents accidental data from silently
entering your study.

You can bypass the prearchive entirely by using **direct archive** mode during
upload (``--direct-archive``). This sends data straight into the permanent archive,
which is useful for automated pipelines where data has already been validated.

You can list and archive prearchive entries with the ``prearchive`` commands:

.. code-block:: console

   $ xnatctl prearchive list --project MYPROJECT
   $ xnatctl prearchive archive --project MYPROJECT --session SESS001

.. tip::

   If your uploaded data does not appear in ``session list``, check the prearchive.
   Data may be waiting for review before it enters the archive:

   .. code-block:: console

      $ xnatctl prearchive list --project MYPROJECT


Authentication
--------------

xnatctl authenticates with your XNAT server using session tokens. When you run
``auth login``, xnatctl sends your credentials to the server and receives a
**JSESSIONID** token -- a temporary key that proves your identity for subsequent
requests. This token is cached locally at ``~/.config/xnatctl/.session`` (with
restricted file permissions) and reused automatically for subsequent commands.

Session tokens expire after a period of inactivity (15 minutes by default). When
xnatctl detects an expired session, it re-authenticates using your stored credentials
and retries the request transparently, so you can run long sequences of commands
without interruption.

.. code-block:: console

   $ xnatctl auth login
   $ xnatctl whoami

See :doc:`configuration` for details on credential setup.
