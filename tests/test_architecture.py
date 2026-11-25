from __future__ import annotations

import ast
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parent.parent / "xnatctl" / "cli"

IMPORT_ALLOWLIST = {"common/__init__.py", "api.py", "auth.py"}

CALL_ALLOWLIST = {"common/__init__.py", "api.py"}

_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "get_json", "stream", "request"})

_DISTINCTIVE_METHODS = frozenset({"get_json"})


def _cli_files() -> list[Path]:
    top_level_init = CLI_DIR / "__init__.py"
    return sorted(p for p in CLI_DIR.rglob("*.py") if p != top_level_init)


def _rel_name(path: Path) -> str:
    return path.relative_to(CLI_DIR).as_posix()


def _is_type_checking_test(test: ast.expr) -> bool:
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
    guarded: set[ast.stmt] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Import | ast.ImportFrom):
                        guarded.add(sub)
    return guarded


def _imports_core_client(node: ast.AST) -> bool:
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


@pytest.mark.parametrize("path", _cli_files(), ids=_rel_name)
def test_cli_module_does_not_import_client_at_runtime(path: Path) -> None:
    if _rel_name(path) in IMPORT_ALLOWLIST:
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
        f"{_rel_name(path)} imports xnatctl.core.client at runtime "
        f"(line {offenders[0].lineno}). Route the work through a service, or move "
        "the import under `if TYPE_CHECKING:` if it is only a type annotation."
    )


def _client_aliases(tree: ast.Module) -> set[str]:
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
    if isinstance(receiver, ast.Name):
        return receiver.id in aliases or "client" in receiver.id.lower()
    if isinstance(receiver, ast.Call):
        return isinstance(receiver.func, ast.Attribute) and receiver.func.attr == "get_client"
    if isinstance(receiver, ast.Attribute):
        return receiver.attr == "client" or "client" in receiver.attr.lower()
    return False


def _raw_http_calls(tree: ast.Module) -> list[ast.Call]:
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


@pytest.mark.parametrize("path", _cli_files(), ids=_rel_name)
def test_cli_module_makes_no_raw_http_calls(path: Path) -> None:
    if _rel_name(path) in CALL_ALLOWLIST:
        return

    offenders = _raw_http_calls(ast.parse(path.read_text()))
    lines = [
        f"line {node.lineno}: .{node.func.attr}(...)"
        for node in offenders
        if isinstance(node.func, ast.Attribute)
    ]
    assert not offenders, (
        f"{_rel_name(path)} calls the HTTP client directly ({'; '.join(lines)}). "
        "Add or extend a service method that owns this REST call instead."
    )


def test_guard_allowlists_reference_real_files() -> None:
    names = {_rel_name(p) for p in _cli_files()}
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
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    pass\n"
        "else:\n"
        "    from xnatctl.core.client import XNATClient\n",
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
        "value = row.get('ID')\n",
        "resp.get('ResultSet', {})\n",
        "# client.get_json in a comment\nx = 1\n",
        "s = \"client.post('/x')\"\n",
        "service.list_rows(parent)\n",
    ],
    ids=["dict-get", "resp-get", "comment", "string", "service-call"],
)
def test_call_guard_ignores_non_client_code(source: str) -> None:
    assert not _raw_http_calls(ast.parse(source))


PACKAGE_DIR = CLI_DIR.parent
# Log calls that put a traceback on the console. Each was there before
# tracebacks moved to the diagnostics file and is left as it was; any other
# call that logs a traceback marks it file-only.
CONSOLE_TRACEBACK_CALLS = sorted(
    [
        ("services/transfer/executor_archive.py", "'%s for %s'"),
        ("services/transfer/executor_archive.py", "'Poll cycle error for %s, retrying next cycle'"),
        (
            "services/transfer/orchestrator.py",
            "'Failed to query dest experiments for reconciliation'",
        ),
        ("services/transfer/orchestrator.py", "'Failed to query dest subjects for reconciliation'"),
        ("services/transfer/poller.py", "'Archive poller crashed'"),
        ("services/transfer/poller.py", "'Archive wait failed for %s in blocking drain'"),
        ("services/transfer/poller.py", "'Failed to fetch prearchive snapshot'"),
        ("services/transfer/poller.py", "'Prearchive archive failed for %s in blocking drain'"),
        ("services/transfer/poller.py", "'Prearchive archive failed for %s'"),
        (
            "services/transfer/poller.py",
            "'Prearchive resolution failed for %s (attempt %d), will retry'",
        ),
        (
            "services/transfer/poller.py",
            "'Prearchive resolution failed for %s in blocking drain, falling back to "
            "wait_for_archive'",
        ),
        ("services/transfer/poller.py", "'count_dest_scans failed for %s, will retry'"),
        ("services/transfer/scan_pipeline_finalize.py", "failed_message"),
        ("services/upload/shared.py", "'Session refresh failed'"),
    ]
)


def _logs_a_traceback(call: ast.Call) -> bool:
    if isinstance(call.func, ast.Attribute) and call.func.attr == "exception":
        return True
    exc_info = next((k.value for k in call.keywords if k.arg == "exc_info"), None)
    if exc_info is None:
        return False
    return not (isinstance(exc_info, ast.Constant) and exc_info.value in (False, None))


def _marks_file_only(call: ast.Call) -> bool:
    extra = next((k.value for k in call.keywords if k.arg == "extra"), None)
    return isinstance(extra, ast.Dict) and any(
        isinstance(key, ast.Name)
        and key.id == "FILE_ONLY_ATTR"
        and isinstance(value, ast.Constant)
        and value.value is True
        for key, value in zip(extra.keys, extra.values, strict=True)
    )


def _traceback_calls() -> list[tuple[str, ast.Call]]:
    calls = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        rel = path.relative_to(PACKAGE_DIR).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
                and _logs_a_traceback(node)
            ):
                calls.append((rel, node))
    return calls


def test_logged_tracebacks_go_to_the_diagnostics_file_only() -> None:
    calls = _traceback_calls()
    console = sorted(
        (rel, ast.unparse(call.args[0]) if call.args else "")
        for rel, call in calls
        if not _marks_file_only(call)
    )

    assert len(calls) > 60
    assert console == CONSOLE_TRACEBACK_CALLS
