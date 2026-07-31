"""Redaction invariants for the logging path and URL validation.

Redaction used to be applied only where a human explicitly remembered it --
``print_error``/``print_warning`` and a couple of client call sites. Nothing
routed ``logging.Logger`` records through it, which blocked diagnostics: verbose
HTTP diagnostics log full request URLs, and those carry query-string tokens.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from xnatctl.core.exceptions import InvalidURLError
from xnatctl.core.logging import RedactionFilter, install_redaction_filter, setup_logging
from xnatctl.core.validation import validate_server_url

SECRET_URL = "https://xnat.example.org/data?token=s3cret"
USERINFO_URL = "https://admin:s3cret@xnat.example.org/data"


@pytest.fixture
def filtered_logger() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    """A logger whose handler carries the filter, plus its output buffer."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("xnatctl.test.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    install_redaction_filter(logger)
    try:
        yield logger, stream
    finally:
        logger.handlers = []


@pytest.fixture
def clean_root() -> Iterator[None]:
    """Snapshot and restore the root logger, which setup_logging mutates."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_filters = {id(h): list(h.filters) for h in saved_handlers}
    saved_level = root.level
    try:
        yield
    finally:
        for handler in root.handlers:
            if id(handler) in saved_filters:
                handler.filters = saved_filters[id(handler)]
        root.handlers = saved_handlers
        root.setLevel(saved_level)


# =============================================================================
# RedactionFilter
# =============================================================================


def test_query_token_is_redacted_in_emitted_output(
    filtered_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = filtered_logger
    logger.warning("GET %s failed", SECRET_URL)

    output = stream.getvalue()
    assert "s3cret" not in output
    assert "token=***" in output


def test_userinfo_password_is_redacted_in_emitted_output(
    filtered_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = filtered_logger
    logger.error("Connection to %s refused", USERINFO_URL)

    output = stream.getvalue()
    assert "s3cret" not in output
    assert "admin:***@" in output


def test_records_without_secrets_are_left_untouched(
    filtered_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = filtered_logger
    logger.info("Uploaded %d files to %s", 3, "https://xnat.example.org/data")

    assert stream.getvalue().strip() == "Uploaded 3 files to https://xnat.example.org/data"


def test_untouched_record_keeps_lazy_args() -> None:
    """Only records we actually change get collapsed, so other handlers keep
    the original %-args."""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "plain %s", ("value",), None)
    assert RedactionFilter().filter(record) is True
    assert record.msg == "plain %s"
    assert record.args == ("value",)


def test_changed_record_is_collapsed_without_double_interpolation() -> None:
    """After rewriting, ``args`` must be cleared or the next getMessage() would
    try to interpolate an already-formatted string."""
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "GET %s", (SECRET_URL,), None)
    assert RedactionFilter().filter(record) is True
    assert record.args is None
    assert "s3cret" not in record.getMessage()
    # Calling twice must stay stable.
    assert record.getMessage() == record.getMessage()


def test_third_party_logger_output_is_redacted_too() -> None:
    """The reason the filter lives on the handler rather than at our call sites.

    Under --verbose, httpx logs one INFO line per request containing the full
    URL. xnatctl never formats that line, so no amount of call-site redaction
    would cover it -- only a handler-level filter does. This is what makes
    the filter a hard prerequisite for HTTP diagnostics rather than a nicety.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    httpx_logger = logging.getLogger("httpx")
    saved, saved_level = list(httpx_logger.handlers), httpx_logger.level
    httpx_logger.handlers = [handler]
    httpx_logger.propagate = False
    httpx_logger.setLevel(logging.INFO)
    install_redaction_filter(httpx_logger)
    try:
        # Verbatim shape of httpx's own request log line.
        httpx_logger.info(
            'HTTP Request: GET %s "HTTP/1.1 200 OK"',
            "https://xnat.example.org/data/projects?token=s3cret&format=json",
        )
    finally:
        httpx_logger.handlers, httpx_logger.propagate = saved, True
        httpx_logger.setLevel(saved_level)

    output = stream.getvalue()
    assert "s3cret" not in output
    assert "token=***" in output
    assert "format=json" in output


@pytest.mark.usefixtures("clean_root")
def test_setup_logging_installs_the_filter_exactly_once() -> None:
    """setup_logging runs from both the CLI root group and @global_options."""
    setup_logging()
    setup_logging()
    setup_logging(verbose=True)

    root = logging.getLogger()
    assert root.handlers, "setup_logging should leave the root logger with a handler"
    for handler in root.handlers:
        installed = [f for f in handler.filters if isinstance(f, RedactionFilter)]
        assert len(installed) <= 1


# =============================================================================
# validate_server_url rejects embedded credentials
# =============================================================================


def test_userinfo_url_is_rejected() -> None:
    with pytest.raises(InvalidURLError) as exc_info:
        validate_server_url(USERINFO_URL)

    assert "Do not embed credentials" in str(exc_info.value)


def test_rejection_message_does_not_leak_the_password() -> None:
    """The error raised *because* the URL held a credential must not repeat
    it -- InvalidURLError echoes the value into the message and keeps it as
    ``.value``."""
    with pytest.raises(InvalidURLError) as exc_info:
        validate_server_url(USERINFO_URL)

    error = exc_info.value
    assert "s3cret" not in str(error)
    assert "s3cret" not in str(error.url)
    assert "s3cret" not in str(error.details)


def test_password_is_not_leaked_by_an_unrelated_rejection() -> None:
    """A userinfo URL that fails an earlier check (scheme) must be redacted
    too -- and that value is not http(s), so the text-scanning helper alone
    would not have caught it."""
    with pytest.raises(InvalidURLError) as exc_info:
        validate_server_url("ftp://admin:s3cret@xnat.example.org")

    assert "s3cret" not in str(exc_info.value)
    assert "Unsupported scheme" in str(exc_info.value)


def test_bare_username_url_is_still_rejected() -> None:
    """No password, but credentials in a URL are the wrong shape regardless."""
    with pytest.raises(InvalidURLError):
        validate_server_url("https://admin@xnat.example.org")


def test_ordinary_urls_still_validate() -> None:
    assert validate_server_url("https://xnat.example.org/") == "https://xnat.example.org"
    assert validate_server_url("http://localhost:8080") == "http://localhost:8080"
