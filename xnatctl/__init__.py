"""xnatctl - A CLI for standardized XNAT REST workflows.

This package provides a command-line interface for interacting with XNAT
neuroimaging data servers, supporting common workflows like:
- List, inspect, and manage projects/subjects/sessions
- Download and upload data with parallel execution
- Trigger pipelines and monitor jobs
- Administrative operations (catalogs, users, renaming)
"""

__version__ = "0.1.0"
__author__ = "CAMH KCNI"

from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConnectionError,
    NetworkError,
    ResourceNotFoundError,
    ValidationError,
    XNATCtlError,
)

__all__ = [
    "__version__",
    "XNATClient",
    "Config",
    "Profile",
    "XNATCtlError",
    "AuthenticationError",
    "ConfigurationError",
    "ConnectionError",
    "NetworkError",
    "ResourceNotFoundError",
    "ValidationError",
]
