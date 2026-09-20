# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Entity links for a project file: attach and unlink (ARCH-001 §2.5, §4.1).

One physical object can be surfaced by many entities (R-LINK-1), so a link is a row
and never a copy: attaching an issue to an existing file adds a row, and unlinking
the **last** row leaves the file and its object untouched as an orphan (AC-21).

The target is validated inside the transaction that writes the row, through the
shared rules in :mod:`plane.utils.file_storage.links` - the same ones the upload
path uses - so a link can never point at another project's entity (AD-06).

Nothing here touches the file's key: a key's entity segment is frozen when the file
is created (DEC-001, :func:`entity_ref_for`), so adding a link to an older file does
not rewrite it, and the file id is what every link operation keys off.
"""

# Django imports
from django.db import IntegrityError, transaction
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.serializers.file import FileLinkSerializer, FileLinkWriteSerializer
from plane.app.views.base import BaseAPIView
from plane.app.views.file.base import file_for_write, project_or_404, require_project_editor
from plane.db.models import FileAccessLog, FileLink
from plane.throttles.project_file import ProjectFileUploadThrottle
from plane.utils.file_storage.audit import record_file_access
from plane.utils.file_storage.errors import ProjectFileError
from plane.utils.file_storage.links import resolve_link


def attach_link(request, slug, project_id, file_id):
    """Create one link from this file to a validated entity (R-LINK-1, AC-16)."""
    project = project_or_404(slug, project_id)
    require_project_editor(request, project)

    # The write-path verdict: a trashed file is a 409, a row the default surface
    # hides is a 404, and neither may grow new links (ADV-001 §5 P-1).
    file_object = file_for_write(project, slug, file_id)

    serializer = FileLinkWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    entity_type = serializer.validated_data["entity_type"]
    entity_id = serializer.validated_data["entity_id"]

    with transaction.atomic():
        # Validation inside the transaction that writes the row (ARCH-001 §2.5), so a
        # target deleted between the check and the insert cannot be linked.
        resolved = resolve_link(project, entity_type, str(entity_id))
        # The resolved type, not the payload's spelling: ``module`` is an accepted
        # alias for ``milestone`` and the row stores the model's value.
        entity_type = resolved["entity_type"]

        if FileLink.objects.filter(
            file_id=file_object.id, entity_type=entity_type, entity_id=entity_id
        ).exists():
            raise ProjectFileError(
                "This file is already linked to that entity.",
                code="link_exists",
                status_code=status.HTTP_409_CONFLICT,
                entity_type=entity_type,
                entity_id=str(entity_id),
            )

        try:
            with transaction.atomic():
                link = FileLink.objects.create(
                    project=project,
                    file=file_object,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    entity_identifier=resolved["entity_identifier"],
                )
        except IntegrityError:
            # The partial unique is the real guard: two concurrent attaches cannot
            # both pass the check above (R-NFR-6).
            raise ProjectFileError(
                "This file is already linked to that entity.",
                code="link_exists",
                status_code=status.HTTP_409_CONFLICT,
                entity_type=entity_type,
                entity_id=str(entity_id),
            )

        record_file_access(
            request,
            action=FileAccessLog.Action.LINKED,
            project=project,
            file_name=file_object.name_display,
            file_id=file_object.id,
            metadata={
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "entity_identifier": resolved["entity_identifier"],
            },
        )

    return Response({"link": FileLinkSerializer(link).data}, status=status.HTTP_200_OK)


def unlink(request, slug, project_id, file_id, link_id):
    """Mark one link inactive; the file, its versions and its objects are untouched.

    Unlinking is a row operation by design (AC-21): the object is shared by every
    link, so removing one never removes bytes, and a file with no live links stays
    listed and downloadable as an orphan.
    """
    project = project_or_404(slug, project_id)
    require_project_editor(request, project)
    file_object = file_for_write(project, slug, file_id)

    with transaction.atomic():
        link = (
            FileLink.objects.select_for_update()
            .filter(id=link_id, file_id=file_object.id)
            .first()
        )
        if link is None:
            # Already unlinked, or another file's link: the live manager is what makes
            # a repeated delete a 404 rather than a second, silent success.
            raise FileLink.DoesNotExist

        # A queryset update rather than the model's ``delete()``: that helper queues a
        # deferred hard-delete job through the broker, and unlinking must not depend on
        # a worker being reachable (the same reason the trash uses an update).
        FileLink.objects.filter(pk=link.pk).update(deleted_at=timezone.now(), updated_at=timezone.now())

        record_file_access(
            request,
            action=FileAccessLog.Action.UNLINKED,
            project=project,
            file_name=file_object.name_display,
            file_id=file_object.id,
            metadata={
                "entity_type": link.entity_type,
                "entity_id": str(link.entity_id),
                "entity_identifier": link.entity_identifier,
            },
        )

    return Response(status=status.HTTP_204_NO_CONTENT)


class FileLinkListEndpoint(BaseAPIView):
    """Attach this file to an entity (ARCH-001 §4.1)."""

    throttle_classes = [ProjectFileUploadThrottle]

    def post(self, request, slug, project_id, file_id):
        return attach_link(request, slug, project_id, file_id)


class FileLinkDetailEndpoint(BaseAPIView):
    """Unlink one entity from this file (ARCH-001 §4.1)."""

    throttle_classes = [ProjectFileUploadThrottle]

    def delete(self, request, slug, project_id, file_id, link_id):
        return unlink(request, slug, project_id, file_id, link_id)
