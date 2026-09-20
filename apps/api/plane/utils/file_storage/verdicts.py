# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The one answer to "may this version be served / is this row visible" (ADV-001 §5 P-1).

These predicates live in ``utils`` rather than beside the endpoints that enforce them
because the **serializers** read them too: a file's ``can_download`` and a version's
``can_activate`` are the advertised form of the same answer the delivery and
activation endpoints give, and a serializer importing a view module would make the
import graph circular. One definition, every reader:

* :func:`is_on_default_surface` - the visibility predicate the SQL in
  ``file_queryset`` mirrors;
* :func:`delivery_refusal` - the refusal the delivery endpoints apply, and the one
  ``permissions.can_download`` and ``can_activate`` are derived from.
"""

# Module imports
from plane.db.models import FileObject, FileVersion
from plane.utils.file_storage.errors import ProjectFileError

#: Version states whose object exists and was verified, so it can be served.
GOOD_VERSION_STATUSES = (FileVersion.Status.ACTIVE, FileVersion.Status.SUPERSEDED)


def is_on_default_surface(file_object):
    """True when the default surface shows this row.

    The predicate ``file_queryset(include_trashed=False)`` applies in SQL: a row
    must be live (``deleted_at IS NULL``) **and** not carry the trashed status.
    Trashing sets both (R-FOLD-4); the two can still disagree after a partial
    write, and when they do every path has to agree they mean "not visible".
    """
    return file_object.deleted_at is None and file_object.status != FileObject.Status.TRASHED


def include_trashed(request, project):
    """True when the caller may address rows the default surface hides.

    ``?trashed=true`` asks for the trash explicitly, and a project ADMIN may address
    those rows without the flag. One function, so the detail endpoint and the version
    history cannot answer the question differently (ADV-001 §5 P-1).
    """
    raw = request.query_params.get("trashed")
    flagged = parse_bool(raw, "trashed") if raw not in (None, "") else False

    return flagged or member_role(request, project) == ROLE.ADMIN.value


def delivery_refusal(file_object, version):
    """Return the refusal the delivery endpoints would apply, or ``None``.

    Kept in one place so the ``permissions`` block a detail response advertises
    and the answer download/preview actually give can never disagree.

    ``version`` is the version the caller resolved (the active one unless a
    specific ``?version=`` was asked for). A file with **no** active version is
    not servable and arrives here as ``None``: there is no fallback to the newest
    stored version, because that would sign a URL for an object the file's own
    pointer no longer claims - reachable as soon as a purge removes the active
    version (ADV-001 §5.1, T-104 F-4).
    """
    if file_object.status == FileObject.Status.TRASHED:
        return ProjectFileError(
            "This file is in the trash; restore it before downloading it.",
            code="file_trashed",
            status_code=409,
        )

    if file_object.status == FileObject.Status.QUARANTINED:
        return ProjectFileError(
            "This file is quarantined and cannot be served.",
            code="file_quarantined",
            status_code=409,
        )

    if not is_on_default_surface(file_object):
        # A row the default surface hides (soft-deleted without the trashed status,
        # or a partial write that left the two markers disagreeing) is not servable
        # either: it is absent from the listing and 404 on detail for a MEMBER, so
        # signing a URL for it would be a third, contradictory answer (P-1).
        return ProjectFileError(
            "This file is not available.",
            code="object_unavailable",
            status_code=409,
            version_no=version.version_no if version is not None else None,
            version_status=version.status if version is not None else None,
        )

    if version is None or version.object_deleted_at is not None or version.status not in GOOD_VERSION_STATUSES:
        return ProjectFileError(
            "This version has no stored object to serve.",
            code="object_unavailable",
            status_code=409,
            version_no=version.version_no if version is not None else None,
            version_status=version.status if version is not None else None,
        )

    return None
