"""Core modules for xnatctl."""

from importlib import import_module
from typing import TYPE_CHECKING

from xnatctl.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConnectionError,
    DownloadError,
    InputValidationError,
    NetworkError,
    OperationError,
    RequestTimeoutError,
    ResourceNotFoundError,
    RetryExhaustedError,
    UploadError,
    ValidationError,
    XNATConnectionError,
    XNATCtlError,
)
from xnatctl.core.logging import LogContext, get_audit_logger, get_logger, setup_logging

# Config, the client, auth, output (Rich), and validation (which pulls in httpx
# via core.timeouts) are exported lazily (PEP 562): importing this package is
# on the path of every `import xnatctl`, and eagerly loading them pulls in
# httpx/Rich/Click for callers who only want the exception hierarchy or
# logging helpers. `from xnatctl.core import XNATClient` still works; it just
# imports the submodule on first use.
if TYPE_CHECKING:
    from xnatctl.core.auth import AuthManager
    from xnatctl.core.client import XNATClient
    from xnatctl.core.config import CONFIG_DIR, CONFIG_FILE, Config, Profile
    from xnatctl.core.output import (
        OutputFormat,
        console,
        print_error,
        print_json,
        print_output,
        print_success,
        print_table,
        print_warning,
    )
    from xnatctl.core.validation import (
        validate_ae_title,
        validate_archive_path,
        validate_path_exists,
        validate_path_writable,
        validate_port,
        validate_project_id,
        validate_regex_pattern,
        validate_resource_label,
        validate_scan_id,
        validate_server_url,
        validate_session_id,
        validate_subject_id,
        validate_timeout,
        validate_workers,
    )

_LAZY_EXPORTS = {
    "AuthManager": "xnatctl.core.auth",
    "XNATClient": "xnatctl.core.client",
    "Config": "xnatctl.core.config",
    "Profile": "xnatctl.core.config",
    "CONFIG_DIR": "xnatctl.core.config",
    "CONFIG_FILE": "xnatctl.core.config",
    "OutputFormat": "xnatctl.core.output",
    "console": "xnatctl.core.output",
    "print_error": "xnatctl.core.output",
    "print_json": "xnatctl.core.output",
    "print_output": "xnatctl.core.output",
    "print_success": "xnatctl.core.output",
    "print_table": "xnatctl.core.output",
    "print_warning": "xnatctl.core.output",
    "validate_ae_title": "xnatctl.core.validation",
    "validate_archive_path": "xnatctl.core.validation",
    "validate_path_exists": "xnatctl.core.validation",
    "validate_path_writable": "xnatctl.core.validation",
    "validate_port": "xnatctl.core.validation",
    "validate_project_id": "xnatctl.core.validation",
    "validate_regex_pattern": "xnatctl.core.validation",
    "validate_resource_label": "xnatctl.core.validation",
    "validate_scan_id": "xnatctl.core.validation",
    "validate_server_url": "xnatctl.core.validation",
    "validate_session_id": "xnatctl.core.validation",
    "validate_subject_id": "xnatctl.core.validation",
    "validate_timeout": "xnatctl.core.validation",
    "validate_workers": "xnatctl.core.validation",
}


def __getattr__(name: str) -> object:
    """Resolve the lazily-exported names (PEP 562)."""
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_path), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include the lazy exports so ``dir(xnatctl.core)`` shows the full surface."""
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    # Exceptions
    "XNATCtlError",
    "AuthenticationError",
    "ConfigurationError",
    "XNATConnectionError",
    "NetworkError",
    "RequestTimeoutError",
    "ResourceNotFoundError",
    "InputValidationError",
    "OperationError",
    "UploadError",
    "DownloadError",
    "RetryExhaustedError",
    # Deprecated shadowing aliases (emit DeprecationWarning on instantiation).
    "ConnectionError",
    "ValidationError",
    # Validation
    "validate_server_url",
    "validate_port",
    "validate_project_id",
    "validate_subject_id",
    "validate_session_id",
    "validate_scan_id",
    "validate_resource_label",
    "validate_ae_title",
    "validate_path_exists",
    "validate_path_writable",
    "validate_archive_path",
    "validate_timeout",
    "validate_workers",
    "validate_regex_pattern",
    # Config
    "Config",
    "Profile",
    "CONFIG_DIR",
    "CONFIG_FILE",
    # Client
    "XNATClient",
    # Auth
    "AuthManager",
    # Output
    "OutputFormat",
    "print_output",
    "print_table",
    "print_json",
    "print_error",
    "print_warning",
    "print_success",
    "console",
    # Logging
    "get_logger",
    "get_audit_logger",
    "setup_logging",
    "LogContext",
]
