# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Cross-project copy and move: one workspace, two projects, one copy at a time (R-OPS-4).

Three invariants shape this module:

* **Both sides are authorized before anything happens.** The caller must be able to
  write in the project in the URL *and* in the destination project, the two must live in
  the same workspace, and a destination the caller cannot see answers the same neutral
  404 as everywhere else in this feature - so this door never confirms a project, or a
  workspace, that the caller could not already see (AD-06, AD-17).
* **The copy is the same copy.** The bytes move through
  :func:`plane.app.views.file.operations.copy_file_into_project`: new file id, a key per
  version under the *target* project's prefix, the target charged through the same
  reserve/settle path an upload uses, and no entity links - links are project-scoped, so
  a copy never carries them.
* **A move never deletes the source before the copy verified.** The HEAD verification
  runs inside the copy's transaction, so a copied object that is missing, truncated,
  mislabelled or otherwise not the source rolls the copy back - rows, counters, audit
  rows - and its objects are deleted; only then is the source trashed and purged.
  Exactly one copy then exists, and a failure leaves the source exactly as it was
  (AC-42).
"""

# Django imports
from django.core.exceptions import ObjectDoesNotExist

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.permissions import ROLE
from plane.app.serializers.file import FileCrossProjectSerializer
from plane.app.views.base import BaseAPIView
from plane.app.views.file.base import (
    file_for_write,
    folder_or_400,
    member_role,
    project_or_404,
    require_project_editor,
    require_writable_project,
)
from plane.app.views.file.operations import (
    copy_file_into_project,
    serialize_file,
    trash_file_row,
)
from plane.db.models import FileAccessLog, Project
from plane.throttles.project_file import ProjectFileUploadThrottle
from plane.utils.file_storage.audit import record_file_access
from plane.utils.file_storage.errors import ProjectFileError
from plane.utils.file_storage.purge import purge_file as run_purge
from plane.utils.magic_bytes import normalize_mime_type

#: The one word each project's activity feed uses for the operation that happened
#: (``metadata.operation``), so a reader can tell a cross-project copy from a move
#: without inferring it from the action alone.
OPERATION_COPY = "copy_to_project"
OPERATION_MOVE = "move_to_project"


def transfer_payload(request):
    """Validate the payload both cross-project routes take."""
    serializer = FileCrossProjectSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def target_project_for(request, source_project, target_project_id):
    """Resolve the destination project, or refuse with the reason that fits (AD-17).

    The order is the isolation order this feature uses everywhere (AD-06):

    1. a destination that does not exist, or that the caller is not an active member
       of, answers the generic 404 - so this door never tells a caller that a project
       (or a workspace) exists that it could not already see;
    2. a destination in another workspace is refused **entirely** (cross-project
       operations are same-workspace by definition, R-OPS-4) - and only a caller who is
       a member of that project can reach this answer, so nothing is confirmed by it;
    3. a destination the caller may only read is refused with the copy the UI prints
       for a project it must not offer (DESIGN §7), and an archived destination is
       read-only for everyone (AC-37).
    """
    if target_project_id is None:
        raise ProjectFileError(
            "target_project_id is required.",
            code="invalid_request",
            status_code=status.HTTP_400_BAD_REQUEST,
            field="target_project_id",
        )

    target = Project.objects.filter(id=target_project_id).first()
    role = member_role(request, target) if target is not None else None
    if role is None:
        raise ObjectDoesNotExist("The required object does not exist.")

    if target.workspace_id != source_project.workspace_id:
        raise ProjectFileError(
            "Files can only be copied or moved between projects of the same workspace.",
            code="cross_workspace_not_supported",
            status_code=status.HTTP_400_BAD_REQUEST,
            field="target_project_id",
        )

    if target.id == source_project.id:
        raise ProjectFileError(
            "This route copies or moves to another project; use the file's own copy or move for this one.",
            code="invalid_request",
            status_code=status.HTTP_400_BAD_REQUEST,
            field="target_project_id",
        )

    if role not in (ROLE.ADMIN.value, ROLE.MEMBER.value):
        raise ProjectFileError(
            f"You need write access to {target.name} to do that.",
            code="permission_denied",
            status_code=status.HTTP_403_FORBIDDEN,
            target_project_id=str(target.id),
        )

    require_writable_project(target)
    return target


def transfer_audit(*, request, source_project, target_project, folder, operation, action):
    """Return the hook that writes both projects' audit rows inside the copy's transaction.

    R-OPS-4 requires the operation to be audited in **both** projects: the source keeps
    the row that names where the file went, the target the row that names where it came
    from. Each row is written in the project it belongs to, so either activity feed
    answers "what happened to this file" without reading the other project's rows
    (R-AUD-1/3). Being called inside the copy's transaction is what keeps the trail from
    claiming an operation that rolled back.
    """

    def audit(copied):
        folder_id = str(folder.id) if folder is not None else None
        record_file_access(
            request,
            action=action,
            project=source_project,
            file_name=copied.source.name_display,
            file_id=copied.source.id,
            version_no=copied.active_version_no,
            metadata={
                "operation": operation,
                "target_project_id": str(target_project.id),
                "target_file_id": str(copied.file.id),
                "copied_versions": len(copied.plan),
                "folder_id": folder_id,
            },
        )
        record_file_access(
            request,
            action=action,
            project=target_project,
            file_name=copied.file.name_display,
            file_id=copied.file.id,
            version_no=copied.active_version_no,
            metadata={
                "operation": operation,
                "source_project_id": str(source_project.id),
                "source_file_id": str(copied.source.id),
                "copied_versions": len(copied.plan),
                "folder_id": folder_id,
            },
        )

    return audit


def _verification_failure(source_version, object_key, *, reason, **details):
    return ProjectFileError(
        "The copied object did not match the source, so the source was left untouched.",
        code="verification_failed",
        status_code=status.HTTP_409_CONFLICT,
        reason=reason,
        version_no=source_version.version_no,
        source_object_key=source_version.object_key,
        copied_object_key=object_key,
        **details,
    )


def verify_copied_objects(storage, copied):
    """HEAD every copied object and require it to match its source, or refuse (AC-42).

    Size, content type and ETag are compared between the two HEAD responses, and the
    source row's own recorded size and type must agree with what the store reports, so a
    copy that is missing, truncated, mislabelled or simply not the same object is caught
    *before* the source is touched. The evidence is what the server observed on both
    sides, never the client's declaration (AD-16); an ETag that cannot be read on either
    side fails closed rather than passing a check it cannot support.

    Returns nothing on success. The refusal it raises is what the move door returns, and
    because it is raised inside the copy's transaction the target keeps nothing: no row,
    no charged byte, no object, no audit row.
    """
    for source_version, object_key in copied.plan:
        observed = storage.get_object_metadata(object_key)
        if observed is None:
            raise _verification_failure(source_version, object_key, reason="object_missing")

        source_metadata = storage.get_object_metadata(source_version.object_key)
        if source_metadata is None:
            raise _verification_failure(source_version, object_key, reason="source_object_missing")

        observed_size = int(observed.get("ContentLength") or 0)
        source_size = int(source_metadata.get("ContentLength") or 0)
        if observed_size != source_size or observed_size != (source_version.size_bytes or 0):
            raise _verification_failure(
                source_version,
                object_key,
                reason="size_mismatch",
                copied_size=observed_size,
                source_size=source_size,
                recorded_size=source_version.size_bytes or 0,
            )

        observed_type = normalize_mime_type(observed.get("ContentType"))
        source_type = normalize_mime_type(source_metadata.get("ContentType"))
        if observed_type != source_type or observed_type != normalize_mime_type(source_version.mime_type):
            raise _verification_failure(
                source_version,
                object_key,
                reason="mime_mismatch",
                copied_mime_type=observed_type,
                source_mime_type=source_type,
                recorded_mime_type=source_version.mime_type,
            )

        observed_etag = observed.get("ETag")
        source_etag = source_metadata.get("ETag") or source_version.etag
        if observed_etag is None or source_etag is None:
            raise _verification_failure(
                source_version,
                object_key,
                reason="etag_unavailable",
                copied_etag=observed_etag,
                source_etag=source_etag,
            )
        if observed_etag != source_etag:
            raise _verification_failure(
                source_version,
                object_key,
                reason="etag_mismatch",
                copied_etag=observed_etag,
                source_etag=source_etag,
            )


def copy_to_project(request, slug, project_id, file_id):
    """Copy a file into another project of the same workspace (R-OPS-4, AC-41).

    The source is not touched: it keeps its row, its keys and every object, and both
    projects record that the copy happened. The target's usage grows by exactly the
    copied bytes - the reservation and the settlement are the upload path's own helpers,
    charged to the target - and the copy carries no links (they are project-scoped).
    """
    project = project_or_404(slug, project_id)
    require_project_editor(request, project)
    payload = transfer_payload(request)
    target = target_project_for(request, project, payload.get("target_project_id"))
    source = file_for_write(project, slug, file_id)
    folder = folder_or_400(target, payload.get("folder_id"))

    copied = copy_file_into_project(
        source=source,
        target_project=target,
        folder=folder,
        name=payload.get("name_display") or source.name_display,
        request=request,
        audit=transfer_audit(
            request=request,
            source_project=project,
            target_project=target,
            folder=folder,
            operation=OPERATION_COPY,
            action=FileAccessLog.Action.COPIED,
        ),
    )

    return Response(
        {
            "file": serialize_file(copied.file),
            "source_file_id": str(source.id),
            "target_project_id": str(target.id),
            "copied_versions": len(copied.plan),
            "target_storage_usage": {
                "project_used_bytes": copied.usage.used_bytes,
                "limit_bytes": copied.usage.limit_bytes,
            },
        },
        status=status.HTTP_200_OK,
    )


def move_to_project(request, slug, project_id, file_id):
    """Move a file to another project: copy, verify, then purge the source (AC-42).

    The verification is the copy's own ``verify`` hook, so it happens before anything at
    the source changes: a copy that does not match its source is rolled back entirely
    and the refusal names the evidence. Only a verified copy lets the source go, and it
    goes through the trash first - the purge only acts on a trashed row, and that is
    also the state a failed purge must leave behind: recoverable, and reported rather
    than retried as a silent delete (R-OPS-4).
    """
    project = project_or_404(slug, project_id)
    require_project_editor(request, project)
    payload = transfer_payload(request)
    target = target_project_for(request, project, payload.get("target_project_id"))
    source = file_for_write(project, slug, file_id)
    folder = folder_or_400(target, payload.get("folder_id"))

    copied = copy_file_into_project(
        source=source,
        target_project=target,
        folder=folder,
        name=payload.get("name_display") or source.name_display,
        request=request,
        audit=transfer_audit(
            request=request,
            source_project=project,
            target_project=target,
            folder=folder,
            operation=OPERATION_MOVE,
            action=FileAccessLog.Action.MOVED,
        ),
        verify=verify_copied_objects,
    )

    trash_file_row(source, request=request, project=project)

    if not run_purge(source, request=request, trigger=OPERATION_MOVE):
        raise ProjectFileError(
            f"The file was copied to {target.name}, but its source could not be deleted; it stays in this "
            "project's trash and the deletion will be retried.",
            code="storage_unavailable",
            status_code=status.HTTP_502_BAD_GATEWAY,
            source_file_id=str(source.id),
            source_status=source.status,
            target_project_id=str(target.id),
            target_file_id=str(copied.file.id),
        )

    return Response(
        {
            "file": serialize_file(copied.file),
            "source_file_id": str(source.id),
            "target_project_id": str(target.id),
            "copied_versions": len(copied.plan),
            "source_purged": True,
        },
        status=status.HTTP_200_OK,
    )


class FileCopyToProjectEndpoint(BaseAPIView):
    """Copy a file into another project of the same workspace (ARCH-001 §4.1).

    Shares the project-file upload throttle: a cross-project copy duplicates bytes and
    charges the target's quota, so it belongs to the same budget as an upload.
    """

    throttle_classes = [ProjectFileUploadThrottle]

    def post(self, request, slug, project_id, file_id):
        return copy_to_project(request, slug, project_id, file_id)


class FileMoveToProjectEndpoint(BaseAPIView):
    """Move a file to another project: copy, verify the copy, purge the source (AC-42)."""

    throttle_classes = [ProjectFileUploadThrottle]

    def post(self, request, slug, project_id, file_id):
        return move_to_project(request, slug, project_id, file_id)
