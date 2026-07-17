"""Centralized timeout defaults for xnatctl."""

DEFAULT_HTTP_TIMEOUT_SECONDS = 21600

# Max seconds `session upload-exam` waits for DICOM to finish archiving before
# attaching session resources. Kept generous: large sessions (100k+ files) can
# take well over an hour to archive, and a premature timeout aborts the command
# before resources/misc files are attached.
DEFAULT_ARCHIVE_WAIT_SECONDS = 14400  # 4 hours
