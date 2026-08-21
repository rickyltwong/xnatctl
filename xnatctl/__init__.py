"""xnatctl - A CLI and Python library for standardized XNAT REST workflows.

As a library, the one-call entry point is
:meth:`XNATClient.from_profile <xnatctl.core.client.XNATClient.from_profile>`::

    import xnatctl

    with xnatctl.XNATClient.from_profile("prod") as client:
        projects = client.projects.list()

The client exposes a bound, cached service for each resource type
(``client.projects``, ``client.sessions``, ``client.downloads``, ...). This
module re-exports the client, config, service classes, resource/progress
models, and the full exception hierarchy as the supported public surface.
"""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

try:
    __version__ = version("xnatctl")
except PackageNotFoundError:  # source checkout without an installed distribution
    __version__ = "0.0.0.dev0"

__author__ = "Ricky Wong"

from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config, Profile
from xnatctl.core.connect import build_client_from_profile
from xnatctl.core.exceptions import (
    AuthenticationError,
    BatchOperationError,
    ClientRequestError,
    ConfigurationError,
    ConnectionError,
    DicomError,
    DicomParseError,
    DicomStoreError,
    DownloadError,
    HTTPResponseError,
    InputValidationError,
    InvalidIdentifierError,
    InvalidPortError,
    InvalidURLError,
    NetworkError,
    NoConfigurationError,
    OperationCancelledError,
    OperationError,
    PathValidationError,
    PermissionDeniedError,
    ProfileNotFoundError,
    RequestTimeoutError,
    ResourceError,
    ResourceExistsError,
    ResourceNotFoundError,
    RetryExhaustedError,
    ServerError,
    ServerUnreachableError,
    SessionExpiredError,
    TimeoutError,
    TransferCircuitBreakerError,
    TransferConfigError,
    TransferConflictError,
    TransferError,
    TransferVerificationError,
    UploadError,
    ValidationError,
    XNATConnectionError,
    XNATCtlError,
)

# Service classes and resource/progress models are exported lazily (PEP 562):
# importing them eagerly nearly doubled cold `import xnatctl` -- a cost every
# CLI invocation pays -- for names most programs never touch. `from xnatctl
# import ProjectService` still works; it just imports the submodule on first
# use.
if TYPE_CHECKING:
    from xnatctl.models.progress import (
        DownloadProgress,
        DownloadSummary,
        OperationPhase,
        OperationResult,
        Progress,
        UploadProgress,
        UploadSummary,
        VerificationReport,
    )
    from xnatctl.models.project import Project
    from xnatctl.models.resource import Resource, ResourceFile
    from xnatctl.models.scan import Scan
    from xnatctl.models.session import Session
    from xnatctl.models.subject import Subject
    from xnatctl.services.admin import AdminService
    from xnatctl.services.downloads import DownloadService
    from xnatctl.services.exam_upload import ExamUploadService
    from xnatctl.services.hierarchy import HierarchyService
    from xnatctl.services.pipelines import PipelineService
    from xnatctl.services.prearchive import PrearchiveService
    from xnatctl.services.projects import ProjectService
    from xnatctl.services.resources import ResourceService
    from xnatctl.services.scans import ScanService
    from xnatctl.services.sessions import SessionService
    from xnatctl.services.subjects import SubjectService
    from xnatctl.services.upload import UploadService

_LAZY_EXPORTS = {
    "DownloadProgress": "xnatctl.models.progress",
    "DownloadSummary": "xnatctl.models.progress",
    "OperationPhase": "xnatctl.models.progress",
    "OperationResult": "xnatctl.models.progress",
    "Progress": "xnatctl.models.progress",
    "UploadProgress": "xnatctl.models.progress",
    "UploadSummary": "xnatctl.models.progress",
    "VerificationReport": "xnatctl.models.progress",
    "Project": "xnatctl.models.project",
    "Resource": "xnatctl.models.resource",
    "ResourceFile": "xnatctl.models.resource",
    "Scan": "xnatctl.models.scan",
    "Session": "xnatctl.models.session",
    "Subject": "xnatctl.models.subject",
    "AdminService": "xnatctl.services.admin",
    "DownloadService": "xnatctl.services.downloads",
    "ExamUploadService": "xnatctl.services.exam_upload",
    "HierarchyService": "xnatctl.services.hierarchy",
    "PipelineService": "xnatctl.services.pipelines",
    "PrearchiveService": "xnatctl.services.prearchive",
    "ProjectService": "xnatctl.services.projects",
    "ResourceService": "xnatctl.services.resources",
    "ScanService": "xnatctl.services.scans",
    "SessionService": "xnatctl.services.sessions",
    "SubjectService": "xnatctl.services.subjects",
    "UploadService": "xnatctl.services.upload",
}


def __getattr__(name: str) -> Any:
    """Resolve the lazily-exported service and model names (PEP 562)."""
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_path), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include the lazy exports so ``dir(xnatctl)`` shows the full surface."""
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    "AdminService",
    "AuthenticationError",
    "BatchOperationError",
    "ClientRequestError",
    "Config",
    "ConfigurationError",
    "ConnectionError",
    "DicomError",
    "DicomParseError",
    "DicomStoreError",
    "DownloadError",
    "DownloadProgress",
    "DownloadService",
    "DownloadSummary",
    "ExamUploadService",
    "HTTPResponseError",
    "HierarchyService",
    "InputValidationError",
    "InvalidIdentifierError",
    "InvalidPortError",
    "InvalidURLError",
    "NetworkError",
    "NoConfigurationError",
    "OperationCancelledError",
    "OperationError",
    "OperationPhase",
    "OperationResult",
    "PathValidationError",
    "PermissionDeniedError",
    "PipelineService",
    "PrearchiveService",
    "Profile",
    "ProfileNotFoundError",
    "Progress",
    "Project",
    "ProjectService",
    "RequestTimeoutError",
    "Resource",
    "ResourceError",
    "ResourceExistsError",
    "ResourceFile",
    "ResourceNotFoundError",
    "ResourceService",
    "RetryExhaustedError",
    "Scan",
    "ScanService",
    "ServerError",
    "ServerUnreachableError",
    "Session",
    "SessionExpiredError",
    "SessionService",
    "Subject",
    "SubjectService",
    "TimeoutError",
    "TransferCircuitBreakerError",
    "TransferConfigError",
    "TransferConflictError",
    "TransferError",
    "TransferVerificationError",
    "UploadError",
    "UploadProgress",
    "UploadService",
    "UploadSummary",
    "ValidationError",
    "VerificationReport",
    "XNATClient",
    "XNATConnectionError",
    "XNATCtlError",
    "__version__",
    "build_client_from_profile",
]
