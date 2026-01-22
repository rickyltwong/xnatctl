"""Service layer for XNAT operations.

Provides service classes that encapsulate XNAT REST API operations.
"""

from __future__ import annotations

from .admin import AdminService
from .base import BaseService
from .downloads import DownloadService
from .pipelines import PipelineService
from .prearchive import PrearchiveService
from .projects import ProjectService
from .resources import ResourceService
from .scans import ScanService
from .sessions import SessionService
from .subjects import SubjectService
from .uploads import UploadService

__all__ = [
    "BaseService",
    "ProjectService",
    "SubjectService",
    "SessionService",
    "ScanService",
    "ResourceService",
    "DownloadService",
    "UploadService",
    "PrearchiveService",
    "PipelineService",
    "AdminService",
]
