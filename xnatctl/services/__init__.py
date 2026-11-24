"""Service layer for XNAT operations.

Provides service classes that encapsulate XNAT REST API operations.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

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
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_path, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
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
