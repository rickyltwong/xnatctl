Quick Start
===========

Initialize a configuration profile
------------------------------------

.. code-block:: console

   $ xnatctl config init

This creates ``~/.config/xnatctl/config.yaml`` with a default profile.

Authenticate
------------

.. code-block:: console

   $ xnatctl auth login

Or set environment variables:

.. code-block:: console

   $ export XNAT_URL=https://xnat.example.org
   $ export XNAT_USER=admin
   $ export XNAT_PASS=secret

List projects
-------------

.. code-block:: console

   $ xnatctl project list --output table

Download a session
------------------

.. code-block:: console

   $ xnatctl session download --project myproj --session SESS01 --dest ./data
