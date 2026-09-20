# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Presigned download and preview URLs (ARCH-001 §4.3).

The security boundary, stated exactly as far as it goes:

* Authorization happens **before** signing, and the file is resolved through the
  workspace and project in the URL, so a non-member (or a file from another
  project) gets the generic 404 and learns nothing (AD-06).
* The presigned URL points at the object-storage host, which is a different
  origin from the application, and the signed ``response-content-disposition``
  forces ``attachment`` for every script-capable type (AD-07). SVG, HTML and the
  other script-capable types are **never** inline (RSCH-002 T1/T2, AC-28).
* The signed ``response-content-type`` pins the type the server verified, so a
  mislabelled object cannot be rendered as a type the server never checked
  (RSCH-002 T3).
* A presigned response is served by the object store, so this endpoint **cannot**
  attach ``X-Content-Type-Options``, ``Content-Security-Policy`` or
  ``Cross-Origin-Resource-Policy``, and no claim is made that it does
  (R-DL-3). Origin separation plus forced attachment plus the inert inline
  allowlist is the whole of the MVP control set; guaranteed ``nosniff`` or inline
  active content would need a proxy/Worker delivery path, which the architecture
  records as a P2 option and not an MVP property.
* The presigned URL is never written to the audit trail (AC-34, R-AUD-2): only
  the actor, the file, the version and the disposition are recorded.
"""

# Python imports
from datetime import timedelta

# Django imports
from django.conf import settings
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.views.base import BaseAPIView
from plane.app.views.file.base import (
    delivery_refusal,
    file_queryset,
    project_or_404,
    require_project_member,
)
from plane.db.models import FileAccessLog, FileObject, FileVersion
from plane.settings.storage import S3Storage
from plane.utils.file_storage.audit import record_file_access
from plane.utils.file_storage.errors import ProjectFileError

#: SigV4 presigned URLs expire after at most seven days on R2 (RSCH-001 S2,
#: ARCH-001 §3). The configured TTL is clamped to this hard cap.
MAX_SIGNED_URL_EXPIRATION = 604800

#: Fallback when ``SIGNED_URL_EXPIRATION`` is unset or not positive.
DEFAULT_SIGNED_URL_EXPIRATION = 3600

#: Types the server verified as inert and safe to render inline. Everything else
#: - including every script-capable type in ``SCRIPT_CAPABLE_MIME_TYPES``, all
#: Office/archive types and ``application/json`` - is forced to ``attachment``.
INLINE_MIME_TYPES = frozenset(
    [
        "application/pdf",
        "text/plain",
        "text/csv",
        "text/markdown",
    ]
)

def is_inline_safe(mime_type):
    """Return True when ``mime_type`` may be rendered inline.

    Raster images are inline (SVG is not: it is XML that can carry script), as
    are PDFs and the plain-text types. The check is fail-closed: an unknown,
    missing or script-capable type is served as an attachment.
    """
    if not mime_type:
        return False

    mime_type = mime_type.split(";")[0].strip().lower()
    if mime_type in settings.SCRIPT_CAPABLE_MIME_TYPES:
        return False

    if mime_type.startswith("image/"):
        return mime_type != "image/svg+xml"

    return mime_type in INLINE_MIME_TYPES


def signed_url_ttl(storage):
    """Return the TTL for a signed URL: configured, positive and hard-capped."""
    configured = int(getattr(storage, "signed_url_expiration", 0) or 0)
    ttl = configured if configured > 0 else DEFAULT_SIGNED_URL_EXPIRATION
    return min(ttl, MAX_SIGNED_URL_EXPIRATION)


def resolve_version(file_object, version_no):
    """Return the requested (or active) version, or raise the shared 404.

    Refuses versions whose object is not live: a purged or failed version, or one
    whose object was already deleted, has nothing to sign, and an unverified
    version must never be handed out (AD-05).
    """
    versions = FileVersion.objects.filter(file=file_object)

    if version_no:
        version = versions.filter(version_no=version_no).first()
        if version is None:
            raise FileVersion.DoesNotExist
    else:
        version = versions.filter(is_active=True).first()
        if version is None:
            # Without an active version there is nothing to serve. Falling back to
            # the newest stored version would sign a URL for an object the file's
            # own pointer does not claim - reachable the moment a purge removes the
            # active version (ARCH-001 §4.3, ADV-001 §5.1).
            raise ProjectFileError(
                "This file has no active version to serve.",
                code="object_unavailable",
                status_code=status.HTTP_409_CONFLICT,
                version_status=None,
            )

    refusal = delivery_refusal(file_object, version)
    if refusal is not None:
        raise refusal

    return version


def issue_file_url(request, project, file_object, *, disposition, action):
    """Sign a GET for the file's live version, audit it and return the body.

    One audit row per issuance, never the URL (AC-34), and ``last_accessed_at``
    moves because the column exists to answer "was this ever fetched" (§2.3).
    """
    version_no = request.GET.get("version")
    if version_no is not None and version_no != "":
        try:
            version_no = int(version_no)
        except (TypeError, ValueError):
            raise ProjectFileError(
                "version must be a whole number.",
                code="invalid_request",
                field="version",
            )
    else:
        version_no = None

    version = resolve_version(file_object, version_no)

    storage = S3Storage(request=request)
    ttl = signed_url_ttl(storage)
    expires_at = timezone.now() + timedelta(seconds=ttl)
    url = storage.generate_presigned_url(
        object_name=version.object_key,
        expiration=ttl,
        http_method="GET",
        disposition=disposition,
        filename=file_object.name_display,
        # Pin the type the server verified, so a mislabelled object cannot be
        # rendered as something the server never checked (RSCH-002 T3).
        response_content_type=version.mime_type,
    )
    if url is None:
        raise ProjectFileError(
            "The storage provider could not sign this link.",
            code="storage_unavailable",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    FileObject.objects.filter(pk=file_object.pk).update(last_accessed_at=timezone.now())
    record_file_access(
        request,
        action=action,
        project=project,
        file_name=file_object.name_display,
        file_id=file_object.id,
        version_no=version.version_no,
        metadata={
            "disposition": disposition,
            "content_type": version.mime_type,
            "expires_at": expires_at.isoformat(),
        },
    )

    return {
        "url": url,
        "expires_at": expires_at.isoformat(),
        "disposition": disposition,
        "file_name": file_object.name_display,
        "version_no": version.version_no,
    }


class FileDownloadEndpoint(BaseAPIView):
    """Return a presigned GET forced to ``attachment`` (ARCH-001 §4.3, R-DL-1)."""

    def get(self, request, slug, project_id, file_id):
        project = project_or_404(slug, project_id)
        require_project_member(request, project)
        # Trashed rows are resolved on purpose (``include_trashed=True``): the
        # documented answer is a 409 ``file_trashed`` refusal, not a 404, while a
        # genuinely missing or purged id still 404s. The query itself comes from
        # the shared resolver so this path and the listing agree on visibility.
        file_object = file_queryset(project, slug, include_trashed=True).get(id=file_id)

        return Response(
            issue_file_url(
                request,
                project,
                file_object,
                disposition="attachment",
                action=FileAccessLog.Action.DOWNLOADED,
            ),
            status=status.HTTP_200_OK,
        )


class FilePreviewEndpoint(BaseAPIView):
    """Return a presigned GET, inline only for the inert allowlist.

    Everything else - SVG, HTML, every script-capable type and any type the
    allowlist does not name - is served with ``attachment`` (AD-07, R-DL-2).
    """

    def get(self, request, slug, project_id, file_id):
        project = project_or_404(slug, project_id)
        require_project_member(request, project)
        file_object = file_queryset(project, slug, include_trashed=True).get(id=file_id)

        disposition = "inline" if is_inline_safe(file_object.mime_type) else "attachment"

        return Response(
            issue_file_url(
                request,
                project,
                file_object,
                disposition=disposition,
                action=FileAccessLog.Action.PREVIEWED,
            ),
            status=status.HTTP_200_OK,
        )
