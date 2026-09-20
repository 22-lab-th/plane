# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Version history, revision initiation and activation (ARCH-001 §2.4, §4.1).

Three rules shape this module:

* **a revision is never active on arrival** (AD-18, AC-43): ``complete-upload``
  stores it ``superseded`` and only :class:`FileVersionActivateEndpoint` moves the
  active pointer;
* **the active pointer moves only to a version that can actually be served**: the
  activation path applies ``delivery_refusal`` - the predicate the delivery
  endpoints and the advertised ``can_activate`` field use - and then asks the
  object store whether the object is still there, because a deletion made outside
  this application leaves no trace in the database (T-104 F-4, on the write side);
* **visibility comes from the shared resolver**, never from a manager picked here
  (ADV-001 §5 P-1).
"""

# Django imports
from django.db import transaction
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.serializers.file import (
    FileObjectSerializer,
    FileVersionInitiateSerializer,
    FileVersionSerializer,
)
from plane.app.views.base import BaseAPIView
from plane.app.views.file.base import (
    delivery_refusal,
    file_for_write,
    file_queryset,
    include_trashed,
    project_or_404,
    require_project_editor,
    require_project_member,
)
from plane.app.views.file.upload import initiate_upload
from plane.db.models import FileAccessLog, FileObject, FileVersion
from plane.settings.storage import S3Storage
from plane.throttles.project_file import ProjectFileUploadThrottle
from plane.utils.file_storage.audit import record_file_access
from plane.utils.file_storage.errors import ProjectFileError


def _store_refusal(version, *, request=None):
    """Return a refusal when the version's object is not in the store, or ``None``.

    A row says an object was stored; only the store says it is still there. A
    deletion performed outside this application (a console ``DELETE``, a lifecycle
    rule somebody added) leaves the row intact, and activating such a version would
    move the file's pointer onto bytes nobody can serve.
    """
    if S3Storage(request=request).get_object_metadata(version.object_key) is None:
        return ProjectFileError(
            "This version's object is no longer in the store.",
            code="object_unavailable",
            status_code=status.HTTP_409_CONFLICT,
            version_no=version.version_no,
            version_status=version.status,
        )

    return None


def activate_version(request, project, file_object, version):
    """Move the active pointer to ``version``; return the response payload.

    The file row is locked for the whole swap so two concurrent activations cannot
    interleave, and the previous active version is demoted before the new one is
    promoted because the partial unique constraint allows exactly one ``is_active``
    row per file (ARCH-001 §2.4).
    """
    previous_version_no = None
    activated = False

    with transaction.atomic():
        FileObject.objects.select_for_update().filter(pk=file_object.pk).get()

        version.refresh_from_db()
        if not version.is_active:
            refusal = delivery_refusal(file_object, version)
            if refusal is None:
                refusal = _store_refusal(version, request=request)
            if refusal is not None:
                raise refusal

            previous = FileVersion.objects.select_for_update().filter(file=file_object, is_active=True).first()
            if previous is not None:
                # Demote first: the partial unique allows exactly one ``is_active``
                # row per file, so promoting before demoting would collide.
                previous.is_active = False
                previous.mark_status(FileVersion.Status.SUPERSEDED, save=False)
                previous.save(update_fields=["status", "status_changed_at", "is_active", "updated_at"])
                previous_version_no = previous.version_no

            version.is_active = True
            version.mark_status(FileVersion.Status.ACTIVE, save=False)
            version.save(update_fields=["status", "status_changed_at", "is_active", "updated_at"])
            FileObject.objects.filter(pk=file_object.pk).update(
                status=FileObject.Status.ACTIVE,
                current_version_no=version.version_no,
                object_key=version.object_key,
                size_bytes=version.size_bytes,
                mime_type=version.mime_type,
                updated_at=timezone.now(),
            )
            activated = True

            record_file_access(
                request,
                action=FileAccessLog.Action.VERSION_ACTIVATED,
                project=project,
                file_name=file_object.name_display,
                file_id=file_object.id,
                version_no=version.version_no,
                metadata={
                    "previous_version_no": previous_version_no,
                    "size_bytes": version.size_bytes,
                    "mime_type": version.mime_type,
                },
            )

    file_object.refresh_from_db()
    version.refresh_from_db()

    return {
        "file": FileObjectSerializer(file_object).data,
        "version": FileVersionSerializer(version).data,
        "previous_version_no": previous_version_no,
        #: False for a repeated activation: the end state is the same and no second
        #: audit row or status change was written (R-NFR-6 idempotency).
        "activated": activated,
    }


class FileVersionListEndpoint(BaseAPIView):
    """List a file's versions (AC-13) and start a new revision (R-VER-1)."""

    throttle_classes = [ProjectFileUploadThrottle]

    def get_throttles(self):
        """The revision door carries the upload throttle; the history read does not."""
        if self.request.method not in ("GET", "HEAD", "OPTIONS"):
            return [ProjectFileUploadThrottle()]
        return super().get_throttles()

    def get(self, request, slug, project_id, file_id):
        project = project_or_404(slug, project_id)
        # The decorator allows GUEST on this path, so membership itself is checked
        # here: a deactivated member is not a reader and learns nothing (AD-06).
        require_project_member(request, project)
        file_object = file_queryset(
            project, slug, include_trashed=include_trashed(request, project)
        ).get(id=file_id)

        versions = list(
            file_object.versions.order_by("-version_no").select_related("uploaded_by", "file__project")
        )

        return Response(FileVersionSerializer(versions, many=True).data, status=status.HTTP_200_OK)

    def post(self, request, slug, project_id, file_id):
        """Start the next version of this file (the same contract as initiate-upload).

        The role check is explicit rather than a decorator, matching the other write
        doors on this surface: a non-member gets the generic 404 and learns nothing,
        a GUEST is refused with the repository's 403 (AD-06, T-104 F-3).
        """
        project = project_or_404(slug, project_id)
        require_project_editor(request, project)

        serializer = FileVersionInitiateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        return initiate_upload(request, slug, project_id, serializer.validated_data, pinned_file_id=file_id)


class FileVersionActivateEndpoint(BaseAPIView):
    """Switch the file's active version (ARCH-001 §4.1, AD-18, AC-43)."""

    throttle_classes = [ProjectFileUploadThrottle]

    def post(self, request, slug, project_id, file_id, version_no):
        project = project_or_404(slug, project_id)
        require_project_editor(request, project)

        # The write-path verdict: a trashed file is a 409, a row the default surface
        # hides is a 404, and neither may have its pointer moved.
        file_object = file_for_write(project, slug, file_id)

        version = FileVersion.objects.filter(file=file_object, version_no=version_no).first()
        if version is None:
            # Resolved through the file, so another project's version is simply not
            # found and the caller learns nothing (AD-06).
            raise FileVersion.DoesNotExist

        return Response(
            activate_version(request, project, file_object, version),
            status=status.HTTP_200_OK,
        )
