"""Layering guard: CLI commands go through services, not the HTTP client.

Click command bodies resolve options, call a service, and format output. The
REST paths and calls live in the service layer. Two files are the sanctioned
exceptions:

- ``cli/common.py`` builds the one :class:`XNATClient` every command shares.
- ``cli/api.py`` is the deliberate raw-HTTP escape hatch (``api get/post/...``).

``cli/auth.py`` also constructs clients -- short-lived probe clients for
login/logout/status/test, each with per-command auth settings (a
session-token-only client for the logout invalidation, credential clients for
login and test). Those are legitimate client *construction*, not REST calls, so
it is allowlisted for the import check only; it makes no raw-HTTP call and so is
still held to the call check.

Type-only imports under ``if TYPE_CHECKING:`` are annotations, not layering, and
are allowed anywhere (``cli/session.py`` imports ``XNATClient`` that way).

See docs/adr/0013-cli-routes-through-services.md.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parent.parent / "xnatctl" / "cli"

# May import xnatctl.core.client at runtime (they construct the client).
IMPORT_ALLOWLIST = {"common.py", "api.py", "auth.py"}

# May call the client's raw-HTTP methods directly.
CALL_ALLOWLIST = {"common.py", "api.py"}

# The client's raw-HTTP surface. Calling any of these on a client (or on
# anything that looks like one) from a CLI module is the layering violation
# this guard exists to catch.
_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "get_json", "stream", "request"})

# These names are distinctive to XNATClient -- no stdlib or Rich/Click object
# in the CLI carries them -- so a call is flagged on ANY receiver.
_DISTINCTIVE_METHODS = frozenset({"get_json"})


def _cli_files() -> list[Path]:
    return sorted(p for p in CLI_DIR.glob("*.py") if p.name != "__init__.py")


# -----------------------------------------------------------------------------
# Import guard
# -----------------------------------------------------------------------------


def _is_type_checking_test(test: ast.expr) -> bool:
    """True only for the exact ``TYPE_CHECKING`` / ``typing.TYPE_CHECKING`` forms."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return (
            test.attr == "TYPE_CHECKING"
            and isinstance(test.value, ast.Name)
            and test.value.id == "typing"
        )
    return False


def _type_only_imports(tree: ast.Module) -> set[ast.stmt]:
    """Collect import nodes inside the BODY of an ``if TYPE_CHECKING:`` block.

    Only ``node.body`` is walked: an import in the ``else:`` branch runs
    precisely when TYPE_CHECKING is false, i.e. at runtime.
    """
    guarded: set[ast.stmt] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Import | ast.ImportFrom):
                        guarded.add(sub)
    return guarded


def _imports_core_client(node: ast.AST) -> bool:
    """True for any import form that binds ``xnatctl.core.client``.

    Covers ``import xnatctl.core.client [as x]``,
    ``from xnatctl.core.client import ...``,
    ``from xnatctl.core import client``, and the relative spellings CLI modules
    could use (``from ..core.client import ...``, ``from ..core import client``).
    """
    if isinstance(node, ast.Import):
        return any(alias.name == "xnatctl.core.client" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        # Relative imports resolve against xnatctl.cli; normalize the module
        # text: from inside the package, "..core.client" arrives as
        # module="core.client" with level=2.
        if module in ("xnatctl.core.client", "core.client"):
            return True
        if module in ("xnatctl.core", "core"):
            return any(alias.name == "client" for alias in node.names)
    return False


@pytest.mark.parametrize("path", _cli_files(), ids=lambda p: p.name)
def test_cli_module_does_not_import_client_at_runtime(path: Path) -> None:
    """No CLI module imports ``xnatctl.core.client`` at runtime, bar the allowlist."""
    if path.name in IMPORT_ALLOWLIST:
        return

    tree = ast.parse(path.read_text())
    type_only = _type_only_imports(tree)

    offenders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        and _imports_core_client(node)
        and node not in type_only
    ]
    assert not offenders, (
        f"{path.name} imports xnatctl.core.client at runtime "
        f"(line {offenders[0].lineno}). Route the work through a service, or move "
        "the import under `if TYPE_CHECKING:` if it is only a type annotation."
    )


# -----------------------------------------------------------------------------
# Call guard
# -----------------------------------------------------------------------------


def _client_aliases(tree: ast.Module) -> set[str]:
    """Names bound from a ``*.get_client()`` call anywhere in the module.

    Catches ``client = ctx.get_client()`` (and any alias name), so a call like
    ``c.post(...)`` on the alias is attributable to the client.
    """

    def _is_get_client_call(value: ast.expr) -> bool:
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "get_client"
        )

    aliases: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign) and _is_get_client_call(node.value):
            targets = list(node.targets)
        elif (
            isinstance(node, ast.AnnAssign | ast.NamedExpr)
            and node.value is not None
            and _is_get_client_call(node.value)
        ):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)
    return aliases


def _is_client_receiver(receiver: ast.expr, aliases: set[str]) -> bool:
    """Whether an attribute call's receiver plausibly IS the HTTP client."""
    # client.post(...), source.get(...) where the name was bound from get_client()
    if isinstance(receiver, ast.Name):
        return receiver.id in aliases or "client" in receiver.id.lower()
    # ctx.get_client().post(...) -- calling straight through the factory
    if isinstance(receiver, ast.Call):
        return isinstance(receiver.func, ast.Attribute) and receiver.func.attr == "get_client"
    # service.client.get_json(...), hierarchy.client.get(...) -- reach-through
    if isinstance(receiver, ast.Attribute):
        return receiver.attr == "client" or "client" in receiver.attr.lower()
    return False


def _raw_http_calls(tree: ast.Module) -> list[ast.Call]:
    """Calls in the module that hit the client's raw-HTTP surface."""
    aliases = _client_aliases(tree)
    offenders: list[ast.Call] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        method = node.func.attr
        if method not in _HTTP_METHODS:
            continue
        if method in _DISTINCTIVE_METHODS or _is_client_receiver(node.func.value, aliases):
            offenders.append(node)
    return offenders


@pytest.mark.parametrize("path", _cli_files(), ids=lambda p: p.name)
def test_cli_module_makes_no_raw_http_calls(path: Path) -> None:
    """No CLI module calls the client's raw-HTTP methods, bar the allowlist."""
    if path.name in CALL_ALLOWLIST:
        return

    offenders = _raw_http_calls(ast.parse(path.read_text()))
    lines = [
        f"line {node.lineno}: .{node.func.attr}(...)"
        for node in offenders
        if isinstance(node.func, ast.Attribute)
    ]
    assert not offenders, (
        f"{path.name} calls the HTTP client directly ({'; '.join(lines)}). "
        "Add or extend a service method that owns this REST call instead."
    )


# -----------------------------------------------------------------------------
# Self-tests: the guard must catch what it claims to catch
# -----------------------------------------------------------------------------


def test_guard_allowlists_reference_real_files() -> None:
    """A renamed/removed allowlisted file must break the guard loudly, not silently."""
    names = {p.name for p in _cli_files()}
    assert names >= IMPORT_ALLOWLIST
    assert names >= CALL_ALLOWLIST


@pytest.mark.parametrize(
    "source",
    [
        "from xnatctl.core.client import XNATClient\n",
        "import xnatctl.core.client\n",
        "import xnatctl.core.client as cc\n",
        "from xnatctl.core import client\n",
        "from ..core.client import XNATClient\n",
        "from ..core import client\n",
        # An import in the ELSE branch of `if TYPE_CHECKING:` runs at runtime.
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    pass\n"
        "else:\n"
        "    from xnatctl.core.client import XNATClient\n",
        # A guard spoof: the test must match TYPE_CHECKING exactly.
        "OTHER_FLAG = True\nif OTHER_FLAG:\n    from xnatctl.core.client import XNATClient\n",
    ],
    ids=[
        "from-module",
        "plain-import",
        "aliased-import",
        "from-package",
        "relative-module",
        "relative-package",
        "else-branch",
        "non-type-checking-if",
    ],
)
def test_import_guard_catches_runtime_import_forms(source: str) -> None:
    tree = ast.parse(source)
    type_only = _type_only_imports(tree)
    offenders = [n for n in ast.walk(tree) if _imports_core_client(n) and n not in type_only]
    assert offenders


def test_import_guard_permits_a_type_only_import() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from xnatctl.core.client import XNATClient\n"
    )
    tree = ast.parse(source)
    type_only = _type_only_imports(tree)
    offenders = [n for n in ast.walk(tree) if _imports_core_client(n) and n not in type_only]
    assert not offenders


@pytest.mark.parametrize(
    "source",
    [
        "client.get_json('/data/projects')\n",
        "resp = client.post('/x', params=p)\n",
        "hierarchy.client.get_json(path)\n",
        "service.client.delete(url)\n",
        "ctx.get_client().put('/x')\n",
        "c = ctx.get_client()\nc.post('/x')\n",
        "with client.stream('GET', path) as r:\n    pass\n",
    ],
    ids=[
        "get-json",
        "client-post",
        "reach-through-get-json",
        "reach-through-delete",
        "factory-chained",
        "factory-alias",
        "client-stream",
    ],
)
def test_call_guard_catches_raw_http_forms(source: str) -> None:
    assert _raw_http_calls(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "value = row.get('ID')\n",  # dict.get is not HTTP
        "resp.get('ResultSet', {})\n",  # nested dict access
        "# client.get_json in a comment\nx = 1\n",  # comments are not code
        "s = \"client.post('/x')\"\n",  # strings are not code
        "service.list_rows(parent)\n",  # a service call is the sanctioned path
    ],
    ids=["dict-get", "resp-get", "comment", "string", "service-call"],
)
def test_call_guard_ignores_non_client_code(source: str) -> None:
    assert not _raw_http_calls(ast.parse(source))
