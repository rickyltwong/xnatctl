Configuration
=============

Config file
-----------

xnatctl uses a YAML configuration file located at ``~/.config/xnatctl/config.yaml``.

.. code-block:: yaml

   current-context: default

   profiles:
     default:
       url: https://xnat.example.org
       username: admin
       verify_ssl: true
       timeout: 30
       default_project: myproj

     staging:
       url: https://xnat-staging.example.org
       username: admin
       verify_ssl: false
       timeout: 60

Profile fields
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

Environment variables
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
     - Session token (skips login)
   * - ``XNAT_PROFILE``
     - Active profile name

Credential priority
-------------------

1. CLI arguments (``--url``, ``--user``, ``--password``)
2. Environment variables
3. Profile configuration
4. Interactive prompt
