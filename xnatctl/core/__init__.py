"""Core modules for xnatctl."""

from xnatctl.core.auth import AuthManager
from xnatctl.core.client import XNATClient
from xnatctl.core.config import CONFIG_DIR, CONFIG_FILE, Config, Profile
from xnatctl.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConnectionError,
    DownloadError,
    NetworkError,
    OperationError,
    ResourceNotFoundError,
    RetryExhaustedError,
    UploadError,
    ValidationError,
    XNATCtlError,
)
from xnatctl.core.logging import LogContext, get_audit_logger, get_logger, setup_logging
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

__all__ = [
    # Exceptions
    "XNATCtlError",
    "AuthenticationError",
    "ConfigurationError",
    "ConnectionError",
    "NetworkError",
    "ResourceNotFoundError",
    "ValidationError",
    "OperationError",
    "UploadError",
    "DownloadError",
    "RetryExhaustedError",
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
