xnatctl
=======

**xnatctl** is a modern command-line interface for administering
`XNAT <https://xnat.org>`_ [#xnat]_ neuroimaging servers. If you manage imaging
studies, move DICOM data between systems, or automate research workflows
against XNAT's REST API, xnatctl gives you a single, consistent tool to do
it all from your terminal.

The CLI is organized around the resources you already work with -- projects,
subjects, sessions, scans, and files -- so commands read like plain English:
``xnatctl session download -E XNAT_E00001``. Every command supports JSON,
table, and quiet output modes, making xnatctl equally useful for interactive
exploration and scripted pipelines. Profile-based configuration lets you
switch between XNAT instances (development, staging, production) with a
single ``--profile`` flag.

Whether you are a neuroimaging researcher downloading a handful of scans or a
platform engineer bulk-loading thousands of DICOM series, xnatctl is designed
to stay out of your way while keeping you informed about what is happening.

.. [#xnat] XNAT is an open source project produced by NRG at the Washington University School of Medicine. See https://xnat.org/.


Feature Highlights
------------------

- **Resource-centric commands** -- Every command follows the pattern
  ``xnatctl <resource> <action>``, so you can guess the syntax for new
  resources once you know one.

- **Profile-based configuration** -- Store credentials and defaults for
  multiple XNAT servers in a single YAML file; switch contexts without
  editing environment variables.

- **Consistent output formats** -- Pass ``--output json`` for machine-readable
  output, ``--output table`` for human-friendly tables, or ``--quiet`` to emit
  only resource IDs (ideal for shell pipelines).

- **Parallel batch operations** -- Uploads and downloads run in parallel by
  default with configurable worker counts and real-time progress bars, so
  large transfers finish faster without extra scripting.

- **Session authentication with token caching** -- Log in once with
  ``xnatctl auth login``; the session token is cached locally and refreshed
  automatically, including transparent re-authentication on expiry.

- **Pure HTTP with httpx** -- Existing Python libraries like
  `pyxnat <https://pyxnat.github.io/pyxnat/>`_ and
  `xnatpy <https://xnat.readthedocs.io/>`_ inspired this project, but they are
  excellent *Python libraries* intended to be imported into your own code. xnatctl
  exists for the complementary use case: a **CLI-first** workflow where you want to
  explore resources, automate API interactions from shell scripts, and run common
  administrative tasks without writing a bespoke Python program.

  The command structure and UX borrow from tools like ``kubectl`` and ``airflowctl``:
  resource-centric subcommands, consistent flags, and output you can read as a human
  or pipe into other tools. xnatctl is a standalone CLI that talks directly to XNAT's
  REST API with automatic retries and exponential backoff -- no Python environment
  required.


Quick Example
-------------

Here are a few commands that illustrate the breadth of what you can do.

List every project on the server as a formatted table:

.. code-block:: console

   $ xnatctl project list --output table

Download all scans for an experiment to a local directory:

.. code-block:: console

   $ xnatctl session download -P myproject -E SESSION_LABEL --out ./data

Upload a batch of DICOM files into a subject, with parallel workers:

.. code-block:: console

   $ xnatctl session upload-dicom -P myproject -S SUBJ01 ./dicoms/ --workers 4

Trigger a catalog refresh for a specific project (admin operation):

.. code-block:: console

   $ xnatctl admin refresh-catalogs --project myproject

.. tip::

   Run ``xnatctl --help`` or ``xnatctl <resource> --help`` at any time to see
   available commands, options, and usage examples.


.. toctree::
   :maxdepth: 2
   :caption: Getting Started

   installation
   concepts
   quickstart
   configuration

.. toctree::
   :maxdepth: 2
   :caption: User Guide

   cli-reference
   skill
   downloading
   uploading
   dicom
   workflows
   administration
   xnat-compatibility

.. toctree::
   :maxdepth: 2
   :caption: Development

   contributing
   changelog

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api/core
   api/models
   api/services
