# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Upload lifecycle for project files: initiate, verified finalize and abort.

The three endpoints in ARCH-001 §4.1/§4.2 own one attempt each:

* ``initiate-upload`` validates the declaration, reserves quota under the
  workspace-then-project lock pair, writes the file and version rows and returns
  a presigned PUT signed for that exact key and ``Content-Type`` (AD-04).
* ``complete-upload`` verifies the object (existence, byte length, content type
  and the first 512 bytes) and only then settles the reservation and stores the
  version; a revision is stored ``superseded`` and never silently becomes the
  active version (AD-05, AD-18).
* ``abort-upload`` ends an attempt and releases its reservation exactly once.

No endpoint trusts a client-supplied checksum as evidence (AD-16) and no
endpoint parses an object key for authorization (AD-02): the file is always
resolved through the workspace and project in the URL.
"""

# Python imports
from datetime import timedelta
from uuid import uuid4

# Django imports
from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.permissions import ROLE, allow_permission
from plane.app.serializers.file import (
    FileUploadAbortSerializer,
    FileUploadCompleteSerializer,
    FileUploadInitiateSerializer,
)
from plane.app.views.base import BaseAPIView
from plane.app.views.file.base import (
    available_display_name,
    file_for_write,
    folder_or_400,
    project_or_404,
    require_writable_project,
    stored_name,
)
from plane.db.models import (
    FileAccessLog,
    FileFolder,
    FileLink,
    FileObject,
    FileVersion,
)
from plane.settings.storage import S3Storage
from plane.throttles.project_file import ProjectFileUploadThrottle
from plane.utils.file_storage import quota
from plane.utils.file_storage.audit import record_file_access
from plane.utils.file_storage.errors import ProjectFileError
from plane.utils.file_storage.links import category_for_entity, entity_ref_for, resolve_link
from plane.utils.file_storage.naming import extension_of, normalize_name
from plane.utils.magic_bytes import HEAD_BYTES, check_magic_bytes, normalize_mime_type
from plane.utils.object_key import build_object_key

#: Category a file inherits from the entity it was created from (DEC-001).


def _target_file(project, slug, file_id):
    """Return the file a new version belongs to, or ``None`` for a brand new file.

    The verdict is the shared write-path one (``file_for_write``), not a manager
    picked here: a trashed row is refused with the documented 409 ``file_trashed``
    - the old lookup resolved through the live manager and answered 404 before its
    own status check could run - and a row the default surface hides, including one
    left ``purge_failed`` by a failed purge, is refused with 404 so a new version
    can never resurrect it.
    """
    if not file_id:
        return None

    return file_for_write(project, slug, file_id)


def _resolve_link(project, link):
    """Validate a payload's ``link`` through the shared rules (ARCH-001 §2.5).

    The upload door and the links door must agree on what a link target is, so the
    lookup itself lives in :mod:`plane.utils.file_storage.links` and this only adapts
    the payload shape.
    """
    if not link:
        return None

    return resolve_link(project, link["entity_type"], link["entity_id"])


def _category_for(requested_category, link, file_object):
    """Return the category for this upload (DEC-001: immutable after creation)."""
    if file_object is not None:
        return file_object.category

    if requested_category:
        return requested_category

    if link:
        return category_for_entity(link["entity_type"])

    return FileObject.Category.ASSETS


def _next_version_no(file_object):
    """Return the next version number for ``file_object`` (1 for a new file)."""
    if file_object is None:
        return 1

    latest = FileVersion.objects.filter(file_id=file_object.id).aggregate(latest=Max("version_no"))["latest"]
    return (latest or 0) + 1


def _refuse_live_attempt(file_object):
    """Refuse a new upload while this file still holds a live reservation.

    A presigned URL is refused while the version it belongs to is ``uploading``
    with a reservation that has not expired, so one member cannot park several
    reservations for the same file up to the ceiling (ARCH-001 §2.4). The row is
    locked so a committed attempt cannot be passed twice.
    """
    live_attempts = list(
        FileVersion.objects.select_for_update()
        .filter(
            file_id=file_object.id,
            status=FileVersion.Status.UPLOADING,
            reservation_released_at__isnull=True,
        )
        .filter(Q(reservation_expires_at__isnull=True) | Q(reservation_expires_at__gt=timezone.now()))
        .order_by("created_at")[:1]
    )
    if not live_attempts:
        return

    attempt = live_attempts[0]
    raise ProjectFileError(
        "Another upload for this file is already in progress.",
        code="upload_in_progress",
        status_code=status.HTTP_409_CONFLICT,
        version_no=attempt.version_no,
        reservation_expires_at=attempt.reservation_expires_at.isoformat()
        if attempt.reservation_expires_at
        else None,
    )


def _file_payload(file_object):
    """The file summary shared by the initiate and complete responses."""
    return {
        "id": str(file_object.id),
        "name_display": file_object.name_display,
        "category": file_object.category,
        "object_key": file_object.object_key,
        "folder_id": str(file_object.folder_id) if file_object.folder_id else None,
    }


def _version_payload(version):
    """The version summary returned by finalize; the checksum stays advisory (AD-16)."""
    return {
        "version_no": version.version_no,
        "size_bytes": version.size_bytes,
        "checksum_sha256": version.client_checksum_sha256,
        "etag": version.etag,
        "status": version.status,
    }


def _usage_payload(usage):
    return {"project_used_bytes": usage.used_bytes, "limit_bytes": usage.limit_bytes}


def _completion_payload(file_object, version, usage):
    """Build the finalize response for a version that is stored (ARCH-001 §4.1)."""
    file_object.refresh_from_db()
    version.refresh_from_db()

    return {
        "file": _file_payload(file_object),
        "version": _version_payload(version),
        "activation_required": not version.is_active,
        "storage_usage": _usage_payload(usage),
    }


def _stored_failure_response(version):
    """Replay the recorded outcome of a version that is no longer uploading.

    A repeated finalize must be idempotent (R-UPL-4, AC-19): it returns what the
    first attempt produced and touches no counters and no audit rows. ``None``
    means "this attempt is either still uploading or already settled", which the
    caller handles explicitly.
    """
    if version.status in (
        FileVersion.Status.UPLOADING,
        FileVersion.Status.ACTIVE,
        FileVersion.Status.SUPERSEDED,
    ):
        return None

    failure = (version.storage_metadata or {}).get("failure") or {}
    return Response(
        {
            "error": failure.get("message", "This upload attempt is no longer active."),
            "code": failure.get("code", "verification_failed"),
            "status": version.status,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


class FileUploadInitiateEndpoint(BaseAPIView):
    """Create the pending file version, reserve quota and presign the upload."""

    throttle_classes = [ProjectFileUploadThrottle]

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id):
        serializer = FileUploadInitiateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        return initiate_upload(request, slug, project_id, serializer.validated_data)


def initiate_upload(request, slug, project_id, payload, *, pinned_file_id=None):
    """Create the pending version, reserve quota and presign the upload (ARCH-001 §4.1).

    Shared by ``initiate-upload/`` - which may carry ``file_id`` in the body for a
    retry or a revision - and ``{file_id}/versions/``, where the URL pins the file.
    ``pinned_file_id`` wins over the body and is the only difference between the two
    doors, so the reservation, the key and the audit row are built once.
    """
    project = project_or_404(slug, project_id)
    require_writable_project(project)
    link = _resolve_link(project, payload.get("link"))
    folder = folder_or_400(project, payload.get("folder_id"))
    target_id = pinned_file_id if pinned_file_id is not None else payload.get("file_id")
    file_object = _target_file(project, slug, target_id)

    category = _category_for(payload.get("category"), link, file_object)
    version_no = _next_version_no(file_object)
    entity_ref = link["entity_ref"] if link else entity_ref_for(file_object)
    bucket = settings.AWS_STORAGE_BUCKET_NAME
    file_id = file_object.id if file_object is not None else uuid4()
    display_name = stored_name(payload["file_name"])
    if file_object is None:
        # A second upload of the same name in the same folder becomes
        # "Name (2).ext" (R-FOLD-5) before the key is built, so the key's
        # filename and the stored name stay in step.
        display_name = available_display_name(project, folder, display_name)

    object_key = build_object_key(
        workspace_slug=project.workspace.slug,
        project_storage_key=project.ensure_storage_key(),
        category=category,
        file_id=file_id,
        version_no=version_no,
        filename=display_name,
        entity_ref=entity_ref,
    )
    upload_ttl = timedelta(seconds=settings.PROJECT_FILE_UPLOAD_URL_TTL_SECONDS)
    expires_at = timezone.now() + upload_ttl

    try:
        # The reservation and the rows that carry it commit together: this
        # commit is what makes the presigned URL valid (ARCH-001 §2.8 item 1).
        with transaction.atomic():
            quota_row, usage_row = quota.lock_usage_rows(project)

            if file_object is not None:
                _refuse_live_attempt(file_object)

            if file_object is None:
                file_object = FileObject(
                    id=file_id,
                    project=project,
                    folder=folder,
                    name_original=display_name,
                    name_display=display_name,
                    name_normalized=normalize_name(display_name),
                    mime_type=payload["mime_type"],
                    extension=extension_of(display_name),
                    bucket=bucket,
                    object_key=object_key,
                    category=category,
                    status=FileObject.Status.PENDING,
                    checksum_sha256=payload.get("checksum_sha256"),
                )
                # The uploader is recorded from the request rather than from
                # the ambient current user, so the uploader filter and the
                # audit trail stay correct outside a request context too.
                file_object.save(force_insert=True, created_by_id=request.user.id)

            version = FileVersion.objects.create(
                project=project,
                file=file_object,
                version_no=version_no,
                object_key=object_key,
                bucket=bucket,
                mime_type=payload["mime_type"],
                client_checksum_sha256=payload.get("checksum_sha256"),
                uploaded_by=request.user,
                status=FileVersion.Status.UPLOADING,
            )

            quota.reserve(
                quota_row,
                usage_row,
                version,
                requested_bytes=payload["size_bytes"],
                expires_at=expires_at,
            )

            if link is not None:
                FileLink.objects.create(
                    project=project,
                    file=file_object,
                    entity_type=link["entity_type"],
                    entity_id=link["entity_id"],
                    entity_identifier=link["entity_identifier"],
                )

            record_file_access(
                request,
                action=FileAccessLog.Action.UPLOAD_INITIATED,
                project=project,
                file_name=display_name,
                file_id=file_object.id,
                version_no=version_no,
                metadata={
                    "size_bytes": payload["size_bytes"],
                    "mime_type": payload["mime_type"],
                    "category": category,
                    "folder_id": str(folder.id) if folder else None,
                    "new_file": version_no == 1,
                },
            )
    except quota.QuotaExceeded as exc:
        record_file_access(
            request,
            action=FileAccessLog.Action.QUOTA_REJECTED,
            project=project,
            file_name=display_name,
            file_id=file_object.id if file_object is not None else None,
            metadata={"size_bytes": payload["size_bytes"], "level": exc.level},
        )
        return Response(exc.as_response(), status=exc.status_code)

    # The reservation is committed, so the URL may now be signed.
    upload = S3Storage(request=request).generate_presigned_put(
        object_name=object_key,
        content_type=payload["mime_type"],
        expires_in=settings.PROJECT_FILE_UPLOAD_URL_TTL_SECONDS,
    )
    if upload is None:
        # The attempt stays uploading with a live reservation; the cleanup
        # sweep releases and deletes it once the URL TTL has passed (AD-13).
        return Response(
            {
                "error": "The storage provider could not sign this upload.",
                "code": "storage_unavailable",
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(
        {
            "file": _file_payload(file_object),
            "version_no": version_no,
            "upload": {
                "url": upload["url"],
                "method": upload["method"],
                "headers": upload["headers"],
                "expires_at": expires_at.isoformat(),
            },
        },
        status=status.HTTP_200_OK,
    )



class FileUploadCompleteEndpoint(BaseAPIView):
    """Verify the uploaded object and settle the attempt (ARCH-001 §4.2 item 3)."""

    throttle_classes = [ProjectFileUploadThrottle]

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, file_id):
        serializer = FileUploadCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        project = project_or_404(slug, project_id)
        require_writable_project(project)
        file_object = FileObject.objects.get(id=file_id, project_id=project.id, workspace__slug=slug)
        version = FileVersion.objects.get(file=file_object, version_no=payload["version_no"])

        stored_response = _stored_failure_response(version)
        if stored_response is not None:
            return stored_response

        if version.status != FileVersion.Status.UPLOADING:
            # Already settled by an earlier (or concurrent) finalize: return the
            # stored result without touching counters or the audit trail.
            _, usage = quota.get_usage_rows(project)
            return Response(_completion_payload(file_object, version, usage), status=status.HTTP_200_OK)

        storage = S3Storage(request=request)
        metadata = storage.get_object_metadata(version.object_key)
        if metadata is None:
            return self._reject(
                request,
                project,
                file_object,
                version,
                code="object_missing",
                message="The uploaded object does not exist in storage.",
                declared_size=payload["size_bytes"],
            )

        observed_size = int(metadata.get("ContentLength") or 0)
        observed_type = normalize_mime_type(metadata.get("ContentType"))

        if observed_size != payload["size_bytes"]:
            return self._reject(
                request,
                project,
                file_object,
                version,
                code="size_mismatch",
                message="The uploaded object does not match the declared size.",
                observed_size=observed_size,
                declared_size=payload["size_bytes"],
                etag=metadata.get("ETag"),
            )

        reserved_bytes = version.reserved_bytes or 0
        if reserved_bytes and observed_size > reserved_bytes:
            # The reservation is an upper bound on what an attempt may store, so a
            # client cannot legitimise an over-sized object by declaring it again
            # at finalize (ARCH-001 §2.8 item 5).
            return self._reject(
                request,
                project,
                file_object,
                version,
                code="size_mismatch",
                message="The uploaded object exceeds the size reserved for this upload.",
                reason="exceeds_reservation",
                observed_size=observed_size,
                reserved_bytes=reserved_bytes,
                etag=metadata.get("ETag"),
            )

        if observed_type != normalize_mime_type(version.mime_type):
            return self._reject(
                request,
                project,
                file_object,
                version,
                code="mime_mismatch",
                message="The stored object's content type does not match the declaration.",
                observed_mime_type=observed_type,
                declared_mime_type=version.mime_type,
                observed_size=observed_size,
                etag=metadata.get("ETag"),
            )

        head = storage.get_object_head_bytes(version.object_key, HEAD_BYTES)
        if head is None:
            return self._reject(
                request,
                project,
                file_object,
                version,
                code="verification_failed",
                message="The first bytes of the object could not be read.",
                reason="head_bytes_unavailable",
            )

        magic_bytes_match = check_magic_bytes(observed_type, head)
        if magic_bytes_match is False:
            return self._reject(
                request,
                project,
                file_object,
                version,
                code="mime_mismatch",
                message="The object's content does not match the declared content type.",
                reason="magic_bytes_mismatch",
                observed_mime_type=observed_type,
                observed_size=observed_size,
                etag=metadata.get("ETag"),
                # The bytes that contradicted the declaration are the evidence.
                observed_head_hex=head[:32].hex(),
            )

        # Activate on a verified upload when the file has no active version yet,
        # which covers a genuine v1 and a retry of a first upload that failed;
        # only a revision that would displace a live version is stored
        # superseded and asks the client to confirm (AD-18).
        activate = not FileVersion.objects.filter(file_id=file_object.id, is_active=True).exists()
        evidence = {
            "mime_type": observed_type,
            # The finalize declaration may repeat the advisory checksum; it is
            # stored, never treated as verification evidence (AD-16).
            "client_checksum_sha256": payload.get("checksum_sha256") or version.client_checksum_sha256,
            "etag": metadata.get("ETag"),
            "uploaded_by_id": request.user.id,
            "magic_bytes_checked_at": timezone.now() if magic_bytes_match is not None else None,
            "storage_metadata": {
                **(version.storage_metadata or {}),
                "content_type": observed_type,
                "content_length": observed_size,
                "etag": metadata.get("ETag"),
                "last_modified": metadata.get("LastModified"),
                "magic_bytes": {
                    "checked": magic_bytes_match is not None,
                    "matches": magic_bytes_match,
                    "declared": observed_type,
                },
            },
        }

        with transaction.atomic():
            quota_row, usage_row = quota.lock_usage_rows(project)

            settled_status = quota.settle(
                quota_row,
                usage_row,
                version,
                observed_bytes=observed_size,
                activate=activate,
                fields=evidence,
            )

            if settled_status is None:
                # Another request settled this attempt first: report its result.
                return Response(_completion_payload(file_object, version, usage_row), status=status.HTTP_200_OK)

            if activate:
                FileObject.objects.filter(pk=file_object.pk).update(
                    status=FileObject.Status.ACTIVE,
                    current_version_no=version.version_no,
                    size_bytes=observed_size,
                    mime_type=observed_type,
                    object_key=version.object_key,
                    updated_at=timezone.now(),
                )

            record_file_access(
                request,
                action=(
                    FileAccessLog.Action.UPLOAD_COMPLETED if activate else FileAccessLog.Action.VERSION_CREATED
                ),
                project=project,
                file_name=file_object.name_display,
                file_id=file_object.id,
                version_no=version.version_no,
                metadata={
                    "size_bytes": observed_size,
                    "mime_type": observed_type,
                    "etag": metadata.get("ETag"),
                    "magic_bytes_checked": magic_bytes_match is not None,
                    "magic_bytes_match": magic_bytes_match,
                },
            )

            response_payload = _completion_payload(file_object, version, usage_row)

        return Response(response_payload, status=status.HTTP_200_OK)

    def _reject(self, request, project, file_object, version, *, code, message, **details):
        """Fail the attempt, release its reservation once and report the evidence.

        The conditional update on ``status='uploading'`` keeps this single-fire:
        if a concurrent request already ended the attempt, this call records
        nothing and replays the stored outcome instead.
        """
        with transaction.atomic():
            quota_row, usage_row = quota.lock_usage_rows(project)

            marked = FileVersion.objects.filter(pk=version.pk, status=FileVersion.Status.UPLOADING).update(
                status=FileVersion.Status.FAILED,
                status_changed_at=timezone.now(),
                is_active=False,
                size_bytes=details.get("observed_size", version.size_bytes),
                storage_metadata={
                    **(version.storage_metadata or {}),
                    "failure": {"code": code, "message": message, **details},
                },
            )

            if not marked:
                stored = _stored_failure_response(version)
                return stored or Response(
                    _completion_payload(file_object, version, usage_row), status=status.HTTP_200_OK
                )

            quota.release(quota_row, usage_row, version)

            record_file_access(
                request,
                action=FileAccessLog.Action.UPLOAD_FAILED,
                project=project,
                file_name=file_object.name_display,
                file_id=file_object.id,
                version_no=version.version_no,
                metadata={"code": code, **details},
            )

        return Response({"error": message, "code": code, **details}, status=status.HTTP_400_BAD_REQUEST)


class FileUploadAbortEndpoint(BaseAPIView):
    """End a pending attempt and release its reservation exactly once."""

    throttle_classes = [ProjectFileUploadThrottle]

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id, file_id):
        serializer = FileUploadAbortSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project = project_or_404(slug, project_id)
        require_writable_project(project)
        file_object = FileObject.objects.get(id=file_id, project_id=project.id, workspace__slug=slug)
        version = FileVersion.objects.get(file=file_object, version_no=serializer.validated_data["version_no"])

        if version.status in (FileVersion.Status.ACTIVE, FileVersion.Status.SUPERSEDED):
            return Response(
                {
                    "error": "This version is already stored and cannot be aborted.",
                    "code": "not_uploading",
                },
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            quota_row, usage_row = quota.lock_usage_rows(project)

            marked = FileVersion.objects.filter(pk=version.pk, status=FileVersion.Status.UPLOADING).update(
                status=FileVersion.Status.FAILED,
                status_changed_at=timezone.now(),
                is_active=False,
            )
            # The release is guarded, so a repeated abort cannot decrement twice.
            quota.release(quota_row, usage_row, version)

            if marked:
                record_file_access(
                    request,
                    action=FileAccessLog.Action.UPLOAD_FAILED,
                    project=project,
                    file_name=file_object.name_display,
                    file_id=file_object.id,
                    version_no=version.version_no,
                    metadata={"reason": "aborted"},
                )

        return Response(status=status.HTTP_204_NO_CONTENT)
