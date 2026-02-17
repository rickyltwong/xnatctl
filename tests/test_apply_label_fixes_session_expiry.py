"""Regression tests for session-expiry handling in apply_label_fixes.

These tests ensure that a session-expiry signal is not swallowed and converted
into an "error count" during long-running batch rename operations.
"""

from __future__ import annotations

import pytest

import scripts.apply_label_fixes as alf
from xnatctl.core.exceptions import SessionExpiredError


def test_apply_subject_patterns_propagates_session_expired(monkeypatch):
    """Session expiry during subject rename should abort the run."""

    monkeypatch.setattr(
        alf,
        "_list_subjects",
        lambda _client, _project: [{"label": "OLD", "ID": "S1"}],
    )

    def _raise_session_expired(*_args, **_kwargs) -> None:
        raise SessionExpiredError("https://example.org")

    monkeypatch.setattr(alf, "_rename_subject", _raise_session_expired)

    patterns = [{"match": r"^OLD$", "to": "NEW"}]

    with pytest.raises(SessionExpiredError):
        alf.apply_subject_patterns(object(), "PROJ", patterns, execute=True)


def test_apply_experiment_label_fixes_propagates_session_expired(monkeypatch):
    """Session expiry during experiment rename should abort the run."""

    monkeypatch.setattr(
        alf,
        "_list_subjects",
        lambda _client, _project: [{"label": "PROJ_S1", "ID": "S1"}],
    )
    monkeypatch.setattr(
        alf,
        "_list_subject_experiments",
        lambda _client, _project, _subject: [
            {
                "ID": "E1",
                "label": "OLD_LABEL",
                "xsiType": "xnat:mrsessiondata",
                "date": "2020-01-01",
                "time": "10:00",
                "insert_date": "2020-01-01 10:00",
                "insert_time": "10:00",
            }
        ],
    )

    def _raise_session_expired(*_args, **_kwargs) -> None:
        raise SessionExpiredError("https://example.org")

    monkeypatch.setattr(alf, "_rename_experiment", _raise_session_expired)

    with pytest.raises(SessionExpiredError):
        alf.apply_experiment_label_fixes(object(), "PROJ", execute=True)
