# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Machine-readable project-file failures (ARCH-001 §4).

Every project-file endpoint that has to refuse answers with a stable ``code``:
``size_mismatch``, ``mime_mismatch``, ``object_missing`` and ``quota_exceeded``
are the codes the architecture fixes for uploads; ``storage_unavailable``,
``verification_failed``, ``not_uploading``, ``upload_in_progress``,
``project_archived`` and ``invalid_request`` cover the remaining refusals across
the upload and listing endpoints. The operations tickets add ``file_trashed``,
``file_not_trashed``, ``confirmation_required`` (a purge without ``confirm=true``),
``unsupported_field``, ``object_unavailable`` (no stored or active version),
``cross_project_not_supported`` and ``permission_denied``.

Subclassing DRF's :class:`APIException` means a raised failure becomes the right
response without every endpoint re-implementing the mapping, and the body stays
``{"error": ..., "code": ...}`` rather than DRF's default ``{"detail": ...}``.
"""

# Third party imports
from rest_framework.exceptions import APIException


class ProjectFileError(APIException):
    """A refused project-file operation, rendered as ``{"error": ..., "code": ...}``."""

    status_code = 400
    default_code = "invalid_request"
    code = "invalid_request"

    def __init__(self, message, *, code=None, status_code=None, **details):
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details
        self.body = {"error": message, "code": self.code, **details}
        # ``detail`` is what DRF serialises into the response body, and
        # ``APIException.__init__`` overwrites it, so the payload goes in there
        # and is then replaced with the original mapping: DRF wraps every scalar
        # in an ``ErrorDetail`` string, which would turn byte counts into
        # strings in the quota responses.
        super().__init__(detail=dict(self.body))
        self.detail = self.body

    def as_response(self):
        """Return the JSON body for this failure, with its original value types."""
        return dict(self.body)
