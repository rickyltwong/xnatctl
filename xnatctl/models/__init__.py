"""Data models for xnatctl.

Provides Pydantic models for XNAT resources and operation progress tracking.
"""

from __future__ import annotations

from .base import BaseModel, XNATResource
from .progress import (
    DownloadProgress,
    DownloadSummary,
    OperationPhase,
    OperationResult,
    Progress,
    UploadProgress,
    UploadSummary,
)
from .project import Project
from .resource import Resource, ResourceFile
from .scan import Scan
from .session import Experiment, Session
from .subject import Subject

__all__ = [
    # Base
    "BaseModel",
    "XNATResource",
    # Resources
    "Project",
    "Subject",
    "Session",
    "Experiment",
    "Scan",
    "Resource",
    "ResourceFile",
    # Progress
    "OperationPhase",
    "Progress",
    "UploadProgress",
    "DownloadProgress",
    "OperationResult",
    "UploadSummary",
    "DownloadSummary",
]
