"""The ``XNATCTL_DEBUG`` wire trace never writes a live session token.

httpcore's DEBUG trace logs every response header, so XNAT's
``Set-Cookie: JSESSIONID=...`` went verbatim to stderr and to the
``--log-file`` diagnostics file: one pasted debug trace leaked a working
session. ``httpx.MockTransport`` never reaches httpcore, so the end-to-end
test here runs a real local HTTP server and asserts on both real outputs.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from xnatctl.core.client import XNATClient
from xnatctl.core.logging import install_log_file, remove_log_file, setup_logging
from xnatctl.core.redact import redact_session_cookies

TOKEN = "CANARYTOKEN0123456789ABCDEF"

#: The trace line shape the leak was found in (httpcore 1.x response headers).
HTTPCORE_HEADERS_LINE = (
    "receive_response_headers.complete return_value=(b'HTTP/1.1', 200, b'OK', "
    f"[(b'Set-Cookie', b'JSESSIONID={TOKEN}; Path=/; HttpOnly'), "
    "(b'Content-Type', b'application/json')])"
)


@pytest.mark.parametrize(
    "text",
    [
        HTTPCORE_HEADERS_LINE,
        f"Cookie: JSESSIONID={TOKEN}",
        f"cookie header jsessionid={TOKEN}; other=1",
        f"(b'Set-Cookie', b'JSESSIONID={TOKEN}')",
        f"(b'Set-Cookie', b'JSESSIONID=\"{TOKEN}\"; Path=/')",
    ],
    ids=[
        "httpcore-set-cookie",
        "cookie-header",
        "lowercase-name",
        "bare-bytes-repr",
        "quoted-value",
    ],
)
def test_session_cookie_value_is_redacted(text: str) -> None:
    redacted = redact_session_cookies(text)

    assert TOKEN not in redacted
    assert "JSESSIONID=***" in redacted.upper()


def test_text_without_a_session_cookie_is_returned_unchanged() -> None:
    text = "receive_response_headers.complete return_value=(b'HTTP/1.1', 200, b'OK', [])"

    assert redact_session_cookies(text) is text


@pytest.fixture
def xnat_setting_a_cookie() -> Iterator[str]:
    """A local server that answers like XNAT does: a fresh JSESSIONID cookie."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
            body = b"{}"
            self.send_response(200)
            self.send_header("Set-Cookie", f"JSESSIONID={TOKEN}; Path=/; HttpOnly")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Undo what setup_logging/install_log_file do to process-global loggers."""
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        yield
    finally:
        remove_log_file()
        root.handlers, root.level = saved[0], saved[1]
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


@pytest.mark.usefixtures("restore_logging")
def test_debug_wire_trace_does_not_write_the_session_token(
    xnat_setting_a_cookie: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XNATCTL_DEBUG", "1")
    # pytest's own capture handler on root would stop setup_logging from
    # adding the stderr handler the CLI really runs with.
    logging.getLogger().handlers = []
    setup_logging(logging.DEBUG)
    log_file = tmp_path / "diag.log"
    install_log_file(log_file)

    client = XNATClient(base_url=xnat_setting_a_cookie, max_retries=0)
    client.get("/data/projects")
    client.close()

    stderr = capsys.readouterr().err
    file_text = log_file.read_text()
    # Not vacuous: the trace really ran and logged the response headers.
    assert "receive_response_headers" in stderr
    assert "receive_response_headers" in file_text
    assert TOKEN not in stderr
    assert TOKEN not in file_text
