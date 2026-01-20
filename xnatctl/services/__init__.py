"""Service layer for XNAT operations.

Provides service classes that encapsulate XNAT REST API operations.
"""

from __future__ import annotations

from .base import BaseService
from .projects import ProjectService
from .subjects import SubjectService
from .sessions import SessionService
from .scans import ScanService
from .resources import ResourceService
from .downloads import DownloadService
from .uploads import UploadService
from .prearchive import PrearchiveService
from .pipelines import PipelineService
from .admin import AdminService

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
