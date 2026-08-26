"""Session upload commands for xnatctl (upload / upload-exam / upload-dicom).

Each command family lives in its own sibling module, split by cohesion:
``session_upload_rest`` (REST/gradual DICOM archive upload), ``session_upload_exam``
(exam-root layout upload), and ``session_upload_dicom`` (C-STORE network transfer).
Importing this module imports all three for their command-registration side
effects on the ``session`` group; it also re-exports their private upload
helpers for callers that import them from here.
"""

from __future__ import annotations

from xnatctl.cli.session_upload_dicom import _upload_dicom_store, session_upload_dicom
from xnatctl.cli.session_upload_exam import session_upload_exam
from xnatctl.cli.session_upload_rest import (
    _upload_directory_parallel,
    _upload_gradual_dicom,
    _upload_single_archive,
    session_upload,
)

__all__ = [
    "_upload_dicom_store",
    "_upload_directory_parallel",
    "_upload_gradual_dicom",
    "_upload_single_archive",
    "session_upload",
    "session_upload_dicom",
    "session_upload_exam",
]
