"""Data models for xnatctl.

Provides Pydantic models for XNAT resources and operation progress tracking.
"""

from __future__ import annotations

from .base import BaseModel, XNATResource
from .hierarchy import (
    ExperimentRef,
    ItemRecord,
    ItemsEnvelope,
    ProjectRef,
    ResolvedExperimentRef,
    ResolvedSubjectRef,
    ResourceRef,
    ResultSetData,
    ResultSetEnvelope,
    ScanRef,
    SubjectRef,
)
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
    "ProjectRef",
    "SubjectRef",
    "ExperimentRef",
    "ScanRef",
    "ResourceRef",
    "ResultSetData",
    "ResultSetEnvelope",
    "ItemRecord",
    "ItemsEnvelope",
    "ResolvedSubjectRef",
    "ResolvedExperimentRef",
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
