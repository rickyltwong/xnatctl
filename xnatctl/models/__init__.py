"""Data models for xnatctl.

Provides Pydantic models for XNAT resources and operation progress tracking.
"""

from __future__ import annotations

from .base import BaseModel, XNATResource
from .project import Project
from .subject import Subject
from .session import Session, Experiment
from .scan import Scan
from .resource import Resource, ResourceFile
from .progress import (
    OperationPhase,
    Progress,
    UploadProgress,
    DownloadProgress,
    OperationResult,
    UploadSummary,
    DownloadSummary,
)

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
