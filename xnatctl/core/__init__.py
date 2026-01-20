"""Core modules for xnatctl."""

from xnatctl.core.exceptions import (
    XNATCtlError,
    AuthenticationError,
    ConfigurationError,
    ConnectionError,
    NetworkError,
    ResourceNotFoundError,
    ValidationError,
    OperationError,
    UploadError,
    DownloadError,
    RetryExhaustedError,
)
from xnatctl.core.validation import (
    validate_server_url,
    validate_port,
    validate_project_id,
    validate_subject_id,
    validate_session_id,
    validate_scan_id,
    validate_resource_label,
    validate_ae_title,
    validate_path_exists,
    validate_path_writable,
    validate_archive_path,
    validate_timeout,
    validate_workers,
    validate_regex_pattern,
)
from xnatctl.core.config import Config, Profile, CONFIG_DIR, CONFIG_FILE
from xnatctl.core.client import XNATClient
from xnatctl.core.auth import AuthManager
from xnatctl.core.output import (
    OutputFormat,
    print_output,
    print_table,
    print_json,
    print_error,
    print_warning,
    print_success,
    console,
)
from xnatctl.core.logging import get_logger, get_audit_logger, setup_logging, LogContext

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
