Adding a Command
=================

This page is a self-contained recipe: follow it top to bottom and you will
end with a new command that fits the codebase's layering, tests, and
documentation conventions without having to reverse-engineer them from
scratch or read anything else first.

xnatctl has three layers. A Click command in ``xnatctl/cli/`` parses
arguments and formats output; it delegates to a service in
``xnatctl/services/``, which builds the REST path and calls the shared
``XNATClient`` (``xnatctl/core/client.py``, httpx-based) to talk to the
server. Pydantic models in ``xnatctl/models/`` give some of those REST
responses a typed shape. The rest of this page walks through each layer in
the order you'll touch them.

The worked example is ``project grant`` / ``project revoke`` / ``project
users`` / ``project access``, four small commands added together for
project membership management. They were chosen because they are recent,
small enough to read in full, and between them touch every case this
recipe covers: a mutating service call, an unconditionally destructive
command (``@confirm_destructive``), a conditionally-destructive one
(``@confirm_destructive_when``), and a read-only listing. Every code excerpt
below is quoted from the current source, not paraphrased or reconstructed
from memory. Where an excerpt is shortened, the elision is explicit: a
docstring abridged to its one-line summary, or an option list or command
body collapsed to ``...`` when only the decorator stack is the point --
what code IS shown is shown unaltered. File paths may drift as the codebase
evolves, but re-reading the cited file will always show you the real, current
version.


Step 1: The Model
------------------

Models that represent a well-known XNAT resource -- ``Project``,
``Subject``, ``Session``, ``Scan``, ``Resource`` -- live in
``xnatctl/models/`` and are built on two classes in
``xnatctl/models/base.py``:

- ``BaseModel`` -- a ``pydantic.BaseModel`` configured with
  ``populate_by_name=True`` (accepts XNAT's field aliases, e.g.
  ``subject_ID``), ``extra="ignore"`` (tolerates fields your model doesn't
  declare), and ``str_strip_whitespace=True``. It provides ``to_dict()``
  and a default ``to_row(columns)`` that projects ``to_dict()`` onto the
  requested column list.
- ``XNATResource`` -- subclasses ``BaseModel`` and adds the fields nearly
  every XNAT entity carries: ``id`` (alias ``ID``), ``label``, ``uri``
  (alias ``URI``), ``xsi_type``, ``insert_date``, ``insert_user``, plus a
  ``display_id`` property that falls back from ``label`` to ``id``.

Not everything in ``xnatctl/models/`` is one of these. ``models/hierarchy.py``
also defines frozen ``@dataclass`` reference types (``ProjectRef``,
``ExperimentRef``, ...) used internally to build REST paths, and
``models/progress.py`` defines plain ``@dataclass``/``Enum`` types for
tracking upload/download progress. Neither needs Pydantic validation or
``populate_by_name`` aliasing, so neither subclasses ``BaseModel``. Reach
for ``XNATResource`` specifically when you're modeling a resource XNAT
returns over REST with the usual ``ID``/``label``/``URI`` shape.

When a model like ``Resource`` (``xnatctl/models/resource.py``) wants
table-friendly output, it declares a ``table_columns()`` classmethod and a
``to_row()`` method:

.. code-block:: python

   @classmethod
   def table_columns(cls) -> list[str]:
       """Return columns for table output."""
       return ["label", "format", "file_count", "file_size_display", "content"]

   def to_row(self, columns: list[str] | None = None) -> dict[str, str]:
       """Convert to row for table output."""
       cols = columns or self.table_columns()
       data = self.to_dict()
       data["file_size_display"] = self.file_size_display  # a computed property
       return {col: str(data.get(col, "")) for col in cols}

**Important:** ``print_output()`` (Step 3) never calls either of these
methods itself. It accepts plain ``dict``/``list[dict]`` data and an
*optional* ``columns=`` list -- as of this writing, nothing in
``xnatctl/cli/`` or ``xnatctl/services/`` calls ``.to_row()`` or
``.table_columns()`` on a model instance (``grep -rn
"\.to_row(\|table_columns(" xnatctl/`` does turn up five
``table_columns()`` hits outside a ``def table_columns`` line, but each one
is ``self.table_columns()`` *inside that same model's own* ``to_row()``
default implementation -- a model falling back to its own column list when
no explicit ``columns`` argument was given, not a call from the CLI or
service layer). So a model's ``to_row()``/``table_columns()`` are only
ever invoked by code you write yourself, if you write it.

What a real command hands to ``print_output()`` varies. Some transform the
service's rows into a different shape first -- ``project_users`` (Step 3)
picks three fields out of each raw membership row and adds a computed
``role`` column. Others pass the service's return value straight through,
because the service already returned render-ready rows:

.. code-block:: python

   # xnatctl/cli/session.py, session_list()
   sessions = SessionService(ctx.get_client()).list_sessions(
       project, subject=subject, modality=modality
   )

   print_output(
       sessions,
       format=ctx.output_format,
       columns=["id", "label", "subject", "date", "modality"],
       column_labels={"id": "ID", "label": "Label", "subject": "Subject",
                       "date": "Date", "modality": "Modality"},
       quiet=ctx.quiet,
       id_field="id",
   )

``SessionService.list_sessions()``'s own docstring says why this works:
"Return classified, modality-filtered rows for the ``session list``
screen... Render-ready dicts with id/label/subject/date/modality keys." The
shaping happened in the service, so the CLI command has nothing left to
transform.

``columns=`` itself is optional, not something every call site supplies.
When the result is a single dict rather than a list -- ``project access``'s
read branch (Step 3) prints one ``{"project": ..., "accessibility": ...}``
dict -- there's no table to build, so it's left out entirely:

.. code-block:: python

   print_output(
       {"project": project, "accessibility": level},
       format=ctx.output_format,
       quiet=ctx.quiet,
       id_field="project",
   )

(Without ``columns``, a ``list`` falls back to JSON output regardless of
the requested format -- ``print_output()``'s table branch only fires when
both ``isinstance(data, list)`` and ``columns`` are truthy. A single
``dict`` doesn't need columns to render as a table, since ``print_output``
wraps it in a one-row list itself when you do pass some.)

So for a new command: have your service method return whatever shape its
callers actually need (plain dicts, already render-ready or not, or model
instances), then in the CLI command either pass those rows straight to
``print_output()`` if they're already right, or transform them first if
they're not -- either way with an explicit ``columns=`` for a list result,
and no ``columns=`` for a single dict. If you *do* add a model with
``table_columns()``/``to_row()``, your command still has to call
``[m.to_row() for m in models]`` itself -- nothing does it for you.

The worked example doesn't add a model at all. XNAT's project-membership
endpoints (``/data/projects/{id}/users``, the accessibility endpoint, the
access-request listing) return row shapes that vary by deployment and don't
map onto a single stable schema the way a ``Project`` does, so
``ProjectService`` returns plain dicts for them and the CLI passes those
straight to ``print_output()`` (shown in Step 3). The same choice shows up
in ``admin user roles`` and the rest of the ``/xapi/users`` surface in
``xnatctl/services/users.py``.


Step 2: The Service
--------------------

Services live in ``xnatctl/services/`` and extend ``BaseService``
(``xnatctl/services/base.py``), which provides ``_get``, ``_post``,
and ``_extract_results`` helpers over the injected
``XNATClient``. A service method resolves a REST path, calls the client,
and returns whatever shape is useful to its callers -- model instances,
plain dicts, or (for a pure mutation) the raw ``httpx.Response``. It's
the one place that owns URL construction and response-shape handling, so
the CLI layer doesn't have to.

The worked example's return types are mixed on purpose -- a listing
returns rows, a mutation returns the response, an accessor returns the
scalar it read:

.. code-block:: python

   # xnatctl/services/projects.py
   def list_users(self, project_id: str) -> builtins.list[dict[str, Any]]:
       """List a project's users and their group membership."""
       data = self.client.get_json(f"/data/projects/{quote_path_segment(project_id)}/users")
       if isinstance(data, list):
           return data
       return HierarchyService.extract_rows(data) if isinstance(data, dict) else []

   def grant(self, project_id: str, username: str, role: str) -> httpx.Response:
       """Grant a user a role on a project."""
       role = _validate_project_role(role)
       group_id = f"{project_id}_{role}"
       path = (
           f"/data/projects/{quote_path_segment(project_id)}"
           f"/users/{quote_path_segment(group_id)}/{quote_path_segment(username)}"
       )
       return self.client.put(path)

   def get_accessibility(self, project_id: str) -> str:
       """Get a project's accessibility level."""
       path = f"/data/projects/{quote_path_segment(project_id)}/accessibility"
       resp = self.client.get(path)
       return resp.text.strip()

   def set_accessibility(self, project_id: str, accessibility: str) -> bool:
       """Set project accessibility level. Returns True if successful."""
       path = (
           f"/data/projects/{quote_path_segment(project_id)}"
           f"/accessibility/{quote_path_segment(accessibility)}"
       )
       self._put(path)
       return True

``list_users()`` and ``access_requests()`` return ``list[dict[str, Any]]``.
``grant()`` returns the raw ``httpx.Response`` -- the CLI command doesn't
need anything from the body, only to know the ``PUT`` succeeded.
``revoke()`` returns ``list[str]`` (the group IDs the user was actually
removed from). ``get_accessibility()`` returns ``str``;
``set_accessibility()`` returns ``bool``. Don't assume every service
method returns the same shape -- match the return type to what the caller
actually needs, the way each of these does. ``grant()`` also shows the
validate-before-mutate pattern worth copying: ``_validate_project_role()``
(a module-level function in ``projects.py``) raises
``InputValidationError`` with the valid role set before the ``PUT`` is
even constructed, and every path segment goes through
``quote_path_segment()`` (``xnatctl/core/validation.py``) so a username or
project ID containing a ``/`` can't redirect the request off-path.

**The layering rule is enforced by a test, not just convention.**
``tests/test_architecture.py`` parses every file in ``xnatctl/cli/`` and
fails the build if a CLI module imports ``xnatctl.core.client`` outside a
``TYPE_CHECKING`` block, or calls any of the client's raw-HTTP methods
(``get``, ``post``, ``put``, ``delete``, ``get_json``, ``stream``,
``request``) directly. Only ``cli/common.py`` (which constructs the one
shared client) and ``cli/api.py`` (the deliberate ``api get/post/...``
raw-HTTP escape hatch) are exempted; ``cli/auth.py`` is allowed to
*construct* short-lived probe clients for login/logout/status/test but is
still held to the call check. If you write a Click command that calls
``ctx.get_client().get(...)`` directly, this test catches it in CI --
route the call through a service method instead.


Step 3: The Click Command
--------------------------

Commands live in ``xnatctl/cli/`` as functions wrapped by several
decorators. Python applies decorators bottom-up: the one closest to
``def`` is applied first. Reading the worked example's read-only command
(``project users``, full body, from ``xnatctl/cli/project.py``):

.. code-block:: python

   @project.command("users")
   @click.argument("project")
   @global_options
   @handle_errors
   @require_auth
   def project_users(ctx: Context, project: str) -> None:
       """List a project's users and roles."""
       from xnatctl.core.validation import validate_project_id

       project = validate_project_id(project)
       service = ProjectService(ctx.get_client())
       rows = service.list_users(project)

       output = []
       for r in rows:
           username = r.get("login") or r.get("username") or r.get("ID") or ""
           group_id = r.get("GROUP_ID") or r.get("groupname") or r.get("group") or ""
           output.append(
               {
                   "username": username,
                   "role": _display_role(project, group_id) if group_id else "",
                   "email": r.get("email", ""),
               }
           )

       print_output(
           output,
           format=ctx.output_format,
           columns=["username", "role", "email"],
           column_labels={"username": "Username", "role": "Role", "email": "Email"},
           quiet=ctx.quiet,
           id_field="username",
       )

Here, ``@require_auth`` (closest to ``def``) is applied first, wrapping
the raw function; ``@handle_errors`` is applied next, wrapping that
result; ``@global_options`` is applied after that; and
``@project.command("users")`` is applied last, turning the fully-wrapped
callable into a registered Click command. Execution at call time runs in
the opposite order -- outside-in: whichever decorator was applied *last*
runs its logic *first*. So for this command, ``@global_options`` runs
first (builds the ``Context``, loads config), then ``@handle_errors``
(installs its try/except), then ``@require_auth`` (ensures an
authenticated client), and finally the function body.

**The one universal invariant** across every command in the codebase is
the relative order of these three: ``@global_options`` -> ``@handle_errors``
-> ``@require_auth``, always in that stacking order (verified across
``project.py``, ``session.py``, ``scan.py``, ``subject.py``, ``xsync.py``,
and ``admin.py``). Where a destructive-command decorator or a batch-option
decorator sits *relative to that trio* is not fixed -- it varies by
command. The worked example's ``project grant`` puts ``@confirm_destructive``
*above* ``@global_options`` (applied last, so its confirmation/dry-run
logic runs before the trio):

.. code-block:: python

   @project.command("grant")
   @click.argument("project")
   @click.argument("username")
   @click.option(
       "--role",
       type=click.Choice(["owner", "member", "collaborator"]),
       required=True,
       help="Role to grant",
   )
   @confirm_destructive("Grant this user access to the project?")
   @global_options
   @handle_errors
   @require_auth
   def project_grant(ctx: Context, project: str, username: str, role: str, dry_run: bool) -> None:
       """Grant a user a role on a project."""
       from xnatctl.core.validation import validate_project_id

       project = validate_project_id(project)

       if dry_run:
           click.echo(f"[DRY-RUN] Would grant {username} the {role} role on {project}", err=True)
           return

       service = ProjectService(ctx.get_client())
       service.grant(project, username, role)
       print_success(f"Granted {username} the {role} role on {project}")

But ``xnatctl/cli/xsync.py``'s ``xsync sync`` places it the opposite way,
*below* (closer to ``def`` than) ``@require_auth``:

.. code-block:: python

   @xsync.command("sync")
   @click.option(...)
   @global_options
   @handle_errors
   @require_auth
   @confirm_destructive("Trigger an XSync run for this project?")
   def xsync_sync(ctx: Context, project_id: str | None, dry_run: bool) -> None: ...

and ``project.py``'s own ``project transfer`` stacks both
``@confirm_destructive`` *and* ``@parallel_options`` below
``@require_auth``:

.. code-block:: python

   @project.command("transfer")
   @click.option(...)
   @dest_profile_options
   @global_options
   @handle_errors
   @require_auth
   @confirm_destructive("Transfer data to destination XNAT?")
   @parallel_options
   def project_transfer(...): ...

Don't copy one command's placement as if it were the universal rule --
check a sibling command for a similar shape, and keep
``@global_options`` -> ``@handle_errors`` -> ``@require_auth`` in that
order no matter where the destructive/batch decorators land.

**``@confirm_destructive(message)``**, defined in ``xnatctl/cli/common.py``,
adds ``--yes``/``-y`` and ``--dry-run``. On a real run without ``--yes``,
it calls ``click.confirm(message, abort=True, err=True)`` -- declining
raises ``click.Abort`` and the command never runs. It does **not**
short-circuit a dry run into some separate no-op path: on ``--dry-run`` it
echoes the ``[DRY-RUN]`` notice, sets ``kwargs["dry_run"] = True``, and
still calls the wrapped function -- so the command body itself is
responsible for checking ``dry_run`` and returning before doing anything
mutating, exactly as ``project_grant`` does above (``if dry_run: ...;
return``, before ``service.grant()`` is ever called). Carrying this
decorator is also what makes a command audited (a record is written to
``~/.config/xnatctl/audit.log`` around the call) -- there is no separate
opt-in.

Use ``@confirm_destructive_when(predicate, message)`` instead when a
command is a plain read in its default invocation and only mutates given a
specific option -- ``project access PROJECT`` (a GET) versus
``project access PROJECT --set public`` (a PUT). It still adds
``--yes``/``--dry-run`` unconditionally so ``--help`` and flag parsing stay
predictable, but the confirmation prompt, the dry-run notice, and the
audit write are all skipped whenever ``predicate(kwargs)`` is ``False``.
As of this writing there are exactly two real uses of it in the codebase:
``project access`` in ``xnatctl/cli/project.py`` (predicate: was ``--set``
given) and ``admin user roles`` in ``xnatctl/cli/admin.py`` (predicate:
was ``--grant`` or ``--revoke`` given). Grep
``confirm_destructive_when(`` in ``xnatctl/cli/`` for the current list --
it's short enough to read in full before deciding whether your command
needs it.

``@global_options`` (also in ``common.py``) currently adds five flags:
``--profile``/``-p``, ``--output``/``-o``, ``--quiet``/``-q``,
``--verbose``/``-v``, and ``--no-color``.

A command whose service call returns plain dicts (the ``users``/
``requests`` shape from Step 1) hands them to ``print_output()`` with
explicit column metadata instead of relying on a model's
``table_columns()`` -- this is exactly the call at the bottom of
``project_users`` above:

.. code-block:: python

   print_output(
       output,
       format=ctx.output_format,
       columns=["username", "role", "email"],
       column_labels={"username": "Username", "role": "Role", "email": "Email"},
       quiet=ctx.quiet,
       id_field="username",
   )


Step 4: The -P / -S / -E Convention
-------------------------------------

``session`` and ``scan`` commands (not project-membership commands, which
take the project ID as a plain positional argument) use three uniform
options for scoping to a parent resource. What the options mean:

.. list-table::
   :header-rows: 1
   :widths: 10 15 75

   * - Option
     - Long form
     - Description
   * - ``-P``
     - ``--project``
     - Project ID. Enables experiment lookup by label. Falls back to
       ``default_project`` from the active profile.
   * - ``-S``
     - ``--subject``
     - Subject ID/label. Narrows experiment lookup.
   * - ``-E``
     - ``--experiment``
     - Experiment ID (accession #) or label. A label requires ``-P``
       (explicit or via profile default).

Which commands offer which options, and whether each is required, varies
per command -- verified against the actual ``@click.option`` declarations
rather than assumed uniform:

.. list-table::
   :header-rows: 1
   :widths: 30 15 15 40

   * - Command
     - ``-P``
     - ``-S``
     - ``-E``
   * - ``session list``
     - optional
     - optional (filter)
     - not offered
   * - ``session show`` / ``session download``
     - optional
     - not offered
     - **required**
   * - ``session upload`` / ``session upload-exam``
     - optional
     - **required**
     - **required**
   * - ``session upload-dicom``
     - not offered
     - not offered
     - not offered
   * - ``scan list`` / ``show`` / ``delete`` / ``download``
     - optional
     - optional (narrows lookup, requires ``-P``)
     - **required**

``session upload-dicom`` sends files over native DICOM C-STORE rather than
importing through the REST hierarchy, so it has no project/subject/session
resolution to do -- its own source comments this explicitly: "No -P/-S/-E
on this command: C-STORE is native DICOM network transfer."

ID-vs-label resolution rules, for the commands that do offer ``-E``:

- ``-E`` alone (no ``-P``): the value must be an experiment accession ID
  (e.g. ``XNAT_E00001``), routed to ``/data/experiments/{id}``.
- ``-E`` with ``-P``: the value can be an accession ID or an experiment
  label, routed to ``/data/projects/{P}/experiments/{E}``.
- ``-E`` with ``-P`` and ``-S``: routed to
  ``/data/projects/{P}/subjects/{S}/experiments/{E}``.
- If ``-P`` is omitted but the active profile has ``default_project`` set,
  that project is used automatically -- so ``-E SESSION_LABEL`` alone
  works as long as the profile carries a default project.

That resolution -- looking up an *existing* experiment by ID or label --
is not one single implementation shared by every command; ``session`` and
``scan`` commands each have their own idiom, both built on
``HierarchyService`` (``xnatctl/services/hierarchy.py``) and
``default_project_from_context()`` (``xnatctl/cli/common.py``), but calling
different pieces of it:

- ``session show``/``download`` build an ``ExperimentRef`` and call
  ``hierarchy.resolve_experiment(ref)`` directly (see ``session_show()`` in
  ``xnatctl/cli/session.py``), which returns a ``ResolvedExperimentRef``
  carrying the canonical experiment ID, subject, and project.
- ``scan list``/``show``/``delete`` go through three helpers local to
  ``xnatctl/cli/scan.py`` instead (``scan download`` is the exception: it
  delegates straight to ``DownloadService.download_scans()``, which owns
  its own resolution): ``_build_experiment_ref(project,
  subject, session_id)`` builds the initial ``ExperimentRef`` (and raises a
  ``click.ClickException`` if ``-S`` is given without ``-P``);
  ``_inspect_experiment(hierarchy, ref)`` resolves it to a canonical ref by
  calling ``hierarchy.get_experiment_json(ref)`` -- *not*
  ``resolve_experiment()`` -- because a scan sub-resource URL
  (``.../experiments/{E}/scans``) needs the subject segment inserted or XNAT
  answers with the parent experiment document instead of the scan list; and
  ``_require_scan_addressable(ref)`` then refuses to proceed if the
  resolved ref still can't address a scan (a bare project-scoped label with
  no subject and no resolvable accession ID). A new ``scan`` command should
  call these three in that order, the way ``scan_list()`` does, rather than
  calling ``resolve_experiment()`` directly or re-implementing the lookup.

``session upload`` and ``session upload-exam`` are a different case again:
they create or import into a session that may not exist yet, so they don't
resolve an existing experiment ref at all -- they use
``require_project_from_context(ctx, project)`` (also in ``common.py``) to
pick a project and pass ``-S``/``-E`` straight through as the literal
subject/experiment labels to create or target.


Step 5: Quiet-Mode ID Extraction
-----------------------------------

``print_output(..., quiet=True, id_field=...)`` (``xnatctl/core/output.py``)
prints one ID per line instead of a table or JSON blob. For each row it
evaluates, in order: ``row.get(id_field) or row.get("ID") or
row.get("label") or row.get("name") or ""``. Because this is an ``or``
chain, it's *truthiness*, not presence, that decides -- the first
**non-empty/non-falsy** value wins. A field that's present in the dict but
holds ``""`` (or ``None`` or ``0``) is treated as absent and the chain
falls through to the next field, even though ``in`` would say the key
exists.

For a resource model, the default ``id_field="id"`` is almost always
right, since ``XNATResource.id`` (aliased from ``ID``) is populated on
every row. For a dict-shaped command whose natural identifier isn't
``id``/``ID``/``label``/``name`` -- like ``project users``, whose rows key
on ``username`` -- pass ``id_field="username"`` explicitly so
``project users -q`` emits usernames instead of nothing.


Step 6: Tests
--------------

A new command gets two test files: ``tests/test_cli_<name>.py`` for the
Click layer and ``tests/test_service_<name>.py`` for the service layer (or
new test classes inside the existing file for that resource, if one
already exists -- the worked-example commands added their tests as new
classes inside the existing ``tests/test_cli_project.py`` and
``tests/test_service_projects.py`` rather than new files, since
``project`` already had both).

``tests/conftest.py`` documents its own harness policy at the top of the
file: CLI tests should use the ``authenticated_cli`` fixture (or
``authenticated_cli_factory`` when a non-default profile is needed) rather
than hand-rolling a mock stack, and service tests should use ``fake_client``
plus ``make_response`` (exposed as the ``response_factory`` fixture). Some
older test files in the suite (including today's
``tests/test_cli_project.py``) predate that policy and instead patch
``xnatctl.cli.common.Config.load`` and ``xnatctl.cli.common.XNATClient``
directly -- both styles work, but write new tests against the documented
fixtures below rather than copying the older hand-rolled pattern.

**CLI test**, using ``authenticated_cli`` -- happy path, confirmation
declined, and dry run:

.. code-block:: python

   from unittest.mock import MagicMock

   from conftest import AuthenticatedCLI


   class TestProjectGrant:
       def test_grant_puts_singular_group_id(self, authenticated_cli: AuthenticatedCLI) -> None:
           authenticated_cli.client.put.return_value = MagicMock(status_code=200)

           result = authenticated_cli.invoke(
               ["project", "grant", "PROJ", "jsmith", "--role", "member", "--yes"]
           )

           assert result.exit_code == 0
           authenticated_cli.client.put.assert_called_once_with(
               "/data/projects/PROJ/users/PROJ_member/jsmith"
           )

       def test_grant_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
           """Declining the confirmation prompt must not call the client."""
           result = authenticated_cli.invoke(
               ["project", "grant", "PROJ", "jsmith", "--role", "member"],
               input="n\n",
           )

           assert result.exit_code != 0
           authenticated_cli.client.put.assert_not_called()

       def test_grant_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
           result = authenticated_cli.invoke(
               ["project", "grant", "PROJ", "jsmith", "--role", "member", "--dry-run"]
           )

           assert result.exit_code == 0
           authenticated_cli.client.put.assert_not_called()

For a profile that needs a non-default ``default_project``, request
``authenticated_cli_factory`` instead and call it with the project you
need: ``cli = authenticated_cli_factory(default_project="OTHERPROJ")``.

**Service test**, using ``fake_client`` and ``response_factory``:

.. code-block:: python

   from xnatctl.services.projects import ProjectService


   class TestProjectGrant:
       def test_grant_puts_singular_group_id(self, fake_client, response_factory) -> None:
           fake_client.put.return_value = response_factory("", content_type="text/plain")
           service = ProjectService(fake_client)

           service.grant("PROJ01", "jsmith", "owner")

           fake_client.put.assert_called_once_with(
               "/data/projects/PROJ01/users/PROJ01_owner/jsmith"
           )

A few autouse fixtures run for every test in the suite without needing to
be requested: ``isolate_audit_log`` and ``isolate_session_cache`` redirect
the audit log and session-token cache to a temp directory so a
``@confirm_destructive`` test never writes into your real
``~/.config/xnatctl/``, and ``disable_rich_colour`` strips ANSI styling
from Rich output so assertions on captured text don't depend on your
terminal. You don't need to set any of these up yourself -- they exist so
your test doesn't have to think about them.


Step 7: Documentation
-----------------------

``docs/cli-reference.rst`` is hand-maintained rst, not generated from the
Click command tree -- there is no ``sphinx-click`` (or equivalent) wired
into ``docs/conf.py``. Adding a command means editing this file by hand,
following the pattern already used for every other resource section. For
example, the ``project`` section's bullet list includes a line like:

.. code-block:: rst

   - ``project grant`` -- Grant a user a role (owner/member/collaborator) on a project

1. Add one bullet like that to the resource's command list, in the same
   format as its neighbors.
2. Add the command to (or start) a ``.. code-block:: console`` example
   block showing realistic invocations, e.g.
   ``$ xnatctl project grant MYPROJ jsmith --role member --yes``.
3. If the command has a non-obvious constraint worth calling out (the
   worked example's ``project requests`` has one -- XNAT resolves an
   access request against whoever is *currently signed in*, not the
   invited user, so there is deliberately no ``--approve``/``--deny``),
   add a ``.. note::`` admonition explaining it, the way
   ``docs/cli-reference.rst`` already does for that command.

``docs/index.rst`` does **not** list individual command groups or
sub-commands -- that's what ``docs/cli-reference.rst`` is for. ``index.rst``
is the landing page: a short feature overview, a few Quick Example
snippets, and the toctree that lists which *pages* exist. Adding a verb to
an existing command group (as all four worked-example commands do) needs
no change there at all.

Introducing an entirely new top-level command *group* is a different, rarer
case, and takes three steps beyond everything above:

1. Define the group in its own new ``xnatctl/cli/<name>.py``, the same way
   every existing group does:

   .. code-block:: python

      @click.group()
      def mygroup() -> None:
          """One-line description of the group."""

2. Import and register it in ``xnatctl/cli/main.py``, alongside the
   existing groups:

   .. code-block:: python

      from xnatctl.cli.mygroup import mygroup
      # ...
      cli.add_command(mygroup)

3. Give the new group its own top-level section in
   ``docs/cli-reference.rst`` (a heading, its command bullets, and example
   invocations), matching the ``project``/``xsync``/etc. sections already
   there. Only a genuinely new *page* (not a new group under an existing
   page) needs a ``docs/index.rst`` toctree entry.

Beyond the CLI reference, add a line describing the new command from a
user's perspective to ``CHANGELOG.md``, under an ``## Unreleased`` heading.

**Check whether that heading already exists before you touch the file.**
Run ``head -10 CHANGELOG.md``. As of this writing it does *not* exist --
the file's most recent heading is a dated release
(``## 0.4.0 - 2026-08-22``), and the structure looks like this:

.. code-block:: text

   # Changelog

   All notable changes to this project will be documented in this file.

   ## 0.4.0 - 2026-08-22

   **Breaking**
   ...

If your ``head -10`` looks like that (no ``## Unreleased`` line), you are
the first change since the last release, and creating the heading is your
job, not something to work around. Insert exactly this, directly above the
top dated heading (``## 0.4.0 - 2026-08-22`` as of this writing) and below
the "All notable changes..." line:

.. code-block:: text

   ## Unreleased

   **Features**

   - `project grant`/`revoke`/`users`/`access` -- manage project membership
     and accessibility from the CLI.

Use whichever of the three established subsections your change needs, in
this order (matching every existing dated entry): ``**Breaking**``,
``**Features**``, ``**Fixes**``. A new command is a ``**Features**`` entry
unless it changes or removes something that already shipped. If
``## Unreleased`` already exists (a later contributor added it after you
read this), just add your bullet under the matching subsection instead of
creating a new heading.


Checklist
---------

Copy this into your PR description and check items off as you go:

.. code-block:: text

   [ ] Model added/extended in xnatctl/models/ if the endpoint maps to a
       stable resource shape; otherwise plain dicts + explicit columns=
       to print_output() (either way, your CLI command builds the row
       dicts itself -- print_output() never calls to_row()/table_columns())
   [ ] Service method added in xnatctl/services/, extending BaseService,
       returning whatever shape the caller actually needs (model, dict,
       list[dict], or httpx.Response for a pure mutation)
   [ ] CLI command added with @global_options -> @handle_errors ->
       @require_auth kept in that relative order; @confirm_destructive[_when]
       (and, for a batch command, @parallel_options) placed by checking a
       similar existing command, not assumed universal
   [ ] -P/-S/-E options added and required-ness matched to the real
       Click declarations, if this is a session/scan command; session
       show/download use hierarchy.resolve_experiment(), scan commands
       use _build_experiment_ref()/_inspect_experiment()/
       _require_scan_addressable(), and upload commands use
       require_project_from_context() instead of resolving an existing ref
   [ ] id_field passed to print_output() if quiet-mode's id/ID/label/name
       fallback chain doesn't already match your data (remember: it's an
       `or` chain, so an empty-string field is skipped as if absent)
   [ ] tests/test_cli_<name>.py (or a class in the existing CLI test file)
       using authenticated_cli / authenticated_cli_factory, covering the
       happy path, a declined confirmation, and --dry-run for any
       destructive command
   [ ] tests/test_service_<name>.py (or a class in the existing service
       test file) using fake_client / response_factory
   [ ] docs/cli-reference.rst updated: bullet + example command +
       any needed note/warning
   [ ] CHANGELOG.md entry under ## Unreleased (create the heading if
       the file doesn't have one yet)

Then run the full local gate:

.. code-block:: console

   $ uv run pytest tests/ -v
   $ uv run ruff check xnatctl tests scripts
   $ uv run ruff format --check xnatctl tests scripts
   $ uv run mypy xnatctl

All four must pass before the change is ready for review.
