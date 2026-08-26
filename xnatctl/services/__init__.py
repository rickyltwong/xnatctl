"""Service layer for XNAT operations.

Provides service classes that encapsulate XNAT REST API operations.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

# Lazy (PEP 562) rather than eager re-exports: ``downloads`` and ``upload``
# both import ``xnatctl.core.client`` at their own module scope (their
# transports need a real ``XNATClient`` reference, not just a type), so an
# eager ``from .downloads import DownloadService`` here would make importing
# ANY single service submodule -- e.g. ``xnatctl.services.admin`` -- run this
# package's __init__ first and close core.client -> services -> core.client.
# Nothing in this codebase reads a class off the package namespace
# (``xnatctl.services.AdminService``); every call site already does
# ``from xnatctl.services.admin import AdminService``, so resolving these
# lazily costs nothing real callers rely on eagerly.
if TYPE_CHECKING:
    from .admin import AdminService
    from .base import BaseService
    from .downloads import DownloadService
    from .hierarchy import HierarchyService
    from .pipelines import PipelineService
    from .prearchive import PrearchiveService
    from .projects import ProjectService
    from .resources import ResourceService
    from .scans import ScanService
    from .sessions import SessionService
    from .subjects import SubjectService
    from .upload import UploadService

_LAZY_EXPORTS = {
    "AdminService": ".admin",
    "BaseService": ".base",
    "DownloadService": ".downloads",
    "HierarchyService": ".hierarchy",
    "PipelineService": ".pipelines",
    "PrearchiveService": ".prearchive",
    "ProjectService": ".projects",
    "ResourceService": ".resources",
    "ScanService": ".scans",
    "SessionService": ".sessions",
    "SubjectService": ".subjects",
    "UploadService": ".upload",
}


def __getattr__(name: str) -> object:
    """Resolve the lazily-exported service class names (PEP 562)."""
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_path, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include the lazy exports so ``dir(xnatctl.services)`` shows the full surface."""
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    "BaseService",
    "ProjectService",
    "SubjectService",
    "SessionService",
    "ScanService",
    "ResourceService",
    "DownloadService",
    "HierarchyService",
    "UploadService",
    "PrearchiveService",
    "PipelineService",
    "AdminService",
]
