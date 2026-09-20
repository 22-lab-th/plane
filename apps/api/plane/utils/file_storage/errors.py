# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Machine-readable upload failures (ARCH-001 §4.2).

The upload endpoints answer a failed attempt with HTTP 400 and a stable
``code``: ``size_mismatch``, ``mime_mismatch``, ``object_missing`` and
``quota_exceeded`` are the codes the architecture fixes; ``storage_unavailable``,
``verification_failed`` and ``not_uploading`` cover the remaining cases, which
must never be reported as a success (AD-05).

Subclassing DRF's :class:`APIException` means a raised failure becomes the right
response without every endpoint re-implementing the mapping, and the body stays
``{"error": ..., "code": ...}`` rather than DRF's default ``{"detail": ...}``.
"""

# Third party imports
from rest_framework.exceptions import APIException


class FileUploadError(APIException):
    """A failed upload step, rendered as ``{"error": ..., "code": ...}``."""

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
