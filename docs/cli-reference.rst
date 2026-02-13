CLI Reference
=============

Global options
--------------

All commands accept the following global options:

.. code-block:: text

   --profile TEXT    Configuration profile to use
   --output TEXT     Output format: json, table (default: table)
   --quiet           Suppress non-essential output
   --dry-run         Show what would be done without executing
   --verbose         Enable verbose logging
   --help            Show help message

Commands
--------

project
~~~~~~~

.. code-block:: console

   $ xnatctl project list [--output json|table]
   $ xnatctl project show PROJECT_ID
   $ xnatctl project create PROJECT_ID --name "Display Name"

subject
~~~~~~~

.. code-block:: console

   $ xnatctl subject list --project PROJECT_ID
   $ xnatctl subject show --project PROJECT_ID --subject SUBJECT_ID
   $ xnatctl subject rename --project PROJECT_ID --subject OLD_ID --new-label NEW_ID
   $ xnatctl subject delete --project PROJECT_ID --subject SUBJECT_ID

session
~~~~~~~

.. code-block:: console

   $ xnatctl session list --project PROJECT_ID
   $ xnatctl session show --project PROJECT_ID --session SESSION_ID
   $ xnatctl session download --project PROJECT_ID --session SESSION_ID --dest ./data
   $ xnatctl session upload --project PROJECT_ID --subject SUBJECT_ID --files ./dicoms/

scan
~~~~

.. code-block:: console

   $ xnatctl scan list --project PROJECT_ID --session SESSION_ID
   $ xnatctl scan delete --project PROJECT_ID --session SESSION_ID --scan SCAN_ID

resource
~~~~~~~~

.. code-block:: console

   $ xnatctl resource list --project PROJECT_ID --session SESSION_ID
   $ xnatctl resource upload --project PROJECT_ID --session SESSION_ID --resource RES --files ./data/
   $ xnatctl resource download --project PROJECT_ID --session SESSION_ID --resource RES --dest ./out/

prearchive
~~~~~~~~~~

.. code-block:: console

   $ xnatctl prearchive list [--project PROJECT_ID]
   $ xnatctl prearchive archive --project PROJECT_ID --session SESSION_ID
   $ xnatctl prearchive delete --project PROJECT_ID --session SESSION_ID

pipeline
~~~~~~~~

.. code-block:: console

   $ xnatctl pipeline list --project PROJECT_ID
   $ xnatctl pipeline run --project PROJECT_ID --session SESSION_ID --pipeline PIPELINE_ID
   $ xnatctl pipeline status --project PROJECT_ID --pipeline-run RUN_ID

admin
~~~~~

.. code-block:: console

   $ xnatctl admin refresh-catalogs --project PROJECT_ID
   $ xnatctl admin user list
   $ xnatctl admin audit --project PROJECT_ID

api
~~~

.. code-block:: console

   $ xnatctl api get /data/projects
   $ xnatctl api post /data/projects --data '{"ID": "test"}'
   $ xnatctl api put /data/projects/test --data '{"name": "Test"}'
   $ xnatctl api delete /data/projects/test
