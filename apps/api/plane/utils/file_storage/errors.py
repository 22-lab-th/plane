# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Machine-readable project-file failures (ARCH-001 §4).

Every project-file endpoint that has to refuse answers with a stable ``code``, and
**this docstring is the vocabulary contract**: the UI tickets read it rather than
the raise sites, so a new code is added here in the same commit that introduces it.
``file_quarantined`` and the codes the later phases add are listed with the reason
they exist, not only the ones a test currently asserts.

Uploads
    ``size_mismatch`` (the stored object's size contradicts the declaration),
    ``mime_mismatch``, ``object_missing`` (nothing at the signed key),
    ``verification_failed`` (a finalize could not verify the upload; also a
    cross-project **move** whose copied object did not match its source),
    ``not_uploading`` (a finalize or abort for an attempt
    that already settled), ``upload_in_progress`` (a second attempt for a file with
    a live reservation), ``quota_exceeded`` (workspace or project ceiling, carrying
    ``level`` and integer byte fields).

Storage
    ``storage_unavailable`` (the provider could not sign, copy, or delete);
    ``object_unavailable`` (no stored object can be served: no active version, a
    purged/purge_failed version, or an object the store no longer has).

Folders
    ``folder_name_conflict``, ``folder_not_found``, ``folder_trashed``,
    ``folder_not_empty``, ``folder_cycle``, ``depth_limit_exceeded``,
    ``folder_conflict`` (an integrity failure this mapping could not attribute).

Files and the trash lifecycle
    ``file_name_conflict``, ``name_conflict`` (no unique name could be derived after
    the suffix attempts), ``file_trashed`` (the row is in the trash),
    ``file_not_trashed`` (restore or purge asked for a live file),
    ``retention_expired`` (restore asked for a file whose retention window has
    elapsed, so the purge owns it - R-DEL-2's edge case; the body carries
    ``retention_days``),
    ``project_archived`` (an archived project is read-only),
    ``confirmation_required`` (an irreversible purge without ``confirm=true``),
    ``permission_denied`` (the caller is a member but lacks the role).

Links
    ``link_exists`` (this file is already linked to that entity),
    ``unsupported_entity_type`` (a target this deployment cannot validate).

Requests
    ``invalid_request`` (a value or target that is wrong, carrying ``field``),
    ``unsupported_field`` (a payload field this endpoint does not implement - the
    cross-project ``target_project_id`` on ``copy/``, whose routes are separate).

Cross-project copy and move (R-OPS-4, AD-17)
    ``cross_workspace_not_supported`` (the destination project is in another
    workspace, so the operation is refused entirely), ``verification_failed`` (a
    move's copied object did not match the source, so the copy was rolled back and
    the source kept - the body carries ``reason`` and the observed evidence), the
    shared ``permission_denied`` (the caller may only read the destination) and the
    neutral 404 (a destination the caller cannot see). A move whose *source* could not
    be purged answers ``storage_unavailable`` with the source left in the trash.

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
