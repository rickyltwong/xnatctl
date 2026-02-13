Configuration
=============

Config File
-----------

xnatctl uses a YAML configuration file located at ``~/.config/xnatctl/config.yaml``.

.. code-block:: yaml

   default_profile: production
   output_format: table

   profiles:
     production:
       url: https://xnat.example.org
       username: myuser
       password: mypassword
       verify_ssl: true
       timeout: 30
       default_project: MYPROJECT

     development:
       url: https://xnat-dev.example.org
       verify_ssl: false

Managing Profiles
-----------------

.. code-block:: console

   $ xnatctl config init --url https://xnat.example.org
   $ xnatctl config add-profile dev --url https://xnat-dev.example.org --no-verify-ssl
   $ xnatctl config use-context dev
   $ xnatctl config show

Profile Fields
--------------

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Field
     - Default
     - Description
   * - ``url``
     - (required)
     - XNAT server base URL
   * - ``username``
     - (optional)
     - Username for authentication
   * - ``password``
     - (optional)
     - Password (prefer env vars instead)
   * - ``verify_ssl``
     - ``true``
     - Whether to verify SSL certificates
   * - ``timeout``
     - ``30``
     - Request timeout in seconds
   * - ``default_project``
     - (optional)
     - Default project ID for commands

Environment Variables
---------------------

Environment variables override profile values:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Variable
     - Description
   * - ``XNAT_URL``
     - Server URL
   * - ``XNAT_USER``
     - Username
   * - ``XNAT_PASS``
     - Password
   * - ``XNAT_TOKEN``
     - Session token (skips login, takes precedence over cached sessions and username/password)
   * - ``XNAT_PROFILE``
     - Active profile name (overrides ``default_profile`` in config.yaml)

.. note::

   Use ``XNAT_USER``/``XNAT_PASS`` for non-interactive auth in CI pipelines and
   scripts. ``XNAT_URL`` and ``XNAT_PROFILE`` override values from
   ``config.yaml`` for the current shell session.

Credential Priority
-------------------

Credentials are resolved in this order (highest to lowest):

1. CLI arguments (``--username``, ``--password``)
2. Environment variables (``XNAT_USER``, ``XNAT_PASS``)
3. Profile configuration (``username``, ``password`` in config.yaml)
4. Interactive prompt

Authentication Flow
-------------------

.. code-block:: console

   $ xnatctl auth login
   $ xnatctl whoami

Session tokens are cached under ``~/.config/xnatctl/.session`` and reused
automatically until they expire.
