from __future__ import annotations

import logging
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
from hypothesis import settings

from xnatctl.cli.common import Context
from xnatctl.cli.main import cli
from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config, Profile
from xnatctl.models.info import UserInfo

_CONFIG_LOAD_SEAM = "xnatctl.cli.common.Config.load"
_CORE_CONFIG_LOAD_SEAM = "xnatctl.core.config.Config.load"
_AUTH_MANAGER_SEAM = "xnatctl.cli.common.AuthManager"

DEFAULT_BASE_URL = "https://xnat.example.org"

settings.register_profile("ci", deadline=None, derandomize=True, max_examples=50)
settings.load_profile("ci")


def make_response(
    json_data: dict | list | str | None = None,
    content_type: str = "application/json",
    status_code: int = 200,
    text: str | None = None,
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    resp.text = text if text is not None else str(json_data if json_data is not None else "")
    resp.headers = {"content-type": content_type}
    return resp


def make_fake_client(base_url: str = DEFAULT_BASE_URL) -> MagicMock:
    client = MagicMock(spec=XNATClient)
    client.is_authenticated = True
    client.base_url = base_url
    client.whoami.return_value = UserInfo(username="user", enabled=True)
    client.current_username.return_value = "user"
    client.server_version = None
    return client


def make_authenticated_context(
    default_project: str | None = "TESTPROJ",
) -> tuple[Context, MagicMock]:
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
    mock_client = MagicMock()
    mock_client.is_authenticated = True
    mock_client.base_url = DEFAULT_BASE_URL
    mock_client.whoami.return_value = UserInfo(username="user", enabled=True)
    mock_client.current_username.return_value = "user"
    ctx.client = cast(Any, mock_client)
    ctx.auth_manager = MagicMock()
    return ctx, mock_client


@dataclass
class AuthenticatedCLI:
    runner: CliRunner
    ctx: Context
    client: MagicMock

    def invoke(self, args: list[str], **kwargs: Any) -> Result:
        with ExitStack() as stack:
            stack.enter_context(patch(_CONFIG_LOAD_SEAM, return_value=self.ctx.config))
            stack.enter_context(patch(_CORE_CONFIG_LOAD_SEAM, return_value=self.ctx.config))
            stack.enter_context(patch.object(Context, "get_client", return_value=self.client))
            auth_cls = stack.enter_context(patch(_AUTH_MANAGER_SEAM))
            auth_cls.return_value = self.ctx.auth_manager
            return self.runner.invoke(cli, args, **kwargs)


def make_authenticated_cli(default_project: str | None = "TESTPROJ") -> AuthenticatedCLI:
    ctx, client = make_authenticated_context(default_project=default_project)
    return AuthenticatedCLI(runner=CliRunner(), ctx=ctx, client=client)


@contextmanager
def authenticated_seams(ctx: Context, client: MagicMock) -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(patch(_CONFIG_LOAD_SEAM, return_value=ctx.config))
        stack.enter_context(patch.object(Context, "get_client", return_value=client))
        auth_cls = stack.enter_context(patch(_AUTH_MANAGER_SEAM))
        auth_cls.return_value = ctx.auth_manager
        yield


@contextmanager
def config_seam(config: Any) -> Iterator[None]:
    with patch(_CONFIG_LOAD_SEAM, return_value=config):
        yield


@contextmanager
def core_config_seam(config: Any) -> Iterator[None]:
    with patch(_CORE_CONFIG_LOAD_SEAM, return_value=config):
        yield


@pytest.fixture
def fake_client() -> MagicMock:
    return make_fake_client()


@pytest.fixture
def response_factory() -> Callable[..., MagicMock]:
    return make_response


@pytest.fixture
def authenticated_cli() -> AuthenticatedCLI:
    return make_authenticated_cli()


@pytest.fixture
def authenticated_cli_factory() -> Callable[..., AuthenticatedCLI]:
    return make_authenticated_cli


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_config_yaml() -> str:
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
    path = tmp_path_factory.mktemp("audit") / "audit.log"
    monkeypatch.setattr("xnatctl.core.logging.AUDIT_LOG_FILE", path)
    return path


@pytest.fixture(autouse=True)
def isolate_session_cache(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    path = tmp_path_factory.mktemp("session") / ".session"
    monkeypatch.setattr("xnatctl.core.auth.SESSION_CACHE_FILE", path)
    monkeypatch.setattr("xnatctl.core.config.SESSION_CACHE_FILE", path, raising=False)
    return path


@pytest.fixture
def no_ambient_credentials(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    path = tmp_path_factory.mktemp("config") / "config.yaml"
    monkeypatch.setattr("xnatctl.core.config.CONFIG_FILE", path)
    for var in ("XNAT_URL", "XNAT_USER", "XNAT_PASS", "XNAT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return path


class _NullHighlighter:
    def __call__(self, text: object) -> object:
        from rich.text import Text

        return Text(text) if isinstance(text, str) else text


@pytest.fixture(autouse=True)
def disable_rich_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    from xnatctl.core import output

    for con in (output.console, output.err_console):
        monkeypatch.setattr(con, "no_color", True)
        monkeypatch.setattr(con, "_force_terminal", False)
        monkeypatch.setattr(con, "_highlight", False, raising=False)
        monkeypatch.setattr(con, "highlighter", _NullHighlighter(), raising=False)


@pytest.fixture
def clean_root() -> Iterator[None]:
    """Undo what setup_logging and install_log_file change on the logging tree."""
    root = logging.getLogger()
    saved_handlers = [(h, h.level, list(h.filters)) for h in root.handlers]
    saved_level = root.level
    saved_levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    pydicom_propagates = logging.getLogger("pydicom").propagate
    try:
        yield
    finally:
        kept = [h for h, _, _ in saved_handlers]
        for handler in root.handlers:
            if handler not in kept:
                handler.close()
        for handler, level, filters in saved_handlers:
            handler.setLevel(level)
            handler.filters = filters
        root.handlers = kept
        root.setLevel(saved_level)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
        logging.getLogger("pydicom").propagate = pydicom_propagates
