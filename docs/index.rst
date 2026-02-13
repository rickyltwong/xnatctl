xnatctl
=======

A modern CLI for XNAT neuroimaging server administration.

**xnatctl** provides resource-centric commands with consistent output formats,
parallel operations, and profile-based configuration for managing XNAT servers.

.. code-block:: console

   $ xnatctl project list --output table
   $ xnatctl session download --project myproj --session SESS01
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
