Quick Start
===========

Initialize a Configuration
--------------------------

.. code-block:: console

   $ xnatctl config init --url https://xnat.example.org

This creates ``~/.config/xnatctl/config.yaml`` with a default profile.

Authenticate
------------

.. code-block:: console

   $ xnatctl auth login

Or set environment variables for non-interactive use (CI, scripts):

.. code-block:: console

   $ export XNAT_URL=https://xnat.example.org
   $ export XNAT_USER=admin
   $ export XNAT_PASS=secret

Check your current session:

.. code-block:: console

   $ xnatctl whoami

Session tokens are cached under ``~/.config/xnatctl/.session`` and used
automatically for subsequent commands.

List Projects
-------------

.. code-block:: console

   $ xnatctl project list
   $ xnatctl project list --output table
   $ xnatctl project list --output json

Show Project Details
--------------------

.. code-block:: console

   $ xnatctl project show MYPROJECT

List Subjects and Sessions
--------------------------

.. code-block:: console

   $ xnatctl subject list --project MYPROJECT
   $ xnatctl session list --project MYPROJECT

Download a Session
------------------

.. code-block:: console

   $ xnatctl session download -E XNAT_E00001 --out ./data

Upload DICOM Files
------------------

.. code-block:: console

   $ xnatctl session upload ./dicoms -P MYPROJECT -S SUB01 -E SESSION01

Use a Different Profile
-----------------------

.. code-block:: console

   $ xnatctl --profile staging project list

Or switch the active profile persistently:

.. code-block:: console

   $ xnatctl config use-context staging

Raw API Access
--------------

For operations not covered by dedicated commands, use the escape hatch:

.. code-block:: console

   $ xnatctl api get /data/projects
   $ xnatctl api post /data/projects --data '{"ID": "test"}'
