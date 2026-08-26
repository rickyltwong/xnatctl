"""Server URL, port, timeout, and worker-count validation.

Covers everything needed to validate the network-facing side of a profile
or CLI option: the XNAT server URL (:func:`validate_server_url`,
:func:`validate_url_or_none`), a TCP port (:func:`validate_port`), and the
two config-ish numeric knobs that gate a connection or a batch of parallel
requests -- request timeout and worker count
(:func:`validate_timeout`, :func:`validate_workers`).
"""

from __future__ import annotations

from urllib.parse import urlparse

from xnatctl.core.exceptions import ConfigurationError, InvalidPortError, InvalidURLError
from xnatctl.core.redact import redact_url_userinfo
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS

# =============================================================================
# Constants
# =============================================================================

MIN_PORT = 1
MAX_PORT = 65535

ALLOWED_URL_SCHEMES = {"http", "https"}


# =============================================================================
# URL Validation
# =============================================================================


def validate_server_url(url: str) -> str:
    """Validate XNAT server URL and return normalized form.

    Args:
        url: Server URL to validate.

    Returns:
        Normalized URL (trailing slash removed).

    Raises:
        InvalidURLError: If URL is malformed or uses unsupported scheme.
    """
    if not url or not isinstance(url, str):
        raise InvalidURLError(str(url), "URL cannot be empty")

    url = url.strip()
    if not url:
        raise InvalidURLError(url, "URL cannot be empty")

    # Every raise below reports the redacted form: InvalidURLError echoes the
    # value into its message *and* keeps it as `.value`, so a rejected
    # `https://admin:s3cret@host` would otherwise leak the password through the
    # error path it was rejected by.
    safe = redact_url_userinfo(url)

    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise InvalidURLError(safe, f"Failed to parse URL: {redact_url_userinfo(str(e))}") from e

    if not parsed.scheme:
        raise InvalidURLError(safe, "URL must include scheme (http:// or https://)")

    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise InvalidURLError(
            safe,
            f"Unsupported scheme '{parsed.scheme}'. Use http or https.",
        )

    if not parsed.netloc:
        raise InvalidURLError(safe, "URL must include hostname")

    # Reject embedded credentials rather than stripping them. Stripping would
    # silently drop credentials the user believed were in effect, and the URL
    # is copied into `base_url`, which surfaces in error messages, log lines
    # and `config show`.
    if parsed.username or parsed.password:
        raise InvalidURLError(
            safe,
            "Do not embed credentials in the URL. Use a profile, "
            "XNAT_USER/XNAT_PASS, or `xnatctl auth login`.",
        )

    return url.rstrip("/")


def validate_url_or_none(url: str | None) -> str | None:
    """Validate URL if provided, or return None."""
    if url is None or (isinstance(url, str) and not url.strip()):
        return None
    return validate_server_url(url)


# =============================================================================
# Port Validation
# =============================================================================


def validate_port(port: int | str | None, allow_none: bool = False) -> int | None:
    """Validate port number.

    Args:
        port: Port number to validate.
        allow_none: If True, None is a valid value.

    Returns:
        Validated port number or None.

    Raises:
        InvalidPortError: If port is invalid.
    """
    if port is None:
        if allow_none:
            return None
        raise InvalidPortError(port)

    try:
        port_int = int(port)
    except (ValueError, TypeError) as e:
        raise InvalidPortError(port) from e

    if port_int < MIN_PORT or port_int > MAX_PORT:
        raise InvalidPortError(port)

    return port_int


# =============================================================================
# Configuration Validation
# =============================================================================


def validate_timeout(
    value: int | float | str | None,
    field_name: str = "timeout",
    *,
    min_value: int = 1,
    max_value: int = 86400 * 30,  # 30 days
    default: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> int:
    """Validate timeout value in seconds.

    Args:
        value: Timeout value to validate.
        field_name: Field name for error messages.
        min_value: Minimum allowed value.
        max_value: Maximum allowed value.
        default: Default value if None.

    Returns:
        Validated timeout in seconds.

    Raises:
        ConfigurationError: If timeout is invalid.
    """
    if value is None:
        return default

    try:
        timeout = int(value)
    except (ValueError, TypeError) as e:
        raise ConfigurationError(
            f"{field_name} must be a valid integer",
            field_name,
            value,
        ) from e

    if timeout < min_value:
        raise ConfigurationError(
            f"{field_name} must be at least {min_value} seconds",
            field_name,
            timeout,
        )

    if timeout > max_value:
        raise ConfigurationError(
            f"{field_name} cannot exceed {max_value} seconds",
            field_name,
            timeout,
        )

    return timeout


def validate_workers(
    value: int | str | None,
    field_name: str = "workers",
    *,
    min_value: int = 1,
    max_value: int = 100,
    default: int = 4,
) -> int:
    """Validate worker count for parallel operations.

    Args:
        value: Worker count to validate.
        field_name: Field name for error messages.
        min_value: Minimum allowed value.
        max_value: Maximum allowed value.
        default: Default value if None.

    Returns:
        Validated worker count.

    Raises:
        ConfigurationError: If value is invalid.
    """
    if value is None:
        return default

    try:
        workers = int(value)
    except (ValueError, TypeError) as e:
        raise ConfigurationError(
            f"{field_name} must be a valid integer",
            field_name,
            value,
        ) from e

    if workers < min_value:
        raise ConfigurationError(
            f"{field_name} must be at least {min_value}",
            field_name,
            workers,
        )

    if workers > max_value:
        raise ConfigurationError(
            f"{field_name} cannot exceed {max_value}",
            field_name,
            workers,
        )

    return workers
