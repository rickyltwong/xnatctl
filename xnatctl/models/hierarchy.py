"""Hierarchy refs and API envelope DTOs for XNAT resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import Field

from .base import BaseModel


@dataclass(frozen=True, slots=True)
class ProjectRef:
    """Reference to a project."""

    project_id: str


@dataclass(frozen=True, slots=True)
class SubjectRef:
    """Reference to a subject.

    When ``project_id`` is set, ``subject`` may be either a subject ID or a
    project-scoped subject label. Without a project, XNAT only supports subject
    IDs.
    """

    subject: str
    project_id: str | None = None
    is_label: bool = False


@dataclass(frozen=True, slots=True)
class ExperimentRef:
    """Reference to an experiment/session."""

    experiment: str
    project_id: str | None = None
    subject: str | None = None
    experiment_is_label: bool = False
    subject_is_label: bool = False


@dataclass(frozen=True, slots=True)
class ScanRef:
    """Reference to a scan within an experiment."""

    experiment: ExperimentRef
    scan_id: str


HierarchyParentRef = ProjectRef | SubjectRef | ExperimentRef | ScanRef


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """Reference to a resource attached to any hierarchy level."""

    parent: HierarchyParentRef
    resource_label: str


class ResultSetData(BaseModel):
    """`ResultSet` payload wrapper used by table-style XNAT responses."""

    results: list[dict[str, Any]] = Field(default_factory=list, alias="Result")


class ResultSetEnvelope(BaseModel):
    """Top-level wrapper for `ResultSet.Result` responses."""

    result_set: ResultSetData = Field(default_factory=ResultSetData, alias="ResultSet")

    @property
    def rows(self) -> list[dict[str, Any]]:
        """Return normalized table rows."""
        return self.result_set.results


class ItemRecord(BaseModel):
    """Single `items[]` record returned by detailed XNAT endpoints."""

    data_fields: dict[str, Any] = Field(default_factory=dict)
    children: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class ItemsEnvelope(BaseModel):
    """Top-level wrapper for `items[]` responses."""

    items: list[ItemRecord] = Field(default_factory=list)

    @property
    def first_item(self) -> ItemRecord | None:
        """Return the first item if present."""
        return self.items[0] if self.items else None


class ResolvedSubjectRef(BaseModel):
    """Canonical subject identity resolved from the API."""

    project_id: str | None = None
    subject_id: str
    subject_label: str | None = None
    uri: str | None = None


class ResolvedExperimentRef(BaseModel):
    """Canonical experiment identity resolved from the API."""

    project_id: str | None = None
    subject_id: str | None = None
    subject_label: str | None = None
    experiment_id: str
    experiment_label: str | None = None
    session_date: str | None = None
    xsi_type: str | None = None
    uri: str | None = None
