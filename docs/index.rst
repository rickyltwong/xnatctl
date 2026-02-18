xnatctl
=======

A modern CLI for XNAT neuroimaging server administration.

**xnatctl** provides resource-centric commands with consistent output formats,
parallel operations, and profile-based configuration for managing XNAT servers.

Features
--------

- **Resource-centric commands**: ``xnatctl <resource> <action> [args]``
- **Profile-based configuration**: YAML config with multiple server profiles
- **Consistent output**: ``--output json|table`` and ``--quiet`` on all commands
- **Parallel operations**: Batch uploads/downloads with progress tracking
- **Session authentication**: Token caching with ``auth login``
- **Pure HTTP**: Direct REST API calls with httpx (no pyxnat dependency)

.. code-block:: console

   $ xnatctl project list --output table
   $ xnatctl session download -E XNAT_E00001 --out ./data
   $ xnatctl admin refresh-catalogs --project myproj

.. toctree::
   :maxdepth: 2
   :caption: Getting Started

   installation
   quickstart
   configuration

.. toctree::
   :maxdepth: 2
   :caption: User Guide

   cli-reference
   uploading
   downloading
   workflows

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
