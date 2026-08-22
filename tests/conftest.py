"""Pytest configuration and shared test harness for xnatctl.

Harness policy — use these instead of hand-rolling a mock stack:

* **CLI tests** use ``authenticated_cli`` (or ``authenticated_cli_factory`` when
  the profile needs a non-default ``default_project``). It owns the three
  patch seams into ``cli/common.py``, so a rename there is a one-line fix here
  rather than a few hundred test edits.
* **Service tests** use ``fake_client`` plus :func:`make_response`.
* **Wire-behaviour tests** use ``httpx.MockTransport`` — not mocks.

:func:`make_response` and :func:`make_authenticated_context` are also importable
directly (``from conftest import make_response``) for module-level use.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Generator, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner, Result

from xnatctl.cli.common import Context
from xnatctl.cli.main import cli
from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config, Profile

# The seams every CLI test patches. Keeping them in one place is the point of
# this module: renaming anything in cli/common.py must not touch test files.
_CONFIG_LOAD_SEAM = "xnatctl.cli.common.Config.load"
_CORE_CONFIG_LOAD_SEAM = "xnatctl.core.config.Config.load"
_AUTH_MANAGER_SEAM = "xnatctl.cli.common.AuthManager"

DEFAULT_BASE_URL = "https://xnat.example.org"


def make_response(
    json_data: dict | list | str | None = None,
    content_type: str = "application/json",
    status_code: int = 200,
    text: str | None = None,
) -> MagicMock:
    """Build a mock ``httpx.Response`` for service-layer tests.

    Args:
        json_data: Payload returned by ``resp.json()``. Left unstubbed if None.
        content_type: Value of the ``content-type`` response header.
        status_code: HTTP status code.
        text: Response body text. Defaults to ``str(json_data)``.
    """
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    resp.text = text if text is not None else str(json_data if json_data is not None else "")
    resp.headers = {"content-type": content_type}
    return resp


def make_fake_client(base_url: str = DEFAULT_BASE_URL) -> MagicMock:
    """Build an authenticated ``XNATClient`` mock.

    ``spec=`` is deliberate: a typo'd attribute must fail loudly rather than
    silently produce another mock.
    """
    client = MagicMock(spec=XNATClient)
    client.is_authenticated = True
    client.base_url = base_url
    client.whoami.return_value = {"login": "user"}
    return client


def make_authenticated_context(
    default_project: str | None = "TESTPROJ",
) -> tuple[Context, MagicMock]:
    """Build a Context with a mocked authenticated client.

    Args:
        default_project: Default project for the profile.

    Returns:
        Tuple of (Context, mock_client).
    """
    ctx = Context()
    ctx.config = Config(
        profiles={
            "default": Profile(
                url=DEFAULT_BASE_URL,
                username="user",
                password="pass",
                default_project=default_project,
            ),
        },
    )
    # Not spec'd: CLI tests routinely stub call shapes the real client does not
    # expose (e.g. chained streaming context managers).
    mock_client = MagicMock()
    mock_client.is_authenticated = True
    mock_client.base_url = DEFAULT_BASE_URL
    mock_client.whoami.return_value = {"login": "user"}
    ctx.client = cast(Any, mock_client)
    ctx.auth_manager = MagicMock()
    return ctx, mock_client


@dataclass
class AuthenticatedCLI:
    """A CliRunner wired to an authenticated Context.

    Attributes:
        runner: The Click test runner.
        ctx: The Context whose config/auth_manager back the patched seams.
        client: The mock client every command will receive.
    """

    runner: CliRunner
    ctx: Context
    client: MagicMock

    def invoke(self, args: list[str], **kwargs: Any) -> Result:
        """Invoke the CLI with the auth seams patched."""
        with ExitStack() as stack:
            stack.enter_context(patch(_CONFIG_LOAD_SEAM, return_value=self.ctx.config))
            stack.enter_context(patch(_CORE_CONFIG_LOAD_SEAM, return_value=self.ctx.config))
            stack.enter_context(patch.object(Context, "get_client", return_value=self.client))
            auth_cls = stack.enter_context(patch(_AUTH_MANAGER_SEAM))
            auth_cls.return_value = self.ctx.auth_manager
            return self.runner.invoke(cli, args, **kwargs)


def make_authenticated_cli(default_project: str | None = "TESTPROJ") -> AuthenticatedCLI:
    """Build an :class:`AuthenticatedCLI` harness."""
    ctx, client = make_authenticated_context(default_project=default_project)
    return AuthenticatedCLI(runner=CliRunner(), ctx=ctx, client=client)


@contextmanager
def authenticated_seams(ctx: Context, client: MagicMock) -> Iterator[None]:
    """Patch the full auth stack for a ``CliRunner.invoke`` call.

    Prefer the ``authenticated_cli`` fixture for new tests; this exists so
    existing tests that build their own Context keep the seam strings in one
    place.
    """
    with ExitStack() as stack:
        stack.enter_context(patch(_CONFIG_LOAD_SEAM, return_value=ctx.config))
        stack.enter_context(patch.object(Context, "get_client", return_value=client))
        auth_cls = stack.enter_context(patch(_AUTH_MANAGER_SEAM))
        auth_cls.return_value = ctx.auth_manager
        yield


@contextmanager
def config_seam(config: Any) -> Iterator[None]:
    """Patch only the CLI's config loader (no client/auth wiring)."""
    with patch(_CONFIG_LOAD_SEAM, return_value=config):
        yield


@contextmanager
def core_config_seam(config: Any) -> Iterator[None]:
    """Patch the core config loader that ``Config.load`` call sites reach."""
    with patch(_CORE_CONFIG_LOAD_SEAM, return_value=config):
        yield


@pytest.fixture
def fake_client() -> MagicMock:
    """An authenticated ``XNATClient`` mock for service-layer tests."""
    return make_fake_client()


@pytest.fixture
def response_factory() -> Callable[..., MagicMock]:
    """Factory fixture wrapping :func:`make_response`."""
    return make_response


@pytest.fixture
def authenticated_cli() -> AuthenticatedCLI:
    """CLI harness with the standard ``TESTPROJ`` default project."""
    return make_authenticated_cli()


@pytest.fixture
def authenticated_cli_factory() -> Callable[..., AuthenticatedCLI]:
    """Factory for CLI harnesses needing a non-default profile."""
    return make_authenticated_cli


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_config_yaml() -> str:
    """Sample config YAML content."""
    return """
default_profile: test
output_format: table

profiles:
  test:
    url: https://xnat-test.example.org
    verify_ssl: false
    timeout: 30
    default_project: TESTPROJ

  production:
    url: https://xnat.example.org
    verify_ssl: true
    timeout: 60
"""


@pytest.fixture
def sample_config_with_credentials_yaml() -> str:
    """Sample config YAML with credentials."""
    return """
default_profile: test
output_format: table

profiles:
  test:
    url: https://xnat-test.example.org
    username: testuser
    password: testpass
    verify_ssl: false
    timeout: 30
    default_project: TESTPROJ

  production:
    url: https://xnat.example.org
    username: produser
    password: prodpass
    verify_ssl: true
    timeout: 60
"""


@pytest.fixture
def sample_patterns_json() -> str:
    """Sample patterns JSON for label fixes."""
    return """
{
  "description": "Test patterns",
  "patterns": [
    {
      "project": "TEST01",
      "match": "^(SUB\\\\d{3})$",
      "to": "{project}_{1}",
      "description": "SUBNNN -> TEST01_SUBNNN"
    }
  ]
}
"""


@pytest.fixture(autouse=True)
def isolate_audit_log(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Keep the audit log out of the developer's real home directory.

    ``confirm_destructive`` writes an audit record for every destructive
    command, so any CliRunner test touching one would otherwise append to
    ``~/.config/xnatctl/audit.log``. Autouse because the write happens inside a
    decorator that individual tests never mention.
    """
    path = tmp_path_factory.mktemp("audit") / "audit.log"
    monkeypatch.setattr("xnatctl.core.logging.AUDIT_LOG_FILE", path)
    return path


@pytest.fixture(autouse=True)
def isolate_session_cache(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Keep the cached JSESSIONID out of the developer's real home directory.

    ``AuthManager()`` defaults to ``~/.config/xnatctl/.session``, and code that
    refreshes a token writes through it without being asked -- a worker-thread
    reauth updates the cache so other processes benefit. A test exercising that
    path therefore overwrites the developer's live session with a fake token
    (observed: a test wrote ``token="FRESH", url="https://x"`` over the real
    cache). Autouse, because the write happens deep inside the refresh path
    that individual tests never name.
    """
    path = tmp_path_factory.mktemp("session") / ".session"
    monkeypatch.setattr("xnatctl.core.auth.SESSION_CACHE_FILE", path)
    monkeypatch.setattr("xnatctl.core.config.SESSION_CACHE_FILE", path, raising=False)
    return path


@pytest.fixture
def no_ambient_credentials(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Make ``Config.load()`` blind to the developer's real profile.

    ``Config.load()`` defaults to ``~/.config/xnatctl/config.yaml``
    (``CONFIG_FILE``), and reads ``XNAT_URL``/``XNAT_USER``/``XNAT_PASS``/
    ``XNAT_TOKEN`` as overrides on top of it. A ``CliRunner`` test that
    invokes the real ``cli`` entry point without patching the harness's own
    ``_CONFIG_LOAD_SEAM`` (``authenticated_seams``/``authenticated_cli``) would
    otherwise silently pick up the developer machine's real config file and
    any ambient env vars -- passing there for the wrong reason (a real
    profile happens to authenticate) while failing in CI, where none of that
    exists and ``@require_auth`` raises "Not authenticated" before the
    command body ever runs. The path points at a directory that is never
    created, so ``CONFIG_FILE.exists()`` is reliably False.

    Request this explicitly in a test that deliberately exercises the
    unauthenticated/no-credentials path, or that must prove something (like
    an eager option callback) runs before ``@require_auth`` regardless of
    ambient state. Not autouse: a large slice of the existing suite already
    relies, pre-existing and out of this change's scope, on a bare
    ``CliRunner()`` picking up *some* profile from elsewhere in the harness;
    scrubbing credentials suite-wide breaks those independently of anything
    here.
    """
    path = tmp_path_factory.mktemp("config") / "config.yaml"
    monkeypatch.setattr("xnatctl.core.config.CONFIG_FILE", path)
    for var in ("XNAT_URL", "XNAT_USER", "XNAT_PASS", "XNAT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return path


class _NullHighlighter:
    """A Rich highlighter that adds no styling."""

    def __call__(self, text: object) -> object:
        from rich.text import Text

        return Text(text) if isinstance(text, str) else text


@pytest.fixture(autouse=True)
def disable_rich_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make assertions on CLI output independent of the developer's terminal.

    Rich styles output when it believes a terminal is attached, and honours
    ``FORCE_COLOR`` even under ``CliRunner`` -- whose captured stream is not a
    tty. A developer who exports ``FORCE_COLOR`` (common in modern shells) then
    sees ``Modified \\x1b[1;36m1\\x1b[0m files`` where the test asserts
    ``Modified 1``, and five dicom tests fail for them and pass in CI.

    Neutralised here rather than by loosening the assertions: the plain strings
    are what a user piping xnatctl to another program sees, so they are the
    right thing to assert.
    """
    # Set on the Console objects, not via the environment: Rich reads
    # FORCE_COLOR once, when the module-level consoles are constructed at
    # import time, which is long before any fixture runs.
    from xnatctl.core import output

    for con in (output.console, output.err_console):
        monkeypatch.setattr(con, "no_color", True)
        monkeypatch.setattr(con, "_force_terminal", False)
        # highlight=True is what wraps bare integers in a style, turning
        # "Modified 1 files" into "Modified \x1b[1;36m1\x1b[0m files".
        monkeypatch.setattr(con, "_highlight", False, raising=False)
        monkeypatch.setattr(con, "highlighter", _NullHighlighter(), raising=False)
